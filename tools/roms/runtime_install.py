"""Install pinned official builds into emulator/<platform>/<id>.

Recipes live in tools/roms/config/portable-runtimes.json. Every source is pinned by size and
SHA-256; downloads stream into a staging directory on the same filesystem and replace only the
recipe's entry file after verification. The previous entry is kept in the operation's rollback
folder. Downloads above the manual limit are never fetched: the user places the official file
in emulator/.incoming/ and the installer verifies it the same way.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import romtool
except ModuleNotFoundError:  # imported as tools.roms.runtime_install (tests)
    from tools.roms import romtool

CHANGE_TYPE = "portable_runtime_install"
ROLLBACK_CHANGE_TYPE = "portable_runtime_rollback"


def _library_tree(directory: Path) -> list[dict]:
    """Complete inventory, including link literals; never follows directory links."""
    def unreadable(error):
        raise error

    if directory.is_file() and not directory.is_symlink():
        return [{'path': '', 'kind': 'file', 'size_bytes': directory.stat().st_size,
                 'sha256': _sha256(directory), 'mode': directory.stat().st_mode & 0o777}]
    rows = []
    for parent, directories, files in os.walk(directory, followlinks=False, onerror=unreadable):
        for name in sorted(directories + files):
            path = Path(parent) / name
            relative = path.relative_to(directory).as_posix()
            if path.is_symlink():
                rows.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
            elif path.is_file():
                mode = path.stat().st_mode & 0o777
                try:
                    digest = _sha256(path)
                except PermissionError:
                    if path.stat().st_uid != os.getuid():
                        raise
                    path.chmod(mode | 0o400)
                    try:
                        digest = _sha256(path)
                    finally:
                        path.chmod(mode)
                rows.append({"path": relative, "kind": "file", "size_bytes": path.stat().st_size,
                             "sha256": digest, "mode": mode})
            elif path.is_dir():
                rows.append({"path": relative, "kind": "directory"})
            else:
                raise ValueError(f"Arquivo especial preservado: {path}")
    return sorted(rows, key=lambda row: row["path"])


def _sync_library_copy(directory: Path, inventory: list[dict]) -> None:
    """Flush copied bytes and directory entries before publishing their root."""
    parents = {directory.parent}
    for row in inventory:
        path = directory / row['path'] if row['path'] else directory
        if row['kind'] == 'file':
            mode = path.stat().st_mode & 0o777
            if not mode & 0o400:
                path.chmod(mode | 0o400)
            try:
                with path.open('rb') as stream:
                    if not mode & 0o400:
                        path.chmod(mode)
                    os.fsync(stream.fileno())
            finally:
                if not mode & 0o400:
                    path.chmod(mode)
            parents.add(path.parent)
        elif row['kind'] == 'directory':
            parents.add(path)
    if directory.is_dir():
        parents.add(directory)
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        romtool.sync_directory_chain(parent)


def _rename_library_path(root: Path, source: Path, destination: Path, move: dict,
                         manifest: dict, opdir: Path, phase: str) -> None:
    """Retain root permissions and ownership evidence across an interrupted rename."""
    for path in (source, destination):
        _library_target(root, path.relative_to(root).as_posix())
    paths = {'source': source.relative_to(root).as_posix(),
             'destination': destination.relative_to(root).as_posix()}
    checkpoints = move.setdefault('rename_checkpoints', {})
    checkpoint = checkpoints.get(phase)
    if checkpoint is None:
        info = source.stat()
        checkpoint = {**paths, 'mode': info.st_mode & 0o777,
                      'directory': source.is_dir(), 'device': info.st_dev, 'inode': info.st_ino,
                      'filesystem_id': os.statvfs(source).f_fsid}
        checkpoints[phase] = checkpoint
        romtool.atomic_json(opdir / 'manifest.json', manifest)
    if any(checkpoint[key] != value for key, value in paths.items()):
        raise ValueError('Caminhos do checkpoint de rename divergentes')
    if source.exists() and destination.exists():
        raise ValueError('Destino de rename ocupado: ' + str(destination))
    current = source if source.exists() else destination
    info = current.stat()
    if (os.statvfs(current).f_fsid, info.st_ino) != (checkpoint['filesystem_id'], checkpoint['inode']):
        raise ValueError('Inode do rename mudou: ' + str(current))
    mode = checkpoint['mode']
    temporary_mode = mode | 0o200 if checkpoint['directory'] else mode
    if (info.st_mode & 0o777) not in {mode, temporary_mode}:
        raise ValueError('Permissões do rename mudaram: ' + str(current))
    if source.exists():
        try:
            if checkpoint['directory']:
                source.chmod(temporary_mode)
            _library_rename_exclusive(source, destination)
        finally:
            for path in (source, destination):
                if path.exists():
                    info = path.stat()
                    if (os.statvfs(path).f_fsid, info.st_ino) == (checkpoint['filesystem_id'], checkpoint['inode']):
                        path.chmod(mode)
    elif checkpoint['directory']:
        destination.chmod(mode)
    if checkpoint['directory']:
        romtool.sync_directory_chain(destination)
    source_parent = source.parent
    while not source_parent.exists():
        source_parent = source_parent.parent
    romtool.sync_directory_chain(source_parent)
    romtool.sync_directory_chain(destination.parent)
    checkpoint['complete'] = True
    romtool.atomic_json(opdir / 'manifest.json', manifest)


def _library_rename_exclusive(source: Path, destination: Path) -> None:
    rename = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOTSUP, 'renameat2 indisponível; original preservado')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source), None, str(destination))


def _library_guard(root: Path) -> None:
    running = romtool.frontend_processes_running()
    if any(running.values()):
        raise ValueError(f"Encerre normalmente os frontends antes da migração: {running}")
    # Read executable identities, never signal another task or trust a lock file alone.
    for process in Path('/proc').glob('[0-9]*'):
        try:
            executable = (process / 'exe').resolve(strict=True)
        except OSError:
            continue
        if any(executable.is_relative_to(root / prefix) for prefix in
               ('tools/roms/runtimes', 'emulator')):
            raise ValueError(f"Runtime ativo PID {process.name}: {executable}; encerre-o normalmente")
        if executable.name in ('dolphin-emu', 'dolphin-emu-nogui'):
            raise ValueError('Dolphin ativo; encerre-o normalmente antes de migrar seus dados')


def _library_target(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f"Caminho inválido no plano: {value}")
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink preservado: {cursor}")
    return cursor


def _library_source(root: Path, move: dict) -> Path:
    if move.get('external_source'):
        app = move.get('external_app', 'org.DolphinEmu.dolphin-emu')
        allowed = {'org.DolphinEmu.dolphin-emu': (('data', 'dolphin-emu'), ('config', 'dolphin-emu')),
                   'org.flycast.Flycast': (('data', 'flycast'),),
                   'net.pcsx2.PCSX2': (('config', 'PCSX2'),)}
        base = Path.home() / '.var/app' / app
        relative = Path(move['external_source'])
        if app not in allowed or relative.is_absolute() or '..' in relative.parts or relative.parts[:2] not in allowed[app]:
            raise ValueError('Fonte externa fora do perfil de emulação selecionado')
        path = base / relative
        for component in [path, *path.parents]:
            if component == Path.home():
                break
            if component.is_symlink():
                raise ValueError('Fonte externa por symlink preservada')
        if move.get('source_absolute') != str(path):
            raise ValueError('A importação/reversão do perfil externo requer o host de origem')
        return path
    return _library_target(root, move['source'])


def library_plan(root: Path) -> dict:
    """One-time, reviewable migration; no aliases and no copies of runtime payloads."""
    import datetime
    import uuid
    operation_id = 'emulator-library-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    opdir = romtool.operation_dir(operation_id)
    opdir.mkdir(parents=True)
    legacy = root / 'tools/roms/runtimes'
    moves = []
    for source in sorted(legacy.iterdir()) if legacy.is_dir() else []:
        if source.name.startswith('.') or source.name == 'zarchive':
            continue
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"Origem de runtime inesperada: {source}")
        platform = 'windows' if source.name == 'xenia-canary' else 'linux'
        destination = root / 'emulator' / platform / source.name
        _library_target(root, str(destination.relative_to(root)))
        if destination.exists():
            raise ValueError(f"Destino existente preservado: {destination}")
        moves.append({'source': source.relative_to(root).as_posix(),
                      'destination': destination.relative_to(root).as_posix(), 'files': _library_tree(source)})

    # Frontends consume the same emulator data; only the frontend's own state
    # stays in its UserData directory. No compatibility symlink is created.
    legacy_data = root / 'frontend/RetroFE/UserData'
    data_names = {'Azahar', 'Cemu', 'Dolphin', 'DuckStation', 'Eden-NX2', 'Eglantine',
                  'EmulatorSelections', 'Flycast', 'MAME', 'MelonDS', 'PCSX2', 'PPSSPP',
                  'PS5PCEM', 'KytyPS5', 'ROMCache', 'RPCS3', 'RetroArch', 'SharpEmu',
                  'Xenia', 'shadPS4', 'xemu'}
    for name in sorted(data_names):
        source = legacy_data / name
        if not source.exists():
            continue
        destination = root / 'emulator/data' / name
        _library_target(root, str(source.relative_to(root)))
        _library_target(root, str(destination.relative_to(root)))
        if not source.is_dir() or destination.exists():
            raise ValueError(f'Origem/destino de dados ambíguo: {source} -> {destination}')
        moves.append({'source': source.relative_to(root).as_posix(),
                      'destination': destination.relative_to(root).as_posix(), 'files': _library_tree(source)})

    # Search active consumers, installers and fixtures. Histories, receipts, build
    # intermediates and the repository export are immutable evidence, not consumers.
    scopes = ['tools/roms', 'tools/distribution', 'frontend/RetroFE/Tools',
              'frontend/RetroFE/Config', 'frontend/ES-DE/UserData/custom_systems',
              'frontend/pegasus-frontend']
    command = ['rg', '-l', '--fixed-strings', '-e', 'tools/roms/runtimes', '-e', 'UserData/',
               '-g', '*.py', '-g', '*.json', '-g', '*.sh', '-g', '*.xml', '-g', '*.cfg', '-g', '*.txt',
               '-g', '!runtimes/**', '-g', '!skraper/**', '-g', '!repository/**', '-g', '!themes/**',
               '-g', '!Manifests/**', '-g', '!UserData/**']
    command.extend(str(root / scope) for scope in scopes if (root / scope).exists())
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise ValueError('Busca de referências falhou: ' + result.stderr)
    references = sorted(set(result.stdout.splitlines()) | {
        str(root / 'frontend/ES-DE/UserData/custom_systems/es_find_rules.xml')})
    for move in moves:
        if move['source'].startswith('frontend/RetroFE/UserData/'):
            for row in move['files']:
                relative = Path(row['path'])
                if (row['kind'] == 'file' and relative.suffix.lower() in
                    {'.ini', '.cfg', '.toml', '.yml', '.yaml', '.json', '.xml'} and
                    'dev_flash' not in relative.parts and
                    not any(part in relative.parts for part in ('saves', 'states', 'memcards', 'memorycards'))):
                    references.append(str(root / move['source'] / relative))
    edits = []
    mappings = sorted([(row['source'], row['destination']) for row in moves], key=lambda row: len(row[0]), reverse=True)
    for value in references:
        path = Path(value)
        if path == Path(__file__).resolve() or not path.is_file():
            continue
        if any(part in path.relative_to(root).parts for part in
               ('runtimes', 'repository', 'Manifests', 'themes', '__pycache__')):
            continue
        _library_target(root, str(path.relative_to(root)))
        before = path.read_bytes()
        after = before
        for old, new in mappings:
            after = after.replace(old.encode(), new.encode())
            if old.startswith('frontend/RetroFE/UserData/'):
                short = old.removeprefix('frontend/RetroFE/')
                after = after.replace(short.encode(), ('../../' + new).encode())
        if after == before:
            continue
        relative = path.relative_to(root).as_posix()
        after_move = False
        for old, new in mappings:
            if relative.startswith(old + '/'):
                relative = new + relative[len(old):]
                after_move = True
                break
        if 'DuckStation' in relative or relative.endswith('/profiles/host-linux.json'):
            after = after.replace(b'../../../../../../bios', b'../../../../../bios')
        preimage = opdir / 'rollback/files' / relative
        proposal = opdir / 'proposed' / relative
        preimage.parent.mkdir(parents=True, exist_ok=True)
        proposal.parent.mkdir(parents=True, exist_ok=True)
        preimage.write_bytes(before)
        proposal.write_bytes(after)
        edits.append({'path': relative, 'sha256_before': _sha256(preimage), 'sha256_after': _sha256(proposal),
                      'mode': path.stat().st_mode & 0o777, 'after_move': after_move})
    # Eden's shell wrapper climbs one fewer parent in the new location.
    for row in moves:
        if row['source'] == 'tools/roms/runtimes/eden-nx2':
            path = root / row['source'] / 'eden-nx2-launch'
            before = path.read_bytes()
            after = before.replace(b'$HERE/../../../..', b'$HERE/../../..')
            relative = row['destination'] + '/eden-nx2-launch'
            for subdir, data in [('rollback/files', before), ('proposed', after)]:
                snapshot = opdir / subdir / relative
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_bytes(data)
            edits.append({'path': relative, 'sha256_before': romtool.hashlib.sha256(before).hexdigest(),
                          'sha256_after': romtool.hashlib.sha256(after).hexdigest(),
                          'mode': path.stat().st_mode & 0o777, 'after_move': True})
    manifest = {'schema_version': 1, 'kind': 'emulator_library_migration', 'operation_id': operation_id,
                'created_at': romtool.now(), 'status': 'prepared', 'moves': moves, 'edits': edits,
                'references_searched': scopes, 'references_found': [str(Path(p).relative_to(root)) for p in references],
                'preserved_non_emulators': ['tools/roms/runtimes/zarchive', 'tools/roms/runtimes/.incoming',
                                           'tools/roms/runtimes/.staging-*'],
                'gameplay_tested': False}
    romtool.atomic_json(opdir / 'manifest.json', manifest)
    romtool.append_event(operation_id, 'library_prepared', action='library_plan', status='prepared',
                         details={'runtime_directories': len(moves), 'reference_edits': len(edits)})
    romtool.rebuild_history_index()
    return {'operation_id': operation_id, 'status': 'prepared', 'moves': len(moves), 'edits': len(edits),
            'bytes': sum(f.get('size_bytes', 0) for row in moves for f in row['files'])}


def library_execute(root: Path, operation_id: str, action: str, confirmed: bool = False) -> dict:
    import fcntl
    opdir = romtool.operation_dir(operation_id)
    manifest = json.loads((opdir / 'manifest.json').read_text())
    if manifest.get('kind') != 'emulator_library_migration':
        raise ValueError('Operação não é migração da biblioteca de emuladores')
    if action != 'verify' and not confirmed:
        raise ValueError('Ação exige --apply')
    lock = os.open(romtool.OPS_ROOT / 'emulator-controls.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _library_guard(root)
        if action == 'apply' and manifest['status'] not in ('prepared', 'failed', 'applying'):
            raise ValueError('Apply requer plano prepared ou checkpoint failed')
        if action == 'rollback' and manifest['status'] not in ('completed_verified', 'applied', 'failed', 'applying', 'rolling_back'):
            raise ValueError('Rollback requer operação aplicada')
        applied = manifest['status'] in ('completed_verified', 'applied', 'failed', 'applying', 'rolling_back')
        # Recheck every precondition before the first mutation.
        for move in manifest['moves']:
            source = _library_source(root, move)
            destination = _library_target(root, move['destination'])
            at_destination = destination.exists() if applied else False
            directory = destination if at_destination else source
            intent = {'source': move['source'], 'destination': move['destination']}
            if (action == 'apply' and at_destination and manifest.get('durable_moves')
                    and move.get('move_intent') != intent):
                raise ValueError('Destino sem intenção durável comprovada: ' + move['destination'])
            preserved_copy = opdir / 'rollback/imported' / move['destination']
            if (action == 'rollback' and move.get('action') == 'copy'
                    and not at_destination and not preserved_copy.exists()):
                checkpoint = move.get('rename_checkpoints', {}).get('apply')
                if (move.get('apply_completed') or checkpoint and
                        (checkpoint.get('complete') or not (root / checkpoint['source']).exists())):
                    raise ValueError('Importação publicada ausente; reconciliar: ' + move['destination'])
            if (action == 'rollback' and move.get('action') == 'copy' and not at_destination
                    and move.get('rollback_intent') and preserved_copy.exists()):
                directory = preserved_copy
            expected = [dict(row) for row in move['files']]
            if applied and action == 'verify':
                for edit in manifest['edits']:
                    prefix = move['destination'] + '/'
                    if edit.get('after_move') and edit['path'].startswith(prefix):
                        for row in expected:
                            if row['path'] == edit['path'][len(prefix):]:
                                row['sha256'] = edit['sha256_after']
                                row['size_bytes'] = (opdir / 'proposed' / edit['path']).stat().st_size
            if applied and action in ('apply', 'rollback'):
                for edit in manifest['edits']:
                    if edit.get('after_move') and edit['path'].startswith(move['destination'] + '/'):
                        rel = edit['path'][len(move['destination']) + 1:]
                        actual_path = directory / rel
                        actual_sha = _sha256(actual_path)
                        if actual_sha not in (edit['sha256_before'], edit['sha256_after']):
                            raise ValueError('Arquivo alterado após migração: ' + edit['path'])
                        for row in expected:
                            if row['path'] == rel:
                                row['sha256'] = actual_sha
                                row['size_bytes'] = actual_path.stat().st_size
            if not directory.exists() or _library_tree(directory) != expected:
                raise ValueError(f'Inventário/SHA divergente: {directory}')
            if (action == 'apply' and destination.exists() and
                    (not applied or (source.exists() and move.get('action') != 'copy'))):
                raise ValueError(f'Destino existente preservado: {destination}')
            if action == 'rollback' and source.exists() and at_destination and move.get('action') != 'copy':
                raise ValueError(f'Origem reapareceu: {source}')
            if action == 'verify' and (not at_destination or (source.exists() and move.get('action') != 'copy')):
                raise ValueError('Migração incompleta: ' + move['source'])
        for edit in manifest['edits']:
            if edit.get('after_move') and action == 'apply':
                continue
            path = _library_target(root, edit['path'])
            if edit.get('after_move') and not path.exists():
                for move in manifest['moves']:
                    if edit['path'].startswith(move['destination'] + '/') and (root / move['source']).exists():
                        path = root / move['source'] / edit['path'][len(move['destination']) + 1:]
                        break
            allowed = ({edit['sha256_before'], edit['sha256_after']} if action in ('apply', 'rollback')
                       else {edit['sha256_after']})
            current_sha = _sha256(path) if path.is_file() else None
            if current_sha not in allowed:
                raise ValueError('Referência mudou: ' + edit['path'])
        if action == 'verify':
            manifest['status'] = 'completed_verified'
        elif action == 'apply':
            manifest['status'] = 'applying'
            manifest['durable_moves'] = True
            romtool.atomic_json(opdir / 'manifest.json', manifest)
            try:
                for index, move in enumerate(manifest['moves']):
                    source, destination = _library_source(root, move), root / move['destination']
                    move['move_intent'] = {'source': move['source'], 'destination': move['destination']}
                    romtool.atomic_json(opdir / 'manifest.json', manifest)
                    if destination.exists() and (not source.exists() or move.get('action') == 'copy'):
                        checkpoint = move.get('rename_checkpoints', {}).get('apply')
                        if checkpoint:
                            _rename_library_path(root, root / checkpoint['source'], destination,
                                                 move, manifest, opdir, 'apply')
                        else:
                            romtool.sync_directory_chain(destination.parent)
                            romtool.sync_directory_chain(source.parent)
                        if move.get('action') == 'copy':
                            _sync_library_copy(destination, move['files'])
                        move['apply_completed'] = True
                        romtool.atomic_json(opdir / 'manifest.json', manifest)
                        continue
                    missing = []
                    parent = destination.parent
                    while not parent.exists() and parent != root:
                        missing.append(parent.relative_to(root).as_posix())
                        parent = parent.parent
                    manifest.setdefault('created_directories', []).extend(value for value in reversed(missing)
                        if value not in manifest.get('created_directories', []))
                    romtool.atomic_json(opdir / 'manifest.json', manifest)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if move.get('action') != 'copy' and source.stat().st_dev != destination.parent.stat().st_dev:
                        raise ValueError('A migração exige rename no mesmo filesystem')
                    # Linux requires owner write on a directory when updating its
                    # parent entry. Restore a read-only package's exact mode.
                    if move.get('action') == 'copy':
                        if any(row['kind'] == 'symlink' for row in move['files']):
                            raise ValueError('Importação Dolphin não admite symlinks')
                        staging = opdir / 'staging/library-copies' / str(index)
                        _library_target(root, staging.relative_to(root).as_posix())
                        staging.parent.mkdir(parents=True, exist_ok=True)
                        for phase, checkpoint in list(move.get('rename_checkpoints', {}).items()):
                            if not phase.startswith('partial-'):
                                continue
                            if not checkpoint.get('complete'):
                                _rename_library_path(root, root / checkpoint['source'],
                                                     root / checkpoint['destination'], move, manifest, opdir, phase)
                            retained = (root / checkpoint['destination']).relative_to(opdir).as_posix()
                            if retained not in move.get('interrupted_copies', []):
                                move.setdefault('interrupted_copies', []).append(retained)
                                romtool.atomic_json(opdir / 'manifest.json', manifest)
                        if staging.exists() and _library_tree(staging) != move['files']:
                            phase = 'partial-' + uuid.uuid4().hex
                            partial = opdir / 'rollback/interrupted-copies' / (str(index) + '-' + phase)
                            partial.parent.mkdir(parents=True, exist_ok=True)
                            _rename_library_path(root, staging, partial, move, manifest, opdir, phase)
                            move.setdefault('interrupted_copies', []).append(partial.relative_to(opdir).as_posix())
                            romtool.atomic_json(opdir / 'manifest.json', manifest)
                        if not staging.exists():
                            if source.is_dir():
                                shutil.copytree(source, staging, symlinks=False)
                            else:
                                shutil.copy2(source, staging)
                        if _library_tree(staging) != move['files'] or _library_tree(source) != move['files']:
                            raise ValueError('Importação divergiu da fonte validada')
                        _sync_library_copy(staging, move['files'])
                        if destination.exists():
                            raise ValueError('Destino da importação apareceu durante a cópia')
                        _rename_library_path(root, staging, destination, move, manifest, opdir, 'apply')
                        move['apply_completed'] = True
                        romtool.atomic_json(opdir / 'manifest.json', manifest)
                        romtool.append_event(operation_id, 'dolphin_data_imported', action='library_apply',
                                             status='applied', source=move['source_absolute'], destination=move['destination'])
                        continue
                    _rename_library_path(root, source, destination, move, manifest, opdir, 'apply')
                    move['apply_completed'] = True
                    romtool.atomic_json(opdir / 'manifest.json', manifest)
                    romtool.append_event(operation_id, 'runtime_moved', action='library_apply', status='applied',
                                         source=move['source'], destination=move['destination'])
                for edit in manifest['edits']:
                    path = root / edit['path']
                    missing = []
                    parent = path.parent
                    while not parent.exists() and parent != root:
                        missing.append(parent.relative_to(root).as_posix())
                        parent = parent.parent
                    manifest.setdefault('created_directories', []).extend(value for value in reversed(missing)
                        if value not in manifest.get('created_directories', []))
                    romtool.atomic_json(opdir / 'manifest.json', manifest)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    data = (opdir / 'proposed' / edit['path']).read_bytes()
                    if hashlib.sha256(data).hexdigest() != edit['sha256_after']:
                        raise ValueError('Snapshot alterado: ' + edit['path'])
                    edit['write_intent'] = {'sha256': edit['sha256_after']}
                    romtool.atomic_json(opdir / 'manifest.json', manifest)
                    romtool.atomic_write_bytes(path, data, mode=edit['mode'])
                    edit['write_completed'] = True
                    romtool.atomic_json(opdir / 'manifest.json', manifest)
                manifest['status'] = 'applied'
            except Exception:
                manifest['status'] = 'failed'
                romtool.atomic_json(opdir / 'manifest.json', manifest)
                raise
        else:
            manifest['status'] = 'rolling_back'
            romtool.atomic_json(opdir / 'manifest.json', manifest)
            for edit in reversed(manifest['edits']):
                path = root / edit['path']
                if edit.get('after_move') and not path.exists():
                    for move in manifest['moves']:
                        if edit['path'].startswith(move['destination'] + '/') and (root / move['source']).exists():
                            path = root / move['source'] / edit['path'][len(move['destination']) + 1:]
                            break
                if edit['sha256_before'] is None:
                    preserved = opdir / 'rollback/applied-files' / edit['path']
                    if not path.exists() and not edit.get('write_intent') and manifest.get('durable_moves'):
                        continue
                    if not path.exists() and preserved.is_file() and _sha256(preserved) == edit['sha256_after']:
                        if edit.get('rename_checkpoints', {}).get('rollback-created-file'):
                            _rename_library_path(root, path, preserved, edit, manifest, opdir, 'rollback-created-file')
                        continue
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    if preserved.exists():
                        raise ValueError('Destino de preservação ocupado: ' + edit['path'])
                    _rename_library_path(root, path, preserved, edit, manifest, opdir, 'rollback-created-file')
                    continue
                before = (opdir / 'rollback/files' / edit['path']).read_bytes()
                if hashlib.sha256(before).hexdigest() != edit['sha256_before']:
                    raise ValueError('Preimagem alterada')
                romtool.atomic_write_bytes(path, before, mode=edit['mode'])
            for move in reversed(manifest['moves']):
                source = root / move['destination']
                destination = _library_source(root, move)
                rollback_target = ((opdir / 'rollback/imported' / move['destination']).relative_to(root).as_posix()
                                   if move.get('action') == 'copy' else move['source'])
                move['rollback_intent'] = {'source': move['destination'], 'destination': rollback_target}
                romtool.atomic_json(opdir / 'manifest.json', manifest)
                if move.get('action') == 'copy':
                    preserved = opdir / 'rollback/imported' / move['destination']
                    if not source.exists() and not preserved.exists():
                        # A copy interrupted in staging has never entered the shared library.
                        # Its original and private staging remain available for inspection.
                        continue
                    if not source.exists() and preserved.exists() and _library_tree(preserved) == move['files']:
                        if move.get('rename_checkpoints', {}).get('rollback'):
                            _rename_library_path(root, source, preserved, move, manifest, opdir, 'rollback')
                        continue
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    if preserved.exists():
                        raise ValueError('Destino de preservação da importação ocupado')
                    _rename_library_path(root, source, preserved, move, manifest, opdir, 'rollback')
                    move['rollback_completed'] = True
                    romtool.atomic_json(opdir / 'manifest.json', manifest)
                    continue
                if not source.exists() and destination.exists():
                    if move.get('rename_checkpoints', {}).get('rollback'):
                        _rename_library_path(root, source, destination, move, manifest, opdir, 'rollback')
                    continue
                _rename_library_path(root, source, destination, move, manifest, opdir, 'rollback')
                move['rollback_completed'] = True
                romtool.atomic_json(opdir / 'manifest.json', manifest)
            for relative in reversed(manifest.get('created_directories', [])):
                directory = _library_target(root, relative)
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
                    romtool.sync_directory_chain(directory.parent)
            manifest['status'] = 'rolled_back'
        romtool.atomic_json(opdir / 'manifest.json', manifest)
        romtool.append_event(operation_id, 'library_' + action, action='library_' + action,
                             status=manifest['status'], details={'moves': len(manifest['moves'])})
        romtool.rebuild_history_index()
        return {'operation_id': operation_id, 'status': manifest['status'], 'moves': len(manifest['moves'])}
    finally:
        os.close(lock)


def model2_configuration_plan(root: Path) -> dict:
    """Coordinate only the relocated legacy package's ROMDir and host reason."""
    import datetime
    import uuid
    operation_id = 'emulator-library-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    relative = 'emulator/windows/model2/EMULATOR.INI'
    path = _library_target(root, relative)
    before = path.read_bytes()
    if before.count(b'Dir1=roms\r\n') != 1:
        raise ValueError('ROMDir Model 2 personalizado ou já corrigido; preservar e revisar')
    profile_relative = 'frontend/RetroFE/Config/profiles/host-linux.json'
    profile_path = _library_target(root, profile_relative)
    profile_before = profile_path.read_bytes()
    profile = json.loads(profile_before)
    system = profile['collections']['Sega Model 2']
    if system['adapter'] != 'unavailable':
        raise ValueError('Ativação Linux existente exige revisão específica')
    system['reason'] = ('Pacote Windows x86 preservado em emulator/windows/model2; Linux sem ponte Wine/DirectX 9 '
                        'e retorno ao frontend validados. Nenhum EXE é executado automaticamente.')
    proposals = [(relative, before, before.replace(b'Dir1=roms\r\n', b'Dir1=../../../roms/model2/roms\r\n')),
                 (profile_relative, profile_before, (json.dumps(profile, ensure_ascii=False, indent=2) + '\n').encode())]
    opdir = romtool.operation_dir(operation_id)
    edits = []
    for name, old, new in proposals:
        backup = opdir / 'rollback/files' / name
        proposal = opdir / 'proposed' / name
        backup.parent.mkdir(parents=True, exist_ok=True)
        proposal.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(old)
        proposal.write_bytes(new)
        edits.append({'path': name, 'sha256_before': hashlib.sha256(old).hexdigest(),
                      'sha256_after': _sha256(proposal), 'mode': (root / name).stat().st_mode & 0o777})
    manifest = {'schema_version': 1, 'kind': 'emulator_library_migration', 'operation_id': operation_id,
                'status': 'prepared', 'created_at': romtool.now(), 'moves': [], 'edits': edits,
                'scope': 'relocated_model2_romdir_and_linux_diagnostic', 'gameplay_tested': False}
    romtool.atomic_json(opdir / 'manifest.json', manifest)
    romtool.append_event(operation_id, 'model2_configuration_prepared', action='library_plan', status='prepared')
    romtool.rebuild_history_index()
    return {'operation_id': operation_id, 'status': 'prepared', 'edits': len(edits)}


