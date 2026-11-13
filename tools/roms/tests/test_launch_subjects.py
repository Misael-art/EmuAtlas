"""Shared name resolution excludes declared tracks and preserves physical ambiguity."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.roms import media_session, media_stray, metadata_ingest, romtool


class LaunchSubjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.roms = self.root / "roms/test"
        self.roms.mkdir(parents=True)
        config = self.root / "frontend/RetroFE/Config"
        config.mkdir(parents=True)
        (config / "platform-catalog.json").write_text(json.dumps({"systems": [
            {"id": "test", "extensions": ["cue", "gdi", "bin", "wav", "raw", "chd", "zip"]}]}))

    def test_cue_binary_and_wave_tracks_are_excluded_in_both_callers(self):
        for name in ["Game Track 1.bin", "Game Track 2.wav"]:
            (self.roms / name).write_bytes(b"track fixture")
        (self.roms / "Game.CUE").write_text('FILE "Game Track 1.bin" BINARY\nFILE "Game Track 2.wav" WAVE\n')
        self.assertEqual(set(media_stray._rom_stems(self.root, "test")), {"Game"})
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Game": {"Game.CUE"}})

    def test_gdi_tracks_are_excluded(self):
        (self.roms / "track01.raw").write_bytes(b"audio")
        (self.roms / "track02.bin").write_bytes(b"data")
        (self.roms / "Game.gdi").write_text('2\n1 0 0 2352 track01.raw 0\n2 45000 4 2352 track02.bin 0\n')
        self.assertEqual(set(media_session._rom_names(self.root, "test")), {"Game"})

    def test_standalone_raw_game_is_preserved(self):
        (self.roms / "Standalone.bin").write_bytes(b"game")
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Standalone": {"Standalone.bin"}})

    def test_saves_and_external_art_are_not_additional_rom_candidates(self):
        for name in ["Game.bin", "Game.sav", "Game.png"]:
            (self.roms / name).write_bytes(b"fixture")
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Game": {"Game.bin"}})

    def test_different_formats_retain_physical_ambiguity(self):
        for name in ["Game.zip", "Game.chd"]:
            (self.roms / name).write_bytes(b"different fixture")
        self.assertEqual(media_session._rom_names(self.root, "test")["Game"], {"Game.zip", "Game.chd"})

    def test_declared_track_does_not_hide_an_unrelated_same_named_file(self):
        for directory in [self.roms / "a", self.roms / "b"]:
            directory.mkdir()
            (directory / "track01.bin").write_bytes(b"fixture")
        (self.roms / "a/Game.cue").write_text('FILE "track01.bin" BINARY\n')
        self.assertEqual(media_session._rom_names(self.root, "test")["track01"], {"b/track01.bin"})

    def test_missing_reference_is_preserved_for_review(self):
        (self.roms / "track01.bin").write_bytes(b"fixture")
        (self.roms / "Game.cue").write_text('FILE "missing.bin" BINARY\n')
        self.assertIn("track01", media_session._rom_names(self.root, "test"))

    def test_directory_name_with_dots_is_preserved(self):
        (self.roms / "Game.v1.0").mkdir()
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Game.v1.0": {"Game.v1.0"}})

    def test_legacy_session_without_catalog_still_discovers_inputs(self):
        (self.root / "frontend/RetroFE/Config/platform-catalog.json").unlink()
        (self.roms / "Game.bin").write_bytes(b"fixture")
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Game": {"Game.bin"}})

    def test_metadata_merge_preserves_existing_track_and_ignores_scraped_updates(self):
        (self.roms / "track01.bin").write_bytes(b"fixture")
        (self.roms / "Game.cue").write_text('FILE "track01.bin" BINARY\n')
        old = b"<gameList><game><path>./track01.bin</path><favorite>true</favorite></game></gameList>"
        new = b"<gameList><game><path>./track01.bin</path><desc>Track text</desc></game><game><path>./Game.cue</path><name>Game</name></game></gameList>"
        output, report = metadata_ingest.merge_gamelist_bytes(old, new, self.roms)
        self.assertEqual(report["excluded_dependencies"], ["./track01.bin"])
        self.assertEqual(report["added"], 1)
        self.assertNotIn(b"Track text", output)
        self.assertIn(b"<favorite>true</favorite>", output)

    def test_metadata_merge_does_not_add_new_track(self):
        (self.roms / "track01.bin").write_bytes(b"fixture")
        (self.roms / "Game.cue").write_text('FILE "track01.bin" BINARY\n')
        output, report = metadata_ingest.merge_gamelist_bytes(None, b"<gameList><game><path>./track01.bin</path></game></gameList>", self.roms)
        self.assertIsNone(output)
        self.assertEqual(report["added"], 0)
        self.assertEqual(report["excluded_dependencies"], ["./track01.bin"])

    def test_hyperlist_projection_skips_tracks_and_keeps_descriptor(self):
        (self.roms / "track01.bin").write_bytes(b"fixture")
        (self.roms / "Game.cue").write_text('FILE "track01.bin" BINARY\n')
        gamelists = self.root / "frontend/ES-DE/UserData/gamelists"
        (gamelists / "test").mkdir(parents=True)
        (gamelists / "test/gamelist.xml").write_text("<gameList><game><path>./track01.bin</path></game><game><path>./Game.cue</path><name>Game</name></game></gameList>")
        entries, skipped = metadata_ingest._materializer().esde_entries("test", self.root, gamelists)
        self.assertEqual(set(entries), {"Game"})
        self.assertEqual(skipped["dependent_track"], 1)
