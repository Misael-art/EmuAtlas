"""Create missing local configuration; preserve every existing customer file."""
from pathlib import Path
import json
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import workspace as w


def bind_available_runtimes(engine):
    """Only rewrite an untouched template or a profile whose last managed hash matches."""
    root = engine.root
    path = w.safe_path(root, 'frontend/RetroFE/Config/profiles/host-linux.json')
    template = w.read(w.HERE / 'templates/frontend/RetroFE/Config/profiles/host-linux.json')
    current = w.read(path)
    state = w.safe_path(engine.state, 'managed-profile.json')
    if current != template and (not state.exists() or w.read(state).get('sha256') != w.digest(path)):
        return {'status': 'preserved_customer_profile'}
    selected = json.loads(json.dumps(template))
    registry = w.read(w.HERE / 'templates/tools/roms/config/portable-runtimes.json')
    bindings = []
    for name, collection in selected.get('collections', {}).items():
        if collection.get('adapter') != 'command':
            continue
        for recipe in registry['runtimes'].values():
            if not recipe.get('profile_command') or recipe['profile_command'] != collection.get('command'):
                continue
            local = (root / 'frontend/RetroFE' / recipe['profile_command']).resolve()
            if local.is_file():
                break
            app = recipe.get('flatpak_app_id')
            if app and app in selected.get('flatpak_apps', {}) and engine.available_flatpak(app):
                collection['adapter'] = 'flatpak'
                collection['app_id'] = app
                collection['command'] = selected['flatpak_apps'][app]['command']
                bindings.append({'collection': name, 'flatpak': app})
                break
            for command in recipe.get('host_commands', []):
                found = shutil.which(command)
                if found and not Path(found).resolve().is_relative_to(root):
                    collection['command'] = command
                    bindings.append({'collection': name, 'native': command})
                    break
            break
    if selected != current:
        backup = engine.state / 'backups' / ('profile.' + w.digest(path) + '.json')
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
        temporary = path.with_name('host-linux.setup.tmp')
        with temporary.open('x') as out:
            out.write(json.dumps(selected, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, path)
    state.parent.mkdir(parents=True, exist_ok=True)
    temporary = state.with_name('managed-profile.setup.tmp')
    with temporary.open('x') as out:
        out.write(json.dumps({'sha256': w.digest(path), 'bindings': bindings}, indent=2) + '\n')
    os.replace(temporary, state)
    return {'status': 'configured', 'bindings': bindings}


def configure(engine):
    root = engine.root
    if not (root / w.MARKER).exists() and (root / 'frontend/RetroFE/settings.conf').is_file():
        return {'status': 'reused', 'existing_customer_configuration_preserved': True}
    created, preserved = [], []
    bindings = bind_available_runtimes(engine)
    def write(name, text):
        p = w.safe_path(root, name)
        if p.exists():
            preserved.append(name)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('x') as out:
            out.write(text)
        created.append(name)

    schema = w.read(w.HERE / 'config/state-schemas.json')
    state_root = schema['legacy_state_directory']
    for name, fields in schema['csv_fields'].items():
        buffer = io.StringIO()
        csv.writer(buffer).writerow(fields)
        write(state_root + '/' + name, buffer.getvalue())
    state_name = state_root + '/estado-final-verificacao.json'
    if not (root / state_name).exists():
        w.write_new(w.safe_path(root, state_name), {'schema_version': 1, 'current_catalog_rows': 0,
                                                   'installation': 'empty customer workspace'})
        created.append(state_name)

    fe = 'frontend/RetroFE/'
    write(fe + 'settings.conf', 'firstCollection = Main\nbaseItemPath = ../../roms\nlayout = Workspace\nfullscreen = no\nhorizontal = 1920\nvertical = 1080\n')
    write(fe + 'controls.conf', 'up = Up,joy0Hat0Up\ndown = Down,joy0Hat0Down\nleft = Left,joy0Hat0Left\nright = Right,joy0Hat0Right\nselect = Return,joy0Button0\nback = Escape,joy0Button1\nquit = Q\n')
    write('emulator/data/RetroArch/retroarch.cfg',
          '# Initial portable defaults; launch adapter resolves BIOS/core/save paths.\n'
          'config_save_on_exit = "false"\ninput_autodetect_enable = "true"\n')
    write(fe + 'Config/display.env', 'RETROFE_WINDOW_WIDTH=1920\nRETROFE_WINDOW_HEIGHT=1080\nRETROFE_FULLSCREEN=no\n')
    profile = w.read(root / fe / 'Config/profiles/host-linux.json')
    eden = root / 'emulator/linux/eden-nx2/build/bin/eden'
    if eden.is_file():
        info = {'version': 'nx2-50fa86bb / automated local build', 'entry': 'build/bin/eden',
                'entry_sha256': w.digest(eden),
                'source_sha256': '8ec96a9b8ad36b1756bd40c4c10216c552a32657504e059d1a43c2e37c535b4a',
                'cpu': 'x86-64-v3', 'gameplay_tested': False}
        name = 'emulator/linux/eden-nx2/build-info.json'
        if not (root / name).exists():
            w.write_new(w.safe_path(root, name), info)
            created.append(name)
    # Root profile already uses project-local BIOS/userdata. Derive a minimal clean
    # library without copying lists, metadata, art or settings from the developer.
    names = []
    for name, item in profile.get('collections', {}).items():
        if not re.fullmatch(r'[\w ().+-]+', name) or name in {'.', '..'}:
            raise ValueError('Nome de coleção inválido')
        if item.get('adapter') == 'unavailable':
            continue
        system = item.get('rom_subdir')
        if not system or '/' in system or '..' in system:
            continue
        names.append(name)
        launcher = 'Workspace - ' + name
        write(fe + f'collections/{name}/settings.conf',
              f'list.path = %BASE_ITEM_PATH%/{system}\nlist.extensions = {item.get("extensions", "zip,7z")}\nlauncher = {launcher}\n'
              f'media.cover = ../../media/{system}/cover-front\nmedia.screenshot = ../../media/{system}/screenshot\nmedia.video = ../../media/{system}/video\nmedia.story = ../../media/{system}/story\n')
        w.safe_path(root, 'roms/' + system).mkdir(parents=True, exist_ok=True)
        # Launcher takes the collection name and content path as argv, not shell text.
        write(fe + f'launchers.linux/{launcher}.conf',
              'executable = python3\narguments = "%RETROFE_PATH%/Tools/launchers/emulators/host-emulator-launcher.py" --collection "%ITEM_COLLECTION_NAME%" --item "%ITEM_FILEPATH%"\ncurrentDirectory = %RETROFE_PATH%\n')
    write(fe + 'collections/Main/menu.txt', '\n'.join(names) + '\n')
    # An intentionally minimal, first-party text fallback, not a copy of Aeon Nox.
    font = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    if os.path.exists('/usr/share/fonts/TTF/DejaVuSans.ttf'):
        font = '/usr/share/fonts/TTF/DejaVuSans.ttf'
    if not Path(font).is_file() and shutil.which('fc-match'):
        result = subprocess.run(['fc-match', '-f', '%{file}', 'DejaVu Sans'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and Path(result.stdout).is_file():
            font = result.stdout
    write(fe + 'layouts/Workspace/layout.xml',
          f'<layout width="1920" height="1080" font="{font}" loadFontSize="32" fontColor="b8c6d1">'
          '<text value="EMULATION WORKSPACE" x="100" y="60" fontSize="42"/>'
          '<menu type="custom" orientation="vertical" scrollTime=".18"><itemDefaults x="140" y="430" fontSize="32" fontColor="b8c6d1"/>'
          '<item yOffset="-180" alpha=".3"/><item yOffset="-90" alpha=".6"/><item yOffset="0" alpha="1"/><item yOffset="90" alpha=".6"/><item yOffset="180" alpha=".3"/></menu>'
          '<text value="Enter: escolher    Esc: voltar    Q: sair" x="100" y="980" fontSize="26" fontColor="8199a8"/></layout>\n')
    write(fe + 'layouts/Workspace/splash.xml',
          f'<layout width="1920" height="1080" font="{font}" loadFontSize="32" fontColor="b8c6d1">'
          '<text value="Carregando biblioteca" x="100" y="460" fontSize="36"/>'
          '<statusText x="100" y="540" fontSize="26" fontColor="8199a8"/></layout>\n')
    esde = 'frontend/ES-DE/UserData/'
    # XML values must be escaped for roots with quotes or special characters.
    cfg = ET.Element('config')
    for key, value in [('ROMDirectory', root / 'roms'), ('MediaDirectory', root / 'media')]:
        ET.SubElement(cfg, 'string', {'name': key, 'value': str(value)})
    write(esde + 'settings/es_settings.xml', ET.tostring(cfg, encoding='unicode') + '\n')
    # Make project-local Python tools available to the existing wrappers.
    venv = root / 'tools/distribution/.venv/bin'
    for name in ['xdftool', 'xdfscan']:
        p = w.safe_path(root, 'tools/roms/bin/' + name)
        if p.exists():
            preserved.append(str(p.relative_to(root)))
        elif (venv / name).is_file():
            write(str(p.relative_to(root)), '#!/bin/sh\nSCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$SCRIPT_DIR/../../distribution/.venv/bin/python" -c \'from amitools.tools.' + name + ' import main; raise SystemExit(main())\' "$@"\n')
            p.chmod(0o755)
    return {'status': 'configured', 'created': created, 'existing_preserved': preserved,
            'theme': 'Workspace fallback; Aeon Nox assets not redistributed', 'runtime_bindings': bindings, 'gameplay_verified': False}
