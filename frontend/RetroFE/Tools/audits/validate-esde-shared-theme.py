#!/usr/bin/env python3
"""Render PS5/Switch2 with the real ES-DE in a private, bounded display.

No game launch, sound, physical device or live userdata is exposed. AppImage
resources are mounted read-only, never unpacked into a second installation.
"""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[4]


def wait_for_native_exit(app, wid, run):
    """Ask only the test window to quit; require its native process to exit."""
    run(['xdotool','windowfocus','--sync',wid])
    focused=run(['xdotool','getwindowfocus'])
    if int(focused,10) != int(wid,10):
        raise RuntimeError('Janela ES-DE perdeu o foco antes do encerramento')
    # The private ES-DE settings use F4, handled by its SDL input manager.
    # Alt+F4 normally relies on a window manager, which Xvfb lacks.
    run(['xdotool','key','--clearmodifiers','F4'])
    try:
        return app.wait(timeout=5), 'configured_f4'
    except subprocess.TimeoutExpired:
        if app.poll() is not None:
            return app.poll(), 'configured_f4'
        # A bare Xvfb server has no window manager to handle Alt+F4.
        # WM_DELETE_WINDOW is delivered to the client, unlike windowclose.
        run(['xdotool','windowquit',wid])
        try:
            return app.wait(timeout=6), 'wm_delete_window'
        except subprocess.TimeoutExpired:
            return None, 'graceful_exit_timeout'


def require_native_success(stdout, process_returncode, out):
    """Enforce the inner native result independently of its process status."""
    if process_returncode != 0:
        raise RuntimeError('Validação ES-DE falhou; consultar validation.log')
    try:
        result=json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError('Validação ES-DE sem resultado nativo verificável') from exc
    if result.get('native_exit_code') != 0 or result.get('shutdown') not in {'configured_f4','wm_delete_window'}:
        raise RuntimeError('Encerramento nativo ES-DE não aprovado: '+str(result.get('native_exit_code')))
    if any(not (out/name).is_file() or not (out/name).stat().st_size
           for name in ('system-0.png','system-1.png','gamelist.png')):
        raise RuntimeError('Capturas ES-DE ausentes ou vazias')