def native_configuration_plan(root: Path) -> dict:
    try:
        from . import portable_emulator
    except ImportError:
        import portable_emulator
    import datetime
    import uuid
    operation_id = 'emulator-library-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    opdir = romtool.operation_dir(operation_id)
    opdir.mkdir(parents=True)
    edits = []
    for relative, data in portable_emulator.configuration_files(root).items():
        path = _library_target(root, relative)
        before = path.read_bytes() if path.is_file() else None
        if before == data:
            continue
        # These are new OS-specific configurations, not replacement user choices.
        if before is not None:
            raise ValueError('Configuração Windows existente preservada: ' + relative)
        proposal = opdir / 'proposed' / relative
        proposal.parent.mkdir(parents=True, exist_ok=True)
        proposal.write_bytes(data)
        edits.append({'path': relative, 'sha256_before': None, 'sha256_after': _sha256(proposal), 'mode': 0o644})
    manifest = {'schema_version': 1, 'kind': 'emulator_library_migration', 'operation_id': operation_id,
                'status': 'prepared', 'created_at': romtool.now(), 'moves': [], 'edits': edits,
                'scope': 'new_windows_specific_configuration_referencing_shared_data', 'gameplay_tested': False}
    romtool.atomic_json(opdir / 'manifest.json', manifest)
    romtool.append_event(operation_id, 'native_configuration_prepared', action='library_plan', status='prepared',
                         details={'files': len(edits)})
    romtool.rebuild_history_index()
    return {'operation_id': operation_id, 'status': 'prepared', 'edits': len(edits)}


