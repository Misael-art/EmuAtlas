"""Session-owned LAN controllers, paired by a temporary QR invitation.

The HTTP process owns only its virtual source pads. The existing input router
assigns them to players and applies the normal emulator/focus/exit contracts.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

REMOTE_PRODUCT = 0xE201
STALE_SECONDS = .75
RECONNECT_SECONDS = 60
INVITE_SECONDS = 120
BUTTONS = ('a', 'b', 'x', 'y', 'leftshoulder', 'rightshoulder', 'back',
           'start', 'guide', 'leftstick', 'rightstick', 'dpup', 'dpdown', 'dpleft', 'dpright')
AXES = ('leftx', 'lefty', 'rightx', 'righty', 'lefttrigger', 'righttrigger')
RAW_MAPPING = dict(zip(BUTTONS[:11], ('b0', 'b1', 'b3', 'b2', 'b4', 'b5',
                                   'b6', 'b7', 'b8', 'b9', 'b10')),
                   leftx='a0', lefty='a1', rightx='a3', righty='a4',
                   lefttrigger='a2', righttrigger='a5',
                   dpup='h0.1', dpdown='h0.4', dpleft='h0.8', dpright='h0.2')


def neutral_state():
    return {'buttons': [], 'axes': dict.fromkeys(AXES, 0)}


def check_input_support():
    try:
        importlib.import_module('evdev')
    except ImportError:
        raise RuntimeError('Controles móveis exigem Python evdev nas dependências do host.') from None
    if not os.access('/dev/uinput', os.R_OK | os.W_OK):
        raise RuntimeError('A sessão precisa de acesso a /dev/uinput para criar o controle virtual.')


def validate_state(value):
    if not isinstance(value, dict):
        raise ValueError('Estado do controle inválido.')
    buttons, axes = value.get('buttons'), value.get('axes')
    if (not isinstance(buttons, list) or len(buttons) > len(BUTTONS)
            or any(b not in BUTTONS for b in buttons)
            or not isinstance(axes, dict) or set(axes) != set(AXES)):
        raise ValueError('Estado do controle inválido.')
    for key, number in axes.items():
        if (type(number) not in (int, float) or not math.isfinite(number)
                or not (0 if key.endswith('trigger') else -1) <= number <= 1):
            raise ValueError('Eixo do controle inválido.')
    return {'buttons': sorted(set(buttons)), 'axes': dict(axes)}


class VirtualSource:
    def __init__(self, identifier, number, name):
        import evdev
        from evdev import ecodes as e
        from controller_session import VID
        self.e = e
        self.button_codes = dict(zip(BUTTONS[:11], (e.BTN_SOUTH, e.BTN_EAST, e.BTN_WEST,
            e.BTN_NORTH, e.BTN_TL, e.BTN_TR, e.BTN_SELECT, e.BTN_START,
            e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR)))
        self.axis_codes = dict(zip(AXES, (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY, e.ABS_Z, e.ABS_RZ)))
        axes = [(code, evdev.AbsInfo(0, 0 if key.endswith('trigger') else -32768,
                 255 if key.endswith('trigger') else 32767, 0, 0, 0))
                for key, code in self.axis_codes.items()]
        axes.extend((code, evdev.AbsInfo(0, -1, 1, 0, 0, 0)) for code in (e.ABS_HAT0X, e.ABS_HAT0Y))
        self.pad = evdev.UInput({e.EV_KEY: list(self.button_codes.values()), e.EV_ABS: axes},
            name=f'EmuAtlas Mobile {number}', vendor=VID, product=REMOTE_PRODUCT,
            phys='emuatlas/mobile/' + identifier, max_effects=0)
        self.previous = {}

    def write(self, state):
        e, buttons = self.e, set(state['buttons'])
        values = {(e.EV_KEY, code): int(key in buttons) for key, code in self.button_codes.items()}
        values.update({(e.EV_ABS, code): round(state['axes'][key] * (255 if key.endswith('trigger') else 32767))
                       for key, code in self.axis_codes.items()})
        values[e.EV_ABS, e.ABS_HAT0X] = int('dpright' in buttons) - int('dpleft' in buttons)
        values[e.EV_ABS, e.ABS_HAT0Y] = int('dpdown' in buttons) - int('dpup' in buttons)
        for key, value in values.items():
            if self.previous.get(key) != value:
                self.pad.write(*key, value)
        self.pad.syn()
        self.previous = values

    def close(self):
        try:
            self.write(neutral_state())
        finally:
            self.pad.close()


class Hub:
    def __init__(self, capacity, sink_factory=VirtualSource, clock=time.monotonic):
        self.capacity, self.sink_factory, self.clock = capacity, sink_factory, clock
        self.lock = threading.RLock()
        self.clients, self.owners = {}, {}
        self.active_owners = {}
        self.invite, self.invite_until = '', 0
        self.enabled = False

    def invitation(self):
        with self.lock:
            self.invite = secrets.token_urlsafe(32)
            self.invite_until = self.clock() + INVITE_SECONDS
            return self.invite

    def join(self, invite, name):
        with self.lock:
            if not isinstance(invite, str) or not secrets.compare_digest(invite, self.invite) or self.clock() >= self.invite_until:
                raise ValueError('QR Code expirado. Abra Adicionar controle novamente.')
            self.sweep()
            if len(self.clients) >= self.capacity:
                raise ValueError('Todas as vagas de controles móveis estão ocupadas.')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32 or any(ord(c) < 32 for c in name):
                raise ValueError('Use um nome de 1 a 32 caracteres.')
            identifier, token = secrets.token_hex(16), secrets.token_urlsafe(32)
            used = {c['number'] for c in self.clients.values()}
            number = next(n for n in range(1, self.capacity + 1) if n not in used)
            sink = self.sink_factory(identifier, number, name.strip())
            from controller_session import VID
            controller_id = hashlib.sha256(f'{VID:04x}:{REMOTE_PRODUCT:04x}:emuatlas/mobile/{identifier}'.encode()).hexdigest()
            self.clients[token] = {'id': identifier, 'controller_id': controller_id,
                'name': name.strip(), 'number': number, 'sink': sink, 'last': self.clock(),
                'sequence': -1, 'player': None, 'request': None, 'error': '', 'waiting_release': True,
                'last_state': neutral_state(), 'rate_at': self.clock(), 'budget': 12.0}
            return {'token': token, **self.status(self.clients[token])}

    def client(self, token):
        client = self.clients.get(token)
        if client is None:
            raise PermissionError('Controle desconectado. Escaneie um QR Code atual.')
        return client

    def status(self, client):
        return {'id': client['id'], 'name': client['name'], 'player': client['player'],
                'capacity': self.capacity, 'enabled': self.enabled, 'error': client['error']}

    def input(self, token, value):
        state = validate_state(value)
        sequence = value.get('sequence')
        if type(sequence) is not int or not 0 <= sequence <= 2**53 - 1:
            raise ValueError('Sequência inválida.')
        with self.lock:
            c, now = self.client(token), self.clock()
            c['budget'] = min(12, c['budget'] + (now-c['rate_at'])*60)
            c['rate_at'] = now
            if c['budget'] < 1:
                raise ValueError('Muitos comandos. Aguarde a próxima atualização.')
            c['budget'] -= 1
            if sequence <= c['sequence']:
                return self.status(c)
            c['sequence'], c['last'] = sequence, now
            c['last_state'] = state
            if not state['buttons'] and not any(state['axes'].values()):
                c['waiting_release'] = False
            c['sink'].write(state if self.enabled and not c['waiting_release'] else neutral_state())
            return self.status(c)

    def choose_player(self, token, player):
        with self.lock:
            c = self.client(token)
            if type(player) is not int or not 0 <= player < self.capacity:
                raise ValueError('Jogador inválido.')
            c['request'] = {'id': secrets.token_hex(8), 'controller_id': c['controller_id'], 'player': player}
            c['error'] = ''
            return self.status(c)

    def sync(self, owner, active, players, acknowledgements):
        with self.lock:
            self.owners[owner] = self.clock()
            # Only the focused session acknowledges assignment and exposes requests.
            if active:
                for c in self.clients.values():
                    c['player'] = players.get(c['controller_id'])
                    if c['request'] and c['request']['id'] in acknowledgements:
                        c['error'] = acknowledgements[c['request']['id']] or ''
                        c['request'] = None
            self.active_owners[owner] = bool(active)
            self.set_enabled(any(self.active_owners.values()))
            return {'clients': [{'controller_id': c['controller_id'], 'name': c['name'],
                                 'online': self.clock()-c['last'] <= STALE_SECONDS}
                                for c in self.clients.values()],
                    'requests': [c['request'] for c in self.clients.values() if c['request']] if active else []}

    def set_enabled(self, enabled):
        if self.enabled == enabled:
            return
        self.enabled = enabled
        for c in self.clients.values():
            c['sink'].write(neutral_state())
            c['waiting_release'] = True

    def leave(self, token):
        with self.lock:
            c = self.clients.pop(token, None)
            if c:
                c['sink'].close()

    def sweep(self):
        with self.lock:
            now = self.clock()
            for token, c in list(self.clients.items()):
                if now-c['last'] > RECONNECT_SECONDS:
                    self.leave(token)
                elif now-c['last'] > STALE_SECONDS and not c['waiting_release']:
                    c['sink'].write(neutral_state())
                    c['waiting_release'] = True
            for owner, last in list(self.owners.items()):
                if now-last > 4:
                    self.active_owners[owner] = False
            self.set_enabled(any(self.active_owners.values()))

    def close(self):
        with self.lock:
            for token in list(self.clients):
                self.leave(token)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, hub, admin_token, hosts, *, admin_only=False):
        self.hub, self.admin_token, self.hosts = hub, admin_token, set(hosts)
        self.admin_only = admin_only
        self.public_server = None
        self.workers = threading.BoundedSemaphore(4 if admin_only else max(16, hub.capacity*4+4))
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *_args):
        pass  # Invitations, browser addresses and session tokens stay private.

    def respond(self, value, status=200, content_type='application/json; charset=utf-8'):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def valid_origin(self):
        host = self.headers.get('Host', '')
        allowed = {f'{ip}:{self.server.server_port}' for ip in self.server.hosts}
        origin = self.headers.get('Origin')
        return host in allowed and (origin is None or origin == 'http://' + host)

    def do_GET(self):
        self.close_connection = True
        if not self.valid_origin() or not ipaddress.ip_address(self.client_address[0]).is_private:
            self.respond({'error': 'Origem inválida.'}, 403)
            return
        files = {'/': ('mobile-controller.html', 'text/html; charset=utf-8'),
                 '/mobile-controller.js': ('mobile-controller.js', 'text/javascript; charset=utf-8'),
                 '/mobile-controller.css': ('mobile-controller.css', 'text/css; charset=utf-8')}
        if self.server.admin_only or self.path not in files:
            self.respond({'error': 'Recurso ausente.'}, 404)
            return
        name, kind = files[self.path]
        self.respond(Path(__file__).with_name(name).read_bytes(), content_type=kind)

    def do_POST(self):
        try:
            if not self.valid_origin() or not ipaddress.ip_address(self.client_address[0]).is_private:
                self.close_connection = True
                raise PermissionError('Use a rede local do host.')
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.close_connection = True
                raise ValueError('Envie JSON.')
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 8192:
                self.close_connection = True
                raise ValueError('Mensagem fora do limite.')
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError('Mensagem inválida.')
            token = self.headers.get('Authorization', '').removeprefix('Bearer ')
            self.respond(self.dispatch(value, token))
        except PermissionError as exc:
            self.respond({'error': str(exc)}, 403)
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            self.respond({'error': str(exc)}, 400)
        except OSError:
            self.respond({'error': 'O dispositivo de entrada ficou indisponível.'}, 503)

    def dispatch(self, value, token):
        hub = self.server.hub
        if self.server.admin_only and not self.path.startswith('/admin/'):
            raise PermissionError('Canal administrativo local.')
        if self.path.startswith('/admin/'):
            if not ipaddress.ip_address(self.client_address[0]).is_loopback or not secrets.compare_digest(token, self.server.admin_token):
                raise PermissionError('Operação local protegida.')
            owner = value.get('owner')
            if self.path == '/admin/invite':
                addresses = lan_addresses()
                public = self.server.public_server or self.server
                public.hosts = {'127.0.0.1', 'localhost', *addresses}
                return {'invite': hub.invitation(), 'expires_seconds': INVITE_SECONDS, 'addresses': addresses}
            if not isinstance(owner, str) or len(owner) > 80:
                raise ValueError('Sessão local inválida.')
            if self.path == '/admin/sync':
                return hub.sync(owner, value.get('active') is True, value.get('players', {}), value.get('acknowledgements', {}))
            if self.path == '/admin/detach':
                with hub.lock:
                    hub.owners.pop(owner, None)
                    hub.active_owners.pop(owner, None)
                    hub.set_enabled(any(hub.active_owners.values()))
                return {'detached': True}
        if self.path == '/api/join':
            return hub.join(value.get('invite'), value.get('name'))
        if self.path == '/api/state':
            return hub.input(token, value)
        if self.path == '/api/player':
            return hub.choose_player(token, value.get('player'))
        if self.path == '/api/leave':
            hub.leave(token)
            return {'disconnected': True}
        raise ValueError('Ação indisponível.')


def lan_addresses():
    result = subprocess.run(['ip', '-j', '-4', 'address', 'show', 'scope', 'global'],
                            capture_output=True, text=True, timeout=2, check=True)
    addresses = []
    for interface in json.loads(result.stdout):
        for row in interface.get('addr_info', []):
            address = ipaddress.ip_address(row['local'])
            if address.is_private and not address.is_loopback and not address.is_link_local:
                addresses.append(str(address))
    return list(dict.fromkeys(addresses))


def runtime_directory(root):
    base = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
    if base.is_symlink() or base.stat().st_uid != os.getuid():
        raise RuntimeError('Diretório de sessão do usuário inválido.')
    path = base / ('emuatlas-mobile-' + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16])
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Diretório privado dos controles inválido.')
    return path


def request(info, action, data):
    payload = json.dumps(data).encode()
    port = info.get('admin_port', info['port'])
    req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/{action}", data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + info['admin_token']})
    # A desktop proxy must never receive the local control channel or its token.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=1) as response:
        return json.load(response)


class Client:
    """A local owner attaches lazily, preserving phones across frontend/game handoff."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.owner = secrets.token_hex(16)
        self.info = None
        self.last_sync = 0
        self.clients, self.acks = [], {}
        self.error = ''

    def discover(self, start=False):
        directory = runtime_directory(self.root)
        with (directory / 'startup.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if start else fcntl.LOCK_NB))
            except BlockingIOError:
                return  # The input/UI loop keeps running while pairing starts.
            path = directory / 'server.json'
            if path.is_symlink():
                raise RuntimeError('Descritor de sessão inválido.')
            if path.exists():
                if path.is_symlink() or path.stat().st_mode & 0o077:
                    raise RuntimeError('Descritor de sessão inválido.')
                info = json.loads(path.read_text())
                from controller_session import identity
                current = identity(info['pid'])
                if current and current[1] == info['start_ticks']:
                    request(info, 'sync', {'owner': self.owner, 'active': False})
                    self.info = info
                    return
                if start:
                    # A stale ephemeral descriptor must never direct a new
                    # administrator token request to an unrelated reused port.
                    path.unlink()
            if not start:
                return
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                              'serve', '--root', str(self.root), '--owner', self.owner],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic()+4
            while time.monotonic() < deadline:
                if path.exists():
                    info = json.loads(path.read_text())
                    try:
                        request(info, 'sync', {'owner': self.owner, 'active': False})
                    except (OSError, urllib.error.URLError):
                        time.sleep(.05)
                        continue
                    self.info = info
                    return
                time.sleep(.05)
            raise RuntimeError('Não foi possível abrir os controles móveis. Verifique evdev e /dev/uinput.')

    def pairing(self):
        if not shutil_which_qrencode():
            raise RuntimeError('QR Code exige qrencode, declarado nas dependências do projeto.')
        check_input_support()
        self.discover(start=True)
        result = request(self.info, 'invite', {})
        addresses = result['addresses']
        if not addresses:
            raise RuntimeError('Conecte o host a uma rede local para adicionar o celular.')
        urls = [f"http://{address}:{self.info['port']}/#" + result['invite'] for address in addresses]
        return {'urls': urls, 'expires_at': time.monotonic()+result['expires_seconds']}

    def sync(self, active, players, assign):
        now = time.monotonic()
        if now-self.last_sync < .35:
            return
        self.last_sync = now
        try:
            if self.info is None:
                self.discover()
            if self.info is None:
                return
            result = request(self.info, 'sync', {'owner': self.owner, 'active': active,
                'players': players, 'acknowledgements': self.acks})
            self.acks = {}
            self.clients = result['clients']
            for action in result['requests']:
                try:
                    assign(action['controller_id'], action['player'])
                    self.acks[action['id']] = ''
                except Exception as exc:
                    self.acks[action['id']] = str(exc)
            self.error = ''
        except (OSError, ValueError, urllib.error.URLError):
            self.error = 'Conexão local dos controles interrompida.'
            self.info, self.clients = None, []

    def close(self):
        if self.info:
            try:
                request(self.info, 'detach', {'owner': self.owner})
            except (OSError, urllib.error.URLError):
                pass
        self.info = None


