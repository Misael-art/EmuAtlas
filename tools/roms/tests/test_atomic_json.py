"""Small fixture-only regressions for durable JSON and lightweight recording."""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import struct
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
        self.assertEqual(events, ["file", "replace"] + ["directory"] * (1 + len(self.path.parent.parents)))
        self.assert_no_temporary_files()

    @unittest.skipIf(os.name == "nt", "POSIX parent directory fsync")
    def test_new_parent_links_are_synced_up_to_existing_ancestor(self):
        directories = []
        synced = []
        handles = {}
        real_open = os.open
        real_sync = os.fsync

        def opened(path, flags):
            directories.append(path)
            descriptor = real_open(path, flags)
            handles[descriptor] = path
            return descriptor

        def sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                synced.append(handles[descriptor])
            return real_sync(descriptor)

        target = self.root / "new" / "operation" / "manifest.json"
        with patch.object(romtool.os, "open", side_effect=opened), patch.object(romtool.os, "fsync", side_effect=sync):
            romtool.atomic_json(target, {})
        expected = [target.parent, *target.parent.parents]
        self.assertEqual(directories, expected)
        self.assertEqual(synced, expected)

    @unittest.skipIf(os.name == "nt", "POSIX directory fsync")
    def test_caller_created_parent_is_also_durably_linked(self):
        target = self.root / "operations/new-operation/manifest.json"
        target.parent.mkdir(parents=True)
        directories = []
        real_sync = romtool.sync_directory_chain
        def sync(directory):
            directories.append(directory)
            real_sync(directory)
        with patch.object(romtool, "sync_directory_chain", side_effect=sync):
            romtool.atomic_json(target, {"recorded": True})
        self.assertEqual(directories, [target.parent])
        self.assertEqual(json.loads(target.read_text()), {"recorded": True})

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
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_symlink_destination_is_preserved_and_rejected(self):
        alias = self.root / "alias.json"
        alias.symlink_to(self.path.name)
        with self.assertRaisesRegex(ValueError, "regular"):
            romtool.atomic_json(alias, {"secret": True})
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_temporary_files()

    @unittest.skipUnless(romtool.sys.platform.startswith("linux"), "Linux access ACL")
    def test_custom_acl_is_preserved_before_staged_data_is_written(self):
        entries = [(1, 6, 0xffffffff), (2, 0, os.getuid() + 1), (4, 4, 0xffffffff),
                   (16, 4, 0xffffffff), (32, 0, 0xffffffff)]
        acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
        try:
            os.setxattr(self.path, "system.posix_acl_access", acl)
        except OSError as exc:
            if exc.errno == errno.ENOTSUP:
                self.skipTest("fixture filesystem lacks POSIX ACL")
            raise
        expected = os.getxattr(self.path, "system.posix_acl_access")
        original = romtool._copy_replacement_acl
        def copied(source, temporary):
            self.assertEqual(temporary.stat().st_size, 0)
            original(source, temporary)
            self.assertEqual(os.getxattr(temporary, "system.posix_acl_access"), expected)
        with patch.object(romtool, "_copy_replacement_acl", side_effect=copied):
            romtool.atomic_json(self.path, {"status": "after"})
        self.assertEqual(os.getxattr(self.path, "system.posix_acl_access"), expected)
        self.assertEqual(json.loads(self.path.read_text()), {"status": "after"})

    def test_acl_copy_failure_preserves_original(self):
        with patch.object(romtool, "_copy_replacement_acl", side_effect=PermissionError("ACL refused")):
            with self.assertRaises(PermissionError):
                romtool.atomic_json(self.path, {"status": "after"})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_temporary_files()

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

    def test_environment_change_default_keeps_index_refresh(self):
        args = argparse.Namespace(change_type='fixture', reason='fixture', details_json='{}')
        with patch.multiple(romtool, PROJECT_ROOT=self.root, OPS_ROOT=self.ops), \
                patch.object(romtool, 'rebuild_history_index') as rebuild:
            result = romtool.command_record_change(args)
        rebuild.assert_called_once_with()
        self.assertNotIn('history_index_refresh', result)

    def test_environment_change_deferral_keeps_manifest_and_both_journals(self):
        args = argparse.Namespace(change_type='fixture', reason='fixture', details_json='{}',
                                  defer_history_index=True)
        with patch.multiple(romtool, PROJECT_ROOT=self.root, OPS_ROOT=self.ops), \
                patch.object(romtool, 'rebuild_history_index') as rebuild:
            result = romtool.command_record_change(args)
        rebuild.assert_not_called()
        manifest = self.ops / result['operation_id'] / 'manifest.json'
        self.assertEqual(json.loads(manifest.read_text())['history_index_refresh'], 'deferred')
        for path in (self.ops / 'journal.jsonl', manifest.parent / 'events.jsonl'):
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(events[-1]['event'], 'environment_change_recorded')

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

    def interrupted_record(self):
        self.args.defer_history_index = True
        with patch.object(romtool, "sync_directory_chain", side_effect=OSError("directory sync failed")):
            with self.assertRaisesRegex(OSError, "record-artifacts --operation-id artifacts-"):
                self.record()
        return next(self.ops.glob("*/manifest.json"))

    def test_post_publication_failure_resumes_the_same_operation(self):
        path = self.interrupted_record()
        original = json.loads(path.read_text())
        self.assertEqual(original["status"], "recording")
        self.args.operation_id = path.parent.name
        result = self.record()
        self.assertEqual(result["operation_id"], original["operation_id"])
        self.assertEqual(result["created_at"], original["created_at"])
        self.assertEqual(result["record_resume_count"], 1)
        self.assertEqual(len(list(self.ops.glob("*/manifest.json"))), 1)
        events = [json.loads(line) for line in (path.parent / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "project_artifacts_record_resumed")

    def test_resume_refuses_changed_artifacts(self):
        path = self.interrupted_record()
        before = path.read_bytes()
        self.args.operation_id = path.parent.name
        (self.root / "source.py").write_text("changed = True\n")
        with self.assertRaisesRegex(ValueError, "divergem"):
            self.record()
        self.assertEqual(path.read_bytes(), before)

    def test_journal_creation_syncs_both_directory_chains(self):
        self.args.defer_history_index = True
        original = romtool.sync_directory_chain
        synced = []
        def sync(directory):
            synced.append(directory)
            original(directory)
        with patch.object(romtool, "sync_directory_chain", side_effect=sync):
            result = self.record()
        self.assertIn(self.ops, synced)
        self.assertEqual(synced.count(self.ops / result["operation_id"]), 3)

    def test_resume_after_event_failure_keeps_one_manifest(self):
        self.args.defer_history_index = True
        original = romtool.append_event
        def failed(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("journal directory sync failed")
        with patch.object(romtool, "append_event", side_effect=failed):
            with self.assertRaisesRegex(OSError, "--operation-id"):
                self.record()
        path = next(self.ops.glob("*/manifest.json"))
        self.args.operation_id = path.parent.name
        result = self.record()
        self.assertEqual(result["operation_id"], path.parent.name)
        self.assertEqual(len(list(self.ops.glob("*/manifest.json"))), 1)


class WindowsAclTests(unittest.TestCase):
    def api_fixture(self, protected=True, divergent=False):
        control = 0x8004 | (0x1000 if protected else 0)
        acl = struct.pack("<BBHHH", 2, 0, 8, 0, 0)
        header = struct.pack("<BBHLLLL", 1, 0, control, 0, 0, 0, 20)
        api = Mock()
        def get_security(path, flags, buffer, length, needed):
            payload = header + acl
            if divergent and path == "temporary":
                payload = header + struct.pack("<BBHHH", 4, 0, 8, 0, 0)
            needed._obj.value = len(payload)
            if buffer is None:
                return 0
            ctypes.memmove(buffer, payload, len(payload))
            return 1
        api.GetFileSecurityW.side_effect = get_security
        api.SetFileSecurityW.return_value = 1
        return api

    def test_windows_dacl_and_inheritance_are_copied_and_rechecked(self):
        for protected, flag in [(True, 0x80000004), (False, 0x20000004)]:
            with self.subTest(protected=protected):
                api = self.api_fixture(protected=protected)
                with patch.object(ctypes, "WinDLL", return_value=api, create=True):
                    romtool._copy_windows_acl(Path("original"), Path("temporary"))
                self.assertEqual(api.SetFileSecurityW.call_args.args[:2], ("temporary", flag))
                self.assertEqual(api.GetFileSecurityW.call_count, 4)

    def test_windows_acl_drift_refuses_publication(self):
        api = self.api_fixture(divergent=True)
        with patch.object(ctypes, "WinDLL", return_value=api, create=True):
            with self.assertRaisesRegex(OSError, "diverge"):
                romtool._copy_windows_acl(Path("original"), Path("temporary"))

    def test_windows_acl_failure_propagates(self):
        api = self.api_fixture()
        api.SetFileSecurityW.return_value = 0
        with patch.object(ctypes, "WinDLL", return_value=api, create=True), \
                patch.object(ctypes, "get_last_error", return_value=5, create=True), \
                patch.object(ctypes, "WinError", return_value=PermissionError("DACL refused"), create=True):
            with self.assertRaises(PermissionError):
                romtool._copy_windows_acl(Path("original"), Path("temporary"))



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