def dolphin_data_plan(root: Path, config_only: bool = False) -> dict:
    """Selected Dolphin import; the external host profile stays intact for rollback."""
    import datetime
    import uuid
    operation_id = 'emulator-library-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    opdir = romtool.operation_dir(operation_id)
    opdir.mkdir(parents=True)
    base = Path.home() / '.var/app/org.DolphinEmu.dolphin-emu'
    shared = root / 'emulator/data/Dolphin/shared'
    moves, edits, preserved = [], [], []
    proposals = {}
    role = 'config' if config_only else 'data'
    source_root = base / role / 'dolphin-emu'
    destination_root = shared / 'Config' if config_only else shared
    if source_root.is_dir():
        for source in sorted(source_root.iterdir()):
            if source.is_symlink():
                raise ValueError('Origem Dolphin por symlink preservada: ' + str(source))
            destination = destination_root / source.name
            inventory = _library_tree(source)
            if destination.exists():
                same = _library_tree(destination) == inventory
                preserved.append({'source': str(source), 'destination': destination.relative_to(root).as_posix(),
                                  'status': 'same_bytes_external_original_preserved' if same else 'project_configuration_retained_host_choice_preserved'})
                continue
            moves.append({'source': '@host-dolphin/' + source.relative_to(base).as_posix(),
                          'source_absolute': str(source), 'external_source': source.relative_to(base).as_posix(),
                          'action': 'copy', 'destination': destination.relative_to(root).as_posix(), 'files': inventory})
    if not config_only:
        source = root / 'emulator/data/Dolphin/config/dolphin-emu'
        if not source.is_dir() or (shared / 'Config').exists():
            raise ValueError('Configuração canônica Dolphin ausente ou destino Config existente')
        moves.append({'source': source.relative_to(root).as_posix(), 'destination': (shared / 'Config').relative_to(root).as_posix(),
                      'files': _library_tree(source)})
        for relative in ['tools/roms/emulator_controls.py', 'tools/roms/tests/test_emulator_controls.py']:
            path = root / relative
            proposals[relative] = path.read_bytes().replace(b'Dolphin/config/dolphin-emu', b'Dolphin/shared/Config')
        relative = 'frontend/RetroFE/Config/profiles/host-linux.json'
        profile = json.loads((root / relative).read_text())
        app = profile['flatpak_apps']['org.DolphinEmu.dolphin-emu']
        for key in list(app):
            if key.startswith(('portable_xdg_', 'legacy_xdg_')):
                app.pop(key)
        for system in profile['collections'].values():
            if system.get('app_id') == 'org.DolphinEmu.dolphin-emu':
                args = system.get('args', ['{item}'])
                system['args'] = ['-u', '{userdata}/shared', *(['-e', '{item}'] if args == ['{item}'] else args)]
        proposals[relative] = (json.dumps(profile, ensure_ascii=False, indent=2) + '\n').encode()
        relative = 'tools/roms/config/portable-runtimes.json'
        recipes = json.loads((root / relative).read_text())
        recipes['windows_launch']['dolphin']['args'] = ['-u', '{userdata}/shared', '-e', '{item}']
        proposals[relative] = (json.dumps(recipes, ensure_ascii=False, indent=2) + '\n').encode()
    for relative, after in proposals.items():
        before = (root / relative).read_bytes()
        if before == after:
            continue
        for folder, payload in [('rollback/files', before), ('proposed', after)]:
            snapshot = opdir / folder / relative
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(payload)
        edits.append({'path': relative, 'sha256_before': hashlib.sha256(before).hexdigest(),
                      'sha256_after': hashlib.sha256(after).hexdigest(), 'mode': (root / relative).stat().st_mode & 0o777})
    manifest = {'schema_version': 1, 'kind': 'emulator_library_migration', 'operation_id': operation_id,
                'status': 'prepared', 'created_at': romtool.now(), 'scope': 'dolphin_selected_host_import',
                'moves': moves, 'edits': edits, 'external_originals_preserved': True, 'preserved_sources': preserved,
                'reference_basis': 'Explicit -u native Dolphin layout, same runtime; project choices retained on conflict',
                'gameplay_tested': False}
    romtool.atomic_json(opdir / 'manifest.json', manifest)
    romtool.append_event(operation_id, 'dolphin_data_prepared', action='library_plan', status='prepared',
                         details={'items': len(moves), 'preserved': len(preserved), 'config_only': config_only})
    romtool.rebuild_history_index()
    return {'operation_id': operation_id, 'status': 'prepared', 'items': len(moves), 'edits': len(edits),
            'external_originals_preserved': len(preserved)}


