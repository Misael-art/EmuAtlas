"""F06: platform runtime states stay distinct and never collapse into 'unavailable'."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher_fixture import load_emutool  # noqa: E402

emutool = load_emutool()

S = emutool.platform_runtime_status


class PlatformStatusTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(S([{"available": True, "onboarding": {"status": "not_declared"}}]), "runtime_ready_unverified")
        self.assertEqual(S([{"available": True, "onboarding": {"status": "manual_required"}}]), "onboarding_pending")
        self.assertEqual(S([{"status": "bios_missing"}]), "dependency_missing")
        self.assertEqual(S([{"status": "runtime_probe_not_configured"}]), "installed_not_probed")
        self.assertEqual(S([{"status": "runtime_probe_timed_out"}]), "runtime_probe_failed")
        self.assertEqual(S([{"status": "retroarch_core_missing"}]), "dependency_missing")
        self.assertEqual(S([{"status": "not_applicable_host"}]), "not_applicable_host")
        self.assertEqual(S([{"status": "command_missing"}]), "runtime_unavailable")

    def test_best_profile_wins(self):
        self.assertEqual(S([{"status": "command_missing"}, {"status": "runtime_probe_not_configured"},
                            {"available": True}]), "runtime_ready_unverified")

    def test_runtime_list_without_selection_path_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "host-linux.json"
            profile.write_text(json.dumps({"collections": {"Microsoft Xbox 360": {
                "rom_subdir": "xbox360", "runtime_id": "xenia-canary-linux",
                "command": "../../emulator/linux/xenia-canary-linux/Xenia.AppImage",
                "probe_args": ["--appimage-version"]}}}), encoding="utf-8")
            original = emutool.PROFILE_PATH
            emutool.PROFILE_PATH = profile
            try:
                result = emutool.runtime_list("xbox360")
            finally:
                emutool.PROFILE_PATH = original
        self.assertIsNone(result["selection_file"])
        self.assertEqual(result["preferred_runtime_id"], "xenia-canary-linux")
        self.assertEqual([item["runtime_id"] for item in result["runtimes"]], ["xenia-canary-linux"])
        self.assertFalse(result["game_launch_tested"])


if __name__ == "__main__":
    unittest.main()
