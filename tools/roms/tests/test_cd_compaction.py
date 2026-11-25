"""Raw CD proof and recovery through the normal compact CD entry points."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from tools.roms import catalog_projection, cd_compaction as cd, romtool, runtime_install


class CdCompactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Library with spaces"
        self.root.mkdir()
        self.tool_root = self.root / "tools/roms"
        self.roms = self.root / "roms/psx"
        self.roms.mkdir(parents=True)
        self.cue = self.roms / "Game.cue"
        self.cue.write_text('FILE "Data.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n'
                            'FILE "Audio.bin" BINARY\n TRACK 02 AUDIO\n INDEX 00 00:00:00\n INDEX 01 00:02:00\n')
        self.data = b"D" * (400 * 2352)
        self.audio = b"\0" * (150 * 2352) + b"A" * (200 * 2352)
        (self.roms / "Data.bin").write_bytes(self.data)
        (self.roms / "Audio.bin").write_bytes(self.audio)
        self.playlist = self.roms / "Game.m3u"
        self.playlist.write_bytes(b"# selected disc\r\nGame.cue\r\n")
        self.gamelist = self.root / "frontend/ES-DE/UserData/gamelists/psx/gamelist.xml"
        self.gamelist.parent.mkdir(parents=True)
        self.gamelist.write_bytes(b'<gameList><game><path>./Game.cue</path><name>Personal</name></game></gameList>')
        self.profile = {"id": "duckstation-chd-cd", "systems": ["psx"], "runtime": "DuckStation",
                        "tool_executable": "python3", "timeout_seconds": 10, "codecs": "cdlz,cdzl,cdfl",
                        "hunk_bytes": 19584, "minimum_gain_bytes": 1, "minimum_gain_ratio": 0.01}
        self.tool_root.joinpath("config").mkdir(parents=True)
        (self.tool_root / "config/compression-profiles.json").write_text(json.dumps({"cd_profiles": [self.profile]}))
        config = self.root / "frontend/RetroFE/Config/profiles"
        config.mkdir(parents=True)
        self.host = config / "host-linux.json"
        self.host.write_text(json.dumps({"collections": {"PSX": {"rom_subdir": "psx", "runtime_candidate": "DuckStation",
                                                                "command": "fixture", "extensions": "cue,chd,m3u"}}}))
        for name, value in {"PROJECT_ROOT": self.root, "TOOL_ROOT": self.tool_root,
                            "OUTPUT_ROOT": self.root / "outputs", "OPS_ROOT": self.root / "outputs/operations"}.items():
            mocked = patch.object(romtool, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name, value in {"frontend_processes_running": {}, "append_event": None,
                            "compression_reference_index": {}}.items():
            mocked = patch.object(romtool, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        guard = patch.object(runtime_install, "_library_guard")
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        converter = patch.object(cd, "_run", side_effect=self.convert)
        self.converter = converter.start()
        self.addCleanup(converter.stop)
        space = patch.object(cd.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100 * 1024**3, 0, 100 * 1024**3))
        space.start()
        self.addCleanup(space.stop)
        self.api = vars(romtool)

    def convert(self, manifest, args, log, api):
        if args[0] == "createcd":
            self.assertEqual(args[-6:], ["-np", "1", "-c", "cdlz,cdzl,cdfl", "-hs", "19584"])
            Path(args[args.index("-o") + 1]).write_bytes(b"verified fixture CHD")
        if args[0] == "extractcd":
            output = Path(args[args.index("-o") + 1])
            output.write_text(self.cue_text)
            (output.parent / "Data.bin").write_bytes(self.data)
            (output.parent / "Audio.bin").write_bytes(self.audio)

    @property
    def cue_text(self):
        return ('FILE "Data.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n'
                'FILE "Audio.bin" BINARY\n TRACK 02 AUDIO\n INDEX 00 00:00:00\n INDEX 01 00:02:00\n')

    def plan(self):
        return cd.plan("psx", "Game.cue", self.profile["id"], self.api)

    def apply(self, manifest):
        with romtool.media_session.write_lock(self.api):
            return cd.apply(manifest, True, self.api)

    def test_complete_roundtrip_publication_and_rollback(self):
        before = self.gamelist.read_bytes(), self.playlist.read_bytes()
        manifest = self.plan()
        result = self.apply(manifest)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(self.cue.exists())
        self.assertFalse((self.roms / "Data.bin").exists())
        self.assertIn(b"Game.chd", self.gamelist.read_bytes())
        self.assertEqual(self.playlist.read_bytes(), b"# selected disc\r\nGame.chd\r\n")
        self.assertLess(manifest["net_disk_freed_bytes"], 0)
        self.assertGreater(manifest["active_library_reduction_bytes"], 0)
        evidence = catalog_projection._evidence(self.root, manifest)
        self.assertEqual(len(evidence), 3)
        self.assertTrue(all(item["action"] == "project" for item in evidence))
        self.assertEqual(self.apply(manifest)["status"], "verified")
        cd.rollback(manifest, True, self.api)
        self.assertEqual(self.cue.read_text(), self.cue_text)
        self.assertEqual((self.roms / "Data.bin").read_bytes(), self.data)
        self.assertEqual((self.roms / "Audio.bin").read_bytes(), self.audio)
        self.assertEqual((self.gamelist.read_bytes(), self.playlist.read_bytes()), before)
        cd.rollback(manifest, True, self.api)

    def test_normal_cli_plan_and_load(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(romtool.main(["compact", "cd", "plan", "--system-id", "psx", "--source", "Game.cue"]), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(cd._load(value["operation_id"], self.api)["source_proof"]["tracks"][1]["index_01"], 150)

    def track_gamelist(self, primary=True, extra="", flags=True):
        main = '<game><path>./Game.cue</path><name>Personal</name><playcount>7</playcount></game>' if primary else ""
        managed = '<hidden>true</hidden><nomultiscrape>true</nomultiscrape><nogamecount>true</nogamecount>' if flags else ""
        tracks = ''.join('<game><path>./' + name + '</path><name>Personal</name><playcount>7</playcount>'
                         + managed + extra + '</game>' for name in ("Data.bin", "Audio.bin"))
        return ('<?xml version="1.0"?><gameList>\r\n<!-- keep -->' + main + tracks + '</gameList>').encode()

    def test_dependent_track_entries_are_consolidated_and_rollback_is_exact(self):
        before = self.track_gamelist()
        self.gamelist.write_bytes(before)
        manifest = self.plan()
        self.assertEqual(len(manifest["retired_track_metadata"]), 2)
        self.apply(manifest)
        games = ET.fromstring(self.gamelist.read_bytes()).findall("game")
        self.assertEqual([game.findtext("path") for game in games], ["./Game.chd"])
        self.assertEqual(games[0].findtext("playcount"), "7")
        self.assertIn(b"<!-- keep -->", self.gamelist.read_bytes())
        cd.rollback(manifest, True, self.api)
        self.assertEqual(self.gamelist.read_bytes(), before)

    def test_track_only_metadata_can_supply_one_visible_primary(self):
        self.gamelist.write_bytes(self.track_gamelist(primary=False))
        manifest = self.plan()
        self.apply(manifest)
        games = ET.fromstring(self.gamelist.read_bytes()).findall("game")
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].findtext("path"), "./Game.chd")
        self.assertIsNone(games[0].find("hidden"))
        self.assertEqual(games[0].findtext("playcount"), "7")

    def test_track_personal_metadata_is_never_discarded(self):
        for extra in ("<favorite>true</favorite>", "<playcount>9</playcount>"):
            with self.subTest(extra=extra):
                before = self.track_gamelist(extra=extra)
                self.gamelist.write_bytes(before)
                with self.assertRaisesRegex(ValueError, "Metadados|repetido"):
                    self.plan()
                self.assertEqual(self.gamelist.read_bytes(), before)
        self.converter.assert_not_called()

    def test_track_personal_flags_are_preserved_for_review(self):
        self.gamelist.write_bytes(self.track_gamelist(flags=False).replace(
            b"<playcount>7</playcount></game>", b"<playcount>7</playcount><hidden>false</hidden></game>"))
        with self.assertRaisesRegex(ValueError, "Sinalizadores pessoais"):
            self.plan()

    def test_duplicate_primary_or_track_is_refused(self):
        for duplicate in (b'<game><path>./Game.cue</path></game>', b'<game><path>./Data.bin</path></game>'):
            with self.subTest(duplicate=duplicate):
                self.gamelist.write_bytes(self.track_gamelist().replace(b"</gameList>", duplicate + b"</gameList>"))
                with self.assertRaisesRegex(ValueError, "entrada principal|repete uma faixa"):
                    self.plan()

    def test_loose_track_reference_is_refused(self):
        self.gamelist.write_bytes(b'<gameList><folder><path>./Data.bin</path></folder></gameList>')
        with self.assertRaisesRegex(ValueError, "fora de uma entrada"):
            self.plan()

    def test_retired_metadata_proof_is_guarded_before_conversion(self):
        self.gamelist.write_bytes(self.track_gamelist())
        manifest = self.plan()
        manifest["retired_track_metadata"][0]["raw_xml"] = "changed proof"
        with self.assertRaisesRegex(ValueError, "Referências CD mudaram"):
            self.apply(manifest)
        self.converter.assert_not_called()

    def test_completed_checkpoint_without_catalog_items_is_reconciled(self):
        manifest = self.plan()
        self.apply(manifest)
        manifest.pop("items")
        cd._save(manifest, self.api)
        self.assertEqual(self.apply(cd._load(manifest["operation_id"], self.api))["status"], "verified")
        self.assertEqual(len(cd._load(manifest["operation_id"], self.api)["items"]), 3)

    def test_one_bin_multiple_tracks_has_same_sector_proof(self):
        original = cd.inspect_cue(self.cue, self.api)
        (self.roms / "Merged.bin").write_bytes(self.data + self.audio)
        self.cue.write_text('FILE "Merged.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n'
                            ' TRACK 02 AUDIO\n INDEX 00 00:05:25\n INDEX 01 00:07:25\n')
        self.assertEqual(cd.inspect_cue(self.cue, self.api)["tracks"], original["tracks"])

    def test_unapproved_directives_are_preserved(self):
        for directive in ('PREGAP 00:02:00', 'FLAGS DCP', 'INDEX 02 00:00:01', 'FILE "other.wav" WAVE'):
            with self.subTest(directive=directive):
                self.cue.write_text(self.cue_text + directive + "\n")
                with self.assertRaisesRegex(ValueError, "Diretiva"):
                    self.plan()

    def test_incoherent_indices_and_raw_size_are_refused(self):
        for old, new in (("INDEX 01 00:02:00", "INDEX 01 99:00:00"),
                         ("TRACK 02 AUDIO", "TRACK 03 AUDIO"),
                         ("INDEX 01 00:02:00", "INDEX 01 00:60:00"),
                         ("INDEX 00 00:00:00", "INDEX 00 00:03:00")):
            with self.subTest(new=new):
                self.cue.write_text(self.cue_text.replace(old, new))
                with self.assertRaises(ValueError):
                    self.plan()
        self.cue.write_text(self.cue_text)
        (self.roms / "Data.bin").write_bytes(b"unaligned")
        with self.assertRaisesRegex(ValueError, "desalinhado"):
            self.plan()

    def test_shared_bin_is_refused(self):
        (self.roms / "Other.cue").write_text(self.cue_text)
        with self.assertRaisesRegex(ValueError, "compartilhada"):
            self.plan()

    def test_direct_track_in_playlist_is_refused(self):
        self.playlist.write_text("Data.bin\n")
        with self.assertRaisesRegex(ValueError, "diretamente"):
            self.plan()

    def test_unknown_frontend_reference_is_refused(self):
        with patch.object(romtool, "compression_reference_index", return_value={"input": {
                "status": "referenced", "path_matches": [str(self.root / "launcher.ini")]}}):
            with self.assertRaisesRegex(ValueError, "Referências CD pendentes"):
                self.plan()

    def test_changed_input_and_metadata_refuse_before_conversion(self):
        manifest = self.plan()
        self.playlist.write_text("# personal change\nGame.cue\n")
        with self.assertRaisesRegex(ValueError, "Referências CD mudaram"):
            self.apply(manifest)
        self.converter.assert_not_called()
        self.playlist.write_bytes(b"# selected disc\r\nGame.cue\r\n")
        (self.roms / "Data.bin").write_bytes(b"E" * len(self.data))
        with self.assertRaisesRegex(ValueError, "Conjunto CD mudou"):
            self.apply(manifest)

    def test_candidate_with_different_track_content_never_publishes(self):
        manifest = self.plan()
        self.audio = b"Z" * len(self.audio)
        with self.assertRaisesRegex(ValueError, "CD reconstruído diverge"):
            self.apply(manifest)
        self.assertTrue(self.cue.exists())
        self.assertFalse(self.cue.with_suffix(".chd").exists())

    def test_no_gain_retains_original_set_and_candidate(self):
        self.profile["minimum_gain_bytes"] = 10 * 1024**2
        (self.tool_root / "config/compression-profiles.json").write_text(json.dumps({"cd_profiles": [self.profile]}))
        manifest = self.plan()
        result = self.apply(manifest)
        self.assertEqual(result["status"], "no_gain_original_preserved")
        self.assertTrue(self.cue.exists())
        self.assertTrue((romtool.operation_dir(manifest["operation_id"]) / "candidate.chd").exists())

    def test_cut_after_verified_conversion_resumes_existing_candidate(self):
        manifest = self.plan()
        original = cd._publication
        with patch.object(cd, "_publication", side_effect=OSError("cut")):
            with self.assertRaises(OSError):
                self.apply(manifest)
        self.converter.reset_mock()
        with patch.object(cd, "_publication", wraps=original):
            self.apply(cd._load(manifest["operation_id"], self.api))
        self.assertFalse(any(call.args[1][0] == "createcd" for call in self.converter.call_args_list))

    def test_cut_during_publication_resumes_backend_intents(self):
        manifest = self.plan()
        original = runtime_install._library_rename_exclusive
        count = 0

        def cut(source, destination):
            nonlocal count
            original(source, destination)
            count += 1
            if count == 2:
                raise OSError("cut after rename")

        with patch.object(runtime_install, "_library_rename_exclusive", side_effect=cut):
            with self.assertRaises(OSError):
                self.apply(manifest)
        self.assertEqual(self.apply(cd._load(manifest["operation_id"], self.api))["status"], "verified")

    def test_cut_after_backend_apply_resumes_verification(self):
        manifest = self.plan()
        with patch.object(cd, "verify", side_effect=OSError("cut")):
            with self.assertRaises(OSError):
                self.apply(manifest)
        self.assertEqual(self.apply(cd._load(manifest["operation_id"], self.api))["status"], "verified")

    def test_rollback_preserves_later_metadata_changes(self):
        manifest = self.plan()
        self.apply(manifest)
        self.gamelist.write_bytes(self.gamelist.read_bytes() + b"\n<!-- personal -->")
        with self.assertRaisesRegex(ValueError, "Referência mudou"):
            cd.rollback(manifest, True, self.api)
        self.assertTrue(self.cue.with_suffix(".chd").exists())

    def test_rollback_cut_can_resume(self):
        manifest = self.plan()
        self.apply(manifest)
        original = runtime_install._library_rename_exclusive

        def cut(source, destination):
            original(source, destination)
            raise OSError("cut")

        with patch.object(runtime_install, "_library_rename_exclusive", side_effect=cut):
            with self.assertRaises(OSError):
                cd.rollback(manifest, True, self.api)
        self.assertEqual(cd.rollback(cd._load(manifest["operation_id"], self.api), True, self.api)["status"], "rolled_back")

    def test_sidecar_is_guarded_and_retained(self):
        sidecar = self.roms / "Game.sbi"
        sidecar.write_bytes(b"SBI\0fixture")
        manifest = self.plan()
        self.apply(manifest)
        self.assertEqual(sidecar.read_bytes(), b"SBI\0fixture")
        sidecar.write_bytes(b"personal change")
        with self.assertRaisesRegex(ValueError, "SBI mudou"):
            cd.verify(manifest, self.api)

    def test_subchannel_container_has_explicit_block(self):
        (self.roms / "Game.sub").write_bytes(b"subchannel")
        with self.assertRaisesRegex(ValueError, "suporte adicional"):
            self.plan()

    def test_target_race_never_overwrites(self):
        manifest = self.plan()
        target = self.cue.with_suffix(".chd")
        target.write_bytes(b"personal CHD")
        with self.assertRaises(FileExistsError):
            self.apply(manifest)
        self.assertEqual(target.read_bytes(), b"personal CHD")
        self.assertTrue(self.cue.exists())

    def test_runtime_change_blocks_apply(self):
        manifest = self.plan()
        self.host.write_text("{}")
        with self.assertRaises(ValueError):
            self.apply(manifest)
        self.converter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