def shutil_which_qrencode():
    import shutil
    return shutil.which('qrencode')


def qr_png(url):
    return subprocess.run([shutil_which_qrencode() or 'qrencode', '-s', '6', '-m', '4', '-o', '-'],
                          input=url.encode(), capture_output=True, check=True, timeout=3).stdout


def serve(root, owner):
    from controller_session import player_capacity, identity
    check_input_support()
    directory = runtime_directory(root)
    with (directory / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        addresses = lan_addresses()
        hub = Hub(player_capacity(root))
        hub.owners[owner] = time.monotonic()
        server = Server(('0.0.0.0', 0), hub, secrets.token_urlsafe(32), ['127.0.0.1', 'localhost', *addresses])
        admin = Server(('127.0.0.1', 0), hub, server.admin_token, ['127.0.0.1'], admin_only=True)
        admin.public_server = server
        info = {'pid': os.getpid(), 'start_ticks': identity(os.getpid())[1], 'port': server.server_port,
                'admin_port': admin.server_port,
                'addresses': addresses, 'admin_token': server.admin_token}
        path = directory / 'server.json'
        temp = directory / ('server-' + secrets.token_hex(8) + '.tmp')
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(info, stream)
        os.replace(temp, path)
        running = threading.Event()
        running.set()
        signal.signal(signal.SIGTERM, lambda *_: running.clear())
        signal.signal(signal.SIGINT, lambda *_: running.clear())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        admin_worker = threading.Thread(target=admin.serve_forever, daemon=True)
        worker.start()
        admin_worker.start()
        try:
            while running.is_set():
                hub.sweep()
                with hub.lock:
                    for key, last in list(hub.owners.items()):
                        if time.monotonic()-last > 10:
                            hub.owners.pop(key)
                            hub.active_owners.pop(key, None)
                    if not hub.owners:
                        break
                time.sleep(.1)
        finally:
            hub.close()
            server.shutdown()
            admin.shutdown()
            server.server_close()
            admin.server_close()
            worker.join(timeout=2)
            admin_worker.join(timeout=2)
            if path.exists() and json.loads(path.read_text()).get('pid') == os.getpid():
                path.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--owner', required=True)
    options = parser.parse_args()
    serve(options.root, options.owner)
