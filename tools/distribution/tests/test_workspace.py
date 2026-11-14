import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workspace as w
import release as r


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'customer space'

    def test_plan_has_no_side_effect_and_repeat_install_preserves_mtime(self):
        p = w.initialize(self.root, ['mastersystem'], False)
        self.assertEqual(p['status'], 'ready')
        self.assertFalse(self.root.exists())
        w.initialize(self.root, ['mastersystem'], True)
        cfg = self.root / 'tools/roms/config/policy.json'
        before = cfg.read_bytes(), cfg.stat().st_mtime_ns
        (self.root / 'roms/mastersystem/customer.sms').write_bytes(b'customer content')
        result = w.initialize(self.root, ['mastersystem'], True)
        self.assertEqual(result['status'], 'already_initialized')
        self.assertEqual((cfg.read_bytes(), cfg.stat().st_mtime_ns), before)
        self.assertEqual((self.root / 'roms/mastersystem/customer.sms').read_bytes(), b'customer content')

    def test_existing_library_without_marker_and_custom_config_are_preserved(self):
        p = self.root / 'roms/mastersystem/existing.sms'
        p.parent.mkdir(parents=True)
        p.write_bytes(b'original')
        with self.assertRaises(ValueError):
            w.initialize(self.root, ['mastersystem'], True)
        self.assertEqual(p.read_bytes(), b'original')
        p.unlink()
        p.parent.rmdir()
        w.initialize(self.root, ['mastersystem'], True)
        cfg = self.root / 'tools/roms/config/policy.json'
        cfg.write_text('{"custom":true}')
        with self.assertRaises(ValueError):
            w.initialize(self.root, ['mastersystem'], True)
        self.assertEqual(cfg.read_text(), '{"custom":true}')

    def test_installed_workspace_can_move_without_old_root_in_config(self):
        w.initialize(self.root, ['mastersystem'], True)
        moved = self.root.with_name('relocated space')
        self.root.rename(moved)
        self.assertEqual(w.initialize(moved, ['mastersystem'], True)['status'], 'already_initialized')
        policy = (moved / 'tools/roms/config/policy.json').read_text()
        self.assertNotIn(str(self.root), policy)
        self.assertIn('${WORKSPACE}', policy)

    def test_traversal_unknown_platform_and_ancestor_symlinks_are_rejected(self):
        self.root.mkdir()
        for value in ['../outside', '/etc/passwd', 'media\\escape']:
            with self.assertRaises(ValueError):
                w.safe_path(self.root, value)
        with self.assertRaises(ValueError):
            w.plan(self.root, ['../anything'])
        (self.root / 'media').symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            w.plan(self.root, [])

    def source(self, payload):
        lock = self.root.parent / 'sources.json'
        lock.write_text(json.dumps({'components': {'fixture': {
            'kind': 'media', 'version': 'fixture', 'url': 'https://media.example.test/cover.png',
            'allowed_hosts': ['media.example.test'], 'source_page': 'https://media.example.test/license',
            'license_review': 'Synthetic test bytes owned by test fixture', 'license_id': 'fixture',
            'approved_for_download': True, 'target': 'media/mastersystem/cover-front/Fixture.png',
            'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}}}))
        return lock

    def response(self, data):
        class Response(io.BytesIO):
            def geturl(self):
                return 'https://media.example.test/cover.png'
        opener = unittest.mock.Mock()
        opener.open.return_value = Response(data)
        return patch.object(w, 'build_opener', return_value=opener)

    def test_download_verifies_then_is_noop_and_never_replaces_user_file(self):
        w.initialize(self.root, ['mastersystem'], True)
        payload = b'synthetic media test bytes'
        source = self.source(payload)
        with self.response(payload):
            self.assertEqual(w.fetch(self.root, 'fixture', True, source)['status'], 'downloaded_verified')
        target = self.root / 'media/mastersystem/cover-front/Fixture.png'
        before = target.read_bytes(), target.stat().st_mtime_ns
        with patch.object(w, 'build_opener', side_effect=AssertionError('no second network request')):
            self.assertEqual(w.fetch(self.root, 'fixture', True, source)['status'], 'already_verified')
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
        target.write_bytes(b'new customer art')
        with self.assertRaises(ValueError):
            w.fetch(self.root, 'fixture', True, source)
        self.assertEqual(target.read_bytes(), b'new customer art')

    def test_corrupt_or_oversize_download_never_publishes_payload(self):
        w.initialize(self.root, ['mastersystem'], True)
        source = self.source(b'expected')
        for data in [b'wrong!!!', b'more bytes than expected']:
            with self.response(data), self.assertRaises(ValueError):
                w.fetch(self.root, 'fixture', True, source)
            self.assertFalse((self.root / 'media/mastersystem/cover-front/Fixture.png').exists())
            self.assertFalse(list(self.root.rglob('.acquire-*')))

    def test_unapproved_source_and_redirect_to_other_host_are_blocked(self):
        with self.assertRaises(ValueError):
            w.source_component('example-owned-cover', w.HERE / 'examples/media-source-lock.json')
        for url in ['http://media.example.test/a', 'https://evil.example.test/a', 'https://user:secret@media.example.test/a']:
            with self.assertRaises(ValueError):
                w.validate_url(url, ['media.example.test'])

    def test_release_rejects_symlinks_payloads_and_personal_paths(self):
        self.root.mkdir()
        p = self.root / 'file.py'
        p.write_text('print("safe")\n')
        entry = {'source': 'file.py', 'target': 'file.py'}
        self.assertTrue(r.inspect_source(self.root, entry))
        p.write_text('private = "' + '/' + 'home/customer/library"\n')
        with self.assertRaises(ValueError):
            r.inspect_source(self.root, entry)
        p.unlink()
        p.symlink_to(self.root.parent / 'external')
        with self.assertRaises(ValueError):
            r.inspect_source(self.root, entry)
        with self.assertRaises(ValueError):
            r.inspect_source(self.root, {'source': 'roms/game.sms', 'target': 'file.py'})

    def test_build_is_deterministic_and_tampering_is_detected(self):
        first = r.build(self.root / 'one', True)
        second = r.build(self.root / 'two', True)
        self.assertEqual(first['sha256'], second['sha256'])
        stage = Path(first['directory'])
        # ES-DE's Linux staticpath invokes this shebang script directly.
        self.assertTrue(os.access(stage / 'frontend/RetroFE/Tools/launchers/emulators/host-emulator-launcher.py', os.X_OK))
        self.assertEqual(r.verify(stage)['status'], 'verified')
        probe = subprocess.run([sys.executable, str(stage / 'tools/distribution/release.py'),
                                'verify', '--root', str(stage)], capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        (stage / 'README.md').write_text('tampered')
        with self.assertRaises(ValueError):
            r.verify(stage)


class SourceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'installed space'
        self.incoming = self.base / 'incoming space'
        self.old = {'README.md': b'old documentation', 'tools/current.py': b'old code',
                    'tools/removed.py': b'old retained code'}
        self.new = {'README.md': b'new documentation', 'tools/current.py': b'new code',
                    'tools/added.py': b'added code'}
        self.make_release(self.root, self.old, '1')
        self.make_release(self.incoming, self.new, '2')
        customer = self.root / 'media/nes/cover-front/customer.png'
        customer.parent.mkdir(parents=True)
        customer.write_bytes(b'customer payload')
        self.customer = customer
        guard = patch('runtime_install._library_guard')
        self.guard = guard.start()
        self.addCleanup(guard.stop)

    def make_release(self, root, files, version):
        rows = []
        for relative, data in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
            rows.append({'target': relative, **r.source_state(root, relative)})
        (root / 'release-manifest.json').write_text(json.dumps({
            'schema_version': 1, 'product': 'fixture', 'version': version, 'files': rows}))

    def prepare(self):
        return r.plan_update(self.root, self.incoming, True)['operation_id']

    def test_source_update_round_trip_preserves_library_and_retains_removed_and_new_code(self):
        self.assertEqual(r.plan_update(self.root, self.incoming)['status'], 'planned')
        self.assertFalse((self.root / 'outputs').exists())
        ident = self.prepare()
        op = self.root / 'outputs/installation/updates' / ident
        self.assertEqual((self.root / 'tools/current.py').read_bytes(), self.old['tools/current.py'])
        r.execute_update(self.root, ident, 'apply', True)
        self.assertEqual(r.execute_update(self.root, ident, 'verify')['status'], 'completed_verified')
        for relative, data in self.new.items():
            self.assertEqual((self.root / relative).read_bytes(), data)
        self.assertEqual((op / 'rollback/removed/tools/removed.py').read_bytes(), self.old['tools/removed.py'])
        self.assertEqual(self.customer.read_bytes(), b'customer payload')
        r.execute_update(self.root, ident, 'rollback', True)
        for relative, data in self.old.items():
            self.assertEqual((self.root / relative).read_bytes(), data)
        self.assertEqual((op / 'rollback/applied-files/tools/added.py').read_bytes(), self.new['tools/added.py'])
        self.assertFalse((self.root / 'tools/added.py').exists())
        self.assertEqual(self.customer.read_bytes(), b'customer payload')

    def test_customized_source_blocks_before_first_mutation(self):
        ident = self.prepare()
        path = self.root / 'tools/current.py'
        path.write_bytes(b'customer customization')
        with self.assertRaises(ValueError):
            r.execute_update(self.root, ident, 'apply', True)
        self.assertEqual((self.root / 'README.md').read_bytes(), self.old['README.md'])
        self.assertEqual(path.read_bytes(), b'customer customization')
        with self.assertRaises(ValueError):
            r.plan_update(self.root, self.incoming, True)

    def test_post_publication_interruption_resumes_and_retains_rollback(self):
        ident = self.prepare()
        write = r.romtool.atomic_write_bytes
        def interrupt_after_file(path, data, **kwargs):
            write(path, data, **kwargs)
            if path == self.root / 'tools/current.py':
                raise OSError('fixture interruption after durable publication')
        with patch.object(r.romtool, 'atomic_write_bytes', side_effect=interrupt_after_file):
            with self.assertRaises(OSError):
                r.execute_update(self.root, ident, 'apply', True)
        r.execute_update(self.root, ident, 'apply', True)
        self.assertEqual(r.execute_update(self.root, ident, 'verify')['status'], 'completed_verified')
        r.execute_update(self.root, ident, 'rollback', True)
        self.assertEqual((self.root / 'tools/current.py').read_bytes(), self.old['tools/current.py'])

    def test_source_changed_after_batch_preflight_is_preserved(self):
        ident = self.prepare()
        write = r.romtool.atomic_write_bytes
        def change_later_source(path, data, **kwargs):
            write(path, data, **kwargs)
            if path == self.root / 'README.md':
                (self.root / 'tools/current.py').write_bytes(b'concurrent edit')
        with patch.object(r.romtool, 'atomic_write_bytes', side_effect=change_later_source):
            with self.assertRaisesRegex(ValueError, 'mudou durante'):
                r.execute_update(self.root, ident, 'apply', True)
        self.assertEqual((self.root / 'tools/current.py').read_bytes(), b'concurrent edit')

    def test_removal_interruption_has_a_durable_owner_and_resumes(self):
        ident = self.prepare()
        sync = r.romtool.sync_directory_chain
        def interrupt_after_move(directory):
            if directory.as_posix().endswith('/rollback/removed/tools'):
                raise OSError('fixture interruption after rename')
            sync(directory)
        with patch.object(r.romtool, 'sync_directory_chain', side_effect=interrupt_after_move):
            with self.assertRaises(OSError):
                r.execute_update(self.root, ident, 'apply', True)
        r.execute_update(self.root, ident, 'apply', True)
        r.execute_update(self.root, ident, 'verify')
        r.execute_update(self.root, ident, 'rollback', True)
        self.assertEqual((self.root / 'tools/removed.py').read_bytes(), self.old['tools/removed.py'])

    def test_later_customization_blocks_rollback_and_other_source_files_are_preserved(self):
        ident = self.prepare()
        r.execute_update(self.root, ident, 'apply', True)
        (self.root / 'tools/current.py').write_bytes(b'later edit')
        with self.assertRaises(ValueError):
            r.execute_update(self.root, ident, 'rollback', True)
        self.assertEqual((self.root / 'README.md').read_bytes(), self.new['README.md'])
        self.assertEqual((self.root / 'tools/current.py').read_bytes(), b'later edit')

    def test_changed_staging_or_active_runtime_blocks_apply(self):
        ident = self.prepare()
        self.guard.side_effect = ValueError('runtime active')
        with self.assertRaisesRegex(ValueError, 'runtime active'):
            r.execute_update(self.root, ident, 'apply', True)
        self.guard.side_effect = None
        manifest = w.read(self.root / 'outputs/installation/updates' / ident / 'manifest.json')
        (self.root / manifest['stage'] / 'README.md').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            r.execute_update(self.root, ident, 'apply', True)
        self.assertEqual((self.root / 'README.md').read_bytes(), self.old['README.md'])

    def test_payload_in_incoming_manifest_is_rejected(self):
        forbidden = self.base / 'payload release'
        self.make_release(forbidden, {'roms/game.py': b'payload'}, '2')
        with self.assertRaisesRegex(ValueError, 'Fonte protegida'):
            r.plan_update(self.root, forbidden, True)
        self.assertFalse((self.root / 'outputs').exists())

    def test_duplicate_target_in_incoming_manifest_is_rejected(self):
        path = self.incoming / 'release-manifest.json'
        manifest = w.read(path)
        manifest['files'].append(manifest['files'][0])
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'repetido'):
            r.plan_update(self.root, self.incoming, True)

    def test_changed_operation_cannot_publish_a_library_payload(self):
        ident = self.prepare()
        manifest_path = self.root / 'outputs/installation/updates' / ident / 'manifest.json'
        manifest = w.read(manifest_path)
        stage = self.root / manifest['stage']
        payload = stage / 'roms/customer.py'
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b'payload fixture')
        manifest['changes'].append({'path': 'roms/customer.py', 'before': None,
                                    'after': r.source_state(stage, 'roms/customer.py'), 'status': 'prepared'})
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Fonte protegida'):
            r.execute_update(self.root, ident, 'apply', True)
        self.assertFalse((self.root / 'roms/customer.py').exists())
        self.assertEqual((self.root / 'README.md').read_bytes(), self.old['README.md'])

    def test_incoming_release_in_project_work_can_be_staged(self):
        incoming = self.root / 'work/release-input'
        self.make_release(incoming, self.new, '2')
        ident = r.plan_update(self.root, incoming, True)['operation_id']
        r.execute_update(self.root, ident, 'apply', True)
        self.assertEqual(r.execute_update(self.root, ident, 'verify')['status'], 'completed_verified')

    def test_update_cli_can_bootstrap_an_installed_release_without_the_new_tool(self):
        ident = self.prepare()
        for action in ['apply', 'verify', 'rollback']:
            arguments = ['release.py', 'update-' + action, '--root', str(self.root), '--operation-id', ident]
            if action != 'verify':
                arguments.append('--apply')
            with patch.object(sys, 'argv', arguments), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(r.main(), 0)
            self.assertEqual(json.loads(output.getvalue())['operation_id'], ident)


if __name__ == '__main__':
    unittest.main()
