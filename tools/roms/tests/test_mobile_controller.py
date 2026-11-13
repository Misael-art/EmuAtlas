"""LAN protocol, player requests and release guarantees with isolated sinks."""
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mobile_controller as m


class Sink:
    def __init__(self, identifier, number, name):
        self.identifier, self.number, self.name = identifier, number, name
        self.states = []
        self.closed = False

    def write(self, state):
        self.states.append(state)

    def close(self):
        self.closed = True


class HubTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.hub = m.Hub(8, Sink, lambda: self.now)
        self.addCleanup(self.hub.close)
        self.invite = self.hub.invitation()

    def join(self, name='Celular'):
        return self.hub.join(self.invite, name)

    def input(self, token, sequence, buttons=()):
        return self.hub.input(token, {**m.neutral_state(), 'buttons': list(buttons), 'sequence': sequence})

    def test_eight_independent_clients_and_configured_capacity(self):
        rows = [self.join(str(n)) for n in range(8)]
        self.assertEqual(len({r['token'] for r in rows}), 8)
        self.hub.sync('owner', True, {}, {})
        for n, row in enumerate(rows):
            self.input(row['token'], 0)
            self.input(row['token'], 1, ['a'] if n == 7 else ['b'])
        self.assertEqual(self.hub.clients[rows[7]['token']]['sink'].states[-1]['buttons'], ['a'])
        self.assertEqual(self.hub.clients[rows[0]['token']]['sink'].states[-1]['buttons'], ['b'])
        with self.assertRaisesRegex(ValueError, 'ocupadas'):
            self.join('Nono')
        expanded = m.Hub(12, Sink)
        self.addCleanup(expanded.close)
        invitation = expanded.invitation()
        for n in range(12):
            expanded.join(invitation, str(n))
        self.assertEqual(len(expanded.clients), 12)

    def test_invitation_expiry_and_rotation_keep_paired_clients(self):
        row = self.join()
        self.now += m.INVITE_SECONDS
        with self.assertRaisesRegex(ValueError, 'expirado'):
            self.join()
        self.invite = self.hub.invitation()
        self.input(row['token'], 0)
        self.join('Tablet')
        self.assertEqual(len(self.hub.clients), 2)

    def test_loss_of_network_releases_and_requires_release_before_resume(self):
        row = self.join()
        token = row['token']
        self.hub.sync('owner', True, {}, {})
        self.input(token, 0)
        self.input(token, 1, ['start', 'a'])
        sink = self.hub.clients[token]['sink']
        self.now += m.STALE_SECONDS + .01
        self.hub.sweep()
        self.assertEqual(sink.states[-1], m.neutral_state())
        self.input(token, 2, ['a'])
        self.assertEqual(sink.states[-1], m.neutral_state())
        self.input(token, 3)
        self.input(token, 4, ['a'])
        self.assertEqual(sink.states[-1]['buttons'], ['a'])

    def test_replay_does_not_refresh_lease_or_overwrite_newer_state(self):
        row = self.join()
        token = row['token']
        self.hub.sync('owner', True, {}, {})
        self.input(token, 3)
        self.input(token, 5, ['b'])
        before = self.hub.clients[token]['last']
        self.now += .5
        self.input(token, 4, ['a'])
        self.assertEqual(self.hub.clients[token]['last'], before)
        self.assertEqual(self.hub.clients[token]['sink'].states[-1]['buttons'], ['b'])

    def test_focus_and_owner_lease_neutralize_virtual_source(self):
        row = self.join()
        token = row['token']
        self.hub.sync('frontend', True, {}, {})
        self.input(token, 0)
        self.input(token, 1, ['a'])
        self.hub.sync('game', True, {}, {})
        self.hub.sync('frontend', False, {}, {})
        self.assertTrue(self.hub.enabled)
        self.hub.sync('game', False, {}, {})
        self.assertFalse(self.hub.enabled)
        self.assertEqual(self.hub.clients[token]['sink'].states[-1], m.neutral_state())
        self.hub.sync('game', True, {}, {})
        self.now += 4.1
        self.hub.sweep()
        self.assertFalse(self.hub.enabled)

    def test_player_request_waits_for_focused_backend_acknowledgement(self):
        row = self.join()
        self.hub.choose_player(row['token'], 7)
        result = self.hub.sync('background', False, {}, {})
        self.assertEqual(result['requests'], [])
        result = self.hub.sync('game', True, {}, {})
        action = result['requests'][0]
        self.assertEqual(action['player'], 7)
        # A missing acknowledgement leaves the request available for retry.
        self.assertEqual(self.hub.sync('game', True, {}, {})['requests'][0], action)
        result = self.hub.sync('game', True, {action['controller_id']: 7}, {action['id']: ''})
        self.assertEqual(result['requests'], [])
        self.assertEqual(self.hub.client(row['token'])['player'], 7)

    def test_assignment_failure_is_returned_to_the_same_client(self):
        row = self.join()
        self.hub.choose_player(row['token'], 2)
        action = self.hub.sync('game', True, {}, {})['requests'][0]
        self.hub.sync('game', True, {}, {action['id']: 'Ordem alterada em outra janela'})
        self.assertIn('outra janela', self.input(row['token'], 0)['error'])

    def test_expired_or_disconnected_clients_release_the_slot_and_sink(self):
        row = self.join()
        sink = self.hub.client(row['token'])['sink']
        self.now += m.RECONNECT_SECONDS + 1
        self.hub.sweep()
        self.assertTrue(sink.closed)
        with self.assertRaises(PermissionError):
            self.input(row['token'], 0)
        self.hub.leave(row['token'])
        new = self.join()
        self.assertEqual(self.hub.client(new['token'])['number'], 1)

    def test_invalid_states_never_reach_the_sink(self):
        row = self.join()
        invalid = [None, [], {'buttons': ['shell'], 'axes': dict.fromkeys(m.AXES, 0)},
                   {'buttons': [], 'axes': dict.fromkeys(m.AXES, float('nan'))},
                   {'buttons': [], 'axes': dict.fromkeys(m.AXES, True)},
                   {'buttons': [], 'axes': {}}, {'buttons': [], 'axes': dict.fromkeys(m.AXES, 2)}]
        for state in invalid:
            with self.subTest(state=state), self.assertRaises((ValueError, AttributeError)):
                self.hub.input(row['token'], state)
        self.assertFalse(self.hub.client(row['token'])['sink'].states)

    def test_unknown_tokens_bad_players_and_rate_limit_are_rejected(self):
        with self.assertRaises(PermissionError):
            self.input('bad', 0)
        row = self.join()
        for player in (-1, 8, True, '1'):
            with self.assertRaises(ValueError):
                self.hub.choose_player(row['token'], player)
        for n in range(12):
            self.input(row['token'], n)
        with self.assertRaisesRegex(ValueError, 'Muitos comandos'):
            self.input(row['token'], 20)

    def test_pairing_checks_input_dependencies_before_starting_a_service(self):
        client = m.Client(Path.cwd())
        with patch.object(m, 'shutil_which_qrencode', return_value='/fixture/qrencode'), patch.object(
                m, 'check_input_support', side_effect=RuntimeError('uinput indisponível')), patch.object(client, 'discover') as discover:
            with self.assertRaisesRegex(RuntimeError, 'uinput'):
                client.pairing()
        discover.assert_not_called()

    def test_dead_service_descriptor_is_removed_before_starting_a_replacement(self):
        import controller_session
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'server.json'
            path.write_text(json.dumps({'pid':10,'start_ticks':'old','port':1,'admin_token':'expired'}))
            path.chmod(0o600)
            def spawn(*_args, **_kwargs):
                self.assertFalse(path.exists())
                path.write_text(json.dumps({'pid':20,'start_ticks':'new','port':2,'admin_token':'current'}))
                path.chmod(0o600)
            def request(info, *_args):
                self.assertEqual(info['pid'], 20)
                return {}
            client = m.Client(root)
            with patch.object(m, 'runtime_directory', return_value=root), patch.object(
                    controller_session, 'identity', return_value=None), patch.object(
                    m.subprocess, 'Popen', side_effect=spawn), patch.object(m, 'request', side_effect=request):
                client.discover(start=True)
            self.assertEqual(client.info['pid'], 20)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = m.Hub(8, Sink)
        cls.server = m.Server(('127.0.0.1', 0), cls.hub, 'local-secret', ['127.0.0.1'])
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)
        cls.hub.close()

    def call(self, path, data=None, token='', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        self.addCleanup(connection.close)
        request_headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
        request_headers.update(headers or {})
        connection.request('GET' if data is None else 'POST', path,
                           None if data is None else json.dumps(data), request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def test_ui_is_offline_same_origin_and_serves_only_explicit_files(self):
        status, headers, body = self.call('/')
        self.assertEqual(status, 200)
        self.assertIn(b'Controle virtual', body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for path in ('/mobile-controller.js', '/mobile-controller.css'):
            self.assertEqual(self.call(path)[0], 200)
        for path in ('/../README.md', '/server.json', '/admin/invite'):
            self.assertEqual(self.call(path)[0], 404)

    def test_admin_secret_and_origin_are_required(self):
        self.assertEqual(self.call('/admin/invite', {}, token='wrong')[0], 403)
        self.assertEqual(self.call('/admin/invite', {}, token='local-secret', headers={'Origin': 'http://evil.invalid'})[0], 403)
        with patch.object(m, 'lan_addresses', return_value=['192.168.1.2']):
            status, _, body = self.call('/admin/invite', {}, token='local-secret')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['invite'])
        self.assertEqual(json.loads(body)['addresses'], ['192.168.1.2'])

    def test_pair_input_player_and_disconnect_over_http(self):
        invite = self.hub.invitation()
        status, _, body = self.call('/api/join', {'invite': invite, 'name': 'Tablet'})
        self.assertEqual(status, 200)
        row = json.loads(body)
        self.hub.sync('test-owner', True, {}, {})
        state = {**m.neutral_state(), 'sequence': 1}
        self.assertEqual(self.call('/api/state', state, row['token'])[0], 200)
        self.assertEqual(self.call('/api/player', {'player': 7}, row['token'])[0], 200)
        self.assertEqual(self.call('/api/state', state, 'wrong')[0], 403)
        self.assertEqual(self.call('/api/leave', {}, row['token'])[0], 200)
        self.assertEqual(self.call('/api/state', state, row['token'])[0], 403)

    def test_admin_channel_has_its_own_listener_and_updates_public_addresses(self):
        admin = m.Server(('127.0.0.1', 0), self.hub, 'isolated-admin', ['127.0.0.1'], admin_only=True)
        admin.public_server = self.server
        worker = threading.Thread(target=admin.serve_forever, daemon=True)
        worker.start()
        try:
            info = {'port':self.server.server_port, 'admin_port':admin.server_port, 'admin_token':'isolated-admin'}
            with patch.object(m, 'lan_addresses', return_value=['192.168.1.3']):
                result = m.request(info, 'invite', {})
            self.assertEqual(result['addresses'], ['192.168.1.3'])
            self.assertIn('192.168.1.3', self.server.hosts)
            connection = http.client.HTTPConnection('127.0.0.1', admin.server_port, timeout=2)
            try:
                connection.request('GET', '/')
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
            finally:
                connection.close()
        finally:
            admin.shutdown()
            admin.server_close()
            worker.join(timeout=2)


class PageInputTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node necessário para o ensaio isolado do JavaScript')
    def test_short_taps_multitouch_and_blur_recovery(self):
        script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let now = 1000;
const sent = [];
const timers = [];
const windowEvents = {};
class Element {
  constructor(dataset = {}) {
    this.dataset = dataset; this.events = {}; this.style = {}; this.options = [];
    this.classList = {toggle() {}, remove() {}};
    this.knob = {style: {}};
  }
  addEventListener(type, callback) { this.events[type] = callback; }
  setPointerCapture() {}
  querySelector() { return this.knob; }
  getBoundingClientRect() { return {left:0, top:0, width:100, height:100}; }
  replaceChildren(...values) { this.options = values; }
  add(value) { this.options.push(value); }
}
const ids = new Map();
const element = id => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
const buttons = ['a','b'].map(button => new Element({button}));
const sticks = ['left','right'].map(stick => new Element({stick}));
const document = {
  hidden:false, documentElement:{}, addEventListener() {},
  getElementById:element,
  querySelectorAll(selector) {
    if (selector === '[data-button]') return buttons;
    if (selector === '[data-stick]') return sticks;
    if (selector === '.stick span') return sticks.map(s => s.knob);
    return [];
  }
};
const storage = new Map([['emuatlas-controller-token','paired']]);
const context = vm.createContext({document, navigator:{}, history:{replaceState(){}},
  location:{hash:'',pathname:'/'}, Option:class {}, AbortController,
  window:{addEventListener(type, callback){windowEvents[type]=callback;}},
  sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
  Date:{now:()=>now}, setInterval:callback=>timers.push(callback), setTimeout:()=>1, clearTimeout(){},
  fetch:async (_url, options)=>{
    sent.push(JSON.parse(options.body));
    return {ok:true,json:async()=>({name:'QA',player:0,capacity:8,enabled:true,error:''})};
  }
});
const flush = () => new Promise(resolve => setImmediate(resolve));
const event = (id,x=50,y=50) => ({pointerId:id,clientX:x,clientY:y,preventDefault(){}});
async function step(time) {now=time;timers[0]();await flush();}
(async()=>{
  vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
  await flush();
  buttons[0].events.pointerdown(event(1));
  buttons[0].events.pointerup(event(1));
  await step(1040); assert.deepEqual(sent.at(-1).buttons,['a']);
  await step(1080); assert.deepEqual(sent.at(-1).buttons,[]);
  now=1200;
  buttons[0].events.pointerdown(event(1));buttons[1].events.pointerdown(event(2));
  await flush();await step(1240);assert.deepEqual(sent.at(-1).buttons,['a','b']);
  buttons[0].events.pointercancel(event(1));await step(1280);assert.deepEqual(sent.at(-1).buttons,['b']);
  buttons[1].events.lostpointercapture(event(2));await step(1320);assert.deepEqual(sent.at(-1).buttons,[]);
  sticks[0].events.pointerdown(event(3,90,50));sticks[1].events.pointerdown(event(4,90,50));
  await step(1360);assert.equal(sent.at(-1).axes.leftx,1);assert.equal(sent.at(-1).axes.rightx,1);
  windowEvents.blur();await flush();
  sticks[0].events.pointerdown(event(5,90,50));await step(1420);
  assert.equal(sent.at(-1).axes.leftx,1);assert.equal(sent.at(-1).axes.rightx,0);
  sticks[0].events.pointercancel(event(5));await step(1460);assert.equal(sent.at(-1).axes.leftx,0);
  console.log('short_taps_multitouch_blur_passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-', str(Path(m.__file__).with_name('mobile-controller.js'))],
                                input=script, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('short_taps_multitouch_blur_passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
