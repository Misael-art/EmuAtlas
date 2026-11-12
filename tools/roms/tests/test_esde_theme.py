import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import esde_theme
import romtool


class SharedThemeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'work/roms-consolidation').mkdir(parents=True)
        (self.root/'operations').mkdir()
        self.destination=self.root/'frontend/ES-DE/UserData/themes/linear-es-de'
        self.patches=[patch.object(romtool,'operation_dir',side_effect=lambda oid:self.root/'operations'/oid),
                      patch.object(romtool,'frontend_processes_running',return_value={}),
                      patch.object(romtool,'append_event'),patch.object(romtool,'rebuild_history_index'),
                      patch.object(esde_theme,'compile_theme',side_effect=self.compile)]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def compile(self,root,stage):
        theme=stage/'generated';theme.mkdir();(theme/'theme.xml').write_text('<theme/>')
        receipt={'recipe':1,'source_appimage_sha256':'fixture','binary_payloads_copied':0,
                 'canonical_art_used':{},'files':esde_theme.files_state(theme)}
        (theme/esde_theme.MARKER).write_text(json.dumps(receipt))
        return theme,receipt

    def test_plan_does_not_install_and_apply_is_idempotent_with_rollback(self):
        p=esde_theme.prepare(self.root,expected_sha256='fixture')
        self.assertEqual(p['status'],'planned');self.assertFalse(self.destination.exists())
        result=esde_theme.prepare(self.root,apply=True,expected_sha256='fixture')
        self.assertEqual(result['status'],'completed_verified')
        self.assertEqual(esde_theme.prepare(self.root,apply=True,expected_sha256='fixture')['status'],'verified')
        esde_theme.rollback(self.root,result['operation_id']);self.assertFalse(self.destination.exists())
        self.assertTrue((self.root/'operations'/result['operation_id']/'preserved-after/theme.xml').is_file())

    def test_unmanaged_and_later_user_edits_are_preserved(self):
        self.destination.mkdir(parents=True);(self.destination/'theme.xml').write_text('personal')
        with self.assertRaises(ValueError):esde_theme.prepare(self.root,apply=True,expected_sha256='fixture')
        self.assertEqual((self.destination/'theme.xml').read_text(),'personal')
        (self.destination/'theme.xml').unlink();self.destination.rmdir()
        result=esde_theme.prepare(self.root,apply=True,expected_sha256='fixture')
        (self.destination/'theme.xml').write_text('edited')
        with self.assertRaises(ValueError):esde_theme.prepare(self.root,apply=True,expected_sha256='fixture')
        with self.assertRaises(ValueError):esde_theme.rollback(self.root,result['operation_id'])
        self.assertEqual((self.destination/'theme.xml').read_text(),'edited')

    def test_bundle_pin_failure_and_active_frontend_do_not_write(self):
        with self.assertRaises(ValueError):esde_theme.prepare(self.root,apply=True,expected_sha256='wrong')
        self.assertFalse(self.destination.exists())
        with patch.object(romtool,'frontend_processes_running',return_value={'esde':[123]}):
            with self.assertRaises(ValueError):esde_theme.prepare(self.root,apply=True,expected_sha256='fixture')
        self.assertFalse(self.destination.exists())

    def test_other_theme_or_external_directory_stays_untouched(self):
        settings=self.root/'frontend/ES-DE/UserData/settings';settings.mkdir(parents=True)
        for text in ['<string name="Theme" value="personal"/>',
                     '<string name="Theme" value="linear-es-de"/><string name="UserThemeDirectory" value="/other/themes"/>']:
            (settings/'es_settings.xml').write_text(text)
            self.assertEqual(esde_theme.prepare(self.root,apply=True)['status'],'not_applicable')
            self.assertFalse(self.destination.exists())


if __name__=='__main__':unittest.main()