class ManualDownloadRequired(RuntimeError):
    pass


class HostDuplicate(RuntimeError):
    pass


def _package_member(value: str) -> Path:
    """Archives must also be safe on case-insensitive Windows volumes."""
    from pathlib import PureWindowsPath
    value = value.replace('\\', '/')
    path = Path(value)
    if (not value or value in ('.', '..') or value.startswith('/') or PureWindowsPath(value).drive or
        any(part in ('..', '.') or ':' in part or part.endswith((' ', '.')) for part in path.parts)):
        raise ValueError(f'Membro inseguro: {value}')
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                *(f'lpt{i}' for i in range(1, 10))}
    if any(part.split('.')[0].casefold() in reserved for part in path.parts):
        raise ValueError(f'Nome reservado Windows: {value}')
    return path


def _extract_package(source: Path, destination: Path, recipe: dict) -> None:
    """Validate the full archive before extracting into an empty private stage."""
    limit = int(recipe.get('max_unpacked_bytes', 2 << 30))
    names = set()
    if recipe['format'] == 'zip_tree':
        with zipfile.ZipFile(source) as bundle:
            total = 0
            members = []
            for info in bundle.infolist():
                relative = _package_member(info.filename.replace('\\', '/').rstrip('/'))
                key = relative.as_posix().casefold()
                if key in names or info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Membro duplicado, cifrado ou symlink: ' + info.filename)
                names.add(key)
                total += info.file_size
                if total > limit:
                    raise ValueError('Arquivo excede o limite de extração')
                members.append((info, relative))
            bad = bundle.testzip()
            if bad:
                raise ValueError('CRC inválido: ' + bad)
            destination.mkdir()
            for info, relative in members:
                target = destination / relative
                if info.is_dir() or info.filename.endswith('\\'):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as src, target.open('xb') as out:
                        shutil.copyfileobj(src, out, 1 << 20)
    elif recipe['format'] == '7z_tree':
        extractor = shutil.which('7z') or shutil.which('7zz')
        if not extractor:
            raise ValueError('Extração requer 7-Zip no host; nenhum instalador é executado')
        listing = subprocess.run([extractor, 'l', '-mmt=1', '-slt', '-ba', str(source)],
                                 capture_output=True, text=True, check=True, timeout=60).stdout
        total = 0
        for block in listing.split('\n\n'):
            fields = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
            if 'Path' not in fields:
                continue
            relative = _package_member(fields['Path'])
            key = relative.as_posix().casefold()
            if key in names or any(k in fields for k in ('Symbolic Link', 'Hard Link')) or fields.get('Encrypted') == '+':
                raise ValueError('Membro duplicado, cifrado ou link: ' + fields['Path'])
            names.add(key)
            total += int(fields.get('Size', '0'))
            if total > limit:
                raise ValueError('Arquivo excede o limite de extração')
        if not names:
            raise ValueError('Arquivo sem membros')
        subprocess.run([extractor, 't', '-mmt=1', '-bd', '-bso0', str(source)], check=True,
                       capture_output=True, timeout=300)
        destination.mkdir()
        subprocess.run([extractor, 'x', '-mmt=1', '-y', '-bd', '-bso0', '-o' + str(destination), str(source)],
                       check=True, capture_output=True, timeout=300)
        extracted = _library_tree(destination)
        if any(row['kind'] == 'symlink' for row in extracted):
            raise ValueError('Link extraído recusado')
        if {row['path'].casefold() for row in extracted if row['kind'] == 'file'} - names:
            raise ValueError('Extração divergiu da listagem')
    else:
        raise ValueError('Formato de pacote não permitido')


