"""Native overlays for the single platform profile and shared emulator data.

No ROM catalog, payload copy or host-home fallback is maintained here. Native
programs and host-specific settings are distinct; data paths remain canonical.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
PROFILE = Path('frontend/RetroFE/Config/profiles/host-linux.json')
APPS = {
    'org.libretro.RetroArch': 'retroarch', 'org.flycast.Flycast': 'flycast',
    'net.kuribo64.melonDS': 'melonds', 'org.azahar_emu.Azahar': 'azahar',
    'org.DolphinEmu.dolphin-emu': 'dolphin', 'info.cemu.Cemu': 'cemu',
    'net.pcsx2.PCSX2': 'pcsx2', 'org.ppsspp.PPSSPP': 'ppsspp', 'app.xemu.xemu': 'xemu',
}


def package_entry(root: Path, runtime_id: str, platform: str) -> Path | None:
    config = root / 'tools/roms/config/portable-runtimes.json'
    if not config.is_file():
        return None
    recipe = json.loads(config.read_text()).get('packages', {}).get(platform, {}).get(runtime_id)
    if not recipe:
        return None
    path = root / 'emulator' / platform / runtime_id / recipe['entry']
    if path.is_symlink() or not path.is_file():
        return None
    return path


def runtime_id(system: dict) -> str | None:
    adapter = system.get('adapter')
    if adapter == 'retroarch':
        return 'retroarch'
    if adapter in ('mame', 'mame_named'):
        return 'mame'
    if system.get('app_id') in APPS:
        return APPS[system['app_id']]
    if system.get('runtime_id'):
        return {'xenia-canary-linux': 'xenia-canary'}.get(system['runtime_id'], system['runtime_id'])
    command = str(system.get('command', '')).replace('\\', '/')
    parts = command.split('/')
    if 'linux' in parts and 'emulator' in parts:
        index = parts.index('linux') + 1
        if index < len(parts):
            return parts[index]
    return None


def configuration_files(root: Path) -> dict[str, bytes]:
    config = json.loads((root / 'tools/roms/config/portable-runtimes.json').read_text())
    files = {}
    duck_program = (root / 'emulator/windows/duckstation' / config['packages']['windows']['duckstation']['entry']).parent
    shared = root / 'emulator/data/DuckStation/config/duckstation'
    relative = lambda target: os.path.relpath(target, duck_program).replace('\\', '/')
    lines = ['[Main]', 'NoDesktopFile = true', '', '[AutoUpdater]', 'CheckAtStartup = false',
             '', '[BIOS]', 'SearchDirectory = ' + relative(root / 'bios'), '',
             '[MemoryCards]', 'Directory = ' + relative(shared / 'memcards'), '', '[Folders]']
    for key, folder in {'Cache': 'cache', 'Cheats': 'cheats', 'Covers': 'covers', 'GameIcons': 'gameicons',
        'GameSettings': 'gamesettings', 'InputProfiles': 'inputprofiles', 'Overlays': 'overlays', 'Patches': 'patches',
        'SaveStates': 'savestates', 'Screenshots': 'screenshots', 'Shaders': 'shaders',
        'Subchannels': 'subchannels', 'Textures': 'textures', 'UserResources': 'resources', 'Videos': 'videos'}.items():
        lines.append(key + ' = ' + relative(shared / folder))
    files[(duck_program / 'settings.ini').relative_to(root).as_posix()] = ('\n'.join(lines) + '\n').encode()
    files[(duck_program / 'portable.txt').relative_to(root).as_posix()] = b''
    ra = (root / 'emulator/data/RetroArch/retroarch.cfg').read_text()
    ra = ra.replace('emulator/linux/retroarch-cores', 'emulator/windows/retroarch-cores')
    defaults = {'audio_driver': 'wasapi', 'video_driver': 'gl', 'input_driver': 'sdl2', 'input_joypad_driver': 'sdl2',
                'config_save_on_exit': 'false'}
    for key, value in defaults.items():
        ra = re.sub(r'(?m)^\s*' + re.escape(key) + r'\s*=.*\n?', '', ra)
        ra += key + ' = ' + json.dumps(value) + '\n'
    files['emulator/data/RetroArch/retroarch-windows.cfg'] = ra.encode()
    ppsspp = (Path('emulator/windows/ppsspp') / config['packages']['windows']['ppsspp']['entry']).parent
    # PPSSPP resolves this path against the explicitly selected shared cwd.
    files[(ppsspp / 'installed.txt').as_posix()] = b'config/ppsspp\n'
    files['emulator/data/PPSSPP/windows/ppsspp.ini'] = b'[General]\n\n'
    profile = json.loads((root / PROFILE).read_text())
    names = {system['launcher'] for system in profile['collections'].values() if system.get('launcher')}
    names.update(system['launcher'] for system in profile.get('systems', {}).values() if system.get('launcher'))
    for name in sorted(names):
        if '/' in name or '\\' in name:
            raise ValueError('ID de launcher inválido')
        relative = 'frontend/RetroFE/launchers.windows/' + name + '.conf'
        files[relative] = ('# EmuAtlas native Windows launcher; single canonical profile\n'
            'executable = %RETROFE_PATH%/../../emulator/windows/python/python.exe\n'
            'arguments = -X utf8 "%RETROFE_PATH%/Tools/launchers/emulators/host-emulator-launcher.py" '
            '--collection "%ITEM_COLLECTION_NAME%" --item "%ITEM_FILEPATH%"\n'
            'currentDirectory = %RETROFE_PATH%\n').encode()
    return files


def prepare_frontends(root: Path, platform: str, confirmed: bool = False) -> dict:
    """Materialize only the existing managed ES-DE projection for this OS."""
    from frontend_projection import esde_projection, esde_settings_projection
    from native_bindings import configuration_lock, assert_closed
    import romtool
    from types import SimpleNamespace
    with configuration_lock(root):
        profile = native_profile(root, json.loads((root / PROFILE).read_text()), platform)
        candidates, covered = esde_projection(root, profile, platform)
        candidates.append(esde_settings_projection(root))
        changes = []
        for relative, data in candidates:
            path = root / relative
            before = path.read_bytes() if path.is_file() else None
            if data != before:
                changes.append((relative, before, data))
        if not confirmed or not changes:
            return {'status': 'prepared' if changes else 'already_prepared_verified', 'platform': platform,
                    'systems': len(covered), 'files': [str(row[0]) for row in changes]}
        assert_closed(root)
        record = romtool.command_record_change(SimpleNamespace(change_type='native_frontend_command_projection',
            reason='Projetar os comandos nativos do SO a partir do perfil/catálogo únicos, preservando comandos pessoais',
            details_json=json.dumps({'platform': platform, 'status': 'prepared', 'systems': len(covered),
                                      'files': [str(row[0]) for row in changes]})))
        opid = record['operation_id']
        record.update(status='prepared', completed_at=None)
        romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json', record)
        items = []
        for relative, before, after in changes:
            path = root / relative
            if (path.read_bytes() if path.exists() else None) != before:
                raise ValueError('Projeção mudou durante a preparação: ' + str(relative))
            backup = romtool.operation_dir(opid) / 'rollback/files' / relative
            if before is not None:
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(before)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.native-tmp')
            with temporary.open('xb') as stream:
                stream.write(after)
            if before is not None:
                temporary.chmod(path.stat().st_mode & 0o777)
            os.replace(temporary, path)
            items.append({'path': str(relative), 'sha256_before': hashlib.sha256(before).hexdigest() if before is not None else None,
                          'sha256_after': hashlib.sha256(after).hexdigest()})
            record['details'].update(status='partially_applied', items=items)
            record['status'] = 'partially_applied'
            romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json', record)
        record['details'].update(status='completed_verified', items=items)
        record.update(status='completed_verified', completed_at=romtool.now())
        romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json', record)
        romtool.append_event(opid, 'native_frontend_projection_verified', action='native_frontend_command_projection', status='completed_verified')
        romtool.rebuild_history_index()
        return {'operation_id': opid, 'status': 'completed_verified', 'platform': platform, 'systems': len(covered)}


def rollback_frontends(root: Path, operation_id: str, confirmed: bool = False) -> dict:
    from native_bindings import configuration_lock, assert_closed, safe_path
    import romtool
    if not confirmed:
        raise ValueError('Rollback exige --apply')
    with configuration_lock(root):
        assert_closed(root)
        manifest = romtool.operation_dir(operation_id) / 'manifest.json'
        record = json.loads(manifest.read_text())
        if record.get('change_type') != 'native_frontend_command_projection' or record['details']['status'] not in ('completed_verified', 'partially_applied'):
            raise ValueError('Operação não é uma projeção nativa concluída')
        items = record['details']['items']
        for item in items:
            path = safe_path(root, item['path'])
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256_after']:
                raise ValueError('Projeção mudou; reverta primeiro a configuração dependente: ' + str(path))
            if item['sha256_before'] is not None:
                backup = manifest.parent / 'rollback/files' / item['path']
                if hashlib.sha256(backup.read_bytes()).hexdigest() != item['sha256_before']:
                    raise ValueError('Preimagem divergente: ' + item['path'])
        for item in reversed(items):
            path = safe_path(root, item['path'])
            preserved = manifest.parent / 'rollback/applied-files' / item['path']
            preserved.parent.mkdir(parents=True, exist_ok=True)
            if preserved.exists():
                raise ValueError('Destino de preservação já existe')
            path.rename(preserved)
            if item['sha256_before'] is not None:
                path.write_bytes((manifest.parent / 'rollback/files' / item['path']).read_bytes())
        record['details']['status'] = 'rolled_back'
        record['status'] = 'rolled_back'
        romtool.atomic_json(manifest, record)
        romtool.append_event(operation_id, 'native_frontend_projection_rolled_back', status='rolled_back')
        romtool.rebuild_history_index()
        return {'operation_id': operation_id, 'status': 'rolled_back'}


def assert_native_ready(root: Path, rid: str) -> None:
    if os.name != 'nt':
        return
    from native_bindings import binding_rows
    blocked = [row for row in binding_rows(root, rid) if row['status'] != 'verified']
    if blocked:
        raise ValueError('Pastas compartilhadas ainda não preparadas; execute emulator\\launch.cmd --prepare antes de abrir o frontend: ' + json.dumps(blocked))
    config = json.loads((root / 'tools/roms/config/portable-runtimes.json').read_text())
    if rid in ('duckstation', 'ppsspp'):
        parent = (root / 'emulator/windows' / rid / config['packages']['windows'][rid]['entry']).parent
        required = ['portable.txt', 'settings.ini'] if rid == 'duckstation' else ['installed.txt']
        if any(not (parent / name).is_file() or (parent / name).is_symlink() for name in required):
            raise ValueError('Configuração portátil ausente; prepare as configurações Windows pelo emutool')
        if rid == 'ppsspp' and (parent / 'installed.txt').read_text().strip() != 'config/ppsspp':
            raise ValueError('Memstick PPSSPP personalizado preservado; requer revisão')


def native_profile(root: Path, original: dict, platform: str | None = None) -> dict:
    platform = platform or ('windows' if os.name == 'nt' else 'linux')
    profile = copy.deepcopy(original)
    ra = profile.get('retroarch', {})
    native_ra = package_entry(root, 'retroarch', platform)
    installed_flatpaks = set()
    if platform == 'linux' and shutil.which('flatpak') and os.environ.get('EMUATLAS_USE_PORTABLE') != '1':
        try:
            result = subprocess.run(['flatpak', 'list', '--app', '--columns=application'], capture_output=True, text=True, timeout=15)
            installed_flatpaks = set(result.stdout.splitlines()) if result.returncode == 0 else set(APPS)
        except (OSError, subprocess.TimeoutExpired):
            installed_flatpaks = set(APPS)
    if native_ra and 'org.libretro.RetroArch' not in installed_flatpaks:
        ra['executable'] = str(native_ra)
    if platform == 'linux':
        config_path = root / 'tools/roms/config/portable-runtimes.json'
        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
        for app_id, rid in APPS.items():
            recipe = config.get('runtimes', {}).get(rid)
            entry = (root / 'emulator/linux' / recipe.get('install_dir', rid) / recipe['entry']) if recipe else package_entry(root, rid, 'linux')
            if entry and entry.is_file() and app_id in profile.get('flatpak_apps', {}) and app_id not in installed_flatpaks:
                profile['flatpak_apps'][app_id]['executable'] = str(entry)
        return profile
    if platform != 'windows':
        raise ValueError('Host sem suporte nativo')
    mame = package_entry(root, 'mame', 'windows')
    profile['mame_executable'] = str(mame) if mame else 'missing-native-mame.exe'
    profile['mame_config_subdir'] = 'cfg-windows'
    ra['core_root'] = '../../emulator/windows/retroarch-cores'
    ra['config_path'] = '../../emulator/data/RetroArch/retroarch-windows.cfg'
    contracts = json.loads((root / 'tools/roms/config/portable-runtimes.json').read_text()).get('windows_launch', {})

    def adapt(system):
        rid = runtime_id(system)
        if system.get('rom_subdir') == 'model2':
            rid = 'model2'
            system.update(adapter='command', runtime_id=rid, userdata_subdir='Model2')
        if system.get('adapter') == 'unavailable':
            return
        executable = package_entry(root, rid or '', 'windows')
        if not executable and not system.get('runtime_options'):
            system.update(adapter='unavailable', reason=f'Runtime Windows nativo ausente: {rid}')
            return
        if system.get('adapter') == 'retroarch':
            system['core'] = system['core'].removesuffix('.so') + '.dll'
            system['core_root'] = '../../emulator/windows/retroarch-cores'
            if not (root / 'emulator/windows/retroarch-cores' / system['core']).is_file():
                system.update(adapter='unavailable', reason='Núcleo Windows ausente: ' + system['core'])
            return
        if system.get('adapter') in ('mame', 'mame_named'):
            return
        contract = contracts.get(rid)
        if not contract and system.get('runtime_options'):
            contract = {'args': system.get('args', ['{item}'])}
            executable = Path('missing-native-' + str(rid) + '.exe')
        if not contract:
            system.update(adapter='unavailable', reason=f'Compartilhamento de dados Windows ainda sem contrato validado: {rid}')
            return
        system.update(adapter='command', command=str(executable), args=contract['args'],
                      environment=contract.get('environment', {}), native_runtime_id=rid)
        system.pop('command_env', None)
        if system.get('app_id') in APPS:
            system['adapter'] = 'native_app'
            profile['flatpak_apps'][system['app_id']]['executable'] = str(executable)
        if contract.get('working_directory'):
            system['working_directory'] = contract['working_directory']
        else:
            system.pop('working_directory', None)
        for option_id, option in system.get('runtime_options', {}).items():
            option_rid = {'xenia-canary-linux': 'xenia-canary'}.get(option_id, option_id)
            option_exe = package_entry(root, option_rid, 'windows')
            option_contract = contracts.get(option_rid)
            if option_exe and option_contract:
                option.update(command=str(option_exe), args=option_contract['args'],
                              environment=option_contract.get('environment', {}), native_runtime_id=option_rid)
                if option_contract.get('working_directory'):
                    option['working_directory'] = option_contract['working_directory']
            else:
                option['command'] = 'missing-native-' + option_rid + '.exe'
                option['limitations'] = ['No native Windows data-isolation contract']
    for system in profile.get('collections', {}).values():
        adapt(system)
        # Windows has explicit native defaults for platforms whose Linux
        # primary runtime has no verified Windows package. Per-title choices
        # remain in the single shared selection file and still take precedence.
        windows_default = {'ps5': 'sharpemu', 'switch2': 'eglantine'}.get(system.get('rom_subdir'))
        options = system.get('runtime_options', {})
        if windows_default and str(system.get('command', '')).startswith('missing-native-') and windows_default in options:
            previous_id = system['runtime_id']
            previous = {key: copy.deepcopy(value) for key, value in system.items() if key != 'runtime_options'}
            selected = options.pop(windows_default)
            options[previous_id] = previous
            system.update(selected)
            system['runtime_id'] = windows_default
            system['runtime_options'] = options
    return profile


def diagnose(root: Path, platform: str) -> dict:
    profile = native_profile(root, json.loads((root / PROFILE).read_text()), platform)
    rows = []
    for name, system in profile.get('collections', {}).items():
        rid = runtime_id(system)
        rows.append({'collection': name, 'system_id': system.get('rom_subdir'),
                     'runtime': rid, 'adapter': system.get('adapter'),
                     'reason': system.get('reason') if system.get('adapter') == 'unavailable' else None})
    return {'platform': platform, 'project_root': str(root), 'shared_userdata': 'emulator/data',
            'roms': 'roms', 'bios': 'bios', 'media': 'media',
            'gameplay_tested': False, 'collections': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Launcher comum para a biblioteca portátil Linux/Windows')
    parser.add_argument('--check', action='store_true', help='diagnóstico somente leitura, sem abrir GUI')
    parser.add_argument('--platform', choices=['linux', 'windows'], help='somente para diagnóstico')
    parser.add_argument('--system-id')
    parser.add_argument('--collection')
    parser.add_argument('--item')
    parser.add_argument('--runtime')
    parser.add_argument('--prepare', action='store_true', help='planeja vínculos Windows para os dados compartilhados')
    parser.add_argument('--apply', action='store_true', help='aplica a preparação explícita, com registro e sem copiar dados')
    parser.add_argument('--rollback-bindings', metavar='OPERATION_ID')
    parser.add_argument('--rollback-frontends', metavar='OPERATION_ID')
    parser.add_argument('--prepare-frontends', action='store_true', help='projeta os comandos gerenciados do ES-DE para o SO atual')
    parser.add_argument('--display', choices=['profile', 'fullscreen', 'windowed'], default='profile')
    args = parser.parse_args(argv)
    platform = 'windows' if os.name == 'nt' else 'linux'
    if args.rollback_frontends:
        print(json.dumps(rollback_frontends(ROOT, args.rollback_frontends, args.apply), ensure_ascii=False, indent=2))
        return 0
    if args.prepare_frontends:
        if args.platform and args.platform != platform and args.apply:
            parser.error('A projeção ativa só pode ser aplicada no SO nativo; --platform permite revisão sem --apply')
        print(json.dumps(prepare_frontends(ROOT, args.platform or platform, args.apply), ensure_ascii=False, indent=2))
        return 0
    if args.rollback_bindings:
        from native_bindings import rollback
        print(json.dumps(rollback(ROOT, args.rollback_bindings, args.apply), ensure_ascii=False, indent=2))
        return 0
    if args.prepare:
        from native_bindings import prepare
        try:
            print(json.dumps({'bindings': prepare(ROOT, args.apply),
                              'frontends': prepare_frontends(ROOT, platform, args.apply)}, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.check:
        print(json.dumps(diagnose(ROOT, args.platform or platform), ensure_ascii=False, indent=2))
        return 0
    if args.platform and args.platform != platform:
        parser.error('Não executamos binários de outro SO; --platform é diagnóstico')
    if args.item and not (args.collection or args.system_id):
        try:
            item = Path(args.item).expanduser().resolve(strict=True)
            relative = item.relative_to((ROOT / 'roms').resolve())
            if len(relative.parts) < 2:
                raise ValueError('selecione um jogo')
            args.item, args.system_id = str(item), relative.parts[0]
        except (OSError, ValueError):
            parser.error('O item deve existir em roms/<system_id>/ para inferir a plataforma')
    if not args.item or not (args.collection or args.system_id):
        parser.error('Lançamento exige --item e --system-id ou --collection')
    profile = json.loads((ROOT / PROFILE).read_text())
    collection = args.collection
    if not collection:
        matches = [name for name, system in profile['collections'].items() if system.get('rom_subdir') == args.system_id]
        if len(matches) != 1:
            parser.error('Plataforma sem coleção única no perfil canônico')
        collection = matches[0]
    launcher = ROOT / 'frontend/RetroFE/Tools/launchers/emulators/host-emulator-launcher.py'
    command = [sys.executable, '-X', 'utf8', str(launcher), '--collection', collection, '--item', args.item, '--display', args.display]
    if args.runtime:
        command += ['--runtime', args.runtime]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
