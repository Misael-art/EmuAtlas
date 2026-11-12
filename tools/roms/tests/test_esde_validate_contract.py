"""F03/F02: ES-DE launch gate keeps its diagnosis and fails with a non-zero code."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.roms import romtool


class EsdeValidateContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / "media"
        (self.media / "snes/box").mkdir(parents=True)
        (self.root / "home").mkdir()
        for attr, value in (("PROJECT_ROOT", self.root), ("MEDIA_DEST_ROOT", self.media),
                            ("OUTPUT_ROOT", self.root / "outputs"), ("OPS_ROOT", self.root / "outputs/ops")):
            patcher = patch.object(romtool, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        home = patch.object(Path, "home", return_value=self.root / "home")
        home.start()
        self.addCleanup(home.stop)

    def run_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = romtool.main(["media", "esde", "validate"])
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_missing_alias_is_blocked_with_cause_and_exit_3(self):
        code, report, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("snes/3dboxes precisa ser symlink relativo para box", report["issues"])
        self.assertIn("snes/3dboxes", err)

    def test_wrong_relative_alias_is_blocked(self):
        os.symlink("../snes/box", self.media / "snes/3dboxes")
        code, report, _ = self.run_cli()
        self.assertEqual(code, 3)
        self.assertTrue(report["issues"])

    def test_occupied_alias_directory_is_blocked(self):
        (self.media / "snes/3dboxes").mkdir()
        code, report, _ = self.run_cli()
        self.assertEqual(code, 3)

    def test_correct_alias_is_ready_and_exit_0(self):
        os.symlink("box", self.media / "snes/3dboxes")
        code, report, err = self.run_cli()
        self.assertEqual((code, report["status"], err), (0, "ready", ""))


class EsdeAliasFlowTests(EsdeValidateContractTests):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        api_patch = patch.object(romtool, "retrofe_processes_running", return_value=[])
        esde_patch = patch.object(romtool, "esde_processes_running", return_value=[])
        with api_patch, esde_patch, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = romtool.main(list(argv))
        return code, json.loads(out.getvalue() or '{}')

    def test_ensure_creates_missing_alias_and_is_idempotent(self):
        (self.media / "snes/video").mkdir()
        code, first = self.run_main("media", "esde", "aliases", "ensure")
        self.assertEqual((code, first["status"]), (0, "verified"))
        self.assertEqual(os.readlink(self.media / "snes/3dboxes"), "box")
        self.assertEqual(os.readlink(self.media / "snes/videos"), "video")
        ops = sorted((self.root / "outputs/ops").iterdir())
        code, second = self.run_main("media", "esde", "aliases", "ensure")
        self.assertEqual((code, second["status"]), (0, "ready"))
        self.assertEqual(ops, sorted((self.root / "outputs/ops").iterdir()), "repeat must not create operations")

    def test_occupied_alias_blocks_without_overwrite(self):
        (self.media / "snes/3dboxes").mkdir()
        (self.media / "snes/3dboxes/keep.png").write_bytes(b"x")
        code, result = self.run_main("media", "esde", "aliases", "ensure")
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue((self.media / "snes/3dboxes/keep.png").is_file())

    def test_rollback_removes_only_own_links_and_refuses_changed_link(self):
        code, plan = self.run_main("media", "esde", "aliases", "plan")
        opid = plan["operation_id"]
        self.run_main("media", "esde", "aliases", "apply", "--operation-id", opid, "--apply")
        code, rolled = self.run_main("media", "esde", "aliases", "rollback", "--operation-id", opid, "--rollback")
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertFalse(os.path.lexists(self.media / "snes/3dboxes"))
        self.assertTrue((self.media / "snes/box").is_dir())
        code, plan = self.run_main("media", "esde", "aliases", "plan")
        self.run_main("media", "esde", "aliases", "apply", "--operation-id", plan["operation_id"], "--apply")
        (self.media / "snes/3dboxes").unlink(); os.symlink("../snes/box", self.media / "snes/3dboxes")
        code, _ = self.run_main("media", "esde", "aliases", "rollback", "--operation-id", plan["operation_id"], "--rollback")
        self.assertEqual(code, 2)
        self.assertTrue(os.path.islink(self.media / "snes/3dboxes"))

    def test_relative_aliases_survive_root_relocation(self):
        self.run_main("media", "esde", "aliases", "ensure")
        moved = Path(self.tmp.name + "-moved")
        os.rename(self.root, moved)
        self.addCleanup(lambda: os.rename(moved, self.root) if moved.exists() else None)
        self.assertEqual((moved / "media/snes/3dboxes").resolve(), (moved / "media/snes/box").resolve())


if __name__ == "__main__":
    unittest.main()
