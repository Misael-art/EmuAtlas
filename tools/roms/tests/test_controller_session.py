"""Routing state and boundaries without touching any host input device."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import controller_session as c


def device(path, kind='external'):
    return {'path':path,'kind':kind}


class RoutingTests(unittest.TestCase):
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
            d.active_keys.return_value=[active]
            s.forward(d,0)
            s.write.assert_any_call(0,e.EV_KEY,e.BTN_WEST,1)
            s.write.assert_any_call(0,e.EV_KEY,e.BTN_NORTH,0)

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
