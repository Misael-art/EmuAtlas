"""Native packaging and shared-data migration safety, using isolated workspaces."""
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import zipfile

from tools.roms import romtool, runtime_install as runtime
from tools.roms import portable_emulator, native_bindings
from tools.roms.frontend_projection import esde_projection


class Interruption(BaseException):
    pass


def pe64():
    data = bytearray(256)
    data[:2] = b'MZ'
    data[60:64] = (128).to_bytes(4, 'little')
    data[128:134] = b'PE\0\0\x64\x86'
    return bytes(data)


class NativeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Library with spaces'
        self.root.mkdir()
        for name, value in [('PROJECT_ROOT', self.root), ('OUTPUT_ROOT', self.root / 'outputs'),
                            ('OPS_ROOT', self.root / 'outputs/operations')]:
            context = patch.object(romtool, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(romtool, 'frontend_processes_running', return_value={})
        context.start()
        self.addCleanup(context.stop)

    def config(self, value):
        path = self.root / 'tools/roms/config/portable-runtimes.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def test_host_materializer_import_preserves_configuration(self):
        import importlib.util
        retrofe = self.root / 'frontend/RetroFE'
        (retrofe / 'collections').mkdir(parents=True)
        (retrofe / 'Config/profiles').mkdir(parents=True)
        (retrofe / 'settings.conf').write_bytes(b'personal frontend settings\n')
        (retrofe / 'Config/profiles/host-linux.json').write_text('{"collections": {}}')
        source = Path(__file__).resolve().parents[3] / 'frontend/RetroFE/Tools/materializers/materialize-host-profile.py'
        target = retrofe / 'Tools/materializers/materialize-host-profile.py'
        target.parent.mkdir(parents=True)
        shutil.copy2(source, target)
        before = runtime._library_tree(self.root)
        spec = importlib.util.spec_from_file_location('host_materializer_import_fixture', target)
        module = importlib.util.module_from_spec(spec)
        with patch('sys.argv', ['fixture-import', '--unknown-import-argument']):
            spec.loader.exec_module(module)
        self.assertEqual(runtime._library_tree(self.root), before)
        self.assertEqual(module.set_conf_keys('launcher = Old\n', {'launcher': 'New'}),
                         'launcher = New\n')

    def cbios_fixture(self):
        names = ['config.ini', 'cbios_main_msx2.rom', 'cbios_logo_msx2.rom',
                 'cbios_sub.rom', 'cbios.txt', 'blueMSX-license.txt']
        source = self.root / 'work/own-machine-fixture'
        source.mkdir(parents=True)
        files = []
        for name in names:
            data = ('own synthetic machine data ' + name).encode()
            (source / name).write_bytes(data)
            files.append({'path': name, 'source_url': 'https://example.invalid/' + name,
                          'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        recipe = {'destination': 'bios/Machines/MSX2 - C-BIOS', 'files': files}
        database = b'<softwaredb><software><title>own fixture</title></software></softwaredb>'
        (source / 'msxromdb.xml').write_bytes(database)
        target = self.root / 'bios/Databases/msxromdb.xml'
        target.parent.mkdir(parents=True)
        target.write_bytes(database)
        recipe['database'] = {'path': 'msxromdb.xml', 'destination': 'bios/Databases/msxromdb.xml',
                              'source_url': 'https://example.invalid/msxromdb.xml',
                              'size_bytes': len(database), 'sha256': hashlib.sha256(database).hexdigest()}
        self.config({'bluemsx_cbios': recipe, 'allowed_download_hosts': ['example.invalid']})
        return source, recipe

    def vita_binding_fixture(self):
        operation_id = 'vita3k-binding-fixture'
        operation = romtool.operation_dir(operation_id)
        data = operation / 'staging/Vita3K'
        (data / 'fs').mkdir(parents=True)
        (data / 'config.yml').write_bytes(b'pref-path: fs\n')
        link = self.root / 'emulator/linux/vita3k/portable'
        link.parent.mkdir(parents=True)
        staged = operation / 'bindings/0'
        staged.parent.mkdir()
        literal = os.path.relpath(self.root / 'emulator/data/Vita3K', link.parent)
        staged.symlink_to(literal, target_is_directory=True)
        manifest = {'schema_version': 1, 'operation_id': operation_id, 'kind': 'emulator_library_migration',
                    'status': 'prepared', 'scope': 'vita3k_portable_userdata', 'edits': [],
                    'moves': [{'source': data.relative_to(self.root).as_posix(),
                               'destination': 'emulator/data/Vita3K', 'files': runtime._library_tree(data)}],
                    'vita3k_bindings': [{'source': link.relative_to(self.root).as_posix(),
                                        'destination': 'emulator/data/Vita3K', 'literal': literal,
                                        'snapshot': runtime._vita3k_binding_snapshot(staged)}]}
        romtool.atomic_json(operation / 'manifest.json', manifest)
        return operation_id, link, staged

    def vita_runtime_fixture(self):
        payload = b'\x7fELF\0\0\0\0AI\x02own synthetic appimage'
        program = self.root / 'emulator/linux/vita3k/Vita3K-x86_64.AppImage'
        program.parent.mkdir(parents=True)
        program.write_bytes(payload)
        program.chmod(0o755)
        recipe = {'display_name': 'Vita3K fixture', 'version': 'own-fixture', 'kind': 'appimage',
                  'source_url': 'https://example.invalid/Vita3K-x86_64.AppImage',
                  'source_size_bytes': len(payload), 'source_sha256': hashlib.sha256(payload).hexdigest(),
                  'format': 'appimage', 'entry': program.name, 'install_dir': 'vita3k'}
        self.config({'runtimes': {'vita3k': recipe}})
        return program

    def vita_app_fixture(self, title_id='PCSA00017', category='gd'):
        directory = self.root / 'roms/psvita' / category / title_id
        metadata = directory / 'sce_sys/param.sfo'
        metadata.parent.mkdir(parents=True)
        fields = [('TITLE_ID', title_id), ('CATEGORY', category)]
        keys = b''
        values = b''
        entries = b''
        for key, value in fields:
            payload = value.encode() + b'\0'
            entries += struct.pack('<HHIII', len(keys), 0x0204, len(payload), len(payload), len(values))
            keys += key.encode() + b'\0'
            values += payload
        key_start = 20 + len(entries)
        metadata.write_bytes(struct.pack('<IIIII', 0x46535000, 0x101, key_start,
                                         key_start + len(keys), len(fields)) + entries + keys + values)
        (directory / 'eboot.bin').write_bytes(b'own synthetic executable fixture')
        return directory

    def vita_game_fixture(self):
        self.vita_runtime_fixture()
        data = runtime.vita3k_data_plan(self.root)
        runtime.library_execute(self.root, data['operation_id'], 'apply', True)
        runtime.library_execute(self.root, data['operation_id'], 'verify')
        return self.vita_app_fixture(), self.root / 'roms/psvita/Own game.psvita'

    def test_vita_game_binds_app_patch_and_descriptor_without_copying_payload(self):
        app, descriptor = self.vita_game_fixture()
        patch_dir = self.vita_app_fixture(category='gp')
        plan = runtime.vita3k_game_plan(self.root, app, descriptor, patch_dir)
        self.assertEqual(runtime.vita3k_game_plan(self.root, app, descriptor, patch_dir)['operation_id'],
                         plan['operation_id'])
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        runtime.library_execute(self.root, plan['operation_id'], 'verify')
        identity = portable_emulator.vita3k_launch_identity(self.root, descriptor)
        self.assertEqual(identity['directory'], app)
        self.assertEqual(identity['title_id'], 'PCSA00017')
        link = self.root / 'emulator/data/Vita3K/fs/ux0/patch/PCSA00017'
        self.assertEqual(link.resolve(), patch_dir)
        self.assertFalse(os.path.isabs(os.readlink(link)))
        save = self.root / 'emulator/data/Vita3K/fs/personal-save'
        save.write_bytes(b'later gameplay')
        runtime.library_execute(self.root, plan['operation_id'], 'rollback', True)
        self.assertFalse(os.path.lexists(link))
        self.assertFalse(descriptor.exists())
        self.assertEqual(save.read_bytes(), b'later gameplay')
        self.assertTrue((app / 'eboot.bin').is_file())

    def test_vita_game_preexisting_alias_survives_descriptor_rollback(self):
        app, descriptor = self.vita_game_fixture()
        link = self.root / 'emulator/data/Vita3K/fs/ux0/app/PCSA00017'
        link.parent.mkdir(parents=True)
        link.symlink_to(os.path.relpath(app, link.parent))
        before = runtime._vita3k_binding_snapshot(link)
        plan = runtime.vita3k_game_plan(self.root, app, descriptor)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        runtime.library_execute(self.root, plan['operation_id'], 'verify')
        runtime.library_execute(self.root, plan['operation_id'], 'rollback', True)
        self.assertEqual(runtime._vita3k_binding_snapshot(link), before)

    def test_vita_game_rejects_mismatched_patch_and_changed_identity(self):
        app, descriptor = self.vita_game_fixture()
        other = self.vita_app_fixture('PCSA00018', 'gp')
        with self.assertRaisesRegex(ValueError, 'outro TITLE_ID'):
            runtime.vita3k_game_plan(self.root, app, descriptor, other)
        plan = runtime.vita3k_game_plan(self.root, app, descriptor)
        metadata = app / 'sce_sys/param.sfo'
        metadata.write_bytes(metadata.read_bytes().replace(b'PCSA00017', b'PCSA00019'))
        with self.assertRaisesRegex(ValueError, 'Identidade'):
            runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        self.assertFalse(descriptor.exists())

    def test_vita_descriptor_rejects_outside_target_and_wrong_title_id(self):
        app, descriptor = self.vita_game_fixture()
        plan = runtime.vita3k_game_plan(self.root, app, descriptor)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        link = self.root / 'emulator/data/Vita3K/fs/ux0/app/PCSA00017'
        link.unlink()
        (self.root / 'work').mkdir()
        link.symlink_to(os.path.relpath(self.root / 'work', link.parent))
        with self.assertRaises(ValueError):
            portable_emulator.vita3k_launch_identity(self.root, descriptor)
        link.unlink()
        other = self.vita_app_fixture('PCSA00018')
        link.symlink_to(os.path.relpath(other, link.parent))
        with self.assertRaisesRegex(ValueError, 'divergiu'):
            portable_emulator.vita3k_launch_identity(self.root, descriptor)

    def test_vita_game_bindings_and_descriptor_survive_library_relocation(self):
        app, descriptor = self.vita_game_fixture()
        plan = runtime.vita3k_game_plan(self.root, app, descriptor)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        moved = self.root.with_name('Moved library')
        self.root.rename(moved)
        identity = portable_emulator.vita3k_launch_identity(moved, moved / descriptor.relative_to(self.root))
        self.assertEqual(identity['directory'], moved / app.relative_to(self.root))

    def test_vita_game_resumes_cut_after_link_publication(self):
        app, descriptor = self.vita_game_fixture()
        plan = runtime.vita3k_game_plan(self.root, app, descriptor)
        original = runtime._library_rename_exclusive

        def cut(source, destination):
            original(source, destination)
            raise Interruption()

        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        resumed = runtime.vita3k_game_plan(self.root, app, descriptor)
        self.assertEqual(resumed['status'], 'application_resume_required')
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        runtime.library_execute(self.root, plan['operation_id'], 'verify')
        self.assertEqual(portable_emulator.vita3k_launch_identity(self.root, descriptor)['directory'], app)

    def test_vita_launcher_forwards_validated_title_id_and_shared_working_directory(self):
        from tools.roms.tests.launcher_fixture import load_launcher
        launcher = load_launcher('vita_command_fixture')
        app, descriptor = self.vita_game_fixture()
        plan = runtime.vita3k_game_plan(self.root, app, descriptor)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        program = self.root / 'emulator/linux/vita3k/Vita3K-x86_64.AppImage'
        system = {'rom_subdir': 'psvita', 'runtime_id': 'vita3k', 'command': str(program),
                  'args': ['--frontend', 'sdl', '--keep-config', '-r', '{title_id}'],
                  'userdata_subdir': 'Vita3K', 'working_directory': '{userdata}'}
        profile = {'rom_root': str(self.root / 'roms'), 'bios_root': str(self.root / 'bios')}
        with patch.object(launcher, 'ROOT', self.root / 'frontend/RetroFE'), \
                patch.object(launcher, 'run_with_music_paused', return_value=0) as run:
            self.assertEqual(launcher.launch_command(profile, system, descriptor), 0)
            self.assertEqual(run.call_args.args[0], [str(program), '--frontend', 'sdl',
                                                    '--keep-config', '-r', 'PCSA00017'])
            self.assertEqual(run.call_args.kwargs['cwd'], self.root / 'emulator/data/Vita3K')
            descriptor.write_text('PCSA00018\n')
            with patch.object(launcher, 'notify'):
                self.assertEqual(launcher.launch_command(profile, system, descriptor), 2)
            self.assertEqual(run.call_count, 1)

    def test_vita_data_plan_and_warm_repeat_preserve_current_saves(self):
        program = self.vita_runtime_fixture()
        plan = runtime.vita3k_data_plan(self.root)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        runtime.library_execute(self.root, plan['operation_id'], 'verify')
        data = self.root / 'emulator/data/Vita3K'
        self.assertEqual((program.parent / 'portable').resolve(), data)
        self.assertIn(b'pref-path: fs', (data / 'config.yml').read_bytes())
        save = data / 'fs/personal-save'
        save.write_bytes(b'new gameplay save')
        self.assertEqual(runtime.vita3k_data_plan(self.root)['status'], 'already_configured_verified')
        self.assertEqual(save.read_bytes(), b'new gameplay save')

    def test_vita_data_preparation_resumes_same_staged_link(self):
        self.vita_runtime_fixture()
        plan = runtime.vita3k_data_plan(self.root)
        staged = romtool.operation_dir(plan['operation_id']) / 'bindings/0'
        before = runtime._vita3k_binding_snapshot(staged)
        repeated = runtime.vita3k_data_plan(self.root, plan['operation_id'])
        self.assertEqual(repeated['operation_id'], plan['operation_id'])
        self.assertEqual(runtime._vita3k_binding_snapshot(staged), before)

    def test_vita_data_requires_pinned_program(self):
        program = self.vita_runtime_fixture()
        program.write_bytes(b'changed executable')
        with self.assertRaisesRegex(ValueError, 'AppImage'):
            runtime.vita3k_data_plan(self.root)
        self.assertFalse((self.root / 'emulator/data/Vita3K').exists())

    def test_library_explicit_index_deferral_keeps_current_manifest_and_journal(self):
        ident, link, staged = self.vita_binding_fixture()
        path = romtool.operation_dir(ident) / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['history_index_deferred'] = True
        romtool.atomic_json(path, manifest)
        with patch.object(romtool, 'rebuild_history_index') as rebuild:
            runtime.library_execute(self.root, ident, 'apply', True)
            runtime.library_execute(self.root, ident, 'verify')
        rebuild.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['status'], 'completed_verified')
        events = [json.loads(line) for line in (path.parent / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'library_verify')

    def test_vita_data_preserves_existing_portable_folder(self):
        program = self.vita_runtime_fixture()
        portable = program.parent / 'portable'
        portable.mkdir()
        personal = portable / 'personal-save'
        personal.write_bytes(b'original user file')
        with self.assertRaisesRegex(ValueError, 'preservado'):
            runtime.vita3k_data_plan(self.root)
        self.assertEqual(personal.read_bytes(), b'original user file')
        self.assertFalse((self.root / 'emulator/data/Vita3K').exists())

    def test_vita_data_plan_recovers_its_inflight_publication(self):
        program = self.vita_runtime_fixture()
        plan = runtime.vita3k_data_plan(self.root)
        link = program.parent / 'portable'
        rename = runtime._library_rename_exclusive
        def cut(source, destination):
            rename(source, destination)
            if destination == link:
                raise Interruption()
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        recovered = runtime.vita3k_data_plan(self.root)
        self.assertEqual(recovered['operation_id'], plan['operation_id'])
        self.assertEqual(recovered['status'], 'application_resume_required')
        runtime.library_execute(self.root, recovered['operation_id'], 'apply', True)
        self.assertEqual(runtime.vita3k_data_plan(self.root)['status'], 'verification_resume_required')
        runtime.library_execute(self.root, plan['operation_id'], 'verify')
        self.assertEqual(runtime.vita3k_data_plan(self.root)['status'], 'already_configured_verified')

    def test_vita_preparation_detects_userdata_appearing_after_plan(self):
        self.vita_runtime_fixture()
        plan = runtime.vita3k_data_plan(self.root)
        data = self.root / 'emulator/data/Vita3K'
        (data / 'fs').mkdir(parents=True)
        (data / 'config.yml').write_bytes(b'pref-path: fs\n')
        with self.assertRaisesRegex(ValueError, 'apareceram'):
            runtime.vita3k_data_plan(self.root, plan['operation_id'])
        self.assertEqual((data / 'config.yml').read_bytes(), b'pref-path: fs\n')

    def test_vita_portable_binding_apply_verify_and_rollback_preserve_link_object(self):
        ident, link, staged = self.vita_binding_fixture()
        snapshot = runtime._vita3k_binding_snapshot(staged)
        runtime.library_execute(self.root, ident, 'apply', True)
        runtime.library_execute(self.root, ident, 'verify')
        self.assertEqual(link.resolve(), self.root / 'emulator/data/Vita3K')
        self.assertFalse(os.path.isabs(os.readlink(link)))
        runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(runtime._vita3k_binding_snapshot(staged), snapshot)
        self.assertFalse((self.root / 'emulator/data/Vita3K').exists())

    def test_vita_binding_resumes_after_publication_and_rollback_cuts(self):
        ident, link, staged = self.vita_binding_fixture()
        rename = runtime._library_rename_exclusive
        def cut_binding(source, target):
            rename(source, target)
            if target in (link, staged):
                raise Interruption()
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut_binding):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'apply', True)
        runtime.library_execute(self.root, ident, 'apply', True)
        runtime.library_execute(self.root, ident, 'verify')
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut_binding):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'rollback', True)
        runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(staged.is_symlink())

    def test_vita_binding_preflight_protects_existing_path_before_userdata_move(self):
        ident, link, staged = self.vita_binding_fixture()
        link.mkdir()
        save = link / 'personal-save'
        save.write_bytes(b'personal')
        with self.assertRaises(ValueError):
            runtime.library_execute(self.root, ident, 'apply', True)
        self.assertEqual(save.read_bytes(), b'personal')
        self.assertFalse((self.root / 'emulator/data/Vita3K').exists())
        self.assertTrue(staged.is_symlink())

    def test_vita_binding_noreplace_preserves_racing_file(self):
        ident, link, staged = self.vita_binding_fixture()
        rename = runtime._library_rename_exclusive
        def collision(source, target):
            if target == link:
                link.write_bytes(b'foreign file')
            rename(source, target)
        with patch.object(runtime, '_library_rename_exclusive', side_effect=collision):
            with self.assertRaises(FileExistsError):
                runtime.library_execute(self.root, ident, 'apply', True)
        self.assertEqual(link.read_bytes(), b'foreign file')
        self.assertTrue(staged.is_symlink())

    def test_vita_userdata_changes_block_rollback_before_removing_binding(self):
        ident, link, staged = self.vita_binding_fixture()
        runtime.library_execute(self.root, ident, 'apply', True)
        save = self.root / 'emulator/data/Vita3K/fs/own-save'
        save.write_bytes(b'later save')
        with self.assertRaisesRegex(ValueError, 'Inventário/SHA'):
            runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertEqual(save.read_bytes(), b'later save')
        self.assertTrue(link.is_symlink())

    def test_vita_binding_replacement_with_same_literal_is_protected(self):
        ident, link, staged = self.vita_binding_fixture()
        runtime.library_execute(self.root, ident, 'apply', True)
        literal = os.readlink(link)
        link.rename(link.with_name('original-binding-preserved'))
        link.symlink_to(literal, target_is_directory=True)
        with self.assertRaises(ValueError):
            runtime.library_execute(self.root, ident, 'verify')
        with self.assertRaises(ValueError):
            runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertEqual(os.readlink(link), literal)

    def test_vita_binding_scope_and_duplicates_are_rejected(self):
        ident, link, staged = self.vita_binding_fixture()
        path = romtool.operation_dir(ident) / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['vita3k_bindings'] *= 2
        romtool.atomic_json(path, manifest)
        with self.assertRaisesRegex(ValueError, 'repetidos'):
            runtime.library_execute(self.root, ident, 'apply', True)
        manifest['vita3k_bindings'] = manifest['vita3k_bindings'][:1]
        manifest['vita3k_bindings'][0]['destination'] = 'roms/psvita'
        romtool.atomic_json(path, manifest)
        with self.assertRaisesRegex(ValueError, 'escopo'):
            runtime.library_execute(self.root, ident, 'apply', True)
        self.assertFalse(os.path.lexists(link))

    def test_vita_portable_binding_verifies_after_root_relocation(self):
        ident, link, staged = self.vita_binding_fixture()
        runtime.library_execute(self.root, ident, 'apply', True)
        original = self.root
        relocated = original.with_name('Relocated library')
        original.rename(relocated)
        self.root = relocated
        with patch.multiple(romtool, PROJECT_ROOT=relocated, OUTPUT_ROOT=relocated / 'outputs',
                            OPS_ROOT=relocated / 'outputs/operations'):
            runtime.library_execute(relocated, ident, 'verify')
            current = relocated / link.relative_to(original)
            self.assertEqual(current.resolve(), relocated / 'emulator/data/Vita3K')
            runtime.library_execute(relocated, ident, 'rollback', True)
            self.assertFalse(os.path.lexists(current))

    def test_cbios_plan_apply_verify_rollback_and_existing_machine_preservation(self):
        source, recipe = self.cbios_fixture()
        plan = runtime.bluemsx_cbios_plan(self.root, from_directory=source)
        target = self.root / recipe['destination']
        self.assertFalse(target.exists())
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        runtime.library_execute(self.root, plan['operation_id'], 'verify')
        for row in recipe['files']:
            self.assertEqual((target / row['path']).read_bytes(), (source / row['path']).read_bytes())
        self.assertEqual(runtime.bluemsx_cbios_plan(self.root)['status'], 'already_configured_verified')
        runtime.library_execute(self.root, plan['operation_id'], 'rollback', True)
        self.assertFalse(target.exists())
        self.assertTrue(source.is_dir())

    def test_cbios_changed_machine_blocks_replacement_and_rollback(self):
        source, recipe = self.cbios_fixture()
        plan = runtime.bluemsx_cbios_plan(self.root, from_directory=source)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        target = self.root / recipe['destination'] / 'config.ini'
        target.write_bytes(b'personal machine choice')
        with self.assertRaisesRegex(ValueError, 'reconcilie'):
            runtime.bluemsx_cbios_plan(self.root)
        with self.assertRaisesRegex(ValueError, 'Inventário/SHA'):
            runtime.library_execute(self.root, plan['operation_id'], 'rollback', True)
        self.assertEqual(target.read_bytes(), b'personal machine choice')

    def test_cbios_acquisition_resumes_after_download_interruption(self):
        source, recipe = self.cbios_fixture()
        count = 0
        def download(url, destination, size, hosts):
            nonlocal count
            count += 1
            if count == 2:
                destination.write_bytes(b'incomplete download')
                raise Interruption()
            shutil.copyfile(source / url.rsplit('/', 1)[-1], destination)
        with patch.object(runtime, '_download', side_effect=download):
            with self.assertRaises(Interruption):
                runtime.bluemsx_cbios_plan(self.root)
        operations = list(romtool.OPS_ROOT.glob('emulator-library-bluemsx-cbios-*'))
        self.assertEqual(len(operations), 1)
        operation = operations[0]
        first = operation / 'staging/MSX2 - C-BIOS/config.ini'
        snapshot = first.stat()
        resumed = runtime.bluemsx_cbios_plan(self.root, operation.name, from_directory=source)
        self.assertEqual(first.stat(), snapshot)
        self.assertEqual(resumed['operation_id'], operation.name)
        runtime.library_execute(self.root, operation.name, 'apply', True)
        runtime.library_execute(self.root, operation.name, 'verify')
        self.assertTrue(any(p.read_bytes() == b'incomplete download' for p in (operation / 'downloads').iterdir()))

    def test_cbios_bad_hash_never_publishes_machine(self):
        source, recipe = self.cbios_fixture()
        changed = source / 'cbios_sub.rom'
        changed.write_bytes(b'X' * changed.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            runtime.bluemsx_cbios_plan(self.root, from_directory=source)
        self.assertFalse((self.root / recipe['destination']).exists())
        operation = next(romtool.OPS_ROOT.glob('emulator-library-bluemsx-cbios-*'))
        self.assertEqual(json.loads((operation / 'manifest.json').read_text())['status'], 'preparation_failed')

    def test_cbios_pin_change_and_unexpected_staging_block_resume(self):
        source, recipe = self.cbios_fixture()
        plan = runtime.bluemsx_cbios_plan(self.root, from_directory=source)
        staging = romtool.OPS_ROOT / plan['operation_id'] / 'staging/MSX2 - C-BIOS'
        (staging / 'personal-note').write_bytes(b'preserve me')
        with self.assertRaisesRegex(ValueError, 'Staging'):
            runtime.bluemsx_cbios_plan(self.root, plan['operation_id'], from_directory=source)
        recipe['version'] = 'another pin'
        self.config({'bluemsx_cbios': recipe, 'allowed_download_hosts': ['example.invalid']})
        with self.assertRaisesRegex(ValueError, 'retomada'):
            runtime.bluemsx_cbios_plan(self.root, plan['operation_id'], from_directory=source)
        self.assertEqual((staging / 'personal-note').read_bytes(), b'preserve me')

    def test_cbios_destination_ancestor_symlink_is_refused(self):
        source, recipe = self.cbios_fixture()
        external = self.root / 'work/external-bios'
        external.mkdir()
        (self.root / 'bios/Machines').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            runtime.bluemsx_cbios_plan(self.root, from_directory=source)
        self.assertEqual(list(external.iterdir()), [])

    def test_cbios_empty_host_gets_database_and_rolls_back_both_roots(self):
        source, recipe = self.cbios_fixture()
        database = self.root / recipe['database']['destination']
        database.unlink()
        plan = runtime.bluemsx_cbios_plan(self.root, from_directory=source)
        self.assertEqual(plan['moves'], 2)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        runtime.library_execute(self.root, plan['operation_id'], 'verify')
        self.assertEqual(database.read_bytes(), (source / 'msxromdb.xml').read_bytes())
        runtime.library_execute(self.root, plan['operation_id'], 'rollback', True)
        self.assertFalse(database.exists())
        self.assertFalse((self.root / recipe['destination']).exists())

    def test_cbios_preserves_valid_custom_database(self):
        source, recipe = self.cbios_fixture()
        database = self.root / recipe['database']['destination']
        custom = b'<softwaredb><software><title>personal update</title></software></softwaredb>'
        database.write_bytes(custom)
        plan = runtime.bluemsx_cbios_plan(self.root, from_directory=source)
        self.assertEqual(plan['moves'], 1)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        result = runtime.bluemsx_cbios_plan(self.root)
        self.assertEqual(result['database']['sha256'], hashlib.sha256(custom).hexdigest())
        self.assertEqual(database.read_bytes(), custom)

    def migration_fixture(self, names=('example',), regular_file=False, new_reference=False):
        operation_id = 'migration-fixture-' + '-'.join(names)
        operation = romtool.operation_dir(operation_id)
        moves = []
        for name in names:
            source = self.root / 'tools/roms/runtimes' / name
            source.parent.mkdir(parents=True, exist_ok=True)
            if regular_file:
                source.write_bytes(b'own payload ' + name.encode())
                source.chmod(0o444)
            else:
                source.mkdir()
                (source / 'runtime.bin').write_bytes(b'own payload ' + name.encode())
                source.chmod(0o555)
            moves.append({'source': source.relative_to(self.root).as_posix(),
                          'destination': 'emulator/linux/' + name, 'files': runtime._library_tree(source)})
        edits = []
        if new_reference:
            relative = 'frontend/RetroFE/Config/new.json'
            proposal = operation / 'proposed' / relative
            proposal.parent.mkdir(parents=True)
            proposal.write_bytes(b'new fixture reference')
            edits.append({'path': relative, 'sha256_before': None,
                          'sha256_after': runtime._sha256(proposal), 'mode': 0o640})
        romtool.atomic_json(operation / 'manifest.json', {
            'operation_id': operation_id, 'kind': 'emulator_library_migration',
            'status': 'prepared', 'moves': moves, 'edits': edits})
        return operation_id, operation, moves

    def copy_fixture(self):
        home = self.root.parent / 'old host'
        source = home / '.var/app/org.flycast.Flycast/data/flycast'
        source.mkdir(parents=True)
        (source / 'save-a.bin').write_bytes(b'own save A')
        (source / 'save-b.bin').write_bytes(b'own save B')
        source.chmod(0o555)
        operation_id = 'copy-fixture'
        operation = romtool.operation_dir(operation_id)
        move = {'action': 'copy', 'external_app': 'org.flycast.Flycast',
                'external_source': 'data/flycast', 'source_absolute': str(source), 'source': str(source),
                'destination': 'emulator/data/Flycast/data/flycast', 'files': runtime._library_tree(source)}
        romtool.atomic_json(operation / 'manifest.json', {
            'operation_id': operation_id, 'kind': 'emulator_library_migration',
            'status': 'prepared', 'moves': [move], 'edits': []})
        return home, source, self.root / move['destination'], operation_id, operation

    def test_move_resumes_after_rename_and_restores_interrupted_readonly_directory_mode(self):
        ident, operation, moves = self.migration_fixture()
        original = runtime._library_rename_exclusive
        def cut(source, destination):
            original(source, destination)
            raise Interruption()
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'apply', True)
        manifest = json.loads((operation / 'manifest.json').read_text())
        self.assertEqual(manifest['status'], 'applying')
        destination = self.root / moves[0]['destination']
        destination.chmod(0o755)  # Model the permission left by a power cut before finally.
        runtime.library_execute(self.root, ident, 'apply', True)
        runtime.library_execute(self.root, ident, 'verify')
        self.assertEqual(destination.stat().st_mode & 0o777, 0o555)
        self.assertFalse((self.root / moves[0]['source']).exists())

    def test_move_resumes_after_directory_sync_failure_and_preserves_readonly_file(self):
        ident, operation, moves = self.migration_fixture(regular_file=True)
        destination = self.root / moves[0]['destination']
        synchronize = romtool.sync_directory_chain
        failed = False
        def fail_after_rename(path):
            nonlocal failed
            if not failed and destination.exists():
                failed = True
                raise OSError('fixture directory sync interruption')
            synchronize(path)
        with patch.object(romtool, 'sync_directory_chain', side_effect=fail_after_rename):
            with self.assertRaises(OSError):
                runtime.library_execute(self.root, ident, 'apply', True)
        runtime.library_execute(self.root, ident, 'apply', True)
        runtime.library_execute(self.root, ident, 'verify')
        self.assertEqual(destination.stat().st_mode & 0o777, 0o444)

    def test_atomic_noreplace_preserves_a_destination_created_after_preflight(self):
        ident, operation, moves = self.migration_fixture()
        original = runtime._library_rename_exclusive
        def collide(source, destination):
            destination.mkdir(mode=0o700)
            original(source, destination)
        with patch.object(runtime, '_library_rename_exclusive', side_effect=collide):
            with self.assertRaises(FileExistsError):
                runtime.library_execute(self.root, ident, 'apply', True)
        self.assertTrue((self.root / moves[0]['source']).is_dir())
        self.assertEqual((self.root / moves[0]['source']).stat().st_mode & 0o777, 0o555)
        self.assertEqual((self.root / moves[0]['destination']).stat().st_mode & 0o777, 0o700)

    def test_resume_refuses_an_identical_replacement_inode(self):
        ident, operation, moves = self.migration_fixture()
        original = runtime._library_rename_exclusive
        def cut(source, destination):
            original(source, destination)
            raise Interruption()
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'apply', True)
        destination = self.root / moves[0]['destination']
        retained = self.root / 'retained-original'
        destination.chmod(0o755)
        destination.rename(retained)
        shutil.copytree(retained, destination)
        with self.assertRaisesRegex(ValueError, 'Inode'):
            runtime.library_execute(self.root, ident, 'apply', True)
        self.assertEqual((retained / 'runtime.bin').read_bytes(), (destination / 'runtime.bin').read_bytes())

    def test_resume_uses_filesystem_identity_when_device_numbers_change(self):
        ident, operation, moves = self.migration_fixture()
        original = runtime._library_rename_exclusive
        def cut(source, destination):
            original(source, destination)
            raise Interruption()
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'apply', True)
        manifest = json.loads((operation / 'manifest.json').read_text())
        manifest['moves'][0]['rename_checkpoints']['apply']['device'] += 1000
        romtool.atomic_json(operation / 'manifest.json', manifest)
        runtime.library_execute(self.root, ident, 'apply', True)
        self.assertTrue((self.root / moves[0]['destination']).exists())

    def test_partial_rollback_resumes_for_regular_files_and_directories(self):
        for regular_file in (False, True):
            with self.subTest(regular_file=regular_file):
                names = ('file-a', 'file-b') if regular_file else ('dir-a', 'dir-b')
                ident, operation, moves = self.migration_fixture(names, regular_file=regular_file)
                runtime.library_execute(self.root, ident, 'apply', True)
                original = runtime._library_rename_exclusive
                def cut(source, destination):
                    original(source, destination)
                    raise Interruption()
                with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
                    with self.assertRaises(Interruption):
                        runtime.library_execute(self.root, ident, 'rollback', True)
                self.assertEqual(json.loads((operation / 'manifest.json').read_text())['status'], 'rolling_back')
                runtime.library_execute(self.root, ident, 'rollback', True)
                self.assertTrue(all((self.root / move['source']).exists() for move in moves))
                self.assertTrue(all(not (self.root / move['destination']).exists() for move in moves))

    def test_rollback_skips_new_reference_not_reached_before_an_interruption(self):
        ident, operation, moves = self.migration_fixture(new_reference=True)
        original = runtime._library_rename_exclusive
        def cut(source, destination):
            original(source, destination)
            raise Interruption()
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'apply', True)
        runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertFalse((self.root / 'frontend/RetroFE/Config/new.json').exists())
        self.assertTrue((self.root / moves[0]['source']).exists())

    def test_created_reference_rollback_resumes_after_its_preservation_rename(self):
        ident, operation, moves = self.migration_fixture(new_reference=True)
        runtime.library_execute(self.root, ident, 'apply', True)
        original = runtime._library_rename_exclusive
        def cut(source, destination):
            original(source, destination)
            raise Interruption()
        with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'rollback', True)
        runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertEqual((operation / 'rollback/applied-files/frontend/RetroFE/Config/new.json').read_bytes(),
                         b'new fixture reference')
        self.assertFalse((self.root / 'frontend/RetroFE/Config/new.json').exists())

    def test_partial_import_stays_outside_canonical_data_and_is_preserved_on_retry(self):
        home, source, destination, ident, operation = self.copy_fixture()
        def cut_copy(original, target, **kwargs):
            target.mkdir()
            (target / 'save-a.bin').write_bytes((original / 'save-a.bin').read_bytes())
            (target / 'save-b.bin').write_bytes(b'own')
            raise Interruption()
        with patch.object(Path, 'home', return_value=home):
            with patch.object(runtime.shutil, 'copytree', side_effect=cut_copy):
                with self.assertRaises(Interruption):
                    runtime.library_execute(self.root, ident, 'apply', True)
            self.assertFalse(destination.exists())
            runtime.library_execute(self.root, ident, 'apply', True)
            runtime.library_execute(self.root, ident, 'verify')
        manifest = json.loads((operation / 'manifest.json').read_text())
        partial = operation / manifest['moves'][0]['interrupted_copies'][0]
        self.assertEqual((partial / 'save-b.bin').read_bytes(), b'own')
        self.assertEqual(runtime._library_tree(source), runtime._library_tree(destination))
        self.assertEqual(destination.stat().st_mode & 0o777, 0o555)

    def test_complete_import_staging_is_reused_after_sync_interruption(self):
        home, source, destination, ident, operation = self.copy_fixture()
        synchronize = runtime._sync_library_copy
        def cut_sync(path, inventory):
            synchronize(path, inventory)
            raise Interruption()
        with patch.object(Path, 'home', return_value=home):
            with patch.object(runtime, '_sync_library_copy', side_effect=cut_sync):
                with self.assertRaises(Interruption):
                    runtime.library_execute(self.root, ident, 'apply', True)
            self.assertFalse(destination.exists())
            with patch.object(runtime.shutil, 'copytree', side_effect=AssertionError('verified copy must be reused')):
                runtime.library_execute(self.root, ident, 'apply', True)
        self.assertEqual(runtime._library_tree(source), runtime._library_tree(destination))

    def test_partial_copy_rollback_preserves_staging_and_external_original(self):
        home, source, destination, ident, operation = self.copy_fixture()
        def cut_copy(original, target, **kwargs):
            target.mkdir()
            (target / 'save-a.bin').write_bytes(b'partial own save')
            raise Interruption()
        with patch.object(Path, 'home', return_value=home):
            with patch.object(runtime.shutil, 'copytree', side_effect=cut_copy):
                with self.assertRaises(Interruption):
                    runtime.library_execute(self.root, ident, 'apply', True)
            runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertFalse(destination.exists())
        self.assertEqual((source / 'save-a.bin').read_bytes(), b'own save A')
        self.assertEqual((operation / 'staging/library-copies/0/save-a.bin').read_bytes(), b'partial own save')

    def test_partial_copy_retention_resumes_after_rename_before_registration(self):
        home, source, destination, ident, operation = self.copy_fixture()
        staging = operation / 'staging/library-copies/0'
        staging.mkdir(parents=True)
        (staging / 'save-a.bin').write_bytes(b'partial own save')
        original = runtime._library_rename_exclusive
        def cut(current, target):
            original(current, target)
            raise Interruption()
        with patch.object(Path, 'home', return_value=home):
            with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
                with self.assertRaises(Interruption):
                    runtime.library_execute(self.root, ident, 'apply', True)
            runtime.library_execute(self.root, ident, 'apply', True)
            runtime.library_execute(self.root, ident, 'verify')
        manifest = json.loads((operation / 'manifest.json').read_text())
        retained = manifest['moves'][0]['interrupted_copies']
        self.assertEqual(len(retained), 1)
        self.assertEqual((operation / retained[0] / 'save-a.bin').read_bytes(), b'partial own save')
        self.assertEqual(runtime._library_tree(source), runtime._library_tree(destination))

    def test_import_reuses_publication_after_rename_before_checkpoint(self):
        home, source, destination, ident, operation = self.copy_fixture()
        original = runtime._library_rename_exclusive
        def cut(current, target):
            original(current, target)
            raise Interruption()
        with patch.object(Path, 'home', return_value=home):
            with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
                with self.assertRaises(Interruption):
                    runtime.library_execute(self.root, ident, 'apply', True)
            with patch.object(runtime.shutil, 'copytree', side_effect=AssertionError('published bytes must be reused')):
                runtime.library_execute(self.root, ident, 'apply', True)
            runtime.library_execute(self.root, ident, 'verify')
        self.assertEqual(runtime._library_tree(source), runtime._library_tree(destination))

    def test_rollback_resumes_after_cleanup_before_final_manifest(self):
        ident, operation, moves = self.migration_fixture(new_reference=True)
        runtime.library_execute(self.root, ident, 'apply', True)
        publish = romtool.atomic_json
        def cut(path, value):
            if path == operation / 'manifest.json' and value.get('status') == 'rolled_back':
                raise Interruption()
            publish(path, value)
        with patch.object(romtool, 'atomic_json', side_effect=cut):
            with self.assertRaises(Interruption):
                runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertFalse((self.root / 'emulator').exists())
        runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertTrue((self.root / moves[0]['source']).exists())

    def test_import_rollback_resumes_without_overwriting_later_external_save_changes(self):
        home, source, destination, ident, operation = self.copy_fixture()
        with patch.object(Path, 'home', return_value=home):
            runtime.library_execute(self.root, ident, 'apply', True)
            original = runtime._library_rename_exclusive
            def cut(current, target):
                original(current, target)
                raise Interruption()
            with patch.object(runtime, '_library_rename_exclusive', side_effect=cut):
                with self.assertRaises(Interruption):
                    runtime.library_execute(self.root, ident, 'rollback', True)
            (source / 'save-a.bin').write_bytes(b'later personal external save')
            runtime.library_execute(self.root, ident, 'rollback', True)
        self.assertFalse(destination.exists())
        self.assertEqual((source / 'save-a.bin').read_bytes(), b'later personal external save')
        self.assertEqual((operation / 'rollback/imported/emulator/data/Flycast/data/flycast/save-a.bin').read_bytes(),
                         b'own save A')

    def test_unreadable_directory_never_passes_as_an_empty_inventory(self):
        if os.geteuid() == 0:
            self.skipTest('root bypasses the filesystem permission fixture')
        directory = self.root / 'unreadable'
        directory.mkdir()
        (directory / 'hidden.bin').write_bytes(b'own hidden payload')
        directory.chmod(0)
        try:
            with self.assertRaises(PermissionError):
                runtime._library_tree(directory)
        finally:
            directory.chmod(0o700)

    def test_frontend_defaults_to_project_cores_and_preserves_explicit_override(self):
        source = (Path(__file__).resolve().parents[3] /
                  'frontend/RetroFE/Tools/launchers/retrofe/launch-retrofe.sh').read_text()
        helper = source[source.index('resolve_project_path() {'):source.index('RETROFE_ROM_ROOT="$(resolve_project_path')]
        assignment = next(line for line in source.splitlines() if line.startswith('RETROFE_RETROARCH_CORE_ROOT="'))
        frontend = self.root / 'frontend/RetroFE'
        script = f'ROOT={shlex.quote(str(frontend))}\n' + helper + assignment + '\nprintf "%s" "$RETROFE_RETROARCH_CORE_ROOT"\n'
        env = dict(os.environ)
        env.pop('RETROFE_RETROARCH_CORE_ROOT', None)
        actual = subprocess.check_output(['bash', '-c', script], env=env, text=True)
        self.assertEqual(actual, str(self.root / 'emulator/linux/retroarch-cores'))
        env['RETROFE_RETROARCH_CORE_ROOT'] = str(self.root / 'chosen cores')
        self.assertEqual(subprocess.check_output(['bash', '-c', script], env=env, text=True), env['RETROFE_RETROARCH_CORE_ROOT'])

    def test_pegasus_executable_identity_participates_in_the_shared_frontend_guard(self):
        process = self.root / 'proc/41'
        process.mkdir(parents=True)
        executable = self.root / 'frontend/pegasus-frontend/pegasus-fe'
        (process / 'exe').symlink_to(executable)
        with patch.object(Path, 'glob', return_value=[process]):
            running = romtool.pegasus_processes_running()
        self.assertEqual(running, [{'pid': 41, 'executable': str(executable)}])
        with patch.object(romtool, 'frontend_processes_running', return_value={'pegasus': running}):
            with self.assertRaisesRegex(ValueError, 'frontends'):
                runtime._library_guard(self.root)

    def test_reference_edits_use_durable_writer_and_ignore_old_temporary_name(self):
        target = self.root / 'emulator/data/RetroArch/retroarch.cfg'
        target.parent.mkdir(parents=True)
        before, after = b'old configuration', b'new configuration'
        target.write_bytes(before)
        stale = target.with_name(target.name + '.emulator-library-tmp')
        stale.write_bytes(b'older interrupted staging')
        opid = 'configuration-durability'
        operation = romtool.operation_dir(opid)
        for folder, data in [('rollback/files', before), ('proposed', after)]:
            path = operation / folder / target.relative_to(self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        romtool.atomic_json(operation / 'manifest.json', {
            'operation_id': opid, 'kind': 'emulator_library_migration', 'status': 'prepared',
            'moves': [], 'edits': [{'path': target.relative_to(self.root).as_posix(),
                'sha256_before': hashlib.sha256(before).hexdigest(), 'sha256_after': hashlib.sha256(after).hexdigest(),
                'mode': 0o640, 'after_move': False}]})
        with patch.object(romtool, 'atomic_write_bytes', wraps=romtool.atomic_write_bytes) as publish:
            runtime.library_execute(self.root, opid, 'apply', True)
            publish.assert_any_call(target, after, mode=0o640)
        self.assertEqual(stale.read_bytes(), b'older interrupted staging')
        # A cut after publication can leave the old prepared manifest on disk.
        manifest = json.loads((operation / 'manifest.json').read_text())
        manifest['status'] = 'prepared'
        romtool.atomic_json(operation / 'manifest.json', manifest)
        runtime.library_execute(self.root, opid, 'apply', True)
        runtime.library_execute(self.root, opid, 'verify')
        self.assertEqual(target.read_bytes(), after)
        with patch.object(romtool, 'atomic_write_bytes', wraps=romtool.atomic_write_bytes) as publish:
            runtime.library_execute(self.root, opid, 'rollback', True)
            publish.assert_any_call(target, before, mode=0o640)
        self.assertEqual(target.read_bytes(), before)

    def test_zip_traversal_drive_names_and_links_are_refused_before_publication(self):
        for index, name in enumerate(['../escape.exe', 'C:\\escape.exe', 'NUL.txt', 'name.']):
            source = self.root / f'bad-{index}.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr(name, b'not an emulator')
            with self.assertRaises(ValueError):
                runtime._extract_package(source, self.root / f'stage-{index}', {'format': 'zip_tree'})
            self.assertFalse((self.root / 'escape.exe').exists())
        source = self.root / 'link.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            link = zipfile.ZipInfo('link')
            link.external_attr = 0o120777 << 16
            archive.writestr(link, '../outside')
        with self.assertRaises(ValueError):
            runtime._extract_package(source, self.root / 'stage-link', {'format': 'zip_tree'})

    def test_native_package_validates_every_member_and_preserves_previous_tree(self):
        archive = self.root / 'official.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('app.exe', pe64())
            bundle.writestr('resources/icon.txt', b'resource')
        recipe = {'entry': 'app.exe', 'format': 'zip_tree', 'source_url': 'https://github.com/example/pinned.zip',
                  'source_size_bytes': archive.stat().st_size, 'source_sha256': runtime._sha256(archive), 'version': '1',
                  'max_unpacked_bytes': 1024}
        self.config({'packages': {'windows': {'example': recipe}}, 'allowed_download_hosts': ['github.com']})
        result = runtime.install_package(self.root, 'example', 'windows', True, archive)
        self.assertEqual(result['status'], 'installed_verified')
        program = self.root / 'emulator/windows/example'
        self.assertEqual((program / 'resources/icon.txt').read_bytes(), b'resource')
        self.assertEqual(runtime.install_package(self.root, 'example', 'windows', False)['status'], 'already_installed_verified')
        (program / 'app.exe').write_bytes(pe64() + b'user modification')
        with self.assertRaisesRegex(ValueError, 'Destino existente'):
            runtime.install_package(self.root, 'example', 'windows', True, archive)
        replacement = runtime.install_package(self.root, 'example', 'windows', True, archive, replace_existing=True)
        backup = romtool.operation_dir(replacement['operation_id']) / 'rollback/previous-package/app.exe'
        self.assertEqual(backup.read_bytes(), pe64() + b'user modification')
        runtime.rollback_install(self.root, replacement['operation_id'], True)
        self.assertEqual((program / 'app.exe').read_bytes(), pe64() + b'user modification')
        self.assertTrue((romtool.operation_dir(replacement['operation_id']) / 'rolled-back-package/app.exe').is_file())

    def test_tar_gz_package_preserves_resources_and_executable_mode(self):
        source = self.root / 'official.tar.gz'
        with tarfile.open(source, 'w:gz') as bundle:
            for name, payload, mode in [('release/emulator', b'\x7fELFfixture', 0o755),
                                        ('release/Config/Games.xml', b'<games/>', 0o644)]:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(payload), mode
                bundle.addfile(info, io.BytesIO(payload))
        recipe = {'entry': 'release/emulator', 'format': 'tar_gz_tree',
                  'source_url': 'https://github.com/example/pinned.tar.gz',
                  'source_size_bytes': source.stat().st_size, 'source_sha256': runtime._sha256(source),
                  'version': '1', 'max_unpacked_bytes': 1024}
        self.config({'packages': {'linux': {'example': recipe}}, 'allowed_download_hosts': ['github.com']})
        result = runtime.install_package(self.root, 'example', 'linux', True, source)
        target = self.root / 'emulator/linux/example/release'
        self.assertEqual(result['status'], 'installed_verified')
        self.assertEqual((target / 'Config/Games.xml').read_bytes(), b'<games/>')
        self.assertEqual((target / 'emulator').stat().st_mode & 0o777, 0o755)
        self.assertEqual(runtime.install_package(self.root, 'example', 'linux', False)['status'],
                         'already_installed_verified')

    def test_tar_gz_refuses_unsafe_members_and_expansion_before_extraction(self):
        cases = [('../escape', tarfile.REGTYPE, 1), ('C:/escape', tarfile.REGTYPE, 1),
                 ('bad?.exe', tarfile.REGTYPE, 1),
                 ('NUL.txt', tarfile.REGTYPE, 1), ('link', tarfile.SYMTYPE, 0),
                 ('hardlink', tarfile.LNKTYPE, 0), ('pipe', tarfile.FIFOTYPE, 0),
                 ('large', tarfile.REGTYPE, 1025)]
        for index, (name, kind, size) in enumerate(cases):
            with self.subTest(name=name):
                source, target = self.root / f'bad-{index}.tar.gz', self.root / f'stage-{index}'
                with tarfile.open(source, 'w:gz') as bundle:
                    info = tarfile.TarInfo(name)
                    info.type, info.size, info.linkname = kind, size, '../outside'
                    bundle.addfile(info, io.BytesIO(b'x' * size) if size else None)
                with self.assertRaises(ValueError):
                    runtime._extract_package(source, target, {'format': 'tar_gz_tree', 'max_unpacked_bytes': 1024})
                self.assertFalse(target.exists())

    def test_tar_gz_refuses_case_collisions_file_parents_and_bad_crc(self):
        for index, names in enumerate([['Game', 'game'], ['file', 'file/child']]):
            source, target = self.root / f'conflict-{index}.tar.gz', self.root / f'conflict-stage-{index}'
            with tarfile.open(source, 'w:gz') as bundle:
                for name in names:
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    bundle.addfile(info, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                runtime._extract_package(source, target, {'format': 'tar_gz_tree'})
            self.assertFalse(target.exists())
        source = self.root / 'crc.tar.gz'
        with tarfile.open(source, 'w:gz') as bundle:
            info = tarfile.TarInfo('valid')
            info.size = 1
            bundle.addfile(info, io.BytesIO(b'x'))
        data = bytearray(source.read_bytes())
        data[-8] ^= 1
        source.write_bytes(data)
        with self.assertRaises(OSError):
            runtime._extract_package(source, self.root / 'bad-crc', {'format': 'tar_gz_tree'})
        self.assertFalse((self.root / 'bad-crc').exists())

    def test_supermodel_data_uses_relative_package_resources_and_preserves_saves(self):
        program = self.root / 'emulator/linux/supermodel/release'
        (program / 'Config').mkdir(parents=True)
        (program / 'Assets').mkdir()
        (program / 'Config/Music.xml').write_bytes(b'<music/>')
        (program / 'supermodel').write_bytes(b'\x7fELFfixture')
        installed = {'status': 'already_installed_verified', 'operation_id': 'fixture-package',
                     'entry': 'emulator/linux/supermodel/release/supermodel'}
        with patch.object(runtime, 'install_package', return_value=installed):
            plan = runtime.supermodel_data_plan(self.root)
            runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
            runtime.library_execute(self.root, plan['operation_id'], 'verify')
            userdata = self.root / 'emulator/data/Supermodel'
            self.assertEqual((userdata / 'Assets').resolve(), program / 'Assets')
            self.assertFalse(os.path.isabs(os.readlink(userdata / 'Config/Music.xml')))
            (userdata / 'NVRAM').mkdir()
            save = userdata / 'NVRAM/game.nv'
            save.write_bytes(b'later personal save')
            self.assertEqual(runtime.supermodel_data_plan(self.root)['status'], 'already_configured_verified')
            self.assertEqual(save.read_bytes(), b'later personal save')
            with self.assertRaisesRegex(ValueError, 'Inventário/SHA'):
                runtime.library_execute(self.root, plan['operation_id'], 'rollback', True)
            self.assertEqual(save.read_bytes(), b'later personal save')
            (userdata / 'Config/Music.xml').unlink()
            (userdata / 'Config/Music.xml').write_bytes(b'personal choice')
            with self.assertRaisesRegex(ValueError, 'reconciliação'):
                runtime.supermodel_data_plan(self.root)
            self.assertEqual((userdata / 'Config/Music.xml').read_bytes(), b'personal choice')

    def test_migration_preserves_readonly_payload_and_refuses_changed_reference_on_rollback(self):
        original = self.root / 'tools/roms/runtimes/example'
        original.mkdir(parents=True)
        (original / 'runtime.bin').write_bytes(b'payload')
        reference = self.root / 'tools/roms/config/launch.json'
        reference.parent.mkdir()
        reference.write_text('{"command":"tools/roms/runtimes/example/runtime.bin"}')
        original.chmod(0o555)
        prepared = runtime.library_plan(self.root)
        operation_id = prepared['operation_id']
        runtime.library_execute(self.root, operation_id, 'apply', True)
        runtime.library_execute(self.root, operation_id, 'verify')
        destination = self.root / 'emulator/linux/example'
        self.assertFalse(original.exists())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o555)
        self.assertEqual((destination / 'runtime.bin').read_bytes(), b'payload')
        after = reference.read_bytes()
        reference.write_bytes(after + b' ')
        with self.assertRaisesRegex(ValueError, 'Referência mudou'):
            runtime.library_execute(self.root, operation_id, 'rollback', True)
        reference.write_bytes(after)
        runtime.library_execute(self.root, operation_id, 'rollback', True)
        self.assertTrue(original.is_dir())
        self.assertEqual(original.stat().st_mode & 0o777, 0o555)
        original.chmod(0o755)
        self.assertIn('tools/roms/runtimes/example', reference.read_text())

    def test_windows_overlay_relocates_program_and_keeps_one_save_tree(self):
        program = self.root / 'emulator/windows/retroarch/retroarch.exe'
        program.parent.mkdir(parents=True)
        program.write_bytes(pe64())
        core = self.root / 'emulator/windows/retroarch-cores/mgba_libretro.dll'
        core.parent.mkdir()
        core.write_bytes(pe64())
        save = self.root / 'emulator/data/RetroArch/saves/Game.srm'
        save.parent.mkdir(parents=True)
        save.write_bytes(b'only save')
        self.config({'packages': {'windows': {'retroarch': {'entry': 'retroarch.exe'}}}})
        profile = {'retroarch': {'savefile_directory': '../../emulator/data/RetroArch/saves'},
                   'collections': {'GBA': {'adapter': 'retroarch', 'core': 'mgba_libretro.so', 'rom_subdir': 'gba'}}}
        first = portable_emulator.native_profile(self.root, profile, 'windows')
        self.assertEqual(first['collections']['GBA']['core'], 'mgba_libretro.dll')
        relocated = self.root.with_name('Relocated library')
        self.root.rename(relocated)
        self.root = relocated
        second = portable_emulator.native_profile(self.root, profile, 'windows')
        self.assertTrue(second['retroarch']['executable'].startswith(str(relocated)))
        self.assertEqual(second['retroarch']['savefile_directory'], first['retroarch']['savefile_directory'])
        self.assertEqual((relocated / 'emulator/data/RetroArch/saves/Game.srm').read_bytes(), b'only save')

    def test_junction_buffer_matches_windows_mount_point_wire_format(self):
        target = 'E:\\Biblioteca portátil\\emulator\\data'
        data = native_bindings.junction_buffer(target)
        tag, length, reserved = struct.unpack_from('<IHH', data)
        self.assertEqual((tag, reserved, length), (0xA0000003, 0, len(data) - 8))
        sub_offset, sub_length, print_offset, print_length = struct.unpack_from('<HHHH', data, 8)
        paths = data[16:]
        self.assertEqual(paths[sub_offset:sub_offset + sub_length].decode('utf-16le'), '\\??\\' + target)
        self.assertEqual(paths[print_offset:print_offset + print_length].decode('utf-16le'), target)

    def test_overlapping_runtime_ids_keep_their_native_operating_system(self):
        for name in ('xenia-canary', 'xenia-canary-linux'):
            path = self.root / 'tools/roms/runtimes' / name
            path.mkdir(parents=True)
            (path / 'runtime').write_bytes(name.encode())
        reference = self.root / 'tools/roms/config/launch.json'
        reference.parent.mkdir()
        reference.write_text('{"command":"tools/roms/runtimes/xenia-canary-linux/runtime"}')
        plan = runtime.library_plan(self.root)
        runtime.library_execute(self.root, plan['operation_id'], 'apply', True)
        self.assertIn('emulator/linux/xenia-canary-linux/runtime', reference.read_text())

    def test_runtime_version_probe_uses_the_same_shared_data_root(self):
        row = {'userdata_subdir': 'RPCS3', 'environment': {'XDG_CONFIG_HOME': '{userdata}/config',
                                                        'XDG_CACHE_HOME': '{userdata}/cache'}}
        environment = romtool.host_runtime_probe_environment(row)
        self.assertEqual(environment['XDG_CONFIG_HOME'], str(self.root / 'emulator/data/RPCS3/config'))
        self.assertEqual(environment['XDG_CACHE_HOME'], str(self.root / 'emulator/data/RPCS3/cache'))
        self.assertFalse((self.root / 'frontend/RetroFE/UserData/RPCS3').exists())
        with self.assertRaises(ValueError):
            romtool.host_runtime_probe_environment({**row, 'userdata_subdir': '../external'})

    def test_runtime_probe_cwd_requires_existing_project_userdata(self):
        row = {'userdata_subdir': 'Supermodel', 'probe_working_directory': '{userdata}'}
        with self.assertRaisesRegex(ValueError, 'Prepare os dados'):
            romtool.host_runtime_probe_directory(row)
        userdata = self.root / 'emulator/data/Supermodel'
        userdata.mkdir(parents=True)
        self.assertEqual(romtool.host_runtime_probe_directory(row), userdata)
        for changes in [{'userdata_subdir': '../outside'}, {'userdata_subdir': '.'},
                        {'probe_working_directory': '/tmp'}]:
            with self.assertRaises(ValueError):
                romtool.host_runtime_probe_directory({**row, **changes})
        userdata.rename(userdata.with_name('Preserved'))
        userdata.symlink_to('Preserved', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            romtool.host_runtime_probe_directory(row)
        userdata.unlink()
        emulator = self.root / 'emulator'
        emulator.rename(self.root / 'external-emulator')
        emulator.symlink_to('external-emulator', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            romtool.host_runtime_probe_directory(row)

    def test_command_launcher_expands_program_resources_and_shared_working_directory(self):
        from tools.roms.tests.launcher_fixture import load_launcher
        host = load_launcher('supermodel_launch_fixture')
        host.ROOT = self.root / 'frontend/RetroFE'
        program = self.root / 'emulator/linux/supermodel/release'
        program.mkdir(parents=True)
        executable = program / 'supermodel'
        executable.write_bytes(b'fixture never executed')
        executable.chmod(0o755)
        item = self.root / 'roms/model3/Game.zip'
        item.parent.mkdir(parents=True)
        item.write_bytes(b'fixture')
        profile = {'rom_root': str(self.root / 'roms'), 'bios_root': str(self.root / 'bios')}
        system = {'rom_subdir': 'model3', 'command': str(executable), 'userdata_subdir': 'Supermodel',
                  'working_directory': '{userdata}',
                  'args': ['{item}', '-game-xml-file={program_dir}/Config/Games.xml']}
        with patch.object(host, 'run_with_music_paused', return_value=0) as run:
            self.assertEqual(host.launch_command(profile, system, item), 0)
        self.assertEqual(run.call_args.args[0], [str(executable), str(item),
                                               '-game-xml-file=' + str(program / 'Config/Games.xml')])
        self.assertEqual(run.call_args.kwargs['cwd'], self.root / 'emulator/data/Supermodel')

    def test_linux_fallback_uses_packaged_dolphin_without_a_host_flatpak(self):
        path = self.root / 'emulator/linux/dolphin/Dolphin.AppImage'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'ELF fixture')
        self.config({'packages': {'linux': {'dolphin': {'entry': 'Dolphin.AppImage'}}}})
        profile = {'flatpak_apps': {'org.DolphinEmu.dolphin-emu': {}}, 'collections': {}}
        with patch.object(portable_emulator.shutil, 'which', return_value=None):
            adapted = portable_emulator.native_profile(self.root, profile, 'linux')
        self.assertEqual(adapted['flatpak_apps']['org.DolphinEmu.dolphin-emu']['executable'], str(path))

    def test_selected_external_save_import_preserves_original_and_refuses_changed_rollback(self):
        home = self.root.parent / 'old host home'
        source = home / '.var/app/org.flycast.Flycast/data/flycast/vmu_save_A1.bin'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'VMU save')
        row = {'action': 'copy', 'external_app': 'org.flycast.Flycast',
               'external_source': 'data/flycast/vmu_save_A1.bin', 'source_absolute': str(source),
               'source': str(source), 'destination': 'emulator/data/Flycast/data/flycast/vmu_save_A1.bin',
               'files': runtime._library_tree(source)}
        opid = 'selected-vmu-import'
        romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json',
                            {'kind': 'emulator_library_migration', 'status': 'prepared', 'operation_id': opid,
                             'moves': [row], 'edits': []})
        destination = self.root / row['destination']
        with patch.object(Path, 'home', return_value=home):
            runtime.library_execute(self.root, opid, 'apply', True)
            runtime.library_execute(self.root, opid, 'verify')
            self.assertEqual(source.read_bytes(), destination.read_bytes())
            destination.write_bytes(b'new session save')
            with self.assertRaisesRegex(ValueError, 'Inventário/SHA divergente'):
                runtime.library_execute(self.root, opid, 'rollback', True)
            destination.write_bytes(b'VMU save')
            runtime.library_execute(self.root, opid, 'rollback', True)
            self.assertEqual(source.read_bytes(), b'VMU save')
            self.assertFalse(destination.exists())

    def test_frontend_projection_can_switch_os_and_preserves_personal_commands(self):
        import xml.etree.ElementTree as ET
        catalog = self.root / 'frontend/RetroFE/Config/platform-catalog.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text('{"systems":[{"id":"gba","display_name":"GBA","extensions":["gba"]}]}')
        custom = self.root / 'frontend/ES-DE/UserData/custom_systems'
        custom.mkdir(parents=True)
        (custom / 'es_systems.xml').write_text('<systemList><system><name>personal</name><command>personal.exe</command></system></systemList>')
        profile = {'collections': {'Game Boy Advance': {'rom_subdir': 'gba', 'adapter': 'retroarch', 'extensions': 'gba'}}}
        files, _ = esde_projection(self.root, profile, 'windows')
        for relative, data in files:
            (self.root / relative).write_bytes(data)
        xml = ET.parse(custom / 'es_systems.xml')
        command = xml.find("system[name='gba']/command").text
        self.assertIn('-X utf8', command)
        self.assertIn('"Game Boy Advance"', command)
        self.assertEqual(xml.find("system[name='personal']/command").text, 'personal.exe')
        files, _ = esde_projection(self.root, profile, 'linux')
        for relative, data in files:
            (self.root / relative).write_bytes(data)
        self.assertNotIn('python.exe', (custom / 'es_find_rules.xml').read_text())
        self.assertIn("'Game Boy Advance'", (custom / 'es_systems.xml').read_text())


if __name__ == '__main__':
    unittest.main()
