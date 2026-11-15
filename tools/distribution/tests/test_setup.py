import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup as s
import workspace as w
from install_config import configure, bind_available_runtimes


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'host with space'
        self.root.mkdir()
        self.engine = s.Installer(self.root, {'components': {}})
        self.log = (self.root / 'commands.log').open('w')
        self.addCleanup(self.log.close)
        self.engine.log = self.log

    def test_graph_orders_dependencies_and_rejects_cycles(self):
        config = {'components': {'b': {'requires': ['a']}, 'a': {}}}
        self.assertEqual(s.graph(config, ['b']), ['a', 'b'])
        config['components']['a']['requires'] = ['b']
        with self.assertRaises(ValueError):
            s.graph(config)

    def test_native_runtime_package_reuses_receipt_and_prepares_shared_data(self):
        runtime = Mock()
        runtime.load_recipes.return_value = {}
        runtime.install_package.return_value = {'status': 'already_installed_verified', 'operation_id': 'package'}
        runtime.supermodel_data_plan.return_value = {'status': 'prepared', 'operation_id': 'shared-data'}
        with patch.dict(sys.modules, {'runtime_install': runtime, 'romtool': SimpleNamespace(PROJECT_ROOT=self.root)}), \
                patch.object(self.engine, 'acquire') as acquire:
            result = self.engine.runtime({'runtime_id': 'supermodel', 'package_platform': 'linux'})
        self.assertEqual(result['status'], 'reused')
        acquire.assert_not_called()
        runtime.library_execute.assert_any_call(self.root, 'shared-data', 'apply', True)
        runtime.library_execute.assert_any_call(self.root, 'shared-data', 'verify')

    def test_native_runtime_package_uses_pinned_source_and_preserves_existing_data(self):
        runtime = Mock()
        recipe = {'source_url': 'https://github.com/example/pinned.tar.gz', 'source_size_bytes': 12,
                  'source_sha256': 'a' * 64}
        runtime.load_recipes.return_value = {'packages': {'linux': {'supermodel': recipe}},
                                            'allowed_download_hosts': ['github.com']}
        runtime.install_package.side_effect = [{'status': 'prepared'},
                                               {'status': 'installed_verified', 'operation_id': 'package'}]
        runtime.supermodel_data_plan.return_value = {'status': 'already_configured_verified'}
        source = self.root / 'pinned-source'
        with patch.dict(sys.modules, {'runtime_install': runtime, 'romtool': SimpleNamespace(PROJECT_ROOT=self.root)}), \
                patch.object(self.engine, 'acquire', return_value=source) as acquire:
            result = self.engine.runtime({'runtime_id': 'supermodel', 'package_platform': 'linux'})
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(acquire.call_args.args[0]['sha256'], recipe['source_sha256'])
        runtime.install_package.assert_called_with(self.root, 'supermodel', 'linux', True, from_file=source)
        runtime.library_execute.assert_not_called()

    def test_bluemsx_machine_prepared_for_reused_verified_and_installed_cores(self):
        for mode in ('host', 'portable', 'install'):
            with self.subTest(mode=mode):
                runtime = Mock()
                entry = self.root / ('emulator/linux/cores-' + mode) / 'bluemsx_libretro.so'
                payload = b'own synthetic core'
                recipe = {'entry': entry.name, 'install_dir': 'cores-' + mode,
                          'source_url': 'https://example.invalid/core.zip', 'source_size_bytes': 12,
                          'source_sha256': 'a' * 64, 'entry_sha256': hashlib.sha256(payload).hexdigest()}
                runtime.load_recipes.return_value = {'runtimes': {'core-bluemsx': recipe},
                                                     'allowed_download_hosts': ['example.invalid']}
                runtime.host_flatpaks.return_value = ({}, True)
                runtime.scan_host.return_value = ['own-host-core'] if mode == 'host' else []
                runtime.install_runtime.return_value = {'operation_id': 'core-install'}
                runtime.bluemsx_cbios_plan.return_value = {'status': 'prepared', 'operation_id': 'machine'}
                runtime.library_execute.return_value = {'status': 'completed_verified'}
                if mode == 'portable':
                    entry.parent.mkdir(parents=True)
                    entry.write_bytes(payload)
                with patch.dict(sys.modules, {'runtime_install': runtime, 'romtool': SimpleNamespace(PROJECT_ROOT=self.root)}), \
                        patch.object(self.engine, 'acquire', return_value=self.root / 'source') as acquire:
                    result = self.engine.runtime({'runtime_id': 'core-bluemsx'})
                self.assertEqual(result['status'], {'host': 'reused', 'portable': 'verified', 'install': 'installed'}[mode])
                self.assertEqual(result['shared_data']['status'], 'completed_verified')
                runtime.bluemsx_cbios_plan.assert_called_once_with(self.root)
                runtime.library_execute.assert_any_call(self.root, 'machine', 'apply', True)
                runtime.library_execute.assert_any_call(self.root, 'machine', 'verify')
                if mode != 'install':
                    acquire.assert_not_called()

    def test_vita_runtime_prepares_shared_portable_data_after_install(self):
        runtime = Mock()
        recipe = {'entry': 'Vita3K-x86_64.AppImage', 'install_dir': 'vita3k', 'format': 'appimage',
                  'source_url': 'https://example.invalid/Vita3K.AppImage',
                  'source_size_bytes': 12, 'source_sha256': 'b' * 64}
        runtime.load_recipes.return_value = {'runtimes': {'vita3k': recipe},
                                            'allowed_download_hosts': ['example.invalid']}
        runtime.host_flatpaks.return_value = ({}, True)
        runtime.scan_host.return_value = []
        runtime.install_runtime.return_value = {'operation_id': 'vita-package'}
        runtime.vita3k_data_plan.return_value = {'status': 'prepared', 'operation_id': 'vita-data'}
        runtime.library_execute.return_value = {'status': 'completed_verified'}
        with patch.dict(sys.modules, {'runtime_install': runtime, 'romtool': SimpleNamespace(PROJECT_ROOT=self.root)}), \
                patch.object(self.engine, 'acquire', return_value=self.root / 'source'):
            result = self.engine.runtime({'runtime_id': 'vita3k'})
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(result['shared_data']['status'], 'completed_verified')
        runtime.vita3k_data_plan.assert_called_once_with(self.root)
        runtime.library_execute.assert_any_call(self.root, 'vita-data', 'apply', True)
        runtime.library_execute.assert_any_call(self.root, 'vita-data', 'verify')

    def make_wheel(self, directory, name, version):
        path = directory / f'{name}-{version}-py3-none-any.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(f'{name}-{version}.dist-info/METADATA',
                             f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
        return path

    def prepare_python_fixture(self, item=None):
        item = item or {'kind': 'python', 'packages': ['Fixture==1.0']}
        def run(command, **kwargs):
            if 'wheel' in command:
                directory = Path(command[command.index('--wheel-dir') + 1])
                self.make_wheel(directory, 'Fixture', '1.0')
                self.make_wheel(directory, 'transitive', '2.0')
        with patch.object(self.engine, 'run', side_effect=run) as commands:
            directory = self.engine.prepare_wheelhouse('python-tools', item, Path('/fixture/python'))
        return item, directory, commands

    def test_python_wheelhouse_locks_transitives_and_reuses_without_acquisition(self):
        item, directory, commands = self.prepare_python_fixture()
        manifest = s.verify_wheelhouse(directory, item)
        self.assertEqual(len(manifest['files']), 2)
        self.assertIn('transitive==2.0 --hash=sha256:', (directory / 'requirements.txt').read_text())
        self.assertIn('--only-binary=:all:', commands.call_args.args[0])
        with patch.object(self.engine, 'run', side_effect=AssertionError('cached closure must be reused')):
            self.assertEqual(self.engine.prepare_wheelhouse('python-tools', item, Path('/fixture/python')), directory)
        with patch.object(s, 'python_host', return_value={'abi': 'other host'}):
            with self.assertRaisesRegex(ValueError, 'outro host'):
                s.verify_wheelhouse(directory, item)

    def test_changed_wheel_lock_or_extra_file_prevents_reuse(self):
        item, directory, _ = self.prepare_python_fixture()
        wheel = next(directory.glob('*.whl'))
        original = wheel.read_bytes()
        wheel.write_bytes(original + b'changed')
        with self.assertRaises(ValueError):
            s.verify_wheelhouse(directory, item)
        wheel.write_bytes(original)
        lock = directory / 'requirements.txt'
        requirements = lock.read_bytes()
        lock.write_bytes(requirements + b'other==1\n')
        with self.assertRaises(ValueError):
            s.verify_wheelhouse(directory, item)
        lock.write_bytes(requirements)
        (directory / 'unexpected').write_text('preserve')
        with self.assertRaises(ValueError):
            s.verify_wheelhouse(directory, item)

    def test_python_install_entry_uses_only_verified_wheels_and_runs_pip_check(self):
        item, directory, _ = self.prepare_python_fixture()
        python = self.root / 'tools/distribution/.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('fixture')
        with patch.object(self.engine, 'run') as commands:
            result = self.engine.action('python-tools', item)
        install = commands.call_args_list[0].args[0]
        for option in ['--no-index', '--require-hashes', '--only-binary=:all:']:
            self.assertIn(option, install)
        self.assertEqual(commands.call_args_list[-1].args[0][-2:], ['pip', 'check'])
        self.assertEqual(result['locked_packages'], 2)
        self.assertEqual(result['wheelhouse_sha256'], w.digest(directory / 'manifest.json'))

    def test_python_preparation_rejects_unpinned_requirements_and_undeclared_source_builds(self):
        with patch.object(self.engine, 'run') as run:
            for item in [{'packages': ['Fixture>=1']},
                         {'packages': ['Fixture==1.0'], 'source_build_packages': ['unknown']}]:
                with self.assertRaises(ValueError):
                    self.engine.prepare_wheelhouse('python-tools', item, Path('/fixture/python'))
            run.assert_not_called()

    def test_interrupted_wheel_publication_reuses_verified_published_closure(self):
        sync = s.romtool.sync_directory_chain
        def fail_after_publication(directory):
            if directory.parent.name == 'wheelhouse' and (directory / 'manifest.json').is_file():
                raise OSError('fixture sync failure after rename')
            sync(directory)
        with patch.object(s.romtool, 'sync_directory_chain', side_effect=fail_after_publication):
            with self.assertRaises(OSError):
                self.prepare_python_fixture()
        item = {'kind': 'python', 'packages': ['Fixture==1.0']}
        with patch.object(self.engine, 'run', side_effect=AssertionError('no new download after publication')):
            directory = self.engine.prepare_wheelhouse('python-tools', item, Path('/fixture/python'))
        self.assertEqual(len(s.verify_wheelhouse(directory, item)['files']), 2)

    def test_failed_parent_blocks_dependant_but_not_independent_component_and_resume_rechecks(self):
        self.engine.config = {'components': {'first': {'kind': 'fixture'},
                              'dependent': {'kind': 'fixture', 'requires': ['first']},
                              'independent': {'kind': 'fixture'}}}
        def action(name, item):
            if name == 'first':
                raise ValueError('fixture failure')
            return {'status': 'verified'}
        with patch.object(self.engine, 'action', side_effect=action), contextlib.redirect_stdout(io.StringIO()):
            result = self.engine.install()
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['results']['dependent']['status'], 'blocked')
        self.assertEqual(result['results']['independent']['status'], 'verified')
        with patch.object(self.engine, 'action', return_value={'status': 'verified'}) as action, contextlib.redirect_stdout(io.StringIO()):
            result = self.engine.install()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(action.call_count, 3)

    def test_interrupt_is_checkpointed_without_claiming_completion(self):
        self.engine.config = {'components': {'first': {'kind': 'fixture'}, 'next': {'kind': 'fixture'}}}
        with patch.object(self.engine, 'action', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            result = self.engine.install()
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['remaining'], ['next'])
        self.assertFalse(w.read(self.engine.state / 'latest.json')['all_components_installed'])

    def test_a_completed_subset_never_claims_the_entire_ecosystem_installed(self):
        self.engine.config = {'components': {'one': {'kind': 'fixture'}, 'other': {'kind': 'fixture'}}}
        with patch.object(self.engine, 'action', return_value={'status': 'verified'}), contextlib.redirect_stdout(io.StringIO()):
            result = self.engine.install(['one'])
        self.assertTrue(result['selected_scope_complete'])
        self.assertFalse(result['all_components_installed'])
        self.assertEqual(result['selected_components'], ['one'])

    def test_native_packages_reuse_and_postcheck(self):
        self.engine.host = {'manager': 'apt-get', 'immutable': False}
        item = {'packages': {'apt-get': ['example']}}
        with patch.object(self.engine, 'installed_package', return_value=True), patch.object(self.engine, 'run') as run:
            self.assertEqual(self.engine.native_packages(item)['status'], 'reused')
            run.assert_not_called()
        with patch.object(self.engine, 'installed_package', return_value=False), patch.object(self.engine, 'run'), patch('os.geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'não confirmados'):
                self.engine.native_packages(item)

    def test_immutable_host_never_unlocked(self):
        self.engine.host = {'manager': 'pacman', 'immutable': True}
        with patch.object(self.engine, 'installed_package', return_value=False), patch.object(self.engine, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'imutável'):
                self.engine.native_packages({'packages': {'pacman': ['fixture']}})
            run.assert_not_called()

    def test_flatpak_already_present_does_not_install_again(self):
        with patch.object(self.engine, 'available_flatpak', return_value=True), patch.object(self.engine, 'run') as run:
            self.assertEqual(self.engine.flatpak({'app_id': 'org.example.App'})['status'], 'reused')
            run.assert_not_called()

    def test_native_steam_prevents_duplicate_flatpak_install(self):
        with patch.object(s.shutil, 'which', return_value='/usr/bin/steam'), patch.object(self.engine, 'run') as run:
            result = self.engine.flatpak({'app_id': 'com.valvesoftware.Steam', 'host_commands': ['steam']})
        self.assertEqual(result['native_command'], 'steam')
        run.assert_not_called()

    def test_reused_flatpak_is_bound_in_owned_profile_and_user_edits_are_preserved(self):
        w.initialize(self.root, [], True)
        with patch.object(self.engine, 'available_flatpak', side_effect=lambda app: app == 'net.rpcs3.RPCS3'):
            bind_available_runtimes(self.engine)
        path = self.root / 'frontend/RetroFE/Config/profiles/host-linux.json'
        profile = w.read(path)
        self.assertEqual(profile['collections']['Sony PlayStation 3']['adapter'], 'flatpak')
        profile['description'] = 'customer edit'
        path.write_text(json.dumps(profile))
        self.assertEqual(bind_available_runtimes(self.engine)['status'], 'preserved_customer_profile')
        self.assertEqual(w.read(path)['description'], 'customer edit')

    def test_publish_never_overwrites_customer_file(self):
        source = self.root / 'source'
        source.write_bytes(b'new')
        target = self.root / 'destination'
        target.write_bytes(b'customer')
        with self.assertRaises(ValueError):
            self.engine.publish(source, 'destination')
        self.assertEqual(target.read_bytes(), b'customer')

    def test_archive_traversal_is_refused_before_writing(self):
        archive = self.root / 'unsafe.tar'
        with tarfile.open(archive, 'w') as out:
            info = tarfile.TarInfo('../escaped')
            info.size = 1
            out.addfile(info, io.BytesIO(b'x'))
        with self.assertRaises(ValueError):
            self.engine.extract(archive, self.root / 'stage')
        self.assertFalse((self.root / 'escaped').exists())

    def test_archive_symlink_is_refused(self):
        archive = self.root / 'unsafe.tar'
        with tarfile.open(archive, 'w') as out:
            info = tarfile.TarInfo('link')
            info.type = tarfile.SYMTYPE
            info.linkname = '/etc/passwd'
            out.addfile(info)
        with self.assertRaises(ValueError):
            self.engine.extract(archive, self.root / 'stage')

    def test_explicit_native_package_selection_skips_unneeded_system_symlinks(self):
        archive = self.root / 'package.tar'
        with tarfile.open(archive, 'w') as out:
            info = tarfile.TarInfo('usr/bin/tool')
            info.type = tarfile.SYMTYPE
            info.linkname = '../lib/tool'
            out.addfile(info)
            info = tarfile.TarInfo('usr/lib/tool')
            info.size = 3
            out.addfile(info, io.BytesIO(b'elf'))
        self.engine.extract(archive, self.root / 'stage', ['usr/lib/tool'])
        self.assertEqual((self.root / 'stage/usr/lib/tool').read_bytes(), b'elf')
        self.assertFalse((self.root / 'stage/usr/bin').exists())

    def test_timeout_kills_only_supervised_child_group(self):
        started = time.monotonic()
        with self.assertRaises(Exception):
            self.engine.run([sys.executable, '-c', 'import time;time.sleep(10)'], timeout=.1)
        self.assertLess(time.monotonic() - started, 4)

    def test_configuration_routes_all_collections_to_context_and_preserves_customization(self):
        w.initialize(self.root, [], True)
        result = configure(self.engine)
        self.assertEqual(result['status'], 'configured')
        fe = self.root / 'frontend/RetroFE'
        text = (fe / 'collections/Sega Master System/settings.conf').read_text()
        self.assertIn('%BASE_ITEM_PATH%/mastersystem', text)
        self.assertIn('../../media/mastersystem/cover-front', text)
        launcher = (fe / 'launchers.linux/Workspace - Sega Master System.conf').read_text()
        self.assertIn('--collection "%ITEM_COLLECTION_NAME%" --item "%ITEM_FILEPATH%"', launcher)
        self.assertTrue((fe / '../../emulator/data/RetroArch/retroarch.cfg').is_file(),
                        'The existing RetroArch adapter requires this file before launch')
        p = fe / 'controls.conf'
        p.write_text('my controls')
        configure(self.engine)
        self.assertEqual(p.read_text(), 'my controls')

    def test_generated_empty_installation_passes_actual_media_launch_preflight(self):
        w.initialize(self.root, [], True)
        for source in (w.SOURCE_ROOT / 'tools/roms').glob('*.py'):
            destination = self.root / 'tools/roms' / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        configure(self.engine)
        layout = self.root / 'frontend/RetroFE/layouts/Workspace'
        for name in ('layout.xml', 'splash.xml'):
            self.assertEqual(ET.parse(layout / name).getroot().tag, 'layout')
        self.assertIsNotNone(ET.parse(layout / 'splash.xml').find('statusText'))
        result = subprocess.run([sys.executable,
            str(self.root / 'tools/roms/materialize-collection-media-paths.py'), 'check-runtime'],
            cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['launch_ready'])
        result = subprocess.run([sys.executable,
            str(self.root / 'tools/roms/materialize-collection-media-paths.py'), 'ensure-runtime'],
            cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['launch_ready'])

    def test_all_declared_recipes_have_a_supported_dispatcher_and_sources(self):
        data = w.read(s.RECIPE_PATH)
        self.assertGreaterEqual(len(s.graph(data)), 40)
        supported = {'packages', 'flatpak', 'runtime', 'scaffold', 'frontend', 'quiver',
                     'build', 'archive', 'python', 'cargo', 'configuration', 'experimental', 'shortcuts'}
        for name, item in data['components'].items():
            self.assertIn(item['kind'], supported, name)
            if 'source' in item:
                w.validate_url(item['source']['url'], item['source']['allowed_hosts'])


if __name__ == '__main__':
    unittest.main()


class ReuseProvenanceTests(unittest.TestCase):
    """F05: an ELF at the expected path is not an installation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'host'
        self.root.mkdir()
        self.engine = s.Installer(self.root, {'components': {}})
        self.engine.log = (self.root / 'commands.log').open('w')
        self.addCleanup(self.engine.log.close)
        self.archive = self.root / 'pkg.tar'
        payload = Path(shutil.which('true')).read_bytes()
        with tarfile.open(self.archive, 'w') as out:
            for name, data in (('pkg/emu', payload), ('pkg/data.txt', b'data')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                out.addfile(info, io.BytesIO(data))
        self.item = {'target_dir': 'tools/emu', 'entry': 'emu', 'entry_kind': 'elf', 'format': 'tar',
                     'source': {'sha256': '0' * 64, 'size_bytes': 1}}
        patcher = patch.object(self.engine, 'acquire', return_value=self.archive)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_foreign_elf_without_receipt_is_preserved_and_refused(self):
        entry = self.root / 'tools/emu/emu'
        entry.parent.mkdir(parents=True)
        entry.write_bytes(Path(shutil.which('false')).read_bytes())
        with self.assertRaises(ValueError):
            self.engine.archive('emu', self.item)
        self.assertEqual(entry.read_bytes(), Path(shutil.which('false')).read_bytes())

    def test_fresh_install_then_reuse_without_writes(self):
        first = self.engine.archive('emu', self.item)
        self.assertEqual(first['status'], 'installed')
        before = {p: p.stat().st_mtime_ns for p in (self.root / 'tools/emu').iterdir()}
        with patch.object(self.engine, 'publish', side_effect=AssertionError('no writes on reuse')):
            second = self.engine.archive('emu', self.item)
        self.assertEqual((second['status'], second['receipt']), ('reused', 'verified'))
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in (self.root / 'tools/emu').iterdir()})

    def test_zip_archive_uses_the_native_extractor_and_preserves_customer_data(self):
        archive = self.root / 'official.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('emu', Path(shutil.which('true')).read_bytes())
            bundle.writestr('LICENSE.md', 'upstream license fixture')
        item = {**self.item, 'format': 'zip', 'member_map': {'emu': 'emu', 'LICENSE.md': 'UPSTREAM-LICENSE.md'}}
        customer = self.root / 'tools/emu/config/settings.txt'
        customer.parent.mkdir(parents=True)
        customer.write_text('customer preferences')
        with patch.object(self.engine, 'acquire', return_value=archive):
            result = self.engine.archive('emu', item)
        self.assertEqual(result['status'], 'installed')
        self.assertEqual(customer.read_text(), 'customer preferences')
        self.assertEqual((self.root / 'tools/emu/UPSTREAM-LICENSE.md').read_text(), 'upstream license fixture')
        with patch.object(self.engine, 'publish', side_effect=AssertionError('reuse should preserve files')):
            self.assertEqual(self.engine.archive('emu', item)['status'], 'reused')

    def test_changed_member_or_recipe_invalidates_reuse(self):
        self.engine.archive('emu', self.item)
        (self.root / 'tools/emu/data.txt').write_bytes(b'customized')
        with self.assertRaises(ValueError):
            self.engine.archive('emu', self.item)
        self.assertEqual((self.root / 'tools/emu/data.txt').read_bytes(), b'customized')
        self.assertFalse(self.engine.receipt_reuse('emu', dict(self.item, version='2'), 'archive')[0])

    def test_interrupted_publication_resumes_only_missing_member(self):
        self.engine.archive('emu', self.item)
        (self.root / 'tools/emu/data.txt').unlink()
        self.engine.receipt_path('emu').unlink()
        result = self.engine.archive('emu', self.item)
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.root / 'tools/emu/data.txt').read_bytes(), b'data')

    def test_build_target_without_receipt_is_not_reused(self):
        target = self.root / 'tools/built/emu'
        target.parent.mkdir(parents=True)
        shutil.copy(shutil.which('true'), target)
        with self.assertRaises(ValueError):
            self.engine.build('kyty', {'target': 'tools/built/emu'})
        self.assertTrue(target.is_file())

    def test_missing_dependency_fails(self):
        with patch.object(self.engine, 'query', return_value=(1, 'libfoo.so => not found')):
            with self.assertRaises(ValueError):
                self.engine.check_elf(Path(shutil.which('true')))
