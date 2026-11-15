#!/usr/bin/env python3
"""Regression checks for cross-theme navigation and durable desktop recovery.

Default tests use isolated state, fake Steam and a controllable clock. They never
change the host controller profile, open a frontend or send keyboard input.
"""
from __future__ import annotations
import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "Tools/launchers/retrofe"
sys.path.insert(0, str(SCRIPT))
import steam_desktop_session as steam

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPT / file)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value

panel = load("session_safety_panel", "navigation-panel.py")
kde = load("session_safety_kde", "input-session.py")
bootstrap = load("session_safety_bootstrap", "steam-input.py")
themes = load("session_safety_themes", "theme-profile.py")
DESKTOP = dict(index=15, type=4, url="template://desktop.vdf", title="Original", gamepad=True, keyboard=True, mouse=True)
GAMEPAD = {**DESKTOP, "url": steam.GAMEPAD_URL, "keyboard":False, "mouse":False}

class Clock:
    now = 100.0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds

class FakeSteam:
    def __init__(self, cold_reset=False):
        self.current = dict(GAMEPAD)
        self.calls = self.selections = 0
        self.cold_reset = cold_reset
    def controllers(self):
        self.calls += 1
        if self.cold_reset and self.calls == 4: self.current = dict(GAMEPAD)
        return [dict(self.current)]
    def select(self, controller, url):
        self.selections += 1
        self.current = dict(DESKTOP)

