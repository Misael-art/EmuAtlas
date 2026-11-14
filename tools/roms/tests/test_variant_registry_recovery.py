"""Registry recovery and declared launch relations use private synthetic data."""
from argparse import Namespace
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from tools.roms import romtool, variant_metadata as variants


class Interruption(BaseException):
    pass


class VariantRegistryRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.operations = self.root / 'outputs/operations'
        self.config = self.root / 'frontend/RetroFE/Config'
        self.config.mkdir(parents=True)
        self.roms = self.root / 'roms/nes'
        self.roms.mkdir(parents=True)
        self.source = self.roms / 'Game (Demo).nes'
        self.source.write_bytes(b'NES\x1aown synthetic demo')
        self.catalog('nes', ['nes'])
        self.registry = self.root / 'metadata/rom-variants.jsonl'
        self.api = {
            'PROJECT_ROOT': self.root, 'TOOL_ROOT': Path(variants.__file__).parent,
            'OPS_ROOT': self.operations, 'operation_dir': lambda ident: self.operations / ident,
            'atomic_json': romtool.atomic_json, 'append_event': lambda *args, **kwargs: None,
            'rebuild_history_index': lambda: None, 'now': lambda: '2026-11-14T06:00:00-03:00'}

    def catalog(self, system, extensions):
        (self.config / 'platform-catalog.json').write_text(json.dumps({'schema_version': 1, 'rom_root': '../../roms', 'systems': [
            {'id': system, 'rom_subdir': system, 'kind': 'platform', 'extensions': extensions}]}))

    def plan(self):
        inventory = variants.command_inventory(Namespace(hash=True), self.api)
        return variants.command_plan(Namespace(inventory_operation_id=inventory['operation_id']), self.api)

    def apply(self, plan):
        return variants.command_apply(Namespace(operation_id=plan['operation_id'], apply=True), self.api)

    def rollback(self, plan):
        return variants.command_rollback(Namespace(operation_id=plan['operation_id'], rollback=True), self.api)

    def seed(self):
        self.apply(self.plan())
        before = self.registry.read_bytes()
        (self.roms / 'Second (Demo).nes').write_bytes(b'NES\x1aown second synthetic demo')
        return before, self.plan()

    def test_apply_resumes_after_publication_before_confirmation(self):
        before, plan = self.seed()
        writer = variants._write_bytes_atomic
        def cut(path, data, mode=0o644):
            writer(path, data, mode)
            if path == self.registry:
                raise Interruption()
        with patch.object(variants, '_write_bytes_atomic', side_effect=cut):
            with self.assertRaises(Interruption):
                self.apply(plan)
        self.assertNotEqual(self.registry.read_bytes(), before)
        with self.assertRaises(ValueError):
            variants.command_verify(Namespace(operation_id=plan['operation_id'], rehash=False), self.api)
        self.apply(plan)
        result = variants.command_verify(Namespace(operation_id=plan['operation_id'], rehash=False), self.api)
        self.assertEqual(result['status'], 'verified')
        backup = self.operations / plan['operation_id'] / 'rollback/registry-before.jsonl'
        self.assertEqual(backup.read_bytes(), before)

    def test_apply_resumes_after_failure_before_publication(self):
        before, plan = self.seed()
        with patch.object(variants, '_write_bytes_atomic', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.apply(plan)['status'], 'completed_verified')

    def test_restore_resumes_after_preimage_publication(self):
        before, plan = self.seed()
        self.apply(plan)
        writer = variants._write_bytes_atomic
        def cut(path, data, mode=0o644):
            writer(path, data, mode)
            if path == self.registry:
                raise Interruption()
        with patch.object(variants, '_write_bytes_atomic', side_effect=cut):
            with self.assertRaises(Interruption):
                self.rollback(plan)
        self.assertEqual(self.registry.read_bytes(), before)
        self.rollback(plan)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_created_registry_preservation_resumes_before_removing_its_active_path(self):
        plan = self.plan()
        self.apply(plan)
        original = self.registry.read_bytes()
        retained = self.operations / plan['operation_id'] / 'rollback/registry-created-by-operation.jsonl'
        writer = variants._write_bytes_atomic
        def cut(path, data, mode=0o644):
            writer(path, data, mode)
            if path == retained:
                raise Interruption()
        with patch.object(variants, '_write_bytes_atomic', side_effect=cut):
            with self.assertRaises(Interruption):
                self.rollback(plan)
        self.assertTrue(self.registry.exists())
        self.rollback(plan)
        self.assertFalse(self.registry.exists())
        self.assertEqual(retained.read_bytes(), original)

    def test_created_registry_restore_resumes_after_active_path_removal(self):
        plan = self.plan()
        self.apply(plan)
        original = self.registry.read_bytes()
        unlink = Path.unlink
        def cut(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path == self.registry:
                raise Interruption()
        with patch.object(Path, 'unlink', cut):
            with self.assertRaises(Interruption):
                self.rollback(plan)
        self.assertFalse(self.registry.exists())
        self.rollback(plan)
        retained = self.operations / plan['operation_id'] / 'rollback/registry-created-by-operation.jsonl'
        self.assertEqual(retained.read_bytes(), original)

    def test_later_content_and_unowned_preservation_block_restore(self):
        plan = self.plan()
        self.apply(plan)
        original = self.registry.read_bytes()
        self.registry.write_bytes(original + b'\n')
        with self.assertRaises(ValueError):
            self.rollback(plan)
        self.assertEqual(self.registry.read_bytes(), original + b'\n')
        self.registry.write_bytes(original)
        retained = self.operations / plan['operation_id'] / 'rollback/registry-created-by-operation.jsonl'
        retained.write_bytes(b'unrelated own fixture evidence')
        with self.assertRaises(ValueError):
            self.rollback(plan)
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertEqual(retained.read_bytes(), b'unrelated own fixture evidence')

    def test_changed_candidate_permissions_block_before_registry_write(self):
        plan = self.plan()
        self.source.chmod(0o444)
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertFalse(self.registry.exists())

    def alias_fixture(self):
        self.catalog('switch2', ['exefs'])
        platform = self.root / 'roms/switch2'
        exefs = platform / 'Game/base/exefs'
        exefs.mkdir(parents=True)
        (exefs / 'main').write_bytes(b'NSO0' + b'\0' * 60)
        npdm = bytearray(0xa0)
        npdm[:4] = b'META'
        struct.pack_into('<I', npdm, 0x70, 0x80)
        npdm[0x80:0x84] = b'ACI0'
        (exefs / 'main.npdm').write_bytes(npdm)
        alias = platform / 'Game (Demo).exefs'
        alias.symlink_to('Game/base/exefs/main')
        profile = {'collections': {'Switch 2': {'rom_subdir': 'switch2', 'runtime_id': 'eden-nx2',
                                              'launch_inputs': ['exefs', 'game_directory']}}}
        (self.config / 'profiles').mkdir()
        (self.config / 'profiles/host-linux.json').write_text(json.dumps(profile))
        return alias

    def test_declared_native_alias_is_reported_as_a_launch_relation(self):
        alias = self.alias_fixture()
        result = variants.command_inventory(Namespace(hash=True), self.api)
        self.assertEqual(result['summary']['managed_launch_alias_count'], 1)
        self.assertEqual(result['summary']['hash_failures'], 0)
        self.assertEqual(result['summary']['candidate_count'], 0)
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertTrue(manifest['hash_requested'])
        self.assertTrue(alias.is_symlink())
        self.assertIn('Game/base/exefs/main', Path(result['filename_safety']).read_text())

    def test_absolute_or_undeclared_alias_remains_a_blocked_candidate(self):
        alias = self.alias_fixture()
        alias.unlink()
        alias.symlink_to(alias.parent / 'Game/base/exefs/main')
        result = variants.command_inventory(Namespace(hash=True), self.api)
        self.assertEqual(result['summary']['managed_launch_alias_count'], 0)
        self.assertEqual(result['summary']['hash_failures'], 1)
        self.assertFalse(json.loads(Path(result['manifest']).read_text())['hash_requested'])
        alias.unlink()
        alias.symlink_to('Game/base/exefs/main')
        profile = self.config / 'profiles/host-linux.json'
        data = json.loads(profile.read_text())
        data['collections']['Switch 2']['runtime_id'] = 'undeclared-runtime'
        profile.write_text(json.dumps(data))
        result = variants.command_inventory(Namespace(hash=True), self.api)
        self.assertEqual(result['summary']['managed_launch_alias_count'], 0)
        self.assertEqual(result['summary']['hash_failures'], 1)


if __name__ == '__main__':
    unittest.main()
