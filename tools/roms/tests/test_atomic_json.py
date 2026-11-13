"""Small fixture-only regressions for durable JSON and lightweight recording."""
import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.roms import romtool


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "manifest.json"
        self.path.write_text('{"status": "before"}\n', encoding="utf-8")
        self.before = self.path.read_bytes()

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.root.glob(".romtool-*.tmp")), [])

    def test_nested_creation_keeps_unicode_format_and_newline(self):
        target = self.root / "new" / "operation" / "manifest.json"
        romtool.atomic_json(target, {"status": "revisão"})
        self.assertEqual(target.read_text(encoding="utf-8"), '{\n  "status": "revisão"\n}\n')

    @unittest.skipIf(os.name == "nt", "POSIX directory fsync")
    def test_file_sync_precedes_replace_and_directory_sync_follows(self):
        events = []
        real_sync = os.fsync
        real_replace = os.replace

        def sync(descriptor):
            events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            return real_sync(descriptor)

        def replace(source, target):
            events.append("replace")
            self.assertEqual(json.loads(source.read_text()), {"status": "after"})
            return real_replace(source, target)

        with patch.object(romtool.os, "fsync", side_effect=sync), patch.object(romtool.os, "replace", side_effect=replace):
            romtool.atomic_json(self.path, {"status": "after"})
        self.assertEqual(events, ["file", "replace", "directory"])
        self.assert_no_temporary_files()

    @unittest.skipIf(os.name == "nt", "POSIX parent directory fsync")
    def test_new_parent_links_are_synced_up_to_existing_ancestor(self):
        directories = []
        real_open = os.open

        def opened(path, flags):
            directories.append(path)
            return real_open(path, flags)

        target = self.root / "new" / "operation" / "manifest.json"
        with patch.object(romtool.os, "open", side_effect=opened):
            romtool.atomic_json(target, {})
        self.assertEqual(directories, [target.parent, target.parent.parent, self.root])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_existing_private_mode_is_preserved(self):
        self.path.chmod(0o600)
        romtool.atomic_json(self.path, {})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_serialization_failure_preserves_original(self):
        with self.assertRaises(TypeError):
            romtool.atomic_json(self.path, {"unsupported": object()})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_temporary_files()

    def test_file_sync_failure_preserves_original(self):
        with patch.object(romtool.os, "fsync", side_effect=OSError("file sync failed")):
            with self.assertRaisesRegex(OSError, "file sync failed"):
                romtool.atomic_json(self.path, {})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_temporary_files()

    def test_replace_failure_preserves_original(self):
        with patch.object(romtool.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                romtool.atomic_json(self.path, {})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_temporary_files()

    @unittest.skipIf(os.name == "nt", "POSIX directory fsync")
    def test_directory_sync_failure_propagates_after_publication(self):
        real_sync = os.fsync

        def sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("directory sync failed")
            return real_sync(descriptor)

        with patch.object(romtool.os, "fsync", side_effect=sync):
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                romtool.atomic_json(self.path, {"status": "after"})
        self.assertEqual(json.loads(self.path.read_text()), {"status": "after"})
        self.assert_no_temporary_files()

    def test_overlapping_writes_use_different_temporary_files(self):
        real_replace = os.replace
        temporaries = []

        def replace(source, target):
            temporaries.append(source)
            if len(temporaries) == 1:
                romtool.atomic_json(self.path, {"writer": "second"})
            return real_replace(source, target)

        with patch.object(romtool.os, "replace", side_effect=replace):
            romtool.atomic_json(self.path, {"writer": "first"})
        self.assertEqual(len(set(temporaries)), 2)
        self.assertEqual(json.loads(self.path.read_text()), {"writer": "first"})
        self.assert_no_temporary_files()

    def test_temporary_name_collision_preserves_existing_file(self):
        existing = self.root / ".romtool-collision.tmp"
        existing.write_bytes(b"preserved temporary")
        with patch.object(romtool.uuid, "uuid4", return_value=argparse.Namespace(hex="collision")):
            with self.assertRaises(FileExistsError):
                romtool.atomic_json(self.path, {})
        self.assertEqual(existing.read_bytes(), b"preserved temporary")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_legacy_temporary_is_preserved(self):
        legacy = self.path.with_name(self.path.name + ".tmp")
        legacy.write_bytes(b"earlier recovery evidence")
        romtool.atomic_json(self.path, {})
        self.assertEqual(legacy.read_bytes(), b"earlier recovery evidence")

    @unittest.skipIf(os.name == "nt", "POSIX filename component limit")
    def test_long_target_name_uses_short_temporary_name(self):
        target = self.root / ("a" * 250 + ".json")
        romtool.atomic_json(target, {"status": "after"})
        self.assertEqual(json.loads(target.read_text()), {"status": "after"})
        self.assert_no_temporary_files()

    def test_windows_branch_syncs_file_without_posix_directory_calls(self):
        windows_os = Mock(wraps=os)
        windows_os.name = "nt"
        with patch.object(romtool, "os", windows_os):
            romtool.atomic_json(self.path, {"status": "after"})
        windows_os.fsync.assert_called_once()
        windows_os.open.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), {"status": "after"})


class LightweightArtifactRecordingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ops = self.root / "outputs" / "operations"
        (self.root / "source.py").write_text("value = 1\n", encoding="utf-8")
        self.args = argparse.Namespace(artifacts_json='["source.py"]', change_type="fixture", reason="fixture")

    def record(self):
        with patch.object(romtool, "PROJECT_ROOT", self.root), patch.object(romtool, "OPS_ROOT", self.ops):
            return romtool.command_record_artifacts(self.args)

    def test_default_refreshes_history_index(self):
        with patch.object(romtool, "rebuild_history_index") as rebuild:
            result = self.record()
        rebuild.assert_called_once_with()
        self.assertNotIn("history_index_refresh", result)

    def test_deferred_record_persists_manifest_and_event(self):
        self.args.defer_history_index = True
        with patch.object(romtool, "rebuild_history_index") as rebuild:
            result = self.record()
        rebuild.assert_not_called()
        manifest = self.ops / result["operation_id"] / "manifest.json"
        self.assertEqual(json.loads(manifest.read_text())["history_index_refresh"], "deferred")
        self.assertEqual(result["details"]["artifact_count"], 1)
        for journal in [self.ops / "journal.jsonl", manifest.parent / "events.jsonl"]:
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "project_artifacts_recorded")



class AtomicBytesTests(unittest.TestCase):
    def test_binary_payload_and_explicit_mode_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            payload = b"\x00\xff\x80binary\n"
            romtool.atomic_write_bytes(path, payload, 0o640)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(path.parent.glob(".romtool-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
