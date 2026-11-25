"""Real archive round trips and reference transactions; synthetic ROM fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import romtool as tool


@unittest.skipUnless(shutil.which('7z') and shutil.which('rg'),'7z and rg required')
class ArchiveNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.tools=self.root/'tools/roms';config=self.tools/'config';config.mkdir(parents=True)
        real=Path(tool.__file__).parent
        for name in ['compression-profiles.json','variant-policy.json']:
            shutil.copy2(real/'config'/name,config/name)
        shutil.copy2(real.parents[1]/'tools/distribution/templates/tools/roms/config/policy.json',
                     config/'policy.json')
        self.roms=self.root/'roms';self.platform=self.roms/'mastersystem';self.platform.mkdir(parents=True)
        self.fe=self.root/'frontend/RetroFE';collection=self.fe/'collections/Sega Master System';collection.mkdir(parents=True)
        (collection/'settings.conf').write_text('list.path = %BASE_ITEM_PATH%/mastersystem\n')
        self.gamelist=collection/'gamelist.xml';self.gamelist.write_text('<gameList><game><path>./Game.zip</path><name>Keep</name></game></gameList>')
        (config/'roots.json').write_text(json.dumps({'canonical_rom_root':str(self.roms),'known_roots':[{'path':str(self.fe),'role':'protected_frontend'}]}))
        host=self.fe/'Config/profiles/host-linux.json';host.parent.mkdir(parents=True);host.write_text(json.dumps({'collections':{'SMS':{'rom_subdir':'mastersystem','extensions':'sms,zip,7z'}}}))
        self.output=self.root/'outputs';self.output.mkdir();self.ops=self.output/'operations';self.ops.mkdir()
        for name,value in {'PROJECT_ROOT':self.root,'TOOL_ROOT':self.tools,'OUTPUT_ROOT':self.output,'OPS_ROOT':self.ops,'POLICY_PATH':config/'policy.json','COMPRESSION_PROFILES_PATH':config/'compression-profiles.json','HOST_PROFILE_PATH':host}.items():
            p=patch.object(tool,name,value);p.start();self.addCleanup(p.stop)
        for name,value in {'ensure_allowed':lambda p:None,'append_event':lambda *a,**k:None,'rebuild_history_index':lambda:None,'retrofe_processes_running':lambda:[],'esde_processes_running':lambda:[],'compression_profile_readiness':lambda *a:('eligible','fixture runtime','test')}.items():
            p=patch.object(tool,name,value);p.start();self.addCleanup(p.stop)
        self.source=self.platform/'Game.zip';self.payload=b'ROM fixture unchanged\0'*32768
        self.make_zip({'Game.sms':self.payload,'docs/readme.txt':b'Preserve documentation','art.png':b'Preserve artwork bytes'})

    def make_zip(self,members):
        with zipfile.ZipFile(self.source,'w',compression=zipfile.ZIP_DEFLATED) as z:
            for name,data in members.items():z.writestr(name,data)

    def plan(self,normalize=True):
        return tool.command_compact_plan(argparse.Namespace(root=str(self.platform),system_id=['mastersystem'],normalize_archives=normalize))

    def apply(self,result):
        return tool.command_compact_apply(argparse.Namespace(operation_id=result['operation_id'],apply=True,allow_active_frontend=False,active_frontend_reason=''))

    def test_roundtrip_preserves_all_members_original_zip_and_gamelist(self):
        original=self.source.read_bytes();refs=self.gamelist.read_bytes();r=self.plan()
        self.assertEqual(r['candidate_count'],1)
        self.assertEqual(self.apply(r)['status'],'completed_verified')
        target=self.source.with_suffix('.7z');self.assertTrue(target.exists());self.assertFalse(self.source.exists())
        self.assertIn('./Game.7z',self.gamelist.read_text())
        manifest=json.loads(Path(r['manifest']).read_text());item=manifest['items'][0]
        self.assertEqual(Path(item['preserved_original']['path']).read_bytes(),original)
        self.assertEqual(item['reclaimed_bytes'],0)
        self.assertEqual(item['net_disk_bytes_freed'],-target.stat().st_size)
        self.assertEqual(tool.command_compact_verify(argparse.Namespace(operation_id=r['operation_id']))['status'],'completed_verified')
        restored=tool.command_compact_rollback(argparse.Namespace(operation_id=r['operation_id'],rollback=True))
        self.assertEqual(restored['status'],'rolled_back');self.assertEqual(self.source.read_bytes(),original);self.assertEqual(self.gamelist.read_bytes(),refs)
        self.assertFalse(target.exists())

    def test_multiple_roms_and_executable_extras_are_preserved(self):
        for members in [{'A.sms':self.payload,'B.sms':self.payload},{'Game.sms':self.payload,'helper.exe':b'No execution'}]:
            self.make_zip(members);r=self.plan();self.assertEqual(r['candidate_count'],0);self.assertTrue(self.source.exists())

    def test_archive_normalization_can_be_explicitly_disabled(self):
        self.assertEqual(self.plan(False)['candidate_count'],0)

    def test_stale_reference_rejected_before_rom_changes(self):
        r=self.plan();self.gamelist.write_text('<gameList/>')
        with self.assertRaisesRegex(ValueError,'Referência mudou'):self.apply(r)
        self.assertTrue(self.source.exists());self.assertFalse(self.source.with_suffix('.7z').exists())

    def test_rollback_rejects_changed_reference_without_losing_original(self):
        r=self.plan();self.apply(r);self.gamelist.write_text('<gameList/>')
        result=tool.command_compact_rollback(argparse.Namespace(operation_id=r['operation_id'],rollback=True))
        self.assertEqual(result['status'],'rollback_failed');self.assertTrue(self.source.with_suffix('.7z').exists())
        item=json.loads(Path(r['manifest']).read_text())['items'][0];self.assertTrue(Path(item['preserved_original']['path']).exists())

    def test_same_destination_chooses_bundle_only_for_equal_rom_bytes(self):
        raw=self.platform/'Game.sms';raw.write_bytes(self.payload);r=self.plan();self.assertEqual(r['candidate_count'],1)
        self.assertTrue(json.loads(Path(r['manifest']).read_text())['items'][0]['source'].endswith('.zip'))
        raw.write_bytes(b'Other ROM bytes');self.assertEqual(self.plan()['candidate_count'],0)

    def test_logical_audit_matches_payload_across_encodings_not_names(self):
        self.make_zip({'Game.sms':self.payload});(self.platform/'Different name.sms').write_bytes(self.payload)
        r=tool.logical_duplicate_audit(self.platform,self.output,'mastersystem')
        self.assertEqual(r['evidence']['duplicate_groups'],1)
        self.assertEqual(sum(x['status']=='same_payload_review_only' for x in r['items']),2)
        self.assertTrue(self.source.exists())

    def test_encrypted_zip_is_reported_as_password_required_with_unverified_crc(self):
        patch_file = self.platform / 'Patch.bps'
        patch_file.write_bytes(b'patch fixture')
        encrypted = self.platform / 'Protected.zip'
        subprocess.run(['7z', 'a', '-tzip', '-pfixture-password', str(encrypted), str(patch_file)],
                       capture_output=True, check=True)
        original = encrypted.read_bytes()
        with self.assertRaises(tool.ArchivePasswordRequired):
            tool.archive_members(encrypted)
        result = tool.verify_one(encrypted, list_archive_contents=True)
        self.assertEqual(result['status'], 'encrypted_archive_password_required')
        self.assertEqual(result['encrypted_member_count'], 1)
        self.assertFalse(result['full_crc_verified'])
        self.assertEqual(result['archive_members'][0]['path'], 'Patch.bps')
        self.assertEqual(encrypted.read_bytes(), original)

    def test_zip_member_listing_rejects_windows_drive_paths(self):
        self.make_zip({'C:/escape.sms': b'fixture'})
        with self.assertRaisesRegex(ValueError, 'inseguro'):
            tool.archive_members(self.source)

    def existing_7z(self, members=None):
        folder=self.root/'staging';folder.mkdir()
        for name,data in (members or {'Game.sms':self.payload}).items():
            f=folder/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(data)
        target=self.platform/'Game.7z'
        subprocess.run(['7z','a','-t7z',str(target),'.'],cwd=folder,capture_output=True,check=True)
        return target

    def test_existing_equal_7z_reused_and_duplicate_zip_restored_without_touching_peer(self):
        self.make_zip({'Game.sms':self.payload});original=self.source.read_bytes();refs=self.gamelist.read_bytes()
        target=self.existing_7z();peer=target.read_bytes();r=self.plan()
        self.assertEqual(r['candidate_count'],1)
        result=self.apply(r);self.assertEqual(result['status'],'completed_verified')
        item=json.loads(Path(r['manifest']).read_text())['items'][0]
        self.assertTrue(item['reuse_existing_destination']);self.assertEqual(item['net_disk_bytes_freed'],0)
        self.assertFalse(self.source.exists());self.assertEqual(target.read_bytes(),peer)
        self.assertEqual(Path(item['preserved_original']['path']).read_bytes(),original)
        self.assertEqual(tool.command_compact_verify(argparse.Namespace(operation_id=r['operation_id']))['status'],'completed_verified')
        result=tool.command_compact_rollback(argparse.Namespace(operation_id=r['operation_id'],rollback=True))
        self.assertEqual(result['status'],'rolled_back');self.assertEqual(target.read_bytes(),peer)
        self.assertEqual(self.source.read_bytes(),original);self.assertEqual(self.gamelist.read_bytes(),refs)

    def test_existing_different_7z_or_missing_bundle_extras_is_blocked(self):
        target=self.existing_7z({'Game.sms':b'Other bytes'})
        self.assertEqual(self.plan()['candidate_count'],0);target.unlink()
        (self.root/'staging/Game.sms').write_bytes(self.payload)
        # A matching ROM alone must not silently subsume a bundle with documentation.
        subprocess.run(['7z','a','-t7z',str(self.platform/'Game.7z'),'.'],cwd=self.root/'staging',capture_output=True,check=True)
        self.assertEqual(self.plan()['candidate_count'],0);self.assertTrue(self.source.exists())

    def test_changed_existing_peer_blocks_before_quarantining_source(self):
        self.make_zip({'Game.sms':self.payload});target=self.existing_7z();r=self.plan();target.write_bytes(b'Changed peer')
        result=self.apply(r);self.assertEqual(result['status'],'failed_partial');self.assertTrue(self.source.exists())

    def test_raw_duplicate_reuses_existing_7z_with_exact_rollback(self):
        self.source.unlink();self.source=self.platform/'Game.sms';self.source.write_bytes(self.payload)
        self.gamelist.write_text('<gameList><game><path>./Game.sms</path></game></gameList>')
        target=self.existing_7z();peer=target.read_bytes();r=self.plan()
        self.assertEqual(self.apply(r)['status'],'completed_verified');self.assertFalse(self.source.exists())
        result=tool.command_compact_rollback(argparse.Namespace(operation_id=r['operation_id'],rollback=True))
        self.assertEqual(result['status'],'rolled_back');self.assertEqual(self.source.read_bytes(),self.payload)
        self.assertEqual(target.read_bytes(),peer)

    def test_logical_audit_system_filter_does_not_merge_platforms(self):
        self.make_zip({'Game.sms':self.payload})
        other=self.roms/'gamegear';other.mkdir();(other/'Game.gg').write_bytes(self.payload)
        r=tool.logical_duplicate_audit(self.roms,self.output,'mastersystem')
        self.assertEqual(len(r['items']),1)
        self.assertEqual(r['evidence']['duplicate_groups'],0)

    def test_retroarch_single_extracted_file_contract_blocks_bundle_extras(self):
        host=tool.HOST_PROFILE_PATH
        doc=json.loads(host.read_text());doc['collections']['SMS'].update(adapter='retroarch',extract_extensions=['7z'])
        host.write_text(json.dumps(doc));r=self.plan();self.assertEqual(r['candidate_count'],0)
        self.assertTrue(self.source.exists())

    def test_esde_session_blocks_mutation(self):
        r=self.plan()
        with patch.object(tool,'esde_processes_running',return_value=[{'pid':42}]):
            with self.assertRaisesRegex(RuntimeError,'ES-DE'):self.apply(r)
        self.assertTrue(self.source.exists())

    def test_history_and_unreadable_binary_logs_are_not_runtime_consumers(self):
        for folder in ['Docs','Manifests']:
            p=self.fe/folder/'history.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'old':'Game.zip'}))
        log=self.fe/'../../emulator/data/RPCS3/cache/RPCS3.log.gz';log.parent.mkdir(parents=True);log.write_bytes(b'private compressed log');log.chmod(0)
        self.addCleanup(lambda:log.chmod(0o600) if log.exists() else None)
        self.assertEqual(self.plan()['candidate_count'],1)

    def test_empty_directories_and_nested_rom_path_survive_repack(self):
        self.make_zip({'nested/Game.sms':self.payload,'empty/':b''})
        r=self.plan();self.assertEqual(self.apply(r)['status'],'completed_verified')
        self.assertEqual(tool.command_compact_verify(argparse.Namespace(operation_id=r['operation_id']))['status'],'completed_verified')

    def test_same_named_reference_in_other_platform_is_preserved(self):
        p=self.fe/'collections/Other';p.mkdir();(p/'settings.conf').write_text('list.path = %BASE_ITEM_PATH%/other\n')
        other=p/'gamelist.xml';original=b'<gameList><game><path>./Game.zip</path></game></gameList>';other.write_bytes(original)
        r=self.plan();self.assertEqual(self.apply(r)['status'],'completed_verified');self.assertEqual(other.read_bytes(),original)

    def test_same_named_relative_esde_reference_in_other_platform_is_preserved(self):
        p=self.root/'frontend/ES-DE/UserData/gamelists/gamegear';p.mkdir(parents=True)
        other=p/'gamelist.xml';original=b'<gameList><game><path>./Game.zip</path></game></gameList>';other.write_bytes(original)
        r=self.plan();self.assertEqual(r['candidate_count'],1)
        self.assertEqual(self.apply(r)['status'],'completed_verified');self.assertEqual(other.read_bytes(),original)
        # Ascending paths could really reference the source platform and must block.
        self.source=self.platform/'Game2.zip';self.make_zip({'Game2.sms':self.payload})
        other.write_text('<gameList><game><path>../mastersystem/Game2.zip</path></game></gameList>')
        self.assertEqual(self.plan()['candidate_count'],0)


if __name__=='__main__':unittest.main()
