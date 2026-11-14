import csv
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import zlib
from unittest.mock import patch

from PIL import Image
from tools.roms import save_journey as m
from tools.roms import save_journey_ui as ui


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rom = self.root / "roms/sms/Game [A], One.sms"
        self.rom.parent.mkdir(parents=True)
        self.rom.write_bytes(b"fixture")
        self.core = self.root / "emulator/linux/cores/test.so"
        self.core.parent.mkdir(parents=True)
        self.core.write_bytes(b"core")
        self.store = m.Store(self.root, "sms", self.rom, self.core)
        self.source = self.root / "emulator/data/RetroArch/states/Game [A], One.state"
        self.source.write_bytes(b"native-state")
        self.png = self.root / "image.png"
        Image.new("RGB", (64, 48), "navy").save(self.png)
        self.meta = {"core_sha256": m.digest(self.core), "session_seconds": 30,
                     "total_observed_seconds": 90, "saved_at": "2026-11-13T20:00:00-03:00"}

    def capture(self):
        return self.store.capture(self.source, "state", self.meta, self.png)

    def session(self):
        config = self.root / "emulator/data/RetroArch/retroarch.cfg"
        config.write_text('savestate_auto_index = "false"\n' +
                          f'libretro_directory = "{self.core.parent}"\n')
        (config.parent / "saves").mkdir(exist_ok=True)
        info = config.parent / "info/test.info"
        info.parent.mkdir(exist_ok=True)
        info.write_text('systemid = "sms"\nsavestate = "true"\n')
        operation = self.root / "outputs/operations/fixture"
        operation.mkdir(parents=True, exist_ok=True)
        context = {"system_id": "sms", "rom": self.rom.relative_to(self.root).as_posix(),
                   "core": self.core.relative_to(self.root).as_posix(),
                   "content": self.rom.relative_to(self.root).as_posix(),
                   "config": config.relative_to(self.root).as_posix()}
        session = m.RetroArchSession(self.root, context, operation)
        self.addCleanup(session.lease.close)
        return session

    def test_native_bytes_are_preserved_and_png_embeds_capture_metadata(self):
        sequence = self.capture()
        row = self.store.get(sequence)
        self.assertEqual(self.source.read_bytes(), b"native-state")
        self.assertEqual((self.store.directory / row["metadata"]["payload"]).read_bytes(), b"native-state")
        with Image.open(self.store.directory / row["preview"]) as image:
            self.assertEqual(json.loads(image.info["EmuAtlas"])["sequence"], sequence)
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 128))

    def test_identical_rewrite_has_one_version(self):
        self.assertEqual(self.capture(), 1)
        self.assertIsNone(self.capture())
        self.assertEqual(self.store.count(), 1)

    def test_same_payload_in_different_slots_is_physically_shared(self):
        self.capture()
        second = self.source.with_name(self.source.name + "2")
        second.write_bytes(self.source.read_bytes())
        self.store.capture(second, "state", self.meta)
        self.assertEqual(len(list((self.store.directory / "objects").iterdir())), 1)

    def test_rewriting_a_native_slot_retains_old_version(self):
        first = self.capture()
        self.source.write_bytes(b"next-state")
        second = self.capture()
        self.assertEqual(self.store.get(first)["sequence"], 1)
        self.assertEqual(self.store.get(second)["sequence"], 2)
        self.assertEqual(len(list((self.store.directory / "objects").iterdir())), 2)

    def test_favorites_precede_newest_and_pagination_is_stable(self):
        for number in range(20):
            self.source.write_bytes(str(number).encode())
            self.store.capture(self.source, "state", self.meta)
        self.store.annotate(2, favorite=True)
        self.assertEqual([r["sequence"] for r in self.store.rows(5)], [2, 20, 19, 18, 17])
        self.assertEqual(self.store.rows(5, 5)[0]["sequence"], 16)

    def test_tags_stage_and_note_roundtrip(self):
        self.capture()
        self.store.annotate(1, tags=["boss", "challenge"], stage=12, note="Antes da arena")
        row = self.store.get(1)
        self.assertEqual(row["stage"], 12)
        self.assertEqual(row["tags"], ["boss", "challenge"])
        self.assertIn("Fase 12", ui.badges(row))

    def test_invalid_annotation_keeps_previous_data(self):
        self.capture()
        for fields in [{"tags": ["unknown"]}, {"stage": -1}, {"stage": 10000}, {"note": "a"*501}]:
            with self.assertRaises(ValueError):
                self.store.annotate(1, **fields)
        self.assertEqual(self.store.get(1)["revision"], 0)

    def test_missing_selection_has_clear_error(self):
        with self.assertRaises(ValueError):
            self.store.annotate(99, favorite=True)

    def test_time_updates_are_idempotent_per_session(self):
        self.store.session("one", 40)
        self.store.session("one", 45, "closed")
        self.store.session("two", 15, "closed")
        self.assertEqual(self.store.total_seconds(), 60)

    def test_bad_thumbnail_keeps_native_state_and_records_image_error(self):
        self.png.write_bytes(b"invalid")
        sequence = self.capture()
        self.assertIsNone(self.store.get(sequence)["preview"])
        self.assertIn("image_error", self.store.get(sequence)["metadata"])

    def test_insufficient_space_preserves_native_file(self):
        with patch.object(m.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1})()):
            with self.assertRaises(OSError):
                self.capture()
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.source.read_bytes(), b"native-state")

    def test_symlink_source_is_rejected(self):
        link = self.source.with_name("alias.state")
        link.symlink_to(self.source.name)
        with self.assertRaises(ValueError):
            self.store.capture(link, "state", self.meta)

    def test_traversal_and_noncanonical_rom_are_rejected(self):
        with self.assertRaises(ValueError):
            m.safe_path(self.root, "../outside")
        with self.assertRaises(ValueError):
            m.Store(self.root, "sms", self.root/"other/Game.sms", self.core)

    def test_export_is_utf8_csv_and_escapes_formulas(self):
        self.capture()
        self.store.annotate(1, note="=NOW()")
        path = self.root / "saves.csv"
        self.store.export_csv(path)
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        self.assertEqual(rows[0]["Nota"], "'=NOW()")

    def test_database_survives_reopening(self):
        self.capture()
        reopened = m.Store(self.root, "sms", self.rom, self.core)
        self.assertEqual(reopened.get(1)["sha256"], m.digest(self.source))

    def test_session_config_is_ephemeral_and_network_stays_disabled(self):
        session = self.session()
        self.assertEqual(session.config["savestate_auto_index"], "false")
        values = m.config_values(session.options)
        self.assertEqual(values["stdin_cmd_enable"], "true")
        self.assertNotIn("network_cmd_enable", values)
        self.assertEqual(values["savestate_thumbnail_enable"], "true")

    def test_launcher_context_must_match_core_and_content(self):
        session = self.session()
        with self.assertRaises(ValueError):
            session.prepare(["retroarch", "-L", "/other/core.so", str(self.rom)])
        command = session.prepare(["retroarch", "-L", str(self.core), str(self.rom)])
        self.assertEqual(command[-2], "--appendconfig")

    def test_scanner_escapes_title_brackets_and_ignores_foreign_files(self):
        session = self.session()
        foreign = self.source.with_name("Game A, One.state")
        foreign.write_bytes(b"other")
        self.assertIn(self.source, session.paths())
        self.assertNotIn(foreign, session.paths())
        self.png.rename(Path(str(self.source)+".png"))
        self.assertNotIn(Path(str(self.source)+".png"), session.paths())

    def test_status_supports_commas_and_partial_stdout(self):
        session = self.session()
        session.lines.put("GET_STATUS PLAYING fixture,Game [A], One,crc32=12")
        session.read_status()
        self.assertEqual(session.status_generation, 0)
        session.lines.put("34\n")
        session.read_status()
        self.assertEqual(session.subject, "Game [A], One")
        self.assertEqual(session.status, "PLAYING")
        self.assertEqual(session.status_generation, 1)
        session.read_status()
        self.assertEqual(session.status_generation, 1)
        session.close()

    def test_loading_refuses_different_core_or_inactive_game(self):
        self.capture()
        session = self.session()
        with self.assertRaisesRegex(ValueError, "conteúdo ativo"):
            session.begin_load(1)
        session.status, session.subject = "PLAYING", self.rom.stem
        session.core_hash = "changed"
        with self.assertRaisesRegex(ValueError, "core mudou"):
            session.begin_load(1)

    def test_battery_links_record_actual_companion_delay(self):
        self.capture()
        source = self.source.with_suffix(".srm")
        source.write_bytes(b"sram")
        battery = self.store.capture(source, "save", self.meta)
        self.store.pair(battery, 1)
        row = self.store.get(battery)
        self.assertEqual(row["companion"], 1)
        self.assertEqual(row["metadata"]["companion_delay_seconds"], 0)

    def test_loading_never_overwrites_existing_native_slot(self):
        self.capture()
        occupied = Path(str(self.source)+"1")
        occupied.write_bytes(b"private-slot")
        session = self.session()
        session.status, session.subject = "PLAYING", self.rom.stem
        sent = []
        session.send = sent.append
        session.begin_load(1)
        self.assertEqual(occupied.read_bytes(), b"private-slot")
        self.assertGreaterEqual(session.load_pending["slot"], 1_000_000)
        self.assertFalse(session.load_pending["target"].exists())
        self.assertEqual(sent, ["SAVE_STATE"])
        session.close()
        self.assertTrue(occupied.exists())
        self.assertFalse(Path(str(self.source)+"2").exists())

    def test_loading_cache_is_published_after_backup_and_preserves_old_bytes(self):
        self.capture()
        session = self.session()
        session.status, session.subject = "PAUSED", self.rom.stem
        session.send = lambda command: None
        session.begin_load(1)
        backup = Path(str(self.source)+"2")
        backup.write_bytes(b"current-state")
        session.store.capture(backup, "state", self.meta)
        session.advance_load()
        target = session.load_pending["target"]
        self.assertEqual(target.read_bytes(), b"native-state")
        self.assertEqual(backup.read_bytes(), b"current-state")
        session.close()
        self.assertFalse(target.exists())

    def test_native_rtc_is_a_dependency_and_does_not_create_another_card(self):
        source = self.source.with_suffix(".srm")
        source.write_bytes(b"save")
        source.with_suffix(".rtc").write_bytes(b"clock")
        sequence = self.store.capture(source, "save", self.meta)
        self.assertEqual(self.store.count(), 1)
        dependency = self.store.get(sequence)["metadata"]["dependencies"][0]
        self.assertEqual((self.store.directory / dependency["payload"]).read_bytes(), b"clock")
        session = self.session()
        self.assertNotIn(source.with_suffix(".rtc"), session.paths())
        session.close()

    def test_missing_info_refuses_monitor_before_any_command_is_sent(self):
        session = self.session()
        session.close()
        (self.root/"emulator/data/RetroArch/info/test.info").unlink()
        config = self.root/"emulator/data/RetroArch/retroarch.cfg"
        context = {"system_id": "sms", "rom": self.rom.relative_to(self.root).as_posix(),
                   "core": self.core.relative_to(self.root).as_posix(),
                   "content": self.rom.relative_to(self.root).as_posix(),
                   "config": config.relative_to(self.root).as_posix()}
        with self.assertRaisesRegex(ValueError, "systemid"):
            m.RetroArchSession(self.root, context, self.root/"outputs/operations/fixture")

    def test_game_identity_is_shared_between_linux_and_windows_core_extensions(self):
        windows = m.Store(self.root, "sms", self.rom, self.core.with_suffix(".dll"))
        self.assertEqual(windows.game_id, self.store.game_id)

    def test_exclusive_publish_never_overwrites_a_destination(self):
        source, target = self.root/"staged", self.root/"target"
        source.write_bytes(b"owned")
        target.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            m.publish_exclusive(source, target)
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertEqual(source.read_bytes(), b"owned")

    def test_companion_state_uses_only_one_blocking_native_task(self):
        session = self.session()
        session.status, session.subject = "PLAYING", self.rom.stem
        session.child = type("Child", (), {"poll": lambda self: None, "stdin": io.BytesIO()})()
        sent = []
        session.send = sent.append
        source = self.root/"emulator/data/RetroArch/saves"/(self.rom.stem+".srm")
        source.write_bytes(b"battery")
        session.capture(source, "save")
        self.assertEqual(sent, ["SAVE_STATE"])
        session.child = None
        session.close()

    def test_changed_rom_is_refused_while_preserved_state_remains_intact(self):
        self.store.capture(self.source, "state", {**self.meta, "content_sha256": m.digest(self.rom)})
        self.rom.write_bytes(b"replacement")
        session = self.session()
        session.status, session.subject = "PLAYING", self.rom.stem
        with self.assertRaisesRegex(ValueError, "conteúdo do jogo mudou"):
            session.begin_load(1)
        self.assertEqual(self.store.get(1)["sha256"], m.digest(self.source))
        session.close()

    def test_rollback_after_database_insert_failure_leaves_no_visible_half_entry(self):
        with patch.object(self.store, "make_preview", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.capture()
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.capture(), 1)

    def test_order_uses_actual_time_across_timezone_offsets(self):
        self.store.capture(self.source, "state", {**self.meta, "saved_at": "2026-11-13T22:00:00+03:00"})
        self.source.write_bytes(b"newer")
        self.store.capture(self.source, "state", {**self.meta, "saved_at": "2026-11-13T18:00:00-03:00"})
        self.assertEqual([row["sequence"] for row in self.store.rows()], [2, 1])

    def test_simultaneous_identical_captures_commit_one_entry(self):
        errors = []
        def capture():
            try:
                self.capture()
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=capture) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(self.store.count(), 1)

    def test_crash_recovery_removes_only_matching_loading_cache(self):
        target = self.source.with_name(self.source.name+"1000000")
        target.write_bytes(b"derived-copy")
        with self.store.connect() as db:
            db.execute("INSERT INTO loading_cache VALUES(?,?,?)",
                       (target.relative_to(self.root).as_posix(), m.digest(target), json.dumps(m.signature(target))))
        self.store.clean_loading_cache()
        self.assertFalse(target.exists())

    def test_crash_recovery_preserves_divergent_loading_cache(self):
        target = self.source.with_name(self.source.name+"1000000")
        target.write_bytes(b"derived-copy")
        with self.store.connect() as db:
            db.execute("INSERT INTO loading_cache VALUES(?,?,?)",
                       (target.relative_to(self.root).as_posix(), m.digest(target), json.dumps(m.signature(target))))
        target.write_bytes(b"later-user-change")
        self.store.clean_loading_cache()
        self.assertEqual(target.read_bytes(), b"later-user-change")

    @staticmethod
    def rastate():
        return b"RASTATE\x01MEM " + (8).to_bytes(4, "little") + b"12345678END " + bytes(4)

    def test_complete_native_container_is_valid(self):
        self.source.write_bytes(self.rastate())
        self.assertEqual(m.validate_state(self.source), "complete_rastate_v1")

    def test_missing_terminator_is_incomplete(self):
        self.source.write_bytes(self.rastate()[:-8])
        with self.assertRaises(BlockingIOError):
            m.validate_state(self.source)

    def test_duplicate_memory_chunk_is_rejected(self):
        data = self.rastate()
        self.source.write_bytes(data[:-8]+data[8:])
        with self.assertRaises(ValueError):
            m.validate_state(self.source)

    def test_rzip_checks_full_container_and_checksum(self):
        data = self.rastate()
        compressed = zlib.compress(data)
        header = b"#RZIPv\x01#"+(128).to_bytes(4,"little")+len(data).to_bytes(8,"little")
        encoded = header+len(compressed).to_bytes(4,"little")+compressed
        self.source.write_bytes(encoded)
        self.assertEqual(m.validate_state(self.source), "complete_rastate_v1")
        self.source.write_bytes(encoded[:-1]+bytes([encoded[-1]^1]))
        with self.assertRaisesRegex(ValueError, "checksum"):
            m.validate_state(self.source)
        self.source.write_bytes(encoded[:-2])
        with self.assertRaises(BlockingIOError):
            m.validate_state(self.source)

    def test_rzip_size_limit_is_checked_before_decompression(self):
        self.source.write_bytes(b"#RZIPv\x01#"+(128).to_bytes(4,"little")+(m.MAX_PAYLOAD+1).to_bytes(8,"little"))
        with self.assertRaisesRegex(ValueError, "limites"):
            m.validate_state(self.source)

    def test_legacy_core_specific_format_remains_explicit(self):
        self.assertEqual(m.validate_state(self.source), "opaque_legacy_or_core_format")

    def test_failed_close_releases_monitor_lease(self):
        session = self.session()
        with patch.object(session.store, "clean_loading_cache", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                session.close()
        self.assertTrue(session.lease.closed)

    def test_confirmation_waits_for_subsequent_native_status_before_handoff(self):
        session = self.session()
        session.status_generation = 10
        session.load_pending = {"phase": "confirm", "render_status": 10, "sequence": 1,
                                "started": m.time.monotonic()}
        session.advance_load()
        self.assertIsNotNone(session.load_pending)
        session.status_generation = 12
        session.advance_load()
        self.assertIsNone(session.load_pending)
        self.assertEqual(session.last_load_outcome, "loaded")
        session.close()

    def test_issued_load_keeps_waiting_after_timeout(self):
        session = self.session()
        session.load_pending = {"phase": "load", "slot": 123, "target": self.source,
                                "started": m.time.monotonic()-100, "timeout": 20}
        session.advance_load()
        self.assertIsNotNone(session.load_pending)
        self.assertIn("aguardando", session.message)
        session.close()

    def test_normal_launcher_passes_portable_original_content_and_core_context(self):
        from tools.roms.tests.launcher_fixture import load_launcher
        launcher = load_launcher("journey_launcher_fixture")
        project = launcher.ROOT.parent.parent
        core = project / "emulator/linux/retroarch-cores/test.so"
        core.parent.mkdir(parents=True)
        core.write_bytes(b"fixture-core")
        native = project / "emulator/data/RetroArch"
        native.mkdir(parents=True)
        (native / "retroarch.cfg").write_text("")
        rom = project / "roms/sms/Game.sms"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"fixture-rom")
        profile = {"bios_root": str(project / "bios"), "rom_root": str(project / "roms"),
                   "retroarch": {"core_root": str(core.parent), "executable": "/fixture/RetroArch.AppImage",
                                 "userdata_root": "../../emulator/data/RetroArch",
                                 "config_path": "../../emulator/data/RetroArch/retroarch.cfg",
                                 "savefile_directory": "../../emulator/data/RetroArch/saves",
                                 "savestate_directory": "../../emulator/data/RetroArch/states",
                                 "app_id": "org.libretro.RetroArch"}}
        with patch.object(launcher, "run_with_music_paused", return_value=0) as run:
            self.assertEqual(launcher.launch_retroarch(profile, {"rom_subdir": "sms", "core": "test.so"}, rom), 0)
        context = run.call_args.kwargs["journey"]
        self.assertEqual(context["rom"], "roms/sms/Game.sms")
        self.assertEqual(context["content"], context["rom"])
        self.assertEqual(context["core"], "emulator/linux/retroarch-cores/test.so")
        self.assertEqual(run.call_args.kwargs["env"]["APPIMAGELAUNCHER_DISABLE"], "1")

    def test_focus_return_script_requires_owned_game_and_current_ui(self):
        from tools.roms import controller_session as controllers
        from unittest.mock import Mock
        focus = controllers.KWinFocus.__new__(controllers.KWinFocus)
        focus.directory = self.root
        focus.name = "fixture"
        processes = Mock()
        processes.contains.return_value = False
        with patch.object(focus, "script") as run, patch.object(focus, "dbus"):
            focus.restore_window(processes, (123, "game-window", False, "DP-1"))
            run.assert_not_called()
            processes.contains.return_value = True
            focus.restore_window(processes, (123, "game-window", False, "DP-1"))
            code = run.call_args.args[1].read_text()
        self.assertIn(f"current.pid === {os.getpid()}", code)
        self.assertIn("w.pid === 123", code)
        self.assertIn('String(w.internalId) === "game-window"', code)

    def test_p1_chord_is_gated_until_release_and_other_players_do_not_navigate(self):
        from unittest.mock import Mock
        shelf = ui.SaveShelf.__new__(ui.SaveShelf)
        shelf.expanded = shelf.release_gate = False
        shelf.loading = shelf.editor = None
        shelf.pad_down = set()
        shelf.expand = Mock(side_effect=lambda **kwargs: setattr(shelf, "expanded", True))
        shelf.move = Mock()
        self.assertFalse(shelf.controller({"back", "y"}, player=1))
        self.assertTrue(shelf.controller({"back", "y"}))
        shelf.controller({"dpdown"})
        shelf.move.assert_not_called()
        shelf.controller(set())
        shelf.controller({"dpdown"})
        shelf.move.assert_called_once_with(1)

    def test_visual_handoff_waits_for_playing_cycles_before_fading(self):
        from unittest.mock import Mock
        shelf = ui.SaveShelf.__new__(ui.SaveShelf)
        shelf.geometry = (0, 0, 1280, 800)
        shelf.width, shelf.row_height, shelf.scale = 320, 114, 1
        shelf.reduce_motion = False
        shelf.loading = {"row": {"sequence": 1}, "start": 0, "revision": 0}
        shelf.transition, shelf.transition_canvas = Mock(), Mock()
        shelf.rounded, shelf.text, shelf.collapse = Mock(), Mock(), Mock()
        shelf.photo = Mock(return_value=None)
        shelf.backend = Mock(load_pending=None, load_requested=None, revision=1,
                             last_load_outcome="loaded", status="PAUSED", status_generation=10,
                             message="Pronto")
        shelf.animate(1)
        shelf.backend.request.assert_called_once_with("resume")
        shelf.backend.status, shelf.backend.status_generation = "PLAYING", 11
        shelf.animate(1.3)
        shelf.collapse.assert_not_called()
        shelf.backend.status_generation = 12
        shelf.animate(1.5)
        shelf.collapse.assert_not_called()
        shelf.animate(2)
        shelf.collapse.assert_called_once()
        self.assertIsNone(shelf.loading)

    def test_failed_load_keeps_shelf_open_with_explanation(self):
        from unittest.mock import Mock
        shelf = ui.SaveShelf.__new__(ui.SaveShelf)
        shelf.geometry = (0, 0, 1280, 800)
        shelf.width, shelf.row_height, shelf.scale = 320, 114, 1
        shelf.reduce_motion = True
        shelf.loading = {"row": {"sequence": 1}, "start": 0, "revision": 0}
        shelf.transition, shelf.transition_canvas = Mock(), Mock()
        shelf.rounded, shelf.text, shelf.draw, shelf.collapse = Mock(), Mock(), Mock(), Mock()
        shelf.photo = Mock(return_value=None)
        shelf.backend = Mock(load_pending=None, load_requested=None, revision=1,
                             last_load_outcome="failed", message="Core mudou")
        shelf.animate(1)
        self.assertIsNone(shelf.loading)
        self.assertEqual(shelf.notice, "Core mudou")
        shelf.collapse.assert_not_called()
        shelf.backend.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
