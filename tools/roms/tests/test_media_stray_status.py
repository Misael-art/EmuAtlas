"""Metadata-only diagnosis and early refusal of inconsistent stray journals."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.roms import media_stray, romtool


class MediaStrayStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "outputs"
        self.ops = self.output / "operations"
        self.operation_id = "media-stray-fixture"
        self.operation = self.ops / self.operation_id
        self.operation.mkdir(parents=True)
        self.source = self.root / "roms/media/nes/cover-front/Title.png"
        self.destination = self.root / "media/nes/cover-front/Title.png"
        self.source.parent.mkdir(parents=True)
        self.destination.parent.mkdir(parents=True)
        self.source.write_bytes(b"small fixture payload")
        self.manifest = {"kind": "media_stray", "operation_id": self.operation_id, "status": "prepared",
                         "items": [{"relative_path": "nes/cover-front/Title.png",
                                    "project_relative": self.source.relative_to(self.root).as_posix(),
                                    "destination_relative": self.destination.relative_to(self.root).as_posix(),
                                    "action": "promote_to_canonical"}]}
        self.api = {"PROJECT_ROOT": self.root, "operation_dir": lambda operation: self.ops / operation}
        self.write_manifest()

    def write_manifest(self):
        (self.operation / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def write_events(self, *events):
        rows = []
        for event, state in events:
            rows.append(json.dumps({"operation_id": self.operation_id, "event": event, "status": state}))
        (self.operation / "events.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def inspect(self):
        real_open = Path.open
        allowed = {self.operation / "manifest.json", self.operation / "events.jsonl"}

        def open_control(path, *args, **kwargs):
            if path not in allowed:
                raise AssertionError(f"content read outside operation control files: {path}")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=open_control), \
                patch.object(media_stray, "_sha256", side_effect=AssertionError("payload hash forbidden")):
            return media_stray.status(self.operation_id, self.api)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_prepared_paths_are_consistent_without_payload_hashes_or_writes(self):
        before = self.snapshot()
        result = self.inspect()
        self.assertEqual(result["status"], "consistent_metadata")
        self.assertEqual(result["summary"]["source_kinds"], {"regular": 1})
        self.assertEqual(result["summary"]["destination_kinds"], {"missing": 1})
        self.assertEqual(result["evidence"], {"read_only": True, "payload_hashes_checked": False,
                                           "global_records_checked": False, "last_lifecycle_event": None})
        self.assertEqual(self.snapshot(), before)

    def test_completed_journal_disagrees_with_prepared_manifest(self):
        self.write_events(("media_stray_applied", "completed_verified"))
        result = self.inspect()
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertTrue(any("journal_manifest_status_mismatch" in issue for issue in result["issues"]))

    def test_apply_refuses_conflict_before_hashes_snapshots_or_manifest_writes(self):
        self.manifest["tool_source_sha256"] = "older source fingerprint"
        self.write_manifest()
        self.write_events(("media_stray_applied", "completed_verified"))
        before = self.snapshot()
        with patch.object(media_stray, "_sha256", side_effect=AssertionError("hash forbidden")), \
                patch.object(media_stray, "_snapshot_lists") as snapshot, \
                patch.object(media_stray, "_save") as save:
            with self.assertRaisesRegex(ValueError, "journal_manifest_status_mismatch"):
                media_stray.apply(self.operation_id, True, self.api)
        snapshot.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_move_without_item_checkpoint_requires_reconciliation(self):
        self.source.rename(self.destination)
        result = self.inspect()
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertIn("source_state_mismatch expected=regular observed=missing", result["items"][0]["issues"])

    def test_existing_source_and_destination_require_reconciliation(self):
        self.destination.write_bytes(b"another payload")
        result = self.inspect()
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertIn("destination_state_mismatch expected=missing observed=regular", result["items"][0]["issues"])

    def test_applying_manifest_can_describe_a_checkpointed_move(self):
        self.source.rename(self.destination)
        self.manifest["status"] = "applying"
        self.manifest["items"][0]["action"] = "promoted"
        self.write_manifest()
        self.assertEqual(self.inspect()["status"], "consistent_metadata")

    def test_completed_move_matches_recorded_paths(self):
        self.source.rename(self.destination)
        self.manifest["status"] = "completed_verified"
        self.manifest["items"][0]["action"] = "promoted"
        self.write_manifest()
        self.write_events(("media_stray_applied", "completed_verified"))
        self.assertEqual(self.inspect()["status"], "consistent_metadata")

    def test_completed_manifest_with_planned_move_is_reported(self):
        self.manifest["status"] = "completed_verified"
        self.write_manifest()
        self.assertIn("completed_manifest_has_planned_move", self.inspect()["items"][0]["issues"])

    def test_planned_quarantine_uses_recorded_default_rollback_path(self):
        self.destination.write_bytes(b"canonical fixture")
        item = self.manifest["items"][0]
        item.update(action="quarantine_duplicate", canonical_relative=item.pop("destination_relative"))
        self.write_manifest()
        result = self.inspect()
        self.assertEqual(result["status"], "consistent_metadata")
        self.assertTrue(result["items"][0]["paths"]["destination"]["path"].endswith("rollback/originals/nes/cover-front/Title.png"))

    def test_completed_quarantine_checks_original_and_canonical_presence(self):
        self.destination.write_bytes(b"canonical fixture")
        rollback = self.operation / "rollback/originals/nes/cover-front/Title.png"
        rollback.parent.mkdir(parents=True)
        self.source.rename(rollback)
        item = self.manifest["items"][0]
        item.update(action="quarantined", canonical_relative=item.pop("destination_relative"),
                    rollback_relative=rollback.relative_to(self.root).as_posix())
        self.manifest["status"] = "completed_verified"
        self.write_manifest()
        self.assertEqual(self.inspect()["status"], "consistent_metadata")
        self.destination.unlink()
        self.assertEqual(self.inspect()["status"], "reconciliation_required")

    def test_latest_rollback_event_matches_rolled_back_manifest(self):
        self.manifest["status"] = "rolled_back"
        self.write_manifest()
        self.write_events(("media_stray_applied", "completed_verified"), ("media_stray_rolled_back", "rolled_back"))
        self.assertEqual(self.inspect()["status"], "consistent_metadata")

    def test_leaf_symlink_is_preserved_without_following_its_target(self):
        self.source.unlink()
        self.source.symlink_to("missing-target.png")
        self.manifest["items"][0]["action"] = "preserve_symlink"
        self.write_manifest()
        self.assertEqual(self.inspect()["status"], "consistent_metadata")

    def test_ancestor_symlink_is_reported(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source.parent, target_is_directory=True)
        self.manifest["items"][0]["project_relative"] = "alias/Title.png"
        self.write_manifest()
        self.assertEqual(self.inspect()["items"][0]["paths"]["source"]["kind"], "ancestor_symlink")

    def test_unsafe_manifest_paths_are_reported(self):
        for path in ["../outside.png", "/outside.png", "C:\\outside.png", "C:outside.png", "", "bad\x00name"]:
            with self.subTest(path=path):
                self.manifest["items"][0]["project_relative"] = path
                self.write_manifest()
                self.assertEqual(self.inspect()["items"][0]["paths"]["source"]["kind"], "unsafe_path")

    def test_truncated_and_non_object_journal_records_are_reported(self):
        (self.operation / "events.jsonl").write_text('[]\n{"unfinished":\n{"event": []}\n')
        result = self.inspect()
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(result["summary"]["issue_count"], 3)

    def test_foreign_operation_record_is_reported(self):
        (self.operation / "events.jsonl").write_text('{"operation_id": "different", "event": "media_stray_applied"}\n')
        self.assertTrue(any("foreign_journal_record" in issue for issue in self.inspect()["issues"]))

    def test_unknown_manifest_status_is_reported(self):
        self.manifest["status"] = "unknown"
        self.write_manifest()
        self.assertIn("unrecognized_manifest_status", self.inspect()["issues"])

    def test_parent_operation_ids_are_refused_before_reading(self):
        for operation_id in [".", "..", "../other"]:
            with self.subTest(operation_id=operation_id), self.assertRaisesRegex(ValueError, "ID de operação inválido"):
                media_stray.status(operation_id, self.api)

    def test_symlinked_journal_is_reported(self):
        (self.operation / "events.jsonl").symlink_to("missing-log.jsonl")
        self.assertIn("journal_symlink", self.inspect()["issues"])

    def run_cli(self):
        out = io.StringIO()
        err = io.StringIO()
        with patch.object(romtool, "PROJECT_ROOT", self.root), patch.object(romtool, "OUTPUT_ROOT", self.output), \
                patch.object(romtool, "OPS_ROOT", self.ops), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = romtool.main(["media", "stray", "status", "--operation-id", self.operation_id])
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_cli_exposes_diagnosis_and_exit_3_for_divergence(self):
        self.write_events(("media_stray_applied", "completed_verified"))
        code, result, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertTrue(result["issues"])
        self.assertTrue(result["evidence"]["read_only"])
        self.assertEqual(err, "")

    def test_cli_consistent_metadata_exits_0(self):
        code, result, err = self.run_cli()
        self.assertEqual((code, result["status"], err), (0, "consistent_metadata", ""))


if __name__ == "__main__":
    unittest.main()
