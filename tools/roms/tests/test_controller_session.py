"""Routing state and boundaries without touching any host input device."""
import importlib.util
import os
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller_session as c

FEEDBACK_PATH = Path(__file__).resolve().parents[3] / 'frontend/RetroFE/Tools/launchers/retrofe/session-feedback.py'
spec = importlib.util.spec_from_file_location('feedback_test', FEEDBACK_PATH)
feedback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback)


class FeedbackTests(unittest.TestCase):
    def hud(self):
        hud = feedback.SessionFeedback.__new__(feedback.SessionFeedback)
        hud.owns_master = False
        hud.toast, hud.hint = Mock(), Mock()
        hud.toast.winfo_viewable.return_value = False
        hud.toast.winfo_pointerxy.return_value = (0, 0)
        hud.animation = hud.highlight = hud.drag = hud.press = hud.picker = hud.selected = None
        hud.highlight_until = 0
        hud.focused = False
        hud.pending = True
        hud.toast_started = None
        hud.signature = None
        hud.drawn = None
        hud.draw = Mock()
        hud.draw_body = Mock()
        return hud

    def test_four_slots_and_keyboard_fallback_are_explicit(self):
        self.assertEqual(feedback.player_rows(['Deck','GameSir']),
                         [('P1','Deck'),('P2','GameSir'),('P3','Sem controle'),('P4','Sem controle')])
        self.assertEqual(feedback.player_rows([])[0], ('P1','Teclado'))
        self.assertEqual(feedback.player_rows([], False)[0], ('P1','Sem controle'))

    def test_hint_persists_after_toast_expires_and_hotplug_reopens_toast(self):
        hud = self.hud()
        area = (1920, 0, 1280, 800)
        hud.set_players(['One'], identity=('event1',))
        hud.update(True, area, 10)
        hud.update(True, area, 16)
        hud.toast.withdraw.assert_called_once()
        self.assertEqual(hud.hint.deiconify.call_count, 2)
        hud.set_players(['One','Two'], identity=('event1','event2'))
        hud.update(True, area, 17)
        self.assertEqual(hud.toast_started, 17)
        self.assertEqual(hud.toast.deiconify.call_count, 2)

    def test_same_names_with_changed_device_identity_notify_again(self):
        hud = self.hud()
        hud.set_players(['Gamepad'], identity=('old',))
        hud.pending = False
        hud.set_players(['Gamepad'], identity=('new',))
        self.assertTrue(hud.pending)

    def test_focus_loss_hides_both_and_pending_assignment_waits_for_return(self):
        hud = self.hud()
        hud.set_players(['Pad'])
        hud.update(False, (0,0,1280,800), 10)
        hud.toast.withdraw.assert_called_once()
        hud.hint.withdraw.assert_called_once()
        self.assertIsNone(hud.toast_started)
        hud.update(True, (0,0,1280,800), 20)
        self.assertEqual(hud.toast_started, 20)

    def test_geometry_changes_reposition_without_restarting_timer(self):
        hud = self.hud()
        hud.update(True, (0,0,1280,800), 0)
        hud.update(True, (1920,0,1280,800), 1)
        self.assertEqual(hud.draw.call_count, 2)
        self.assertEqual(hud.toast_started, 0)

    def test_legend_matches_actual_held_chord(self):
        self.assertIn('SELECT / BACK + START', feedback.exit_hint(True)[0])
        self.assertIn('2 s', feedback.exit_hint(True)[1])
        self.assertIn('Ctrl + Shift + F10', feedback.KEYBOARD_EXIT_HINT)
        self.assertIn('F10', feedback.exit_hint(False)[0])

    def test_hover_keeps_the_card_visible_and_opaque(self):
        hud = self.hud()
        hud.pending = False
        hud.focused = True
        hud.toast_started = 0
        hud.toast.winfo_viewable.return_value = True
        hud.toast.winfo_pointerxy.return_value = (10, 10)
        hud.toast.winfo_rootx.return_value = hud.toast.winfo_rooty.return_value = 0
        hud.toast.winfo_width.return_value = hud.toast.winfo_height.return_value = 500
        hud.update(True, (0, 0, 1280, 800), 20)
        hud.toast.attributes.assert_called_with('-alpha', 1)

    def test_drop_waits_for_backend_acknowledgement(self):
        hud = self.hud()
        hud.devices = {'a': {'name': 'Pad A', 'routable': True}}
        hud.models = {'generic': {'label': 'Controle'}}
        hud.ports = ['a', None, None, None]
        hud.scale = 1
        hud.row_height = 80
        def accepted(key, player):
            hud.ports = [None, None, key, None]
        hud.on_assign = Mock(side_effect=accepted)
        hud.apply_drop('a', 2, (40, 50))
        hud.on_assign.assert_called_once_with('a', 2)
        self.assertEqual(hud.highlight, 2)
        self.assertIn('P3', hud.notice)
        self.assertEqual(hud.ports, [None, None, 'a', None])

    def test_failed_drop_keeps_the_acknowledged_ports(self):
        hud = self.hud()
        hud.devices = {'a': {'name': 'Pad A', 'routable': True}}
        hud.ports = ['a', None, None, None]
        hud.on_assign = Mock(side_effect=ValueError('Ordem alterada'))
        hud.apply_drop('a', 2, (40, 50))
        self.assertEqual(hud.ports, ['a', None, None, None])
        self.assertEqual(hud.notice, 'Ordem alterada')

    def test_disconnection_cancels_an_active_drag(self):
        hud = self.hud()
        hud.drag = {'id': 'a', 'position': (5, 5)}
        hud.set_devices([], [])
        self.assertIsNone(hud.drag)
        self.assertIn('desconectado', hud.notice)

    def test_models_match_history_usb_ids_and_explicit_identification(self):
        models = json.loads((FEEDBACK_PATH.parents[3] / 'Config/controller-artwork.json').read_text())['models']
        self.assertEqual(feedback.controller_model({'name': 'Gamesir-T4pro_2980'}, models), 'gamesir-t4-pro')
        self.assertEqual(feedback.controller_model({'name': '8BitDo Ultimate 2C Wireless'}, models), '8bitdo-ultimate-2c')
        self.assertEqual(feedback.controller_model({'name': 'Wireless Controller', 'vendor': 0x054c, 'product': 0x0ce6}, models), 'dualsense')
        self.assertEqual(feedback.controller_model({'name': 'Xbox 360', 'model_override': 'gamesir-t4-pro'}, models), 'gamesir-t4-pro')
        self.assertEqual(feedback.controller_model({'name': 'Unknown'}, models), 'generic')


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        import romtool
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        operations = self.root / 'outputs/operations'
        operations.mkdir(parents=True)
        for name, value in [('PROJECT_ROOT', self.root), ('OPS_ROOT', operations)]:
            mocked = patch.object(romtool, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.events = patch.object(romtool, 'append_event').start()
        self.addCleanup(patch.stopall)
        self.devices = [{'path': 'event1', 'controller_id': 'pad-a', 'name': 'A', 'kind': 'external'},
                        {'path': 'event2', 'controller_id': 'pad-b', 'name': 'B', 'kind': 'external'}]

    def test_one_player_can_move_to_p3_and_keep_p1_empty_after_reload(self):
        prefs = c.assign_player(self.root, self.devices[:1], ['event1'], 'pad-a', 2, 0)
        self.assertEqual(prefs['slots'], [None, None, 'pad-a', None])
        self.assertEqual(c.load_preferences(self.root), prefs)
        self.assertEqual(c.slots(self.devices[:1], True, preferred=prefs['slots']), [None, None, 'event1', None])

    def test_occupied_target_swaps_players_and_disconnect_preserves_surviving_port(self):
        prefs = c.assign_player(self.root, self.devices, ['event1', 'event2'], 'pad-a', 1, 0)
        self.assertEqual(prefs['slots'], ['pad-b', 'pad-a', None, None])
        self.assertEqual(c.slots(self.devices[:1], True, preferred=prefs['slots']), [None, 'event1', None, None])

    def test_reconnected_device_uses_stable_identity_and_new_device_fills_free_port(self):
        prefs = c.assign_player(self.root, self.devices[:1], ['event1'], 'pad-a', 2, 0)
        current = [{**self.devices[0], 'path': 'event9'}, self.devices[1]]
        self.assertEqual(c.slots(current, True, preferred=prefs['slots']), ['event2', None, 'event9', None])

    def test_disconnected_or_unmapped_source_cannot_change_preferences(self):
        for rows in [[], [{**self.devices[0], 'routable': False}]]:
            with self.assertRaises(ValueError):
                c.assign_player(self.root, rows, [], 'pad-a', 1, 0)
        self.assertFalse((self.root / c.ORDER_PATH).exists())

    def test_stale_window_revision_cannot_overwrite_the_new_order(self):
        c.assign_player(self.root, self.devices, ['event1', 'event2'], 'pad-a', 2, 0)
        before = (self.root / c.ORDER_PATH).read_bytes()
        with self.assertRaisesRegex(ValueError, 'outra janela'):
            c.assign_player(self.root, self.devices, ['event1', 'event2'], 'pad-a', 1, 0)
        self.assertEqual((self.root / c.ORDER_PATH).read_bytes(), before)

    def test_model_choice_is_persistent_and_keeps_player_ports(self):
        prefs = c.assign_player(self.root, self.devices, ['event1', 'event2'], 'pad-a', 2, 0)
        result = c.identify_controller(self.root, 'pad-a', 'gamesir-t4-pro', prefs['revision'])
        self.assertEqual(result['slots'], prefs['slots'])
        self.assertEqual(c.load_preferences(self.root)['models'], {'pad-a': 'gamesir-t4-pro'})

    def test_live_scan_neutralizes_all_ports_and_applies_the_actual_p3_mapping(self):
        c.assign_player(self.root, self.devices[:1], ['event1'], 'pad-a', 2, 0)
        session = c.Session.__new__(c.Session)
        session.root = self.root
        session.preferences = {'slots': None, 'models': {}, 'revision': 0}
        session.inputs = {'event1': Mock()}
        session.source_maps = {'event1': {'a': 'b0'}}
        session.source_signature = (('event1', 'external', 'pad-a', None),)
        session.assigned = ['event1']
        session.deck_identity = None
        session.keyboard_down = {1}
        session.keyboard_state = {'event1': {1}}
        session.exit_chords = {'event1': c.HeldChord()}
        session.manifest = {'docked': True, 'changes': []}
        session.stop_effects = Mock()
        session.neutral = Mock()
        session.forward = Mock()
        session.save = Mock()
        session.feedback = None
        with patch.object(c, 'input_inventory', return_value=self.devices[:1]):
            session.scan(True)
        self.assertEqual(session.assigned, [None, None, 'event1', None])
        self.assertEqual(session.neutral.call_args_list, [unittest.mock.call(n) for n in range(4)])
        session.forward.assert_called_once_with(session.inputs['event1'], 2)
        self.assertEqual(session.manifest['changes'][0]['players'], [{'player': 3, 'name': 'A'}])
        self.assertFalse(session.keyboard_down)
        self.assertFalse(session.exit_chords)
        old_device = session.inputs['event1']
        new_device = Mock()
        session.evdev = Mock()
        session.evdev.InputDevice.return_value = new_device
        session.neutral.reset_mock()
        session.forward.reset_mock()
        reconnected = {**self.devices[0], 'device_cookie': 99}
        with patch.object(c, 'input_inventory', return_value=[reconnected]), patch.object(
                c, 'source_gamepads', return_value=[{'path': 'event1', 'mapping': {'a': 'b0'}}]):
            session.scan(True)
        old_device.close.assert_called_once()
        session.forward.assert_called_once_with(new_device, 2)
        self.assertEqual(session.neutral.call_count, 4)
        self.assertEqual(session.assigned, [None, None, 'event1', None])

    def test_journal_failure_restores_existing_configuration(self):
        c.assign_player(self.root, self.devices, ['event1', 'event2'], 'pad-a', 2, 0)
        before = (self.root / c.ORDER_PATH).read_bytes()
        self.events.side_effect = OSError('journal unavailable')
        with self.assertRaises(OSError):
            c.assign_player(self.root, self.devices, ['event1', 'event2'], 'pad-a', 1, 1)
        self.assertEqual((self.root / c.ORDER_PATH).read_bytes(), before)

    def test_invalid_or_symlink_configuration_is_preserved(self):
        path = self.root / c.ORDER_PATH
        path.parent.mkdir(parents=True)
        path.write_text('[]')
        with self.assertRaises(ValueError):
            c.load_preferences(self.root)
        path.unlink()
        other = self.root / 'personal.json'
        other.write_text('personal')
        path.symlink_to(other)
        with self.assertRaises(ValueError):
            c.load_preferences(self.root)
        self.assertEqual(other.read_text(), 'personal')


def device(path, kind='external'):
    return {'path':path,'kind':kind}


class BackendEntryTests(unittest.TestCase):
    def test_navigation_resolves_backend_dependencies_in_an_isolated_python(self):
        project = FEEDBACK_PATH.parents[5]
        code = """from pathlib import Path
import importlib.util, sys, tempfile
root = Path.cwd()
directory = root / 'frontend/RetroFE/Tools/launchers/retrofe'
sys.path.insert(0, str(directory))
spec = importlib.util.spec_from_file_location('panel_entry', directory / 'navigation-panel.py')
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)
backend = panel.controller_backend(root / 'frontend/RetroFE')
with tempfile.TemporaryDirectory() as directory:
    assert backend.load_preferences(Path(directory))['revision'] == 0
"""
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], cwd=project,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class RoutingTests(unittest.TestCase):
    def test_reassignment_waits_for_release_then_routes_new_presses_to_p3(self):
        from evdev import ecodes as e
        session = c.Session.__new__(c.Session)
        session.e = e
        session.source_maps = {'event1': {'a': 'b0'}}
        session.wait_for_release = {'event1'}
        session.virtual = [Mock() for _ in range(4)]
        session.neutral = Mock()
        session.write = Mock()
        device = Mock(path='event1')
        device.capabilities.return_value = {e.EV_KEY: [e.BTN_SOUTH]}
        device.active_keys.return_value = [e.BTN_SOUTH]
        session.forward(device, 2)
        session.write.assert_not_called()
        self.assertIn('event1', session.wait_for_release)
        device.active_keys.return_value = []
        session.forward(device, 2)
        self.assertNotIn('event1', session.wait_for_release)
        device.active_keys.return_value = [e.BTN_SOUTH]
        session.forward(device, 2)
        session.write.assert_any_call(2, e.EV_KEY, e.BTN_SOUTH, 1)

    def test_preview_and_game_select_unique_mapping_by_path_before_vendor(self):
        row = {'path':'event1','vendor':1,'product':2}
        pads = [{'path':'event1','vendor':1,'product':2,'mapping':{'a':'b0'}},
                {'path':'event2','vendor':1,'product':2,'mapping':{'a':'b3'}}]
        self.assertEqual(c.source_mapping(row,pads), {'a':'b0'})
        self.assertIsNone(c.source_mapping({**row,'path':'other'},pads))
        self.assertEqual(c.source_mapping({**row,'path':'other'},pads[:1]), {'a':'b0'})

    def test_dock_keyboard_drives_p1_then_yields_to_hotplug_and_neutralizes_on_blur(self):
        from evdev import ecodes as e
        s=c.Session.__new__(c.Session)
        s.e=e;s.last_tick=0;s.last_process_scan=s.last_scan=2;s.focused_seconds=0
        s.processes=Mock();s.processes.contains.return_value=True
        s.focus=Mock(current=(123,'game',False,'DP-1'))
        s.last_focus=True;s.exit_pending=s.was_exit_dialog=False;s.dialog_down=set()
        s.focus_retry_key=None;s.focus_retries=0;s.last_focus_retry=0
        s.manifest={'owned_window_seen':True};s.inputs={};s.virtual=[Mock() for _ in range(4)]
        s.keyboard_down={e.KEY_Z};s.keyboard_state={};s.assigned=[];s.exit_chords={}
        s.keyboard_fallback=True;s.ra_keyboard=None;s.output_state=[{} for _ in range(4)]
        s.write=Mock();s.neutral=Mock();s.stop_effects=Mock()
        with patch.object(c.time,'monotonic',return_value=1),patch.object(c.select,'select',return_value=([],[],[])):
            s.tick()
            s.write.assert_any_call(0,e.EV_KEY,e.BTN_SOUTH,1)
            s.write.reset_mock();s.assigned=['external']
            s.tick();s.write.assert_not_called()
            s.processes.contains.return_value=False
            s.tick()
        self.assertEqual(s.neutral.call_count,4)
        self.assertEqual(s.keyboard_down,set())
        s.stop_effects.assert_called_once()

    def test_sdl_tokens_cover_buttons_hats_axes_half_axes_and_inversion(self):
        self.assertEqual(c.binding_value('b3',{3},{},{}),1)
        self.assertEqual(c.binding_value('h0.8',set(),{},{0:9}),1)
        self.assertEqual(c.binding_value('a0',set(),{0:-.75},{}),-.75)
        self.assertEqual(c.binding_value('a0~',set(),{0:-.75},{}),.75)
        self.assertEqual(c.binding_value('-a0',set(),{0:-.75},{}),.75)
        self.assertEqual(c.binding_value('+a0',set(),{0:-.75},{}),0)

    def test_xbox_and_sony_raw_x_buttons_reach_the_same_virtual_button(self):
        from evdev import ecodes as e,AbsInfo
        for raw,active in [('b2',e.BTN_NORTH),('b3',e.BTN_WEST)]:
            s=c.Session.__new__(c.Session)
            s.e=e;s.source_maps={'test':{'x':raw}}
            s.virtual=[Mock()]
            s.write=Mock()
            d=Mock(path='test')
            d.capabilities.return_value={e.EV_KEY:[e.BTN_SOUTH,e.BTN_EAST,e.BTN_NORTH,e.BTN_WEST],
                e.EV_ABS:[(e.ABS_X,AbsInfo(0,-32768,32767,0,0,0))]}
            d.absinfo.return_value=AbsInfo(0,-32768,32767,0,0,0)
            d.active_keys.return_value=[active]
            s.forward(d,0)
            s.write.assert_any_call(0,e.EV_KEY,e.BTN_WEST,1)
            s.write.assert_any_call(0,e.EV_KEY,e.BTN_NORTH,0)

    def test_live_axes_and_dpad_override_the_cached_opening_state(self):
        from evdev import ecodes as e, AbsInfo
        s = c.Session.__new__(c.Session)
        s.e = e
        s.source_maps = {'test': {'leftx': 'a0', 'lefty': 'a1', 'dpright': 'h0.2',
                                  'dpleft': 'h0.8', 'dpup': 'h0.1', 'dpdown': 'h0.4'}}
        s.virtual = [Mock()]
        s.write = Mock()
        d = Mock(path='test')
        centered = AbsInfo(128, 0, 255, 0, 0, 0)
        neutral_hat = AbsInfo(0, -1, 1, 0, 0, 0)
        d.capabilities.return_value = {e.EV_ABS: [(e.ABS_X, centered), (e.ABS_Y, centered),
                                                (e.ABS_HAT0X, neutral_hat), (e.ABS_HAT0Y, neutral_hat)]}
        d.active_keys.return_value = []
        live = {e.ABS_X: centered._replace(value=255), e.ABS_Y: centered._replace(value=0),
                e.ABS_HAT0X: neutral_hat._replace(value=1), e.ABS_HAT0Y: neutral_hat._replace(value=-1)}
        d.absinfo.side_effect = live.__getitem__
        s.forward(d, 0)
        s.write.assert_any_call(0, e.EV_ABS, e.ABS_X, 32767)
        s.write.assert_any_call(0, e.EV_ABS, e.ABS_Y, -32767)
        s.write.assert_any_call(0, e.EV_ABS, e.ABS_HAT0X, 1)
        s.write.assert_any_call(0, e.EV_ABS, e.ABS_HAT0Y, -1)
        s.write.reset_mock()
        live.update({e.ABS_X: centered, e.ABS_Y: centered,
                     e.ABS_HAT0X: neutral_hat, e.ABS_HAT0Y: neutral_hat})
        s.forward(d, 0)
        s.write.assert_any_call(0, e.EV_ABS, e.ABS_HAT0X, 0)
        s.write.assert_any_call(0, e.EV_ABS, e.ABS_HAT0Y, 0)

    def test_handheld_deck_is_player_one(self):
        self.assertEqual(c.slots([device('b'),device('deck','deck'),device('a')],False), ['deck','a','b'])

    def test_dock_never_assigns_builtin_as_player_one(self):
        self.assertEqual(c.slots([device('deck','deck')],True), [])

    def test_dock_external_replaces_keyboard_without_relaunch(self):
        self.assertEqual(c.slots([],True), [])
        self.assertEqual(c.slots([device('new')],True), ['new'])

    def test_reconnect_fills_four_native_ports_in_stable_order(self):
        previous = ['b','a','c','d']
        self.assertEqual(c.slots([device(x) for x in ['a','b','c','d','e']],True,previous),previous)
        self.assertEqual(c.slots([device(x) for x in ['a','c','d','e']],True,previous),['a','c','d','e'])

    def test_keyboards_and_ambiguous_virtual_pads_are_not_players(self):
        self.assertEqual(c.slots([device('kbd','keyboard'),device('virtual','deck_virtual')],False),[])

    def test_dock_uses_active_output_not_connected_cable(self):
        self.assertFalse(c.dock_mode('eDP-1'))
        self.assertFalse(c.dock_mode(''))
        self.assertTrue(c.dock_mode('DP-1'))
        self.assertTrue(c.dock_mode('HDMI-A-1'))


