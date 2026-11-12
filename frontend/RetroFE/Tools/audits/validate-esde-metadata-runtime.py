#!/usr/bin/env python3
"""Check HyperList import in one bounded, isolated RetroFE process tree.

Uses the frontend session lease and media lock. The real tree is read-only;
RetroFE writes into a disposable overlay, with private /dev and a PID namespace.
No Steam/Input session is changed and no game launcher is allowed to run.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(PROJECT / 'tools/roms'))
import romtool
import media_session


def stop_owned(process, grace=3):
    """Escalate only within the new process group created by this audit."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        # Reap group descendants even when its first process already exited.
        if sig == signal.SIGTERM:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return
    raise RuntimeError('A árvore privada do teste não confirmou encerramento')


def bounded_run(command, timeout, **kwargs):
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        return process.wait(timeout=timeout)
    finally:
        stop_owned(process)


def metadata_rows():
    with sqlite3.connect('file:' + str(ROOT / 'meta.db') + '?mode=ro', uri=True) as db:
        return {
            'total_rows': db.execute('select count(*) from Meta').fetchone()[0],
            'master_system_rows': db.execute("select count(*) from Meta where collectionName='Sega Master System'").fetchone()[0],
            'sample': db.execute("select collectionName,name,year,developer,genre,score,players from Meta where name='Fantasy Zone (World) (Beta)'").fetchall(),
        }


def worker(stage, timeout):
    mounts = Path('/proc/self/mountinfo').read_text().splitlines()
    if not any(line.split()[4] == str(ROOT) and ' - overlay ' in line for line in mounts):
        raise RuntimeError('Worker exige overlay privado da instalação; execução direta recusada')
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (int(timeout) + 10, int(timeout) + 15))
    stage = Path(stage)
    before = metadata_rows()
    env = {**os.environ, 'SDL_AUDIODRIVER': 'dummy', 'SDL_VIDEODRIVER': 'x11',
           'LIBGL_ALWAYS_SOFTWARE': '1', 'OMP_NUM_THREADS': '1', 'LP_NUM_THREADS': '1',
           'LD_LIBRARY_PATH': str(ROOT / 'core/linux/lib')}
    log = ROOT / 'log.txt'
    log.write_bytes(b'')
    process = None
    ready = False
    cleanup = 'not_started'
    try:
        with (stage / 'engine-output.txt').open('wb') as output:
            process = subprocess.Popen([str(ROOT / 'RetroFE')], cwd=ROOT, env=env,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and process.poll() is None:
                text = log.read_text(errors='replace')
                # The splash emits the first Layout Initialized before metadata import.
                if text.count('Layout] Initialized') >= 2:
                    ready = True
                    break
                time.sleep(.25)
            if not ready:
                raise RuntimeError('Importação/layout principal não ficaram prontos dentro do limite; não repetir automaticamente')
            after = metadata_rows()
            result = subprocess.run(['xdotool', 'search', '--pid', str(process.pid), '--class', 'RetroFE'],
                                    capture_output=True, text=True, timeout=3)
            if result.returncode == 0 and result.stdout.splitlines():
                window = result.stdout.splitlines()[0]
                subprocess.run(['xdotool', 'windowfocus', window, 'key', '--window', window, 'q'],
                               capture_output=True, timeout=3)
            try:
                process.wait(timeout=5)
                cleanup = 'normal_quit'
            except subprocess.TimeoutExpired:
                cleanup = 'owned_test_group_escalation'
            text = log.read_text(errors='replace')
            report = {'ready': ready, 'before': before, 'after': after,
                      'hyperlists_imported': text.count('Importing hyperlist'),
                      'engine_errors': [line for line in text.splitlines() if '[ERROR]' in line][:20]}
            (stage / 'engine-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if process is not None:
            stop_owned(process)
        (stage / 'cleanup.json').write_text(json.dumps({'cleanup': cleanup, 'namespace_children_removed_on_exit': True}))


def run(timeout, destination):
    for command in ('bwrap', 'xvfb-run', 'Xvfb', 'xdotool'):
        if not shutil.which(command):
            raise RuntimeError(f'Dependência de teste ausente: {command}')
    spec = importlib.util.spec_from_file_location('media_paths', PROJECT / 'tools/roms/materialize-collection-media-paths.py')
    paths = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(paths)
    original = romtool.sha256_file(ROOT / 'meta.db')
    report = {'status': 'failed', 'scope': 'isolated metadata import; no gameplay, physical controls or FPS certification'}
    try:
        with paths.frontend_instance_lock(), media_session.write_lock(vars(romtool)):
            if any(romtool.frontend_processes_running().values()):
                raise RuntimeError('Feche os frontends normalmente antes do teste')
            scratch = PROJECT / 'work/roms-consolidation'
            scratch.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='retrofe-metadata-test-', dir=scratch) as temporary:
                stage = Path(temporary)
                guard = stage / 'blocked-game-launcher'
                guard.write_text('#!/bin/sh\nexit 99\n')
                guard.chmod(0o555)
                command = ['xvfb-run', '-a', '-s', '-screen 0 1400x900x24 -nolisten tcp',
                           'bwrap', '--unshare-pid', '--die-with-parent', '--ro-bind', '/', '/',
                           '--dev', '/dev', '--proc', '/proc', '--overlay-src', str(ROOT), '--tmp-overlay', str(ROOT),
                           '--bind', str(stage), str(stage), '--ro-bind', str(guard),
                           str(ROOT / 'Tools/launchers/emulators/host-emulator-launcher.py'),
                           sys.executable, str(Path(__file__).resolve()), '--worker', str(stage), '--timeout', str(timeout)]
                with (stage / 'runner.log').open('wb') as output:
                    code = bounded_run(command, timeout + 25, stdout=output, stderr=subprocess.STDOUT)
                report['runner_returncode'] = code
                for name in ('engine-result', 'cleanup'):
                    file = stage / (name + '.json')
                    if file.is_file():
                        report[name] = json.loads(file.read_text())
                if code or not report.get('engine-result', {}).get('ready'):
                    raise RuntimeError('Teste não concluiu: ' + (stage / 'runner.log').read_text(errors='replace')[-2000:])
                report['status'] = 'passed_with_warnings' if report['engine-result'].get('engine_errors') else 'passed'
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        report['canonical_database_unchanged'] = romtool.sha256_file(ROOT / 'meta.db') == original
        report['remaining_frontends'] = romtool.frontend_processes_running()
        if not report['canonical_database_unchanged'] or any(report['remaining_frontends'].values()):
            report['status'] = 'failed'
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('x') as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 300:
        parser.error('--timeout deve ficar entre 10 e 300 segundos')
    if args.worker:
        worker(args.worker, args.timeout)
    else:
        output = ROOT / 'Docs/TestResults' / ('esde-metadata-runtime-' + datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.json')
        result = run(args.timeout, output)
        print(json.dumps({'status': result['status'], 'report': str(output), 'error': result.get('error')}, ensure_ascii=False))
        raise SystemExit(0 if result['status'] in {'passed', 'passed_with_warnings'} else 1)
