"""Selected M3U sets preserve discs and recover interrupted publication/reversal."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.roms import disc_sets, media_session, romtool


class DiscSetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.roms = self.root / "roms/psx"
        self.roms.mkdir(parents=True)
        config = self.root / "frontend/RetroFE/Config"
        config.mkdir(parents=True)
        (config / "platform-catalog.json").write_text(json.dumps({"systems": [
            {"id": "psx", "extensions": ["img", "cue", "bin", "m3u"]}]}))
        for n in (1, 2):
            (self.roms / f"Game (Disc {n}).img").write_bytes(f"disc fixture {n}".encode())
        self.api = {"PROJECT_ROOT": self.root, "media_session": media_session,
                    "operation_dir": lambda oid: self.root / "outputs/operations" / oid,
                    "atomic_json": romtool.atomic_json, "sha256_file": romtool.sha256_file,
                    "path_snapshot": romtool.path_snapshot, "reject_symlink_components": romtool.reject_symlink_components,
                    "verify_cue": romtool.verify_cue, "verify_gdi": romtool.verify_gdi, "verify_m3u": romtool.verify_m3u,
                    "now": lambda: "fixture-time", "append_event": lambda *a, **k: None,
                    "frontend_processes_running": lambda: {"retrofe": [], "esde": []}}
        self.request = {"system_id": "psx", "playlist": "Game.m3u", "disc_count": 2,
                        "identity_evidence": "reviewed fixture identities and complete set",
                        "runtime_evidence": "fixture runtime supports relative M3U",
                        "discs": [{"path": f"Game (Disc {n}).img", "disc_number": n,
                                   "identity_evidence": f"fixture internal ID {n}"} for n in (1, 2)]}

    def test_plan_apply_verify_and_reversible_preservation(self):
        manifest = disc_sets.plan(self.request, self.api)
        self.assertFalse((self.roms / "Game.m3u").exists())
        result = disc_sets.apply(manifest, True, self.api)
        self.assertEqual(result["status"], "verified")
        self.assertEqual((self.roms / "Game.m3u").read_text(), "Game (Disc 1).img\nGame (Disc 2).img\n")
        disc_sets.rollback(manifest, True, self.api)
        retained = self.api["operation_dir"](manifest["operation_id"]) / "rollback-playlist.m3u"
        self.assertTrue(retained.is_file())
        self.assertFalse((self.roms / "Game.m3u").exists())
        self.assertEqual(len(list(self.roms.glob("*.img"))), 2)

    def test_changed_disc_refuses_before_publication(self):
        manifest = disc_sets.plan(self.request, self.api)
        (self.roms / "Game (Disc 2).img").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            disc_sets.apply(manifest, True, self.api)
        self.assertFalse((self.roms / "Game.m3u").exists())

    def test_existing_playlist_is_preserved(self):
        manifest = disc_sets.plan(self.request, self.api)
        (self.roms / "Game.m3u").write_bytes(b"personal")
        with self.assertRaises(FileExistsError):
            disc_sets.apply(manifest, True, self.api)
        self.assertEqual((self.roms / "Game.m3u").read_bytes(), b"personal")

    def test_wrong_order_count_duplicate_and_unsafe_names_are_rejected(self):
        variants = []
        for field, value in [("disc_count", 3), ("playlist", "../Game.m3u"), ("playlist", "CON.m3u"),
                             ("runtime_evidence", ""), ("identity_evidence", "")]:
            request = copy.deepcopy(self.request)
            request[field] = value
            variants.append(request)
        for path in ["../escape.img", "#Game.img", "Game\n.img", " Game.img", "/absolute.img"]:
            request = copy.deepcopy(self.request)
            request["discs"][0]["path"] = path
            variants.append(request)
        request = copy.deepcopy(self.request)
        request["discs"][1]["disc_number"] = 1
        variants.append(request)
        request = copy.deepcopy(self.request)
        request["discs"][1]["path"] = request["discs"][0]["path"]
        variants.append(request)
        for request in variants:
            with self.subTest(request=request), self.assertRaises((ValueError, FileNotFoundError)):
                disc_sets.plan(request, self.api)

    def test_cue_tracks_are_dependencies_and_never_disc_entries(self):
        (self.roms / "Track.bin").write_bytes(b"track")
        (self.roms / "Disc.cue").write_text('FILE "Track.bin" BINARY\n')
        self.request["discs"][0]["path"] = "Disc.cue"
        manifest = disc_sets.plan(self.request, self.api)
        self.assertEqual({r["path"] for r in manifest["inputs"]}, {"Disc.cue", "Track.bin", "Game (Disc 2).img"})
        self.request["discs"][0]["path"] = "Track.bin"
        with self.assertRaises(ValueError):
            disc_sets.plan(self.request, self.api)

    def test_publication_resumes_after_link_before_checkpoint(self):
        manifest = disc_sets.plan(self.request, self.api)
        original = disc_sets.os.link
        def interrupted(source, target):
            original(source, target)
            raise OSError("cut after link")
        with patch.object(disc_sets.os, "link", side_effect=interrupted):
            with self.assertRaises(OSError):
                disc_sets.apply(manifest, True, self.api)
        saved = disc_sets._load(manifest["operation_id"], self.api)
        self.assertEqual(disc_sets.apply(saved, True, self.api)["status"], "verified")

    def test_rollback_resumes_after_rename(self):
        manifest = disc_sets.plan(self.request, self.api)
        disc_sets.apply(manifest, True, self.api)
        original = disc_sets.os.rename
        def interrupted(source, target):
            original(source, target)
            raise OSError("cut after rename")
        with patch.object(disc_sets.os, "rename", side_effect=interrupted):
            with self.assertRaises(OSError):
                disc_sets.rollback(manifest, True, self.api)
        saved = disc_sets._load(manifest["operation_id"], self.api)
        self.assertEqual(disc_sets.rollback(saved, True, self.api)["status"], "rolled_back")

    def test_changed_playlist_blocks_rollback(self):
        manifest = disc_sets.plan(self.request, self.api)
        disc_sets.apply(manifest, True, self.api)
        (self.roms / "Game.m3u").write_text("personal update\n")
        with self.assertRaises(ValueError):
            disc_sets.rollback(manifest, True, self.api)
        self.assertEqual((self.roms / "Game.m3u").read_text(), "personal update\n")

    def test_cli_plan_uses_normal_entry_point(self):
        args = romtool.build_parser().parse_args(["discs", "plan", "--file", "reviewed.json"])
        self.assertEqual(args.discs_command, "plan")

    def test_existing_set_and_windows_name_collision_are_preserved(self):
        (self.roms / "Existing.m3u").write_text("Game (Disc 1).img\nGame (Disc 2).img\n")
        with self.assertRaisesRegex(ValueError, "já tem uma playlist"):
            disc_sets.plan(self.request, self.api)
        self.request["playlist"] = "EXISTING.m3u"
        with self.assertRaises(FileExistsError):
            disc_sets.plan(self.request, self.api)

    def test_two_prepared_names_cannot_publish_the_same_set(self):
        first = disc_sets.plan(self.request, self.api)
        second_request = copy.deepcopy(self.request)
        second_request["playlist"] = "Another name.m3u"
        second = disc_sets.plan(second_request, self.api)
        disc_sets.apply(first, True, self.api)
        with self.assertRaisesRegex(ValueError, "já tem uma playlist"):
            disc_sets.apply(second, True, self.api)
        self.assertFalse((self.roms / "Another name.m3u").exists())