class SafetyTests(unittest.TestCase):
    def panel_instance(self, opened=False):
        instance = panel.OptionsPanel.__new__(panel.OptionsPanel)
        instance.opened = opened
        instance.front_active = True
        instance.busy = False
        instance.closing = None
        instance.ignore_until = 0
        for name in ("show", "back", "confirm", "move"):
            setattr(instance, name, Mock())
        return instance

    def test_native_start_raw_11_and_12_open_options(self):
        for button in (11, 12):
            instance = self.panel_instance()
            tokens = [f"joyButton{button}", *panel.inputs.semantic_tokens({"start": f"b{button}"}, f"b{button}")]
            instance.event("SDL gamepad", tokens, [])
            instance.show.assert_called_once_with()

    def test_raw_seven_right_shoulder_cannot_open_options(self):
        instance = self.panel_instance()
        instance.event("SDL pad", ["joyButton7", *panel.inputs.semantic_tokens({"rightshoulder":"b7"}, "b7")], [])
        instance.show.assert_not_called()

    def test_unmapped_pad_preserves_raw_start_fallback(self):
        instance = self.panel_instance()
        instance.event("SDL pad", ["joyButton7"], [])
        instance.show.assert_called_once_with()

    def test_mapped_confirm_back_and_dpad_are_logical(self):
        for semantic, raw, action, args in [("a","b3","confirm",()), ("b","b4","back",()),
                                             ("dpup","b16","move",(-1,)), ("dpdown","h0.4","move",(1,))]:
            instance = self.panel_instance(True)
            tokens = panel.inputs.semantic_tokens({semantic:raw}, raw)
            instance.event("SDL pad", tokens, [])
            getattr(instance, action).assert_called_once_with(*args)

    def test_inverted_and_half_axis_directions_follow_sdl_mapping(self):
        self.assertIn("gamepad:lefty+", panel.inputs.semantic_tokens({"lefty":"a2~"}, "a2", -1))
        self.assertIn("gamepad:dpup", panel.inputs.semantic_tokens({"dpup":"-a3"}, "a3", -1))
        instance = self.panel_instance(True)
        instance.event("SDL pad", panel.inputs.semantic_tokens({"lefty":"a2~"}, "a2", -1), [])
        instance.move.assert_called_once_with(1)

    def test_controller_exit_still_requires_explicit_confirmation(self):
        instance = self.panel_instance(True)
        instance.root_path = ROOT
        instance.screen = "exit"
        instance.rows = instance.make_rows()
        instance.index = 0
        instance.log = Mock()
        instance.close = Mock()
        instance.confirm = panel.OptionsPanel.confirm.__get__(instance)
        instance.confirm()
        instance.close.assert_called_once_with()
        instance.close.reset_mock()
        instance.index = 1
        instance.confirm()
        instance.close.assert_called_once_with("confirm_exit")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="retrofe-session-safety-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "original-state.json"
        self.state = {"controllers":[dict(DESKTOP)], "owners":{}}
        self.path.write_text(json.dumps(self.state))
    def restore(self, client):
        clock = Clock()
        with patch.object(steam.time,"monotonic",clock.monotonic), patch.object(steam.time,"sleep",clock.sleep):
            steam.restore(client,self.path,self.state)
        return clock
    def test_cold_steam_overwrite_reapplied_before_stable_confirmation(self):
        client = FakeSteam(cold_reset=True)
        clock = self.restore(client)
        self.assertGreaterEqual(client.selections,2)
        self.assertGreaterEqual(clock.now,104.5)
        self.assertFalse(self.path.exists())
        self.assertEqual(client.current,DESKTOP)
    def test_disconnected_controller_keeps_original_recovery(self):
        original = self.path.read_bytes()
        with patch.object(FakeSteam,"controllers",return_value=[]):
            with self.assertRaises(RuntimeError): self.restore(FakeSteam())
        self.assertEqual(self.path.read_bytes(),original)
    def test_api_failure_keeps_original_recovery(self):
        original = self.path.read_bytes()
        with patch.object(FakeSteam,"controllers",side_effect=OSError("Steam unavailable")):
            with self.assertRaises(OSError): self.restore(FakeSteam())
        self.assertEqual(self.path.read_bytes(),original)
    def test_pid_reused_in_another_boot_cannot_hold_gamepad(self):
        owner = {"pid":os.getpid(),"start_ticks":steam.proc_identity(os.getpid()),"boot_id":"previous-boot","children":[]}
        self.assertFalse(steam.owner_active(owner))
        self.assertFalse(kde.guard_owner_alive(str(owner["pid"]), owner))
    def test_live_child_retains_gamepad_if_supervisor_dies(self):
        child = {"pid":os.getpid(),"start_ticks":steam.proc_identity(os.getpid()),"boot_id":steam.boot_id()}
        owner = {"pid":99999999,"start_ticks":"0","boot_id":steam.boot_id(),"children":[child]}
        self.assertTrue(steam.owner_active(owner))
    def test_kde_watch_retries_failed_restore(self):
        with patch.object(kde,"process_alive",return_value=False), patch.object(kde,"release_guard",side_effect=[(False,"bus unavailable"),(True,"restored")]) as release, patch.object(kde.time,"sleep"):
            kde.watch_guard(self.directory,99999,"0")
        self.assertEqual(release.call_count,2)
    def test_failed_kde_bus_restore_retries_empty_owner_state(self):
        guard_path = self.directory / kde.STATE_NAME
        kde.write_state(guard_path,{"schema":1,"active":True,"owners":{"99999":{"pid":99999,"start_ticks":"0"}}})
        with patch.object(kde,"qdbus_command",side_effect=[(False,"bus unavailable"),(True,"")]) as bus:
            self.assertFalse(kde.release_guard(self.directory,99999)[0])
            self.assertTrue(guard_path.exists())
            self.assertTrue(kde.release_guard(self.directory,99999)[0])
        self.assertEqual(bus.call_count,2)
        self.assertFalse(guard_path.exists())
    def test_kde_recovery_runs_when_steam_is_unavailable(self):
        with patch.object(bootstrap.runpy,"run_path",return_value={"recover_guard":lambda _: (True,"KDE restored")}) as loaded, patch.object(steam,"snapshot",return_value=self.state), patch.object(bootstrap,"process_running",return_value=True), patch.object(steam,"release",side_effect=RuntimeError("Steam unavailable")), patch.object(bootstrap.desktop_environment,"restore",return_value=["keyboard"]) as local_restore:
            with self.assertRaises(RuntimeError): bootstrap.recover_session_once(ROOT)
        loaded.assert_called_once()
        local_restore.assert_called_once()
    def fixture(self):
        root = self.directory / "frontend"
        (root/"Config").mkdir(parents=True)
        registry = json.loads((ROOT/"Config/themes.json").read_text())
        (root/"Config/themes.json").write_text(json.dumps(registry))
        (root/"controls.conf").write_bytes((ROOT/"controls.conf").read_bytes())
        for theme in registry["themes"]:
            path = root/"layouts"/theme["layout"]
            path.mkdir(parents=True)
            for file in {"layout.xml",*theme["variants"].values()}: (path/file).write_text('<layout width="1920" height="1080"/>')
        return root,registry
    def test_all_catalogued_themes_inherit_session_navigation(self):
        root,registry = self.fixture()
        for theme in registry["themes"]:
            selected,_ = panel.navigation_contract(root,theme["id"])
            self.assertEqual(selected["id"],theme["id"])
    def test_debug_names_keyboard_options_without_false_engine_binding(self):
        debug=panel.inputs
        engine=debug.parse_controls(ROOT/"controls.conf")
        session=debug.parse_controls(ROOT/"controls.conf",include_session=True)
        self.assertEqual(debug.resolve_actions(engine,["F10"]),[])
        self.assertEqual(debug.resolve_actions(session,["F10"]),["optionsPanel"])
    def test_keyboard_options_conflict_rejected(self):
        root,_ = self.fixture()
        with (root/"controls.conf").open("a") as out:out.write("\nrandom = F10\n")
        with self.assertRaises(RuntimeError):panel.navigation_contract(root,"aura-memorias")
    def test_opening_key_cannot_immediately_close_new_panel(self):
        instance=panel.OptionsPanel.__new__(panel.OptionsPanel)
        instance.opened=True;instance.busy=False;instance.closing=None;instance.ignore_until=101
        event=argparse.Namespace(keysym="F10")
        with patch.object(panel.time,"monotonic",return_value=100), patch.object(instance,"back") as back:
            instance.key(event)
        back.assert_not_called()
    def test_immediate_sdl_quit_rejected(self):
        root,_ = self.fixture()
        with (root/"controls.conf").open("a") as out:out.write("\nquit = joyButton10\n")
        with self.assertRaises(RuntimeError):panel.navigation_contract(root,"aeon-nox")
    def test_bad_theme_provider_refuses_switch_before_pending_write(self):
        root,registry = self.fixture()
        registry["themes"][1]["session_navigation"]["provider"] = "unverified-native"
        (root/"Config/themes.json").write_text(json.dumps(registry))
        args = argparse.Namespace(root=root,theme="aura-memorias",display_profile_id="16x9-hd",width=1920,height=1080)
        with self.assertRaises(ValueError):themes.command_request(args)
        self.assertFalse((root/themes.PENDING_REL).exists())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",type=Path,default=ROOT/"Docs/TestResults/retrofe-session-safety-regressions.json")
    args = parser.parse_args()
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        result = unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SafetyTests))
    report = {"passed":result.wasSuccessful(),"tests":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"host_profiles_modified":False,"transcript":stream.getvalue()}
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k!="transcript"}))
    if not result.wasSuccessful():print(stream.getvalue())
    raise SystemExit(0 if result.wasSuccessful() else 1)
