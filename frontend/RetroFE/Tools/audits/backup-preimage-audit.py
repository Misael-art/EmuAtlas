#!/usr/bin/env python3
"""Cross a historical RetroFE tree backup with preimages recorded in manifests.

Read-only. For each XML/.conf of the backup index, it answers whether the backup
copy is the "before" state registered by a manifest and whether that manifest
already embeds the preimage (so rollback does not depend on the backup tree).
"""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "Config/platform-catalog.json").is_file() and (candidate / "Manifests").is_dir():
            return candidate
    raise RuntimeError("Não encontrei a raiz do RetroFE acima do auditor.")


ROOT = find_project_root()
DEFAULT_BACKUP = ROOT.parent / "RetroFE.pre-canonical-20261024"
DEFAULT_EXTENSIONS = (".xml", ".conf")
BEFORE_KEYS = ("before_sha256", "sha256_before", "expected_before_sha256", "captured_before_sha256")
AFTER_KEYS = ("after_sha256", "sha256_after")
PATH_KEYS = ("path", "relative_path", "xml_path", "file")
FIELDS = [
    "path", "size", "backup_sha256", "status", "current_sha256", "current_matches_backup",
    "hash_in_current_tree", "manifests", "embedded_preimage_verified",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize(value: str) -> str:
    value = value.replace("\\", "/").lstrip("./")
    for prefix in ("frontend/RetroFE/", "RetroFE/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value


def embedded_sha(record: dict) -> str | None:
    for key, value in record.items():
        if key.startswith("before") and key.endswith("gzip_base64") and isinstance(value, str):
            try:
                return hashlib.sha256(gzip.decompress(base64.b64decode(value))).hexdigest()
            except (ValueError, OSError):
                return "invalid"
    return None


def collect_records(manifest_dirs: list[Path]) -> tuple[dict, int]:
    """Map relative path -> list of (manifest, before_sha, embedded_sha)."""
    records: dict[str, list[tuple[str, str, str | None]]] = {}
    scanned = 0

    def walk(node, manifest: str) -> None:
        if isinstance(node, dict):
            before = next((node[k] for k in BEFORE_KEYS if isinstance(node.get(k), str)), None)
            path = next((node[k] for k in PATH_KEYS if isinstance(node.get(k), str)), None)
            if before and path:
                records.setdefault(normalize(path), []).append((manifest, before, embedded_sha(node)))
            for value in node.values():
                walk(value, manifest)
        elif isinstance(node, list):
            for value in node:
                walk(value, manifest)

    for directory in manifest_dirs:
        for manifest in sorted(directory.rglob("*.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            scanned += 1
            walk(data, str(manifest.relative_to(ROOT.parent.parent)) if manifest.is_relative_to(ROOT.parent.parent) else str(manifest))
    return records, scanned


def current_hashes(extensions: tuple[str, ...]) -> set[str]:
    hashes = set()
    for path in ROOT.rglob("*"):
        if path.suffix.lower() in extensions and path.is_file() and not path.is_symlink():
            hashes.add(sha256_file(path))
    return hashes


def classify(row: dict, records: dict, current_set: set[str]) -> dict:
    rel, digest = row["path"], row["sha256"]
    current = ROOT / rel
    current_sha = sha256_file(current) if current.is_file() else ""
    entries = records.get(normalize(rel), [])
    matching = [entry for entry in entries if entry[1] == digest]
    embedded_ok = any(entry[2] == digest for entry in matching)
    if current_sha == digest:
        status = "IGUAL_AO_ATUAL"
    elif matching and embedded_ok:
        status = "PREIMAGEM_REGISTRADA_E_EMBUTIDA"
    elif matching:
        status = "PREIMAGEM_REGISTRADA_SO_NO_BACKUP"
    elif entries:
        status = "CAMINHO_REGISTRADO_HASH_DIFERENTE"
    elif digest in current_set:
        status = "CONTEUDO_EXISTE_EM_OUTRO_CAMINHO"
    else:
        status = "SEM_REGISTRO_EM_MANIFESTO"
    return {
        "path": rel,
        "size": row["size"],
        "backup_sha256": digest,
        "status": status,
        "current_sha256": current_sha,
        "current_matches_backup": str(current_sha == digest).lower(),
        "hash_in_current_tree": str(digest in current_set).lower(),
        "manifests": "|".join(sorted({entry[0] for entry in (matching or entries)})),
        "embedded_preimage_verified": str(embedded_ok).lower(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP, help="Árvore de backup histórica.")
    parser.add_argument("--index", type=Path, help="CSV path,size,sha256 do backup (padrão: <backup>.sha256.csv).")
    parser.add_argument("--manifest-dir", type=Path, action="append",
                        help="Diretório de manifestos (repetível; padrão: Manifests/ do RetroFE).")
    parser.add_argument("--extension", action="append", help="Extensão auditada (repetível; padrão .xml e .conf).")
    parser.add_argument("--output", type=Path, help="CSV de saída; sem ele, só imprime o resumo.")
    parser.add_argument("--no-verify-backup", action="store_true", help="Não recalcula o SHA-256 do arquivo no backup.")
    args = parser.parse_args()

    index = args.index or args.backup.with_name(args.backup.name + ".sha256.csv")
    extensions = tuple(e.lower() if e.startswith(".") else "." + e.lower() for e in (args.extension or DEFAULT_EXTENSIONS))
    manifest_dirs = args.manifest_dir or [ROOT / "Manifests"]
    if args.output and args.output.resolve().is_relative_to(args.backup.resolve()):
        parser.error("--output não pode ficar dentro do backup.")

    with index.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if Path(row["path"]).suffix.lower() in extensions]

    integrity_errors = []
    if not args.no_verify_backup:
        for row in rows:
            source = args.backup / row["path"]
            if not source.is_file() or sha256_file(source) != row["sha256"]:
                integrity_errors.append(row["path"])

    records, scanned = collect_records(manifest_dirs)
    current_set = current_hashes(extensions)
    results = [classify(row, records, current_set) for row in rows]

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(results)

    summary = {
        "backup": str(args.backup),
        "index": str(index),
        "extensions": list(extensions),
        "manifests_scanned": scanned,
        "paths_with_recorded_preimage": len(records),
        "files_audited": len(results),
        "backup_integrity_errors": len(integrity_errors),
        "status": dict(Counter(r["status"] for r in results).most_common()),
        "output": str(args.output) if args.output else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if integrity_errors else 0


if __name__ == "__main__":
    sys.exit(main())
