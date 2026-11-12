"""F08: launcher archives are listed and vetted before any write."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher_fixture import load_launcher  # noqa: E402

launcher = load_launcher("hel_archive")
SEVENZ = shutil.which("7z") or shutil.which("7zz")


@unittest.skipUnless(SEVENZ, "7z ausente")
class ArchiveExtractionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.stage = self.dir / "stage"
        self.stage.mkdir()

    def zip(self, name, entries, symlink=None):
        path = self.dir / name
        with zipfile.ZipFile(path, "w") as out:
            for member, data in entries:
                out.writestr(member, data)
            if symlink:
                info = zipfile.ZipInfo(symlink[0])
                info.external_attr = 0o120777 << 16
                out.writestr(info, symlink[1])
        return path

    def test_valid_single_payload_extracts(self):
        item = self.zip("ok.zip", [("game.nds", b"x" * 100)])
        self.assertEqual(launcher.safe_extract_archive(SEVENZ, item, self.stage), "")
        self.assertEqual((self.stage / "game.nds").read_bytes(), b"x" * 100)

    def test_unsafe_archives_refused_before_writing(self):
        cases = {
            "traversal.zip": self.zip("traversal.zip", [("../escape.nds", b"x")]),
            "symlink.zip": self.zip("symlink.zip", [("a.nds", b"x")], symlink=("link", "/etc/passwd")),
            "collision.zip": self.zip("collision.zip", [("A.nds", b"x"), ("a.nds", b"y")]),
        }
        for name, item in cases.items():
            with self.subTest(name=name):
                self.assertNotEqual(launcher.safe_extract_archive(SEVENZ, item, self.stage), "")
                self.assertEqual(list(self.stage.iterdir()), [])
        self.assertFalse((self.dir / "escape.nds").exists())

    def test_budget_and_member_limits(self):
        members = [{"Path": f"f{i}", "Size": "1"} for i in range(launcher.ARCHIVE_MAX_MEMBERS + 1)]
        self.assertIn("limite", launcher.vet_archive_members(members, 1 << 40)[1])
        self.assertIn("espaço", launcher.vet_archive_members([{"Path": "big", "Size": str(1 << 30)}], 1 << 30)[1])
        self.assertIn("criptografado", launcher.vet_archive_members([{"Path": "e", "Size": "1", "Encrypted": "+"}], 1 << 40)[1])

    def test_corrupt_archive_refused(self):
        item = self.dir / "bad.zip"
        item.write_bytes(b"PK\x03\x04garbage")
        self.assertNotEqual(launcher.safe_extract_archive(SEVENZ, item, self.stage), "")


if __name__ == "__main__":
    unittest.main()