def _check_pe64(path: Path) -> None:
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b'MZ':
            raise ValueError('Entrada não é PE Windows')
        stream.seek(int.from_bytes(header[60:64], 'little'))
        pe = stream.read(6)
        if pe[:4] != b'PE\0\0' or pe[4:6] != b'\x64\x86':
            raise ValueError('Entrada não é PE x86_64')


def install_package(root: Path, runtime_id: str, platform: str, confirmed: bool = False,
                    from_file: Path | None = None, replace_existing: bool = False) -> dict:
    if not confirmed:
        return _install_package(root, runtime_id, platform, confirmed, from_file, replace_existing)
    try:
        from .native_bindings import configuration_lock
    except ImportError:
        from native_bindings import configuration_lock
    with configuration_lock(root):
        return _install_package(root, runtime_id, platform, confirmed, from_file, replace_existing)


def _install_package(root: Path, runtime_id: str, platform: str, confirmed: bool = False,
                    from_file: Path | None = None, replace_existing: bool = False) -> dict:
    """Native multi-file packages use the same recipe store, downloader and history."""
    config = json.loads(_paths(root).config.read_text())
    if platform not in ('linux', 'windows'):
        raise ValueError('Plataforma inválida')
    recipe = config.get('packages', {}).get(platform, {}).get(runtime_id)
    if recipe is None:
        raise ValueError('Pacote nativo sem receita fixada: ' + runtime_id)
    if recipe.get('installable') is False:
        raise ValueError('Pacote local preservado sem receita/licença de redistribuição: ' + runtime_id)
    _package_member(runtime_id)
    target = _library_target(root, f'emulator/{platform}/{runtime_id}')
    entry_relative = _package_member(recipe['entry'])
    expected_sha = recipe['source_sha256']
    if len(expected_sha) != 64 or recipe['source_size_bytes'] <= 0:
        raise ValueError('Pin de pacote incompleto')
    proposal = {'runtime_id': runtime_id, 'platform': platform, 'version': recipe['version'],
                'source_url': recipe['source_url'], 'source_sha256': expected_sha,
                'source_size_bytes': recipe['source_size_bytes'], 'destination': target.relative_to(root).as_posix(),
                'entry': (target / entry_relative).relative_to(root).as_posix(),
                'existing_preserved_in_rollback': target.exists(), 'gameplay_tested': False}
    # Installation receipts are evidence only while every immutable package byte
    # is still present. Writable data is always outside the program directory.
    for record in sorted(romtool.OPS_ROOT.glob('environment-*/manifest.json'), reverse=True):
        prior = json.loads(record.read_text()).get('details', {})
        if (prior.get('package_install') and prior.get('platform') == platform and
            prior.get('runtime_id') == runtime_id and prior.get('source_sha256') == expected_sha and
            prior.get('status') == 'installed_verified' and target.is_dir()):
            inventory = _library_tree(target)
            recorded = prior.get('inventory', [])
            current = {row['path']: row for row in inventory}
            if all(current.get(row['path']) == row for row in recorded):
                return {**proposal, 'status': 'already_installed_verified', 'operation_id': record.parent.name}
    if not confirmed:
        return {**proposal, 'status': 'prepared', 'apply_available': not target.exists() or replace_existing}
    _library_guard(root)
    if target.exists() and not replace_existing:
        raise ValueError('Destino existente preservado; reveja o plano antes de --replace-existing')
    staging_root = root / 'emulator/.incoming'
    staging_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(staging_root).free < recipe['source_size_bytes'] + recipe.get('max_unpacked_bytes', 2 << 30) + (256 << 20):
        raise ValueError('Espaço insuficiente para o pacote e margem')
    staging = Path(tempfile.mkdtemp(prefix='.package-' + runtime_id + '-', dir=staging_root))
    try:
        source = staging / 'source'
        if from_file is None:
            if recipe['source_size_bytes'] > int(config.get('package_download_limit_bytes', 256 << 20)):
                raise ManualDownloadRequired('Pacote excede o limite configurado')
            _download(recipe['source_url'], source, recipe['source_size_bytes'], set(config['allowed_download_hosts']))
        else:
            if from_file.is_symlink() or not from_file.is_file():
                raise ValueError('Fonte local inválida')
            if from_file.stat().st_dev == staging.stat().st_dev:
                os.link(from_file, source)
            else:
                shutil.copyfile(from_file, source)
        if source.stat().st_size != recipe['source_size_bytes'] or _sha256(source) != expected_sha:
            raise ValueError('Tamanho/SHA do pacote divergem da origem fixada')
        package = staging / 'package'
        _extract_package(source, package, recipe)
        entry = package / entry_relative
        if platform == 'windows':
            _check_pe64(entry)
        elif recipe.get('entry_kind') == 'appimage':
            _check_appimage(entry)
            entry.chmod(0o755)
        else:
            if entry.read_bytes()[:4] != b'\x7fELF':
                raise ValueError('Entrada Linux não é ELF')
            entry.chmod(0o755)
        details = {**proposal, 'package_install': True, 'status': 'prepared',
                   'entry_sha256': _sha256(entry), 'inventory': _library_tree(package),
                   'previous_inventory': _library_tree(target) if target.exists() else [],
                   'validation': 'full_archive_crc_and_all_member_sha256; native_header; no execution'}
        recorded = romtool.command_record_change(SimpleNamespace(change_type='native_runtime_package_install',
            details_json=json.dumps(details), reason='Instalação portátil nativa selecionada, sem jogos/BIOS/firmware'))
        opid = recorded['operation_id']
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            rollback = romtool.operation_dir(opid) / 'rollback'
            rollback.mkdir()
            target.rename(rollback / 'previous-package')
        package.rename(target)
        if _library_tree(target) != details['inventory']:
            raise ValueError('Publicação divergiu do inventário validado')
        details['status'] = 'installed_verified'
        recorded['details'] = details
        romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json', recorded)
        romtool.append_event(opid, 'package_installed_verified', action='native_runtime_package_install',
                             status='completed', destination=proposal['destination'],
                             sha256_after=details['entry_sha256'], details={'platform': platform, 'runtime_id': runtime_id})
        romtool.rebuild_history_index()
        return {'operation_id': opid, **proposal, 'status': details['status'], 'members': len(details['inventory'])}
    finally:
        # Only this operation's reproducible download/extraction stage is removed.
        shutil.rmtree(staging, ignore_errors=True)


