import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.roms import catalog_projection, romtool


class CatalogProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="catalog-project-test-")
        self.root = Path(self.temp.name)
        self.output = self.root / "outputs"
        self.ops = self.output / "operations"
        self.ops.mkdir(parents=True)
        destination = self.root / "roms/nes/Title.nes"
        destination.parent.mkdir(parents=True)
        payload = b"verified-destination"
        destination.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        source = "/tmp/catalog-projection-missing-source/Title.nes"
        source_id = "change-test-source"
        source_dir = self.ops / source_id
        source_dir.mkdir()
        manifest = {
            "operation_id": source_id, "kind": "change", "status": "completed_verified",
            "items": [{
                "item_id": source_id + "-0001", "status": "completed_verified", "system_id": "nes",
                "classification": "game", "source": source, "destination": str(destination),
                "after": {"sha256": digest, "size_bytes": len(payload)},
            }],
        }
        (source_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.source_bytes = (source_dir / "manifest.json").read_bytes()
        self.write_csv(self.output / "catalogo-arquivos.csv", [{
            "absolute_path": source, "current_absolute_path": source, "file_name": "Title.nes",
            "system_id": "nes", "sha256": digest, "size_bytes": str(len(payload)),
        }], ["system_id", "absolute_path", "current_absolute_path", "file_name", "sha256", "size_bytes",
             "current_sha256", "current_size_bytes", "current_operation_id", "current_path_status"])
        self.write_csv(self.output / "roms-pendentes-validacao-exclusao.csv", [{
            "review_status": "PENDENTE", "current_path": source, "original_path": source,
            "review_note": "origem antiga", "destination_or_canonical_peer": "", "operation_id": "",
            "deletion_candidate": "NÃO NESTA ETAPA",
        }], ["review_status", "current_path", "original_path", "review_note", "destination_or_canonical_peer",
             "operation_id", "deletion_candidate"])
        (self.output / "estado-final-verificacao.json").write_text("{}", encoding="utf-8")
        (self.output / "pendencias-integridade-dependencias.csv").write_text("type\n", encoding="utf-8")
        self.destination = destination
        self.digest = digest
        self.source = source
        self.api = {
            "PROJECT_ROOT": self.root, "OUTPUT_ROOT": self.output,
            "OPS_ROOT": self.ops,
            "operation_dir": lambda operation: self.ops / operation,
            "now": lambda: "2026-11-01T12:00:00-03:00",
            "atomic_json": self.atomic_json,
            "append_event": lambda *args, **kwargs: None,
            "rebuild_history_index": lambda: None,
            "write_csv_atomic": self.write_csv,
            "atomic_write_bytes": romtool.atomic_write_bytes,
            "reject_symlink_components": romtool.reject_symlink_components,
        }

    def tearDown(self):
        self.temp.cleanup()

    def atomic_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)

    def write_csv(self, path, rows, fields):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)

    def test_verified_destination_updates_current_path_without_rewriting_journal(self):
        planned = catalog_projection.plan(self.api, "change-test-source")
        applied = catalog_projection.apply(planned["operation_id"], True, self.api)
        self.assertEqual(applied["summary"]["catalog_rows_updated"], 1)
        self.assertEqual(applied["summary"]["pending_rows_marked"], 1)
        with (self.output / "catalogo-arquivos.csv").open(encoding="utf-8-sig") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["absolute_path"], self.source)
        self.assertEqual(row["current_absolute_path"], str(self.destination))
        self.assertEqual(row["current_sha256"], self.digest)
        with (self.output / "roms-pendentes-validacao-exclusao.csv").open(encoding="utf-8-sig") as stream:
            pending = next(csv.DictReader(stream))
        self.assertEqual(pending["review_status"], "DESTINO_VERIFICADO_PRESENTE")
        self.assertEqual(pending["deletion_candidate"], "NÃO NESTA ETAPA")
        self.assertEqual((self.ops / "change-test-source" / "manifest.json").read_bytes(), self.source_bytes)
        self.assertEqual(catalog_projection.verify(planned["operation_id"], self.api)["status"], "verified")
        catalog = self.output / "catalogo-arquivos.csv"
        applied_bytes = catalog.read_bytes()
        catalog.write_bytes(applied_bytes + b"\n")
        with self.assertRaises(ValueError):
            catalog_projection.rollback(planned["operation_id"], True, self.api)
        catalog.write_bytes(applied_bytes)
        catalog_projection.rollback(planned["operation_id"], True, self.api)
        with (self.output / "catalogo-arquivos.csv").open(encoding="utf-8") as stream:
            restored = next(csv.DictReader(stream))
        self.assertEqual(restored["current_absolute_path"], self.source)

    def test_apply_resumes_after_publication_before_checkpoint(self):
        planned = catalog_projection.plan(self.api, 'change-test-source')
        ident = planned['operation_id']
        target = self.output / 'catalogo-arquivos.csv'
        original = self.api['atomic_write_bytes']
        def cut(path, data, **kwargs):
            original(path, data, **kwargs)
            if path == target:
                raise OSError('fixture cut after publication')
        with patch.dict(self.api, atomic_write_bytes=cut):
            with self.assertRaises(OSError):
                catalog_projection.apply(ident, True, self.api)
        self.assertEqual(json.loads((self.ops / ident / 'manifest.json').read_text())['status'], 'applying')
        catalog_projection.apply(ident, True, self.api)
        self.assertEqual(catalog_projection.verify(ident, self.api)['status'], 'verified')
        self.assertEqual((self.ops / 'change-test-source/manifest.json').read_bytes(), self.source_bytes)

    def test_rollback_resumes_after_partial_apply_and_partial_restore(self):
        planned = catalog_projection.plan(self.api, 'change-test-source')
        ident = planned['operation_id']
        targets = list(catalog_projection._lists(self.api).values())
        before = {p: p.read_bytes() for p in targets}
        original = self.api['atomic_write_bytes']
        def cut(path, data, **kwargs):
            original(path, data, **kwargs)
            if path == targets[0]:
                raise OSError('fixture cut')
        with patch.dict(self.api, atomic_write_bytes=cut):
            with self.assertRaises(OSError):
                catalog_projection.apply(ident, True, self.api)
            with self.assertRaises(OSError):
                catalog_projection.rollback(ident, True, self.api)
        catalog_projection.rollback(ident, True, self.api)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual((self.ops / ident / 'rollback' / path.name).read_bytes(), data)

    def test_later_metadata_change_blocks_entire_retry_and_rollback(self):
        planned = catalog_projection.plan(self.api, 'change-test-source')
        ident = planned['operation_id']
        original = self.api['atomic_write_bytes']
        def cut(path, data, **kwargs):
            original(path, data, **kwargs)
            if path == self.output / 'catalogo-arquivos.csv':
                raise OSError('fixture cut')
        with patch.dict(self.api, atomic_write_bytes=cut):
            with self.assertRaises(OSError):
                catalog_projection.apply(ident, True, self.api)
        changed = self.output / 'estado-final-verificacao.json'
        changed.write_bytes(b'{"later_personal_change":true}')
        snapshot = {p: p.read_bytes() for p in catalog_projection._lists(self.api).values()}
        for operation in (catalog_projection.apply, catalog_projection.rollback):
            with self.assertRaises(ValueError):
                operation(ident, True, self.api)
            self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)

    def test_changed_source_journal_blocks_before_publication(self):
        planned = catalog_projection.plan(self.api, 'change-test-source')
        journal = self.ops / 'change-test-source/manifest.json'
        journal.write_bytes(self.source_bytes + b'\n')
        with self.assertRaisesRegex(ValueError, 'Diário'):
            catalog_projection.apply(planned['operation_id'], True, self.api)

    def test_symlink_ancestor_is_refused_even_for_matching_bytes(self):
        planned = catalog_projection.plan(self.api, 'change-test-source')
        directory = self.root / 'roms/nes'
        retained = self.root / 'retained-nes'
        directory.rename(retained)
        directory.symlink_to(retained, target_is_directory=True)
        with self.assertRaises(ValueError):
            catalog_projection.apply(planned['operation_id'], True, self.api)

    def test_legacy_completed_projection_can_restore_without_consuming_preimages(self):
        planned = catalog_projection.plan(self.api, 'change-test-source')
        ident = planned['operation_id']
        catalog_projection.apply(ident, True, self.api)
        path = self.ops / ident / 'manifest.json'
        manifest = json.loads(path.read_text())
        for field in ('publication_prepared', 'publication_intent', 'publication_checkpoints', 'list_modes',
                      'source_manifest_sha256'):
            manifest.pop(field, None)
        path.write_text(json.dumps(manifest))
        catalog_projection.rollback(ident, True, self.api)
        self.assertTrue((self.ops / ident / 'rollback/catalogo-arquivos.csv').is_file())

    def test_tampered_staging_and_later_permissions_block_publication(self):
        planned = catalog_projection.plan(self.api, 'change-test-source')
        ident = planned['operation_id']
        with patch.object(catalog_projection, '_publish', side_effect=OSError('fixture before publication')):
            with self.assertRaises(OSError):
                catalog_projection.apply(ident, True, self.api)
        stage = self.ops / ident / 'proposed/catalogo-arquivos.csv'
        original = stage.read_bytes()
        stage.write_bytes(b'tampered fixture staging')
        with self.assertRaisesRegex(ValueError, 'Snapshot'):
            catalog_projection.apply(ident, True, self.api)
        stage.write_bytes(original)
        catalog = self.output / 'catalogo-arquivos.csv'
        catalog.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'Permissões'):
            catalog_projection.apply(ident, True, self.api)
