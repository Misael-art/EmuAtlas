"""Selection, format gates and independent userdata; never launch a runtime."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS=Path(__file__).resolve().parents[1]
PROJECT=TOOLS.parents[1]
sys.path.insert(0,str(TOOLS))
import emutool


class AlternativesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='runtime-alternatives-',dir=PROJECT/'work/roms-consolidation')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.fe=self.root/'frontend/RetroFE';(self.fe/'UserData').mkdir(parents=True)
        launchers=PROJECT/'frontend/RetroFE/Tools/launchers/emulators'
        sys.path.insert(0,str(launchers));self.addCleanup(lambda:sys.path.remove(str(launchers)))
        spec=importlib.util.spec_from_file_location('alternatives_launcher',launchers/'host-emulator-launcher.py')
        self.host=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.host)
        self.host.ROOT=self.fe
        actual=json.loads((PROJECT/'frontend/RetroFE/Config/profiles/host-linux.json').read_text())
        self.ps5=actual['collections']['Sony PlayStation 5']
        self.switch=actual['collections']['Nintendo Switch 2']
        self.profile=self.fe/'Config/profiles/host-linux.json';self.profile.parent.mkdir(parents=True)
        self.profile.write_text(json.dumps({'collections':{'PS5':self.ps5,'Switch2':self.switch}}))
        for name,value in [('ROOT',self.root),('RETROFE_ROOT',self.fe),('PROFILE_PATH',self.profile)]:
            p=patch.object(emutool,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(emutool,'_probe_runtime_entry',return_value={'available':True,'game_launch_tested':False});p.start();self.addCleanup(p.stop)
        p=patch.object(emutool,'_record_file_change',return_value='test-operation');p.start();self.addCleanup(p.stop)

    def selection(self,sid,payload):
        path=self.fe/f'../../emulator/data/EmulatorSelections/{sid}.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'schema_version':1,'system_id':sid,'selections':{},**payload}))
        return path

    def game(self):
        p=self.root/'roms/ps5/game';(p/'sce_sys').mkdir(parents=True)
        (p/'sce_sys/param.json').write_text('{"TITLE_ID":"PPSA02929"}')
        (p/'eboot.bin').write_bytes(b'\x7fELF')
        return p

    def test_zar_uses_explicit_preference_without_guessing_title_from_filename(self):
        p=self.root/'roms/ps5/unrelated-name.zar';p.parent.mkdir(parents=True);p.write_bytes(b'format checked by runtime')
        self.selection('ps5',{'preferred_runtime_id':'sharpemu'})
        selected,runtime,tid,exe=self.host.resolve_runtime_choice(self.ps5,p)
        self.assertEqual((runtime,tid,exe),('sharpemu','',None))
        self.assertEqual(selected['userdata_subdir'],'SharpEmu')

    def test_incompatible_keyboard_only_runner_never_receives_zar(self):
        p=self.root/'roms/ps5/game.zar';p.parent.mkdir(parents=True);p.write_bytes(b'archive')
        result=self.host.resolve_runtime_choice(self.ps5,p,forced_runtime='ps5pcem')
        self.assertIsInstance(result,str);self.assertIn('não aceita',result)

    def test_unknown_forced_runtime_is_blocked(self):
        result=self.host.resolve_runtime_choice(self.ps5,self.game(),forced_runtime='arbitrary-command')
        self.assertIsInstance(result,str);self.assertIn('não declarado',result)

    def test_per_title_choice_beats_platform_preference(self):
        self.selection('ps5',{'preferred_runtime_id':'sharpemu','selections':{'PPSA02929':'kytyps5'}})
        result=self.host.resolve_runtime_choice(self.ps5,self.game())
        self.assertEqual(result[1],'kytyps5')

    def test_setting_base_runtime_overrides_a_different_preference(self):
        path=self.selection('ps5',{'preferred_runtime_id':'sharpemu'})
        game=self.game();emutool.runtime_set('ps5',str(game),'kytyps5')
        result=json.loads(path.read_text())
        self.assertEqual(result['preferred_runtime_id'],'sharpemu')
        self.assertEqual(result['selections']['PPSA02929'],'kytyps5')
        emutool.runtime_clear('ps5',str(game))
        result=json.loads(path.read_text());self.assertEqual(result['preferred_runtime_id'],'sharpemu');self.assertEqual(result['selections'],{})

    def test_preference_plan_has_no_write_and_apply_keeps_title_choices(self):
        path=self.selection('ps5',{'selections':{'PPSA02929':'ps5pcem'}});before=path.read_bytes()
        self.assertEqual(emutool.runtime_prefer('ps5','sharpemu')['status'],'planned')
        self.assertEqual(path.read_bytes(),before)
        emutool.runtime_prefer('ps5','sharpemu',True)
        self.assertEqual(json.loads(path.read_text())['selections'],{'PPSA02929':'ps5pcem'})

    def test_corrupt_selection_is_preserved_and_blocks_launch(self):
        path=self.selection('ps5',{});path.write_text('{broken')
        result=self.host.resolve_runtime_choice(self.ps5,self.game())
        self.assertIsInstance(result,str);self.assertEqual(path.read_text(),'{broken')

    def test_non_string_preference_is_rejected_without_writing(self):
        path=self.selection('ps5',{'preferred_runtime_id':[]});before=path.read_bytes()
        self.assertIsInstance(self.host.resolve_runtime_choice(self.ps5,self.game()),str)
        with self.assertRaises(ValueError):emutool.runtime_prefer('ps5','sharpemu',True)
        self.assertEqual(path.read_bytes(),before)

    def test_switch2_selects_by_internal_npdm_id_and_preserves_shared_game(self):
        p=self.root/'roms/switch2/game/exefs';p.mkdir(parents=True);(p.parent/'romfs').mkdir()
        (p/'main').write_bytes(b'NSO0'+b'\0'*20)
        npdm=bytearray(0xa0);npdm[:4]=b'META';struct.pack_into('<I',npdm,0x70,0x80);npdm[0x80:0x84]=b'ACI0';struct.pack_into('<Q',npdm,0x90,0x0100123456789000)
        (p/'main.npdm').write_bytes(npdm)
        self.selection('switch2',{'selections':{'0100123456789000':'eglantine'}})
        chosen,runtime,tid,exe=self.host.resolve_runtime_choice(self.switch,p/'main')
        self.assertEqual((runtime,tid,exe),('eglantine','0100123456789000',p/'main'))
        self.assertEqual(chosen['userdata_subdir'],'Eglantine');self.assertEqual((p/'main.npdm').read_bytes(),npdm)
        binary=self.root/'emulator/linux/eglantine/Eglantine.AppImage';binary.parent.mkdir(parents=True);binary.write_bytes(b'not executed');binary.chmod(0o700)
        self.host.RUNTIME_OVERRIDE='eglantine'
        profile={'rom_root':str(self.root/'roms'),'bios_root':str(self.root/'bios')}
        with patch.object(self.host,'run_with_music_paused',return_value=0) as run:
            self.assertEqual(self.host.launch_command(profile,self.switch,p/'main'),0)
        self.assertIn(str(p),run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['system_id'],'switch2')


class NativeControllerSchemaTests(unittest.TestCase):
    def test_xbox_subcollection_uses_native_launcher_and_preserves_media(self):
        import emulator_controls as controls
        with tempfile.TemporaryDirectory(prefix='xbox-alias-',dir=PROJECT/'work/roms-consolidation') as directory:
            root=Path(directory);profile=root/controls.PROFILE;profile.parent.mkdir(parents=True)
            profile.write_text(json.dumps({'collections':{'Xbox':{'rom_subdir':'xbox360','adapter':'command','extensions':'iso,xex','subcollections':['Arcade']}}}))
            path=root/controls.HOST/'collections/Arcade/settings.conf';path.parent.mkdir(parents=True)
            path.write_text('launcher = Host Emulator Unavailable\nlist.extensions = iso,xex,zip,7z\nmedia.cover = personal/covers\n')
            with patch.object(controls,'esde_projection',return_value=([],{})):
                changes=controls.proposals(root,system_id='xbox360',frontends=True)
                self.assertEqual(len(changes),1)
                self.assertIn(b'launcher = Host Command',changes[0]['after'])
                self.assertIn(b'list.extensions = iso,xex\n',changes[0]['after'])
                self.assertIn(b'media.cover = personal/covers',changes[0]['after'])
                path.write_bytes(changes[0]['after'])
                self.assertEqual(controls.proposals(root,system_id='xbox360',frontends=True),[])
                path.write_text('launcher = My Custom Launcher\n')
                with self.assertRaises(ValueError):controls.proposals(root,system_id='xbox360',frontends=True)

    def test_catalog_rebuild_preserves_frontend_metadata_while_refreshing_source(self):
        path=PROJECT/'frontend/RetroFE/Tools/materializers/materialize-platform-catalog.py'
        spec=importlib.util.spec_from_file_location('catalog_preservation_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='catalog-preserve-',dir=PROJECT/'work/roms-consolidation') as directory:
            root=Path(directory);roms=root/'roms';(roms/'test').mkdir(parents=True)
            (roms/'test/systeminfo.txt').write_text('Full system name:\nTest Console\nSupported file extensions:\n.rom\n')
            catalog=root/'catalog.json';frontend={'esde':{'platform':'test','theme':'personal-theme'}}
            catalog.write_text(json.dumps({'systems':[{'id':'test','extensions':['outdated'],'frontends':frontend}]}))
            with patch.object(module,'CATALOG',catalog),patch.object(module,'PROJECT_ROOT',root):
                result=module.collect_systems(roms)
            self.assertEqual(result[0]['frontends'],frontend)
            self.assertEqual(result[0]['extensions'],['rom'])

    def test_eden_and_eglantine_clear_sdl_name_crc_and_can_reprepare_managed_values(self):
        import emulator_controls as controls
        with tempfile.TemporaryDirectory(prefix='native-guid-',dir=PROJECT/'work/roms-consolidation') as directory:
            root=Path(directory);profile=root/controls.PROFILE;profile.parent.mkdir(parents=True);profile.write_text('{"collections":{}}')
            raw={'a':'b0','b':'b1','x':'b3','y':'b2','back':'b6','start':'b7','leftstick':'b9','rightstick':'b10','leftshoulder':'b4','rightshoulder':'b5','lefttrigger':'a2','righttrigger':'a5','dpup':'h0.1','dpright':'h0.2','dpdown':'h0.4','dpleft':'h0.8'}
            pads=[{'guid':f'03001234091200000{n+1}e1000001000000','name':f'Emulation P{n+1}','mapping':raw,'index':n} for n in range(4)]
            changes=controls.proposals(root,pad=pads[0],pads=pads,routed=True,system_id='switch2',frontends=False)
            self.assertEqual(len(changes),4)
            for change in changes:
                self.assertIn(b'guid:030000000912000001e1000001000000',change['after'])
                self.assertNotIn(b'guid:030012340912',change['after'])
                path=root/change['path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(change['after'])
            self.assertEqual(controls.proposals(root,pad=pads[0],pads=pads,routed=True,system_id='switch2',frontends=False),[])


if __name__=='__main__':unittest.main()
