#!/usr/bin/env python3
"""Install and diagnose the project-local Quiver Launcher without touching ROM payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import fcntl
import errno
import importlib.util
import math
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
ROM_ROOT = PROJECT_ROOT / "roms"
APP_NAME = "QuiverLauncher-linux-x64.AppImage"
APP_IMAGE = SCRIPT_DIR / APP_NAME
APP_URL = (
    "https://github.com/tgeorgiadis/quiver-launcher/releases/download/"
    "v3.5.1/QuiverLauncher-linux-x64.AppImage"
)
APP_RELEASE = "v3.5.1"
APP_SIZE = 54_876_664
APP_SHA256 = "a8cd5398b6dae924bcec09ece1781c03cbfd73298675476f26fc1c072d3c629b"
DOWNLOAD_CANDIDATE = Path.home() / "Downloads" / APP_NAME
ROMTOOL = PROJECT_ROOT / "tools" / "roms" / "romtool.py"
DATA_FILES = ("apps.json", "settings.json")
DATA_DIRS = ("Apps", "Cache", "Backups/apps")


def project_api():
    sys.path.insert(0,str(PROJECT_ROOT/'tools/roms'))
    import romtool
    return romtool


@contextmanager
def startup_mutation_lock():
    api=project_api();api.OPS_ROOT.mkdir(parents=True,exist_ok=True)
    fd=os.open(api.OPS_ROOT/'quiver-launch.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Quiver iniciando ou ativo; preparação/rollback recusado para preservar os dados em uso')
        yield
    finally:os.close(fd)


def window_helper():
    """Reuse the project's single window implementation; no copy or host path."""
    path=PROJECT_ROOT/'frontend/RetroFE/Tools/launchers/retrofe/window_placement.py'
    spec=importlib.util.spec_from_file_location('quiver_shared_window',path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


def target_monitor():
    helper=window_helper()
    if not helper.backend_available():raise RuntimeError('Monitor: DISPLAY, wmctrl, xdotool e xprop são necessários; não iniciei uma janela em tela indefinida')
    monitor,reason=helper.detect_target_monitor(os.environ.get('QUIVER_TARGET_OUTPUT','auto'))
    return helper,monitor,reason


def wizard_settings(data,monitor=None):
    updated=dict(data)
    for key in ['FirstStartup','LocalFirstCatalogMigrationComplete','UsageDataAsked','UsageDataEnabled']:
        if key in data and not isinstance(data[key],bool):raise ValueError('Flag de configuração inválida: '+key)
    updated.update(FirstStartup=False,LocalFirstCatalogMigrationComplete=True,UsageDataAsked=True)
    # An existing choice remains intact; a fresh installation does not opt in.
    updated.setdefault('UsageDataEnabled',False)
    updated.setdefault('AppsPath','')
    if monitor:
        previous=data.get('DesktopWindowPlacement')
        previous=previous if isinstance(previous,dict) else {}
        def dimension(key,default):
            value=previous.get(key,default)
            return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and 0<value<=100000 else default
        x,y=previous.get('X'),previous.get('Y');bounds=monitor.rectangle
        keep_position=type(x) is int and type(y) is int and bounds.x<=x<bounds.x+bounds.width and bounds.y<=y<bounds.y+bounds.height
        updated['DesktopWindowPlacement']={'Width':dimension('Width',1100),'Height':dimension('Height',700),
            'X':x if keep_position else bounds.x+32,'Y':y if keep_position else bounds.y+32,'Maximized':previous.get('Maximized') is True}
    changes=[key for key in updated if data.get(key)!=updated[key] or key not in data]
    return updated,changes


def desktop_quote(value):
    escaped=''.join('\\'+c if c in '\\"`$' else c for c in str(value))
    return '"'+escaped.replace('\\','\\\\').replace('%','%%')+'"'


def shortcut_paths():
    desktop=None
    if shutil.which('xdg-user-dir'):
        result=subprocess.run(['xdg-user-dir','DESKTOP'],capture_output=True,text=True,timeout=5)
        if result.returncode==0 and result.stdout.strip():desktop=Path(result.stdout.strip())
    desktop=desktop or Path.home()/'Desktop'
    applications=Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')/'applications'
    if not desktop.is_absolute() or not applications.is_absolute():raise ValueError('Pastas XDG dos atalhos precisam ser absolutas')
    return [desktop/'quiver-launcher-canonical.desktop',applications/'quiver-launcher-canonical.desktop']


def shortcut_payload():
    return ('[Desktop Entry]\nVersion=1.0\nType=Application\nName=Quiver Launcher\nName[pt_BR]=Quiver Launcher\n'
        'GenericName=Application and game launcher\nGenericName[pt_BR]=Inicializador de aplicativos e jogos\n'
        'Comment=Browse and launch Quiver applications\nComment[pt_BR]=Navegue e inicie aplicativos pelo Quiver\n'
        f'Exec={desktop_quote(SCRIPT_DIR/"launch.sh")}\nPath={SCRIPT_DIR}\n'
        'Icon=applications-games\nTerminal=false\nStartupNotify=true\nCategories=Game;Emulator;\n'
        'X-EmulationProject-Managed=QuiverLauncher\n').encode()


def preparation_plan(monitor=None,settings=True,shortcuts=True):
    library_status()  # never write over invalid/ambiguous state
    items=[];keys=[]
    if settings:
        path=SCRIPT_DIR/'settings.json'
        before=path.read_bytes() if path.exists() else None
        data=load_object(path) if before is not None else {}
        updated,keys=wizard_settings(data,monitor)
        if keys:
            payload=(json.dumps(updated,ensure_ascii=False,indent=2)+'\n').encode()
            if before and before.startswith(b'\xef\xbb\xbf'):payload=b'\xef\xbb\xbf'+payload
            items.append({'path':path,'before':before,'after':payload,'mode':stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600,'role':'quiver_settings','keys':keys})
    destinations=shortcut_paths() if shortcuts else []
    for path in destinations:
        if path.is_symlink():raise ValueError('Atalho por symlink preservado; exige revisão: '+str(path))
        before=path.read_bytes() if path.exists() else None
        if before is not None and b'X-EmulationProject-Managed=QuiverLauncher' not in before.splitlines():
            if ('Exec='+desktop_quote(SCRIPT_DIR/'launch.sh')).encode() in before.splitlines():continue
            raise ValueError('Atalho pessoal preservado; destino ocupado: '+str(path))
        after=shortcut_payload()
        mode=stat.S_IMODE(path.stat().st_mode) if before is not None else None
        if before!=after or mode!=0o755:
            items.append({'path':path,'before':before,'after':after,'mode':0o755,'role':'desktop_shortcut','keys':[]})
    return {'items':items,'settings_keys':keys,'shortcuts':[str(p) for p in destinations]}


def atomic_bytes(path,data,mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        temporary.chmod(mode);os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)


def apply_preparation(plan):
    if not plan['items']:return {'status':'already_prepared','changes':0,'shortcuts':plan['shortcuts']}
    api=project_api();operation_id='quiver-startup-'+time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
    op=api.operation_dir(operation_id);(op/'rollback').mkdir(parents=True)
    records=[]
    manifest={'operation_id':operation_id,'kind':'quiver_startup_preparation','created_at':api.now(),'status':'preparing_preimages','items':records,'payload_changes':False}
    api.atomic_json(op/'manifest.json',manifest)
    for index,item in enumerate(plan['items']):
        path=item['path'];before=item['before']
        current=path.read_bytes() if path.exists() else None
        if path.is_symlink() or current!=before:raise RuntimeError('Arquivo mudou durante o preflight: '+str(path))
        backup=op/'rollback'/str(index)
        if before is not None:atomic_bytes(backup,before,0o600)
        records.append({'path':str(path),'project_relative':str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT) else None,
            'role':item['role'],'keys':item['keys'],'before_sha256':hashlib.sha256(before).hexdigest() if before is not None else None,
            'after_sha256':hashlib.sha256(item['after']).hexdigest(),'before_mode':stat.S_IMODE(path.stat().st_mode) if before is not None else None,
            'after_mode':item['mode'],'backup_relative':str(backup.relative_to(PROJECT_ROOT)) if before is not None else None,'status':'prepared'})
        api.atomic_json(op/'manifest.json',manifest)
    manifest['status']='prepared'
    api.atomic_json(op/'manifest.json',manifest)
    try:
        for item,record in zip(plan['items'],records):
            path=item['path']
            if path.is_symlink() or (path.read_bytes() if path.exists() else None)!=item['before']:raise RuntimeError('Pré-condição mudou: '+str(path))
            record['status']='writing';api.atomic_json(op/'manifest.json',manifest)
            atomic_bytes(path,item['after'],item['mode'])
            if sha256_file(path)!=record['after_sha256']:raise RuntimeError('SHA após escrita divergiu: '+str(path))
            record['status']='verified';api.atomic_json(op/'manifest.json',manifest)
            api.append_event(operation_id,'startup_file_verified',action=record['role'],status='verified',details=record)
    except Exception as exc:
        for item,record in reversed(list(zip(plan['items'],records))):
            path=item['path']
            if record['status'] not in {'writing','verified'} or not path.is_file() or path.is_symlink() or sha256_file(path)!=record['after_sha256']:continue
            if item['before'] is None:path.unlink()  # exact file created by this failed transaction
            else:atomic_bytes(path,item['before'],record['before_mode'])
            record['status']='restored_after_error'
        manifest.update(status='failed_restored',error=str(exc));api.atomic_json(op/'manifest.json',manifest);api.rebuild_history_index();raise
    manifest.update(status='completed_verified',completed_at=api.now());api.atomic_json(op/'manifest.json',manifest);api.rebuild_history_index()
    if any(item['role']=='desktop_shortcut' for item in records):
        command=shutil.which('kbuildsycoca6') or shutil.which('kbuildsycoca5')
        if command:
            try:
                refresh=subprocess.run([command,'--noincremental'],capture_output=True,timeout=30)
                api.append_event(operation_id,'desktop_cache_refreshed',action='kde_shortcuts',status='verified' if refresh.returncode==0 else 'warning',details={'command':[command,'--noincremental'],'exit_code':refresh.returncode})
            except (OSError,subprocess.TimeoutExpired) as exc:
                api.append_event(operation_id,'desktop_cache_refresh_failed',action='kde_shortcuts',status='warning',details={'error':str(exc)})
    return {'status':'prepared','operation_id':operation_id,'changes':len(records),'shortcuts':plan['shortcuts']}