class ExitTests(unittest.TestCase):
    def test_native_dialog_uses_normalized_button_once_and_releases_virtual_key(self):
        from evdev import ecodes as e
        s=c.Session.__new__(c.Session)
        s.e=e;s.source_maps={'test':{'a':'b3'}}
        s.exit_pending=s.dialog_armed=True
        s.focus=Mock(current=(123,'dialog',True,'DP-1'))
        s.processes=Mock();s.processes.contains.return_value=True
        s.dialog_down=set();s.dialog_keyboard=Mock();s.neutral=Mock()
        d=Mock(path='test')
        d.capabilities.return_value={e.EV_KEY:[e.BTN_SOUTH,e.BTN_EAST,e.BTN_NORTH,e.BTN_WEST]}
        d.active_keys.return_value=[e.BTN_WEST]
        s.forward(d,0);s.forward(d,0)
        self.assertEqual(s.dialog_keyboard.write.call_args_list, [unittest.mock.call(e.EV_KEY,e.KEY_ENTER,1),unittest.mock.call(e.EV_KEY,e.KEY_ENTER,0)])
        self.assertEqual(s.neutral.call_count,2)

    def test_focus_loss_stops_and_erases_only_owned_rumble_effects(self):
        from evdev import ecodes as e
        s=c.Session.__new__(c.Session);s.e=e
        physical=Mock();s.forwarded_effects=[{1:(physical,7)},{},{},{}]
        s.stop_effects()
        physical.write.assert_called_once_with(e.EV_FF,7,0)
        physical.erase_effect.assert_called_once_with(7)
        self.assertEqual(s.forwarded_effects,[{},{},{},{}])

    def test_steam_virtual_source_hint_is_present_and_environment_restored_on_probe_failure(self):
        import emulator_controls
        previous={k:os.environ.get(k) for k in ['SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD','SDL_JOYSTICK_HIDAPI']}
        def fail():
            self.assertEqual(os.environ['SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD'],'1')
            self.assertEqual(os.environ['SDL_JOYSTICK_HIDAPI'],'0')
            raise RuntimeError('probe failed')
        with patch.object(emulator_controls,'gamepads',side_effect=fail):
            with self.assertRaises(RuntimeError):c.source_gamepads()
        self.assertEqual({k:os.environ.get(k) for k in previous},previous)

    def test_deliberate_hold_fires_once_until_release(self):
        h=c.HeldChord()
        self.assertFalse(h.update(True,True,0))
        self.assertFalse(h.update(True,True,1.99))
        self.assertTrue(h.update(True,True,2))
        self.assertFalse(h.update(True,True,3))
        self.assertFalse(h.update(False,True,4))
        self.assertFalse(h.update(True,True,5))
        self.assertTrue(h.update(True,True,7))

    def test_focus_loss_requires_a_new_full_hold(self):
        h=c.HeldChord()
        h.update(True,True,0)
        self.assertFalse(h.update(True,False,3))
        self.assertFalse(h.update(True,True,4))
        self.assertFalse(h.update(True,True,5))
        self.assertTrue(h.update(True,True,6))

    def test_close_is_rejected_for_unowned_window(self):
        f=c.KWinFocus.__new__(c.KWinFocus)
        f.current=(123,'window',False,'DP-1')
        p=Mock();p.contains.return_value=False
        with patch.object(f,'script') as script:
            self.assertFalse(f.close_window(p))
            script.assert_not_called()

    def test_stale_focus_cannot_close_another_app_after_refresh(self):
        f=c.KWinFocus.__new__(c.KWinFocus);f.connection=Mock();f.current=(123,'old-game',False,'DP-1')
        def refresh():f.current=(456,'other-app',False,'DP-1');return True
        f.refresh_current=Mock(side_effect=refresh)
        p=Mock();p.contains.side_effect=lambda pid:pid==123
        with patch.object(f,'script') as script:
            self.assertFalse(f.close_window(p));script.assert_not_called()
        f.refresh_current.assert_called_once()

    def test_exit_is_blocked_if_fresh_focus_cannot_be_confirmed(self):
        f=c.KWinFocus.__new__(c.KWinFocus);f.connection=Mock();f.current=(123,'game',False,'DP-1')
        f.refresh_current=Mock(return_value=False)
        with patch.object(f,'script') as script:
            self.assertFalse(f.close_window(Mock()));script.assert_not_called()

    def test_pid_reuse_does_not_own_another_process(self):
        p=c.OwnedProcesses();p.members={123:'old'}
        with patch.object(c,'identity',return_value=(1,'new')):
            self.assertFalse(p.contains(123))

    def test_configuration_failure_does_not_start_a_game_and_cleans_up(self):
        session=Mock();session.env={}
        with patch.object(c,'Session',return_value=session), \
             patch.object(c.subprocess,'run',side_effect=RuntimeError('configuration failed')), \
             patch.object(c.subprocess,'Popen') as launch:
            with self.assertRaises(RuntimeError):c.run(Path('/project'),'ps2',['emulator'],{})
            launch.assert_not_called()
            session.close.assert_called_once()

    def test_callback_failure_keeps_virtual_devices_until_detached_child_exits(self):
        session=Mock();session.env={}
        events=[]
        session.tick.side_effect=lambda:events.append('tick')
        session.close.side_effect=lambda status:events.append('close')
        child=Mock(pid=41,returncode=0)
        child.poll.return_value=0  # Launcher wrapper already exited.
        remaining=Mock(side_effect=[True, OSError('temporary /proc failure'), True, False])
        children=Mock(side_effect=[RuntimeError('transient scan'),
                                   [{'pid':42,'start_ticks':'123'}]])
        with patch.object(c,'Session',return_value=session), \
             patch.object(c.subprocess,'run'), \
             patch.object(c.subprocess,'Popen',return_value=child), \
             patch.object(c,'identity',return_value=(1,'123')):
            with self.assertRaisesRegex(RuntimeError,'transient scan'):
                c.run(Path('/project'),'ps2',['wrapper'],{},
                      remaining=remaining,owned_children=children)
        self.assertEqual(events,['tick','close'])
        session.processes.add.assert_any_call(42)
        session.close.assert_called_once_with('interrupted_with_child_preserved')
        self.assertEqual(remaining.call_count,4)


if __name__=='__main__':unittest.main()
