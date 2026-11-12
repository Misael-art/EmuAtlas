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


if __name__ == '__main__':
    unittest.main()
