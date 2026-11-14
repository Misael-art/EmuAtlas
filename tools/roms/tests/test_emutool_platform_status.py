"""F06: platform runtime states stay distinct and never collapse into 'unavailable'."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher_fixture import load_emutool  # noqa: E402

emutool = load_emutool()

S = emutool.platform_runtime_status


class PlatformStatusTests(unittest.TestCase):
    def test_command_timeout_with_partial_bytes_remains_json_serializable(self):
        error = subprocess.TimeoutExpired('own-fixture', 30, output=b'sa\xc3\xadda \xff')
        system = {'adapter': 'command', 'command': 'own-fixture', 'probe_args': ['--version']}
        with patch.object(emutool, 'resolve_executable', return_value='own-fixture'), \
                patch.object(emutool.subprocess, 'run', side_effect=error):
            result = emutool.runtime_probe('Own fixture', system, {'bios_root': 'bios'},
                                           Path('own-cores'), {}, True, None)
        self.assertEqual(result['status'], 'runtime_probe_timed_out')
        self.assertEqual(result['runtime_probe_output'], 'saída \ufffd')
        self.assertFalse(result['available'])
        json.dumps(result)

    def test_alternative_runtime_timeout_decodes_partial_output(self):
        error = subprocess.TimeoutExpired('own-fixture', 30, output=b'partial \xff')
        entry = {'command': 'own-fixture', 'probe_args': ['--version']}
        with patch.object(emutool, 'resolve_executable', return_value='own-fixture'), \
                patch.object(emutool.subprocess, 'run', side_effect=error):
            result = emutool._probe_runtime_entry('own-runtime', {}, entry)
        self.assertEqual(result['status'], 'probe_timed_out')
        self.assertEqual(result['output'], 'partial \ufffd')
        json.dumps(result)

    def test_validator_timeout_decodes_both_streams_and_handles_text_or_absence(self):
        for stdout, stderr in [(b'partial \xff', b'error \xfe'), ('text', None), (None, None)]:
            with self.subTest(stdout=stdout, stderr=stderr):
                error = subprocess.TimeoutExpired('own-fixture', 1, output=stdout, stderr=stderr)
                with patch.object(emutool.romtool.subprocess, 'run', side_effect=error):
                    result = emutool.romtool.run_validator(['own-fixture'], timeout=1)
                self.assertTrue(result['timed_out'])
                self.assertIsNone(result['returncode'])
                self.assertIsInstance(result['stdout'], str)
                self.assertIsInstance(result['stderr'], str)
                json.dumps(result)

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
