import contextlib
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
import unittest
from unittest.mock import patch

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
