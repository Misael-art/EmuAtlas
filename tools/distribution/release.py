#!/usr/bin/env python3
"""Export an explicit, reviewed source selection; never copy an installed library."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
import uuid

sys.dont_write_bytecode = True
from workspace import HERE, SOURCE_ROOT, read, root_path, safe_path, digest

sys.path.insert(0, str(SOURCE_ROOT / 'tools/roms'))
import romtool
import workspace as w

FORBIDDEN = {"roms", "bios", "saves", "media", "UserData", "emulator", "quarantine", "outputs", "work", "runtimes", "vendor", "skraper", "Assets", "Manifests", "__pycache__", ".git"}
# Documentation SVGs are reviewed text sources, included only by the explicit allowlist.
EXTENSIONS = {".py", ".sh", ".json", ".md", ".js", ".html", ".css", ".yml", ".txt", ".svg"}
CODE_EXCEPTIONS = {"tools/roms/skraper/launch.sh", "emulator/linux/eden-nx2/eden-nx2-launch", "emulator/launch.sh", "emulator/launch.cmd", "emulator/README.md"}


def inspect_source(root, entry):
    source, target = entry["source"], entry["target"]
    # New-install templates may describe protected paths but contain no payloads.
    is_template = source.startswith("tools/distribution/templates/")
    source_parts = Path(source).parts
    forbidden_source = FORBIDDEN - {"roms"}
    if not is_template and source not in CODE_EXCEPTIONS and (source_parts[0] in {"roms", "media", "bios", "saves", "outputs", "work"} or any(part in forbidden_source for part in source_parts)):
        raise ValueError(f"Fonte protegida: {source}")
    if any(part in {"UserData", "emulator", "runtimes", "vendor", "Assets", "Manifests", "roms", "bios", "saves", "media", "outputs", "work", ".git"} for part in Path(target).parts) and not target.startswith(("tools/roms/", "tools/distribution/templates/")) and target not in CODE_EXCEPTIONS:
        raise ValueError(f"Destino não distribuível: {target}")
    p = safe_path(root, source)
    if not p.is_file() or p.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"Fonte ausente/grande: {source}")
    if p.suffix not in EXTENSIONS and p.name not in {".gitignore", "LICENSE"} and source not in CODE_EXCEPTIONS:
        raise ValueError(f"Tipo não distribuível: {source}")
    data = p.read_bytes()
    text = data.decode("utf-8")
    if re.search(r"/(?:home|Users)/[A-Za-z0-9_.-]+/", text):
        raise ValueError(f"Caminho pessoal no pacote: {source}")
    if re.search(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", text):
        raise ValueError(f"Material privado: {source}")
    return data


def selection(root=SOURCE_ROOT):
    entries = read(HERE / "config/release-files.json")["files"]
    targets = [e["target"] for e in entries]
    if len(set(targets)) != len(targets):
        raise ValueError("Destinos duplicados na allowlist")
    result = []
    for entry in sorted(entries, key=lambda i: i["target"]):
        data = inspect_source(root, entry)
        safe_path(root / "unused-target-validation", entry["target"])
        result.append({**entry, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                       "mode": entry.get("mode", "0755" if entry["target"].endswith(".sh") else "0644")})
    return result


def build(output, apply=False):
    product = read(HERE / "config/product.json")
    name = product["slug"] + "-" + product["version"]
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
        raise ValueError("Identidade de release inválida")
    output = root_path(output)
    target = safe_path(output, name)
    archive = safe_path(output, name + ".tar.gz")
    entries = selection()
    if not apply:
        return {"status": "planned", "file_count": len(entries), "bytes": sum(i["size_bytes"] for i in entries), "destination": str(target), "commercial_release_approved": False}
    if target.exists() or archive.exists():
        raise ValueError("Release existente preservada; escolha outro output ou versão")
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".release-", dir=output))
    temporary_archive = output / ("." + name + f"-{os.getpid()}.tar.gz")
    try:
        for entry in entries:
            data = inspect_source(SOURCE_ROOT, entry)
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("Fonte mudou durante a preparação")
            p = safe_path(stage, entry["target"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            p.chmod(int(entry["mode"], 8))
        manifest = {"schema_version": 1, "product": product["slug"], "version": product["version"],
                    "stage": product["stage"], "commercial_release_approved": False,
                    "files": [{k: v for k, v in e.items() if k != "source"} for e in entries]}
        (stage / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        verify(stage)
        with temporary_archive.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
            for p in sorted(stage.rglob("*")):
                if not p.is_file():
                    continue
                info = tarfile.TarInfo(name + "/" + p.relative_to(stage).as_posix())
                info.size = p.stat().st_size
                info.mode = p.stat().st_mode & 0o777
                info.mtime = 0
                tar.addfile(info, io.BytesIO(p.read_bytes()))
        # mkdir reserves the destination; no existing tree can be overwritten.
        target.mkdir()
        for p in stage.iterdir():
            os.rename(p, target / p.name)
        os.link(temporary_archive, archive)
        return {"status": "built_verified", "directory": str(target), "archive": str(archive),
                "sha256": digest(archive), "bytes": archive.stat().st_size, "file_count": len(entries),
                "commercial_release_approved": False}
    finally:
        shutil.rmtree(stage)
        temporary_archive.unlink(missing_ok=True)


def verify(root):
    root = root_path(root)
    manifest = read(root / "release-manifest.json")
    expected = {item["target"] for item in manifest["files"]} | {"release-manifest.json"}
    actual = set()
    for p in root.rglob("*"):
        relative = p.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if p.is_symlink():
            raise ValueError(f"Symlink no pacote: {relative}")
        if p.is_file():
            actual.add(relative.as_posix())
    if expected != actual:
        raise ValueError(f"Arquivos fora do manifesto: {sorted(actual - expected)[:8]}; ausentes: {sorted(expected - actual)[:8]}")
    for entry in manifest["files"]:
        p = safe_path(root, entry["target"])
        if digest(p) != entry["sha256"] or p.stat().st_size != entry["size_bytes"] or p.stat().st_mode & 0o777 != int(entry["mode"], 8):
            raise ValueError(f"Arquivo divergente: {entry['target']}")
    return {"status": "verified", "files": len(manifest["files"]), "unexpected_files": 0}


def source_state(root, relative):
    path = safe_path(root, relative)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('Destino de fonte deve ser arquivo regular: ' + relative)
    return {'sha256': digest(path), 'size_bytes': path.stat().st_size,
            'mode': format(path.stat().st_mode & 0o777, '04o')}


def source_entries(root):
    manifest = read(safe_path(root, 'release-manifest.json'))
    entries = {}
    for row in manifest['files']:
        relative = row['target']
        if relative == 'release-manifest.json' or relative in entries:
            raise ValueError('Destino repetido/reservado no manifesto de fontes')
        # Apply the same reviewed source boundaries to incoming updates.
        inspect_source(root, {'source': relative, 'target': relative})
        entries[relative] = {key: row[key] for key in ['sha256', 'size_bytes', 'mode']}
    return manifest, entries


def plan_update(root, incoming, prepare=False):
    root, incoming = root_path(root), root_path(incoming)
    if root == incoming:
        raise ValueError('Selecione a release de entrada em uma pasta própria')
    verify(incoming)
    previous, old = source_entries(root)
    following, new = source_entries(incoming)
    if previous.get('product') != following.get('product'):
        raise ValueError('Atualização de outro produto')
    old['release-manifest.json'] = source_state(root, 'release-manifest.json')
    new['release-manifest.json'] = source_state(incoming, 'release-manifest.json')
    changes = []
    # Inspect the entire owned selection before preparing or changing anything.
    for relative in sorted(old.keys() | new.keys()):
        current = source_state(root, relative)
        if current != old.get(relative):
            raise ValueError('Fonte personalizada/existente preservada: ' + relative)
        if old.get(relative) != new.get(relative):
            changes.append({'path': relative, 'before': old.get(relative),
                            'after': new.get(relative), 'status': 'prepared'})
    result = {'kind': 'source_release_update', 'status': 'prepared' if prepare else 'planned',
              'product': following['product'], 'from_version': previous['version'],
              'to_version': following['version'], 'changes': changes,
              'library_payloads_modified': False, 'gameplay_tested': False}
    if not prepare:
        return result
    ident = 'source-update-' + uuid.uuid4().hex
    op = safe_path(root, 'outputs/installation/updates/' + ident)
    stage = safe_path(root, 'work/installation/updates/' + ident + '/staging')
    with w.lock(root):
        op.mkdir(parents=True)
        for change in changes:
            relative = change['path']
            # Recheck after obtaining the lock, including existing new targets.
            if source_state(root, relative) != change['before']:
                raise ValueError('Fonte mudou durante a preparação: ' + relative)
            if change['before']:
                snapshot = safe_path(op / 'rollback/files', relative)
                romtool.atomic_write_bytes(snapshot, safe_path(root, relative).read_bytes(),
                                            mode=int(change['before']['mode'], 8))
            if change['after']:
                source = safe_path(incoming, relative)
                if source_state(incoming, relative) != change['after']:
                    raise ValueError('Release mudou durante a preparação: ' + relative)
                romtool.atomic_write_bytes(safe_path(stage, relative), source.read_bytes(),
                                            mode=int(change['after']['mode'], 8))
        result.update(operation_id=ident, stage=stage.relative_to(root).as_posix(),
                      created_at=romtool.now())
        romtool.atomic_json(op / 'manifest.json', result)
    return result


def execute_update(root, ident, action, confirmed=False):
    if not re.fullmatch(r'source-update-[0-9a-f]{32}', ident):
        raise ValueError('ID de atualização inválido')
    if action != 'verify' and not confirmed:
        raise ValueError('Confirme a aplicação/reversão explicitamente')
    root = root_path(root)
    op = safe_path(root, 'outputs/installation/updates/' + ident)
    manifest_path = safe_path(op, 'manifest.json')
    with ExitStack() as stack:
        stack.enter_context(w.lock(root))
        install_lock = safe_path(root, 'outputs/installation/install.lock')
        fd = os.open(install_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        stack.callback(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = read(manifest_path)
        if manifest.get('kind') != 'source_release_update' or manifest.get('operation_id') != ident:
            raise ValueError('Manifesto de outra operação')
        allowed = {'apply': {'prepared', 'applying', 'applied'},
                   'verify': {'applied', 'completed_verified'},
                   'rollback': {'applying', 'applied', 'completed_verified', 'rolling_back'}}
        if action not in allowed or manifest['status'] not in allowed[action]:
            raise ValueError('Estado incompatível com a ação de atualização')
        if action != 'verify':
            from runtime_install import _library_guard
            _library_guard(root)
        changes = manifest['changes']
        stage = safe_path(root, 'work/installation/updates/' + ident + '/staging')
        rollback_files = safe_path(root, 'outputs/installation/updates/' + ident + '/rollback/files')
        removed_files = safe_path(root, 'outputs/installation/updates/' + ident + '/rollback/removed')
        applied_files = safe_path(root, 'outputs/installation/updates/' + ident + '/rollback/applied-files')
        # Verify all preimages, proposals and current states before any mutation.
        for change in changes:
            relative = change['path']
            for image, folder in [('before', rollback_files), ('after', stage)]:
                if change[image] is not None and source_state(folder, relative) != change[image]:
                    raise ValueError('Snapshot alterado: ' + relative)
                if change[image] is not None:
                    inspect_source(folder, {'source': relative, 'target': relative})
            current = source_state(root, relative)
            acceptable = [change['after']] if action == 'verify' else [change['before'], change['after']]
            if current not in acceptable:
                raise ValueError('Alteração posterior preservada: ' + relative)
            if current is None and change['before'] and not change['after']:
                if (not change.get('intent') or
                        source_state(removed_files, relative) != change['before']):
                    raise ValueError('Fonte removida sem checkpoint comprovado: ' + relative)
            if action == 'rollback' and current is None and not change['before'] and change['after']:
                if (change.get('intent') == 'rollback' and
                        source_state(applied_files, relative) != change['after']):
                    raise ValueError('Nova fonte ausente sem preservação comprovada: ' + relative)
        if action == 'verify':
            for row in read(root / 'release-manifest.json')['files']:
                if source_state(root, row['target']) != {key: row[key] for key in ['sha256', 'size_bytes', 'mode']}:
                    raise ValueError('Fonte publicada diverge: ' + row['target'])
            manifest['status'] = 'completed_verified'
        else:
            manifest['status'] = 'applying' if action == 'apply' else 'rolling_back'
            romtool.atomic_json(manifest_path, manifest)
            sequence = changes if action == 'apply' else reversed(changes)
            for change in sequence:
                relative = change['path']
                desired = change['after'] if action == 'apply' else change['before']
                current = source_state(root, relative)
                if current == desired:
                    continue
                expected = change['before'] if action == 'apply' else change['after']
                if current != expected:
                    raise ValueError('Fonte mudou durante a publicação: ' + relative)
                change['intent'] = action
                romtool.atomic_json(manifest_path, manifest)
                path = safe_path(root, relative)
                if desired is None:
                    folder = removed_files if action == 'apply' else applied_files
                    preserved = safe_path(folder, relative)
                    if preserved.exists():
                        raise ValueError('Destino de preservação existente: ' + relative)
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    path.rename(preserved)
                    romtool.sync_directory_chain(preserved.parent)
                    romtool.sync_directory_chain(path.parent)
                else:
                    folder = stage if action == 'apply' else rollback_files
                    data = safe_path(folder, relative).read_bytes()
                    if hashlib.sha256(data).hexdigest() != desired['sha256']:
                        raise ValueError('Snapshot mudou durante a publicação: ' + relative)
                    romtool.atomic_write_bytes(path, data, mode=int(desired['mode'], 8))
                change['status'] = 'applied' if action == 'apply' else 'rolled_back'
                romtool.atomic_json(manifest_path, manifest)
            manifest['status'] = 'applied' if action == 'apply' else 'rolled_back'
        manifest['updated_at'] = romtool.now()
        romtool.atomic_json(manifest_path, manifest)
        return {'operation_id': ident, 'status': manifest['status'], 'changed_files': len(changes),
                'library_payloads_modified': False, 'gameplay_tested': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["plan", "build"]:
        p = sub.add_parser(name)
        p.add_argument("--output", type=Path, default=SOURCE_ROOT / "outputs/releases")
        if name == "build":
            p.add_argument("--apply", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("--root", type=Path, required=True)
    p = sub.add_parser('update-plan')
    p.add_argument('--root', type=Path, default=SOURCE_ROOT)
    p.add_argument('--incoming', type=Path, required=True)
    p.add_argument('--prepare', action='store_true')
    for name in ['update-apply', 'update-verify', 'update-rollback']:
        p = sub.add_parser(name)
        p.add_argument('--root', type=Path, default=SOURCE_ROOT)
        p.add_argument('--operation-id', required=True)
        p.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'update-plan':
            result = plan_update(args.root, args.incoming, args.prepare)
        elif args.command.startswith('update-'):
            result = execute_update(args.root, args.operation_id, args.command.removeprefix('update-'), args.apply)
        elif args.command == 'verify':
            result = verify(args.root)
        else:
            result = build(args.output, args.command == 'build' and args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