def host_flatpaks() -> tuple[dict[str, str], bool]:
    """Installed Flatpak apps (user and system) with versions; (found, query_ok)."""
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return {}, False
    try:
        result = subprocess.run([flatpak, "list", "--app", "--columns=application,version"],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}, False
    if result.returncode:
        return {}, False
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        app_id = fields[0].strip() if fields else ""
        if app_id:
            found[app_id] = fields[1].strip() if len(fields) > 1 else ""
    return found, True


def scan_host(root: Path, recipe: dict, flatpaks: dict[str, str], which=None) -> list[dict]:
    """Emulators already provided by the host, outside the project; never a project path."""
    sources = []
    if recipe.get("flatpak_app_id") in flatpaks:
        sources.append({"kind": "flatpak", "app_id": recipe["flatpak_app_id"], "version": flatpaks[recipe["flatpak_app_id"]]})
    project = root.resolve()
    if recipe.get("host_core"):
        profile = json.loads(_paths(root).profile.read_text(encoding="utf-8"))
        core_directory = Path(profile.get("retroarch", {}).get("core_root", "")).expanduser()
        if not core_directory.is_absolute():
            core_directory = root / "frontend/RetroFE" / core_directory
        host_core = core_directory / recipe["host_core"]
        if host_core.is_file() and not host_core.resolve().is_relative_to(project):
            sources.append({"kind": "retroarch_core", "path": str(host_core), "version": ""})
    for name in recipe.get("host_commands", []):
        found = (which or shutil.which)(name)
        if found and not Path(found).resolve().is_relative_to(project):
            sources.append({"kind": "path", "command": name, "path": found})
            break
    return sources


def _paths(root: Path, target_platform: str = 'linux') -> SimpleNamespace:
    if target_platform not in ('linux', 'windows'):
        raise ValueError('Plataforma de runtime inválida')
    runtimes = root / 'emulator' / target_platform
    return SimpleNamespace(config=root / "tools/roms/config/portable-runtimes.json", runtimes=runtimes,
                           incoming=root / "emulator/.incoming", profile=root / "frontend/RetroFE/Config/profiles/host-linux.json")


def load_recipes(root: Path, target_platform: str = 'linux') -> dict:
    config = json.loads(_paths(root).config.read_text(encoding="utf-8"))
    if target_platform == 'windows':
        config['runtimes'] = config.get('windows_cores', {})
    for runtime_id, recipe in config.get("runtimes", {}).items():
        if not runtime_id.replace("-", "").isalnum():
            raise ValueError(f"ID de runtime inválido: {runtime_id}")
        for key in ("display_name", "version", "source_url", "source_size_bytes", "source_sha256", "format", "entry"):
            if not recipe.get(key):
                raise ValueError(f"Receita {runtime_id} sem {key}")
        install_dir = recipe.get("install_dir", runtime_id)
        if "/" in install_dir or install_dir.startswith(".") or recipe.get("kind", "appimage") not in {"appimage", "libretro_core", "windows_core"}:
            raise ValueError(f"Receita {runtime_id} com pasta ou tipo inválidos")
        if recipe["format"] not in {"appimage", "zip_member"} or "/" in recipe["entry"] or recipe["entry"].startswith("."):
            raise ValueError(f"Receita {runtime_id} com formato ou entrada inválidos")
    return config


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _install_records(root: Path, target_platform: str = 'linux') -> dict:
    """Latest verified install per runtime, read from the official operation history."""
    records = {}
    ops = romtool.OPS_ROOT
    for path in sorted(ops.glob("environment-*/manifest.json"), key=lambda path: path.stat().st_mtime_ns):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        details = manifest.get("details", {})
        if details.get('platform', 'linux') != target_platform:
            continue
        if manifest.get("change_type") == CHANGE_TYPE and details.get("status") in {"installed_verified", "already_installed_verified"}:
            records[details["runtime_id"]] = {"operation_id": manifest["operation_id"], **details}
        elif manifest.get("change_type") == ROLLBACK_CHANGE_TYPE:
            records.pop(details.get("runtime_id"), None)
    return records


def _used_by(root: Path, recipe: dict) -> list[dict]:
    profile = json.loads(_paths(root).profile.read_text(encoding="utf-8"))
    used = []
    for name, collection in profile.get("collections", {}).items():
        if recipe.get("profile_command") and collection.get("command") == recipe["profile_command"]:
            used.append({"collection": name, "launch": "portable"})
        elif recipe.get("host_core") and collection.get("core") == recipe["host_core"] and collection.get("adapter") == "retroarch":
            used.append({"collection": name, "launch": "portable" if collection.get("core_root") else "retroarch_host_core"})
        elif recipe.get("flatpak_app_id") and collection.get("app_id") == recipe["flatpak_app_id"]:
            used.append({"collection": name, "launch": "flatpak", "project_userdata": bool(collection.get("userdata_subdir"))})
    return used