def quiver_processes():
    found=[]
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:
            executable=os.readlink(entry/'exe')
            if Path(executable).name!='QuiverLauncher':continue
            env=dict(part.split(b'=',1) for part in (entry/'environ').read_bytes().split(b'\0') if b'=' in part)
            if env.get(b'APPIMAGE',b'').decode(errors='replace')==str(APP_IMAGE):found.append(int(entry.name))
        except (OSError,ValueError):continue
    return found


def on_monitor(window,monitor):
    a=window.frame;b=monitor.rectangle
    overlap=max(0,min(a.x+a.width,b.x+b.width)-max(a.x,b.x))*max(0,min(a.y+a.height,b.y+b.height)-max(a.y,b.y))
    return b.x<=a.x+a.width/2<b.x+b.width and b.y<=a.y+a.height/2<b.y+b.height and overlap>=a.width*a.height*.9


def place_quiver(helper,monitor,pids,timeout=15):
    deadline=time.monotonic()+timeout;window=None
    while time.monotonic()<deadline:
        owned=set(pids)|set(quiver_processes())
        window=next((w for w in helper.list_windows() if w.pid in owned and 'quiver' in w.wm_class.lower()),None)
        if window:break
        time.sleep(.15)
    if window is None:raise RuntimeError('Quiver não criou uma janela verificável no prazo')
    before=window.frame.as_dict()
    if not on_monitor(window,monitor):
        state=helper.run(['xprop','-id',window.window_id,'_NET_WM_STATE']).stdout
        maximized='_NET_WM_STATE_MAXIMIZED' in state;fullscreen=window.fullscreen
        bounds=monitor.rectangle
        width=min(window.frame.width,max(160,bounds.width-64));height=min(window.frame.height,max(120,bounds.height-64))
        target=helper.Rectangle(bounds.x+(bounds.width-width)//2,bounds.y+(bounds.height-height)//2,width,height)
        placed=False
        for _ in range(5):
            if helper.place_window(window.window_id,target,activate=True):placed=True;break
            time.sleep(.2)
        if not placed:raise RuntimeError('Compositor não confirmou o posicionamento do Quiver')
        if fullscreen:helper.set_fullscreen(window.window_id,True)
        elif maximized:helper.run(['wmctrl','-ir',window.window_id,'-b','add,maximized_vert,maximized_horz'])
        time.sleep(.5)
    actual=helper.window_by_id(window.window_id)
    if not actual or not on_monitor(actual,monitor):raise RuntimeError('Janela Quiver ficou fora do monitor escolhido')
    helper.run(['wmctrl','-ia',actual.window_id])
    return {'pid':actual.pid,'window_id':actual.window_id,'monitor':monitor.name,'monitor_geometry':monitor.rectangle.as_dict(),'before_geometry':before,'actual_geometry':actual.frame.as_dict(),'verified':True}


def launch_quiver(arguments):
    arguments=arguments[1:] if arguments[:1]==['--'] else arguments
    if arguments and arguments[0] not in {'--kiosk'}:
        verify_appimage(APP_IMAGE);library_status()
        return subprocess.run([str(APP_IMAGE),*arguments],cwd=SCRIPT_DIR,env={**os.environ,'APPIMAGELAUNCHER_DISABLE':'1'}).returncode
    helper,monitor,reason=target_monitor()  # before the app changes the active output
    api=project_api();api.OPS_ROOT.mkdir(parents=True,exist_ok=True)
    lock=api.OPS_ROOT/'quiver-launch.lock'
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            proof=place_quiver(helper,monitor,quiver_processes())
            record_change('quiver-existing-window-focused','Reutilizar instância Quiver já aberta no monitor de execução, sem duplicar processos.',proof)
            print(json.dumps(proof,ensure_ascii=False));return 0
        existing=quiver_processes()
        if existing:
            proof=place_quiver(helper,monitor,existing)
            record_change('quiver-existing-window-focused','Reutilizar instância Quiver existente no monitor de execução.',proof)
            print(json.dumps(proof,ensure_ascii=False));return 0
        storage_check();ensure_installation()
        preparation=apply_preparation(preparation_plan(monitor))
        print('[Quiver] Preparação: '+json.dumps(preparation,ensure_ascii=False),flush=True)
        operation_id='quiver-session-'+time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
        op=api.operation_dir(operation_id);op.mkdir(parents=True)
        helper_path=PROJECT_ROOT/'frontend/RetroFE/Tools/launchers/retrofe/window_placement.py'
        manifest={'operation_id':operation_id,'kind':'quiver_launch_session','status':'starting','created_at':api.now(),'monitor':monitor.name,'monitor_reason':reason,'preparation':preparation,'appimage_sha256':APP_SHA256,'arguments':arguments,'items':[],
                  'window_helper_relative':str(helper_path.relative_to(PROJECT_ROOT)),'window_helper_sha256':sha256_file(helper_path),'structurer_sha256':sha256_file(SCRIPT_DIR/'structure-frontend.py')}
        api.atomic_json(op/'manifest.json',manifest)
        manifest['settings_sha256_before']=sha256_file(SCRIPT_DIR/'settings.json')
        try:child=subprocess.Popen([str(APP_IMAGE),*arguments],cwd=SCRIPT_DIR,env={**os.environ,'APPIMAGELAUNCHER_DISABLE':'1'},pass_fds=(fd,))
        except OSError as exc:
            manifest.update(status='launch_failed',error=str(exc));api.atomic_json(op/'manifest.json',manifest);api.rebuild_history_index();raise
        manifest['child_pid']=child.pid;api.atomic_json(op/'manifest.json',manifest)
        try:
            manifest['window']=place_quiver(helper,monitor,[child.pid]);manifest['status']='running_verified'
            print('[Quiver] Monitor confirmado: '+json.dumps(manifest['window'],ensure_ascii=False),flush=True)
        except Exception as exc:
            manifest.update(status='running_monitor_unconfirmed',error=str(exc));print('[Quiver] '+str(exc),file=sys.stderr,flush=True)
        api.atomic_json(op/'manifest.json',manifest);api.append_event(operation_id,'quiver_window_checked',action='monitor_placement',status=manifest['status'],details={k:manifest[k] for k in ['monitor','monitor_reason','window'] if k in manifest});api.rebuild_history_index()
        code=child.wait();manifest.update(status='closed_verified' if manifest.get('window') and code==0 else 'closed_with_issues',exit_code=code,completed_at=api.now(),settings_sha256_after=sha256_file(SCRIPT_DIR/'settings.json'))
        api.atomic_json(op/'manifest.json',manifest);api.append_event(operation_id,'quiver_session_closed',action='supervised_launch',status=manifest['status'],details={'exit_code':code});api.rebuild_history_index()
        return code if code else (0 if manifest.get('window') else 2)
    finally:os.close(fd)


def rollback_startup(operation_id):
    if not operation_id.startswith('quiver-startup-') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in operation_id):raise ValueError('ID de operação Quiver inválido')
    if quiver_processes():raise RuntimeError('Feche o Quiver normalmente antes do rollback')
    api=project_api();path=api.operation_dir(operation_id)/'manifest.json'
    manifest=load_object(path)
    if manifest.get('kind')!='quiver_startup_preparation' or manifest.get('status')!='completed_verified':raise ValueError('Operação não está em estado reversível')
    allowed={SCRIPT_DIR/'settings.json',*shortcut_paths()}
    for item in manifest['items']:
        target=Path(item['path'])
        if target not in allowed or target.is_symlink() or not target.is_file() or sha256_file(target)!=item['after_sha256']:
            raise ValueError('Arquivo mudou após preparação; rollback preservou alterações posteriores: '+str(target))
        if item['backup_relative'] and sha256_file(PROJECT_ROOT/item['backup_relative'])!=item['before_sha256']:raise ValueError('Preimagem de rollback divergente')
    for item in reversed(manifest['items']):
        target=Path(item['path'])
        if item['backup_relative']:atomic_bytes(target,(PROJECT_ROOT/item['backup_relative']).read_bytes(),item['before_mode'])
        else:
            # Move the exact created file into operation rollback, never delete
            # user data or the application/library payload.
            destination=path.parent/'rollback'/('created-'+uuid.uuid4().hex)
            try:os.rename(target,destination)
            except OSError as exc:
                if exc.errno!=errno.EXDEV:raise
                data=target.read_bytes();atomic_bytes(destination,data,0o600)
                if sha256_file(destination)!=item['after_sha256'] or sha256_file(target)!=item['after_sha256']:raise ValueError('Arquivo mudou durante preservação do rollback')
                target.unlink()  # exact newly-created file retained in operation rollback
    manifest.update(status='rolled_back',completed_at=api.now());api.atomic_json(path,manifest);api.append_event(operation_id,'startup_rolled_back',action='quiver_startup',status='verified',details={'permanent_payload_deletion':False});api.rebuild_history_index()
    return {'status':'rolled_back','operation_id':operation_id}


