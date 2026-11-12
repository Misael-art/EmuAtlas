"""Portable Eden-NX2 diagnostics; --check never starts the emulator."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'emulator/linux/eden-nx2'
USERDATA = ROOT / 'emulator/data/Eden-NX2'


def program_id(path):
    raw = path.read_bytes()
    if len(raw) < 0x80 or raw[:4] != b'META':
        raise ValueError(f'NPDM inválido: {path}')
    offset = struct.unpack_from('<I', raw, 0x70)[0]
    if offset + 24 > len(raw) or raw[offset:offset+4] != b'ACI0':
        raise ValueError(f'ACI0 inválido: {path}')
    return struct.unpack_from('<Q', raw, offset + 16)[0]


def has_romfs(exefs):
    return (exefs.parent / 'romfs').is_dir() or any(p.is_file() for p in exefs.glob('*.romfs'))


def is_nso(path):
    with path.open('rb') as f:
        return f.read(4) == b'NSO0'


def resolve_dump(item, rom_root):
    item = Path(item).resolve(strict=True)
    if not item.is_relative_to(rom_root.resolve()):
        raise ValueError('Entrada fora de roms/switch2')
    candidates = [item] if item.is_dir() else [item.parent] if item.name == 'main' else []
    if item.is_dir():
        candidates += [item / 'base/exefs', item / 'exefs']
    for exefs in candidates:
        if not (exefs / 'main').is_file() or not (exefs / 'main.npdm').is_file():
            continue
        if not is_nso(exefs / 'main'):
            raise ValueError('main não é NSO0')
        tid = program_id(exefs / 'main.npdm')
        if has_romfs(exefs):
            return exefs, 'base_or_explicit_exefs_with_romfs', tid
        if exefs.parent.name == 'base':
            alternatives = [p for p in exefs.parent.parent.glob('updates/*/exefs')
                            if (p / 'main').is_file() and (p / 'main.npdm').is_file()
                            and has_romfs(p) and program_id(p / 'main.npdm') == tid
                            and p.resolve().is_relative_to(rom_root.resolve()) and is_nso(p / 'main')]
            if len(alternatives) == 1:
                return alternatives[0], 'base_romfs_missing_single_matching_update', tid
        raise ValueError(f'RomFS ausente ou conjunto ambíguo: {exefs}')
    raise ValueError('Não encontrei main + main.npdm no dump')


def environment():
    env = dict(os.environ)
    for key, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache')]:
        env[key] = str(USERDATA / sub)
    env['LD_LIBRARY_PATH'] = str(RUNTIME / 'deps/usr/lib') + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    env['APPIMAGELAUNCHER_DISABLE'] = '1'
    return env


def check():
    info = json.loads((RUNTIME / 'build-info.json').read_text())
    binary = RUNTIME / info['entry']
    with binary.open('rb') as f:
        magic = f.read(4)
        f.seek(0)
        hasher = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b''):
            hasher.update(block)
        digest = hasher.hexdigest()
    if magic != b'\x7fELF' or digest != info['entry_sha256'] or not os.access(binary, os.X_OK):
        raise ValueError('Binário ausente, não executável ou divergente do manifesto da build')
    flags = set(Path('/proc/cpuinfo').read_text().lower().split())
    required = {'avx', 'avx2', 'bmi1', 'bmi2', 'f16c', 'fma', 'movbe', 'xsave',
                'sse4_1', 'sse4_2', 'ssse3', 'cx16', 'lahf_lm', 'popcnt'}
    if required - flags or not ({'abm', 'lzcnt'} & flags):
        raise ValueError('CPU sem recursos x86-64-v3 exigidos: ' + ', '.join(sorted(required - flags)))
    result = subprocess.run(['ldd', '-r', str(binary)], env=environment(), capture_output=True, text=True, timeout=10)
    if result.returncode or any(x in result.stdout + result.stderr for x in ['not found', 'undefined symbol']):
        raise ValueError('Dependências não resolvidas: ' + result.stdout + result.stderr)
    return {'status': 'runtime_ready_unverified', 'entry': str(binary.relative_to(ROOT)),
            'sha256': digest, 'version': info['version'], 'gameplay_tested': False,
            'probe': 'ELF/hash/CPU/dependencies; no emulator process or GUI'}


def launch_lock(directory):
    import fcntl
    fd = os.open(directory / 'session.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)  # Kept by the emulator; released on normal exit or crash.
        return fd
    except BlockingIOError:
        os.close(fd)
        raise ValueError('Uma sessão Eden-NX2 já está ativa; encerre-a normalmente.')


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args[:1] in [['--help'], ['-h']]:
            print('Eden-NX2: --check | --version | --resolve <dump> | <dump>. Sem argumento abre a GUI.')
            return 0
        if args[:1] in [['--version'], ['-v']]:
            print('Eden-NX2 ' + json.loads((RUNTIME / 'build-info.json').read_text())['version'] + ' (metadata only)')
            return 0
        if args[:1] == ['--check']:
            print(json.dumps(check(), ensure_ascii=False))
            return 0
        resolution = None
        resolve_only = args[:1] == ['--resolve']
        if resolve_only:
            args.pop(0)
        while args[:1] in [['-f'], ['--fullscreen']]:
            args.pop(0)
        if args[:1] in [['-g'], ['--game']]:
            args.pop(0)
            if not args:
                raise ValueError('-g/--game exige um dump')
        if args:
            target, reason, tid = resolve_dump(args.pop(0), ROOT / 'roms/switch2')
            resolution = {'target': str(target.relative_to(ROOT)), 'reason': reason,
                          'program_id': f'{tid:016x}', 'gameplay_tested': False}
        if resolve_only:
            if resolution is None:
                raise ValueError('--resolve exige entrada')
            print(json.dumps(resolution, ensure_ascii=False))
            return 0
        check()
        for sub in ['config', 'data', 'cache']:
            (USERDATA / sub).mkdir(parents=True, exist_ok=True)
        binary = RUNTIME / json.loads((RUNTIME / 'build-info.json').read_text())['entry']
        command = [str(binary)] + (['-f', '-g', str(ROOT / resolution['target'])] if resolution else []) + args
        fd = launch_lock(USERDATA)
        try:
            os.execve(binary, command, environment())
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print('Eden-NX2: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