def inner(binary, userdata, out, width, height):
    env={**os.environ,'SDL_AUDIODRIVER':'dummy','SDL_RENDER_DRIVER':'software',
         'LIBGL_ALWAYS_SOFTWARE':'1','ESDE_APPDATA_DIR':str(userdata)}
    log=(out/'esde-native-stdout.log').open('w')
    native_env={**env,'LD_LIBRARY_PATH':str(binary.parent.parent/'lib')}
    app=subprocess.Popen([str(binary),'--resolution',str(width),str(height),'--no-splash'],
                         env=native_env,stdout=log,stderr=subprocess.STDOUT)
    def run(args):
        return subprocess.run(args,capture_output=True,text=True,check=True,timeout=10).stdout.strip()
    try:
        deadline=time.monotonic()+45;wid=None
        while time.monotonic()<deadline:
            if app.poll() is not None:raise RuntimeError('ES-DE encerrou antes da captura; confira log')
            found=subprocess.run(['xdotool','search','--onlyvisible','--pid',str(app.pid)],capture_output=True,text=True)
            if found.returncode==0:
                wid=found.stdout.splitlines()[0];break
            time.sleep(.2)
        if not wid:raise RuntimeError('Janela ES-DE ausente')
        run(['xdotool','windowfocus','--sync',wid])
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            engine=userdata/'logs/es_log.txt'
            if engine.is_file() and 'Finished loading theme' in engine.read_text(errors='replace'):break
            if app.poll() is not None:raise RuntimeError('ES-DE saiu durante inicialização')
            time.sleep(.2)
        else:raise RuntimeError('Tema ES-DE não terminou a inicialização')
        time.sleep(2)
        for name,key in [('system-0',None),('system-1','Right'),('gamelist','Return')]:
            if key:run(['xdotool','key','--clearmodifiers',key]);time.sleep(2)
            deadline=time.monotonic()+10
            while True:
                run(['import','-display',env['DISPLAY'],'-window',wid,str(out/(name+'.png'))])
                # ImageMagick already captures the frame; keep validation independent
                # from the host's Pillow/libjpeg ABI and reject uniform captures.
                deviation=run(['identify','-format',
                    '%[fx:standard_deviation.r] %[fx:standard_deviation.g] %[fx:standard_deviation.b]',
                    str(out/(name+'.png'))])
                if max(float(value) for value in deviation.split())>3/255:break
                if time.monotonic()>deadline:raise RuntimeError('Captura preta não aprova o tema: '+name)
                time.sleep(.3)
        native_exit,shutdown=wait_for_native_exit(app,wid,run)
        print(json.dumps({'native_exit_code':native_exit,'shutdown':shutdown,
                          'captures':['system-0.png','system-1.png','gamelist.png'],
                          'physical_input_test':False,'gameplay_test':False}),flush=True)
        if native_exit != 0:
            raise RuntimeError('ES-DE não encerrou normalmente: '+str(native_exit)+' ('+shutdown+')')
    finally:
        if app.poll() is None:
            print('ES-DE: cleanup restrito ao processo de teste (SIGTERM)',file=sys.stderr,flush=True)
            app.terminate()
            try:app.wait(timeout=4)
            except subprocess.TimeoutExpired:
                print('ES-DE: cleanup escalado para SIGKILL',file=sys.stderr,flush=True)
                app.kill();app.wait()
        log.close()
        for p in userdata.glob('logs/*'):
            if p.is_file():shutil.copy2(p,out/p.name)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--theme-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--width',type=int,default=1920);parser.add_argument('--height',type=int,default=1080)
    parser.add_argument('--inner',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args();out=args.output.resolve();theme=args.theme_root.resolve()
    if not out.is_relative_to(ROOT/'outputs/roms-consolidacao-2026-10-24/operations'):
        raise ValueError('Capturas exigem uma operação oficial')
    if not theme.is_relative_to(ROOT) or not (theme/'theme.xml').is_file():
        raise ValueError('Tema precisa estar no projeto')
    if not (640<=args.width<=1920 and 480<=args.height<=1200):raise ValueError('Resolução fora do orçamento')
    userdata=ROOT/'frontend/ES-DE/UserData'
    if args.inner:
        inner(args.inner,userdata,out,args.width,args.height);return
    sys.path.insert(0,str(ROOT/'tools/roms'));import romtool
    if any(romtool.frontend_processes_running().values()):raise ValueError('Feche os frontends normalmente')
    out.mkdir(parents=True,exist_ok=True)
    image=ROOT/'frontend/ES-DE/ES-DE_x64_SteamDeck.AppImage'
    mount=None
    with tempfile.TemporaryDirectory(prefix='esde-native-',dir=ROOT/'work/roms-consolidation') as temporary:
        scratch=Path(temporary);data=scratch/'UserData'
        for p in ['settings','custom_systems','themes/linear-es-de','logs']:(data/p).mkdir(parents=True,exist_ok=True)
        config=ROOT/'frontend/ES-DE/UserData/custom_systems/es_systems.xml'
        tree=ET.parse(config)
        for system in list(tree.getroot()):
            if system.findtext('name') not in {'ps5','switch2'}:tree.getroot().remove(system);continue
            for command in system.findall('command'):command.text='/bin/false %ROM%'
        tree.write(data/'custom_systems/es_systems.xml',encoding='utf-8',xml_declaration=True)
        shutil.copy2(config.with_name('es_find_rules.xml'),data/'custom_systems/es_find_rules.xml')
        (data/'settings/es_settings.xml').write_text(
            '<string name="ROMDirectory" value="'+str(ROOT/'roms')+'"/>\n'
            '<string name="MediaDirectory" value="'+str(ROOT/'media')+'"/>\n'
            '<string name="Theme" value="linear-es-de"/>\n<bool name="SplashScreen" value="false"/>\n'
            '<string name="KeyboardQuitShortcut" value="F4"/>\n'
            '<int name="ScreensaverTimer" value="0"/>\n')
        for p in ['rom-view/ps5','rom-view/switch2']:(scratch/p).mkdir(parents=True,exist_ok=True)
        env={**os.environ,'APPIMAGELAUNCHER_DISABLE':'1','TMPDIR':str(scratch)}
        with (out/'appimage-mount.log').open('w') as mountlog:
            mount=subprocess.Popen([str(image),'--appimage-mount'],stdout=subprocess.PIPE,stderr=mountlog,text=True,env=env)
            try:
                deadline=time.monotonic()+15;mounted=None
                while time.monotonic()<deadline:
                    if mount.poll() is not None:break
                    ready,_,_=select.select([mount.stdout],[],[],.2)
                    if ready:
                        line=mount.stdout.readline().strip()
                        if line.startswith('/') and (Path(line)/'usr/bin/es-de').is_file():mounted=Path(line);break
                if mounted is None:raise RuntimeError('Não consegui montar AppImage somente leitura')
                command=['bwrap','--unshare-pid','--die-with-parent','--ro-bind','/','/','--dev','/dev','--proc','/proc',
                    '--bind',str(scratch),str(scratch),'--bind',str(data),str(userdata),
                    '--ro-bind',str(theme),str(userdata/'themes/linear-es-de'),
                    '--ro-bind',str(scratch/'rom-view'),str(ROOT/'roms'),
                    '--ro-bind',str(ROOT/'roms/ps5'),str(ROOT/'roms/ps5'),
                    '--ro-bind',str(ROOT/'roms/switch2'),str(ROOT/'roms/switch2'),
                    '--bind',str(out),str(out),'--ro-bind',str(mounted),str(mounted),
                    sys.executable,'-B',str(Path(__file__).resolve()),'--theme-root',str(theme),'--output',str(out),
                    '--width',str(args.width),'--height',str(args.height),'--inner',str(mounted/'usr/bin/es-de')]
                env.update(SDL_AUDIODRIVER='dummy',XDG_CACHE_HOME=str(scratch/'cache'))
                env.pop('LD_LIBRARY_PATH',None)
                env.pop('WAYLAND_DISPLAY',None)
                process=subprocess.Popen(['xvfb-run','--auto-servernum','--server-args',
                    f'-screen 0 {args.width+100}x{args.height+100}x24 -nolisten tcp',*command],
                    env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
                try:
                    stdout,stderr=process.communicate(timeout=100)
                finally:
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL);process.wait()
                (out/'validation.log').write_text(stdout+stderr)
                print(stdout.strip());print('VALIDATION_EXIT',process.returncode)
                require_native_success(stdout,process.returncode,out)
            finally:
                if mount.poll() is None:mount.terminate()
                try:mount.wait(timeout=5)
                except subprocess.TimeoutExpired:mount.kill();mount.wait()


if __name__=='__main__':main()