def load_object(path: Path) -> dict:
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError(f"Chave JSON duplicada: {key}")
            result[key]=value
        return result
    payload=json.loads(path.read_text(encoding='utf-8-sig'),object_pairs_hook=unique)
    if not isinstance(payload,dict):raise ValueError(f"JSON precisa ser objeto: {path.name}")
    return payload


def storage_check() -> dict:
    """Read-only guard against Quiver's documented OS fallback/move mechanism."""
    if not SCRIPT_DIR.is_dir() or not os.access(SCRIPT_DIR,os.W_OK|os.X_OK):
        raise ValueError('Pasta do Quiver não é gravável; fallback externo não é permitido')
    for relative in [*DATA_FILES,'games.json','apps.json.lock','Apps','Cache','Backups','Backups/apps','move-data-to-user-folder']:
        path=SCRIPT_DIR/relative
        if path.is_symlink():raise ValueError(f'Dado Quiver por symlink exige revisão: {relative}')
        if path.exists() and relative in {'Apps','Cache','Backups','Backups/apps'} and not path.is_dir():
            raise ValueError(f'Pasta de dados substituída por arquivo: {relative}')
    marker=SCRIPT_DIR/'move-data-to-user-folder'
    if marker.exists():raise ValueError('Migração Quiver para pasta do usuário foi solicitada; início bloqueado para reconciliar sem copiar/apagar dados')
    fallbacks={Path.home()/'.local/share/QuiverLauncher'}
    xdg=os.environ.get('XDG_DATA_HOME')
    if xdg:
        if not Path(xdg).is_absolute():raise ValueError('XDG_DATA_HOME relativo não é suportado')
        fallbacks.add(Path(xdg)/'QuiverLauncher')
    for path in fallbacks:
        if path.resolve()==SCRIPT_DIR:continue
        if path.is_symlink() or path.exists():
            raise ValueError('Estrutura Quiver fora da raiz canônica exige reconciliação: '+str(path))
    settings=load_object(SCRIPT_DIR/'settings.json') if (SCRIPT_DIR/'settings.json').exists() else {}
    value=settings.get('AppsPath','')
    if not isinstance(value,str):raise ValueError('AppsPath precisa ser texto')
    if value.strip():
        location=Path(value)
        location=location if location.is_absolute() else SCRIPT_DIR/location
        if location.resolve()!=(SCRIPT_DIR/'Apps').resolve():raise ValueError('AppsPath fora da pasta oficial Apps; configuração preservada, sem início')
    return {'data_root':str(SCRIPT_DIR),'apps_root':str(SCRIPT_DIR/'Apps'),'migration_pending':False,'external_data_found':False,'apps_path_default':not bool(value.strip())}


