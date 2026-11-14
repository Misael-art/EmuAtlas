import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.roms import media_session as session, esde_media, media_optimizer


class PdfCompatibilityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'Manual [USA].pdf'
        self.path.write_bytes(b'PDF fixture')
        self.warning = (f'WARNING: {self.path} (object 2 0, offset 72): '
                        'stream keyword followed by carriage return only')
        self.rendered_pages = 2
        self.render_calls = 0
        self.optimizer = SimpleNamespace(_probe_pdf=lambda path: {'Pages': '2'},
                                         _run=self.run_validator, _text=str)

    def run_validator(self, command, **kwargs):
        if '--check' in command:
            return SimpleNamespace(returncode=3, stderr=self.warning)
        self.render_calls += 1
        for number in range(self.rendered_pages):
            Path(command[-1] + f'-{number + 1}.png').write_bytes(b'render fixture')
        return SimpleNamespace(returncode=0)

    def test_stream_eol_warning_requires_its_explicit_review_and_all_pages(self):
        api = {'media_optimizer': self.optimizer}
        with self.assertRaises(ValueError):
            session._validate(self.path, api)
        with self.assertRaises(ValueError):
            session._validate(self.path, api, pdf_linearization_review=True)
        result = session._validate(self.path, api, pdf_stream_eol_review=True)
        self.assertTrue(result['stream_eol_warnings_reviewed'])
        self.assertTrue(result['full_page_render'])
        self.assertFalse(result['linearization_warnings_reviewed'])
        self.assertEqual(result['qpdf_warning_lines'], [self.warning])
        self.assertEqual(self.render_calls, 1)
        self.assertEqual(self.path.read_bytes(), b'PDF fixture')

    def test_unreviewed_warning_alongside_known_warning_blocks(self):
        self.warning += f'\nWARNING: {self.path}: damaged cross-reference table'
        with self.assertRaises(ValueError):
            session._validate(self.path, {'media_optimizer': self.optimizer}, pdf_stream_eol_review=True)
        self.assertEqual(self.render_calls, 0)

    def test_stream_warning_for_another_file_blocks(self):
        self.warning = self.warning.replace(str(self.path), str(self.path) + '.other')
        with self.assertRaises(ValueError):
            session._validate(self.path, {'media_optimizer': self.optimizer}, pdf_stream_eol_review=True)

    def test_missing_rendered_page_blocks_even_reviewed_warning(self):
        self.rendered_pages = 1
        with self.assertRaisesRegex(ValueError, 'páginas'):
            session._validate(self.path, {'media_optimizer': self.optimizer}, pdf_stream_eol_review=True)


