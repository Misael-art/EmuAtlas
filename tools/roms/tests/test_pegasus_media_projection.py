"""Shared media, native launch projections and preservation of personal metadata."""
import base64
import contextlib
import importlib.util
import io
import json
import shutil
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.roms import romtool
from tools.distribution.workspace import safe_path


TOOL = Path(__file__).resolve().parents[3] / "tools/roms/materialize-collection-media-paths.py"



class PegasusMediaProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project with spaces"
        self.front = self.root / "frontend/RetroFE"
        self.consumer = self.root / "frontend/pegasus-frontend"
        self.consumer.mkdir(parents=True)
        config = self.front / "Config"
        config.mkdir(parents=True)
        (self.front / 'collections').mkdir()
        (config / 'media-map.json').write_text('{}')
        tools = self.root / 'tools/roms'
        (tools / 'config').mkdir(parents=True)
        (tools / 'config/roots.json').write_text('{}')
        copied = tools / TOOL.name
        shutil.copyfile(TOOL, copied)
        spec = importlib.util.spec_from_file_location('pegasus_fixture', copied)
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        (config / "platform-catalog.json").write_text(json.dumps({"systems": [
            {"id": "nes", "rom_subdir": "nes", "kind": "platform", "extensions": ["7z"]}]}))
        self.roms = self.root / "roms/nes"
        self.roms.mkdir(parents=True)
        (self.roms / "Game [Rev 1].7z").write_bytes(b"rom fixture")
        self.metadata = self.roms / "metadata.pegasus.txt"
        self.metadata.write_text('collection: Nintendo Entertainment System\nshortname: nes\nlaunch: preserve-personal-command "{file.path}"\n')
        directory = self.root / "media/nes/cover-front"
        directory.mkdir(parents=True)
        self.cover = directory / "Game [Rev 1].png"
        self.cover.write_bytes(b"canonical media fixture")
        (directory / "Game [Rev 1] (Alt 2).png").write_bytes(b"alternative fixture")
        self.dirs = self.consumer / "game_dirs.txt"
        self.dirs.write_text(str(self.root.with_name("Emulation") / "roms/nes") + "\n/personal/location\n")
        self.originals = {p: p.read_bytes() for p in [self.metadata, self.cover, self.dirs]}
        self.patcher = patch.multiple(self.tool, PROJECT_ROOT=self.root, ROOT=self.front,
                                     PLATFORM_CATALOG_PATH=config / "platform-catalog.json")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        for name in ["retrofe_processes", "pegasus_processes"]:
            patcher = patch.object(self.tool, name, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = self.front / "Manifests/pegasus-media-fixture.json"

    def test_plan_apply_verify_and_rollback_preserve_payloads_and_personal_commands(self):
        plan = self.tool.make_pegasus_plan(self.path)
        self.assertEqual(plan["summary"]["games_with_media"], 1)
        self.assertEqual(plan["summary"]["canonical_media_references"], 1)
        self.tool.apply_plan(self.path, plan)
        projection = self.consumer / "metafiles/canonical-media.metadata.pegasus.txt"
        text = projection.read_text()
        self.assertIn("assets.box_front: ../../../media/nes/cover-front/Game [Rev 1].png", text)
        self.assertIn("file: ../../../roms/nes/Game [Rev 1].7z", text)
        self.assertNotIn("Alt 2", text)
        self.assertIn("/personal/location", self.dirs.read_text())
        self.assertEqual(self.tool.verify_plan(self.path, plan, {})["failed_files"], 0)
        self.assertEqual(self.metadata.read_bytes(), self.originals[self.metadata])
        self.tool.rollback_plan(self.path, plan)
        self.assertEqual({p: p.read_bytes() for p in self.originals}, self.originals)
        self.assertFalse(projection.exists())
        self.assertFalse(projection.parent.exists())

    def test_media_or_metadata_changes_block_before_any_configuration_write(self):
        for target in [self.cover, self.metadata]:
            with self.subTest(target=target):
                plan = self.tool.make_pegasus_plan(self.path)
                target.write_bytes(b"changed after plan")
                with self.assertRaises(ValueError):
                    self.tool.apply_plan(self.path, plan)
                self.assertEqual(self.dirs.read_bytes(), self.originals[self.dirs])
                target.write_bytes(self.originals[target])
                self.path.unlink()

    def test_unmanaged_projection_and_symlink_are_preserved(self):
        projection = self.consumer / "metafiles/canonical-media.metadata.pegasus.txt"
        projection.parent.mkdir()
        projection.write_text("personal metadata")
        with self.assertRaises(ValueError):
            self.tool.make_pegasus_plan(self.path)
        self.assertEqual(projection.read_text(), "personal metadata")
        projection.unlink()
        projection.symlink_to(self.metadata)
        with self.assertRaises(ValueError):
            self.tool.make_pegasus_plan(self.path)

    def test_consumer_target_allowlist_rejects_unrelated_files(self):
        plan = self.tool.make_pegasus_plan(self.path)
        plan["files"][0]["path"] = "../../outside.txt"
        with self.assertRaises(ValueError):
            self.tool.apply_plan(self.path, plan)
        self.assertEqual(self.dirs.read_bytes(), self.originals[self.dirs])

    def test_changed_projection_blocks_rollback(self):
        plan = self.tool.make_pegasus_plan(self.path)
        self.tool.apply_plan(self.path, plan)
        projection = self.consumer / "metafiles/canonical-media.metadata.pegasus.txt"
        projection.write_text(projection.read_text() + "# user change\n")
        with self.assertRaises(ValueError):
            self.tool.rollback_plan(self.path, plan)
        self.assertTrue(projection.exists())

    def portable_data(self, personal=False):
        config = self.consumer / 'config'
        config.mkdir()
        self.dirs.rename(config / 'game_dirs.txt')
        self.dirs = config / 'game_dirs.txt'
        profiles = self.front / 'Config/profiles'
        profiles.mkdir()
        self.profile = profiles / 'host-linux.json'
        self.profile.write_text(json.dumps({'collections': {'Native NES': {
            'rom_subdir': 'nes', 'adapter': 'retroarch', 'core': 'mesen_libretro.so', 'extensions': '7z,cue,bin,m3u'}}}))
        if not personal:
            old_core = self.root.parent / '.var/app/org.libretro.RetroArch/config/retroarch/cores/old_libretro.so'
            self.metadata.write_text('collection: Nintendo Entertainment System\nshortname: nes\n'
                                     f'launch: launch:/usr/bin/flatpak run org.libretro.RetroArch -L {old_core} "{{file.path}}"\n')
        return config

    def test_portable_projection_uses_native_profile_and_relative_directories_with_reversible_retirement(self):
        config = self.portable_data()
        missing = self.root.with_name('Emulation') / 'roms/absent'
        with self.dirs.open('a') as stream:
            stream.write(str(missing) + '\n')
        before = self.dirs.read_bytes()
        plan = self.tool.make_pegasus_plan(self.path, portable_runtime=True)
        self.assertEqual(plan['summary']['games_with_launcher'], 1)
        self.assertEqual(plan['summary']['retired_missing_managed_references'], 1)
        self.tool.apply_plan(self.path, plan)
        text = (config / 'metafiles/canonical-media.metadata.pegasus.txt').read_text()
        self.assertIn('--collection "Native NES" --item "{file.path}"', text)
        self.assertIn('{env.EMUATLAS_ROOT}', text)
        self.assertIn('assets.box_front: ../../../../media/nes/cover-front/Game [Rev 1].png', text)
        self.assertEqual(self.dirs.read_text(), '../../roms/nes\n/personal/location\n')
        self.assertEqual(self.tool.verify_plan(self.path, plan, {})['failed_files'], 0)
        self.tool.rollback_plan(self.path, plan)
        self.assertEqual(self.dirs.read_bytes(), before)

    def test_portable_profile_or_launch_entry_changes_block_before_publication(self):
        self.portable_data()
        for target in [self.profile, self.roms / 'Game [Rev 1].7z']:
            with self.subTest(target=target):
                plan = self.tool.make_pegasus_plan(self.path, portable_runtime=True)
                original = target.read_bytes()
                target.write_bytes(original + b' ')
                with self.assertRaises(ValueError):
                    self.tool.apply_plan(self.path, plan)
                target.write_bytes(original)
                self.path.unlink()

    def test_portable_projection_preserves_personal_launch_commands(self):
        config = self.portable_data(personal=True)
        plan = self.tool.make_pegasus_plan(self.path, portable_runtime=True)
        self.assertEqual(plan['summary']['games_with_launcher'], 0)
        self.assertTrue(any('pessoal' in item['reason'] for item in plan['blocked']))
        self.tool.apply_plan(self.path, plan)
        projection = (config / 'metafiles/canonical-media.metadata.pegasus.txt').read_text()
        self.assertNotIn('launch:', projection)
        self.assertIn('preserve-personal-command', self.metadata.read_text())

    def test_portable_projection_includes_entries_without_media_and_excludes_descriptor_dependencies(self):
        config = self.portable_data()
        catalog = self.front / 'Config/platform-catalog.json'
        data = json.loads(catalog.read_text())
        data['systems'][0]['extensions'] = ['7z', 'cue', 'bin', 'm3u']
        catalog.write_text(json.dumps(data))
        (self.roms / 'No media.7z').write_bytes(b'cartridge')
        (self.roms / 'Track 01.bin').write_bytes(b'track')
        (self.roms / 'Disc.cue').write_text('FILE "Track 01.bin" BINARY\n')
        (self.roms / 'Set.m3u').write_text('Disc.cue\n')
        plan = self.tool.make_pegasus_plan(self.path, portable_runtime=True)
        self.tool.apply_plan(self.path, plan)
        text = (config / 'metafiles/canonical-media.metadata.pegasus.txt').read_text()
        self.assertIn('game: No media', text)
        self.assertIn('game: Set', text)
        self.assertNotIn('game: Track 01', text)
        self.assertNotIn('game: Disc\n', text)
        self.assertIn('ignore-file: ../../../../roms/nes/Track 01.bin', text)
        self.assertIn('ignore-file: ../../../../roms/nes/Disc.cue', text)

    def test_duplicate_userdata_roots_block_the_portable_plan(self):
        self.portable_data()
        (self.consumer / 'game_dirs.txt').write_text('duplicate')
        with self.assertRaisesRegex(ValueError, 'duplicados'):
            self.tool.make_pegasus_plan(self.path, portable_runtime=True)

    def launcher_fixture(self):
        app = self.consumer / 'pegasus-fe'
        app.write_bytes(b'native executable fixture')
        engine = SimpleNamespace(root=self.root, config={'components': {'pegasus': {
            'target_dir': 'frontend/pegasus-frontend', 'entry': 'pegasus-fe', 'version': 'fixture'}}},
            receipt_reuse=lambda *args: (True, 'fixture receipt'), check_elf=lambda path: None)
        return patch.dict(sys.modules, {'setup': SimpleNamespace(Installer=lambda root: engine),
                                      'workspace': SimpleNamespace(safe_path=safe_path)})

    def test_launcher_check_is_read_only_and_normal_start_uses_portable_native_session(self):
        self.portable_data()
        with self.launcher_fixture(), patch.object(self.tool.subprocess, 'call', return_value=23) as run:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.tool.launch_pegasus(['--', '--check']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'ready')
            run.assert_not_called()
            self.assertFalse((self.root / 'outputs').exists())
            self.assertEqual(self.tool.launch_pegasus([]), 23)
        command = run.call_args.args[0]
        self.assertIn('session-run', command)
        self.assertIn('--portable', command)
        self.assertEqual(run.call_args.kwargs['cwd'], self.consumer)
        self.assertEqual(run.call_args.kwargs['env']['EMUATLAS_ROOT'], str(self.root))
        self.assertEqual(run.call_args.kwargs['env']['EMUATLAS_PYTHON'], sys.executable)
        self.assertEqual(run.call_args.kwargs['env']['QT_QPA_PLATFORM'], 'xcb')
        self.assertEqual(run.call_args.kwargs['env']['XDG_CACHE_HOME'], str(self.consumer / 'config/cache'))

    def test_launcher_preserves_legacy_data_and_allows_native_version_probe(self):
        with self.launcher_fixture(), patch.object(self.tool.subprocess, 'call', return_value=0) as run:
            with self.assertRaisesRegex(ValueError, 'legados'):
                self.tool.launch_pegasus(['--check'])
            run.assert_not_called()
            self.assertEqual(self.dirs.read_bytes(), self.originals[self.dirs])
            self.assertEqual(self.tool.launch_pegasus(['--version']), 0)
            self.assertEqual(run.call_args.args[0], [str(self.consumer / 'pegasus-fe'), '--portable', '--version'])
            self.assertFalse((self.root / 'outputs').exists())

    def test_launcher_blocks_missing_media_reference_before_opening_the_frontend(self):
        config = self.portable_data()
        projection = config / 'metafiles/canonical-media.metadata.pegasus.txt'
        projection.parent.mkdir()
        projection.write_text('# emulation-managed canonical-media v1\nassets.box_front: ../../../../media/nes/missing.png\n')
        with self.launcher_fixture(), patch.object(self.tool.subprocess, 'call') as run:
            with self.assertRaisesRegex(ValueError, 'Referência Pegasus'):
                self.tool.launch_pegasus([])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