def library_status() -> dict:
    storage=storage_check()
    path=SCRIPT_DIR/'apps.json'
    payload=load_object(path) if path.exists() else {'apps':[]}
    if not isinstance(payload.get('apps'),list):raise ValueError('apps.json precisa conter uma lista apps')
    seen=set();entries=[]
    for index,app in enumerate(payload['apps']):
        if not isinstance(app,dict):raise ValueError(f'Entrada {index} não é objeto')
        folder=app.get('folderName')
        if not isinstance(folder,str) or not folder.strip() or folder in {'.','..'} or any(c in folder for c in '/\\\0'):
            raise ValueError(f'Entrada {index} sem folderName seguro e único')
        if folder.casefold() in seen:raise ValueError('folderName duplicado: '+folder)
        seen.add(folder.casefold())
        install=app.get('installPath')
        if install is not None and not isinstance(install,str):raise ValueError('installPath precisa ser texto ou null')
        location=Path(install) if install else SCRIPT_DIR/'Apps'/folder
        if not location.is_absolute():location=SCRIPT_DIR/location
        if location.is_symlink() or not location.resolve().is_relative_to((SCRIPT_DIR/'Apps').resolve()):
            raise ValueError('Instalação fora de Apps ou por symlink exige revisão: '+folder)
        row={'folder_name':folder,'install_relative':location.relative_to(SCRIPT_DIR).as_posix(),'registered':True,'game_launch_verified':False}
        if not location.exists():row['status']='not_installed'
        elif not location.is_dir():raise ValueError('Destino do aplicativo não é pasta: '+folder)
        elif not any(location.iterdir()):row['status']='not_installed'
        else:
            row['status']='installation_present_not_runtime_verified'
            selected=location/'selected_executable.txt'
            if selected.is_symlink():raise ValueError('Seleção de executável por symlink exige revisão: '+folder)
            if selected.exists():
                raw=selected.read_text(encoding='utf-8-sig').strip()
                executable=Path(raw) if raw else location/'__missing_selection__'
                if not executable.is_absolute():executable=location/executable
                if not executable.resolve().is_relative_to(location.resolve()):raise ValueError('Executável externo à instalação exige perfil comprovado: '+folder)
                row['selected_executable_exists']=executable.is_file()
                row['status']='selected_executable_present_not_runtime_verified' if executable.is_file() else 'selected_executable_missing'
                row['absolute_executable_reference']=Path(raw).is_absolute()
        entries.append(row)
    return {**storage,'registered_apps':len(entries),'not_installed':sum(x['status']=='not_installed' for x in entries),'installation_directories_present':sum(x['status']!='not_installed' for x in entries),'entries':entries,'payloads_changed':False}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_appimage(path: Path) -> tuple[str, int, int]:
    if path.is_symlink():
        raise RuntimeError(f"Não aceito AppImage por symlink: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise RuntimeError(f"Não consegui ler o AppImage {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"O AppImage não é um arquivo regular: {path}")
    if info.st_size != APP_SIZE:
        raise RuntimeError(
            f"Tamanho inesperado em {path}: {info.st_size} bytes; esperado {APP_SIZE}. "
            "O arquivo foi preservado."
        )
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
        raise RuntimeError(f"O arquivo não é ELF64 little-endian: {path}")
    if int.from_bytes(header[18:20], "little") != 62:
        raise RuntimeError(f"O AppImage não é x86-64: {path}")
    digest = sha256_file(path)
    if digest != APP_SHA256:
        raise RuntimeError(
            f"SHA-256 divergente em {path}: {digest}; esperado {APP_SHA256}. "
            "O arquivo foi preservado e não será executado."
        )
    return digest, info.st_size, stat.S_IMODE(info.st_mode)


def record_change(change_type: str, reason: str, details: dict[str, object]) -> str:
    if not ROMTOOL.is_file():
        raise RuntimeError(f"Não encontrei o registrador do projeto: {ROMTOOL}")
    command = [
        sys.executable,
        str(ROMTOOL),
        "record-change",
        "--change-type",
        change_type,
        "--reason",
        reason,
        "--details-json",
        json.dumps(details, ensure_ascii=False, separators=(",", ":")),
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            "Não consegui registrar a mudança no histórico do projeto: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    try:
        return str(json.loads(result.stdout).get("operation_id", "recorded"))
    except (json.JSONDecodeError, AttributeError):
        return "recorded"


def installation_dir() -> None:
    if SCRIPT_DIR.is_symlink():
        raise RuntimeError(f"A pasta canônica de instalação é symlink: {SCRIPT_DIR}")
    if not SCRIPT_DIR.exists():
        SCRIPT_DIR.mkdir(parents=True)
    if not SCRIPT_DIR.is_dir() or not os.access(SCRIPT_DIR, os.W_OK | os.X_OK):
        raise RuntimeError(f"A pasta do frontend precisa ser diretório gravável: {SCRIPT_DIR}")


def transfer_verified_source(source_path: Path | None) -> tuple[str, str]:
    """Stage a pinned release, install atomically, then record it before removing a source copy."""
    installation_dir()
    if APP_IMAGE.is_symlink():
        raise RuntimeError(f"O destino de instalação é symlink; não vou substituí-lo: {APP_IMAGE}")
    if APP_IMAGE.exists():
        raise RuntimeError(f"Já existe algo no destino não validado; preservei-o: {APP_IMAGE}")

    source_stat = source_path.stat() if source_path is not None else None
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{APP_NAME}.", suffix=".partial", dir=SCRIPT_DIR
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with ExitStack() as stack:
            if source_path is not None:
                incoming = stack.enter_context(source_path.open("rb"))
                response_url = None
            else:
                request = Request(APP_URL, headers={"User-Agent": "EmulationProject-Quiver-Installer/1.0"})
                response = stack.enter_context(urlopen(request, timeout=60))
                if urlparse(response.geturl()).scheme != "https":
                    raise RuntimeError("O download do GitHub redirecionou para uma URL sem HTTPS.")
                declared_size = response.headers.get("Content-Length")
                if declared_size and int(declared_size) != APP_SIZE:
                    raise RuntimeError(
                        f"O servidor anunciou {declared_size} bytes; o release fixado tem {APP_SIZE}."
                    )
                incoming = response
                response_url = response.geturl()
            output = stack.enter_context(os.fdopen(fd, "wb"))
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > APP_SIZE:
                    raise RuntimeError("O pacote excedeu o tamanho fixado; descartei o temporário.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        actual_digest = digest.hexdigest()
        if size != APP_SIZE or actual_digest != APP_SHA256:
            raise RuntimeError(
                f"O payload não corresponde ao release {APP_RELEASE}: "
                f"bytes={size}, SHA-256={actual_digest}; temporário descartado."
            )
        with temporary.open("rb") as stream:
            header = stream.read(20)
        if (
            len(header) < 20
            or header[:4] != b"\x7fELF"
            or header[4] != 2
            or header[5] != 1
            or int.from_bytes(header[18:20], "little") != 62
        ):
            raise RuntimeError("O download não é um executável ELF64 x86-64; temporário descartado.")
        os.chmod(temporary, 0o755)
        try:
            os.link(temporary, APP_IMAGE)
        except FileExistsError:
            existing_digest, _, _ = verify_appimage(APP_IMAGE)
            temporary.unlink()
            return existing_digest, "already-installed"
        temporary.unlink()

        details = {
            "product": "Quiver Launcher",
            "version": APP_RELEASE,
            "installation_path": str(APP_IMAGE),
            "source_path": str(source_path) if source_path else None,
            "source_url": response_url if source_path is None else None,
            "size_bytes": size,
            "sha256": actual_digest,
            "architecture": "x86_64",
            "rom_payloads_moved_or_created": False,
            "media_payloads_moved_or_created": False,
        }
        try:
            operation_id = record_change(
                "quiver-launcher-installed",
                "Install the pinned Quiver Launcher x64 AppImage in the project's sole frontend location.",
                details,
            )
        except Exception:
            if APP_IMAGE.is_file() and not APP_IMAGE.is_symlink() and sha256_file(APP_IMAGE) == APP_SHA256:
                APP_IMAGE.unlink()
            raise

        if source_path is not None:
            current = source_path.stat() if source_path.exists() else None
            unchanged = (
                source_stat is not None
                and current is not None
                and (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                == (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns)
                and sha256_file(source_path) == APP_SHA256
            )
            if unchanged:
                source_path.unlink()
            else:
                print(
                    f"[PENDÊNCIA] Não removi a fonte porque ela mudou durante a instalação: {source_path}",
                    file=sys.stderr,
                )
        return actual_digest, operation_id
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ensure_installation() -> None:
    if APP_IMAGE.is_symlink():
        raise RuntimeError(f"O AppImage canônico é symlink: {APP_IMAGE}")
    if APP_IMAGE.exists():
        digest, size, mode = verify_appimage(APP_IMAGE)
        if mode & 0o111 == 0:
            os.chmod(APP_IMAGE, 0o755)
            try:
                operation_id = record_change(
                    "quiver-launcher-executable-bit-restored",
                    "Restore executable permissions on the verified Quiver Launcher AppImage.",
                    {
                        "product": "Quiver Launcher",
                        "installation_path": str(APP_IMAGE),
                        "size_bytes": size,
                        "sha256": digest,
                        "previous_mode": oct(mode),
                        "new_mode": "0755",
                    },
                )
            except Exception:
                os.chmod(APP_IMAGE, mode)
                raise
            print(f"[CORRIGIDO] Permissão executável restaurada; registro {operation_id}.")
        print(
            f"[OK] Quiver Launcher {APP_RELEASE}: {APP_IMAGE}; "
            f"{size} bytes; SHA-256 {digest}."
        )
        return

    if DOWNLOAD_CANDIDATE.is_file() and not DOWNLOAD_CANDIDATE.is_symlink():
        try:
            verify_appimage(DOWNLOAD_CANDIDATE)
        except RuntimeError as exc:
            print(f"[REVISÃO] Mantive {DOWNLOAD_CANDIDATE}: {exc}", file=sys.stderr)
        else:
            digest, operation_id = transfer_verified_source(DOWNLOAD_CANDIDATE)
            if operation_id == "already-installed":
                print("[OK] Outra execução instalou o mesmo release; nenhuma cópia criada.")
            else:
                print(
                    f"[INSTALADO] Release {APP_RELEASE} centralizado do Downloads; "
                    f"SHA-256 {digest}; registro {operation_id}."
                )
            return

    digest, operation_id = transfer_verified_source(None)
    if operation_id == "already-installed":
        print("[OK] Outra execução instalou o mesmo release durante o download.")
    else:
        print(
            f"[INSTALADO] Quiver Launcher {APP_RELEASE} baixado do release oficial, "
            f"validado e registrado como {operation_id}; SHA-256 {digest}."
        )


def rom_root_summary() -> tuple[int, int]:
    if ROM_ROOT.is_symlink() or not ROM_ROOT.is_dir():
        return 0, 0
    platform_dirs = 0
    nonempty_dirs = 0
    for child in ROM_ROOT.iterdir():
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        platform_dirs += 1
        try:
            next(child.iterdir())
            nonempty_dirs += 1
        except StopIteration:
            pass
    return platform_dirs, nonempty_dirs


def check_library() -> tuple[bool, str]:
    try:
        status=library_status()
    except (OSError,ValueError,UnicodeDecodeError) as exc:
        return False,str(exc)
    return True,(f"Cadastro válido: {status['registered_apps']} entrada(s); "
                 f"{status['installation_directories_present']} pasta(s) com conteúdo; "
                 f"{status['not_installed']} não instalado(s). Presença de pasta não comprova lançamento; nenhuma alteração aplicada.")


def diagnose() -> int:
    print(f"[Quiver] Instalação canônica: {SCRIPT_DIR}")
    if APP_IMAGE.exists() and not APP_IMAGE.is_symlink():
        digest, size, mode = verify_appimage(APP_IMAGE)
        print(
            f"[OK] AppImage {APP_RELEASE}: {size} bytes; SHA-256 {digest}; "
            f"executável={str(bool(mode & 0o111)).lower()}."
        )
    else:
        print(f"[AÇÃO] AppImage ausente; `--install` baixará {APP_URL}.")
    if DOWNLOAD_CANDIDATE.exists():
        try:
            verify_appimage(DOWNLOAD_CANDIDATE)
            print(f"[OK] Download local verificado: {DOWNLOAD_CANDIDATE}")
        except RuntimeError as exc:
            print(f"[REVISÃO] Download local preservado, release não confirmado: {exc}")
    platform_dirs, nonempty = rom_root_summary()
    print(
        f"[ROMs] Raiz oficial existente: {ROM_ROOT}; {platform_dirs} plataformas, "
        f"{nonempty} não vazias. Não varri nem alterei payloads."
    )
    print(
        "[ROMs] Quiver não oferece ROMDirectory global; o caminho precisa ser "
        "configurado por cada port, conforme sua documentação oficial."
    )
    for directory in DATA_DIRS:
        print(f"[Quiver] Dados {directory}/: {'presente' if (SCRIPT_DIR / directory).exists() else 'a criar pelo app'}")
    valid, message = check_library()
    print(f"[Quiver] {message}")
    return 0 if valid else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--diagnose", action="store_true", help="diagnóstico sem escrita")
    modes.add_argument('--diagnose-json',action='store_true',help='estado local sanitizado de armazenamento/cadastro, sem escrita')
    modes.add_argument("--ensure-installation", action="store_true", help="instala/valida o AppImage fixado")
    modes.add_argument("--check-library", action="store_true", help="valida apps.json sem reescrevê-lo")
    modes.add_argument('--check-startup',action='store_true',help='preflight completo sem escrita')
    modes.add_argument('--prepare-startup',action='store_true',help='prepara configurações iniciais e atalhos, sem abrir')
    modes.add_argument('--prepare-shortcuts',action='store_true',help='mantém os dois atalhos gerenciados, sem alterar preferências')
    modes.add_argument('--rollback-startup',metavar='OPERATION_ID')
    modes.add_argument('--launch',nargs=argparse.REMAINDER,default=None,help='inicia sessão supervisionada no monitor de execução')
    args = parser.parse_args()
    try:
        if args.launch is not None:return launch_quiver(args.launch)
        if args.rollback_startup:
            with startup_mutation_lock():result=rollback_startup(args.rollback_startup)
            print(json.dumps(result,ensure_ascii=False));return 0
        if args.check_startup or args.prepare_startup or args.prepare_shortcuts:
            verify_appimage(APP_IMAGE)
            if quiver_processes() and not args.check_startup:raise RuntimeError('Quiver já está aberto; configuração inicial preservada')
            monitor=None;reason=None
            if not args.prepare_shortcuts:_,monitor,reason=target_monitor()
            plan=preparation_plan(monitor,settings=not args.prepare_shortcuts)
            if args.check_startup:
                print(json.dumps({'status':'preparation_needed' if plan['items'] else 'ready','changes':len(plan['items']),'settings_keys':plan['settings_keys'],'shortcuts':plan['shortcuts'],'monitor':monitor.name,'monitor_reason':reason},ensure_ascii=False,indent=2));return 0
            with startup_mutation_lock():
                if quiver_processes():raise RuntimeError('Quiver já está aberto; preparação preservada')
                result=apply_preparation(preparation_plan(monitor,settings=not args.prepare_shortcuts))
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        if args.diagnose_json:
            digest,size,_=verify_appimage(APP_IMAGE)
            print(json.dumps({'appimage_sha256':digest,'appimage_bytes':size,'library':library_status()},ensure_ascii=False,indent=2));return 0
        if args.diagnose:
            return diagnose()
        if args.ensure_installation:
            storage_check()
            ensure_installation()
            valid, message = check_library()
            print(f"[Quiver] {message}")
            if not valid:
                raise RuntimeError(message)
            return 0
        valid, message = check_library()
        print(f"[Quiver] {message}")
        return 0 if valid else 1
    except Exception as exc:
        print(f"[Quiver] ERRO DE ESTRUTURAÇÃO: {exc}", file=sys.stderr)
        if args.launch is not None and shutil.which('notify-send'):
            subprocess.run(['notify-send','Quiver: início bloqueado',str(exc)[:300]],check=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
