"""Shared name resolution excludes declared tracks and preserves physical ambiguity."""
import csv
import json
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
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
            {"id": "test", "extensions": ["cue", "gdi", "bin", "wav", "raw", "chd", "zip", "img", "m3u"]}]}))

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

    def test_multiple_rekeys_can_share_one_verified_existing_payload(self):
        media = self.root / "media/test/cover-front"
        media.mkdir(parents=True)
        (media / "Game.png").write_bytes(b"exact same content")
        actions = []
        for name in ["Track 1.png", "Track 2.png"]:
            source = media / name
            source.write_bytes(b"exact same content")
            actions.append({"action": "media_rekey_link", "source": str(source),
                            "destination": str(media / "Game.png"), "system_id": "test",
                            "media_type": "cover-front", "game_title_hint": "Game",
                            "reuses_existing_payload": True, "classification": "media",
                            "item_identity": "Game", "reason": "reviewed fixture",
                            "reference_check": "preserved_by_relative_symlink",
                            "validation_result": "fixture_bytes", "dependency_check": "fixture"})
        request = self.root / "request.json"
        request.write_text(json.dumps({"actions": actions}))
        with ExitStack() as stack:
            for key, value in {"PROJECT_ROOT": self.root, "MEDIA_DEST_ROOT": self.root / "media",
                               "OPS_ROOT": self.root / "outputs/operations"}.items():
                stack.enter_context(patch.object(romtool, key, value))
            stack.enter_context(patch.object(romtool, "load_policy", return_value={}))
            stack.enter_context(patch.object(romtool, "ensure_allowed"))
            stack.enter_context(patch.object(romtool, "append_event"))
            stack.enter_context(patch.object(romtool, "supersede_prior_plans", return_value=[]))
            stack.enter_context(patch.object(romtool, "rebuild_history_index"))
            manifest = romtool.plan_actions(request)
        self.assertEqual(len(manifest["items"]), 2)
        self.assertTrue(all(i["media"]["reuses_existing_payload"] for i in manifest["items"]))
        self.assertTrue(all((media / name).is_file() for name in ["Track 1.png", "Track 2.png"]))

    def test_playlist_is_the_media_subject_for_its_discs(self):
        for name in ["Game Disc 1.img", "Game Disc 2.img"]:
            (self.roms / name).write_bytes(b"fixture")
        (self.roms / "Game.m3u").write_text("Game Disc 1.img\nGame Disc 2.img\n")
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Game": {"Game.m3u"}})
        self.assertEqual(media_session._track_paths(self.roms), set())

    def test_recursive_playlist_members_remain_for_review(self):
        (self.roms / "Game.img").write_bytes(b"fixture")
        (self.roms / "Game.m3u").write_text("Nested.m3u\nGame.img\n")
        (self.roms / "Nested.m3u").write_text("Game.m3u\n")
        self.assertEqual(set(media_session._rom_names(self.root, "test")), {"Game", "Nested"})
        self.assertEqual(len(media_session._rom_names(self.root, "test")["Game"]), 2)

    def test_descriptor_outside_platform_does_not_suppress_local_candidates(self):
        (self.roms / "Game.bin").write_bytes(b"fixture")
        (self.roms.parent / "Outside.bin").write_bytes(b"fixture")
        (self.roms / "Game.cue").write_text('FILE "Game.bin" BINARY\nFILE "../Outside.bin" BINARY\n')
        self.assertEqual(media_session._track_paths(self.roms), set())

    def test_configured_runtime_filters_archives_and_container_directories(self):
        profiles = self.root / "frontend/RetroFE/Config/profiles"
        profiles.mkdir()
        (profiles / "host-linux.json").write_text(json.dumps({"collections": {"Test": {
            "rom_subdir": "test", "extensions": "cue,bin,img,m3u"}}}))
        (self.roms / "Game").mkdir()
        (self.roms / "Game/Game.cue").write_text('FILE "Track.bin" BINARY\n')
        (self.roms / "Game/Track.bin").write_bytes(b"fixture")
        (self.roms / "Game.zip").write_bytes(b"archive fixture")
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Game": {"Game/Game.cue"}})

    def test_explicitly_supported_directory_remains_a_subject(self):
        profiles = self.root / "frontend/RetroFE/Config/profiles"
        profiles.mkdir()
        (profiles / "host-linux.json").write_text(json.dumps({"collections": {"Test": {
            "rom_subdir": "test", "extensions": "img", "accepts_directories": True}}}))
        (self.roms / "Game.v1.0").mkdir()
        self.assertEqual(media_session._rom_names(self.root, "test"), {"Game.v1.0": {"Game.v1.0"}})

    def test_disjoint_runtime_extensions_do_not_enable_unconfigured_fallback(self):
        profiles = self.root / "frontend/RetroFE/Config/profiles"
        profiles.mkdir()
        (profiles / "host-linux.json").write_text(json.dumps({"collections": {"Test": {
            "rom_subdir": "test", "extensions": "unknown"}}}))
        (self.roms / "Game.bin").write_bytes(b"fixture")
        self.assertEqual(media_session._rom_names(self.root, "test"), {})


class SemanticMediaRekeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "media/test/cover/Game.png"
        self.target = self.root / "media/test/background/Game (Alt 2).png"
        self.source.parent.mkdir(parents=True)
        self.target.parent.mkdir(parents=True)
        self.source.write_bytes(b"reviewed original illustration")
        (self.root / "roms/test").mkdir(parents=True)
        self.review_id = "media-cover-review-fixture"
        self.review_path = self.root / "outputs/operations" / self.review_id / "manifest.json"
        self.review_path.parent.mkdir(parents=True)
        with (self.root / "outputs/catalogo-arquivos.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["source", "destination_proposal", "current_path_status", "metadata_recheck",
                                                       "function_hint", "current_absolute_path", "current_sha256",
                                                       "current_size_bytes", "current_operation_id"])
            writer.writeheader()
            writer.writerow({"source": "RetroFE_game_media", "destination_proposal": str(self.source), "function_hint": "cover"})
        (self.root / "outputs/estado-final-verificacao.json").write_text("{}\n")
        (self.root / "outputs/RELATORIO-CONSOLIDACAO.md").write_text("# Fixture\n")
        self.review = {"operation_id": self.review_id, "kind": "media_cover_review", "read_only": True,
                       "status": "completed_verified", "items": [{"source": "test/cover/Game.png",
                       "sha256": romtool.sha256_file(self.source), "classification": "not-cover",
                       "target_media_type": "background"}]}
        self.review_path.write_text(json.dumps(self.review))
        self.action = {"action": "media_rekey_link", "source": str(self.source), "destination": str(self.target),
                       "system_id": "test", "media_type": "background", "source_media_type": "cover",
                       "semantic_review_operation_id": self.review_id, "game_title_hint": "Game",
                       "classification": "media", "item_identity": "Game illustration",
                       "reason": "Selected visual classification with intact bytes",
                       "reference_check": "preserved_by_relative_symlink", "validation_result": "fixture",
                       "dependency_check": "Exact single game entry"}
        self.stack = self.enterContext(ExitStack())
        for key, value in {"PROJECT_ROOT": self.root, "MEDIA_DEST_ROOT": self.root / "media",
                           "OUTPUT_ROOT": self.root / "outputs", "OPS_ROOT": self.root / "outputs/operations"}.items():
            self.stack.enter_context(patch.object(romtool, key, value))
        for key, value in {"load_policy": {}, "frontend_processes_running": {},
                           "retrofe_processes_running": [], "supersede_prior_plans": []}.items():
            self.stack.enter_context(patch.object(romtool, key, return_value=value))
        for key in ("ensure_allowed", "append_event", "rebuild_history_index"):
            self.stack.enter_context(patch.object(romtool, key))

    def plan(self):
        request = self.root / "request.json"
        request.write_text(json.dumps({"actions": [self.action]}))
        return romtool.plan_actions(request)

    def test_selected_role_change_preserves_bytes_alias_and_rollback(self):
        original = self.source.read_bytes()
        manifest = self.plan()
        item = manifest["items"][0]
        self.assertEqual(item["media"]["semantic_review_sha256"], romtool.sha256_file(self.review_path))
        romtool.apply_media_action(item, manifest["operation_id"])
        self.assertTrue(self.source.is_symlink())
        self.assertFalse(self.source.readlink().is_absolute())
        self.assertEqual(self.target.read_bytes(), original)
        romtool.rollback_media_action(item, manifest["operation_id"])
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.target.exists())

    def test_normal_apply_verify_and_rollback_entry_points(self):
        from types import SimpleNamespace
        original = self.source.read_bytes()
        manifest = self.plan()
        operation_id = manifest["operation_id"]
        applied = romtool.apply_operation(operation_id, True)
        self.assertEqual(applied["status"], "completed_verified")
        with (self.root / "outputs/catalogo-arquivos.csv").open() as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["function_hint"], "background")
        self.assertEqual(row["current_absolute_path"], str(self.target))
        verified = romtool.command_media_verify(SimpleNamespace(operation_id=operation_id))
        self.assertTrue(all(item["status"] == "verified_relative_alias_and_payload" for item in verified["items"]))
        self.assertEqual(romtool.rollback_operation(operation_id, True)["status"], "rolled_back_verified")
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.source.read_bytes(), original)
        with (self.root / "outputs/catalogo-arquivos.csv").open() as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["function_hint"], "cover")
        self.assertEqual(row["current_absolute_path"], str(self.source))

    def test_changed_review_blocks_the_whole_normal_apply_preflight(self):
        manifest = self.plan()
        self.review_path.write_text(json.dumps(self.review) + "\n")
        with self.assertRaisesRegex(ValueError, "mudou desde o plano"):
            romtool.apply_operation(manifest["operation_id"], True)
        stored = json.loads((romtool.operation_dir(manifest["operation_id"]) / "manifest.json").read_text())
        self.assertEqual(stored["status"], "prepared")
        self.assertFalse(self.source.is_symlink())
        self.assertFalse(self.target.exists())

    def test_missing_review_role_or_source_hash_never_moves(self):
        for field, value in (("target_media_type", "manual"), ("classification", "front-cover"), ("sha256", "0" * 64)):
            with self.subTest(field=field):
                review = json.loads(json.dumps(self.review))
                review["items"][0][field] = value
                self.review_path.write_text(json.dumps(review))
                with self.assertRaisesRegex(ValueError, "não comprova"):
                    self.plan()
                self.assertTrue(self.source.is_file())
                self.assertFalse(self.target.exists())

    def test_review_mutation_blocks_before_payload_move(self):
        manifest = self.plan()
        self.review_path.write_text(json.dumps(self.review) + "\n")
        with self.assertRaisesRegex(ValueError, "mudou desde o plano"):
            romtool.apply_media_action(manifest["items"][0], manifest["operation_id"])
        self.assertFalse(self.source.is_symlink())
        self.assertFalse(self.target.exists())

    def test_other_category_pairs_and_missing_review_are_rejected(self):
        for change in ({"source_media_type": "cover-front"}, {"semantic_review_operation_id": ""}):
            with self.subTest(change=change):
                details = dict(self.action, **change)
                with self.assertRaises(ValueError):
                    romtool.media_semantic_rekey_proof(self.source, details, romtool.sha256_file(self.source))

    def test_active_frontend_blocks_selected_role_change(self):
        manifest = self.plan()
        with patch.object(romtool, "frontend_processes_running", return_value={"esde": [123]}):
            with self.assertRaisesRegex(RuntimeError, "Feche os frontends"):
                romtool.apply_media_action(manifest["items"][0], manifest["operation_id"])
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.target.exists())
        item = manifest["items"][0]
        romtool.apply_media_action(item, manifest["operation_id"])
        with patch.object(romtool, "frontend_processes_running", return_value={"esde": [123]}):
            with self.assertRaisesRegex(RuntimeError, "Feche os frontends"):
                romtool.rollback_media_action(item, manifest["operation_id"])
        self.assertTrue(self.source.is_symlink())
        self.assertTrue(self.target.is_file())
