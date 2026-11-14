#!/usr/bin/env python3
"""Install the declared host ecosystem, resume safely, and report each real outcome."""
from __future__ import annotations

import argparse
import datetime as dt
from email.parser import BytesParser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
import uuid
from urllib.request import Request, build_opener

import workspace as w

sys.path.insert(0, str(w.SOURCE_ROOT / 'tools/roms'))
import romtool

RECIPE_PATH = w.HERE / "config/installation.json"
GOOD = {"installed", "verified", "reused", "configured"}


def os_info(path=Path('/etc/os-release')):
    values = {}
    for line in path.read_text().splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            values[k] = v.strip('"')
    family = values.get('ID', '') + ' ' + values.get('ID_LIKE', '')
    manager = next((m for names, m in [(('arch', 'manjaro', 'biglinux'), 'pacman'),
                                      (('debian', 'ubuntu'), 'apt-get'),
                                      (('fedora', 'rhel'), 'dnf')] if any(n in family.split() for n in names)), None)
    immutable = values.get('ID') in {'steamos', 'bazzite', 'silverblue', 'kinoite'} or Path('/run/ostree-booted').exists()
    return {"id": values.get('ID'), "manager": manager, "immutable": immutable}


def atomic_json(path, value):
    romtool.atomic_json(path, value)


def python_host():
    return {'python': platform.python_version(), 'implementation': sys.implementation.name,
            'abi': sysconfig.get_config_var('SOABI'), 'platform': sysconfig.get_platform(),
            'libc': list(platform.libc_ver()), 'distribution': os_info()['id']}


def wheel_requirements(files):
    return ''.join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
                   for item in sorted(files, key=lambda item: item['name']))


def pinned_python_packages(packages):
    result = {}
    for package in packages:
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)', package)
        if not match:
            raise ValueError('Requisito Python deve ter nome e versão exatos: ' + package)
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in result:
            raise ValueError('Requisito Python repetido: ' + name)
        result[name] = match[2]
    if not result:
        raise ValueError('Receita Python vazia')
    return result


def wheel_inventory(directory):
    files, names = [], set()
    for path in sorted(directory.glob('*.whl')):
        if path.is_symlink() or not path.is_file():
            raise ValueError('Wheel deve ser arquivo regular')
        with zipfile.ZipFile(path) as archive:
            metadata_files = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            if len(metadata_files) != 1 or archive.testzip() is not None:
                raise ValueError('Wheel inválido: ' + path.name)
            metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
        name, version = metadata.get('Name', ''), metadata.get('Version', '')
        if (len(metadata.get_all('Name', [])) != 1 or len(metadata.get_all('Version', [])) != 1
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+!_-]*', version)):
            raise ValueError('Identidade inválida no wheel: ' + path.name)
        name = re.sub(r'[-_.]+', '-', name).lower()
        if name in names:
            raise ValueError('Distribuição repetida no wheelhouse: ' + name)
        names.add(name)
        files.append({'file': path.name, 'name': name, 'version': version,
                      'size_bytes': path.stat().st_size, 'sha256': w.digest(path)})
    if not files:
        raise ValueError('Wheelhouse vazio')
    return files


def verify_wheelhouse(directory, item):
    directory = w.root_path(directory)
    manifest = w.read(w.safe_path(directory, 'manifest.json'))
    if (manifest.get('schema_version') != 1 or manifest.get('host') != python_host()
            or manifest.get('packages') != item['packages']
            or manifest.get('source_build_packages') != item.get('source_build_packages', [])):
        raise ValueError('Wheelhouse pertence a outro host Python ou receita')
    expected = {row['file'] for row in manifest['files']} | {'manifest.json', 'requirements.txt'}
    if {path.name for path in directory.iterdir()} != expected:
        raise ValueError('Arquivos inesperados/ausentes no wheelhouse')
    actual = wheel_inventory(directory)
    if actual != manifest['files']:
        raise ValueError('Wheelhouse alterado após preparação')
    requirements = w.safe_path(directory, 'requirements.txt')
    if requirements.read_text() != wheel_requirements(actual):
        raise ValueError('Lock Python alterado após preparação')
    available = {row['name']: row['version'] for row in actual}
    for name, version in pinned_python_packages(item['packages']).items():
        if available.get(name) != version:
            raise ValueError('Requisito pinado ausente no wheelhouse: ' + name)
    return manifest


def graph(config, selected=None):
    items = config['components']
    output, visiting, done = [], set(), set()
    def visit(name):
        if name in done:
            return
        if name not in items or name in visiting:
            raise ValueError('Dependência ausente/cíclica: ' + name)
        visiting.add(name)
        for parent in items[name].get('requires', []):
            visit(parent)
        visiting.remove(name)
        done.add(name)
        output.append(name)
    for name in selected or items:
        visit(name)
    return output


