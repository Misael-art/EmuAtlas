#!/usr/bin/env python3
"""Export an explicit, reviewed source selection; never copy an installed library."""
from __future__ import annotations

import argparse
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

sys.dont_write_bytecode = True
from workspace import HERE, SOURCE_ROOT, read, root_path, safe_path, digest

FORBIDDEN = {"roms", "bios", "saves", "media", "UserData", "emulator", "quarantine", "outputs", "work", "runtimes", "vendor", "skraper", "Assets", "Manifests", "__pycache__", ".git"}
# Documentation SVGs are reviewed text sources, included only by the explicit allowlist.
EXTENSIONS = {".py", ".sh", ".json", ".md", ".js", ".html", ".yml", ".txt", ".svg"}
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
    args = parser.parse_args()
    try:
        result = verify(args.root) if args.command == "verify" else build(args.output, args.command == "build" and args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
