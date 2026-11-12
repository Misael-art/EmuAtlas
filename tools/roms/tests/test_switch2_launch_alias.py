"""F04: managed Switch 2 .exefs aliases pass the dispatcher; other symlinks stay refused."""
import importlib.util
import os
import struct
import tempfile
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher_fixture import load_launcher  # noqa: E402

launcher = load_launcher("host_emulator_launcher")
SYSTEM = {"runtime_id": "eden-nx2", "launch_inputs": ["exefs", "game_directory"]}


def npdm():
    raw = bytearray(0x100)
    raw[:4] = b"META"
    struct.pack_into("<I", raw, 0x70, 0x80)
    raw[0x80:0x84] = b"ACI0"
    struct.pack_into("<Q", raw, 0x90, 0x0100ABCD00000000)
    return bytes(raw)


class Switch2AliasTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "roms/switch2"
        exefs = self.root / "Game/base/exefs"
        exefs.mkdir(parents=True)
        (exefs / "main").write_bytes(b"NSO0" + b"\0" * 60)
        (exefs / "main.npdm").write_bytes(npdm())

    def link(self, name, target):
        path = self.root / name
        os.symlink(target, path)
        return path

    def check(self, path, system=SYSTEM):
        return launcher.validate_managed_launch_alias(path, self.root, system)

    def test_valid_alias_resolves_to_main(self):
        target, why = self.check(self.link("Game.exefs", "Game/base/exefs/main"))
        self.assertEqual((target, why), ((self.root / "Game/base/exefs/main").resolve(), ""))

    def test_refusals(self):
        outside = self.root.parent / "evil"; outside.mkdir()
        (outside / "main").write_bytes(b"NSO0")
        cases = {
            "abs.exefs": str(self.root / "Game/base/exefs/main"),
            "up.exefs": "../evil/main",
            "npdm.exefs": "Game/base/exefs/main.npdm",
            "missing.exefs": "Game/base/exefs/nothere",
            "wrongsuffix.zip": "Game/base/exefs/main",
        }
        for name, target in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(self.check(self.link(name, target))[0])
        os.symlink("Game", self.root / "Redirect")
        self.assertIsNone(self.check(self.link("hop.exefs", "Redirect/base/exefs/main"))[0])
        os.symlink("loop2.exefs", self.root / "loop1.exefs"); os.symlink("loop1.exefs", self.root / "loop2.exefs")
        self.assertIsNone(self.check(self.root / "loop1.exefs")[0])
        self.assertIsNone(self.check(self.link("other.exefs", "Game/base/exefs/main"), {"runtime_id": "x", "launch_inputs": ["exefs"]})[0])

    def test_non_nso_and_bad_npdm_refused(self):
        alias = self.link("Game.exefs", "Game/base/exefs/main")
        (self.root / "Game/base/exefs/main.npdm").write_bytes(b"META" + b"\0" * 200)
        self.assertIn("ACI0", self.check(alias)[1])
        (self.root / "Game/base/exefs/main").write_bytes(b"XXXX")
        self.assertIn("NSO0", self.check(alias)[1])

    def test_real_library_aliases_when_present(self):
        real = ROOT / "roms/switch2"
        aliases = sorted(real.glob("*.exefs")) if real.is_dir() else []
        if not aliases:
            self.skipTest("biblioteca real ausente neste host")
        for alias in aliases:
            with self.subTest(alias=alias.name):
                target, why = launcher.validate_managed_launch_alias(Path(os.path.abspath(alias)), Path(os.path.abspath(real)), SYSTEM)
                self.assertIsNotNone(target, why)


if __name__ == "__main__":
    unittest.main()