class Installer:
    def __init__(self, root, config=None, jobs=1):
        self.root = w.root_path(root)
        self.config = config or w.read(RECIPE_PATH)
        self.jobs = min(max(int(jobs), 1), 2)
        self.host = os_info()
        self.state = w.safe_path(self.root, 'outputs/installation')
        self.work = w.safe_path(self.root, 'work/installation')
        self.log = None
        self.env = dict(os.environ)
        self.env['CMAKE_BUILD_PARALLEL_LEVEL'] = str(self.jobs)
        self.env['CARGO_BUILD_JOBS'] = str(self.jobs)
        self.env['LC_ALL'] = 'C'

    def run(self, argv, cwd=None, timeout=3600, env=None, bounded=False, require_limits=False):
        """Kill only this command's private process group on timeout or interruption."""
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            raise ValueError('Comando deve ser argv explícito')
        unit = None
        if bounded and shutil.which('systemd-run') and Path(os.environ.get('XDG_RUNTIME_DIR', '/nonexistent'), 'bus').exists():
            unit = 'emulation-build-' + uuid.uuid4().hex
            argv = ['systemd-run', '--user', '--wait', '--pipe', '--collect', '--service-type=exec',
                    '--unit=' + unit, '--property=MemoryMax=5G', '--property=MemorySwapMax=0',
                    '--property=CPUQuota=150%', '--property=TasksMax=256',
                    '--property=RuntimeMaxSec=' + str(timeout), '--setenv=LC_ALL=C',
                    '--working-directory=' + str(cwd or self.root), '--', *argv]
        elif require_limits:
            raise ValueError('Build pesada requer systemd de usuário para limitar RAM/CPU; execução sem limites recusada')
        self.log.write(json.dumps({'argv': argv, 'cwd': str(cwd or self.root)}, ensure_ascii=False) + '\n')
        self.log.flush()
        p = subprocess.Popen(argv, cwd=cwd or self.root, env=env or self.env,
                             stdin=None, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = p.wait(timeout=timeout)
        except BaseException:
            if unit:
                subprocess.run(['systemctl', '--user', 'stop', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            raise
        finally:
            if unit:
                subprocess.run(['systemctl', '--user', 'stop', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if code:
            raise RuntimeError(f'Comando retornou {code}: {argv[0]} (ver log do componente)')

    def query(self, argv):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=self.env)
        return p.returncode, (p.stdout + p.stderr).strip()

    def prepare_wheelhouse(self, name, item, python):
        packages = pinned_python_packages(item['packages'])
        build_names = item.get('source_build_packages', [])
        if any(value not in packages for value in build_names):
            raise ValueError('Build Python deve selecionar pacote pinado da receita')
        identity = {'host': python_host(), 'packages': item['packages'],
                    'source_build_packages': build_names}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        directory = w.safe_path(self.work, 'wheelhouse/' + key)
        directory.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory.parent / 'prepare.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if directory.exists():
                verify_wheelhouse(directory, item)
                romtool.sync_directory_chain(directory)
                return directory
            # Preserve an interrupted preparation for diagnosis and reuse only a
            # complete published cache. Pip's temporary build files stay on disk.
            stage = Path(tempfile.mkdtemp(prefix='.prepare-', dir=directory.parent))
            temporary = stage / 'temporary'
            temporary.mkdir()
            wheels = stage / 'wheels'
            wheels.mkdir()
            env = dict(self.env, TMPDIR=str(temporary), PIP_CACHE_DIR=str(self.work / 'pip-cache'))
            command = [str(python), '-m', 'pip', 'wheel', '--disable-pip-version-check',
                       '--wheel-dir', str(wheels), '--only-binary=:all:']
            if build_names:
                command.append('--no-binary=' + ','.join(build_names))
            self.run([*command, *item['packages']], timeout=900, env=env)
            files = wheel_inventory(wheels)
            for row in files:
                with (wheels / row['file']).open('rb') as stream:
                    os.fsync(stream.fileno())
            romtool.atomic_write_bytes(wheels / 'requirements.txt', wheel_requirements(files).encode())
            atomic_json(wheels / 'manifest.json', {'schema_version': 1, **identity, 'files': files,
                         'prepared_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'component': name})
            verify_wheelhouse(wheels, item)
            # Rename publishes the entire verified closure. Existing cache paths
            # are protected by this preparation lock and are never replaced.
            wheels.rename(directory)
            romtool.sync_directory_chain(directory)
            romtool.sync_directory_chain(stage)
            if not any(temporary.iterdir()):
                temporary.rmdir()
            if not any(stage.iterdir()):
                stage.rmdir()
            return directory
        finally:
            os.close(fd)

    def installed_package(self, package):
        # rustup and older 7-Zip packages may already supply these commands.
        providers = {'rust': ['cargo', 'rustc'], 'cargo': ['cargo'], '7zip': ['7z']}
        if package in providers and all(shutil.which(name) for name in providers[package]):
            return True
        manager = self.host['manager']
        cmd = {'pacman': ['pacman', '-Q', package],
               'apt-get': ['dpkg-query', '-W', '-f=${Status}', package],
               'dnf': ['rpm', '-q', package]}.get(manager)
        if not cmd or not shutil.which(cmd[0]):
            return False
        code, text = self.query(cmd)
        return code == 0 and (manager != 'apt-get' or text == 'install ok installed')

    def available_flatpak(self, app):
        if not shutil.which('flatpak'):
            return False
        # Reuse system or user scope; do not create another copy in the user scope.
        return any(self.query(['flatpak', 'info', scope, app])[0] == 0
                   for scope in ['--user', '--system'])

    def plan(self, selected=None):
        order = graph(self.config, selected)
        result = []
        for name in order:
            item = self.config['components'][name]
            row = {'id': name, 'kind': item['kind'], 'requires': item.get('requires', [])}
            if item['kind'] == 'packages':
                packages = item.get('packages', {}).get(self.host['manager'], [])
                row['missing'] = [p for p in packages if not self.installed_package(p)]
                row['status'] = 'reused' if packages and not row['missing'] else 'planned'
                if not packages or (self.host['immutable'] and row['missing']):
                    row['status'] = 'blocked_host_package_backend'
            elif item['kind'] == 'flatpak':
                row['status'] = 'reused' if self.available_flatpak(item['app_id']) else 'planned'
            elif item['kind'] in {'archive', 'build'}:
                reusable, reason = self.receipt_reuse(name, item, item['kind'])
                row['status'] = 'reused' if reusable else 'planned'
                row['reuse_evidence'] = reason
            else:
                row['status'] = 'planned'
            result.append(row)
        return {'host': self.host, 'components': result, 'downloads': 'only on --apply',
                'changes_host': False, 'all_components_installed': False}

    def acquire(self, source):
        """Content-addressed, verified cache; consumers never trust an incomplete file."""
        if not re.fullmatch('[0-9a-f]{64}', source['sha256']) or not 0 < source['size_bytes'] <= 4 * 1024**3:
            raise ValueError('Fonte sem hash/tamanho válido')
        w.validate_url(source['url'], source['allowed_hosts'])
        target = w.safe_path(self.work, 'downloads/' + source['sha256'])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size != source['size_bytes'] or w.digest(target) != source['sha256']:
                raise ValueError('Cache existente divergente; preservado')
            return target
        if shutil.disk_usage(target.parent).free < source['size_bytes'] * 2 + (128 << 20):
            raise ValueError('Espaço insuficiente para aquisição')
        fd, temp = tempfile.mkstemp(prefix='.download-', dir=target.parent)
        try:
            opener = build_opener(w.RestrictedRedirect(source['allowed_hosts']))
            with os.fdopen(fd, 'wb') as out, opener.open(Request(source['url'], headers={'User-Agent': 'EmulationWorkspace/0.2'}), timeout=60) as response:
                w.validate_url(response.geturl(), source['allowed_hosts'])
                received = 0
                while block := response.read(1 << 20):
                    received += len(block)
                    if received > source['size_bytes']:
                        raise ValueError('Origem ultrapassou tamanho pinado')
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if received != source['size_bytes'] or w.digest(temp) != source['sha256']:
                raise ValueError('SHA-256/tamanho de download divergente')
            os.link(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
        return target

    def publish(self, source, relative, executable=True):
        target = w.safe_path(self.root, relative)
        if target.exists():
            if w.digest(source) == w.digest(target):
                return
            raise ValueError('Destino diferente preservado: ' + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.publish-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as out, Path(source).open('rb') as stream:
                shutil.copyfileobj(stream, out)
            os.chmod(temp, 0o755 if executable else 0o644)
            os.link(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)

    def extract(self, source, target, members=None):
        """Accept only regular files/directories, bounded by pinned source size policy."""
        if target.exists():
            raise ValueError('Staging de extração já existe')
        target.mkdir(parents=True)
        total = 0
        decoded = None
        with Path(source).open('rb') as header:
            zstd = header.read(4) == b'\x28\xb5\x2f\xfd'
        if zstd:
            # Python before 3.14 does not read .tar.zst natively.
            decoded = target.parent / (target.name + '.decoded.tar')
            self.run(['zstd', '--decompress', '--no-progress', str(source), '-o', str(decoded)], timeout=120)
            source = decoded
        try:
            self._extract_tar(source, target, members)
        finally:
            if decoded is not None:
                decoded.unlink(missing_ok=True)

    def _extract_tar(self, source, target, selected=None):
        total = 0
        with tarfile.open(source) as archive:
            members = archive.getmembers()
            if selected is not None:
                members = [member for member in members if member.name in selected]
                if {member.name for member in members} != set(selected):
                    raise ValueError('Archive sem todos os membros explícitos')
            for member in members:
                if not (member.isfile() or member.isdir()):
                    raise ValueError('Link/dispositivo em archive recusado: ' + member.name)
                w.safe_path(target, member.name)
                total += member.size
                if total > 8 * 1024**3:
                    raise ValueError('Archive excede limite de extração')
            for member in members:
                path = w.safe_path(target, member.name)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as stream, path.open('xb') as out:
                        shutil.copyfileobj(stream, out)

    def native_packages(self, item):
        packages = item['packages'].get(self.host['manager'])
        if not packages:
            raise ValueError('Família de distribuição sem receita de pacotes')
        missing = [p for p in packages if not self.installed_package(p)]
        if not missing:
            return {'status': 'reused', 'packages': packages}
        if self.host['immutable']:
            raise ValueError('Host imutável: não será desbloqueado para instalar pacotes nativos')
        manager = self.host['manager']
        args = {'pacman': ['pacman', '-S', '--needed', '--noconfirm'],
                'apt-get': ['apt-get', 'install', '-y'], 'dnf': ['dnf', 'install', '-y']}[manager]
        privilege = []
        if os.geteuid() != 0:
            if shutil.which('pkexec'):
                privilege = ['pkexec']
            elif shutil.which('sudo'):
                privilege = ['sudo']
            else:
                raise ValueError('Sem agente administrativo para instalar pacotes faltantes')
        # Never update the whole host, disable signatures or add arbitrary package repositories.
        self.run(privilege + args + missing, timeout=3600)
        absent = [p for p in packages if not self.installed_package(p)]
        if absent:
            raise ValueError('Pacotes não confirmados depois da instalação: ' + ', '.join(absent))
        return {'status': 'installed', 'packages': packages}

    def flatpak(self, item):
        app = item['app_id']
        for command in item.get('host_commands', []):
            found = shutil.which(command)
            if found and not Path(found).resolve().is_relative_to(self.root):
                return {'status': 'reused', 'native_command': command}
        if self.available_flatpak(app):
            return {'status': 'reused', 'app_id': app}
        if not shutil.which('flatpak'):
            raise ValueError('Flatpak indisponível após dependência de pacotes')
        self.run(['flatpak', 'remote-add', '--user', '--if-not-exists', 'flathub',
                  'https://dl.flathub.org/repo/flathub.flatpakrepo'], timeout=120)
        self.run(['flatpak', 'install', '--user', '--noninteractive', '-y', 'flathub', app], timeout=3600)
        if not self.available_flatpak(app):
            raise ValueError('Flatpak não confirmado após install')
        return {'status': 'installed', 'app_id': app, 'actual_commit': self.query(['flatpak', 'info', '--show-commit', app])[1]}

    def runtime(self, item):
        sys.path.insert(0, str(self.root / 'tools/roms'))
        import runtime_install as runtime
        import romtool
        if romtool.PROJECT_ROOT != self.root:
            raise ValueError('Execute o instalador a partir da release implantada no destino')
        config = runtime.load_recipes(self.root)
        if item.get('package_platform'):
            platform = item['package_platform']
            prepared = runtime.install_package(self.root, item['runtime_id'], platform)
            if prepared['status'] == 'already_installed_verified':
                result, status = prepared, 'reused'
            else:
                recipe = config['packages'][platform][item['runtime_id']]
                source = self.acquire({'url': recipe['source_url'], 'size_bytes': recipe['source_size_bytes'],
                                       'sha256': recipe['source_sha256'], 'allowed_hosts': config['allowed_download_hosts']})
                result = runtime.install_package(self.root, item['runtime_id'], platform, True, from_file=source)
                status = 'installed'
            data = None
            if platform == 'linux' and item['runtime_id'] == 'supermodel':
                data = runtime.supermodel_data_plan(self.root)
                if data['status'] == 'prepared':
                    runtime.library_execute(self.root, data['operation_id'], 'apply', True)
                    data = runtime.library_execute(self.root, data['operation_id'], 'verify')
            return {'status': status, 'operation_id': result['operation_id'], 'shared_data': data}
        recipe = config['runtimes'].get(item['runtime_id'])
        if recipe is None:
            template = w.read(w.HERE / 'templates/tools/roms/config/portable-runtimes.json')
            recipe = template['runtimes'][item['runtime_id']]
        flatpaks, _ = runtime.host_flatpaks()
        existing = runtime.scan_host(self.root, recipe, flatpaks)
        if existing:
            result = {'status': 'reused', 'host_sources': existing}
        else:
            if item['runtime_id'] not in config['runtimes']:
                path = self.root / 'tools/roms/config/portable-runtimes.json'
                backup = self.state / 'backups' / ('portable-runtimes.' + w.digest(path) + '.json')
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(path, backup)
                config['runtimes'][item['runtime_id']] = recipe
                atomic_json(path, config)
            target = self.root / 'emulator/linux' / recipe.get('install_dir', item['runtime_id']) / recipe['entry']
            if target.is_symlink():
                raise ValueError('Runtime symlink preservado')
            expected = recipe.get('entry_sha256') or (recipe['source_sha256'] if recipe['format'] == 'appimage' else None)
            if target.is_file() and expected and w.digest(target) == expected:
                result = {'status': 'verified', 'entry': str(target.relative_to(self.root))}
            else:
                source = self.acquire({'url': recipe['source_url'], 'size_bytes': recipe['source_size_bytes'],
                                       'sha256': recipe['source_sha256'], 'allowed_hosts': config['allowed_download_hosts']})
                installed = runtime.install_runtime(self.root, item['runtime_id'], True, from_file=source,
                                                    run_declared_probe=False,
                                                    defer_history_index=item['runtime_id'] == 'vita3k')
                result = {'status': 'installed', 'operation_id': installed['operation_id']}
        if item['runtime_id'] in ('core-bluemsx', 'vita3k'):
            data = (runtime.bluemsx_cbios_plan(self.root) if item['runtime_id'] == 'core-bluemsx'
                    else runtime.vita3k_data_plan(self.root))
            if data['status'] in ('prepared', 'application_resume_required'):
                runtime.library_execute(self.root, data['operation_id'], 'apply', True)
            if data['status'] in ('prepared', 'application_resume_required', 'verification_resume_required'):
                data = runtime.library_execute(self.root, data['operation_id'], 'verify')
            result['shared_data'] = data
        return result

    def source_tree(self, name, item):
        parent = w.safe_path(self.work, 'builds/' + name)
        parent.mkdir(parents=True, exist_ok=True)
        source = parent / 'source'
        if item.get('repository'):
            if not re.fullmatch('[0-9a-f]{40}', item['commit']) or not item['repository'].startswith('https://github.com/'):
                raise ValueError('Git precisa repositório HTTPS e commit exato')
            if not source.exists():
                self.run(['git', 'init', str(source)])
                self.run(['git', '-C', str(source), 'remote', 'add', 'origin', item['repository']])
            elif not (source / '.git').is_dir():
                raise ValueError('Fonte existente sem proveniência Git')
            status = self.query(['git', '-C', str(source), 'status', '--porcelain'])[1]
            if status:
                if self.query(['git', '-C', str(source), 'rev-parse', 'HEAD'])[1] != item['commit']:
                    raise ValueError('Fonte Git alterada em outro commit; preservada')
                for row in status.splitlines():
                    change = row.strip().split(' ', 1)
                    patches = [patch for patch in item.get('patches', [])
                               if len(change) == 2 and patch['file'] == change[1]]
                    if len(change) != 2 or change[0] != 'M' or not patches:
                        raise ValueError('Fonte Git com alterações pessoais; preservada')
                    data = w.safe_path(source, change[1]).read_bytes()
                    for patch in reversed(patches):
                        if data.count(patch['after'].encode()) == 1:
                            data = data.replace(patch['after'].encode(), patch['before'].encode())
                    original = subprocess.check_output(
                        ['git', '-C', str(source), 'show', 'HEAD:' + change[1]], timeout=30)
                    if data != original:
                        raise ValueError('Fonte Git diverge dos patches revisados; preservada')
                return parent, source
            self.run(['git', '-C', str(source), 'sparse-checkout', 'init', '--cone'])
            self.run(['git', '-C', str(source), 'sparse-checkout', 'set', *item['sparse_paths']])
            self.run(['git', '-C', str(source), 'fetch', '--depth=1', '--filter=blob:none', 'origin', item['commit']], timeout=600)
            self.run(['git', '-C', str(source), 'checkout', '--detach', item['commit']])
            if self.query(['git', '-C', str(source), 'rev-parse', 'HEAD'])[1] != item['commit']:
                raise ValueError('Commit Git divergente')
        else:
            pinned = self.acquire(item['source'])
            marker = parent / 'source-origin.json'
            if source.exists() and (not marker.exists() or w.read(marker).get('sha256') != item['source']['sha256']):
                raise ValueError('Fonte extraída existente sem origem confirmada')
            if not source.exists():
                stage = Path(tempfile.mkdtemp(prefix='.extract-', dir=parent))
                try:
                    self.extract(pinned, stage / 'archive')
                    contents = list((stage / 'archive').iterdir())
                    selected = contents[0] if len(contents) == 1 and contents[0].is_dir() else stage / 'archive'
                    os.rename(selected, source)
                    atomic_json(marker, {'sha256': item['source']['sha256'], 'files': {
                        p.relative_to(source).as_posix(): w.digest(p) for p in source.rglob('*') if p.is_file()}})
                finally:
                    shutil.rmtree(stage)
            baseline = w.read(marker).get('files')
            if not baseline:
                raise ValueError('Inventário da fonte ausente; revisar staging existente')
            for relative, expected in baseline.items():
                p = w.safe_path(source, relative)
                data = p.read_bytes()
                if hashlib.sha256(data).hexdigest() != expected:
                    patches = [patch for patch in item.get('patches', []) if patch['file'] == relative]
                    for patch in reversed(patches):
                        if data.count(patch['after'].encode()) == 1:
                            data = data.replace(patch['after'].encode(), patch['before'].encode())
                    if hashlib.sha256(data).hexdigest() != expected:
                        raise ValueError('Fonte mudou desde a aquisição: ' + relative)
        return parent, source

    def build(self, name, item):
        if item.get('requires_cpu') == 'x86-64-v3':
            flags = Path('/proc/cpuinfo').read_text()
            if not all(re.search(r'\b' + f + r'\b', flags) for f in ['avx2', 'bmi2', 'fma']):
                raise ValueError('CPU sem instruções x86-64-v3 requeridas')
        target = w.safe_path(self.root, item['target'])
        if os.path.lexists(target):
            reusable, reason = self.receipt_reuse(name, item, 'build')
            if not reusable:
                # Existing/customized binary without matching provenance: preserved, never rebuilt over.
                raise ValueError(f'Destino de build existente sem proveniência válida ({reason}); preservado: ' + item['target'])
            self.check_elf(target)
            return {'status': 'reused', 'sha256': w.digest(target), 'receipt': 'verified', 'gameplay_verified': False}
        if shutil.disk_usage(self.root).free < item.get('minimum_free_bytes', 3 * 1024**3):
            raise ValueError('Espaço insuficiente para compilação')
        parent, source = self.source_tree(name, item)
        for patch in item.get('patches', []):
            path = w.safe_path(source, patch['file'])
            data = path.read_bytes()
            if data.count(patch['after'].encode()) == 1:
                continue
            if data.count(patch['before'].encode()) == 1:
                path.write_bytes(data.replace(patch['before'].encode(), patch['after'].encode()))
            else:
                raise ValueError('Patch não corresponde à fonte pinada')
        substitutions = {'{source}': str(source), '{build}': str(parent / 'build'),
                         '{prefix}': str(parent / 'prefix'), '{jobs}': str(self.jobs)}
        def expand(value):
            for k, v in substitutions.items():
                value = value.replace(k, v)
            return value
        for command in item['commands']:
            self.run([expand(a) for a in command], cwd=source, timeout=item.get('timeout', 3600),
                     bounded=True, require_limits=item.get('require_resource_limits', False))
        output = Path(expand(item['output']))
        self.check_elf(output)
        self.publish(output, item['target'])
        atomic_json(parent / 'build-receipt.json', {'source': item.get('commit', item.get('source', {})),
                                                   'target': item['target'], 'sha256': w.digest(target),
                                                   'jobs': self.jobs, 'gameplay_verified': False})
        self.write_receipt(name, item, 'build', [item['target']])
        return {'status': 'installed', 'target': item['target'], 'sha256': w.digest(target)}

    # F05: reuse needs provenance, not just "an ELF exists at the path".
    def recipe_digest(self, item):
        return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def receipt_path(self, name):
        return w.safe_path(self.state, 'receipts/' + name + '.json')

    def write_receipt(self, name, item, kind, files):
        atomic_json(self.receipt_path(name), {
            'schema_version': 1, 'component': name, 'kind': kind, 'recipe_sha256': self.recipe_digest(item),
            'source': item.get('source', {}).get('sha256') or item.get('commit'), 'version': item.get('version'),
            'files': {rel: w.digest(w.safe_path(self.root, rel)) for rel in sorted(files)}})

    def receipt_reuse(self, name, item, kind):
        """Return (reusable, reason). Every recorded member must still exist with its hash."""
        path = self.receipt_path(name)
        if not path.is_file():
            return False, 'sem recibo de instalação'
        try:
            receipt = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return False, 'recibo ilegível'
        if receipt.get('kind') != kind or receipt.get('recipe_sha256') != self.recipe_digest(item):
            return False, 'receita/versão diferente do recibo'
        for rel, digest in receipt.get('files', {}).items():
            target = w.safe_path(self.root, rel)
            if target.is_symlink() or not target.is_file():
                return False, 'arquivo instalado ausente ou symlink: ' + rel
            if w.digest(target) != digest:
                return False, 'arquivo instalado alterado: ' + rel
        return bool(receipt.get('files')), 'recibo verificado'

    def check_elf(self, path):
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                raise ValueError('Entrada não é ELF; preservada')
        code, result = self.query(['ldd', '-r', str(path)])
        if 'not found' in result or 'undefined symbol' in result:
            raise ValueError('Dependências dinâmicas ausentes: ' + str(path))
        # A nonzero code on a static executable is valid; arbitrary errors are not.
        if code and not any(text in result for text in ['statically linked', 'not a dynamic executable']):
            raise ValueError('Não consegui confirmar dependências ELF: ' + str(path))

    def archive(self, name, item):
        directory = w.safe_path(self.root, item['target_dir'])
        entry = w.safe_path(directory, item['entry'])
        reusable, reason = self.receipt_reuse(name, item, 'archive')
        if reusable:
            if item.get('entry_kind') == 'elf':
                self.check_elf(entry)
            return {'status': 'reused', 'sha256': w.digest(entry), 'receipt': 'verified', 'gameplay_verified': False}
        # No valid receipt: re-derive from the pinned source. Identical files are adopted, partial
        # publications are completed, and any differing/customized file blocks (preserved).
        source = self.acquire(item['source'])
        parent = w.safe_path(self.work, 'archives/' + name)
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.unpack-', dir=parent))
        try:
            if item['format'] == 'zip':
                from runtime_install import _extract_package
                _extract_package(source, stage / 'payload', {
                    'format': 'zip_tree', 'max_unpacked_bytes': item.get('max_unpacked_bytes', 2 << 30)})
                stage = stage / 'payload'
            elif item['format'] == '7z':
                # py7zr is installed into the project venv. It handles archive parsing;
                # validate every name/link before extraction into private staging.
                python = self.root / 'tools/distribution/.venv/bin/python'
                script = ('import py7zr,sys,pathlib; a=py7zr.SevenZipFile(sys.argv[1]); '
                          'infos=a.list(); '
                          'assert all(not pathlib.PurePosixPath(i.filename).is_absolute() and ".." not in pathlib.PurePosixPath(i.filename).parts and not getattr(i,"is_symlink",False) for i in infos); '
                          'assert sum(i.uncompressed or 0 for i in infos)<2147483648; '
                          'a.extractall(sys.argv[2]); a.close()')
                self.run([str(python), '-c', script, str(source), str(stage)], timeout=300)
            else:
                self.extract(source, stage / 'payload', item.get('member_map'))
                stage = stage / 'payload'
            for p in stage.rglob('*'):
                if p.is_symlink():
                    raise ValueError('Link em archive recusado')
            if item.get('member_map'):
                members = item['member_map']
            else:
                matches = list(stage.rglob(item['entry']))
                if len(matches) != 1:
                    raise ValueError('Entrada de archive ausente/ambígua')
                base = matches[0].parent
                members = {p.relative_to(stage).as_posix(): p.relative_to(base).as_posix()
                           for p in base.rglob('*') if p.is_file()}
            # Check every destination before publishing any file; resume preserves
            # files already published with the same verified source bytes.
            for member, target in members.items():
                original = w.safe_path(stage, member)
                destination = w.safe_path(directory, target)
                if not original.is_file() or (destination.exists() and w.digest(destination) != w.digest(original)):
                    raise ValueError('Archive conflita com arquivo existente: ' + target)
            preexisting = all(w.safe_path(directory, target).is_file() for target in members.values())
            for member, target in members.items():
                self.publish(w.safe_path(stage, member), (Path(item['target_dir']) / target).as_posix(),
                             executable=Path(target).suffix not in {'.txt', '.md', '.json', '.png'})
            if not entry.is_file():
                raise ValueError('Entrada não confirmada após publicação')
            if item.get('entry_kind') == 'elf':
                self.check_elf(entry)
            self.write_receipt(name, item, 'archive',
                               [(Path(item['target_dir']) / target).as_posix() for target in members.values()])
            return {'status': 'verified' if preexisting else 'installed', 'adopted_existing': preexisting,
                    'entry': str(entry.relative_to(self.root)), 'sha256': w.digest(entry)}
        finally:
            # Only the generated temporary tree belonging to this invocation.
            ancestor = stage if stage.name.startswith('.unpack-') else stage.parent
            shutil.rmtree(ancestor)

    def action(self, name, item):
        kind = item['kind']
        if kind == 'packages':
            return self.native_packages(item)
        if kind == 'flatpak':
            return self.flatpak(item)
        if kind == 'runtime':
            return self.runtime(item)
        if kind == 'build':
            return self.build(name, item)
        if kind == 'archive':
            return self.archive(name, item)
        if kind == 'scaffold':
            if (self.root / w.MARKER).is_file():
                marker = w.read(self.root / w.MARKER)
                if marker.get('product') != w.read(w.HERE / 'config/product.json')['slug']:
                    raise ValueError('Marcador de outro produto; instalação preservada')
                return {'status': 'reused', 'initialized_workspace': True}
            if not (self.root / w.MARKER).exists() and any((self.root / 'roms').glob('*')):
                if not (self.root / 'frontend/RetroFE/Config/profiles/host-linux.json').is_file():
                    raise ValueError('Biblioteca existente sem perfil reconhecido')
                return {'status': 'reused', 'existing_library_preserved': True}
            result = w.initialize(self.root, [], True)
            return {'status': 'configured', 'result': result['status']}
        if kind == 'frontend':
            result = w.fetch(self.root, item['component_id'], True)
            return {'status': 'verified', 'result': result['status']}
        if kind == 'quiver':
            self.run([sys.executable, str(self.root / 'frontend/QuiverLauncher/structure-frontend.py'), '--ensure-installation'], timeout=600)
            return {'status': 'configured'}
        if kind == 'python':
            venv = w.safe_path(self.root, 'tools/distribution/.venv')
            if not (venv / 'bin/python').is_file():
                self.run([sys.executable, '-m', 'venv', str(venv)])
            wheelhouse = self.prepare_wheelhouse(name, item, venv / 'bin/python')
            manifest = verify_wheelhouse(wheelhouse, item)
            self.run([str(venv / 'bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check',
                      '--no-index', '--find-links', str(wheelhouse), '--only-binary=:all:', '--require-hashes',
                      '--report', str(self.state / 'python-install-report.json'),
                      '-r', str(wheelhouse / 'requirements.txt')], timeout=600)
            self.run([str(venv / 'bin/python'), '-m', 'pip', 'check'], timeout=60)
            return {'status': 'installed', 'python': str(venv / 'bin/python'),
                    'wheelhouse': str(wheelhouse.relative_to(self.root)),
                    'wheelhouse_sha256': w.digest(wheelhouse / 'manifest.json'),
                    'locked_packages': len(manifest['files'])}
        if kind == 'cargo':
            target = w.safe_path(self.root, item['target'])
            if target.is_file():
                self.check_elf(target)
                return {'status': 'reused', 'sha256': w.digest(target)}
            prefix = w.safe_path(self.work, 'cargo/' + name)
            self.run(['cargo', 'install', '--locked', '--jobs', str(self.jobs), '--version', item['version'], '--root', str(prefix), item['crate']], timeout=1800, bounded=True)
            self.publish(prefix / 'bin' / item['crate'], item['target'])
            return {'status': 'installed', 'sha256': w.digest(target)}
        if kind == 'configuration':
            from install_config import configure
            result = configure(self)
            # Auxiliary upstream assets (for example xemu's empty disk image)
            # are separate from BIOS/firmware and never replace existing userdata.
            sys.path.insert(0, str(self.root / 'tools/roms'))
            import runtime_install as runtime
            import romtool
            if romtool.PROJECT_ROOT != self.root:
                raise ValueError('Configuração deve usar as ferramentas da própria instalação')
            result['auxiliary_assets'] = {}
            for asset_id, asset in runtime.load_recipes(self.root).get('assets', {}).items():
                destination = w.safe_path(self.root, asset['destination'])
                if destination.exists():
                    result['auxiliary_assets'][asset_id] = {'status': 'reused', 'customer_userdata_preserved': True}
                else:
                    result['auxiliary_assets'][asset_id] = runtime.install_asset(self.root, asset_id, True)
            return result
        if kind == 'shortcuts':
            self.run(['bash', str(self.root / 'frontend/RetroFE/Tools/launchers/retrofe/launch-retrofe.sh'), '--install-shortcuts'], timeout=60)
            self.run(['bash', str(self.root / 'frontend/ES-DE/launch.sh'), '--install-shortcuts'], timeout=60)
            self.run([sys.executable, str(self.root / 'frontend/QuiverLauncher/structure-frontend.py'), '--prepare-shortcuts'], timeout=60)
            return {'status': 'configured', 'desktop_and_application_menu': True, 'gui_started': False}
        if kind == 'experimental':
            return {'status': 'blocked', 'reason': item['reason']}
        raise ValueError('Tipo de componente não suportado: ' + kind)

    def install(self, selected=None):
        order = graph(self.config, selected)
        self.state.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        fd = os.open(w.safe_path(self.state, 'install.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            run_dir = self.state / run_id
            run_dir.mkdir()
            results = {}
            atomic_json(run_dir / 'plan.json', self.plan(selected))
            for name in order:
                item = self.config['components'][name]
                parents = item.get('requires', [])
                print(f'[SETUP] {name}', flush=True)
                if any(results[p]['status'] not in GOOD for p in parents):
                    result = {'status': 'blocked', 'reason': 'Dependência não concluída', 'requires': parents}
                else:
                    try:
                        with (run_dir / (name + '.log')).open('a') as self.log:
                            result = self.action(name, item)
                    except KeyboardInterrupt:
                        results[name] = {'status': 'interrupted', 'reason': 'Retomar repetindo o comando; estado real será verificado'}
                        summary = {'run_id': run_id, 'status': 'interrupted', 'results': results,
                                   'all_components_installed': False, 'remaining': order[order.index(name) + 1:]}
                        atomic_json(run_dir / 'completion.json', summary)
                        atomic_json(self.state / 'latest.json', summary)
                        return summary
                    except (Exception,) as exc:
                        result = {'status': 'failed', 'reason': str(exc)}
                results[name] = result
                atomic_json(run_dir / 'results.json', results)
                atomic_json(self.state / 'latest.json', {'run_id': run_id, 'results': results, 'status': 'in_progress'})
            complete = all(v['status'] in GOOD for v in results.values())
            summary = {'run_id': run_id, 'status': 'completed' if complete else 'incomplete',
                       'all_components_installed': complete and set(order) == set(self.config['components']),
                       'selected_scope_complete': complete, 'selected_components': order,
                       'registered_component_count': len(self.config['components']),
                       'results': results, 'gameplay_accepted': False}
            atomic_json(run_dir / 'completion.json', summary)
            atomic_json(self.state / 'latest.json', summary)
            return summary
        finally:
            os.close(fd)


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['plan', 'install', 'status'])
    p.add_argument('--root', type=Path, default=w.SOURCE_ROOT)
    p.add_argument('--components', nargs='+')
    p.add_argument('--jobs', type=int, default=1, choices=[1, 2])
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    try:
        engine = Installer(args.root, jobs=args.jobs)
        if args.command == 'status':
            result = w.read(engine.state / 'latest.json') if (engine.state / 'latest.json').is_file() else {'status': 'not_started'}
        elif args.command == 'install' and args.apply:
            if args.root.resolve() != w.SOURCE_ROOT.resolve():
                raise ValueError('Extraia a release no destino e execute o install.sh daquela pasta')
            result = engine.install(args.components)
        else:
            result = engine.plan(args.components)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get('status') == 'interrupted':
            return 130
        return 2 if result.get('status') == 'incomplete' else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
