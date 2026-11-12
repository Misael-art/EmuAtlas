"""Managed Windows directory junctions for runtimes with fixed data folders.

Only the explicitly declared directories are bound. Personal directories are
preserved, NTFS is checked, and all changes use the official operation journal.
No shell, administrator elevation, payload copying or home fallback is used.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace


def is_junction(path: Path) -> bool:
    return bool(getattr(path, 'is_junction', lambda: False)())


def junction_buffer(target: str) -> bytes:
    substitute = ('\\??\\' + target).encode('utf-16le')
    display = target.encode('utf-16le')
    paths = substitute + b'\0\0' + display + b'\0\0'
    payload = struct.pack('<HHHH', 0, len(substitute), len(substitute) + 2, len(display)) + paths
    return struct.pack('<IHH', 0xA0000003, len(payload), 0) + payload


def create_junction(path: Path, target: Path) -> None:
    if os.name != 'nt':
        raise ValueError('Junctions só são materializadas no Windows nativo')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    ioctl = kernel.DeviceIoControl
    ioctl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    ioctl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    volume = ctypes.create_unicode_buffer(32768)
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    if not kernel.GetVolumePathNameW(str(path.parent), volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    filesystem = ctypes.create_unicode_buffer(64)
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.restype = wintypes.BOOL
    if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem)):
        raise ctypes.WinError(ctypes.get_last_error())
    if filesystem.value.upper() != 'NTFS':
        raise ValueError(f'{filesystem.value} não suporta os vínculos exigidos por este runtime; dados preservados')
    path.mkdir()
    handle = create(str(path), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        path.rmdir()  # Newly created, still empty; no user payload.
        raise ctypes.WinError(ctypes.get_last_error())
    success = False
    try:
        data = junction_buffer(str(target))
        buffer = ctypes.create_string_buffer(data)
        count = wintypes.DWORD()
        if not ioctl(handle, 0x000900A4, buffer, len(data), None, 0, ctypes.byref(count), None):
            error = ctypes.get_last_error()
            raise ctypes.WinError(error)
        success = True
    finally:
        kernel.CloseHandle(handle)
        if not success:
            path.rmdir()


def safe_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Caminho de vínculo inválido')
    if relative.parts[0] not in ('emulator', 'bios', 'media', 'frontend', 'saves', 'mods'):
        raise ValueError('Vínculo fora das raízes canônicas')
    path = root / relative
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink() or is_junction(parent):
            raise ValueError('Ancestral de vínculo não gerenciado: ' + str(parent))
    return path


def binding_rows(root: Path, runtime_id: str | None = None) -> list[dict]:
    config = json.loads((root / 'tools/roms/config/portable-runtimes.json').read_text())
    state_path = root / 'outputs/emulator-native-bindings.json'
    state = json.loads(state_path.read_text()).get('bindings', {}) if state_path.is_file() else {}
    rows = []
    for rid, contract in config.get('windows_launch', {}).items():
        if runtime_id and rid != runtime_id:
            continue
        for source, destination in contract.get('bindings', {}).items():
            source_path, target = safe_path(root, source), safe_path(root, destination)
            exists = os.path.lexists(source_path)
            kind = 'junction' if is_junction(source_path) else 'symlink' if source_path.is_symlink() else 'directory' if source_path.is_dir() else 'file' if exists else 'absent'
            literal = os.readlink(source_path) if kind in ('junction', 'symlink') else None
            owned = source in state and state[source].get('literal') == literal
            correct = kind == 'junction' and owned and source_path.resolve() == target.resolve()
            rows.append({'runtime_id': rid, 'source': source, 'destination': destination, 'kind': kind,
                         'literal': literal,
                         'status': 'verified' if correct else 'preparation_required' if not exists else 'blocked_existing_path'})
    return rows


@contextmanager
def configuration_lock(root: Path):
    path = root / 'outputs/roms-consolidacao-2026-10-24/operations/emulator-controls.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


def assert_closed(root: Path) -> None:
    if os.name != 'nt':
        import runtime_install
        runtime_install._library_guard(root)
        return
    import subprocess
    active = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True, check=True).stdout.casefold()
    names = {'retrofe.exe', 'es-de.exe', 'emulationstation.exe'}
    config = json.loads((root / 'tools/roms/config/portable-runtimes.json').read_text())
    names.update(Path(recipe['entry']).name.casefold() for rid, recipe in config.get('packages', {}).get('windows', {}).items()
                 if rid not in ('python', '7zip'))
    names.add('emulator.exe')
    if any(('"' + name + '"') in active for name in names):
        raise ValueError('Feche normalmente os frontends e emuladores antes de alterar seus caminhos')


def prepare(root: Path, confirmed: bool = False) -> dict:
    if os.name != 'nt':
        raise ValueError('Prepare deve ser executado no Windows nativo; nenhum EXE será executado por Wine')
    import romtool
    with configuration_lock(root):
        rows = binding_rows(root)
        state_path = root / 'outputs/emulator-native-bindings.json'
        previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
        managed = previous.get('bindings', {})
        for row in rows:
            if row['status'] == 'blocked_existing_path':
                prior = managed.get(row['source'])
                if row['kind'] == 'junction' and prior and row['literal'] == prior.get('literal'):
                    row['status'] = 'managed_relocation_required'
        blocked = [row for row in rows if row['status'] == 'blocked_existing_path']
        if blocked:
            raise ValueError('Dados/vínculos pessoais preservados: ' + json.dumps(blocked))
        if not confirmed:
            return {'status': 'prepared', 'bindings': rows, 'apply_required': True}
        assert_closed(root)
        changes = [row for row in rows if row['status'] != 'verified']
        if not changes:
            return {'status': 'already_prepared_verified', 'bindings': len(rows)}
        record = romtool.command_record_change(SimpleNamespace(change_type='native_windows_shared_bindings',
            reason='Materializar somente vínculos declarados para dados canônicos únicos, sem copiar payloads',
            details_json=json.dumps({'bindings': changes, 'status': 'prepared', 'project_root': str(root)})))
        opid = record['operation_id']
        record.update(status='prepared', completed_at=None)
        romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json', record)
        record['details']['applied_bindings'] = {}
        for row in changes:
            source, target = safe_path(root, row['source']), safe_path(root, row['destination'])
            source.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir(parents=True, exist_ok=True)
            if is_junction(source):
                source.rmdir()  # Only a managed reparse point, never its payload.
            create_junction(source, target)
            if source.resolve() != target.resolve():
                raise ValueError('Vínculo não resolveu para a origem canônica')
            managed[row['source']] = {**row, 'literal': os.readlink(source), 'operation_id': opid}
            record['details']['applied_bindings'][row['source']] = managed[row['source']]
            record['details']['status'] = 'partially_applied'
            record['status'] = 'partially_applied'
            romtool.atomic_json(state_path, {'project_root': str(root), 'bindings': managed})
            romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json', record)
            romtool.append_event(opid, 'binding_verified', action='native_windows_shared_bindings',
                                 status='verified', source=row['source'], destination=row['destination'])
        romtool.atomic_json(state_path, {'project_root': str(root), 'bindings': managed})
        record['details']['status'] = 'completed_verified'
        record.update(status='completed_verified', completed_at=romtool.now())
        record['details']['applied_bindings'] = {row['source']: managed[row['source']] for row in changes}
        romtool.atomic_json(romtool.operation_dir(opid) / 'manifest.json', record)
        romtool.rebuild_history_index()
        return {'operation_id': opid, 'status': 'completed_verified', 'bindings': len(rows),
                'rollback': 'Only remove unchanged managed junctions; payloads always stay at destination'}


def rollback(root: Path, operation_id: str, confirmed: bool = False) -> dict:
    if os.name != 'nt' or not confirmed:
        raise ValueError('Rollback requer Windows nativo e --apply')
    import romtool
    with configuration_lock(root):
        assert_closed(root)
        path = romtool.operation_dir(operation_id) / 'manifest.json'
        record = json.loads(path.read_text())
        if record.get('change_type') != 'native_windows_shared_bindings' or record.get('details', {}).get('status') not in ('completed_verified', 'partially_applied'):
            raise ValueError('Operação de vínculos não está concluída/verificada')
        applied = record['details']['applied_bindings']
        for source, row in applied.items():
            link = safe_path(root, source)
            if not is_junction(link) or os.readlink(link) != row['literal']:
                raise ValueError('Vínculo mudou após preparação: ' + source)
        state_path = root / 'outputs/emulator-native-bindings.json'
        state = json.loads(state_path.read_text())
        for row in reversed(record['details']['bindings']):
            if row['source'] not in applied:
                continue
            link = safe_path(root, row['source'])
            link.rmdir()
            if row['kind'] == 'junction':
                old = row['literal'].removeprefix('\\\\?\\').removeprefix('\\??\\')
                create_junction(link, Path(old))
                state['bindings'][row['source']] = {**row, 'literal': os.readlink(link)}
            else:
                state['bindings'].pop(row['source'], None)
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        record['details']['status'] = 'rolled_back'
        record['status'] = 'rolled_back'
        romtool.atomic_json(path, record)
        romtool.append_event(operation_id, 'bindings_rolled_back', action='native_windows_shared_bindings', status='rolled_back')
        romtool.rebuild_history_index()
        return {'operation_id': operation_id, 'status': 'rolled_back', 'payloads_preserved': True}
