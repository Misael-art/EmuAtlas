"""Linear theme projection: XML only, bundled resources and canonical artwork.

Called by the existing ES-DE structurator. Never extracts/copies font, image,
video or ROM payloads, and never replaces an unmanaged or edited user theme.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET

BASE = 'usr/share/es-de/themes/linear-es-de/'
RESOURCE = ':/../themes/linear-es-de/'
MARKER = '.emulation-managed.json'
OFFSET = 1226208  # Pinned ES-DE 3.5.0 r52; independently checked SQUASHFS header.


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def files_state(folder):
    result = {}
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise ValueError('Tema gerenciado contém symlink inesperado: ' + str(path))
        if path.is_file():
            result[path.relative_to(folder).as_posix()] = digest(path)
    return result


def selected_theme(root):
    path = root / 'frontend/ES-DE/UserData/settings/es_settings.xml'
    if not path.exists():
        return 'linear-es-de'
    if path.is_symlink():
        raise ValueError('Configuração de tema ES-DE é symlink; arquivo preservado')
    text = re.sub(r'<\?xml[^>]*>', '', path.read_text(encoding='utf-8'))
    settings = ET.fromstring('<settings>' + text + '</settings>')
    directories = [e.get('value') for e in settings if e.get('name') == 'UserThemeDirectory']
    if len(directories) > 1:
        raise ValueError('Diretório de temas ES-DE duplicado')
    if directories and directories[0]:
        configured = Path(directories[0]).expanduser()
        if configured.resolve() != (root / 'frontend/ES-DE/UserData/themes').resolve():
            return 'custom-directory-preserved'
    values = [e.get('value') for e in settings if e.get('name') == 'Theme']
    if len(values) > 1:
        raise ValueError('Seleção de tema ES-DE duplicada')
    return values[0] if values else 'linear-es-de'


def project_art(root, *, bundled_logos=frozenset(), bundled_art=frozenset()):
    frontend = root / 'frontend/RetroFE'
    profile = json.loads((frontend / 'Config/profiles/host-linux.json').read_text())
    catalog = json.loads((frontend / 'Manifests/asset-catalog.json').read_text())
    definitions = json.loads((frontend / 'Config/platform-catalog.json').read_text())
    definitions = {s['id']: s for s in definitions['systems']}
    aliases = catalog['main_artwork_aliases']
    result = {}
    for collection, entry in profile['collections'].items():
        sid = entry['rom_subdir']
        theme = definitions.get(sid, {}).get('frontends', {}).get('esde', {}).get('theme', sid)
        if theme != sid:
            continue  # Do not replace shared regional theme artwork with another identity.
        item = {}
        for kind, role in [('logo_aeon_nox_light_white', 'logo'), ('device', 'art')]:
            if sid in (bundled_logos if role == 'logo' else bundled_art):
                continue
            found = [a for a in aliases if a['source_type'] == kind
                     and Path(a['path']).stem.casefold() == collection.casefold()]
            if len(found) > 1:
                raise ValueError('Arte de plataforma ambígua: ' + collection + '/' + kind)
            if found:
                a = found[0];path = frontend / a['target']
                if not path.resolve().is_relative_to(frontend / 'Assets/shared') or digest(path) != a['sha256']:
                    raise ValueError('Arte canônica divergiu: ' + a['target'])
                item[role] = {'path': str(path.relative_to(root)), 'sha256': a['sha256']}
        if item:
            result[sid] = item
    return result


def compile_theme(root, stage):
    """Generate the selected bundled theme's XML, with zero binary duplication."""
    image = root / 'frontend/ES-DE/ES-DE_x64_SteamDeck.AppImage'
    extractor = shutil.which('unsquashfs')
    if not extractor:
        raise ValueError('A projeção de tema exige squashfs-tools (unsquashfs) do instalador oficial')
    with image.open('rb') as stream:
        stream.seek(OFFSET)
        if stream.read(4) != b'hsqs':
            raise ValueError('Offset do pacote ES-DE divergiu; não extrair por suposição')
    listing = subprocess.run([extractor, '-ll', '-o', str(OFFSET), str(image)],
                             capture_output=True, text=True, timeout=20, check=True).stdout
    paths = [line.split('squashfs-root/', 1)[1] for line in listing.splitlines()
             if line.startswith('-') and 'squashfs-root/' + BASE in line]
    if any('..' in PurePosixPath(p).parts or not p.startswith(BASE) for p in paths):
        raise ValueError('Pacote contém caminhos inesperados')
    selected = [p for p in paths if p.endswith('.xml') or Path(p).name in {'LICENSE', 'LICENSE.md', 'CREDITS.md', 'README.md'}]
    subprocess.run([extractor, '-no-progress', '-processors', '1', '-o', str(OFFSET),
                    '-d', str(stage / 'package'), str(image), *selected],
                   capture_output=True, text=True, timeout=30, check=True)
    theme = stage / 'package' / BASE
    if any(p.suffix.lower() not in {'.xml', '.md', ''} for p in theme.rglob('*') if p.is_file()):
        raise ValueError('A projeção deve conter somente XML e créditos')
    logo_ids = {Path(p).stem for p in paths if p.startswith(BASE + 'system/logos/') and p.endswith('.svg')}
    art_ids = {Path(p).stem for p in paths if p.startswith(BASE + 'system/systemart/') and p.endswith('.webp')}
    assets = project_art(root, bundled_logos=logo_ids, bundled_art=art_ids)
    metadata_ids = {Path(p).stem for p in paths if p.startswith(BASE + 'system/metadata/') and p.endswith('.xml')}
    labels = {}
    profile = json.loads((root / 'frontend/RetroFE/Config/profiles/host-linux.json').read_text())
    for collection, entry in profile['collections'].items():
        sid = entry['rom_subdir']
        if sid in metadata_ids:
            continue
        info = root / 'frontend/RetroFE/collections' / collection / 'info.conf'
        if info.is_file():
            values = dict(line.split('=', 1) for line in info.read_text().splitlines()
                          if '=' in line and not line.lstrip().startswith('#'))
            values = {key.strip().casefold(): value.strip() for key, value in values.items()}
            labels[sid] = {dest: values[source] for source, dest in
                           [('manufacturer', 'systemManufacturer'), ('type', 'systemHardwareType'), ('year', 'systemReleaseYear')]
                           if values.get(source)}
    destination = root / 'frontend/ES-DE/UserData/themes/linear-es-de'
    for path in theme.rglob('*.xml'):
        tree = ET.parse(path)
        for node in tree.iter():
            if node.tag != 'include' and node.text and node.text.strip().startswith('./'):
                node.text = RESOURCE + node.text.strip()[2:]
            if node.text and 'system/logos/${system.theme}.svg' in node.text:
                node.text = '${projectSystemLogo}'
            elif node.text and 'system/custom/logo/${system.theme}.svg' in node.text:
                node.text = '${projectSystemLogo}'
            elif node.text and 'system/systemart/${system.theme}.webp' in node.text:
                node.text = '${projectSystemArt}'
        if path == theme / 'theme.xml':
            variables = tree.getroot().find('variables')
            if variables is None:
                raise ValueError('Schema Linear esperado não encontrado')
            # ES-DE ignores empty variables. Keep its ordinary missing-file
            # behavior for unknown identities instead of a fatal resource URI.
            ET.SubElement(variables, 'projectSystemLogo').text = './system/logos/${system.theme}.svg'
            ET.SubElement(variables, 'projectSystemArt').text = './system/systemart/${system.theme}.webp'
        tree.write(path, encoding='utf-8', xml_declaration=True)
    used = {}
    for sid in sorted(logo_ids | art_ids | set(assets)):
        if not re.fullmatch(r'[A-Za-z0-9._-]+', sid):
            raise ValueError('ID de tema inseguro')
        path = theme / 'system/metadata-custom' / (sid + '.xml')
        tree = ET.parse(path) if path.exists() else ET.ElementTree(ET.Element('theme'))
        variables = tree.getroot().find('variables')
        if variables is None:
            variables = ET.SubElement(tree.getroot(), 'variables')
        for role, known, folder, extension, variable in [
            ('logo', logo_ids, 'logos', '.svg', 'projectSystemLogo'),
            ('art', art_ids, 'systemart', '.webp', 'projectSystemArt')]:
            value = RESOURCE + f'system/{folder}/{sid}{extension}' if sid in known else ''
            if not value and role in assets.get(sid, {}):
                asset = assets[sid][role]
                value = os.path.relpath(root / asset['path'], destination).replace(os.sep, '/')
                used.setdefault(sid, {})[role] = asset
            ET.SubElement(variables, variable).text = value
        for key, value in labels.get(sid, {}).items():
            ET.SubElement(variables, key).text = value
        path.parent.mkdir(parents=True, exist_ok=True)
        tree.write(path, encoding='utf-8', xml_declaration=True)
    receipt = {'recipe': 1, 'source_appimage_sha256': digest(image), 'binary_payloads_copied': 0,
               'resource_provider': RESOURCE, 'canonical_art_used': used, 'files': files_state(theme)}
    (theme / MARKER).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    return theme, receipt


