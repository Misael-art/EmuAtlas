"""Policy refresh preserves payloads/index and recovers partial metadata publication."""
import importlib.util
import json
import os
import shutil
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from tools.roms import romtool, runtime_install, steam_centralizer


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
        operations = self.root.parents[1] / "outputs/operations"
        operations.mkdir(parents=True)
        patcher = patch.multiple(romtool, PROJECT_ROOT=self.root.parents[1],
                                 OUTPUT_ROOT=operations.parent, OPS_ROOT=operations)
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

    def rename_fixture(self, *, display_name=False):
        destination = self.shared / "images/controllers/platforms/fixture.png"
        destination.parent.mkdir(parents=True)
        self.asset.rename(destination)
        self.asset = destination
        digest = self.tool.digest_file(self.asset)
        alias = self.root / "collections/Main/medium_artwork/controller/Fixture.png"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(os.path.relpath(self.asset, alias.parent))
        asset_relative = self.asset.relative_to(self.root).as_posix()
        alias_relative = alias.relative_to(self.root).as_posix()
        catalog = {
            "schema_version": 2, "mode": "semantic-library",
            "canonical_assets": [{"path": asset_relative, "sha256": digest,
                                  "size_bytes": self.asset.stat().st_size,
                                  "media_type": "images", "category": "controllers/platforms",
                                  "style": "standard", "display_name": "Fixture", "logical_name": "Fixture",
                                  "hash_bucket": digest[:2]}],
            "main_artwork_aliases": [{"path": alias_relative, "target": asset_relative,
                                      "sha256": digest, "size_bytes": self.asset.stat().st_size,
                                      "category": "controllers/platforms", "style": "standard"}],
        }
        self.catalog.write_text(json.dumps(catalog))
        self.index.write_bytes(self.tool.make_search_index(catalog))
        self.path = self.catalog.with_name("rename-fixture.json")
        self.rename_before = {p: p.read_bytes() for p in (self.catalog, self.index, self.asset)}
        self.alias = alias
        self.alias_literal = os.readlink(alias)
        self.alias_inode = alias.lstat().st_ino
        if display_name:
            manifest = self.tool.build_display_name_fix_plan(
                self.path, [asset_relative + "=Fixture New"], "fixture display correction")
            self.renamed = alias.with_name("Fixture New.png")
        else:
            self.renamed = self.asset.with_name("fixture-after.png")
            manifest = self.tool.build_rename_plan(
                self.path, [asset_relative + "=" + self.renamed.relative_to(self.root).as_posix()],
                "fixture payload rename")
        self.tool.write_manifest(self.path, manifest)
        return manifest

    def assert_rename_restored(self):
        self.assertEqual({p: p.read_bytes() for p in self.rename_before}, self.rename_before)
        self.assertEqual(os.readlink(self.alias), self.alias_literal)
        self.assertEqual(self.alias.lstat().st_ino, self.alias_inode)
        self.assertFalse(os.path.lexists(self.renamed))

    def test_payload_rename_roundtrip_preserves_original_alias_object(self):
        self.rename_fixture()
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")
        self.assertEqual(self.alias.resolve(), self.renamed)
        self.assertEqual(self.tool.rollback_manifest(self.path)["status"], "rolled_back_verified")
        self.assert_rename_restored()

    def test_display_name_roundtrip_preserves_alias_object_and_payload(self):
        self.rename_fixture(display_name=True)
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")
        self.assertEqual(self.renamed.lstat().st_ino, self.alias_inode)
        self.assertEqual(self.tool.rollback_manifest(self.path)["status"], "rolled_back_verified")
        self.assert_rename_restored()

    def test_payload_apply_resumes_after_exclusive_rename_before_checkpoint(self):
        self.rename_fixture()
        original = runtime_install._library_rename_exclusive

        def cut(source, destination):
            original(source, destination)
            raise OSError("cut after rename")

        with patch.object(runtime_install, "_library_rename_exclusive", side_effect=cut):
            with self.assertRaisesRegex(OSError, "cut after rename"):
                self.tool.apply_manifest(self.path)
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")
        self.tool.rollback_manifest(self.path)
        self.assert_rename_restored()

    def test_alias_exchange_apply_and_rollback_resume_after_exchange(self):
        self.rename_fixture()
        original = steam_centralizer._save_links_exchange

        def cut(left, right):
            original(left, right)
            raise OSError("cut after exchange")

        with patch.object(steam_centralizer, "_save_links_exchange", side_effect=cut):
            with self.assertRaisesRegex(OSError, "cut after exchange"):
                self.tool.apply_manifest(self.path)
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")
        with patch.object(steam_centralizer, "_save_links_exchange", side_effect=cut):
            with self.assertRaisesRegex(OSError, "cut after exchange"):
                self.tool.rollback_manifest(self.path)
        self.assertEqual(self.tool.rollback_manifest(self.path)["status"], "rolled_back_verified")
        self.assert_rename_restored()

    def test_display_name_apply_resumes_after_alias_move(self):
        self.rename_fixture(display_name=True)
        original = runtime_install._library_rename_exclusive

        def cut(source, destination):
            original(source, destination)
            raise OSError("cut after alias move")

        with patch.object(runtime_install, "_library_rename_exclusive", side_effect=cut):
            with self.assertRaisesRegex(OSError, "cut after alias move"):
                self.tool.apply_manifest(self.path)
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")
        self.tool.rollback_manifest(self.path)
        self.assert_rename_restored()

    def test_rename_apply_resumes_after_catalog_publication(self):
        self.rename_fixture()
        original = self.tool.atomic_write

        def cut(path, data):
            original(path, data)
            if path == self.catalog:
                raise OSError("cut after catalog")

        with patch.object(self.tool, "atomic_write", side_effect=cut):
            with self.assertRaisesRegex(OSError, "cut after catalog"):
                self.tool.apply_manifest(self.path)
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")
        self.tool.rollback_manifest(self.path)
        self.assert_rename_restored()

    def test_destination_race_preserves_both_files(self):
        self.rename_fixture()
        original = runtime_install._library_rename_exclusive

        def collision(source, destination):
            destination.write_bytes(b"external file")
            return original(source, destination)

        with patch.object(runtime_install, "_library_rename_exclusive", side_effect=collision):
            with self.assertRaises(FileExistsError):
                self.tool.apply_manifest(self.path)
        self.assertEqual(self.renamed.read_bytes(), b"external file")
        self.assertEqual(self.asset.read_bytes(), self.rename_before[self.asset])
        with self.assertRaises(ValueError):
            self.tool.rollback_manifest(self.path)
        self.assertEqual(self.renamed.read_bytes(), b"external file")

    def test_rollback_preserves_equal_file_that_reappeared_at_original_path(self):
        self.rename_fixture()
        self.tool.apply_manifest(self.path)
        self.asset.write_bytes(self.renamed.read_bytes())
        before = self.catalog.read_bytes(), self.index.read_bytes()
        with self.assertRaises(ValueError):
            self.tool.rollback_manifest(self.path)
        self.assertTrue(self.asset.is_file())
        self.assertTrue(self.renamed.is_file())
        self.assertEqual((self.catalog.read_bytes(), self.index.read_bytes()), before)

    def test_display_name_rollback_preserves_regular_file_at_alias_path(self):
        self.rename_fixture(display_name=True)
        self.tool.apply_manifest(self.path)
        self.alias.write_bytes(b"external file")
        before = self.catalog.read_bytes(), self.index.read_bytes()
        with self.assertRaises(ValueError):
            self.tool.rollback_manifest(self.path)
        self.assertEqual(self.alias.read_bytes(), b"external file")
        self.assertTrue(self.renamed.is_symlink())
        self.assertEqual((self.catalog.read_bytes(), self.index.read_bytes()), before)

    def test_alias_staging_cut_recovers_without_replacing_original_object(self):
        self.rename_fixture()
        original = Path.symlink_to

        def cut(path, target, target_is_directory=False):
            original(path, target, target_is_directory=target_is_directory)
            raise OSError("cut after symlink creation")

        with patch.object(Path, "symlink_to", new=cut):
            with self.assertRaisesRegex(OSError, "cut after symlink creation"):
                self.tool.apply_manifest(self.path)
        self.assertEqual(self.alias.lstat().st_ino, self.alias_inode)
        self.assertEqual(self.tool.apply_manifest(self.path)["status"], "completed_verified")
        self.tool.rollback_manifest(self.path)
        self.assert_rename_restored()

    def test_missing_alias_blocks_rollback_before_metadata_or_payload_changes(self):
        self.rename_fixture()
        self.tool.apply_manifest(self.path)
        self.alias.unlink()
        before = self.catalog.read_bytes(), self.index.read_bytes(), self.renamed.read_bytes()
        with self.assertRaises(ValueError):
            self.tool.rollback_manifest(self.path)
        self.assertFalse(self.asset.exists())
        self.assertEqual((self.catalog.read_bytes(), self.index.read_bytes(), self.renamed.read_bytes()), before)

    def test_legacy_completed_rename_can_restore_link_text_and_bytes(self):
        self.rename_fixture()
        manifest = self.tool.apply_manifest(self.path)
        for item in manifest["items"]:
            item.pop("rename_checkpoints", None)
        for alias in manifest["aliases"]:
            alias.pop("link_transition", None)
            alias.pop("object_before", None)
        (self.path.with_suffix("") / "rollback/alias-links/0000.link").unlink()
        self.tool.write_manifest(self.path, manifest)
        self.assertEqual(self.tool.rollback_manifest(self.path)["status"], "rolled_back_verified")
        self.assertEqual({p: p.read_bytes() for p in self.rename_before}, self.rename_before)
        self.assertEqual(os.readlink(self.alias), self.alias_literal)
        self.assertFalse(self.renamed.exists())

    def test_running_frontend_blocks_payload_rename_before_mutation(self):
        self.rename_fixture()
        with patch.object(romtool, "frontend_processes_running", return_value={"retrofe": [{"pid": 1}], "esde": []}):
            with self.assertRaises(RuntimeError):
                self.tool.apply_manifest(self.path)
        self.assert_rename_restored()

    def test_plan_modes_are_exclusive_before_writing_a_manifest(self):
        argv = ["organize-asset-library.py", "plan", "--manifest", str(self.path),
                "--refresh-catalog-policy", "--fix-display-name", "Assets/shared/fixture.png=New", "--reason", "fixture"]
        with patch.object(sys, "argv", argv), patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            self.tool.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.path.exists())

    def test_replaced_alias_object_blocks_before_payload_move(self):
        self.rename_fixture()
        preserved = self.catalog.parent / "external-original.link"
        self.alias.rename(preserved)
        self.alias.symlink_to(self.alias_literal)
        with self.assertRaisesRegex(ValueError, "Objeto do alias mudou"):
            self.tool.apply_manifest(self.path)
        self.assertEqual(self.asset.read_bytes(), self.rename_before[self.asset])
        self.assertFalse(self.renamed.exists())
        self.assertEqual(os.readlink(self.alias), self.alias_literal)
        self.assertEqual(preserved.lstat().st_ino, self.alias_inode)

    def test_display_name_rollback_resumes_after_alias_move(self):
        self.rename_fixture(display_name=True)
        self.tool.apply_manifest(self.path)
        original = runtime_install._library_rename_exclusive

        def cut(source, destination):
            original(source, destination)
            raise OSError("cut after rollback move")

        with patch.object(runtime_install, "_library_rename_exclusive", side_effect=cut):
            with self.assertRaisesRegex(OSError, "cut after rollback move"):
                self.tool.rollback_manifest(self.path)
        self.assertEqual(self.tool.rollback_manifest(self.path)["status"], "rolled_back_verified")
        self.assert_rename_restored()

    def test_payload_rollback_resumes_after_catalog_restoration(self):
        self.rename_fixture()
        self.tool.apply_manifest(self.path)
        original = self.tool.atomic_write

        def cut(path, data):
            original(path, data)
            if path == self.catalog:
                raise OSError("cut after restoring catalog")

        with patch.object(self.tool, "atomic_write", side_effect=cut):
            with self.assertRaisesRegex(OSError, "cut after restoring catalog"):
                self.tool.rollback_manifest(self.path)
        self.assertEqual(self.tool.rollback_manifest(self.path)["status"], "rolled_back_verified")
        self.assert_rename_restored()
