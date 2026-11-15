import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT=Path(__file__).resolve().parents[3]
LAUNCHERS=PROJECT/"frontend/RetroFE/Tools/launchers/retrofe"


def load_steam_input():
    sys.path.insert(0,str(LAUNCHERS))
    try:
        spec=importlib.util.spec_from_file_location("steam_input_recovery_test",LAUNCHERS/"steam-input.py")
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(LAUNCHERS))


class FakeSystemd:
    def __init__(self,graphical=True):
        self.active={"graphical-session.target":graphical}
        self.calls=[]

    def __call__(self,args,*,timeout=60):
        self.calls.append(args[1:])
        if args[1:3]==["--user","is-active"]:
            return subprocess.CompletedProcess(args,0 if self.active.get(args[3]) else 3,"","")
        if "--now" in args:
            self.active["retrofe-input-recovery.service"]=True
        return subprocess.CompletedProcess(args,0,"","")


class InputRecoveryUnitTests(unittest.TestCase):
    def setUp(self):
        self.steam_input=load_steam_input()
        self.temp=tempfile.TemporaryDirectory(prefix="input-recovery-unit-test-")
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        env=patch.dict(os.environ,{"XDG_CONFIG_HOME":str(self.home/"config"),"XDG_STATE_HOME":str(self.home/"state")})
        env.start()
        self.addCleanup(env.stop)
        self.changes=[]
        record=patch.object(self.steam_input,"record_environment_change",lambda root,data:self.changes.append(data))
        record.start()
        self.addCleanup(record.stop)

    def ensure(self,systemd):
        with patch.object(self.steam_input.shutil,"which",return_value="/usr/bin/systemctl"), \
             patch.object(self.steam_input,"run",systemd), \
             patch.object(self.steam_input.subprocess,"Popen") as popen:
            self.steam_input.ensure_recovery_guard(self.home)
        return popen

    def test_unit_waits_for_graphical_session_display(self):
        systemd=FakeSystemd()
        self.ensure(systemd)
        unit=(self.home/"config/systemd/user/retrofe-input-recovery.service").read_text()
        self.assertIn("After=graphical-session.target\n",unit)
        self.assertIn("PartOf=graphical-session.target\n",unit)
        self.assertIn("WantedBy=graphical-session.target\n",unit)
        self.assertNotIn("default.target",unit)
        self.assertIn(["--user","reenable","--now","retrofe-input-recovery.service"],systemd.calls)

    def test_unchanged_unit_keeps_enable_without_relinking(self):
        self.ensure(FakeSystemd())
        systemd=FakeSystemd()
        systemd.active["retrofe-input-recovery.service"]=True
        self.ensure(systemd)
        self.assertIn(["--user","enable","--now","retrofe-input-recovery.service"],systemd.calls)
        self.assertNotIn(["--user","daemon-reload"],systemd.calls)
        self.assertEqual(len(self.changes),1)

    def test_without_graphical_session_uses_xdg_autostart(self):
        popen=self.ensure(FakeSystemd(graphical=False))
        self.assertFalse((self.home/"config/systemd/user/retrofe-input-recovery.service").exists())
        desktop=(self.home/"config/autostart/retrofe-input-recovery.desktop").read_text()
        self.assertIn("X-RetroFE-Managed-Input-Recovery=true",desktop)
        popen.assert_called_once()
        self.assertEqual(self.changes[0]["backend"],"xdg-autostart")

    def test_unmanaged_unit_is_preserved(self):
        unit=self.home/"config/systemd/user/retrofe-input-recovery.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\nDescription=personal\n")
        with self.assertRaises(RuntimeError):
            self.ensure(FakeSystemd())
        self.assertEqual(unit.read_text(),"[Unit]\nDescription=personal\n")

    def test_masked_unit_reports_the_systemd_block_and_preserves_it(self):
        unit = self.home / "config/systemd/user/retrofe-input-recovery.service"
        unit.parent.mkdir(parents=True)
        unit.symlink_to("/dev/null")
        with self.assertRaisesRegex(RuntimeError, "mascarado"):
            self.ensure(FakeSystemd())
        self.assertTrue(unit.is_symlink())
        self.assertEqual(os.readlink(unit), "/dev/null")

    def test_native_default_launches_without_any_steam_preparation(self):
        with patch.dict(os.environ):
            os.environ.pop("RETROFE_INPUT_BACKEND", None)
            os.environ.pop(self.steam_input.steam_desktop_session.SESSION_TOKEN_ENV, None)
            with patch.object(self.steam_input, "ensure_recovery_guard") as guard, \
                 patch.object(self.steam_input, "find_steam") as find, \
                 patch.object(self.steam_input.steam_desktop_session, "snapshot") as snapshot, \
                 patch.object(self.steam_input.runpy, "run_path") as inspect, \
                 patch.object(self.steam_input.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as run:
                self.assertEqual(self.steam_input.run_session(self.home, ["--", "frontend"]), 7)
                self.assertEqual(os.environ["RETROFE_INPUT_BACKEND"], "native")
            run.assert_called_once_with(["frontend"], check=False)
            guard.assert_not_called()
            find.assert_not_called()
            snapshot.assert_not_called()
            inspect.assert_not_called()

    def test_explicit_native_takes_precedence_over_inherited_steam_token(self):
        with patch.dict(os.environ, {"RETROFE_INPUT_BACKEND": "native",
                                     self.steam_input.steam_desktop_session.SESSION_TOKEN_ENV: "legacy-token"}):
            self.assertEqual(self.steam_input.input_backend(), "native")

    def test_native_emulator_uses_existing_controller_router(self):
        router = unittest.mock.Mock()
        router.run.return_value = 0
        with patch.dict(os.environ, {"RETROFE_INPUT_BACKEND": "native"}), \
             patch.dict(sys.modules, {"controller_session": router}), \
             patch.object(self.steam_input.runpy, "run_path", return_value={"detect_session_mode": lambda: "desktop-kde"}), \
             patch.object(self.steam_input, "ensure_recovery_guard") as guard:
            self.assertEqual(self.steam_input.run_session(PROJECT / "frontend/RetroFE", ["emulator"], "psx"), 0)
        args = router.run.call_args.args
        self.assertEqual(args[:3], (PROJECT, "psx", ["emulator"]))
        self.assertEqual(args[3]["RETROFE_INPUT_BACKEND"], "native")
        guard.assert_not_called()

    def test_legacy_inherited_lease_keeps_steam_backend(self):
        with patch.dict(os.environ, {self.steam_input.steam_desktop_session.SESSION_TOKEN_ENV: "legacy-token"}):
            os.environ.pop("RETROFE_INPUT_BACKEND", None)
            self.assertEqual(self.steam_input.input_backend(), "steam")

    def test_invalid_backend_refuses_before_launch(self):
        with patch.dict(os.environ, {"RETROFE_INPUT_BACKEND": "unknown"}), \
             patch.object(self.steam_input.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "native ou steam"):
                self.steam_input.run_session(self.home, ["frontend"])
        run.assert_not_called()

    def test_explicit_steam_keeps_its_recovery_requirement(self):
        with patch.dict(os.environ, {"RETROFE_INPUT_BACKEND": "steam"}), \
             patch.object(Path, "read_text", return_value="I: Vendor=28de Product=1205"), \
             patch.object(self.steam_input.runpy, "run_path", return_value={"detect_session_mode": lambda: "desktop-kde"}), \
             patch.object(self.steam_input, "ensure_recovery_guard", side_effect=RuntimeError("masked")), \
             patch.object(self.steam_input.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "masked"):
                self.steam_input.run_session(self.home, ["frontend"])
        run.assert_not_called()

    def test_recovery_does_not_restore_desktop_when_process_scan_fails(self):
        with patch.object(self.steam_input.runpy, "run_path", return_value={"recover_guard": lambda path: (True, "")}), \
             patch.object(self.steam_input.steam_desktop_session, "snapshot", return_value={"owners": {"1": {}}}), \
             patch.object(self.steam_input.steam_desktop_session, "has_live_owners",
                          side_effect=RuntimeError("Não foi possível verificar /proc")), \
             patch.object(self.steam_input.desktop_environment, "restore") as restore:
            with self.assertRaisesRegex(RuntimeError, "Não foi possível verificar /proc"):
                self.steam_input.recover_session_once(self.home)
            restore.assert_not_called()


if __name__=="__main__":
    unittest.main()
