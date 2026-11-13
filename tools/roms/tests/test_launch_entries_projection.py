"""Frontend flags follow descriptor relationships and preserve personal metadata."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from tools.roms import romtool


class LaunchEntriesProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.front = self.root / "frontend/RetroFE"
        (self.front / "collections/Sony").mkdir(parents=True)
        (self.front / "Config").mkdir()
        (self.front / "Config/media-map.json").write_text("{}")
        (self.front / "settings.conf").write_text("baseItemPath = ../../roms\n")
        self.settings = self.front / "collections/Sony/settings.conf"
        self.settings.write_text("list.path = %BASE_ITEM_PATH%/psx\nlist.extensions = cue,bin,img,m3u\n")
        tools = self.root / "tools/roms"
        (tools / "config").mkdir(parents=True)
        (tools / "config/roots.json").write_text("{}")
        source = Path(__file__).resolve().parents[1] / "materialize-collection-media-paths.py"
        copied = tools / source.name
        shutil.copyfile(source, copied)
        spec = importlib.util.spec_from_file_location("launch_entries_fixture", copied)
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.roms = self.root / "roms/psx"
        self.roms.mkdir(parents=True)
        (self.roms / "Track 01.bin").write_bytes(b"track")
        (self.roms / "Disc.cue").write_text('FILE "Track 01.bin" BINARY\n')
        for n in (1, 2):
            (self.roms / f"Game Disc {n}.img").write_bytes(b"disc fixture")
        (self.roms / "Game.m3u").write_text("Game Disc 1.img\nGame Disc 2.img\n")
        self.gamelist = self.root / "frontend/ES-DE/UserData/gamelists/psx/gamelist.xml"
        self.gamelist.parent.mkdir(parents=True)
        self.gamelist.write_text("<gameList><game><path>./Track 01.bin</path><favorite>true</favorite><playcount>7</playcount></game><game><path>./Game Disc 1.img</path><name>Game title</name><desc>Story</desc><favorite>true</favorite></game></gameList>")
        self.config = self.root / "frontend/ES-DE/UserData/settings/es_settings.xml"
        self.config.parent.mkdir()
        self.config.write_text('<config><bool name="ShowHiddenGames" value="true"/><bool name="ScraperRespectExclusions" value="true"/></config>')
        helper = Path(__file__).resolve().parents[3] / "frontend/ES-DE/configure-rom-directory.py"
        shutil.copyfile(helper, self.root / "frontend/ES-DE/configure-rom-directory.py")
        self.before = {p: p.read_bytes() for p in (self.gamelist, self.config, self.settings)}
        for target, name in [(self.tool, "retrofe_processes"), (romtool, "esde_processes_running")]:
            patcher = patch.object(target, name, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = self.front / "Manifests/entries.json"

    def test_apply_verify_and_rollback_preserve_personal_fields(self):
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        self.assertEqual(manifest["summary"]["dependencies_hidden_esde"], 3)
        self.tool.apply_plan(self.path, manifest)
        self.assertEqual(self.tool.verify_plan(self.path, manifest, {})["failed_files"], 0)
        games = {g.findtext("path"): g for g in ET.parse(self.gamelist).getroot().findall("game")}
        track = games["./Track 01.bin"]
        self.assertEqual(track.findtext("favorite"), "true")
        self.assertEqual(track.findtext("playcount"), "7")
        self.assertEqual(track.findtext("hidden"), "true")
        self.assertEqual(track.findtext("nomultiscrape"), "true")
        self.assertEqual(games["./Game.m3u"].findtext("name"), "Game title")
        self.assertEqual(games["./Game.m3u"].findtext("desc"), "Story")
        self.assertIsNone(games["./Game.m3u"].find("favorite"))
        self.assertIn("Track 01", (self.settings.parent / "exclude.txt").read_text())
        self.tool.rollback_plan(self.path, manifest)
        self.assertEqual({p: p.read_bytes() for p in self.before}, self.before)
        self.assertFalse((self.settings.parent / "exclude.txt").exists())

    def test_descriptor_change_blocks_before_configuration_write(self):
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        (self.roms / "Disc.cue").write_text('FILE "missing.bin" BINARY\n')
        with self.assertRaises(ValueError):
            self.tool.apply_plan(self.path, manifest)
        self.assertEqual({p: p.read_bytes() for p in self.before}, self.before)

    def test_shared_basename_preserves_visible_descriptor(self):
        (self.roms / "Disc.bin").write_bytes(b"track")
        (self.roms / "Disc.cue").write_text('FILE "Disc.bin" BINARY\n')
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        self.assertIn("Disc", manifest["summary"]["shared_basenames_retained"])
        self.tool.apply_plan(self.path, manifest)
        self.assertNotIn("Disc", (self.settings.parent / "exclude.txt").read_text().splitlines())

    def test_unrelated_target_is_rejected(self):
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        manifest["files"][0]["path"] = "../ES-DE/UserData/other.xml"
        with self.assertRaises(ValueError):
            self.tool.apply_plan(self.path, manifest)
        self.assertEqual(self.gamelist.read_bytes(), self.before[self.gamelist])

    def test_running_esde_blocks_apply_and_rollback(self):
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        with patch.object(romtool, "esde_processes_running", return_value=[{"pid": 123}]):
            with self.assertRaises(RuntimeError):
                self.tool.apply_plan(self.path, manifest)
        self.tool.apply_plan(self.path, manifest)
        with patch.object(romtool, "esde_processes_running", return_value=[{"pid": 123}]):
            with self.assertRaises(RuntimeError):
                self.tool.rollback_plan(self.path, manifest)

    def test_second_plan_is_idempotent(self):
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        self.tool.apply_plan(self.path, manifest)
        result = self.tool.make_launch_entries_plan(self.path.with_name("next.json"), "psx")
        self.assertEqual(result["files"], [])

    def test_native_settings_fragment_and_comments_are_preserved(self):
        self.config.write_text('<?xml version="1.0"?>\n<!-- keep setting notes -->\n<bool name="ShowHiddenGames" value="true" />\n<bool name="ScraperRespectExclusions" value="true" />\n')
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        self.tool.apply_plan(self.path, manifest)
        text = self.config.read_text()
        self.assertIn('<!-- keep setting notes -->', text)
        self.assertIn('name="ShowHiddenGames" value="false"', text)
        self.assertNotIn('<config>', text)

    def test_global_base_path_change_blocks_before_configuration_write(self):
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        (self.front / "settings.conf").write_text("baseItemPath = ../../different-roms\n")
        with self.assertRaises(ValueError):
            self.tool.apply_plan(self.path, manifest)
        self.assertEqual(self.gamelist.read_bytes(), self.before[self.gamelist])

    def test_noncanonical_global_base_does_not_rewrite_collection_lists(self):
        (self.front / "settings.conf").write_text("baseItemPath = ../../different-roms\n")
        manifest = self.tool.make_launch_entries_plan(self.path, "psx")
        self.assertEqual(manifest["summary"]["retrofe_collections"], 0)
        self.assertTrue(all(not row["path"].startswith("collections/") for row in manifest["files"]))

    def test_unknown_gamelist_root_is_preserved(self):
        self.gamelist.write_text("<personal><game><path>./Track 01.bin</path></game></personal>")
        before = self.gamelist.read_bytes()
        with self.assertRaises(ValueError):
            self.tool.make_launch_entries_plan(self.path, "psx")
        self.assertEqual(self.gamelist.read_bytes(), before)
