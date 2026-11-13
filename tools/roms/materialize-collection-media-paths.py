#!/usr/bin/env python3
"""Audit and safely materialize RetroFE collection media paths and separators."""

from __future__ import annotations

import argparse
import base64
import csv
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import stat
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


def find_project_root() -> Path:
    """Resolve the project root from this tool's location, without a host path."""
    for candidate in Path(__file__).resolve().parents:
        retrofe = candidate / "frontend" / "RetroFE"
        if (
            (retrofe / "collections").is_dir()
            and (retrofe / "Config" / "media-map.json").is_file()
            and (candidate / "tools" / "roms" / "config" / "roots.json").is_file()
        ):
            return candidate
    raise RuntimeError(
        "Não encontrei a raiz oficial do projeto acima de tools/roms. "
        "Mantenha este utilitário dentro da árvore tools/roms do projeto."
    )


PROJECT_ROOT = find_project_root()
ROOT = PROJECT_ROOT / "frontend" / "RetroFE"
MEDIA_MAP_PATH = ROOT / "Config" / "media-map.json"
PLATFORM_CATALOG_PATH = ROOT / "Config" / "platform-catalog.json"
SYSTEMS_PATH = ROOT / "Config" / "systems.json"
AUDIT_JSON = ROOT / "Manifests" / "collection-media-path-audit.json"
AUDIT_CSV = ROOT / "Manifests" / "collection-media-path-audit.csv"
PLATFORM_MEDIA_CSV = ROOT / "Manifests" / "platform-media-coverage.csv"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_atomic(path: Path, data: Any) -> None:
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    write_bytes_atomic(path, payload)


def write_bytes_atomic(path: Path, payload: bytes, mode: int | None = None) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.roms import romtool
    romtool.atomic_write_bytes(path, payload, mode)