def runtime_status(root: Path, flatpak_query=None) -> dict:
    config = load_recipes(root)
    flatpaks, flatpak_ok = (flatpak_query or host_flatpaks)()
    paths = _paths(root)
    records = _install_records(root)
    limit = int(config["manual_download_limit_bytes"])
    rows = []
    for runtime_id, recipe in config["runtimes"].items():
        entry = paths.runtimes / recipe.get("install_dir", runtime_id) / recipe["entry"]
        record = records.get(runtime_id)
        expected_size = recipe.get("entry_size_bytes") or (recipe["source_size_bytes"] if recipe["format"] == "appimage" else None)
        if entry.is_symlink():
            status = "blocked_symlink"
        elif not entry.is_file():
            status = "not_installed"
        elif record and record.get("entry_size_bytes") == entry.stat().st_size and record.get("version") == recipe["version"]:
            status = "installed_recorded"
        elif expected_size and entry.stat().st_size == expected_size:
            status = "installed_size_matches"
        else:
            status = "installed_other_version"
        manual = recipe["source_size_bytes"] > limit
        host = scan_host(root, recipe, flatpaks)
        used = _used_by(root, recipe)
        if status not in {"not_installed", "blocked_symlink"}:
            decision = "portable_in_project"
        elif host:
            decision = "use_host_configured" if any(u["launch"] in {"flatpak", "retroarch_host_core"} for u in used) else "use_host_not_bound"
        else:
            decision = "install_available"
        incoming = paths.incoming / Path(urlparse(recipe["source_url"]).path).name
        rows.append({
            "runtime_id": runtime_id, "display_name": recipe["display_name"], "systems": recipe.get("systems", ""),
            "version": recipe["version"], "status": status, "entry": str(entry.relative_to(root)),
            "source_url": recipe["source_url"], "release_page": recipe.get("release_page", ""),
            "source_size_bytes": recipe["source_size_bytes"], "source_sha256": recipe["source_sha256"],
            "manual_download": manual, "incoming_path": str(incoming.relative_to(root)),
            "incoming_ready": incoming.is_file() and not incoming.is_symlink() and incoming.stat().st_size == recipe["source_size_bytes"],
            "install_operation_id": (record or {}).get("operation_id", ""), "used_by": used,
            "host_sources": host, "decision": decision,
        })
    assets = [{"asset_id": asset_id, "display_name": asset["display_name"], "version": asset["version"],
               "collection": asset.get("used_by_collection", ""), "destination": asset["destination"],
               "source_url": asset["source_url"], "source_size_bytes": asset["source_size_bytes"],
               "present": os.path.lexists(root / asset["destination"])}
              for asset_id, asset in config.get("assets", {}).items()]
    return {"runtimes": rows, "assets": assets, "not_installable": config.get("not_installable", {}),
            "manual_download_limit_bytes": limit, "host_flatpak_query_ok": flatpak_ok}


def _download(url: str, destination: Path, size: int, allowed_hosts: set[str]) -> None:
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in allowed_hosts:
        raise ValueError(f"Origem fora dos hosts oficiais permitidos: {url}")
    request = Request(url, headers={"User-Agent": "emulation-emutool-portable-runtime"})
    with urlopen(request, timeout=60) as response, destination.open("xb") as out:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname not in allowed_hosts:
            raise ValueError(f"Redirecionamento para host não permitido: {final.hostname}")
        received = 0
        for block in iter(lambda: response.read(1 << 20), b""):
            received += len(block)
            if received > size:
                raise ValueError("O download excedeu o tamanho fixado; descartado")
            out.write(block)
        out.flush()
        os.fsync(out.fileno())


