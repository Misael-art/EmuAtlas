"""Policy refresh preserves payloads/index and recovers partial metadata publication."""
import importlib.util
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.roms import romtool


SOURCE = Path(__file__).resolve().parents[3] / "frontend/RetroFE/Tools/materializers/organize-asset-library.py"

class AssetCatalogPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "frontend/RetroFE"
        self.shared = self.root / "Assets/shared"
        self.shared.mkdir(parents=True)
        self.asset = self.shared / "fixture.png"
        self.asset.write_bytes(b"metadata-only fixture")
        self.catalog = self.root / "Manifests/asset-catalog.json"
        self.catalog.parent.mkdir()
        (self.root / "layouts").mkdir()
        materializers = self.root / "Tools/materializers"
        materializers.mkdir(parents=True)
        audits = self.root / "Tools/audits"
        audits.mkdir()
        for name in ("organize-asset-library.py", "reorganize-shared-assets.py"):
            shutil.copyfile(SOURCE.parent / name, materializers / name)
        shutil.copyfile(SOURCE.parents[1] / "audits/layout-references.py", audits / "layout-references.py")
        spec = importlib.util.spec_from_file_location("asset_policy_fixture_tool", materializers / SOURCE.name)
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

        catalog = {"canonical_assets": [{"path": "Assets/shared/fixture.png", "sha256": self.tool.digest_file(self.asset),
                                        "size_bytes": self.asset.stat().st_size}],
                   "storage_rules": {"per_game_artwork": "obsolete path"}, "summary": {"canonical_assets": 1}}
        self.catalog.write_text(json.dumps(catalog))
        self.mapping = self.root / "Config/media-map.json"
        self.mapping.parent.mkdir()
        self.mapping.write_text(json.dumps({"runtime_assets": {"count": 99, "last_materialized_manifest": "historic.json"},
                                           "other_personal_field": "preserve"}))
        self.index = self.catalog.with_name("asset-search-index.csv")
        self.index.write_bytes(b"preserve existing index bytes\n")
        self.before = {p: p.read_bytes() for p in (self.catalog, self.mapping, self.index, self.asset)}
        self.path = self.catalog.with_name("policy.json")
        patcher = patch.multiple(self.tool, ROOT=self.root, SHARED=self.shared, CATALOG=self.catalog, SEARCH_INDEX=self.index)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(romtool, "frontend_processes_running", return_value={"retrofe": [], "esde": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self):
        manifest = self.tool.build_catalog_policy_plan(self.path, "align current project policy")
        self.tool.write_manifest(self.path, manifest)
        return manifest

    def test_refresh_verify_and_exact_rollback(self):
        self.plan()
        result = self.tool.apply_manifest(self.path)
        self.assertEqual(result["status"], "completed_verified")
        self.assertEqual(self.tool.verify_catalog_policy(self.path)["status"], "verified")
        mapping = json.loads(self.mapping.read_bytes())
        self.assertEqual(mapping["runtime_assets"]["count"], 1)
        self.assertEqual(mapping["runtime_assets"]["last_materialized_manifest"], "historic.json")
        self.assertEqual(mapping["other_personal_field"], "preserve")
        self.assertEqual(self.index.read_bytes(), self.before[self.index])
        self.assertEqual(self.asset.read_bytes(), self.before[self.asset])
        self.tool.rollback_manifest(self.path)
        self.assertEqual({p: p.read_bytes() for p in self.before}, self.before)

    def test_changed_metadata_and_index_block_before_publication(self):
        for changed in (self.mapping, self.index):
            with self.subTest(changed=changed):
                self.plan()
                changed.write_bytes(changed.read_bytes() + b" ")
                with self.assertRaises(ValueError):
                    self.tool.apply_manifest(self.path)
                self.assertEqual(self.catalog.read_bytes(), self.before[self.catalog])
                changed.write_bytes(self.before[changed])
                self.path.unlink()

    def test_missing_payload_is_reported_before_plan(self):
        self.asset.rename(self.asset.with_suffix(".preserved"))
        with self.assertRaises(ValueError):
            self.plan()
        self.assertFalse(self.path.exists())

    def test_apply_resumes_after_first_metadata_publication(self):
        self.plan()
        original = self.tool.atomic_write
        def cut(path, data):
            original(path, data)
            if path == self.catalog:
                raise OSError("cut after first policy file")
        with patch.object(self.tool, "atomic_write", side_effect=cut):
            with self.assertRaises(OSError):
                self.tool.apply_manifest(self.path)
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")

    def test_later_metadata_blocks_rollback(self):
        self.plan()
        self.tool.apply_manifest(self.path)
        self.mapping.write_bytes(self.mapping.read_bytes() + b" ")
        current = self.mapping.read_bytes()
        with self.assertRaises(ValueError):
            self.tool.rollback_manifest(self.path)
        self.assertEqual(self.mapping.read_bytes(), current)

    def test_unrelated_target_is_rejected(self):
        manifest = self.plan()
        manifest["files"][1]["path"] = "settings.conf"
        self.tool.write_manifest(self.path, manifest)
        with self.assertRaises(ValueError):
            self.tool.apply_manifest(self.path)
        self.assertEqual(self.catalog.read_bytes(), self.before[self.catalog])

    def test_running_frontend_blocks_policy_publication(self):
        self.plan()
        with patch.object(romtool, "frontend_processes_running", return_value={"retrofe": [{"pid": 1}], "esde": []}):
            with self.assertRaises(RuntimeError):
                self.tool.apply_manifest(self.path)