def relative_to_root(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def parse_properties(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^[ \t]*([^#=]+?)[ \t]*=[ \t]*(.*?)[ \t]*$", line)
        if match:
            values[match.group(1).strip().casefold()] = match.group(2).strip()
    return values


def config_system_records() -> tuple[dict[str, list[str]], dict[str, str], dict[str, str]]:
    catalog = load_json(PLATFORM_CATALOG_PATH)
    by_subdir: dict[str, list[str]] = defaultdict(list)
    display_to_id: dict[str, list[str]] = defaultdict(list)
    for record in catalog.get("systems", []):
        system_id = str(record.get("id", "")).strip()
        subdir = str(record.get("rom_subdir", "")).strip()
        display = str(record.get("display_name", "")).strip()
        if system_id and subdir:
            by_subdir[subdir.casefold()].append(system_id)
        if system_id and display:
            display_to_id[display.casefold()].append(system_id)

    systems_json = load_json(SYSTEMS_PATH)
    collection_to_subdir: dict[str, str] = {}
    for record in systems_json.get("systems", []):
        collection = str(record.get("collection", "")).strip()
        subdir = str(record.get("rom_subdir", "")).strip()
        if collection and subdir:
            collection_to_subdir[collection.casefold()] = subdir
        for child in record.get("subcollections", []):
            if collection and subdir:
                collection_to_subdir[str(child).strip().casefold()] = subdir

    profile = load_json(ROOT / "Config" / "profiles" / "host-linux.json")
    for collection, record in profile.get("collections", {}).items():
        subdir = str(record.get("rom_subdir", "")).strip()
        if collection and subdir:
            collection_to_subdir[str(collection).strip().casefold()] = subdir
        for child in record.get("subcollections", []):
            if subdir:
                collection_to_subdir[str(child).strip().casefold()] = subdir

    return by_subdir, collection_to_subdir, {
        key: values[0] for key, values in display_to_id.items() if len(set(values)) == 1
    }


def media_system_overrides(media_map: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for collection, record in media_map.get("collection_media_mapping", {}).get("collection_system_overrides", {}).items():
        if isinstance(record, dict) and record.get("system_id"):
            result[collection.casefold()] = {
                "system_id": str(record["system_id"]),
                "evidence": str(record.get("evidence", "explicit media-map override")),
            }
    return result


def collection_media_path_overrides(media_map: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return narrowly scoped, evidence-backed legacy media root corrections."""
    result: dict[str, dict[str, str]] = {}
    for collection, record in media_map.get("collection_media_mapping", {}).get("collection_media_path_overrides", {}).items():
        if not isinstance(record, dict) or not record.get("system_id") or not record.get("legacy_source_prefix"):
            continue
        result[collection.casefold()] = {
            "system_id": str(record["system_id"]).strip(),
            "legacy_source_prefix": str(record["legacy_source_prefix"]).replace("\\", "/").strip(),
            "evidence": str(record.get("evidence", "explicit media-map path override")),
        }
    return result


def system_for_collection(
    collection: str,
    list_path: str,
    by_subdir: dict[str, list[str]],
    collection_to_subdir: dict[str, str],
    display_to_id: dict[str, str],
    overrides: dict[str, dict[str, str]],
) -> tuple[str, str, str]:
    override = overrides.get(collection.casefold())
    if override:
        return override["system_id"], "media-map collection override", override["evidence"]

    normalized = list_path.replace("\\", "/").rstrip("/")
    tail = normalized.rsplit("/", 1)[-1].strip()
    if tail and "%" not in tail:
        candidates = sorted(set(by_subdir.get(tail.casefold(), [])))
        if len(candidates) == 1:
            return candidates[0], "settings.conf:list.path", f"list.path ends in configured rom_subdir {tail}"
        if len(candidates) > 1:
            return "", "ambiguous list.path", f"rom_subdir {tail} maps to {','.join(candidates)}"

    subdir = collection_to_subdir.get(collection.casefold())
    if subdir:
        candidates = sorted(set(by_subdir.get(subdir.casefold(), [])))
        if len(candidates) == 1:
            return candidates[0], "configured collection/subcollection", f"configured rom_subdir {subdir}"
        # Explicit systems.json/profile records are authoritative even when the
        # platform has no local games yet. The configured rom_subdir is the
        # stable ID used by the canonical media tree as well.
        if not candidates:
            return subdir, "configured collection/subcollection", f"explicit rom_subdir {subdir}; no platform-catalog ID or local ROM directory is required for media mapping"
        if len(candidates) > 1:
            return "", "ambiguous collection mapping", f"rom_subdir {subdir} maps to {','.join(candidates)}"

    display_id = display_to_id.get(collection.casefold())
    if display_id:
        return display_id, "platform-catalog display_name", "collection name exactly matches one configured display_name"

    return "", "unresolved", "no unique rom_subdir or explicit collection mapping"


def settings_media_lines(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(keepends=True), 1):
        match = re.match(r"^([ \t\ufeff]*media\.)([^=]+?)([ \t]*=[ \t]*)(.*?)(\r?\n)?$", line)
        if not match:
            continue
        key = match.group(2).strip()
        value = match.group(4).strip()
        rows.append({"line": index, "key": key, "value": value, "line_text": line, "match": match})
    return rows


def count_files(path: Path) -> tuple[int, int]:
    if not path.is_dir():
        return 0, 0
    total = 0
    broken = 0
    for entry in path.rglob("*"):
        if entry.is_symlink():
            if not entry.exists():
                broken += 1
            else:
                total += 1
        elif entry.is_file():
            total += 1
    return total, broken


def normalize_config_target(value: str, collection: str) -> Path | None:
    raw = value.replace("\\", "/")
    raw = raw.replace("%ITEM_COLLECTION_NAME%", collection)
    if any(marker in raw for marker in ("%BASE_ITEM_PATH%", "%ITEM_", "$")):
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


def has_nonportable_relative_separators(value: str) -> bool:
    """Find Windows separators in relative media paths without rewriting drives/UNC roots."""
    raw = value.strip()
    if "\\" not in raw or re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", raw):
        return False
    return True


def legacy_media_map(media_map: dict[str, Any]) -> tuple[str, dict[str, str]]:
    mapping = media_map.get("collection_media_mapping", {})
    return str(mapping.get("root_relative_to_retrofe", "../../media")).rstrip("/"), {
        str(k).casefold(): str(v) for k, v in mapping.get("legacy_key_to_media_type", {}).items()
    }


def scan_collection_settings(
    media_map: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    by_subdir, collection_to_subdir, display_to_id = config_system_records()
    overrides = media_system_overrides(media_map)
    path_overrides = collection_media_path_overrides(media_map)
    root_relative, category_map = legacy_media_map(media_map)
    audit_rows: list[dict[str, Any]] = []
    file_plans: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    total_legacy = 0

    for settings_path in sorted((ROOT / "collections").glob("*/settings.conf")):
        collection = settings_path.parent.name
        original_bytes = settings_path.read_bytes()
        text = original_bytes.decode("utf-8")
        properties = parse_properties(text)
        list_path = properties.get("list.path", "")
        system_id, mapping_source, evidence = system_for_collection(
            collection, list_path, by_subdir, collection_to_subdir, display_to_id, overrides
        )
        media_lines = settings_media_lines(text)
        replacements: list[dict[str, Any]] = []
        output_lines: list[str] = []
        line_to_replacement = {entry["line"]: entry for entry in media_lines}

        for line_no, line in enumerate(text.splitlines(keepends=True), 1):
            entry = line_to_replacement.get(line_no)
            if not entry:
                output_lines.append(line)
                continue
            key = entry["key"]
            value = entry["value"]
            media_type = category_map.get(key.casefold(), "")
            normalized_value = value.replace("\\", "/")
            path_override = path_overrides.get(collection.casefold())
            override_prefix = path_override.get("legacy_source_prefix", "") if path_override else ""
            override_canonical_prefix = (
                f"{root_relative}/{path_override['system_id']}/" if path_override else ""
            )
            use_path_override = bool(
                path_override and media_type and (
                    normalized_value.casefold().startswith(override_prefix.casefold())
                    or normalized_value.casefold().startswith(override_canonical_prefix.casefold())
                )
            )
            resolved_system_id = path_override["system_id"] if use_path_override else system_id
            resolved_mapping_source = "explicit media-map path override" if use_path_override else mapping_source
            resolved_evidence = path_override["evidence"] if use_path_override else evidence
            expected = f"{root_relative}/{resolved_system_id}/{media_type}" if resolved_system_id and media_type else ""
            is_launchbox = "launchbox" in value.casefold()

            target_path = normalize_config_target(value, collection)
            if target_path:
                target_exists = target_path.exists()
                file_count, broken_links = count_files(target_path)
            else:
                target_exists, file_count, broken_links = False, 0, 0

            if is_launchbox:
                total_legacy += 1
                status = "external_path_present" if target_exists else "external_path_missing"
                if not system_id or not media_type:
                    status = "unresolved_mapping" if not system_id else "unmapped_media_key"
                    unresolved.append({
                        "collection": collection,
                        "settings_path": relative_to_root(settings_path),
                        "line": entry["line"],
                        "media_key": key,
                        "current_value": value,
                        "system_id": system_id,
                        "mapping_source": mapping_source,
                        "mapping_evidence": evidence,
                        "reason": "No unique platform mapping or no configured media category.",
                    })
                    output_lines.append(line)
                else:
                    match = entry["match"]
                    replacement_line = f"{match.group(1)}{match.group(2)}{match.group(3)}{expected}{match.group(5) or ''}"
                    replacements.append({
                        "line": entry["line"],
                        "media_key": key,
                        "media_type": media_type,
                        "old_value": value,
                        "new_value": expected,
                        "mapping_source": mapping_source,
                        "mapping_evidence": evidence,
                        "target_file_count": count_files(PROJECT_ROOT / "media" / system_id / media_type)[0],
                    })
                    output_lines.append(replacement_line)
            else:
                line_output = line
                if expected and normalized_value.rstrip("/") == expected and not has_nonportable_relative_separators(value):
                    status = "canonical_media_path"
                    canonical_dir = PROJECT_ROOT / "media" / resolved_system_id / media_type
                    file_count, broken_links = count_files(canonical_dir)
                    target_exists = canonical_dir.is_dir()
                elif use_path_override and normalized_value.casefold().startswith(override_prefix.casefold()):
                    match = entry["match"]
                    replacement_line = f"{match.group(1)}{match.group(2)}{match.group(3)}{expected}{match.group(5) or ''}"
                    replacements.append({
                        "line": entry["line"],
                        "media_key": key,
                        "media_type": media_type,
                        "old_value": value,
                        "new_value": expected,
                        "mapping_source": resolved_mapping_source,
                        "mapping_evidence": resolved_evidence,
                        "target_file_count": count_files(PROJECT_ROOT / "media" / resolved_system_id / media_type)[0],
                    })
                    line_output = replacement_line
                    status = "canonical_path_repair_planned"
                    canonical_dir = PROJECT_ROOT / "media" / resolved_system_id / media_type
                    file_count, broken_links = count_files(canonical_dir)
                    target_exists = canonical_dir.is_dir()
                elif has_nonportable_relative_separators(value):
                    match = entry["match"]
                    replacement_line = f"{match.group(1)}{match.group(2)}{match.group(3)}{normalized_value}{match.group(5) or ''}"
                    replacements.append({
                        "line": entry["line"],
                        "media_key": key,
                        "media_type": media_type,
                        "old_value": value,
                        "new_value": normalized_value,
                        "mapping_source": mapping_source,
                        "mapping_evidence": evidence,
                        "target_file_count": file_count,
                        "path_change": "separator-only; preserves the relative target",
                    })
                    line_output = replacement_line
                    status = "relative_path_nonportable_separators"
                elif target_path and target_path.exists():
                    status = "existing_relative_path"
                elif target_path:
                    status = "configured_path_missing"
                else:
                    status = "dynamic_or_unresolved_path"
                output_lines.append(line_output)

            audit_rows.append({
                "collection": collection,
                "system_id": resolved_system_id,
                "mapping_source": resolved_mapping_source,
                "media_key": key,
                "media_type": media_type,
                "settings_path": relative_to_root(settings_path),
                "line": entry["line"],
                "current_value": value,
                "expected_value": expected,
                "status": status,
                "target_directory_exists": "yes" if target_exists else "no",
                "target_file_count": file_count,
                "broken_links": broken_links,
            })

        if replacements:
            after_text = "".join(output_lines)
            after_bytes = after_text.encode("utf-8")
            if after_bytes != original_bytes:
                file_plans.append({
                    "path": relative_to_root(settings_path),
                    "before_sha256": sha256_bytes(original_bytes),
                    "after_sha256": sha256_bytes(after_bytes),
                    "mode": stat.S_IMODE(settings_path.stat().st_mode),
                    "before_base64": base64.b64encode(original_bytes).decode("ascii"),
                    "after_base64": base64.b64encode(after_bytes).decode("ascii"),
                    "replacements": replacements,
                })

    summary = {
        "settings_files_scanned": len(list((ROOT / "collections").glob("*/settings.conf"))),
        "media_settings_scanned": len(audit_rows),
        "legacy_launchbox_paths": total_legacy,
        "settings_files_to_update": len(file_plans),
        "settings_paths_to_update": sum(len(x["replacements"]) for x in file_plans),
        "unresolved_launchbox_paths": len(unresolved),
        "unresolved_by_reason": dict(Counter(x["reason"] for x in unresolved)),
        "mapped_platforms": len({x["system_id"] for x in audit_rows if x["system_id"]}),
    }
    return audit_rows, summary, file_plans, unresolved


def active_layout_name(media_map: dict[str, Any]) -> str:
    root_settings = ROOT / "settings.conf"
    if root_settings.is_file():
        current = parse_properties(root_settings.read_text(encoding="utf-8", errors="replace")).get("layout", "")
        if current:
            return current
    return str(media_map.get("active_layout", ""))


def audit_main_platform_media(media_map: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    layout = active_layout_name(media_map)
    layout_file = ROOT / "layouts" / layout / "layout.xml"
    rows: list[dict[str, Any]] = []
    if not layout_file.is_file():
        return rows, {"layout": layout, "image_types": [], "platform_items": 0, "status": "active_layout_missing"}

    try:
        xml_root = ET.parse(layout_file).getroot()
    except ET.ParseError as error:
        return rows, {"layout": layout, "image_types": [], "platform_items": 0, "status": f"active_layout_xml_error: {error}"}

    image_types = sorted({
        menu.get("imageType", "").strip()
        for menu in xml_root.iter("menu")
        if menu.get("menuIndex", "0") == "0"
        and menu.get("imageType", "").strip()
    })
    menu_file = ROOT / "collections" / "Main" / "menu.txt"
    entries = [x.strip() for x in menu_file.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    entries = [x for x in entries if x.casefold() != "menu collections"]
    image_extensions = {".png", ".jpg", ".jpeg", ".jfif", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
    alias_root = ROOT / "collections" / "Main" / "medium_artwork"

    for image_type in image_types:
        category_dir = alias_root / image_type
        available: dict[str, list[Path]] = defaultdict(list)
        if category_dir.is_dir():
            for entry in category_dir.iterdir():
                if entry.is_file() or entry.is_symlink():
                    if entry.suffix.casefold() in image_extensions:
                        available[entry.stem.casefold()].append(entry)
        for title in entries:
            candidates = available.get(title.casefold(), [])
            status = "missing_platform_media"
            resolved = ""
            target = ""
            if candidates:
                valid = []
                for candidate in candidates:
                    try:
                        resolved_path = candidate.resolve(strict=True)
                        resolved_path.relative_to(ROOT)
                        if resolved_path.is_file():
                            valid.append((candidate, resolved_path))
                    except (OSError, ValueError):
                        continue
                if len(valid) == 1:
                    status = "valid_asset_path"
                    resolved = relative_to_root(valid[0][1])
                    target = os.readlink(valid[0][0]) if valid[0][0].is_symlink() else ""
                elif len(valid) > 1:
                    status = "multiple_candidates_review"
                else:
                    status = "broken_or_outside_project"
            rows.append({
                "layout": layout,
                "menu_collection": title,
                "image_type": image_type,
                "expected_directory": relative_to_root(category_dir),
                "candidate_count": len(candidates),
                "status": status,
                "resolved_target": resolved,
                "symlink_target": target,
            })

    counts = Counter(row["status"] for row in rows)
    return rows, {
        "layout": layout,
        "image_types": image_types,
        "platform_items": len(entries),
        "checks": len(rows),
        "status_counts": dict(counts),
        "status": "audited",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        write_bytes_atomic(path, b"\n")
        return
    fields = list(rows[0].keys())
    from io import StringIO
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    write_bytes_atomic(path, stream.getvalue().encode("utf-8"))


def create_audit(media_map: dict[str, Any], write_reports: bool = True) -> dict[str, Any]:
    collection_rows, collection_summary, file_plans, unresolved = scan_collection_settings(media_map)
    platform_rows, platform_summary = audit_main_platform_media(media_map)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "project_root_relative_to_retrofe": "../..",
        "collection_media_summary": collection_summary,
        "collection_media_paths": collection_rows,
        "platform_media_summary": platform_summary,
        "platform_media_paths": platform_rows,
        "unresolved_launchbox_paths": unresolved,
        "planned_settings_files": len(file_plans),
    }
    if write_reports:
        save_json_atomic(AUDIT_JSON, report)
        write_csv(AUDIT_CSV, collection_rows)
        write_csv(PLATFORM_MEDIA_CSV, platform_rows)
    return report


def retrofe_processes() -> list[dict[str, Any]]:
    found = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            exe = os.readlink(entry / "exe")
        except (OSError, FileNotFoundError):
            continue
        if comm.casefold() == "retrofe" or Path(exe).name.casefold() == "retrofe":
            found.append({"pid": int(entry.name), "comm": comm, "exe": exe})
    return found


def default_manifest_path() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    return ROOT / "Manifests" / f"collection-media-path-migration-{stamp}.json"


def make_plan(path: Path, media_map: dict[str, Any]) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"O manifesto já existe; escolha outro caminho: {path}")
    report = create_audit(media_map, write_reports=True)
    _, _, file_plans, unresolved = scan_collection_settings(media_map)
    plan = {
        "schema_version": 1,
        "operation": "retrofe_collection_media_path_migration",
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "project_root_relative_to_retrofe": "../..",
        "retrofe_root": ".",
        "status": "prepared",
                "reason": "Reapontar paths LaunchBox conforme o media-map, corrigir somente overrides internos comprovados e normalizar separadores Windows em caminhos relativos media.* sem alterar seus destinos.",
        "expected_unresolved_items": unresolved,
        "summary": report["collection_media_summary"],
        "platform_media_summary": report["platform_media_summary"],
        "files": file_plans,
    }
    save_json_atomic(path, plan)
    return plan


def read_manifest(path: Path) -> dict[str, Any]:
    plan = load_json(path)
    if plan.get("operation") != "retrofe_collection_media_path_migration":
        raise ValueError(f"Manifesto não é uma migração de caminhos de mídia: {path}")
    return plan


def mutation_event(manifest_path,event,**details):
    """Small append-only journal; never rewrite a large plan for every alias."""
    with manifest_path.with_suffix('.events.jsonl').open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'at':datetime.now().astimezone().isoformat(timespec='seconds'),'event':event,**details},ensure_ascii=False)+'\n')
        stream.flush();os.fsync(stream.fileno())


def planned_target(manifest: dict, item: dict) -> Path:
    if manifest.get("consumer") == "pegasus":
        allowed = {"../pegasus-frontend/game_dirs.txt", "../pegasus-frontend/metafiles/canonical-media.metadata.pegasus.txt"}
        if item["path"] not in allowed:
            raise ValueError("Destino fora da allowlist Pegasus")
    elif manifest.get("consumer") == "launch_entries":
        system = manifest.get("system_id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", system):
            raise ValueError("Plataforma inválida no plano de entradas")
        allowed = {f"../ES-DE/UserData/gamelists/{system}/gamelist.xml", "../ES-DE/UserData/settings/es_settings.xml"}
        for settings in _entry_collections(system):
            for name in ("exclude.txt", "exclude_all.txt"):
                allowed.add(relative_to_root(settings.parent / name))
        if item["path"] not in allowed:
            raise ValueError("Destino fora da allowlist de entradas dos frontends")
    elif Path(item["path"]).is_absolute() or ".." in Path(item["path"]).parts:
        raise ValueError("Destino fora da raiz RetroFE")
    target = ROOT / item["path"]
    for parent in [target, *target.parents]:
        if parent == PROJECT_ROOT:
            break
        if parent.is_symlink():
            raise ValueError(f"Caminho de configuração atravessa symlink: {parent}")
    return target


def validate_consumer_references(manifest: dict) -> None:
    if manifest.get("consumer") == "launch_entries":
        if _entry_snapshot(manifest["system_id"]) != manifest["entry_snapshot"]:
            raise ValueError("Descritores ou arquivos da plataforma mudaram desde o plano de entradas")
        for item in manifest["collection_inputs"]:
            settings = ROOT / item["path"]
            if settings.is_symlink() or sha256_bytes(settings.read_bytes()) != item["sha256"]:
                raise ValueError("A configuração da coleção mudou desde o plano de entradas")
    for item in manifest.get("reference_files", []):
        relative = Path(item["project_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Referência de consumidor fora da raiz")
        source = PROJECT_ROOT / relative
        if relative.parts[0] != "media" and relative.parts[:4] != ("frontend", "RetroFE", "Assets", "shared"):
            raise ValueError("Referência fora das bibliotecas canônicas")
        if source.is_symlink() or not source.is_file() or sha256_bytes(source.read_bytes()) != item["sha256"]:
            raise ValueError(f"Referência de mídia mudou desde o plano: {relative}")
    for item in manifest.get("source_files", []):
        relative = Path(item["project_path"])
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 3
                or relative.parts[0] != "roms" or relative.name != "metadata.pegasus.txt"):
            raise ValueError("Fonte de metadados fora do escopo Pegasus")
        source = PROJECT_ROOT / relative
        if source.is_symlink() or not source.is_file() or sha256_bytes(source.read_bytes()) != item["sha256"]:
            raise ValueError(f"Metadados de origem mudaram desde o plano: {relative}")


def _entry_collections(system):
    expected = (PROJECT_ROOT / "roms" / system).resolve()
    global_config = ROOT / "settings.conf"
    global_values = parse_properties(global_config.read_text()) if global_config.is_file() else {}
    raw_base = global_values.get("baseitempath", "")
    base = (ROOT / raw_base).resolve() if raw_base else None
    found = []
    for settings in sorted((ROOT / "collections").glob("*/settings.conf")):
        if settings.is_symlink() or settings.parent.is_symlink():
            continue
        values = parse_properties(settings.read_text())
        raw = values.get("list.path", "").replace("\\", "/")
        if "%BASE_ITEM_PATH%" in raw:
            if base is None:
                continue
            raw = raw.replace("%BASE_ITEM_PATH%", str(base))
        raw = raw.replace("%ITEM_COLLECTION_NAME%", settings.parent.name)
        if raw and (ROOT / raw).resolve() == expected:
            found.append(settings)
    return found


def _entry_snapshot(system):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", system):
        raise ValueError("Plataforma inválida")
    root = PROJECT_ROOT / "roms" / system
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Pasta canônica da plataforma ausente")
    rows = []
    for current, directories, files in os.walk(root, followlinks=False):
        parent = Path(current)
        directories[:] = [name for name in directories if not (parent / name).is_symlink()]
        for name in sorted(files):
            path = parent / name
            if path.is_symlink():
                continue
            info = path.stat()
            row = {"path": path.relative_to(root).as_posix(), "size": info.st_size,
                   "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "inode": info.st_ino}
            if path.suffix.lower() in {".cue", ".gdi", ".m3u"}:
                if info.st_size > 1024 * 1024:
                    raise ValueError("Descritor acima do limite de leitura")
                row["sha256"] = sha256_bytes(path.read_bytes())
            rows.append(row)
    return sorted(rows, key=lambda row: row["path"])


def make_launch_entries_plan(path, system):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import media_session
    import romtool
    if path.exists():
        raise FileExistsError("Manifesto existente preservado")
    snapshot = _entry_snapshot(system)
    root = PROJECT_ROOT / "roms" / system
    dependencies = media_session._track_paths(root, include_playlists=True)
    relative_dependencies = {p.relative_to(root).as_posix() for p in dependencies}
    gamelist = PROJECT_ROOT / "frontend/ES-DE/UserData/gamelists" / system / "gamelist.xml"
    settings_xml = PROJECT_ROOT / "frontend/ES-DE/UserData/settings/es_settings.xml"
    for configuration in (gamelist, settings_xml, ROOT / "settings.conf"):
        romtool.reject_symlink_components(configuration)
    files = []
    def proposal(target, after):
        exists = target.exists()
        if target.is_symlink():
            raise ValueError("Configuração por symlink preservada")
        before = target.read_bytes() if exists else b""
        if before != after:
            files.append({"path": Path(os.path.relpath(target, ROOT)).as_posix(), "before_exists": exists,
                          "before_sha256": sha256_bytes(before), "after_sha256": sha256_bytes(after),
                          "before_base64": base64.b64encode(before).decode(), "after_base64": base64.b64encode(after).decode(),
                          "mode": stat.S_IMODE(target.stat().st_mode) if exists else 0o644})
    xml = ET.fromstring(gamelist.read_bytes(), parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    if xml.tag != "gameList":
        raise ValueError("Gamelist exige o elemento gameList do ES-DE")
    games = {}
    for game in xml.findall("game"):
        key = (game.findtext("path") or "").replace("\\", "/").removeprefix("./")
        if key in games:
            raise ValueError("Gamelist tem caminhos duplicados; reconciliar antes de projetar entradas")
        games[key] = game
    for relative in sorted(relative_dependencies):
        game = games.get(relative)
        if game is None:
            game = ET.SubElement(xml, "game")
            ET.SubElement(game, "path").text = "./" + relative
        for tag in ("hidden", "nomultiscrape", "nogamecount"):
            node = game.find(tag)
            if node is None:
                node = ET.SubElement(game, tag)
            node.text = "true"
    added = []
    descriptive = {"name", "desc", "rating", "releasedate", "developer", "publisher", "genre", "players", "region", "lang"}
    for row in snapshot:
        descriptor = root / row["path"]
        if descriptor.suffix.lower() != ".m3u" or row["path"] in games or descriptor in dependencies:
            continue
        check = romtool.verify_m3u(descriptor)
        if check["status"] != "valid":
            continue
        members = [descriptor.parent / ref["name"] for ref in check["references"]]
        if any(not member.resolve().is_relative_to(root.resolve()) or member.suffix.lower() == ".m3u" for member in members):
            continue
        first = members[0]
        source = games.get(first.relative_to(root).as_posix())
        game = ET.SubElement(xml, "game")
        ET.SubElement(game, "path").text = "./" + row["path"]
        if source is not None:
            for child in source:
                if child.tag in descriptive:
                    node = ET.SubElement(game, child.tag, child.attrib)
                    node.text = child.text
        if game.find("name") is None:
            ET.SubElement(game, "name").text = descriptor.stem
        added.append(row["path"])
    proposal(gamelist, ET.tostring(xml, encoding="utf-8", xml_declaration=True))
    helper = media_session._helper({"PROJECT_ROOT": PROJECT_ROOT})
    config_text = settings_xml.read_text()
    for key, value in {"ShowHiddenGames": "false", "ScraperRespectExclusions": "true"}.items():
        if len(helper.setting_entries(config_text, key, "bool")) != 1:
            raise ValueError("Configuração ES-DE ambígua ou incompleta para " + key)
        config_text, _ = helper.updated_setting_text(config_text, key, value, "bool")
    proposal(settings_xml, config_text.encode())
    independent_stems = {Path(row["path"]).stem for row in snapshot if row["path"] not in relative_dependencies}
    excluded = sorted({p.stem for p in dependencies if p.stem not in independent_stems})
    retained = sorted({p.stem for p in dependencies if p.stem in independent_stems})
    collections = _entry_collections(system)
    global_config = ROOT / "settings.conf"
    collection_inputs = ([{"path": "settings.conf", "sha256": sha256_bytes(global_config.read_bytes())}]
                         if global_config.is_file() else [])
    for settings in collections:
        collection_inputs.append({"path": relative_to_root(settings), "sha256": sha256_bytes(settings.read_bytes())})
        for name in ("exclude.txt", "exclude_all.txt"):
            target = settings.parent / name
            text = target.read_text() if target.exists() else ""
            present = {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}
            missing = [stem for stem in excluded if stem not in present]
            if missing:
                proposal(target, (text.rstrip() + ("\n" if text.strip() else "") + "\n".join(missing) + "\n").encode())
    manifest = {"schema_version": 1, "operation": "retrofe_collection_media_path_migration", "mode": "consumer",
                "consumer": "launch_entries", "system_id": system, "status": "prepared",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"), "files": files,
                "entry_snapshot": snapshot, "collection_inputs": collection_inputs,
                "summary": {"dependencies_hidden_esde": len(dependencies), "playlist_entries_added": len(added),
                            "retrofe_excluded_basenames": len(excluded), "retrofe_collections": len(collections),
                            "files": len(files), "shared_basenames_retained": retained},
                "presentation_policy": "M3U usa metadados descritivos do primeiro disco. Estatísticas e favoritos originais permanecem nos registros existentes.",
                "permanent_deletion": False, "payload_copies": 0}
    save_json_atomic(path, manifest)
    return manifest


def make_pegasus_plan(path: Path) -> dict[str, Any]:
    if path.exists():
        raise FileExistsError(path)
    consumer = PROJECT_ROOT / "frontend/pegasus-frontend"
    directories = consumer / "game_dirs.txt"
    if consumer.is_symlink() or directories.is_symlink() or not directories.is_file():
        raise ValueError("Configuração Pegasus canônica ausente ou simbólica")
    data = load_json(PLATFORM_CATALOG_PATH)
    systems = {row["id"]: row for row in data.get("systems", []) if row.get("kind") == "platform"}
    old_lines = directories.read_text().splitlines()
    new_lines, blocked, changed = [], [], 0
    for line in old_lines:
        # The legacy spelling is a proven project path; do not rebase another home/disk.
        old_prefix = str(PROJECT_ROOT.with_name("Emulation") / "roms") + "/"
        if line.startswith(old_prefix):
            relative = Path(line[len(old_prefix):])
            target = PROJECT_ROOT / "roms" / relative
            if ".." not in relative.parts and target.is_dir() and not target.is_symlink():
                new_lines.append(str(target)); changed += 1
            else:
                new_lines.append(line); blocked.append({"path": line, "reason": "diretório legado sem alvo atual comprovado"})
        else:
            new_lines.append(line)
    projection = consumer / "metafiles/canonical-media.metadata.pegasus.txt"
    marker = "# emulation-managed canonical-media v1"
    if projection.exists() and (projection.is_symlink() or not projection.read_text().startswith(marker + "\n")):
        raise ValueError("Projeção Pegasus pessoal/divergente preservada")
    mapping = {"box_front": "cover-front", "box_back": "cover-back", "box_spine": "cover-spine",
               "box_full": "cover-full", "logo": "clearlogo", "marquee": "marquee", "background": "background",
               "screenshot": "screenshot", "titlescreen": "screentitle", "video": "video", "music": "music"}
    output, references, sources, games, collections = [marker, ""], {}, [], 0, 0
    for system_id, record in sorted(systems.items()):
        rom_root = PROJECT_ROOT / "roms" / record.get("rom_subdir", system_id)
        metadata = rom_root / "metadata.pegasus.txt"
        if not metadata.is_file() or metadata.is_symlink():
            continue  # Extend only collections already declared by this frontend.
        text = metadata.read_text()
        sources.append({"project_path": metadata.relative_to(PROJECT_ROOT).as_posix(), "sha256": sha256_bytes(metadata.read_bytes())})
        match = re.search(r"^collection:\s*(.+)$", text, re.MULTILINE)
        if not match:
            blocked.append({"path": str(metadata), "reason": "coleção sem identidade explícita"}); continue
        name = match.group(1).strip()
        short = re.search(r"^shortname:\s*(.+)$", text, re.MULTILINE)
        output.extend([f"collection: {name}", f"shortname: {short.group(1).strip() if short else system_id}", ""])
        extensions = {value.casefold().lstrip(".") for value in record.get("extensions", [])}
        by_stem = defaultdict(list)
        for current, dirs, files in os.walk(rom_root, followlinks=False):
            base = Path(current); dirs[:] = [value for value in dirs if not (base / value).is_symlink()]
            for filename in files:
                candidate = base / filename
                if not candidate.is_symlink() and candidate.suffix.casefold().lstrip(".") in extensions:
                    by_stem[candidate.stem].append(candidate)
        collections += 1
        for stem, candidates in sorted(by_stem.items()):
            if any(char in str(candidates[0]) for char in ("\r", "\n")):
                blocked.append({"path": system_id + "/" + stem, "reason": "nome com quebra de linha não projetável"}); continue
            if len(candidates) != 1:
                blocked.append({"path": system_id + "/" + stem, "reason": "basename de ROM ambíguo; mídia não associada"}); continue
            media_lines = []
            for field, category in mapping.items():
                directory = PROJECT_ROOT / "media" / system_id / category
                matches = sorted(p for p in directory.glob(stem.replace("[", "[[]") + ".*")
                                 if p.stem == stem and p.is_file() and not p.is_symlink()) if directory.is_dir() and not directory.is_symlink() else []
                if len(matches) != 1:
                    continue
                payload = matches[0]
                if any(char in str(payload) for char in ("\r", "\n")):
                    continue
                relative = payload.relative_to(PROJECT_ROOT).as_posix()
                references[relative] = {"project_path": relative, "sha256": sha256_bytes(payload.read_bytes())}
                media_lines.append(f"assets.{field}: {Path(os.path.relpath(payload, projection.parent)).as_posix()}")
            if media_lines:
                output.extend([f"game: {stem}", f"file: {Path(os.path.relpath(candidates[0], projection.parent)).as_posix()}", *media_lines, ""])
                games += 1
    files = []
    for target, after in [(directories, ("\n".join(new_lines) + "\n").encode()), (projection, ("\n".join(output) + "\n").encode())]:
        exists = target.is_file()
        before = target.read_bytes() if exists else b""
        if exists and before == after:
            continue
        files.append({"path": Path(os.path.relpath(target, ROOT)).as_posix(), "before_exists": exists,
                      "before_base64": base64.b64encode(before).decode(), "after_base64": base64.b64encode(after).decode(),
                      "before_sha256": sha256_bytes(before), "after_sha256": sha256_bytes(after),
                      "mode": stat.S_IMODE(target.stat().st_mode) if exists else 0o644})
    manifest = {"schema_version": 1, "operation": "retrofe_collection_media_path_migration", "mode": "consumer",
                "consumer": "pegasus", "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": "prepared", "files": files, "links": [], "reference_files": list(references.values()), "source_files": sources,
                "summary": {"directory_paths_rebased": changed, "collections": collections, "games_with_media": games,
                            "canonical_media_references": len(references), "blocked": len(blocked)}, "blocked": blocked}
    for item in files:
        planned_target(manifest, item)
    save_json_atomic(path, manifest)
    return manifest


def pegasus_processes() -> list[int]:
    found = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            if path.read_text().strip().casefold() in {"pegasus-fe", "pegasus-fe.exe", "pegasus-fronten"}:
                found.append(int(path.parent.name))
        except OSError:
            pass
    return found


def check_entry_frontend(manifest):
    if manifest.get("consumer") == "launch_entries":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import romtool
        if romtool.esde_processes_running():
            raise RuntimeError("Feche ES-DE normalmente antes de alterar suas entradas")


def apply_plan(path: Path, manifest: dict[str, Any]) -> None:
    check_entry_frontend(manifest)
    processes = retrofe_processes()
    if processes:
        raise RuntimeError(f"RetroFE está aberto; não alterei settings.conf: {processes}")
    if manifest.get("consumer") == "pegasus" and pegasus_processes():
        raise RuntimeError("Feche Pegasus normalmente antes de alterar suas fontes")
    if manifest.get("status") != "prepared":
        raise ValueError(f"Manifesto não está preparado: {manifest.get('status')}")
    path_errors=manifest.get('runtime_media',{}).get('path_diagnostics',{}).get('blocking_errors',[])
    if path_errors:
        raise ValueError('Caminhos de ROMs/mídia bloqueados: '+json.dumps(path_errors,ensure_ascii=False))
    validate_consumer_references(manifest)

    for item in manifest.get("links", []):
        target=ROOT/item["path"];source=ROOT/item["source"]
        if os.path.lexists(target):raise FileExistsError(f"Alias apareceu depois do plano: {target}")
        if source.is_symlink() or not source.is_file() or sha256_bytes(source.read_bytes())!=item["sha256"]:
            raise ValueError(f"Mídia mudou depois do plano: {source}")
        if not source.resolve().is_relative_to(PROJECT_ROOT/'media'):raise ValueError("Alias fora da mídia canônica")
        for parent in target.parents:
            if parent==ROOT:break
            if parent.is_symlink():raise ValueError(f"Projeção atravessa symlink: {parent}")

    for item in manifest.get("files", []):
        target = planned_target(manifest, item)
        if not item.get("before_exists", True) and os.path.lexists(target):
            raise FileExistsError(target)
        current = target.read_bytes() if item.get("before_exists", True) else b""
        if sha256_bytes(current) != item["before_sha256"]:
            raise ValueError(f"settings.conf mudou depois do plano: {item['path']}")
        after = base64.b64decode(item["after_base64"], validate=True)
        if sha256_bytes(after) != item["after_sha256"]:
            raise ValueError(f"Hash do conteúdo previsto não confere: {item['path']}")

    applied: list[str] = []
    linked: list[Path] = []
    created_dirs=set()
    try:
        for item in manifest.get("links", []):
            target=ROOT/item["path"]
            parent=target.parent
            while not parent.exists():
                created_dirs.add(relative_to_root(parent));parent=parent.parent
            target.parent.mkdir(parents=True,exist_ok=True)
            mutation_event(path,'alias_create_started',path=item['path'],target=item['target'])
            target.symlink_to(item["target"])
            linked.append(target)
            mutation_event(path,'alias_created',path=item['path'],target=item['target'],source_sha256=item['sha256'])
        for item in manifest.get("files", []):
            target = planned_target(manifest, item)
            if not target.parent.exists():
                created_dirs.add(relative_to_root(target.parent))
            after = base64.b64decode(item["after_base64"], validate=True)
            mutation_event(path,'settings_write_started',path=item['path'],before_sha256=item['before_sha256'],after_sha256=item['after_sha256'])
            write_bytes_atomic(target, after, int(item["mode"]))
            if sha256_bytes(target.read_bytes()) != item["after_sha256"]:
                raise RuntimeError(f"Verificação imediata falhou: {item['path']}")
            applied.append(item["path"])
            mutation_event(path,'settings_written',path=item['path'],sha256=item['after_sha256'])
    except Exception:
        for target in linked:
            expected=next(x['target'] for x in manifest['links'] if ROOT/x['path']==target)
            if target.is_symlink() and os.readlink(target)==expected:target.unlink()
        for directory in sorted(created_dirs,key=len,reverse=True):
            target=ROOT/directory
            if target.is_dir() and not any(target.iterdir()):target.rmdir()
        for item in manifest.get("files", []):
            if item["path"] not in applied:
                continue
            target = planned_target(manifest, item)
            if sha256_bytes(target.read_bytes()) == item["after_sha256"]:
                before = base64.b64decode(item["before_base64"], validate=True)
                if item.get("before_exists", True):
                    write_bytes_atomic(target, before, int(item["mode"]))
                else:
                    target.unlink()
        raise

    manifest["status"] = "applied"
    manifest["applied_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    manifest["applied_files"] = applied
    manifest["created_directories"]=sorted(created_dirs)
    save_json_atomic(path, manifest)
    mutation_event(path,'apply_completed',files=len(applied),aliases=len(linked))


def verify_plan(path: Path, manifest: dict[str, Any], media_map: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for item in manifest.get("files", []):
        target = planned_target(manifest, item)
        exists = target.is_file()
        current_hash = sha256_bytes(target.read_bytes()) if exists else ""
        checks.append({"path": item["path"], "status": "verified" if current_hash == item["after_sha256"] else "failed", "sha256": current_hash})
    for item in manifest.get("links", []):
        target=ROOT/item["path"]
        good=target.is_symlink() and os.readlink(target)==item["target"] and target.is_file() and sha256_bytes(target.read_bytes())==item["sha256"]
        checks.append({"path":item["path"],"status":"verified" if good else "failed"})
    if manifest.get("mode") in {"runtime", "consumer"}:
        validate_consumer_references(manifest)
        failed=sum(x["status"]!="verified" for x in checks)
        manifest.update(status="completed_verified" if not failed else "verification_failed",verification={"failed":failed,"checks":checks})
        save_json_atomic(path,manifest)
        return {"manifest":relative_to_root(path),"status":manifest["status"],"failed_files":failed,"verified_files":len(checks)-failed}
    report = create_audit(media_map, write_reports=True)
    verified_files = sum(x["status"] == "verified" for x in checks)
    remaining_legacy = report["collection_media_summary"]["legacy_launchbox_paths"]
    unresolved = report["collection_media_summary"]["unresolved_launchbox_paths"]
    failed_files = len(checks) - verified_files
    if not failed_files:
        manifest["status"] = "completed_verified" if not remaining_legacy and not unresolved else "completed_with_pending"
        manifest["verified_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        manifest["verification_summary"] = {
            "verified_settings_files": verified_files,
            "failed_settings_files": failed_files,
            "remaining_legacy_paths": remaining_legacy,
            "unresolved_launchbox_paths": unresolved,
        }
        save_json_atomic(path, manifest)
    result = {
        "operation": manifest.get("operation"),
        "manifest": relative_to_root(path),
        "manifest_status": manifest.get("status"),
        "file_checks": len(checks),
        "verified_files": verified_files,
        "failed_files": failed_files,
        "remaining_legacy_paths": remaining_legacy,
        "unresolved_launchbox_paths": unresolved,
        "platform_media_summary": report["platform_media_summary"],
        "checks": checks,
    }
    return result


def rollback_plan(path: Path, manifest: dict[str, Any]) -> None:
    check_entry_frontend(manifest)
    processes = retrofe_processes()
    if processes:
        raise RuntimeError(f"RetroFE está aberto; não alterei settings.conf: {processes}")
    if manifest.get("consumer") == "pegasus" and pegasus_processes():
        raise RuntimeError("Feche Pegasus normalmente antes de reverter suas fontes")
    if manifest.get("status") not in {"applied", "completed_verified", "completed_with_pending"}:
        raise ValueError(f"Manifesto não está aplicado: {manifest.get('status')}")
    for item in manifest.get("files", []):
        target = planned_target(manifest, item)
        if not target.is_file() or sha256_bytes(target.read_bytes()) != item["after_sha256"]:
            raise ValueError(f"Arquivo mudou depois da aplicação; rollback bloqueado: {item['path']}")
    for item in manifest.get("links", []):
        target=ROOT/item["path"]
        if not target.is_symlink() or os.readlink(target)!=item["target"]:raise ValueError(f"Alias mudou: {target}")
    for item in manifest.get("files", []):
        target = planned_target(manifest, item)
        before = base64.b64decode(item["before_base64"], validate=True)
        if item.get("before_exists", True):
            write_bytes_atomic(target, before, int(item["mode"]))
        else:
            target.unlink()
    for item in manifest.get("links", []):(ROOT/item["path"]).unlink()
    for directory in sorted(manifest.get('created_directories',[]),key=len,reverse=True):
        target=ROOT/directory
        if target.is_dir() and not any(target.iterdir()):target.rmdir()
    manifest["status"] = "rolled_back"
    manifest["rolled_back_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    save_json_atomic(path, manifest)


def print_summary(report: dict[str, Any]) -> None:
    print(json.dumps({
        "collection_media_summary": report["collection_media_summary"],
        "platform_media_summary": report["platform_media_summary"],
        "reports": [str(AUDIT_JSON), str(AUDIT_CSV), str(PLATFORM_MEDIA_CSV)],
    }, ensure_ascii=False, indent=2))


def runtime_plan(media_map: dict[str, Any]) -> dict[str, Any]:
    """Prepare config and relative cover aliases, without changing game/media bytes."""
    policy=media_map["collection_media_mapping"]["runtime_media"]
    by_subdir,configured,display=config_system_records()
    overrides=media_system_overrides(media_map)
    root_relative,_=legacy_media_map(media_map)
    files,links,collections,games,blocked=[],[],[],[],[]
    cache={}
    global_settings=ROOT/'settings.conf'
    global_values=parse_properties(global_settings.read_text()) if global_settings.is_file() else {}
    def root_path(value):
        path=Path(value).expanduser()
        return path if path.is_absolute() else ROOT/path
    rom_base=root_path(global_values.get('baseitempath',str(PROJECT_ROOT/'roms')))
    media_base=root_path(global_values.get('basemediapath','.') or '.')
    diagnostics={'rom_base':os.path.relpath(rom_base,ROOT),'media_base':os.path.relpath(media_base,ROOT),'blocking_errors':[],'collections':[]}
    if not rom_base.is_dir():
        diagnostics['blocking_errors'].append({'code':'rom_base_missing','path':os.path.relpath(rom_base,ROOT),'reason':'Raiz de ROMs configurada não está acessível; confira a montagem ou o perfil do host.'})
    if media_base.resolve()!=ROOT.resolve():
        diagnostics['blocking_errors'].append({'code':'media_base_conflict','path':os.path.relpath(media_base,ROOT),'reason':'baseMediaPath diverge da base dos caminhos relativos do mapa canônico; preservar configuração e corrigir o perfil antes de aplicar.'})
    inventory_cache={}
    for settings in sorted((ROOT/'collections').glob('*/settings.conf')):
        if settings.is_symlink():raise ValueError(f"settings.conf por symlink recusado: {settings}")
        collection=settings.parent.name
        before=settings.read_bytes();text=before.decode('utf-8');values=parse_properties(text)
        sid,origin,evidence=system_for_collection(collection,values.get('list.path',''),by_subdir,configured,display,overrides)
        if not sid:continue  # composite/menu collections retain their item origins
        rom_root=rom_base/sid
        raw=values.get('list.path','').replace('\\','/').replace('%BASE_ITEM_PATH%',str(rom_base)).replace('%ITEM_COLLECTION_NAME%',collection)
        actual=Path(raw) if raw.startswith('/') else ROOT/raw
        path_row={'collection':collection,'system_id':sid,'configured_rom_path':values.get('list.path',''),'resolved_rom_path':os.path.relpath(actual,ROOT),'rom_path_exists':actual.is_dir(),'issues':[]}
        diagnostics['collections'].append(path_row)
        if not raw or actual.resolve()!=rom_root.resolve():
            path_row['issues'].append('list.path não comprova a raiz física desta plataforma')
            blocked.append({'collection':collection,'system_id':sid,'reason':'list.path não comprova a raiz física desta plataforma'});continue
        if not rom_root.is_dir():
            path_row['issues'].append('diretório de ROMs não acessível; nenhum caminho foi reescrito')
            blocked.append({'collection':collection,'system_id':sid,'reason':'diretório de ROMs não acessível','path':os.path.relpath(rom_root,ROOT)});continue
        extensions={'.'+s.strip().lower().lstrip('.') for s in values.get('list.extensions','').split(',') if s.strip()}
        recursive=values.get('list.romhierarchy','false').lower() in {'true','yes','1'}
        key=(sid,tuple(sorted(extensions)),recursive)
        if key not in cache:
            names=defaultdict(list)
            paths=rom_root.rglob('*') if recursive else rom_root.glob('*')
            for rom in paths:
                if rom.is_file() and rom.suffix.lower() in extensions:names[rom.stem].append(rom)
            cache[key]=names
        names=cache[key]
        root=PROJECT_ROOT/'media'/sid
        projection=settings.parent/'medium_artwork/cover'
        if projection.is_symlink() or projection.parent.is_symlink():
            path_row['issues'].append('projeção cover existente por diretório symlink preservada')
            blocked.append({'collection':collection,'system_id':sid,'reason':'projeção cover por symlink de diretório; preservar estrutura existente'});continue
        cover_matches={}
        for name,roms in names.items():
            if len(roms)!=1:
                blocked.append({'collection':collection,'system_id':sid,'name':name,'reason':'basename de ROM ambíguo; não associar mídia'});continue
            # Existing presentation files retain precedence. No title guessing,
            # default-as-game substitution or directory-wide category switching.
            existing=next((projection/(name+ext) for ext in policy['image_extensions'] if (projection/(name+ext)).is_file()),None)
            if existing:
                cover_matches[name]=relative_to_root(existing);continue
            source=next((root/category/(name+ext) for category in policy['cover_source_order'] for ext in policy['image_extensions'] if (root/category/(name+ext)).is_file()),None)
            if not source:continue
            resolved=source.resolve(strict=True)
            if not resolved.is_relative_to(PROJECT_ROOT/'media'):raise ValueError(f"Mídia fora da raiz: {source}")
            target=projection/source.name
            if os.path.lexists(target):
                blocked.append({'collection':collection,'system_id':sid,'name':name,'reason':'alias cover existente divergente/quebrado; sem sobrescrita'});continue
            links.append({'path':relative_to_root(target),'source':os.path.relpath(resolved,ROOT),'target':os.path.relpath(resolved,target.parent),'sha256':sha256_bytes(resolved.read_bytes()),'size_bytes':resolved.stat().st_size,'system_id':sid,'rom':os.path.relpath(roms[0],PROJECT_ROOT),'reason':'exact unique physical ROM basename; semantic cover/front-cover rule','source_category':source.parent.name})
            cover_matches[name]=relative_to_root(target)
        expected={key:f'{root_relative}/{sid}/{category}' for key,category in policy['keys'].items()}
        expected['cover']=relative_to_root(projection)
        changes=[];out=text
        for media_key,value in expected.items():
            key='media.'+media_key;old=values.get(key)
            if old==value:continue
            normalized=(old or '').replace('\\','/').replace('%ITEM_COLLECTION_NAME%',collection)
            legacy=normalized.startswith(f'collections/{collection}/medium_artwork/')
            canonical=normalized.startswith(root_relative+'/'+sid+'/')
            if old and not (legacy or canonical or 'launchbox' in normalized.lower()):
                blocked.append({'collection':collection,'system_id':sid,'media_key':media_key,'reason':'saída personalizada preservada; exige evidência de migração','current_value':old});continue
            pattern=r'^[ \t]*'+re.escape(key)+r'[ \t]*=.*$'
            matches=list(re.finditer(pattern,out,re.MULTILINE|re.IGNORECASE))
            removed_duplicates=0
            if len(matches)>1:
                line_values=[out[match.start():match.end()].split('=',1)[1].strip() for match in matches]
                if len(set(line_values))!=1:
                    blocked.append({'collection':collection,'system_id':sid,'media_key':media_key,'reason':'chave duplicada com valores diferentes; arquivo preservado','current_values':line_values})
                    continue
                for match in reversed(matches[1:]):
                    start,end=match.start(),match.end()
                    if end<len(out) and out[end]=='\n':end+=1
                    out=out[:start]+out[end:]
                    removed_duplicates+=1
                matches=list(re.finditer(pattern,out,re.MULTILINE|re.IGNORECASE))
                if len(matches)!=1:
                    blocked.append({'collection':collection,'system_id':sid,'media_key':media_key,'reason':'chave duplicada idêntica não pôde ser reduzida; arquivo preservado'})
                    continue
            line=f'{key} = {value}'
            if matches:
                match=matches[0];out=out[:match.start()]+line+out[match.end():]
            else:out=out.rstrip()+'\n'+line+'\n'
            change={'media_key':media_key,'old_value':old,'new_value':value,'mapping_source':origin,'mapping_evidence':evidence}
            if removed_duplicates:change['duplicate_identical_lines_removed']=removed_duplicates
            changes.append(change)
        after=out.encode('utf-8')
        if after!=before:
            files.append({'path':relative_to_root(settings),'before_sha256':sha256_bytes(before),'after_sha256':sha256_bytes(after),'mode':stat.S_IMODE(settings.stat().st_mode),'before_base64':base64.b64encode(before).decode(),'after_base64':base64.b64encode(after).decode(),'replacements':changes})
        effective=parse_properties(out)
        counts=Counter()
        for name,roms in names.items():
            if len(roms)!=1:continue
            media={}
            for media_key in ['cover','video','screenshot','artwork_front','logo']:
                raw=effective.get('media.'+media_key,'')
                target=normalize_config_target(raw,collection) if raw else None
                extensions=['.mp4','.mkv','.avi','.webm'] if media_key=='video' else policy['image_extensions']
                found=next((target/(name+ext) for ext in extensions if target and (target/(name+ext)).is_file()),None)
                # Planned links are visible only after application.
                if media_key=='cover' and raw==expected['cover'] and name in cover_matches:media[media_key]=cover_matches[name]
                else:media[media_key]=os.path.relpath(found,ROOT) if found else None
                if media[media_key]:counts[media_key]+=1
            games.append({'collection':collection,'system_id':sid,'rom':os.path.relpath(roms[0],PROJECT_ROOT),'basename':name,'media':media})
        collections.append({'collection':collection,'system_id':sid,'games':sum(len(v)==1 for v in names.values()),'available_exact_matches':dict(counts),'mapping_source':origin})
        path_row['unique_rom_basenames']=sum(len(v)==1 for v in names.values())
        path_row['ambiguous_rom_basenames']=sum(len(v)>1 for v in names.values())
        path_row['missing_media_by_type']={k:path_row['unique_rom_basenames']-counts[k] for k in ['cover','video','screenshot','artwork_front','logo']}
        path_row['media_paths']={}
        for media_key in ['cover','video','screenshot','artwork_front','logo']:
            raw=effective.get('media.'+media_key,'')
            target=normalize_config_target(raw,collection) if raw else None
            entries=[]
            if target is not None:
                key=str(target)
                if key not in inventory_cache:inventory_cache[key]=list(target.iterdir()) if target.is_dir() else []
                entries=inventory_cache[key]
            missing_links=[p.name for p in entries if p.is_symlink() and not p.exists()]
            supported=['.mp4','.mkv','.avi','.webm'] if media_key=='video' else policy['image_extensions']
            unmatched=[p.name for p in entries if p.is_file() and p.suffix.lower() in supported and p.stem.casefold()!='default' and (p.stem not in names or len(names[p.stem])!=1)]
            path_row['media_paths'][media_key]={'configured':raw,'resolved':os.path.relpath(target,ROOT) if target is not None else None,'directory_exists':bool(target and target.is_dir()),'broken_links':missing_links,'files_without_unique_rom_match':len(unmatched),'opaque_filenames':sum(bool(re.fullmatch(r'[0-9a-fA-F]{32,64}',Path(n).stem)) for n in unmatched)}
    diagnostics['summary']={'collections_with_rom_path_issues':sum(bool(r['issues']) for r in diagnostics['collections']),'missing_media_directories':sum(not p['directory_exists'] for r in diagnostics['collections'] for p in r.get('media_paths',{}).values()),'broken_media_links':sum(len(p['broken_links']) for r in diagnostics['collections'] for p in r.get('media_paths',{}).values()),'collections_with_missing_game_media':sum(any(r.get('missing_media_by_type',{}).values()) for r in diagnostics['collections'])}
    return {'schema_version':1,'operation':'retrofe_collection_media_path_migration','mode':'runtime','status':'prepared','created_at':datetime.now().astimezone().isoformat(timespec='seconds'),'files':files,'links':links,'runtime_media':{'collections':collections,'games':games,'blocked':blocked,'path_diagnostics':diagnostics,'summary':{'collections':len(collections),'games':len(games),'planned_settings':len(files),'planned_aliases':len(links),'blocked':len(blocked),'game_media_counts':dict(Counter(k for g in games for k,v in g['media'].items() if v))}},'permanent_deletion':False,'payload_copies':0}


def runtime_readiness(plan):
    diagnostics=plan['runtime_media']['path_diagnostics']
    warnings=bool(plan['runtime_media']['blocked'] or any(diagnostics['summary'].values()))
    ready=not diagnostics['blocking_errors']
    status='blocked' if not ready else 'needs_configuration' if plan['files'] or plan['links'] else 'ready_with_warnings' if warnings else 'ready'
    return {'status':status,'launch_ready':ready,'summary':plan['runtime_media']['summary'],'path_summary':diagnostics['summary'],'blocking_errors':diagnostics['blocking_errors'],'audit':str(AUDIT_JSON.relative_to(PROJECT_ROOT))}


def write_runtime_report(plan):
    report=load_json(AUDIT_JSON) if AUDIT_JSON.exists() else {}
    runtime=plan['runtime_media']
    runtime['summary']={k:v for k,v in runtime['summary'].items() if not k.startswith('planned_')}
    if report.get('runtime_media')!=runtime:
        report['runtime_media']=runtime
        save_json_atomic(AUDIT_JSON,report)


def runtime_records(plan,romtool,manifest=None):
    """Refresh the existing global state/pending list, never a parallel catalogue."""
    catalog_rows=None
    if manifest:catalog_rows=runtime_catalog(read_manifest(manifest),romtool,manifest)
    path=romtool.OUTPUT_ROOT/'pendencias-integridade-dependencias.csv'
    if path.exists():
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
        previous=list(rows);rows=[r for r in rows if r.get('source')!='retrofe-runtime-media']
        grouped=defaultdict(list)
        for issue in plan['runtime_media'].get('path_diagnostics',{}).get('blocking_errors',[]):
            grouped['Main'].append({'reason':issue['reason']+' Caminho: '+issue['path']})
        for case in plan['runtime_media']['blocked']:grouped[case['collection']].append(case)
        for row in plan['runtime_media'].get('path_diagnostics',{}).get('collections',[]):
            if any(row.get('missing_media_by_type',{}).values()):
                grouped[row['collection']].append({'reason':'mídia ausente ou sem associação exata: '+json.dumps(row['missing_media_by_type'],ensure_ascii=False)})
        for collection,cases in sorted(grouped.items()):
            reasons=dict(Counter(c['reason'] for c in cases))
            rows.append({k:v for k,v in {'type':'frontend_media','source':'retrofe-runtime-media','path':'frontend/RetroFE/collections/'+collection,'status':json.dumps(reasons,ensure_ascii=False),'dependency_or_impact':'Caminhos, mídia ausente e associações ambíguas separados; arquivos preservados, sem autorização de exclusão.','next_step':'Consultar runtime_media.path_diagnostics e blocked na auditoria oficial; confirmar basename/edição e proveniência antes de associar arquivos opacos.'}.items() if k in fields})
        normalized=[{k:str(r.get(k,'')) for k in fields} for r in rows]
        if normalized!=previous:romtool.write_csv_atomic(path,rows,fields)
    path=romtool.OUTPUT_ROOT/'estado-final-verificacao.json'
    if path.exists():
        state=load_json(path)
        catalog_changed=catalog_rows is not None and state.get('current_catalog_rows')!=catalog_rows
        if catalog_rows is not None:state['current_catalog_rows']=catalog_rows
        previous=state.get('retrofe_runtime_media',{})
        summary=plan['runtime_media']['summary']
        readiness=runtime_readiness(plan)
        node={**previous,'status':readiness['status'],'launch_ready':readiness['launch_ready'],'path_summary':readiness['path_summary'],'summary':{k:v for k,v in summary.items() if not k.startswith('planned_')},'audit':'frontend/RetroFE/Manifests/collection-media-path-audit.json','payload_copies':0,'permanent_deletion':False,'launcher_preflight':True}
        if manifest:node['last_applied_manifest']=relative_to_root(manifest)
        if node!=previous or catalog_changed:
            node['as_of']=romtool.now();state['retrofe_runtime_media']=node;romtool.atomic_json(path,state)


def runtime_catalog(manifest,romtool,path):
    catalog=romtool.OUTPUT_ROOT/'catalogo-arquivos.csv'
    if not catalog.exists():return None
    with catalog.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
    index={row.get('current_absolute_path') or row.get('absolute_path'):row for row in rows}
    for link in manifest.get('links',[]):
        alias=ROOT/link['path'];key=str(alias)
        row=index.get(key)
        if row is None:
            row={};rows.append(row)
        literal=link['target'].encode()
        data={'system_id':link['system_id'],'source':'retrofe_runtime_media','source_root':str(ROOT),'relative_path':link['path'],'absolute_path':key,'file_name':alias.name,'game_or_set_hint':Path(link['rom']).stem,'function_hint':'alias relativo de apresentação; nenhum payload duplicado','inventory_category':'media_alias','extension':alias.suffix,'size_bytes':len(literal),'sha256':sha256_bytes(literal),'hash_status':'SHA-256 do texto do symlink; hash do payload na criação consta no manifesto','hash_evidence':os.path.relpath(path,PROJECT_ROOT),'dependencies':link['target']+'; payload_sha256_at_creation='+link['sha256'],'integrity_status':'alias verificado na aplicação','destination_proposal':key,'action':'apresentação cover por symlink relativo','current_absolute_path':key,'current_path_status':'alias removido por rollback' if manifest['status']=='rolled_back' else 'alias relativo verificado; payload canônico compartilhado'}
        row.update({k:v for k,v in data.items() if k in fields})
    romtool.write_csv_atomic(catalog,rows,fields)
    return len(rows)



SESSION_LOCK_ENV = 'RETROFE_MEDIA_SESSION_FD'
MEDIA_TASK_PENDING = Path('UserData/retrofe/media-task-pending.json')


def runtime_lock_path():
    # Reuse the project's only media lock and its configured operations root.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import romtool
    return romtool.OPS_ROOT / 'media-write.lock'


def validated_session_fd(path):
    raw = os.environ.get(SESSION_LOCK_ENV)
    if raw is None:
        return None
    try:
        fd = int(raw)
        expected, actual = path.lstat(), os.fstat(fd)
        if not stat.S_ISREG(expected.st_mode) or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError('Descritor não pertence ao lock canônico de mídia')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, ValueError) as exc:
        raise RuntimeError('Reserva de mídia herdada inválida; não vou ignorar o bloqueio') from exc


@contextmanager
def startup_media_lock(path, wait_seconds=120, on_wait=None):
    # Blocking flock queues at the next item boundary; polling lost repeatedly
    # to the batch writer. Never unlink the inode or signal its current owner.
    if path.is_symlink():
        raise ValueError('Lock de mídia por symlink recusado')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    def expired(signum, frame):
        raise TimeoutError(f'O gravador de mídia não liberou a operação atual em {wait_seconds:g} segundos; aguarde seu encerramento normal e tente novamente.')
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if on_wait: on_wait()
            previous = signal.signal(signal.SIGALRM, expired)
            timer = signal.setitimer(signal.ITIMER_REAL, wait_seconds)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            finally:
                signal.setitimer(signal.ITIMER_REAL, *timer)
                signal.signal(signal.SIGALRM, previous)
        acquired = True
        yield fd
    finally:
        if acquired: fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def frontend_instance_lock():
    """One launcher or isolated engine audit per installation; never queue clicks."""
    path = ROOT / 'UserData/retrofe/frontend-session.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Já existe uma sessão ou teste do RetroFE nesta instalação; não abri outra instância.') from exc
        yield fd
    finally:
        os.close(fd)


def launch_runtime(arguments):
    with frontend_instance_lock():
        return _launch_runtime(arguments)


def _launch_runtime(arguments):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import romtool
    active = [p for p in romtool.retrofe_processes_running()
              if p['executable'].removesuffix(' (deleted)') == str(ROOT / 'RetroFE')]
    if active:
        raise RuntimeError(f'RetroFE já está ativo fora deste supervisor; encerre a sessão normalmente antes de abrir outra: {active}')
    path = runtime_lock_path()
    if os.environ.get(SESSION_LOCK_ENV) is not None:
        raise RuntimeError('Uma reserva já existe; launch-runtime não pode ser aninhado')
    def waiting():
        message = 'Aguardando a gravação de mídia atual. O lote continuará ao fechar o RetroFE.'
        print('[MÍDIA] ' + message, file=sys.stderr, flush=True)
        notifier = shutil.which('notify-send')
        if notifier and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            try: subprocess.run([notifier, '--app-name=RetroFE', '--expire-time=10000', 'RetroFE — aguardando mídia', message], timeout=3, capture_output=True)
            except (OSError, subprocess.TimeoutExpired): pass
    args = list(arguments)
    if args[:1] == ['--']: args = args[1:]
    while True:
        with startup_media_lock(path, on_wait=waiting) as fd:
            env = {**os.environ, SESSION_LOCK_ENV: str(fd)}
            child = subprocess.Popen(['bash', str(ROOT / 'Tools/launchers/retrofe/launch-retrofe.sh'), *args], env=env, pass_fds=(fd,))
            def forward(signum, frame):
                if child.poll() is None: child.send_signal(signum)
            old = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
            try:
                result = child.wait()
                result = result if result >= 0 else 128-result
            finally:
                # Explicit unlock in startup_media_lock releases even descriptors
                # inherited by a detached helper; it cannot strand the batch lock.
                for sig, handler in old.items(): signal.signal(sig, handler)
        # A media task queued from Options (Skraper) needs the same lock: it runs only
        # after RetroFE closed normally and the lease above was released, then RetroFE reopens.
        if result != 0 or not (ROOT / MEDIA_TASK_PENDING).is_file():
            return result
        print('[MÍDIA] Executando a tarefa agendada em Options; o RetroFE reabre ao terminar.', file=sys.stderr, flush=True)
        task = subprocess.run([sys.executable, str(ROOT / 'Tools/launchers/retrofe/media-tasks.py'), 'consume-pending'])
        if (ROOT / MEDIA_TASK_PENDING).exists():
            print('[MÍDIA] O pedido não foi consumido; sessão encerrada sem relançar para evitar repetição. Confira media-task-pending.json.', file=sys.stderr, flush=True)
            return task.returncode or 1
        if task.returncode:
            print(f'[MÍDIA] A tarefa terminou com código {task.returncode}; reabrindo o RetroFE.', file=sys.stderr, flush=True)


def supersede_runtime_plans(new_path):
    """A new runtime plan re-evaluates every collection; older pending plans are kept as superseded records."""
    superseded=[]
    for old in sorted((ROOT/'Manifests').glob('collection-media-runtime-*.json')):
        if old==new_path or old.name>=new_path.name:continue
        plan=load_json(old)
        if plan.get('status')!='prepared' or plan.get('operation')!='retrofe_collection_media_path_migration':continue
        plan.update(status='superseded',superseded_by=new_path.name,superseded_at=datetime.now().astimezone().isoformat(timespec='seconds'))
        save_json_atomic(old,plan)
        mutation_event(old,'plan_superseded',superseded_by=new_path.name)
        superseded.append(old.name)
    return superseded


def runtime_command(command,media_map,path=None):
    if command=='runtime-plan':
        plan=runtime_plan(media_map)
        path=path or ROOT/'Manifests'/('collection-media-runtime-'+datetime.now().strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.json')
        if path.exists():raise FileExistsError(path)
        save_json_atomic(path,plan)
        return {'manifest':str(path),'summary':plan['runtime_media']['summary'],'superseded_plans':supersede_runtime_plans(path)}
    if command=='check-runtime':
        plan=runtime_plan(media_map)
        return runtime_readiness(plan)
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    import romtool,media_session
    def ensure_locked():
        if retrofe_processes():raise RuntimeError('Feche o RetroFE normalmente; preflight não altera sessão ativa')
        plan=runtime_plan(media_map)
        readiness=runtime_readiness(plan)
        if not readiness['launch_ready']:
            write_runtime_report(plan)
            runtime_records(plan,romtool)
            raise RuntimeError('Validação de caminhos bloqueou a abertura: '+json.dumps(readiness['blocking_errors'],ensure_ascii=False)+'; relatório: '+readiness['audit'])
        if not plan['files'] and not plan['links']:
            write_runtime_report(plan)
            runtime_records(plan,romtool)
            return {**readiness,'changes':0}
        path=ROOT/'Manifests'/('collection-media-runtime-'+datetime.now().strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.json')
        save_json_atomic(path,plan)
        apply_plan(path,plan)
        verification=verify_plan(path,plan,media_map)
        if verification['failed_files']:raise RuntimeError(f'Validação falhou; consultar {path}')
        current=runtime_plan(media_map)
        write_runtime_report(current)
        runtime_records(current,romtool,path)
        from types import SimpleNamespace
        artifacts=[str(p.relative_to(PROJECT_ROOT)) for p in [path,AUDIT_JSON,romtool.OUTPUT_ROOT/'catalogo-arquivos.csv',romtool.OUTPUT_ROOT/'estado-final-verificacao.json',romtool.OUTPUT_ROOT/'pendencias-integridade-dependencias.csv',*[ROOT/item['path'] for item in plan['files']]]]
        record=romtool.command_record_artifacts(SimpleNamespace(change_type='retrofe-runtime-media-preflight',reason='Preencher apontamentos de mídia e aliases relativos por basename físico exato; sem cópia ou alteração de ROM/mídia.',artifacts_json=json.dumps(artifacts)))
        return {**runtime_readiness(current),'changes':len(plan['files'])+len(plan['links']),'manifest':str(path),'artifact_operation_id':record['operation_id']}
    inherited = validated_session_fd(runtime_lock_path())
    if inherited is not None:
        # Seed only this process's existing nested-lock mechanism. The parent
        # supervisor owns the descriptor for the whole frontend session.
        if getattr(media_session._LOCAL, 'depth', 0):
            raise RuntimeError('Contexto de lock já ativo no preflight')
        media_session._LOCAL.depth, media_session._LOCAL.fd = 1, inherited
        try: return ensure_locked()
        finally: media_session._LOCAL.depth = 0
    deadline=time.monotonic()+20
    while True:
        try:
            with media_session.write_lock(vars(romtool)):return ensure_locked()
        except media_session.MediaBusy:
            if time.monotonic()>=deadline:raise
            time.sleep(.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    launch_parser = subparsers.add_parser('launch-runtime', help='aguarda a gravação atual e reserva mídia durante o launcher canônico')
    launch_parser.add_argument('launcher_arguments', nargs=argparse.REMAINDER)
    subparsers.add_parser("audit", help="audita apontamentos, separadores de mídia e arte de plataforma")
    subparsers.add_parser('ensure-runtime',help='preflight portátil e idempotente de mídia antes de abrir o RetroFE')
    subparsers.add_parser('check-runtime',help='verifica configuração de mídia sem gravar')
    runtime_parser=subparsers.add_parser('runtime-plan',help='prepara plano revisável de apontamentos e aliases por título exato')
    runtime_parser.add_argument('--manifest',type=Path)
    entries_parser = subparsers.add_parser("entries-plan", help="projeta entradas CUE/GDI/M3U e exclui dependências dos menus e scraping")
    entries_parser.add_argument("--system-id", required=True)
    entries_parser.add_argument("--manifest", type=Path, required=True)
    consumer_parser = subparsers.add_parser("consumer-plan", help="projeta fontes e mídia canônicas em consumidores com adaptador explícito")
    consumer_parser.add_argument("--frontend", choices=("pegasus",), required=True)
    consumer_parser.add_argument("--manifest", type=Path, required=True)
    plan_parser = subparsers.add_parser("plan", help="prepara migração reversível de paths e separadores de mídia")
    plan_parser.add_argument("--manifest", type=Path, help="destino do manifesto; padrão Manifests/ com timestamp")
    apply_parser = subparsers.add_parser("apply", help="aplica plano conferido; exige --apply")
    apply_parser.add_argument("--manifest", type=Path, required=True)
    apply_parser.add_argument("--apply", action="store_true", help="confirma a gravação dos settings.conf")
    verify_parser = subparsers.add_parser("verify", help="confere configuração aplicada e reaudita caminhos")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    discard_parser = subparsers.add_parser("discard", help="retira plano pendente; preserva o manifesto; exige --apply")
    discard_parser.add_argument("--manifest", type=Path, required=True)
    discard_parser.add_argument("--reason", required=True)
    discard_parser.add_argument("--apply", action="store_true", help="confirma o descarte")
    rollback_parser = subparsers.add_parser("rollback", help="restaura settings.conf do manifesto; exige --rollback")
    rollback_parser.add_argument("--manifest", type=Path, required=True)
    rollback_parser.add_argument("--rollback", action="store_true", help="confirma a restauração")
    args = parser.parse_args()

    try:
        if args.command == 'launch-runtime':
            return launch_runtime(args.launcher_arguments)
        media_map = load_json(MEDIA_MAP_PATH)
        if args.command == "entries-plan":
            result = make_launch_entries_plan(args.manifest, args.system_id)
            print(json.dumps({"manifest": str(args.manifest), "status": result["status"], "summary": result["summary"]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "consumer-plan":
            result = make_pegasus_plan(args.manifest)
            print(json.dumps({"manifest": str(args.manifest), "status": result["status"], "summary": result["summary"]}, ensure_ascii=False, indent=2))
            return 0
        if args.command in {'ensure-runtime','check-runtime','runtime-plan'}:
            result=runtime_command(args.command,media_map,getattr(args,'manifest',None))
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result.get('launch_ready',True) else 1
        if args.command == "audit":
            print_summary(create_audit(media_map, write_reports=True))
            return 0
        if args.command == "plan":
            manifest_path = args.manifest or default_manifest_path()
            manifest = make_plan(manifest_path, media_map)
            print(json.dumps({
                "manifest": str(manifest_path),
                "status": manifest["status"],
                "summary": manifest["summary"],
                "unresolved_launchbox_paths": len(manifest["expected_unresolved_items"]),
                "planned_files": len(manifest["files"]),
                "planned_path_changes": sum(len(item["replacements"]) for item in manifest["files"]),
            }, ensure_ascii=False, indent=2))
            return 0
        manifest_path = args.manifest.resolve()
        manifest = read_manifest(manifest_path)
        if args.command == "apply":
            if not args.apply:
                raise ValueError("Use --apply somente depois de revisar o manifesto.")
            sys.path.insert(0,str(Path(__file__).resolve().parent))
            import romtool,media_session
            with media_session.write_lock(vars(romtool)):apply_plan(manifest_path,manifest)
            print(f"Aplicado: {manifest_path}")
            return 0
        if args.command == "discard":
            if not args.apply or not args.reason.strip():
                raise ValueError("Descarte exige --apply e motivo.")
            if manifest.get("status") != "prepared":
                raise ValueError(f"Manifesto não está pendente: {manifest.get('status')}")
            manifest.update(status="discarded", discard_reason=args.reason.strip(),
                            discarded_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            save_json_atomic(manifest_path, manifest)
            mutation_event(manifest_path, "plan_discarded", reason=args.reason.strip())
            print(json.dumps({"manifest": str(manifest_path), "status": "discarded"}, ensure_ascii=False))
            return 0
        if args.command == "verify":
            result = verify_plan(manifest_path, manifest, media_map)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["failed_files"] == 0 else 2
        if args.command == "rollback":
            if not args.rollback:
                raise ValueError("Use --rollback somente depois de revisar as pré-condições.")
            if manifest.get('mode')=='runtime':
                sys.path.insert(0,str(Path(__file__).resolve().parent))
                import romtool,media_session
                from types import SimpleNamespace
                with media_session.write_lock(vars(romtool)):
                    rollback_plan(manifest_path,manifest)
                    runtime_catalog(manifest,romtool,manifest_path)
                    state_path=romtool.OUTPUT_ROOT/'estado-final-verificacao.json'
                    state=load_json(state_path)
                    state['retrofe_runtime_media']={**state.get('retrofe_runtime_media',{}),'status':'rolled_back','as_of':romtool.now(),'rollback_manifest':relative_to_root(manifest_path),'audit_snapshot_is_current':False}
                    save_json_atomic(state_path,state)
                    paths=[manifest_path,state_path,romtool.OUTPUT_ROOT/'catalogo-arquivos.csv',*[ROOT/item['path'] for item in manifest['files']]]
                    romtool.command_record_artifacts(SimpleNamespace(change_type='retrofe-runtime-media-rollback',reason='Restaurar settings e remover somente aliases criados; payloads preservados e catálogo/estado atualizados.',artifacts_json=json.dumps([str(p.relative_to(PROJECT_ROOT)) for p in paths])))
            else:
                sys.path.insert(0,str(Path(__file__).resolve().parent))
                import romtool,media_session
                with media_session.write_lock(vars(romtool)):rollback_plan(manifest_path,manifest)
            print(f"Rollback aplicado: {manifest_path}")
            return 0
    except Exception as error:
        if args.command == 'launch-runtime' and shutil.which('kdialog') and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            subprocess.run(['kdialog', '--error', str(error), '--title', 'RetroFE — coordenação de mídia'], check=False)
        print(f"ERRO: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