def _extract_member(archive: Path, recipe: dict, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        names = [info for info in bundle.infolist() if not info.is_dir()]
        if recipe.get("member"):
            chosen = [info for info in names if info.filename == recipe["member"]]
        else:
            chosen = [info for info in names if info.filename.endswith(recipe["member_suffix"])]
        if len(chosen) != 1:
            raise ValueError(f"O arquivo não contém exatamente um membro esperado: {[i.filename for i in chosen]}")
        info = chosen[0]
        if info.filename.startswith("/") or ".." in Path(info.filename).parts or (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"Membro inseguro no arquivo: {info.filename}")
        with bundle.open(info) as source, destination.open("xb") as out:
            shutil.copyfileobj(source, out, 1 << 20)


def _check_elf_shared_object(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(18)
    if header[:4] != b"\x7fELF" or int.from_bytes(header[16:18], "little") != 3:
        raise ValueError("O núcleo instalado não é uma biblioteca ELF compartilhada")


def _check_appimage(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(11)
    if header[:4] != b"\x7fELF" or header[8:11] not in {b"AI\x01", b"AI\x02"}:
        raise ValueError("A entrada instalada não é um AppImage ELF válido")


def _probe(path: Path, recipe: dict, scratch: Path) -> dict:
    args = recipe.get("probe_args")
    if not args:
        return {"status": "not_declared", "note": "Sem sonda não interativa declarada; verificação por hash e cabeçalho AppImage."}
    for name in ("config", "data", "cache"):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **recipe.get("environment", {}),
           "XDG_CONFIG_HOME": str(scratch / "config"), "XDG_DATA_HOME": str(scratch / "data"), "XDG_CACHE_HOME": str(scratch / "cache"),
           "TMPDIR": str(scratch)}
    result = subprocess.run([str(path), *args], env=env, cwd=scratch, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, errors="replace", timeout=60)
    if result.returncode:
        raise RuntimeError(f"Sonda {args} falhou com código {result.returncode}: {(result.stderr or result.stdout)[-300:]}")
    return {"status": "passed", "args": args, "output": (result.stdout or result.stderr).strip()[:200]}


def install_runtime(root: Path, runtime_id: str, confirmed: bool, from_file: Path | None = None,
                    allow_host_duplicate: bool = False, flatpak_query=None,
                    run_declared_probe: bool = True, target_platform: str = 'linux') -> dict:
    if not confirmed:
        raise ValueError("Instalação exige --apply explícito")
    config = load_recipes(root, target_platform)
    recipe = config["runtimes"].get(runtime_id)
    if not recipe:
        raise ValueError(f"Runtime sem receita portátil: {runtime_id}; {config.get('not_installable', {}).get(runtime_id, '')}")
    paths = _paths(root, target_platform)
    target_dir = paths.runtimes / recipe.get("install_dir", runtime_id)
    entry = target_dir / recipe["entry"]
    for path in (paths.runtimes, target_dir, entry):
        if path.is_symlink():
            raise ValueError(f"Caminho de instalação é symlink; preservado: {path}")
    if entry.is_file():
        prior=_install_records(root, target_platform).get(runtime_id)
        if (prior and prior.get('version')==recipe['version']
                and prior.get('source_sha256')==recipe['source_sha256']
                and prior.get('entry_size_bytes')==entry.stat().st_size
                and prior.get('entry_sha256')==_sha256(entry)):
            return {**prior,'status':'already_installed_verified','reused_without_download':True,
                    'used_by':_used_by(root,recipe)}
    if not entry.exists() and not allow_host_duplicate and target_platform == 'linux':
        flatpaks, flatpak_ok = (flatpak_query or host_flatpaks)()
        if not flatpak_ok and recipe.get("flatpak_app_id") and shutil.which("flatpak"):
            raise RuntimeError("Não consegui consultar os Flatpaks do host; instalação bloqueada para não duplicar")
        host = scan_host(root, recipe, flatpaks)
        if host:
            where = "; ".join(f"Flatpak {h['app_id']} {h['version']}" if h["kind"] == "flatpak" else f"{h['path']}" for h in host)
            raise HostDuplicate(f"{recipe['display_name']} já está disponível no host ({where}); use-o pelo perfil em vez de duplicar. "
                                "Para instalar mesmo assim, use --allow-host-duplicate.")
    incoming = paths.incoming / Path(urlparse(recipe["source_url"]).path).name
    if from_file is None and incoming.is_file():
        from_file = incoming
    if from_file is None and recipe["source_size_bytes"] > int(config["manual_download_limit_bytes"]):
        raise ManualDownloadRequired(
            f"Download de {recipe['source_size_bytes']:,} bytes excede o limite automático; baixe {recipe['source_url']} "
            f"(SHA-256 {recipe['source_sha256']}) e coloque em {incoming.relative_to(root)}; depois instale de novo.")
    paths.runtimes.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.runtimes).free
    needed = recipe["source_size_bytes"] * 3 + (64 << 20)
    if free < needed:
        raise RuntimeError(f"Espaço insuficiente: livre {free:,}, necessário {needed:,} bytes")
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{runtime_id}-", dir=paths.runtimes))
    try:
        source = staging / "source"
        if from_file is not None:
            if from_file.is_symlink() or not from_file.is_file():
                raise ValueError(f"Arquivo local inválido: {from_file}")
            shutil.copyfile(from_file, source)
        else:
            _download(recipe["source_url"], source, recipe["source_size_bytes"], set(config["allowed_download_hosts"]))
        if source.stat().st_size != recipe["source_size_bytes"] or _sha256(source) != recipe["source_sha256"]:
            raise ValueError("Tamanho ou SHA-256 não conferem com a release fixada; nada foi instalado")
        candidate = staging / recipe["entry"]
        if recipe["format"] == "zip_member":
            _extract_member(source, recipe, candidate)
        else:
            source.rename(candidate)
        candidate.chmod(0o755)
        if recipe.get('kind') == 'windows_core':
            _check_pe64(candidate)
        elif recipe.get("kind") == "libretro_core":
            _check_elf_shared_object(candidate)
        else:
            _check_appimage(candidate)
        entry_sha = _sha256(candidate)
        if recipe.get("entry_sha256") and entry_sha != recipe["entry_sha256"]:
            raise ValueError("SHA-256 do executável extraído não confere com a receita")
        scratch = staging / "probe-home"
        scratch.mkdir()
        probe = _probe(candidate, recipe, scratch) if run_declared_probe and target_platform == 'linux' else {
            "status": "not_run", "note": "Host installer uses static validation and does not start emulator probes."}
        if entry.is_file() and _sha256(entry) == entry_sha:
            status = "already_installed_verified"
            previous = None
        else:
            status = "installed_verified"
            previous = entry if entry.exists() else None
        details = {"runtime_id": runtime_id, "status": status, "version": recipe["version"],
                   'platform': target_platform,
                   "source_url": recipe["source_url"], "source_sha256": recipe["source_sha256"],
                   "source_size_bytes": recipe["source_size_bytes"], "source_origin": "local_file" if from_file else "official_download",
                   "installation_path": str(entry.relative_to(root)), "entry_sha256": entry_sha,
                   "entry_size_bytes": candidate.stat().st_size, "probe": probe,
                   "previous_entry_sha256": _sha256(previous) if previous else "",
                   "game_launch_tested": False}
        operation = romtool.command_record_change(SimpleNamespace(
            change_type=CHANGE_TYPE, details_json=json.dumps(details, ensure_ascii=False),
            reason=f"Instalar {recipe['display_name']} {recipe['version']} portátil a partir da release oficial fixada."))
        opid = operation["operation_id"]
        if status == "installed_verified":
            target_dir.mkdir(exist_ok=True)
            if previous:
                rollback = romtool.operation_dir(opid) / "rollback"
                rollback.mkdir()
                os.replace(previous, rollback / previous.name)
            os.replace(candidate, entry)
        romtool.append_event(opid, "portable_runtime_" + status, action=CHANGE_TYPE, status="completed",
                             destination=details["installation_path"], sha256_after=entry_sha,
                             sha256_before=details["previous_entry_sha256"], size_after=details["entry_size_bytes"],
                             details={"runtime_id": runtime_id, "version": recipe["version"]})
        romtool.rebuild_history_index()
        if from_file == incoming and incoming.is_file():
            incoming.unlink()
        return {"operation_id": opid, **details, "used_by": _used_by(root, recipe)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def rollback_install(root: Path, operation_id: str, confirmed: bool) -> dict:
    if not confirmed:
        raise ValueError("Rollback exige --rollback explícito")
    opdir = romtool.operation_dir(operation_id)
    manifest = json.loads((opdir / "manifest.json").read_text(encoding="utf-8"))
    details = manifest.get("details", {})
    if manifest.get('change_type') == 'native_runtime_package_install' and details.get('status') == 'installed_verified':
        _library_guard(root)
        target = _library_target(root, details['destination'])
        if _library_tree(target) != details['inventory']:
            raise ValueError('Pacote mudou; reverta primeiro configurações/vínculos dependentes e preserve dados novos')
        preserved = opdir / 'rolled-back-package'
        if preserved.exists():
            raise ValueError('Rollback do pacote já existe')
        target.rename(preserved)
        previous = opdir / 'rollback/previous-package'
        restored = previous.is_dir()
        if restored:
            previous.rename(target)
        result = romtool.command_record_change(SimpleNamespace(change_type='native_runtime_package_rollback',
            reason='Reverter somente pacote imutável inalterado, preservando todos os bytes',
            details_json=json.dumps({'runtime_id': details['runtime_id'], 'platform': details['platform'],
                                     'installation_operation_id': operation_id, 'restored_previous': restored,
                                     'preserved_package': preserved.relative_to(root).as_posix()})))
        return {'operation_id': result['operation_id'], 'status': 'rolled_back', 'restored_previous': restored}
    if manifest.get("change_type") != CHANGE_TYPE or details.get("status") != "installed_verified":
        raise ValueError("Operação não é uma instalação portátil aplicada")
    entry = root / details["installation_path"]
    if entry.is_symlink() or not entry.is_file() or _sha256(entry) != details["entry_sha256"]:
        raise ValueError("O executável mudou desde a instalação; rollback recusado")
    undone = opdir / "rolled-back"
    undone.mkdir(exist_ok=False)
    os.replace(entry, undone / entry.name)
    previous = opdir / "rollback" / entry.name
    restored = previous.is_file()
    if restored:
        os.replace(previous, entry)
    record = romtool.command_record_change(SimpleNamespace(
        change_type=ROLLBACK_CHANGE_TYPE, reason=f"Desfazer instalação {operation_id}",
        details_json=json.dumps({"runtime_id": details["runtime_id"], "installation_operation_id": operation_id,
                                 'platform': details.get('platform', 'linux'),
                                 "restored_previous": restored,
                                 "removed_entry_kept_at": str((undone / entry.name).relative_to(root))}, ensure_ascii=False)))
    return {"operation_id": record["operation_id"], "status": "rolled_back", "runtime_id": details["runtime_id"]}


def install_asset(root: Path, asset_id: str, confirmed: bool) -> dict:
    """Install a pinned support file (e.g. a blank HDD image). Mutable user state: never overwritten."""
    if not confirmed:
        raise ValueError("Instalação exige --apply explícito")
    config = load_recipes(root)
    asset = config.get("assets", {}).get(asset_id)
    if not asset:
        raise ValueError(f"Asset sem receita: {asset_id}")
    destination = (root / asset["destination"])
    if not destination.resolve().is_relative_to((root / "emulator/data").resolve()):
        raise ValueError("Destino de asset fora de emulator/data")
    _library_target(root, destination.relative_to(root).as_posix())
    if os.path.lexists(destination):
        return {"asset_id": asset_id, "status": "already_present_preserved", "destination": asset["destination"],
                "note": "Arquivo existente guarda estado do usuário; não foi substituído."}
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{asset_id}-", dir=destination.parent))
    try:
        source = staging / "source"
        _download(asset["source_url"], source, asset["source_size_bytes"], set(config["allowed_download_hosts"]))
        if source.stat().st_size != asset["source_size_bytes"] or _sha256(source) != asset["source_sha256"]:
            raise ValueError("Tamanho ou SHA-256 do asset não conferem; nada foi instalado")
        candidate = staging / destination.name
        _extract_member(source, {"member": asset["member"]}, candidate)
        if (candidate.stat().st_size != asset["member_size_bytes"] or _sha256(candidate) != asset["member_sha256"]
                or candidate.read_bytes()[:4] != asset["magic"].encode("latin-1")):
            raise ValueError("Conteúdo extraído não confere com a receita")
        os.link(candidate, destination)  # fails if the destination appeared meanwhile; never overwrites
        details = {"asset_id": asset_id, "status": "installed_verified", "version": asset["version"],
                   "source_url": asset["source_url"], "source_sha256": asset["source_sha256"],
                   "hash_origin": asset.get("hash_origin", ""), "destination": asset["destination"],
                   "sha256": asset["member_sha256"], "size_bytes": asset["member_size_bytes"]}
        operation = romtool.command_record_change(SimpleNamespace(
            change_type="portable_support_asset_install", details_json=json.dumps(details, ensure_ascii=False),
            reason=f"Instalar {asset['display_name']} a partir da release oficial fixada."))
        return {"operation_id": operation["operation_id"], **details}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