def prepare(root, *, apply=False, expected_sha256=None):
    import romtool
    if selected_theme(root) != 'linear-es-de':
        return {'status': 'not_applicable', 'reason': 'Tema escolhido preservado'}
    destination = root / 'frontend/ES-DE/UserData/themes/linear-es-de'
    if destination.is_symlink() or any(p.is_symlink() for p in destination.parents if p.is_relative_to(root)):
        raise ValueError('Caminho de tema contém symlink; preservado')
    if destination.exists():
        marker = destination / MARKER
        if not marker.is_file():
            raise ValueError('Tema Linear pessoal preservado; não é projeção gerenciada')
        previous = json.loads(marker.read_text())
        current = files_state(destination);current.pop(MARKER, None)
        if current != previous['files']:
            raise ValueError('Tema Linear gerenciado foi editado; alterações pessoais preservadas')
    work = root / 'work/roms-consolidation'
    with tempfile.TemporaryDirectory(prefix='esde-theme-', dir=work) as temporary:
        generated, receipt = compile_theme(root, Path(temporary))
        if not expected_sha256 or receipt['source_appimage_sha256'] != expected_sha256:
            raise ValueError('Pacote do tema não corresponde ao pin validado pelo estruturador')
        if destination.exists() and files_state(destination) == files_state(generated):
            return {'status': 'verified', 'binary_payloads_copied': 0, 'files': len(receipt['files'])}
        if not apply:
            return {'status': 'planned', 'files': len(receipt['files']), 'binary_payloads_copied': 0,
                    'canonical_art_used': receipt['canonical_art_used']}
        if any(romtool.frontend_processes_running().values()):
            raise ValueError('Feche os frontends normalmente antes de preparar o tema')
        oid = 'esde-shared-theme-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
        operation = romtool.operation_dir(oid);operation.mkdir()
        manifest = {'operation_id': oid, 'kind': 'esde_shared_theme', 'created_at': romtool.now(),
                    'status': 'applying', 'destination': str(destination.relative_to(root)),
                    'existed_before': destination.exists(), 'source': receipt,
                    'rollback': 'Restore exact prior directory only while after_files matches; preserve user changes.'}
        romtool.atomic_json(operation / 'manifest.json', manifest)
        if destination.exists():
            os.rename(destination, operation / 'before')
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(generated, destination)
            manifest.update(status='completed_verified', completed_at=romtool.now(), after_files=files_state(destination))
            romtool.atomic_json(operation / 'manifest.json', manifest)
            romtool.append_event(oid, 'theme_projected', action='esde_shared_theme', status='completed_verified',
                                 details={'files': len(receipt['files']), 'binary_payloads_copied': 0})
            romtool.rebuild_history_index()
        except BaseException:
            if destination.exists():os.rename(destination, operation / 'failed-generated')
            if (operation / 'before').exists():os.rename(operation / 'before', destination)
            manifest['status']='failed_restored';romtool.atomic_json(operation/'manifest.json',manifest)
            romtool.append_event(oid, 'theme_projection_failed_restored', action='esde_shared_theme', status='failed_restored')
            romtool.rebuild_history_index()
            raise
        return {'status': manifest['status'], 'operation_id': oid, 'binary_payloads_copied': 0}


def rollback(root, oid):
    import romtool
    if Path(oid).name != oid or not oid.startswith('esde-shared-theme-'):
        raise ValueError('ID de operação inválido')
    operation=romtool.operation_dir(oid);manifest=json.loads((operation/'manifest.json').read_text())
    destination=root/'frontend/ES-DE/UserData/themes/linear-es-de'
    if manifest.get('kind')!='esde_shared_theme' or manifest.get('status')!='completed_verified':
        raise ValueError('Estado de rollback inválido')
    if any(romtool.frontend_processes_running().values()) or files_state(destination)!=manifest['after_files']:
        raise ValueError('Tema ativo ou modificado; rollback preservou o ambiente')
    os.rename(destination,operation/'preserved-after')
    if manifest['existed_before']:os.rename(operation/'before',destination)
    manifest.update(status='rolled_back_verified',rolled_back_at=romtool.now())
    romtool.atomic_json(operation/'manifest.json',manifest);romtool.rebuild_history_index()
    return {'operation_id':oid,'status':manifest['status']}