@unittest.skipUnless(shutil.which("magick"), "ImageMagick required")
class MediaSessionTests(unittest.TestCase):
    def setUp(self):
        self.project=Path(__file__).resolve().parents[3]
        self.temp=tempfile.TemporaryDirectory(prefix="media-session-test-",dir=self.project/"work/roms-consolidation")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.media=self.root/"media"
        self.target=self.media/"nes/cover-front/Title.png"
        self.target.parent.mkdir(parents=True)
        self.make_png(self.target,16,16,"red")
        (self.target.parent.parent/"covers").symlink_to("cover-front",target_is_directory=True)
        (self.root/"roms/nes").mkdir(parents=True)
        (self.root/"roms/nes/Title.nes").write_bytes(b"fixture")
        config=self.root/"frontend/RetroFE/Config"
        config.mkdir(parents=True)
        (config/"platform-catalog.json").write_text(json.dumps({"systems":[{"id":"nes"}]}))
        user=self.root/"frontend/ES-DE/UserData/settings"
        user.mkdir(parents=True)
        (user/"es_settings.xml").write_text(f'<string name="MediaDirectory" value="{self.media}" />\n<string name="Keep" value="user preference" />\n')
        self.events=[]
        self.api={"PROJECT_ROOT":self.root,"MEDIA_DEST_ROOT":self.media,"OPS_ROOT":self.root/"outputs/operations",
                  "TOOL_ROOT":self.project/"tools/roms","MEDIA_TYPES":{"cover-front","screenshot"},
                  "esde_media":esde_media,"media_optimizer":media_optimizer,
                  "operation_dir":lambda op:self.root/"outputs/operations"/op,
                  "now":lambda:"2026-10-26T00:00:00Z","sha256_file":lambda p:hashlib.sha256(p.read_bytes()).hexdigest(),
                  "atomic_json":lambda p,d:p.write_text(json.dumps(d)),
                  "append_event":lambda *a,**k:self.events.append((a,k)),"rebuild_history_index":lambda:None,
                  "frontend_processes_running":lambda:{"esde":[],"retrofe":[]}}
        spec=importlib.util.spec_from_file_location("session_test_helper",self.project/"frontend/ES-DE/configure-rom-directory.py")
        self.helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.helper)
        self.helper_patch=patch.object(session,"_helper",return_value=self.helper)
        self.helper_patch.start();self.addCleanup(self.helper_patch.stop)

    def make_png(self,path,w,h,color):
        path.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(["magick","-size",f"{w}x{h}",f"xc:{color}","-define","png:compression-level=0",str(path)],check=True,capture_output=True)

    def prepare(self):
        try:return session.prepare_view("esde","wine",self.api)
        except RuntimeError as exc:
            if "Operation not supported" in str(exc):self.skipTest("Filesystem does not support reflinks")
            raise

    def test_in_place_overwrite_is_isolated_and_larger_valid_image_publishes_with_rollback(self):
        original=self.target.read_bytes()
        m=self.prepare();view=self.root/m["view_relative"]
        self.make_png(view/"nes/covers/Title.png",32,32,"blue")
        self.assertEqual(self.target.read_bytes(),original)
        session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"published")
        old=self.media/m["items"][0]["previous_payload_relative"]
        self.assertEqual(old.read_bytes(),original)
        self.assertFalse(view.exists())
        session.rollback(m["operation_id"],True,self.api)
        self.assertEqual(self.target.read_bytes(),original)
        self.assertFalse(old.exists())

    def test_no_changes_leave_no_snapshot_or_view_payload(self):
        original=self.target.read_bytes();m=self.prepare();view=self.root/m["view_relative"]
        session.reconcile(m,self.api)
        self.assertEqual(m["items"],[])
        self.assertFalse(view.exists())
        self.assertFalse((self.root/m["baseline_relative"]).exists())
        self.assertEqual(self.target.read_bytes(),original)
        self.assertLess((self.api["operation_dir"](m["operation_id"])/"manifest.json").stat().st_size,10000)

    def test_duplicate_download_is_not_added_as_an_alt(self):
        m=self.prepare();view=self.root/m["view_relative"]
        raw=(view/"nes/cover-front/Title.png").read_bytes()
        (view/"nes/cover-front/Title.png").write_bytes(raw)
        session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"deduplicated")
        self.assertEqual(len(list(self.target.parent.glob("*.png"))),1)

    def test_existing_track_media_cannot_receive_a_new_scrape(self):
        track = 'Title (Track 1)'
        (self.root / 'roms/nes' / (track + '.bin')).write_bytes(b'track')
        (self.root / 'roms/nes/Disc.cue').write_text('FILE "Title (Track 1).bin" BINARY\n')
        image = self.media / 'nes/cover-front' / (track + '.png')
        self.make_png(image, 4, 4, 'red')
        original = image.read_bytes()
        manifest = self.prepare()
        view = self.root / manifest['view_relative']
        self.make_png(view / 'nes/cover-front' / (track + '.png'), 8, 8, 'blue')
        session.reconcile(manifest, self.api)
        self.assertEqual(image.read_bytes(), original)
        self.assertTrue(any(item['status'] == 'blocked' for item in manifest['items']))

    def test_bad_download_is_preserved_but_unchanged_clones_are_cleaned(self):
        original=self.target.read_bytes();m=self.prepare();view=self.root/m["view_relative"]
        (view/"nes/cover-front/Title.png").write_bytes(b"not an image")
        session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"blocked")
        self.assertEqual((self.root/m["items"][0]["preserved_relative"]).read_bytes(),b"not an image")
        self.assertEqual(self.target.read_bytes(),original)
        self.assertFalse(view.exists())

    def test_external_canonical_change_is_not_overwritten(self):
        m=self.prepare();view=self.root/m["view_relative"]
        self.make_png(view/"nes/cover-front/Title.png",32,32,"blue")
        self.target.write_bytes(b"external change")
        session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"blocked")
        self.assertEqual(self.target.read_bytes(),b"external change")

    def test_second_writer_is_blocked_even_from_another_thread(self):
        from concurrent.futures import ThreadPoolExecutor
        with session.write_lock(self.api):
            with session.write_lock(self.api):pass
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(session.busy,self.api).result())
        self.assertFalse(session.busy(self.api))

    def test_unsupported_clone_does_not_fall_back_to_full_copy(self):
        original=self.target.read_bytes()
        with patch.object(session,"clone",side_effect=OSError("Operation not supported")):
            with self.assertRaisesRegex(RuntimeError,"Sem fallback"):
                session.prepare_view("esde","wine",self.api)
        self.assertEqual(self.target.read_bytes(),original)
        self.assertEqual(list(self.api["OPS_ROOT"].glob("*/staging/media")),[])

    def test_atomic_replace_download_cannot_replace_canonical_before_commit(self):
        original=self.target.read_bytes();m=self.prepare();view=self.root/m["view_relative"]
        temporary=view/"incoming.png";self.make_png(temporary,32,32,"blue")
        temporary.replace(view/"nes/covers/Title.png")
        self.assertEqual(self.target.read_bytes(),original)
        session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"published")

    def test_crash_after_publication_recovers_without_an_extra_alt(self):
        original=self.target.read_bytes();m=self.prepare();view=self.root/m["view_relative"]
        self.make_png(view/"nes/cover-front/Title.png",32,32,"blue")
        normal=self.api["atomic_json"];fired=[False]
        def write(path,data):
            if not fired[0] and any(x.get("status")=="finishing" for x in data.get("items",[])):
                fired[0]=True;raise OSError("simulated power interruption")
            normal(path,data)
        self.api["atomic_json"]=write
        with self.assertRaises(OSError):session.reconcile(m,self.api)
        self.api["atomic_json"]=normal
        session._recover(self.api)
        recovered=json.loads((self.api["operation_dir"](m["operation_id"])/"manifest.json").read_text())
        self.assertEqual(recovered["status"],"completed")
        self.assertEqual((self.media/recovered["items"][0]["previous_payload_relative"]).read_bytes(),original)
        self.assertEqual(len(list(self.target.parent.glob("*Alt*"))),1)

    def test_nested_rom_view_and_download_keep_canonical_media_flat(self):
        rom=self.root/"roms/nes/Title.nes"
        (rom.parent/"translation-pt-br").mkdir()
        rom.rename(rom.parent/"translation-pt-br/Title.nes")
        m=self.prepare();view=self.root/m["view_relative"]
        nested=view/"nes/covers/translation-pt-br/Title.png"
        self.assertTrue(nested.is_file())
        original=self.target.read_bytes()
        self.make_png(nested,32,32,"blue")
        self.assertEqual(original,self.target.read_bytes())
        session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"published")
        self.assertFalse((self.target.parent/"translation-pt-br").exists())

    def test_ambiguous_nested_name_is_preserved_without_merging_games(self):
        for folder in ("translation-pt-br","hack"):
            p=self.root/"roms/nes"/folder;p.mkdir();(p/"Title.nes").write_bytes(b"variant")
        m=self.prepare();view=self.root/m["view_relative"]
        self.make_png(view/"nes/covers/hack/Title.png",32,32,"blue")
        session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"blocked")

    def test_complete_child_session_redirects_and_restores_configuration(self):
        import sys
        settings=self.root/"frontend/ES-DE/UserData/settings/es_settings.xml"
        code='import xml.etree.ElementTree as E; from pathlib import Path; p=Path('+repr(str(settings))+'); r=E.fromstring("<r>"+p.read_text()+"</r>"); v=Path(next(x.attrib["value"] for x in r if x.attrib.get("name")=="MediaDirectory")); (v/"nes/cover-front/Title.png").write_bytes((v/"nes/cover-front/Title.png").read_bytes())'
        with patch.object(self.helper,"apply_configuration",return_value=0):
            m=session.command_session(SimpleNamespace(client="esde",runtime="wine",child_command=[sys.executable,"-c",code]),self.api)
        self.assertEqual(m["child_exit_code"],0)
        self.assertTrue(m["view_cleaned"])
        self.assertIn(str(self.media),settings.read_text())
        self.assertIn('user preference',settings.read_text())
        self.assertEqual(m["items"][0]["status"],"deduplicated")

    def test_frontend_exit_does_not_restore_media_while_emulator_is_alive(self):
        ready = self.root / "emulator-ready"
        release = self.root / "emulator-release"
        emulator = ("from pathlib import Path; import time; "
                    f"Path({str(ready)!r}).touch(); "
                    f"release=Path({str(release)!r}); "
                    "\nwhile not release.exists(): time.sleep(.025)")
        frontend = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{emulator!r}])"
        outcome = {}
        def run():
            try:
                with patch.object(self.helper, "apply_configuration", return_value=0):
                    outcome["result"] = session.command_session(
                        SimpleNamespace(client="esde", runtime="wine",
                                        child_command=[sys.executable, "-c", frontend]), self.api)
            except Exception as exc:
                outcome["error"] = exc
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertTrue(ready.exists(), outcome)
            time.sleep(.15)
            self.assertTrue(worker.is_alive(), "Sessão terminou enquanto emulador ainda estava vivo")
            self.assertNotIn(str(self.media),
                             (self.root / "frontend/ES-DE/UserData/settings/es_settings.xml").read_text())
        finally:
            release.touch()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive(), "Sessão não concluiu após o emulador encerrar")
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["child_exit_code"], 0)

    def test_observed_detached_emulator_survives_reparenting_and_environment_change(self):
        ready = self.root / "emulator-pid"
        change_env = self.root / "emulator-change-env"
        changed = self.root / "emulator-changed-env"
        frontend_exit = self.root / "frontend-exit"
        emulator_exit = self.root / "emulator-exit"
        after_exec = ("from pathlib import Path; import time; "
                      f"Path({str(changed)!r}).touch(); "
                      f"exit_file=Path({str(emulator_exit)!r}); "
                      "\nwhile not exit_file.exists(): time.sleep(.025)")
        emulator = ("import os,sys,time; from pathlib import Path; os.setsid(); "
                    f"Path({str(ready)!r}).write_text(str(os.getpid())); "
                    f"change=Path({str(change_env)!r}); "
                    "\nwhile not change.exists(): time.sleep(.025)\n"
                    f"os.execve(sys.executable,[sys.executable,'-c',{after_exec!r}],{{'PATH':os.environ.get('PATH','')}})")
        frontend = ("import subprocess,sys,time; from pathlib import Path; "
                    f"subprocess.Popen([sys.executable,'-c',{emulator!r}]); "
                    f"exit_file=Path({str(frontend_exit)!r}); "
                    "\nwhile not exit_file.exists(): time.sleep(.025)")
        outcome = {}

        def run():
            try:
                with patch.object(self.helper, "apply_configuration", return_value=0):
                    outcome["result"] = session.command_session(
                        SimpleNamespace(client="esde", runtime="wine",
                                        child_command=[sys.executable, "-c", frontend]), self.api)
            except Exception as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        emulator_pid = None
        emulator_ticks = None
        try:
            deadline = time.monotonic() + 6
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertTrue(ready.exists(), outcome)
            emulator_pid = int(ready.read_text())
            identity = session._process_identity(emulator_pid)
            self.assertIsNotNone(identity)
            emulator_ticks = identity["start"]
            # Observation must be persisted before the descendant execs with
            # a clean environment and loses its original group/parent.
            while time.monotonic() < deadline:
                manifests = list(self.api["OPS_ROOT"].glob("media-session-*/manifest.json"))
                if manifests:
                    manifest = json.loads(manifests[0].read_text())
                    if str(emulator_pid) in manifest.get("process_guard", {}).get("observed", {}):
                        break
                time.sleep(.025)
            else:
                self.fail("Emulador destacado não foi observado antes de mudar de ambiente")
            change_env.touch()
            while not changed.exists() and time.monotonic() < deadline:
                time.sleep(.025)
            self.assertTrue(changed.exists())
            frontend_exit.touch()
            time.sleep(.35)
            self.assertTrue(worker.is_alive(), "Sessão liberou a mídia enquanto o emulador destacado estava vivo")
            settings = self.root / "frontend/ES-DE/UserData/settings/es_settings.xml"
            self.assertNotIn(str(self.media), settings.read_text())
        finally:
            change_env.touch()
            frontend_exit.touch()
            emulator_exit.touch()
            worker.join(timeout=8)
            if worker.is_alive() and emulator_pid:
                # Only terminate the private fixture process, never a session
                # that belongs to the actual desktop.
                try:
                    identity = session._process_identity(emulator_pid)
                    if identity is not None and identity["start"] == emulator_ticks:
                        os.kill(emulator_pid, 15)
                except ProcessLookupError:
                    pass
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), outcome)
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["child_exit_code"], 0)

    def test_recovery_blocks_active_process_after_spawn_before_pid_persisted(self):
        manifest = self.prepare()
        session._configure(manifest, False, self.api)
        token = uuid.uuid4().hex
        manifest.update(status="starting", process_guard={
            "token": token, "boot_id": session._boot_id(), "start_floor": 0,
            "group": None, "observed": {}})
        session._save(manifest, self.api)
        # Simulate a crash between Popen() and recording the child's PID.
        env = {**os.environ, session._SESSION_TOKEN_ENV: token}
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"],
                                 env=env, start_new_session=True)
        try:
            with self.assertRaisesRegex(session.MediaBusy, "processos ativos"):
                session._recover(self.api)
            settings = self.root / "frontend/ES-DE/UserData/settings/es_settings.xml"
            self.assertNotIn(str(self.media), settings.read_text())
        finally:
            child.terminate()
            child.wait(timeout=5)
        session._recover(self.api)
        restored = json.loads((self.api["operation_dir"](manifest["operation_id"]) / "manifest.json").read_text())
        self.assertEqual(restored["status"], "completed")
        self.assertIn(str(self.media), settings.read_text())

    def test_recovery_from_previous_boot_does_not_block_on_reused_pid(self):
        manifest = self.prepare()
        session._configure(manifest, False, self.api)
        manifest.update(status="waiting_descendants", child_pid=os.getpid(), process_guard={
            "token": uuid.uuid4().hex, "boot_id": str(uuid.uuid4()),
            "start_floor": 0, "group": os.getpid(),
            "observed": {str(os.getpid()): session._process_identity(os.getpid())["start"]}})
        session._save(manifest, self.api)
        session._recover(self.api)
        self.assertEqual(json.loads((self.api["operation_dir"](manifest["operation_id"]) /
                                     "manifest.json").read_text())["status"], "completed")

    def test_failed_frontend_start_restores_configuration(self):
        with patch.object(self.helper, "apply_configuration", return_value=0):
            result = session.command_session(
                SimpleNamespace(client="esde", runtime="wine",
                                child_command=[str(self.root / "missing-executable")]), self.api)
        self.assertEqual(result["child_exit_code"], 127)
        self.assertIn(str(self.media), (self.root / "frontend/ES-DE/UserData/settings/es_settings.xml").read_text())

    def test_skraper_redirect_restore_both_runtimes(self):
        path=self.root/"tools/roms/skraper/skraper.config.mono.json";path.parent.mkdir(parents=True)
        for runtime in ("mono","wine"):
            path.write_text(json.dumps({"Systems":[{"GamesAndRoms":{"Path":str(self.root/'roms/nes')},"GameList":{"Type":0},"Medias":[{"OutputFolder":"%ROMROOTFOLDER%/../media/nes/cover-front/","EmptyOutputFolder":True}]}],"AllSystems":{"GameList":{"Type":0}}}))
            m=session.prepare_view("skraper",runtime,self.api)
            session._configure(m,False,self.api)
            redirected = json.loads(path.read_text())
            folder = redirected["Systems"][0]["Medias"][0]["OutputFolder"].replace("\\", "/")
            gamelist = redirected["Systems"][0]["GameList"]["OutputFile"].replace("\\", "/")
            self.assertIn("staging/media/", folder)
            self.assertIn("staging/gamelists/nes/gamelist.xml", gamelist)
            self.assertEqual(redirected["Systems"][0]["GameList"]["Type"], 1)
            self.assertEqual(redirected["AllSystems"]["GameList"]["Type"], 0)
            session._configure(m,True,self.api)
            restored = json.loads(path.read_text())
            folder = restored["Systems"][0]["Medias"][0]["OutputFolder"].replace("\\", "/")
            gamelist = restored["Systems"][0]["GameList"]["OutputFile"].replace("\\", "/")
            self.assertEqual(folder, "%ROMROOTFOLDER%/../../media/nes/cover-front")
            self.assertEqual(gamelist, "%ROMROOTFOLDER%/../../frontend/ES-DE/UserData/gamelists/nes/gamelist.xml")
            self.assertEqual(restored["Systems"][0]["GameList"]["Type"], 0)
            self.assertFalse(restored["Systems"][0]["Medias"][0]["EmptyOutputFolder"])
            self.assertNotIn("staging", path.read_text())
            session.reconcile(m,self.api)

    def test_managed_skraper_session_does_not_enlarge_roms_media(self):
        import sys
        path = self.root / "tools/roms/skraper/skraper.config.mono.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"Systems":[{"GamesAndRoms":{"Path":str(self.root/'roms/nes')},"GameList":{"Type":0,"OutputFile":"old"},"Medias":[{"OutputFolder":"%ROMROOTFOLDER%/../media/nes/cover-front/","EmptyOutputFolder":True}]}],"AllSystems":{"GameList":{"Type":0,"OutputFile":""}}}))
        (self.root / "frontend/RetroFE/Config/profiles").mkdir(parents=True)
        (self.root / "frontend/RetroFE/Config/profiles/host-linux.json").write_text(json.dumps({"collections":{"NES":{"rom_subdir":"nes"}}}))
        script = r'''
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text())
row = cfg["Systems"][0]
media = Path(row["Medias"][0]["OutputFolder"])
media.mkdir(parents=True, exist_ok=True)
source = Path(sys.argv[2])
(media / "Title.png").write_bytes(source.read_bytes())
gamelist = Path(row["GameList"]["OutputFile"])
gamelist.parent.mkdir(parents=True, exist_ok=True)
gamelist.write_text("<gameList><game><path>./Title.nes</path><name>Title</name><desc>Uma sinopse real.</desc><rating>0</rating><favorite>true</favorite></game></gameList>", encoding="utf-8")
'''
        child = self.root / "child.py"
        child.write_text(script)
        with patch.object(self.helper, "prepare_skraper", return_value=0):
            result = session.command_session(SimpleNamespace(client="skraper", runtime="mono", child_command=[sys.executable, str(child), str(path), str(self.target)]), self.api)
        self.assertEqual(result["child_exit_code"], 0)
        self.assertFalse((self.root / "roms/media").exists())
        self.assertNotIn("staging", path.read_text())
        self.assertEqual((self.root / "media/nes/story/Title.txt").read_text(encoding="utf-8"), "Uma sinopse real.\n")
        restored = json.loads(path.read_text())
        self.assertEqual(restored["Systems"][0]["GameList"]["Type"], 0)
        self.assertNotIn("staging", restored["Systems"][0]["GameList"]["OutputFile"])

    def test_managed_esde_session_publishes_story_without_roms_media(self):
        import sys
        (self.root / "frontend/RetroFE/Config/profiles").mkdir(parents=True)
        (self.root / "frontend/RetroFE/Config/profiles/host-linux.json").write_text(json.dumps(
            {"collections": {"Nintendo Entertainment System": {"rom_subdir": "nes"}}}))
        (self.root / "frontend/RetroFE/collections/Nintendo Entertainment System").mkdir(parents=True)
        (self.root / "frontend/RetroFE/collections/Nintendo Entertainment System/settings.conf").write_text(
            "list.path = %BASE_ITEM_PATH%/nes\nmedia.story = collections/%ITEM_COLLECTION_NAME%/medium_artwork/story\n")
        settings = self.root / "frontend/ES-DE/UserData/settings/es_settings.xml"
        gamelist = self.root / "frontend/ES-DE/UserData/gamelists/nes/gamelist.xml"
        script = r'''
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
media = Path(re.search(r'name="MediaDirectory" value="([^"]+)"', text).group(1))
target = media / "nes/cover-front"
target.mkdir(parents=True, exist_ok=True)
(target / "Title.png").write_bytes(Path(sys.argv[2]).read_bytes())
out = Path(sys.argv[3])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("<gameList><game><path>./Title.nes</path><name>Title</name><desc>Uma sinopse real.</desc><rating>0</rating><favorite>true</favorite></game></gameList>", encoding="utf-8")
'''
        child = self.root / "child-esde.py"
        child.write_text(script)
        with patch.object(self.helper, "apply_configuration", return_value=0):
            result = session.command_session(SimpleNamespace(
                client="esde", runtime="wine",
                child_command=[sys.executable, str(child), str(settings), str(self.target), str(gamelist)]), self.api)
        self.assertEqual(result["child_exit_code"], 0)
        self.assertTrue(result.get("metadata_ingested"))
        self.assertFalse((self.root / "roms/media").exists())
        self.assertNotIn("staging", settings.read_text(encoding="utf-8"))
        self.assertEqual((self.root / "media/nes/story/Title.txt").read_text(encoding="utf-8"), "Uma sinopse real.\n")
        self.assertIn("<favorite>true</favorite>", gamelist.read_text(encoding="utf-8"))
        hyper = (self.root / "frontend/RetroFE/meta/hyperlist/Nintendo Entertainment System.xml").read_text(encoding="utf-8")
        self.assertIn(">Title<", hyper)
        self.assertIn(">0.0<", hyper)
        self.assertNotIn("Uma sinopse real", hyper)
        story_line = (self.root / "frontend/RetroFE/collections/Nintendo Entertainment System/settings.conf").read_text(encoding="utf-8")
        self.assertIn("media.story = ../../media/nes/story", story_line)

    def test_native_auto_profile_cannot_be_changed_to_lossy(self):
        m=self.prepare();view=self.root/m["view_relative"]
        self.make_png(view/"nes/covers/Title.png",32,32,"blue")
        config,path,digest=media_optimizer._config(self.api)
        for profile in config["profiles"]:
            if profile["id"]=="png-lossless-repack":profile["lossy"]=True
        with patch.object(media_optimizer,"_config",return_value=(config,path,digest)):
            session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"blocked")

    def test_known_lossless_representation_deduplicates_a_redownload(self):
        original=self.target.read_bytes();raw_hash=self.api["sha256_file"](self.target)
        subprocess.run(["magick",str(self.target),"-define","png:compression-level=9",str(self.target)],check=True,capture_output=True)
        output_hash=self.api["sha256_file"](self.target)
        self.assertNotEqual(raw_hash,output_hash)
        m=self.prepare();(self.root/m["view_relative"]/"nes/cover-front/Title.png").write_bytes(original)
        with patch.object(session,"_representations",return_value={("nes/cover-front/Title.png",raw_hash):output_hash}):
            session.reconcile(m,self.api)
        self.assertEqual(m["items"][0]["status"],"deduplicated")
        self.assertEqual(len(list(self.target.parent.glob('*.png'))),1)

    def test_defaults_are_idempotent_and_preserve_unrelated_preferences(self):
        text='<bool name="ScraperOverwriteData" value="true" />\n<bool name="MiximageOverwrite" value="true" />\n<string name="Keep" value="yes" />\n'
        first,_=self.helper.updated_settings_text(text,self.root/"roms",self.media)
        second,_=self.helper.updated_settings_text(first,self.root/"roms",self.media)
        self.assertEqual(first,second)
        for key in ("ScraperOverwriteData","MiximageOverwrite"):
            self.assertEqual(self.helper.setting_entries(first,key,"bool")[0][1],"false")
            self.assertEqual(len(self.helper.setting_entries(first,key,"bool")),1)
        self.assertIn('name="Keep" value="yes"',second)

    def test_global_catalogue_and_rollback_are_updated(self):
        import csv
        output=self.root/'outputs';output.mkdir();self.api['OUTPUT_ROOT']=output
        def write(path,rows,fields):
            with path.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
        self.api['write_csv_atomic']=write
        write(output/'catalogo-arquivos.csv',[{'absolute_path':str(self.target),'current_absolute_path':str(self.target)}],['absolute_path','current_absolute_path','current_path_status'])
        write(output/'pendencias-integridade-dependencias.csv',[],['type','source','path','status','dependency_or_impact','next_step'])
        (output/'estado-final-verificacao.json').write_text('{}')
        m=self.prepare();self.make_png(self.root/m['view_relative']/'nes/covers/Title.png',32,32,'blue')
        session.reconcile(m,self.api)
        with (output/'catalogo-arquivos.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),2)
        self.assertEqual(json.loads((output/'estado-final-verificacao.json').read_text())['current_catalog_rows'],2)
        session.rollback(m['operation_id'],True,self.api)
        with (output/'catalogo-arquivos.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual(rows[0]['current_sha256'],self.api['sha256_file'](self.target))
        self.assertIn('rollback',rows[1]['current_path_status'])

    def test_crash_during_owned_view_cleanup_recovers(self):
        m=self.prepare();normal=session.shutil.rmtree
        with patch.object(session.shutil,'rmtree',side_effect=OSError('simulated cleanup interruption')):
            with self.assertRaises(OSError):session.reconcile(m,self.api)
        self.assertEqual(m['status'],'cleanup_pending')
        session._recover(self.api)
        recovered=json.loads((self.api['operation_dir'](m['operation_id'])/'manifest.json').read_text())
        self.assertEqual(recovered['status'],'completed')
        self.assertFalse((self.root/m['view_relative']).exists())


if __name__=="__main__":unittest.main()
