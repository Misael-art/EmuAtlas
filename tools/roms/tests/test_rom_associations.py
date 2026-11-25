"""Desktop ROM associations project the catalog and open through the shared controller session."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.roms import rom_associations as ra


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rom-associations-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "emulation"
        sms_family = ["7z", "zip", "sms", "gg", "md", "gen"]
        write(self.root / ra.CATALOG, {"systems": [
            {"id": "mastersystem", "display_name": "Sega Master System", "extensions": sms_family},
            {"id": "gamegear", "display_name": "Sega Game Gear", "extensions": sms_family},
            {"id": "megadrive", "display_name": "Sega Mega Drive", "extensions": sms_family},
            {"id": "genesis", "display_name": "Sega Genesis", "extensions": sms_family},
            {"id": "sega32xna", "display_name": "Sega 32X", "extensions": ["32x", "md"]},
            {"id": "switch", "display_name": "Nintendo Switch", "extensions": ["nsp"]},
        ]})
        gpgx = {"adapter": "retroarch", "core": "genesis_plus_gx_libretro.so", "extensions": "7z,zip,sms"}
        write(self.root / ra.PROFILE, {"rom_root": "../../roms", "collections": {
            "Sega Master System": {**gpgx, "rom_subdir": "mastersystem"},
            "Sega Game Gear": {**gpgx, "rom_subdir": "gamegear"},
            "Sega Mega Drive": {**gpgx, "rom_subdir": "megadrive"},
            "Sega Genesis": {**gpgx, "rom_subdir": "genesis"},
            "Sega 32X": {"adapter": "retroarch", "core": "picodrive_libretro.so", "rom_subdir": "sega32xna"},
            "Nintendo Switch": {"adapter": "unavailable", "rom_subdir": "switch"},
        }})
        write(self.root / ra.NATIVE, {"profiles": [{"source_extensions_by_system": {
            "mastersystem": [".sms"], "gamegear": [".gg"], "megadrive": [".gen", ".md"],
            "genesis": [".gen", ".md"], "sega32xna": [".md", ".32x"]}}]})
        for relative in ("roms/genesis/a.gen", "roms/genesis/b.gen", "roms/megadrive/c.gen",
                         "roms/mastersystem/Alex Kidd.7z"):
            write(self.root / relative, "")
        self.systems = ra.rom_systems(self.root)
        self.native = ra.native_extensions(self.root)


class ClassificationTests(Fixture):
    def test_only_game_types_become_defaults(self):
        self.assertEqual(ra.classify("sms", "application/x-sms-rom"), ("default", "application/x-sms-rom"))
        self.assertEqual(ra.classify("nsp", "application/x-zerosize"), ("own", "application/x-emuatlas-rom-nsp"))
        self.assertEqual(ra.classify("zip", "application/zip"), ("added", "application/zip"))
        # Found on the real host: these types would have been taken over by a naive rule.
        for ext, mime in (("desktop", "application/x-desktop"), ("crt", "application/pkix-cert"),
                          ("cmd", "application/x-bat"), ("bin", "application/x-executable"),
                          ("md", "text/markdown"), ("rom", "application/x-zerosize"),
                          ("dmg", "application/x-apple-diskimage"), ("psf", "application/x-font-linux-psf")):
            self.assertEqual(ra.classify(ext, mime)[0], "skip", ext)

    def test_unavailable_platforms_are_not_offered(self):
        self.assertNotIn("switch", self.systems)
        self.assertNotIn("nsp", ra.extension_map(self.systems))


class ResolveTests(Fixture):
    def resolve(self, path):
        return ra.resolve(self.root, Path(path), self.systems, self.native)

    def test_library_folder_decides_even_for_shared_containers(self):
        found = self.resolve(self.root / "roms/mastersystem/Alex Kidd.7z")
        self.assertEqual((found["system"], found["external"]), ("mastersystem", False))

    def test_native_extension_opens_outside_the_library_without_asking(self):
        found = self.resolve("/home/u/Downloads/marrom.sms")
        self.assertEqual((found["system"], found["external"]), ("mastersystem", True))

    def test_regional_variants_on_the_same_core_pick_the_larger_library(self):
        self.assertEqual(self.resolve("/home/u/Downloads/x.gen")["system"], "genesis")

    def test_different_emulators_ask(self):
        found = self.resolve("/home/u/Downloads/x.md")
        self.assertIsNone(found["system"])
        self.assertEqual(found["candidates"], ["genesis", "megadrive", "sega32xna"])

    def test_launch_goes_straight_to_the_launcher_that_owns_the_controller_session(self):
        command = ra.launch_command(self.root, Path("/home/u/Downloads/marrom.sms"), "mastersystem",
                                    self.systems, True)
        self.assertTrue(command[1].endswith(str(ra.LAUNCHER)))
        # Found on the host: an extra session-run around the launcher nests two controller sessions.
        self.assertNotIn("session-run", command)
        self.assertEqual(command[2:], ["--collection", "Sega Master System", "--item",
                                       "/home/u/Downloads/marrom.sms", "--external"])
        library = ra.launch_command(self.root, self.root / "roms/mastersystem/Alex Kidd.7z", "mastersystem",
                                    self.systems, False)
        self.assertNotIn("--external", library)

    def test_launcher_wraps_emulators_in_the_controller_session(self):
        audio = Path(__file__).resolve().parents[3] / "frontend/RetroFE/Tools/launchers/emulators/retrofe_audio.py"
        text = audio.read_text(encoding="utf-8")
        self.assertIn('"session-run", "--system-id", system_id', text)


class ApplyTests(Fixture):
    def setUp(self):
        super().setUp()
        home = Path(self.tmp.name) / "home"
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(home / "share"), "XDG_CONFIG_HOME": str(home / "config"),
                                      "XDG_STATE_HOME": str(home / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.mimeapps = write(home / "config/mimeapps.list",
                              "# escolhas do usuário\n[Default Applications]\napplication/zip=org.kde.ark.desktop;\n"
                              "application/x-sms-rom=org.libretro.RetroArch.desktop;\n")
        for name, value in (("refresh_caches", lambda: None), ("current_default", self.default_of)):
            patcher = patch.object(ra, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.rows = ra.build_plan(self.root, {"sms": "application/x-sms-rom", "gg": "application/x-gamegear-rom",
                                              "zip": "application/zip", "7z": "application/x-7z-compressed",
                                              "md": "text/markdown", "gen": "application/x-genesis-rom",
                                              "32x": "application/x-genesis-32x-rom"}.get)

    def default_of(self, mime):
        for name, lines in ra.read_sections(self.mimeapps):
            if name == "Default Applications":
                for line in lines:
                    if line.startswith(mime + "="):
                        return line.split("=", 1)[1].split(";")[0]
        return ""

    def test_apply_then_remove_restores_previous_defaults_and_user_changes(self):
        result = ra.apply(self.root, self.rows)
        self.assertEqual(self.default_of("application/x-sms-rom"), ra.DESKTOP_ID)
        self.assertEqual(self.default_of("application/zip"), "org.kde.ark.desktop")
        text = self.mimeapps.read_text()
        self.assertIn("# escolhas do usuário", text)
        self.assertIn("application/zip=" + ra.DESKTOP_ID + ";", text.split("[Added Associations]")[1])
        desktop = (ra.data_home() / "applications" / ra.DESKTOP_ID).read_text()
        self.assertIn(ra.MARKER, desktop)
        self.assertIn("application/x-sms-rom;", desktop)
        self.assertNotIn("text/markdown", desktop)
        self.assertEqual(result["defaults"], 4)
        # The user picks another app for Game Gear after EmuAtlas: removal keeps that choice.
        sections = ra.read_sections(self.mimeapps)
        ra.edit_entry(sections, "Default Applications", "application/x-gamegear-rom", lambda v: ["outro.desktop"])
        ra.write_sections(self.mimeapps, sections)
        ra.remove()
        self.assertEqual(self.default_of("application/x-sms-rom"), "org.libretro.RetroArch.desktop")
        self.assertEqual(self.default_of("application/x-gamegear-rom"), "outro.desktop")
        self.assertEqual(self.default_of("application/x-genesis-rom"), "")
        self.assertNotIn(ra.DESKTOP_ID, self.mimeapps.read_text())
        self.assertFalse((ra.data_home() / "applications" / ra.DESKTOP_ID).exists())
        self.assertFalse(ra.state_file().exists())

    def test_reapply_keeps_the_first_previous_default(self):
        ra.apply(self.root, self.rows)
        ra.apply(self.root, self.rows)
        record = json.loads(ra.state_file().read_text())
        self.assertEqual(record["previous_defaults"]["application/x-sms-rom"], "org.libretro.RetroArch.desktop")

    def test_unmanaged_files_with_our_names_are_preserved(self):
        foreign = write(ra.data_home() / "applications" / ra.DESKTOP_ID, "[Desktop Entry]\nName=Meu\n")
        with self.assertRaises(ValueError):
            ra.apply(self.root, self.rows)
        self.assertEqual(foreign.read_text(), "[Desktop Entry]\nName=Meu\n")

    def test_own_types_only_for_extensions_without_a_type(self):
        rows = ra.build_plan(self.root, lambda ext: "application/x-zerosize")
        package = ra.mime_package(rows)
        self.assertIn('<glob pattern="*.sms"/>', package)
        self.assertNotIn('pattern="*.zip"', package)


if __name__ == "__main__":
    unittest.main()
