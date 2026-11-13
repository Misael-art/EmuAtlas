"""Regression checks for the menu-to-emulator input handoff. No real input effects."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock, MagicMock
import xml.etree.ElementTree as ET

TOOLS = Path(__file__).resolve().parents[1]
PROJECT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))
import emulator_controls as controls
import romtool


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BindingsTests(unittest.TestCase):
    def test_eight_routed_ports_are_ordered_and_missing_or_duplicate_ports_fail(self):
        pads = [{'name':f'Emulation P{n+1}', 'index':n} for n in range(8)]
        with patch.dict(os.environ, {'EMULATION_PLAYER_CAPACITY':'8'}):
            self.assertEqual(controls.routed_players(list(reversed(pads))), pads)
            for invalid in (pads[:-1], pads + [pads[0]]):
                with self.assertRaises(RuntimeError):
                    controls.routed_players(invalid)

    def test_retroarch_configures_the_eighth_port_and_keeps_console_capacity_local(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / controls.PROFILE
            profile.parent.mkdir(parents=True)
            profile.write_text(json.dumps({'collections':{'sms':{'rom_subdir':'sms','adapter':'retroarch'}},
                'retroarch':{'config_path':'UserData/retroarch/retroarch.cfg','userdata_root':'UserData/retroarch'}}))
            config = root / controls.HOST / 'UserData/retroarch/retroarch.cfg'
            config.parent.mkdir(parents=True)
            config.write_text('# personal settings\n')
            pads = [{'name':f'Emulation P{n+1}', 'index':n, 'vendor':0x1209, 'product':0xE101+n} for n in range(8)]
            with patch.object(controls, 'prior_bytes', return_value=None):
                changes = controls.proposals(root, pad=pads[0], pads=pads, routed=True, system_id='sms', frontends=False)
            data = next(item['after'] for item in changes if item['path'].endswith('retroarch.cfg'))
            self.assertIn(b'input_player8_joypad_index = "7"', data)
            self.assertIn(b'input_max_users = "8"', data)
            self.assertTrue(any(item['path'].endswith('emulation-player8.cfg') for item in changes))

    def test_retrofe_reserves_real_start_and_maps_each_pad_independently(self):
        before = b'up = Up,joy0Hat0Up\nselect = Return,joy0Button0\nback = Escape,joy0Button1\nfavPlaylist = F,joyButton6\npageUp = A,joy0Button9\nquit = Q\n'
        pads = [{'index':0, 'mapping':{'start':'b6','back':'b4','a':'b0','b':'b1','dpup':'h0.1','lefty':'a1','leftstick':'b7'}},
                {'index':1, 'mapping':{'start':'b11','back':'b10','a':'b0','b':'b1','dpup':'h0.1','lefty':'a1','leftstick':'b13'}}]
        after = controls.retrofe_controls(before, pads)
        self.assertIn(b'favPlaylist = F,joy2Button6,joy3Button6,joy4Button6,joy5Button6,joy6Button6,joy7Button6,joy8Button6,joy9Button6,joy0Button4,joy1Button10', after)
        self.assertIn(b'pageUp = A,joy0Button7,joy1Button13', after)
        self.assertIn(b'select = Return,joy0Button0,joy1Button0', after)
        self.assertIn(b'quit = Q\n', after)
        self.assertNotIn(b'joy0Button6', after)
        self.assertNotIn(b'joy1Button11', after)
        self.assertEqual(controls.retrofe_controls(after, pads), after)

    def test_eighth_future_mobile_pad_can_navigate_with_deck_already_present(self):
        deck = {'index':0, 'mapping':{'a':'b0','start':'b12'}}
        data = controls.retrofe_controls(b'select = Return,joyButton0\n', [deck])
        self.assertIn(b'joy8Button0', data)
        self.assertNotIn(b'joy0Button12', data)

    def test_retrofe_projection_keeps_bom_crlf_keyboard_and_custom_actions(self):
        before = b'\xef\xbb\xbfselect = Return,joy0Button0\r\ncustom = F9\r\n# personal\r\n'
        after = controls.retrofe_controls(before, [{'index':0,'mapping':{'a':'b3'}}])
        self.assertIn(b'\xef\xbb\xbfselect = Return,joy0Button3\r\ncustom = F9\r\n# personal\r\n', after)

    def test_retrofe_mapping_supports_inverted_axes_and_button_dpad(self):
        after = controls.retrofe_controls(b'up = Up\ndown = Down\n',
            [{'index':0,'mapping':{'dpup':'b16','dpdown':'b17','lefty':'a2~'}}])
        self.assertIn(b'up = Up,joy0Button16,joy0Axis2+', after)
        self.assertIn(b'down = Down,joy0Button17,joy0Axis2-', after)

    def test_retrofe_ambiguous_duplicate_action_is_preserved(self):
        with self.assertRaises(ValueError):
            controls.retrofe_controls(b'select = Return\nselect = Space\n', [])

    def test_retrofe_custom_start_conflict_blocks_before_writing(self):
        with self.assertRaisesRegex(ValueError, 'Start SDL'):
            controls.retrofe_controls(b'custom = joyButton12\n', [{'index':0,'mapping':{'start':'b12'}}])

    def test_retrofe_new_bindings_follow_a_complete_line(self):
        after = controls.retrofe_controls(b'# personal', [{'index':0,'mapping':{'a':'b3'}}])
        self.assertEqual(after, b'# personal\nselect = joy0Button3\n')

    def test_keyboard_and_sdl_are_alternatives_not_a_chord(self):
        original = b"; personal preference\r\n[Pad1]\r\nCross = Keyboard/K\r\n[Pad2]\r\nType = None\r\n"
        data = controls.ini_update(original, {"Pad1": {"Cross": "SDL-0/A"}}, append=True)
        self.assertIn(b"Cross = Keyboard/K\r\nCross = SDL-0/A\r\n", data)
        self.assertIn(b"[Pad2]\r\nType = None", data)
        self.assertNotIn(b" & ", data)
        self.assertEqual(controls.ini_update(data, {"Pad1": {"Cross": "SDL-0/A"}}, append=True), data)

    def test_custom_sdl_and_utf8_bom_survive(self):
        data = b"\xef\xbb\xbf[Pad1]\nCross = SDL-2/A\n"
        self.assertEqual(controls.ini_update(data, {"Pad1": {"Cross": "SDL-0/A"}}, append=True), data)

    def test_ambiguous_sections_block(self):
        with self.assertRaises(ValueError):
            controls.ini_update(b"[Pad1]\n[Pad1]\n", {"Pad1": {"Cross": "SDL-0/A"}})

    def test_dolphin_keeps_keyboard_as_an_or_expression(self):
        data = controls.ini_update(b"[GCPad1]\nButtons/A = `X`\n", {"GCPad1": {"Buttons/A": "`SDL/0/Deck:Button S`"}}, expression=True)
        self.assertIn(b"`X` | `SDL/0/Deck:Button S`", data)
        self.assertEqual(controls.ini_update(data, {"GCPad1": {"Buttons/A": "`SDL/0/Deck:Button S`"}}, expression=True), data)

    def test_melonds_raw_button_and_hat_follow_the_detected_mapping(self):
        self.assertEqual(controls.melonds_button("b9"), 9)
        self.assertEqual(controls.melonds_button("h0.4"), 0x104)
        self.assertEqual(controls.melonds_button("h1.8"), 0x118)
        with self.assertRaises(ValueError):
            controls.melonds_button("+a1")

    def test_azahar_preserves_keyboard_profile_and_selects_one_managed_profile(self):
        original = b"[Controls]\nprofiles\\size=1\nprofiles\\1\\name=Default\nprofiles\\1\\button_a=engine:keyboard,code:65\n"
        pad = {"guid": "03000000de2800000512000001000000"}
        updated = controls.azahar_profile(original, pad)
        self.assertIn(b"profiles\\1\\button_a=engine:keyboard,code:65", updated)
        self.assertIn(b"profiles\\2\\button_a = \"engine:sdl,api:controller,", updated)
        self.assertIn(b"profiles\\size = 2", updated)
        self.assertEqual(controls.azahar_profile(updated, pad), updated)

    def test_cemu_profile_uses_native_schema_guid_and_all_24_game_inputs(self):
        pad = {"guid": "03000000de2800000512000001000000", "name": "Steam Deck Controller"}
        node = ET.fromstring(controls.cemu_profile(pad))
        self.assertEqual(node.findtext("type"), "Wii U GamePad")
        self.assertEqual(node.findtext("controller/api"), "SDLController")
        self.assertEqual(node.findtext("controller/uuid"), "0_" + pad["guid"])
        entries = {int(x.findtext("mapping")): int(x.findtext("button")) for x in node.findall("controller/mappings/entry")}
        self.assertEqual(len(entries), 24)
        self.assertEqual(entries[17], 45)  # left stick up is the negative Y axis
        self.assertEqual(entries[7], 42)   # analog trigger, not a boolean button

    def test_rpcs3_selects_only_player_one_and_rejects_ambiguous_yaml(self):
        data = b'Player 1 Input:\n  Handler: SDL\n  Device: "Steam Deck Controller 1"\nPlayer 2 Input:\n  Handler: Keyboard\n'
        self.assertEqual(controls.rpcs3_selection(data), {"Handler": "SDL", "Device": "Steam Deck Controller 1"})
        self.assertIsNone(controls.rpcs3_selection(b"Player 1 Input:\n  Handler: SDL\n  Device: one\n  Device: two\n"))


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="controller-config-test-", dir=PROJECT / "work/roms-consolidation")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        profile = self.root / controls.PROFILE
        profile.parent.mkdir(parents=True)
        profile.write_text('{"collections": {}}')
        self.ops = self.root / "outputs/roms-consolidacao-2026-10-24/operations"
        self.ops.mkdir(parents=True)
        for key, value in [("PROJECT_ROOT", self.root), ("OPS_ROOT", self.ops)]:
            self.enterContext(patch.object(romtool, key, value))
        self.enterContext(patch.object(romtool, "rebuild_history_index"))
        self.enterContext(patch.object(controls, "assert_closed"))
        self.enterContext(patch.object(controls, "gamepads", return_value=[]))
        self.relative = Path("emulator/data/DuckStation/config/duckstation/settings.ini")
        self.file = self.root / self.relative
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b"keyboard original\n")
        self.proposals = [{"path": str(self.relative), "before": self.file.read_bytes(),
                           "after": b"keyboard original\ncontroller added\n", "reason": "test regression"}]
        self.enterContext(patch.object(controls, "proposals", side_effect=lambda *a, **kw: self.proposals))

    def test_apply_verify_rollback_preserve_exact_original(self):
        manifest = controls.plan(self.root)
        op = manifest["operation_id"]
        self.assertEqual(self.file.read_bytes(), b"keyboard original\n")
        result = controls.execute(self.root, op, "apply")
        self.assertEqual(result["status"], "completed_verified")
        self.assertEqual(controls.execute(self.root, op, "verify")["status"], "configuration_verified")
        controls.execute(self.root, op, "rollback")
        self.assertEqual(self.file.read_bytes(), b"keyboard original\n")

    def test_later_user_change_blocks_rollback(self):
        op = controls.plan(self.root)["operation_id"]
        controls.execute(self.root, op, "apply")
        self.file.write_bytes(b"new user preference\n")
        with self.assertRaisesRegex(RuntimeError, "Mudança posterior"):
            controls.execute(self.root, op, "rollback")
        self.assertEqual(self.file.read_bytes(), b"new user preference\n")

    def test_tampered_snapshot_blocks_before_first_write(self):
        m = controls.plan(self.root)
        (self.root / m["items"][0]["after_snapshot"]).write_bytes(b"wrong bytes")
        with self.assertRaises(RuntimeError):
            controls.execute(self.root, m["operation_id"], "apply")
        self.assertEqual(self.file.read_bytes(), b"keyboard original\n")

    def test_retry_after_partial_apply_finishes_without_overwriting_new_work(self):
        second = self.relative.with_name("other.ini")
        self.proposals.append({"path": str(second), "before": None, "after": b"second\n", "reason": "second"})
        op = controls.plan(self.root)["operation_id"]
        import emutool
        original_write = emutool._atomic_write

        def fail_second(path, data, mode):
            if path.name == "other.ini":
                raise OSError("interrupted")
            return original_write(path, data, mode)

        with patch.object(emutool, "_atomic_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                controls.execute(self.root, op, "apply")
        self.assertEqual(self.file.read_bytes(), self.proposals[0]["after"])
        controls.execute(self.root, op, "apply")
        self.assertEqual((self.root / second).read_bytes(), b"second\n")
        controls.execute(self.root, op, "rollback")
        self.assertFalse((self.root / second).exists())
        self.assertTrue((self.ops / op / "rollback/generated" / second).is_file())

    def test_symlinks_and_traversal_are_rejected(self):
        with self.assertRaises(ValueError):
            controls.target(self.root, Path("../outside"))
        self.file.unlink()
        self.file.symlink_to("outside")
        with self.assertRaises(ValueError):
            controls.target(self.root, self.relative)

    def test_runtime_cannot_apply_a_frontend_plan(self):
        m = controls.plan(self.root)
        with self.assertRaises(ValueError):
            controls.execute(self.root, m["operation_id"], "apply", runtime=True)

    def test_running_emulator_blocks_a_configuration_change(self):
        (self.root / controls.PROFILE).write_text('{"collections": {"DS": {"rom_subdir": "nds", "adapter": "flatpak"}}}')
        process = MagicMock()
        process.name = "123456"
        (process / "comm").read_text.return_value = "melonDS"
        with patch.object(controls, "gamepads", return_value=[{"index": 0, "name": "Gamepad"}]), \
             patch.object(Path, "iterdir", return_value=iter([process])), \
             patch.object(controls, "plan") as plan:
            with self.assertRaisesRegex(RuntimeError, "já está em uso"):
                controls.prepare_runtime(self.root, "nds")
        plan.assert_not_called()


class LaunchEnvelopeTests(unittest.TestCase):
    def test_steam_confirmation_reselects_after_a_cold_cache_reset_and_requires_stability(self):
        launchers = PROJECT / "frontend/RetroFE/Tools/launchers/retrofe"
        sys.path.insert(0, str(launchers))
        try:
            import steam_desktop_session as steam
        finally:
            sys.path.remove(str(launchers))
        expected = {"index": 15, "type": 4, "url": steam.GAMEPAD_URL, "gamepad": True, "keyboard": False, "mouse": False}
        wrong = {**expected, "url": "original", "keyboard": True, "mouse": True}
        clock = [0.0]
        client = Mock()
        current = [wrong]
        client.controllers.side_effect = lambda: [dict(current[0])]
        client.select.side_effect = lambda *a: current.__setitem__(0, expected)
        with patch.object(steam.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(steam.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            steam.confirm(client, [expected])
        self.assertGreaterEqual(clock[0], 3)
        self.assertEqual(client.select.call_count, 1)

    def test_session_keeps_lease_until_the_emulator_exits(self):
        launchers = PROJECT / "frontend/RetroFE/Tools/launchers/retrofe"
        with patch.dict(sys.modules):
            sys.path.insert(0, str(launchers))
            try:
                steam = load("controller_test_steam", launchers / "steam-input.py")
            finally:
                sys.path.remove(str(launchers))
        events = []
        child = Mock(pid=123456)
        child.wait.side_effect = lambda: events.append("exit") or 0
        import controller_session
        def routed(root, sid, command, env, attach=None, remaining=None, owned_children=None):
            self.assertEqual(sid, 'psx')
            self.assertEqual(env['SDL_JOYSTICK_HIDAPI_STEAMDECK'], '0')
            attach(child.pid)
            result = child.wait()
            self.assertFalse(remaining())
            return result
        with patch.dict(os.environ, {"RETROFE_INPUT_BACKEND": "steam"}), \
             patch.object(Path, "read_text", return_value="I: Vendor=28de Product=1205"), \
             patch.object(steam.runpy, "run_path", return_value={"detect_session_mode": lambda: "desktop-kde"}), \
             patch.object(steam, "ensure_recovery_guard"), \
             patch.object(steam.steam_desktop_session, "snapshot", return_value={"owners": {"parent": {}}}), \
             patch.object(steam.steam_desktop_session, "owner_active", return_value=True), \
             patch.object(steam.steam_desktop_session, "acquire", side_effect=lambda *a: events.append("acquire") or True), \
             patch.object(steam.steam_desktop_session, "attach", side_effect=lambda *a: events.append("attach")), \
             patch.object(steam.steam_desktop_session, "release", side_effect=lambda *a: events.append("restore")), \
             patch.object(controller_session, 'run', side_effect=routed), \
             patch.object(steam.subprocess, "run", return_value=Mock(returncode=0)) as prepare:
            self.assertEqual(steam.run_session(PROJECT / "frontend/RetroFE", ["emulator", "game.rom"], "psx"), 0)
        self.assertEqual(events, ["acquire", "attach", "exit", "restore"])

    def test_emulator_xdg_variables_do_not_reconfigure_the_host_guardian(self):
        audio = load("controller_test_audio", PROJECT / "frontend/RetroFE/Tools/launchers/emulators/retrofe_audio.py")
        env = dict(os.environ, XDG_CONFIG_HOME="/project/emulator/config")
        with patch.object(audio, "set_background_music_paused"), patch.object(audio.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertEqual(audio.run_with_music_paused(["emulator", "a game.rom"], PROJECT, env=env, system_id="psx"), 0)
        command = run.call_args.args[0]
        self.assertIn("session-run", command)
        self.assertIn("--system-id", command)
        self.assertIn("XDG_CONFIG_HOME=/project/emulator/config", command)
        self.assertEqual(command[-2:], ["emulator", "a game.rom"])
        self.assertIsNone(run.call_args.kwargs["env"])

    def test_both_frontends_resolve_to_the_same_launcher(self):
        profile = json.loads((PROJECT / controls.PROFILE).read_text())
        projected, covered = controls.esde_projection(PROJECT, profile)
        systems = ET.fromstring(projected[0][1])
        for sid in ["psx", "ps2", "gc", "nds", "snes", "mastersystem", "switch2"]:
            self.assertIn(sid, covered)
            command = systems.find(f"system[name='{sid}']/command").text
            self.assertIn("%EMULATOR_EMULATION-PROJECT%", command)
            self.assertTrue(command.endswith("--item %ROM%"))
        self.assertNotIn("psvita", covered)
        self.assertIn("wiiware", covered)
        self.assertIn(".3ds", systems.find("system[name='n3ds']/extension").text.split())


if __name__ == "__main__":
    unittest.main()
