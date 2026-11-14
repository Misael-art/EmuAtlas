"""Native packaging and shared-data migration safety, using isolated workspaces."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.roms import romtool, runtime_install as runtime
from tools.roms import portable_emulator, native_bindings
from tools.roms.frontend_projection import esde_projection


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
