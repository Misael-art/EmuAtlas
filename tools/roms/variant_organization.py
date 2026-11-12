"""Plan and reversibly place DAT-confirmed variants in one-level platform folders."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

try:
    from . import variant_metadata
except ImportError:
    import variant_metadata


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_operation(operation_id: str, expected_kind: str, api: dict) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", operation_id):
        raise ValueError(f"ID de operação inválido: {operation_id!r}")
    path = Path(api["operation_dir"](operation_id)) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("operation_id") != operation_id or manifest.get("kind") != expected_kind:
        raise ValueError(f"Operação {operation_id} não é {expected_kind}")
    return manifest


def _safe_relative_path(value: str, system_id: str, *, review_origin: bool = False) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Caminho de inventário não é relativo e seguro: {value}")
    if len(path.parts) == 3 and path.parts[:2] == ("roms", system_id):
        return Path(*path.parts)
    if review_origin and len(path.parts) == 4 and path.parts[:3] == ("mods", "pending", system_id):
        return Path(*path.parts)
    raise ValueError(f"Candidato não está diretamente em roms/{system_id}: {value}")


def _placement_parent(relative: Path, system_id: str) -> Path:
    """Review files may be read from mods/pending, but they are placed under roms/<id>."""
    if relative.parts[:2] == ("mods", "pending"):
        return Path("roms") / system_id
    return relative.parent


def _operation_path_key(value: str, project_root: Path) -> str:
    """Normalize absolute/relative operation paths to a safe project-relative key."""
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.resolve(strict=False).relative_to(project_root.resolve())
        except ValueError:
            return ""
    if not path.parts or path.is_absolute() or ".." in path.parts:
        return ""
    return path.as_posix()


def _profile_extensions(system_id: str, project_root: Path) -> set[str]:
    path = project_root / "frontend/RetroFE/Config/profiles/host-linux.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    matches = [item for item in profile.get("collections", {}).values()
               if item.get("rom_subdir") == system_id]
    if len(matches) != 1:
        raise ValueError(f"Perfil host deve declarar exatamente uma coleção para {system_id}")
    raw = matches[0].get("extensions", "")
    values = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    return {str(value).strip().casefold().lstrip(".") for value in values if str(value).strip()}


def _dat_rom_index(dat_path: Path) -> dict[tuple[str, str], list[dict]]:
    root = ET.parse(dat_path).getroot()
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for game in root.iter():
        if game.tag.rsplit("}", 1)[-1] not in {"game", "machine"}:
            continue
        for rom in game:
            if rom.tag.rsplit("}", 1)[-1] != "rom":
                continue
            name = str(rom.get("name", ""))
            size = str(rom.get("size", ""))
            if not name or not size.isdecimal():
                continue
            index[(name.casefold(), size)].append({
                "game_name": str(game.get("name", "")),
                "rom_name": name,
                "size_bytes": int(size),
                "crc32": str(rom.get("crc", "")).casefold(),
                "md5": str(rom.get("md5", "")).casefold(),
                "sha1": str(rom.get("sha1", "")).casefold(),
                "sha256": str(rom.get("sha256", "")).casefold(),
            })
    return index


def _translation_base_candidates(member_name: str, size: int, index: dict) -> tuple[list[dict], str]:
    """Resolve conservative legacy DAT presentation changes, never fuzzy titles."""
    member = PurePosixPath(member_name)
    stem = re.sub(r"\s+\(Translated\s+[^()]+\)$", "", member.stem, flags=re.I).strip()
    exact = index.get(((stem+member.suffix).casefold(), str(size)), [])
    if exact: return exact, 'exact_member_name_and_size'
    countries={'usa','europe','brazil','japan','korea','taiwan','world','australia','china','asia','canada','france','germany','spain','italy','sweden','united kingdom'}
    def identity(value):
        title=re.split(r'\s*\(',Path(value).stem,maxsplit=1)[0]
        groups=re.findall(r'\(([^()]*)\)',Path(value).stem)
        regions=set();specific=[]
        for group in groups:
            tokens={v.strip().casefold() for v in group.split(',')}
            if tokens and tokens<=countries: regions|=tokens
            elif re.fullmatch(r'[a-z]{2}(?:,[a-z]{2})*',group,re.I):
                # New DAT language annotations may be absent in old names.
                specific.append(('language',group.casefold()))
            else: specific.append(('edition',group.casefold()))
        return ' '.join(title.split()).casefold(),regions,specific
    title,regions,specific=identity(stem+member.suffix)
    editions=[x for x in specific if x[0]=='edition']
    languages=[x for x in specific if x[0]=='language']
    matches=[]
    for (name,logical_size),records in index.items():
        if logical_size!=str(size) or Path(name).suffix.casefold()!=member.suffix.casefold():continue
        other,other_regions,other_specific=identity(name)
        if title!=other or not regions or not regions<=other_regions:continue
        if editions!=[x for x in other_specific if x[0]=='edition']:continue
        if languages and languages!=[x for x in other_specific if x[0]=='language']:continue
        matches.extend(records)
    return matches,'legacy_exact_title_region_subset_and_size'


def _sms_copier_header_evidence(source: Path,member: str,row: dict) -> dict | None:
    size=int(row.get('payload_size_bytes',0))
    if Path(member).suffix.casefold()!='.sms' or size%16384!=512 or size>8388608:return None
    if source.suffix.casefold()=='.sms':data=source.read_bytes()
    else:
        result=subprocess.run(['7z','x','-so','-spd',str(source),member],capture_output=True,timeout=30)
        if result.returncode:return None
        data=result.stdout
    if len(data)!=size or _sha(data)!=row.get('payload_sha256'):return None
    positions=[p for p in (0x1ff0,0x3ff0,0x7ff0) if data[p+512:p+520]==b'TMR SEGA' and data[p:p+8]!=b'TMR SEGA']
    if not positions:return None
    return {'prefix_bytes':512,'signature':'TMR SEGA','signature_offsets':[p+512 for p in positions],'payload_sha256':_sha(data),'bytes_removed':0,'scope':'base-size candidate evidence only; copier prefix remains unchanged'}


def _retrofe_reference_owner(gamelist: Path, system_id: str,
                             project_root: Path) -> tuple[str, str]:
    """Resolve a RetroFE gamelist owner from its explicit list.path setting."""
    settings_path = gamelist.parent / "settings.conf"
    if not settings_path.is_file() or settings_path.is_symlink():
        return "unknown", "settings.conf ausente ou simbólico"
    values = []
    for line in settings_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip().casefold() == "list.path":
            values.append(value.strip().strip("\"'"))
    if len(values) != 1 or not values[0]:
        return "unknown", "settings.conf não contém exatamente um list.path"

    raw = values[0]
    raw = re.sub(r"%BASE_ITEM_PATH%", str(project_root / "roms"), raw,
                 flags=re.IGNORECASE)
    if "%" in raw:
        return "unknown", f"list.path contém variável não resolvida: {raw}"
    target = Path(raw).expanduser()
    if not target.is_absolute():
        # RetroFE resolves relative list.path values from its installation root.
        target = project_root / "frontend/RetroFE" / target
    target = target.resolve(strict=False)
    project = project_root.resolve(strict=False)
    roms_root = (project_root / "roms").resolve(strict=False)
    expected = (project_root / "roms" / system_id).resolve(strict=False)
    if target == expected:
        return "same_platform_root", str(target)
    if target.is_relative_to(roms_root):
        if target.parent == roms_root:
            return "other_rom_platform", str(target)
        return "other_rom_subdirectory", str(target)
    if target.is_relative_to(project):
        return "non_rom_collection", str(target)
    return "unknown", f"list.path aponta para fora da raiz do projeto: {target}"


def _path_refs_for_moves(move_map: dict[str, str], system_id: str,
                         project_root: Path, esde_home: Path,
                         source_paths: dict[str,str] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    retrofe_root = project_root / "frontend/RetroFE"
    retrofe_collections = retrofe_root / "collections"
    esde_system_root = esde_home / "gamelists" / system_id
    candidate_files: list[tuple[Path, str]] = []
    if retrofe_collections.is_dir():
        candidate_files.extend((path, "retrofe") for path in retrofe_collections.rglob("gamelist.xml")
                               if path.is_file() and not path.is_symlink())
    if esde_system_root.is_dir():
        candidate_files.extend((path, "esde") for path in esde_system_root.rglob("gamelist.xml")
                               if path.is_file() and not path.is_symlink())
    esde_lists_root = esde_home / 'gamelists'
    if esde_lists_root.is_dir():
        candidate_files.extend((path, 'esde_other_platform') for path in esde_lists_root.rglob('gamelist.xml')
                               if path.is_file() and not path.is_symlink()
                               and path.relative_to(esde_lists_root).parts[0] != system_id)
    local_gamelist = project_root / "roms" / system_id / "gamelist.xml"
    if local_gamelist.is_file() and not local_gamelist.is_symlink():
        candidate_files.append((local_gamelist, "platform_local"))
    updated: dict[Path, bytes] = {}
    originals: dict[Path, bytes] = {}
    blockers: list[dict] = []
    exclusions: list[dict] = []
    pattern = re.compile(rb"(<path(?:\s+[^>]*)?>)(.*?)(</path\s*>)", re.IGNORECASE | re.DOTALL)
    for gamelist, scope in sorted(set(candidate_files), key=lambda item: str(item[0]).casefold()):
        before = gamelist.read_bytes()
        try:
            parsed = ET.fromstring(before)
        except ET.ParseError as exc:
            for name in move_map:
                if name.encode("utf-8") in before:
                    blockers.append({"path": str(gamelist), "filename": name,
                                     "reason": f"XML inválido: {exc}"})
            continue
        parsed_paths = [node.text or "" for node in parsed.iter()
                        if node.tag.rsplit("}", 1)[-1].casefold() == "path"]
        parsed_by_name: dict[str, list[str]] = defaultdict(list)
        for raw in parsed_paths:
            normalized = raw.strip().replace("\\", "/")
            basename = PurePosixPath(normalized).name
            if basename in move_map:
                parsed_by_name[basename].append(normalized)
        if not parsed_by_name:
            continue
        if scope == 'esde_other_platform':
            owner = gamelist.relative_to(esde_lists_root).parts[0]
            for basename, raw_paths in parsed_by_name.items():
                safe = all(not raw.startswith('/') and not re.match(r'^[A-Za-z]:', raw)
                           and '..' not in PurePosixPath(raw).parts for raw in raw_paths)
                if safe:
                    exclusions.append({'path': str(gamelist), 'filename': basename,
                                       'reason': 'gamelist ES-DE relativa pertence a outra plataforma; preservar',
                                       'owner': str(project_root / 'roms' / owner)})
                else:
                    blockers.append({'path': str(gamelist), 'filename': basename,
                                     'reason': 'gamelist de outra plataforma contém caminho absoluto/ascendente; revisão necessária'})
            continue
        if scope == "retrofe":
            owner, owner_detail = _retrofe_reference_owner(gamelist, system_id, project_root)
            if owner != "same_platform_root":
                if owner in {"other_rom_platform", "other_rom_subdirectory", "non_rom_collection"}:
                    reason = {
                        "other_rom_platform": "gamelist pertence a outra plataforma; referência preservada",
                        "other_rom_subdirectory": "gamelist aponta para subdiretório diferente da raiz da plataforma; referência preservada",
                        "non_rom_collection": "gamelist pertence a coleção que não usa a árvore de ROMs; referência preservada",
                    }[owner]
                    exclusions.extend({"path": str(gamelist), "filename": basename,
                                       "reason": reason, "owner": owner_detail}
                                      for basename in sorted(parsed_by_name))
                else:
                    blockers.extend({"path": str(gamelist), "filename": basename,
                                     "reason": "proprietário RetroFE não resolvido",
                                     "detail": owner_detail}
                                    for basename in sorted(parsed_by_name))
                continue
        replacements: dict[str, str] = {}
        for basename, raw_paths in parsed_by_name.items():
            category_path = move_map[basename]
            for raw in raw_paths:
                if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
                    blockers.append({"path": str(gamelist), "filename": basename,
                                     "reference": raw, "reason": "referência absoluta precisa de regra explícita"})
                    continue
                parts = [part for part in raw.split("/") if part not in {"", "."}]
                if any(part == ".." for part in parts):
                    blockers.append({"path": str(gamelist), "filename": basename,
                                     "reference": raw, "reason": "referência com caminho ascendente não é portável"})
                    continue
                expected=(source_paths or {}).get(basename,basename).split('/')
                if parts not in (expected, ["roms",*expected]):
                    blockers.append({"path": str(gamelist), "filename": basename,
                                     "reference": raw, "reason": "prefixo de gamelist não conhecido"})
                    continue
                prefix = "./roms/" if parts[0] == "roms" else "./"
                replacements[raw] = prefix + category_path
        if blockers and any(item.get("path") == str(gamelist) for item in blockers):
            continue
        if not replacements:
            continue

        def replace_tag(match: re.Match[bytes]) -> bytes:
            value = html.unescape(match.group(2).decode("utf-8")).strip()
            replacement = replacements.get(value.replace("\\", "/"))
            if replacement is None:
                return match.group(0)
            encoded = html.escape(replacement, quote=False).encode("utf-8")
            return match.group(1) + encoded + match.group(3)

        after = pattern.sub(replace_tag, before)
        if after == before:
            blockers.append({"path": str(gamelist), "reason": "paths identificados não puderam ser reescritos"})
        else:
            updated[gamelist] = after
            originals[gamelist] = before
    files = [{"path": str(path), "before_bytes": originals[path], "after_bytes": updated[path],
              "sha256_before": _sha(originals[path]), "sha256_after": _sha(updated[path]),
              "size_before": len(originals[path]), "size_after": len(updated[path])}
             for path in updated]
    return files, blockers, exclusions


def _record_no_action_plan(system_id: str, title: str, inputs: dict,
                           considered: int, blockers: list[dict], api: dict,
                           *, organization_type: str = "folder_placement",
                           attestation: dict | None = None,
                           reference_exclusions: list[dict] | None = None) -> dict:
    operation_id = f"rom-variant-organize-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = Path(api["operation_dir"](operation_id))
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id,
        "kind": "rom_variant_organization", "organization_type": organization_type,
        "title": title, "created_at": api["now"](), "completed_at": api["now"](),
        "status": "no_eligible_candidates", "read_only": True, "system_id": system_id,
        **inputs, "candidate_paths_considered": considered, "planned_file_count": 0,
        "category_counts": {}, "blocked_candidate_count": len(blockers),
        "blockers": blockers, "items": [], "frontend_reference_updates": [],
        "excluded_frontend_references": reference_exclusions or [],
        "apply_available": False, "attestation": attestation,
    }
    api["atomic_json"](opdir / "manifest.json", manifest)
    api["append_event"](operation_id, "plan_created_no_eligible_candidates",
                        action="rom_variant_organization_no_action_plan",
                        status=manifest["status"],
                        details={"system_id": system_id, "candidate_paths_considered": considered,
                                 "blocker_count": len(blockers),
                                 "blocker_reasons": dict(Counter(str(row.get("reason", "unknown"))
                                                                  for row in blockers))})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": manifest["status"],
            "system_id": system_id, "planned_files": 0, "blocked_candidates": len(blockers),
            "manifest": str(opdir / "manifest.json")}


def plan(system_id: str, inventory_operation_id: str, dat_operation_id: str,
         verify_operation_id: str, hierarchy_operation_id: str, api: dict) -> dict:
    project_root: Path = api["PROJECT_ROOT"]
    inventory = _load_operation(inventory_operation_id, "rom_variant_inventory", api)
    dat = _load_operation(dat_operation_id, "dat_match", api)
    validation = _load_operation(verify_operation_id, "verify", api)
    hierarchy = _load_operation(hierarchy_operation_id, "retrofe_rom_hierarchy", api)
    if inventory.get("status") not in {"completed", "completed_with_blockers"}:
        raise ValueError("Inventário de variantes não está concluído")
    if dat.get("status") != "completed" or dat.get("evidence", {}).get("system_id") != system_id:
        raise ValueError("DAT match precisa estar concluído e corresponder à plataforma")
    if validation.get("status") not in {"completed", "completed_with_issues"}:
        raise ValueError("Validação de formato não está concluída")
    if hierarchy.get("system_id") != system_id or hierarchy.get("status") not in {"prepared", "completed_verified", "verified"}:
        raise ValueError("Plano de hierarquia precisa pertencer à plataforma")
    if inventory.get("hash_requested") is not True:
        raise ValueError("Inventário de variantes sem hash integral; não é elegível para organizar")

    profile_extensions = _profile_extensions(system_id, project_root)
    policy_path = Path(api.get("variant_policy_path", project_root / "tools/roms/config/variant-policy.json"))
    variant_policy = json.loads(policy_path.read_text(encoding="utf-8"))
    category_map = variant_policy.get("variant_directories", {})
    dat_by_path = {_operation_path_key(str(row.get("source_path", "")), project_root): row
                   for row in dat.get("items", [])}
    validation_by_path = {str(row.get("path", "")): row for row in validation.get("items", [])}
    hash_paths: dict[str, list[str]] = defaultdict(list)
    for row in inventory.get("items", []):
        digest = str(row.get("source_file_sha256", ""))
        if digest and row.get("hash_status") == "hashed":
            hash_paths[digest].append(str(row.get("path", "")))

    planned: list[dict] = []
    blockers: list[dict] = []
    considered = 0
    platform_root = project_root / "roms" / system_id
    for row in inventory.get("items", []):
        if row.get("system_id") != system_id or row.get("artifact_role") != "catalog_supported_content_candidate":
            continue
        kinds = row.get("name_evidence", {}).get("kind_candidates", [])
        if not kinds:
            continue
        considered += 1
        source_text = str(row.get("path", ""))
        try:
            relative = _safe_relative_path(source_text, system_id, review_origin=True)
        except ValueError as exc:
            blockers.append({"source": source_text, "reason": "not_top_level_candidate", "detail": str(exc)})
            continue
        source = project_root / relative
        match = dat_by_path.get(source_text)
        if len(kinds) != 1:
            blockers.append({"source": source_text, "reason": "multiple_category_candidates",
                             "categories": [str(kind.get("value", "")) for kind in kinds]})
            continue
        category = str(kinds[0].get("value", ""))
        template = str(category_map.get(category, ""))
        if not template or "{" in template or "/" in template or "\\" in template:
            blockers.append({"source": source_text, "reason": "category_has_no_safe_one_level_directory",
                             "category": category})
            continue
        if (not match or match.get("status") != "full_dat_match"
                or str(match.get("matching_dat_records")) != "1"):
            blockers.append({"source": source_text, "reason": "no_unique_full_dat_match"})
            continue
        if (str(match.get("archive_member_count")) != "1"
                or str(match.get("non_rom_member_count")) != "0"):
            blockers.append({"source": source_text, "reason": "archive_is_not_one_rom_only",
                             "archive_member_count": match.get("archive_member_count"),
                             "non_rom_member_count": match.get("non_rom_member_count")})
            continue
        member = PurePosixPath(str(match.get("payload_member", "")))
        if member.is_absolute() or len(member.parts) != 1 or any(x in {"", ".", ".."} for x in member.parts):
            blockers.append({"source": source_text, "reason": "rom_member_is_not_at_archive_root",
                             "member": str(member)})
            continue
        suggested = str(match.get("suggested_container_name", ""))
        if not suggested or Path(suggested).stem != source.name.rsplit(".", 1)[0]:
            blockers.append({"source": source_text, "reason": "container_basename_differs_from_dat",
                             "suggested_container_name": suggested})
            continue
        extension = source.suffix.casefold().lstrip(".")
        if extension not in profile_extensions or not row.get("extension_supported"):
            blockers.append({"source": source_text, "reason": "extension_not_supported_by_host_profile",
                             "extension": extension})
            continue
        check_path = str(source)
        validated = validation_by_path.get(check_path)
        if (not validated or validated.get("status") != "ok"
                or validated.get("sha256") != row.get("source_file_sha256")
                or str(validated.get("member_count")) != "1"
                or str(validated.get("encrypted_member_count")) not in {"0", ""}):
            blockers.append({"source": source_text, "reason": "format_validation_missing_or_changed"})
            continue
        duplicate_paths = sorted(set(path for path in hash_paths.get(str(row.get("source_file_sha256")), [])
                                     if path != source_text))
        if duplicate_paths:
            blockers.append({"source": source_text, "reason": "same_stored_bytes_in_another_candidate_path",
                             "other_paths": duplicate_paths})
            continue
        if source.is_symlink() or not source.is_file():
            blockers.append({"source": source_text, "reason": "source_missing_or_symlink"})
            continue
        if api["sha256_file"](source) != row.get("source_file_sha256"):
            blockers.append({"source": source_text, "reason": "source_hash_changed_since_inventory"})
            continue
        destination_relative = _placement_parent(relative, system_id) / template / source.name
        destination = project_root / destination_relative
        if destination.exists() or destination.is_symlink():
            blockers.append({"source": source_text, "reason": "destination_collision",
                             "destination": str(destination)})
            continue
        if not destination_relative.is_relative_to(Path("roms") / system_id):
            blockers.append({"source": source_text, "reason": "destination_escaped_platform_root"})
            continue
        planned.append({
            "system_id": system_id, "source": str(source), "source_relative": relative.as_posix(),
            "destination": str(destination), "destination_relative": destination_relative.as_posix(),
            "filename": source.name, "category": category, "category_directory": template,
            "size_bytes": int(row["size_bytes"]), "sha256": str(row["source_file_sha256"]),
            "dat_sha256": str(match["dat_sha256"]), "dat_rom_name": str(match["dat_rom_name"]),
            "dat_size_bytes": int(match["payload_size_bytes"]), "dat_crc32": str(match["payload_crc32"]),
            "dat_md5": str(match["payload_md5"]), "dat_sha1": str(match["payload_sha1"]),
            "dat_payload_sha256": str(match["payload_sha256"]), "dat_member": str(match["payload_member"]),
            "archive_member_count": 1, "non_rom_member_count": 0,
            "validation_operation_id": verify_operation_id,
            "validation_status": str(validated["status"]),
        })

    move_map = {row["filename"]: f"{row['category_directory']}/{row['filename']}" for row in planned}
    esde_home = Path(api.get("ESDE_HOME", Path(api["PROJECT_ROOT"]) / "frontend" / "ES-DE" / "UserData"))
    reference_files, reference_blockers, reference_exclusions = _path_refs_for_moves(
        move_map, system_id, project_root, esde_home)
    blockers.extend(reference_blockers)
    blocked_reference_names = {str(item.get("filename", "")) for item in reference_blockers}
    if blocked_reference_names:
        planned = [item for item in planned if item["filename"] not in blocked_reference_names]
        move_map = {row["filename"]: f"{row['category_directory']}/{row['filename']}" for row in planned}
        reference_files, remaining, reference_exclusions = _path_refs_for_moves(
            move_map, system_id, project_root, esde_home)
        blockers.extend(remaining)

    if not planned:
        return _record_no_action_plan(
            system_id, f"No eligible DAT-confirmed variants under {system_id}",
            {"inventory_operation_id": inventory_operation_id,
             "dat_match_operation_id": dat_operation_id,
             "validation_operation_id": verify_operation_id,
             "hierarchy_operation_id": hierarchy_operation_id},
            considered, blockers, api)
    operation_id = f"rom-variant-organize-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = Path(api["operation_dir"](operation_id))
    opdir.mkdir(parents=True, exist_ok=False)
    rollback_dir = opdir / "rollback" / "references"
    proposal_dir = opdir / "proposal" / "references"
    rollback_dir.mkdir(parents=True)
    proposal_dir.mkdir(parents=True)
    reference_records = []
    for index, item in enumerate(reference_files, 1):
        path = Path(item["path"])
        before_path = rollback_dir / f"{index:04d}-{path.name}.before"
        after_path = proposal_dir / f"{index:04d}-{path.name}.after"
        before_path.write_bytes(item.pop("before_bytes"))
        after_path.write_bytes(item.pop("after_bytes"))
        reference_records.append({**item, "rollback_snapshot": str(before_path),
                                  "proposed_file": str(after_path)})
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "rom_variant_organization",
        "organization_type": "folder_placement",
        "title": f"Place verified {system_id} release variants in one-level folders",
        "created_at": api["now"](), "status": "prepared", "system_id": system_id,
        "inventory_operation_id": inventory_operation_id, "dat_match_operation_id": dat_operation_id,
        "validation_operation_id": verify_operation_id, "hierarchy_operation_id": hierarchy_operation_id,
        "hierarchy_required_status": "verified", "candidate_paths_considered": considered,
        "planned_file_count": len(planned), "category_counts": dict(Counter(item["category"] for item in planned)),
        "blocked_candidate_count": len(blockers), "blockers": blockers,
        "items": planned, "frontend_reference_updates": reference_records,
        "excluded_frontend_references": reference_exclusions,
        "policy": {"one_level_only": True, "no_overwrite": True, "no_extraction": True,
                   "no_permanent_delete": True, "same_filesystem_rename_only": True,
                   "requires_active_hierarchy_verification": True,
                   "requires_frontends_closed_for_apply_and_rollback": True},
    }
    api["atomic_json"](opdir / "manifest.json", manifest)
    api["append_event"](operation_id, "plan_created", action="rom_variant_organization_plan",
                        status="prepared", details={"system_id": system_id, "file_count": len(planned),
                                                     "category_counts": manifest["category_counts"],
                                                     "reference_file_count": len(reference_records),
                                                     "excluded_reference_count": len(reference_exclusions),
                                                     "blocked_candidate_count": len(blockers)})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": "prepared", "system_id": system_id,
            "planned_files": len(planned), "category_counts": manifest["category_counts"],
            "reference_files": len(reference_records), "blocked_candidates": len(blockers),
            "manifest": str(opdir / "manifest.json")}


def plan_user_attested_translations(system_id: str, inventory_operation_id: str,
                                    dat_operation_id: str, verify_operation_id: str,
                                    hierarchy_operation_id: str, language: str,
                                    attestation: str, api: dict) -> dict:
    """Plan one-level placement for complete translations explicitly attested by the user.

    The tool requires an intact raw ROM or single-ROM archive and a unique
    DAT base candidate by conservative title, region, edition and logical size. It deliberately does
    not call a modified payload a full DAT match or rename it to the base title.
    """
    if not attestation.strip():
        raise ValueError("Organização de tradução exige uma atestação registrada")
    project_root: Path = api["PROJECT_ROOT"]
    inventory = _load_operation(inventory_operation_id, "rom_variant_inventory", api)
    dat = _load_operation(dat_operation_id, "dat_match", api)
    validation = _load_operation(verify_operation_id, "verify", api)
    hierarchy = _load_operation(hierarchy_operation_id, "retrofe_rom_hierarchy", api)
    if inventory.get("status") not in {"completed", "completed_with_blockers"} or inventory.get("hash_requested") is not True:
        raise ValueError("Planejamento exige inventário completo de variantes com hashes integrais")
    if dat.get("status") != "completed" or dat.get("evidence", {}).get("system_id") != system_id:
        raise ValueError("DAT match precisa estar concluído e corresponder à plataforma")
    if validation.get("status") not in {"completed", "completed_with_issues"}:
        raise ValueError("Validação de formato não está concluída")
    if hierarchy.get("system_id") != system_id or hierarchy.get("status") not in {
            "prepared", "completed_verified", "verified"}:
        raise ValueError("Plano de hierarquia RetroFE inválido para a plataforma")

    language_tag = language.strip()
    directory = variant_metadata.variant_directory("translation", language_tag)
    dat_evidence = dat.get("evidence", {})
    dat_path = Path(str(dat_evidence.get("dat_path", "")))
    dat_sha256 = str(dat_evidence.get("dat_sha256", ""))
    if (not dat_path.is_file() or dat_path.is_symlink()
            or not dat_path.resolve().is_relative_to(project_root.resolve())
            or api["sha256_file"](dat_path) != dat_sha256):
        raise ValueError("DAT de base ausente, fora de work/ do projeto, simbólico ou alterado")
    dat_index = _dat_rom_index(dat_path)

    catalog_path = project_root / "frontend/RetroFE/Config/platform-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    systems = [row for row in catalog.get("systems", [])
               if row.get("id") == system_id and row.get("kind") == "platform"]
    if len(systems) != 1:
        raise ValueError(f"Catálogo não contém plataforma única para {system_id}")
    payload_extensions = {("." + str(ext).lstrip(".")).casefold()
                          for ext in systems[0].get("extensions", [])}
    archive_extensions = {".zip", ".7z", ".rar", ".lha", ".lzh", ".tar", ".gz", ".bz2", ".xz"}
    payload_extensions -= archive_extensions

    dat_by_path = {_operation_path_key(str(row.get("source_path", "")), project_root): row
                   for row in dat.get("items", [])}
    validation_by_path = {str(row.get("path", "")): row for row in validation.get("items", [])}
    profile_extensions = _profile_extensions(system_id, project_root)
    hash_paths: dict[str, list[str]] = defaultdict(list)
    for row in inventory.get("items", []):
        digest = str(row.get("source_file_sha256", ""))
        if digest and row.get("hash_status") in {"hashed", "reused_prior_inventory"}:
            hash_paths[digest].append(str(row.get("path", "")))

    planned: list[dict] = []
    blockers: list[dict] = []
    considered = 0
    already_placed = []
    for row in inventory.get("items", []):
        if row.get("system_id") != system_id or row.get("artifact_role") != "catalog_supported_content_candidate":
            continue
        kinds = row.get("name_evidence", {}).get("kind_candidates", [])
        if not any(str(item.get("value", "")).casefold() == "translation" for item in kinds):
            continue
        considered += 1
        source_text = str(row.get("path", ""))
        kind_values={str(item.get('value','')).casefold() for item in kinds}
        expected_directory=variant_metadata.variant_directory('translation_and_hack' if 'hack' in kind_values else 'translation',language_tag)
        path_parts=PurePosixPath(source_text).parts
        if len(path_parts)==4 and path_parts[:3]==('roms',system_id,expected_directory):
            already_placed.append({'source':source_text,'status':'already_in_requested_directory','identity_not_reclassified':True})
            continue
        try:
            relative = _safe_relative_path(source_text, system_id, review_origin=True)
        except ValueError as exc:
            blockers.append({"source": source_text, "reason": "not_top_level_candidate", "detail": str(exc)})
            continue
        source = project_root / relative
        kind_values={str(item.get('value','')).casefold() for item in kinds}
        if kind_values not in ({'translation'},{'translation','hack'}):
            blockers.append({"source": source_text, "reason": "multiple_variant_categories",
                             "categories": [str(item.get("value", "")) for item in kinds]})
            continue
        language_candidates = {str(item.get("tag_candidate", "")).casefold(): item
                               for item in row.get("name_evidence", {}).get("language_candidates", [])
                               if item.get("tag_candidate")}
        matching_languages = [value for value in language_candidates
                              if value == language_tag.casefold()]
        if len(language_candidates) != 1 or len(matching_languages) != 1:
            blockers.append({"source": source_text, "reason": "language_candidate_not_unique_or_different",
                             "language_candidates": sorted(language_candidates),
                             "requested_language": language_tag})
            continue
        if (row.get("hash_status") not in {"hashed", "reused_prior_inventory"}
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("source_file_sha256", "")))):
            blockers.append({"source": source_text, "reason": "stored_file_sha256_missing"})
            continue
        if source.is_symlink() or not source.is_file():
            blockers.append({"source": source_text, "reason": "source_missing_or_symlink"})
            continue
        source_stat = source.stat()
        expected_stat = {"device": row.get("device"), "inode": row.get("inode"),
                         "size_bytes": row.get("size_bytes"), "mtime_ns": row.get("mtime_ns"),
                         "mode": row.get("mode")}
        actual_stat = {"device": source_stat.st_dev, "inode": source_stat.st_ino,
                       "size_bytes": source_stat.st_size, "mtime_ns": source_stat.st_mtime_ns,
                       "mode": source_stat.st_mode & 0o777}
        if actual_stat != expected_stat:
            blockers.append({"source": source_text, "reason": "source_snapshot_changed_since_inventory"})
            continue
        if api["sha256_file"](source) != row["source_file_sha256"]:
            blockers.append({"source": source_text, "reason": "source_hash_changed_since_inventory"})
            continue

        dat_row = dat_by_path.get(source_text)
        if not dat_row or dat_row.get("status") != "no_match_retain":
            blockers.append({"source": source_text, "reason": "not_a_modified_dat_miss"})
            continue
        raw_payload=source.suffix.casefold() in payload_extensions
        if not raw_payload and (str(dat_row.get("archive_member_count")) != "1"
                or str(dat_row.get("non_rom_member_count")) != "0"):
            blockers.append({"source": source_text, "reason": "archive_is_not_one_rom_only",
                             "archive_member_count": dat_row.get("archive_member_count"),
                             "non_rom_member_count": dat_row.get("non_rom_member_count")})
            continue
        member = PurePosixPath(source.name if raw_payload else str(dat_row.get("payload_member", "")))
        if (member.is_absolute() or len(member.parts) != 1
                or any(part in {"", ".", ".."} for part in member.parts)
                or member.suffix.casefold() not in payload_extensions):
            blockers.append({"source": source_text, "reason": "payload_not_a_supported_root_rom_member",
                             "member": str(member), "payload_extensions": sorted(payload_extensions)})
            continue

        member_stem = re.sub(r"\s+\(Translated\s+[^()]+\)$", "", member.stem, flags=re.IGNORECASE).strip()
        base_member_name = member_stem + member.suffix
        base_matches,base_method = _translation_base_candidates(str(member),int(dat_row.get('payload_size_bytes',0)),dat_index)
        prefix_evidence=None
        if not base_matches:
            prefix_evidence=_sms_copier_header_evidence(source,str(member),dat_row)
            if prefix_evidence:
                base_matches,base_method=_translation_base_candidates(str(member),int(dat_row['payload_size_bytes'])-512,dat_index)
                base_method+='; retained_512_byte_sms_copier_prefix'
        if len(base_matches) != 1:
            blockers.append({"source": source_text, "reason": "base_identity_not_unique_by_dat_member_name_and_size",
                             "payload_member": str(member), "normalized_base_member": base_member_name,
                             "payload_size_bytes": dat_row.get("payload_size_bytes"),
                             "matching_dat_records": len(base_matches)})
            continue
        extension = source.suffix.casefold().lstrip(".")
        if extension not in profile_extensions or not row.get("extension_supported"):
            blockers.append({"source": source_text, "reason": "container_extension_not_supported_by_host_profile",
                             "extension": extension})
            continue
        validation_row = validation_by_path.get(str(source))
        validation_members = None if not validation_row else validation_row.get("member_count")
        validation_encrypted = None if not validation_row else validation_row.get("encrypted_member_count")
        acceptable_validation={'ok','not_validated_by_format_specific_tool'} if raw_payload else {'ok'}
        if (not validation_row or validation_row.get("status") not in acceptable_validation
                or validation_row.get("sha256") != row["source_file_sha256"]
                or validation_members not in {None, "", 1, "1"}
                or validation_encrypted not in {None, "", 0, "0"}):
            blockers.append({"source": source_text, "reason": "format_validation_missing_or_changed"})
            continue
        duplicate_paths = sorted(set(path for path in hash_paths.get(str(row["source_file_sha256"]), [])
                                     if path != source_text))
        if duplicate_paths:
            blockers.append({"source": source_text, "reason": "same_stored_bytes_in_another_candidate_path",
                             "other_paths": duplicate_paths})
            continue
        item_directory=variant_metadata.variant_directory('translation_and_hack' if 'hack' in kind_values else 'translation',language_tag)
        destination_relative = _placement_parent(relative, system_id) / item_directory / source.name
        destination = project_root / destination_relative
        if destination.exists() or destination.is_symlink():
            blockers.append({"source": source_text, "reason": "destination_collision",
                             "destination": str(destination)})
            continue
        if not destination_relative.is_relative_to(Path("roms") / system_id):
            blockers.append({"source": source_text, "reason": "destination_escaped_platform_root"})
            continue
        base = base_matches[0]
        planned.append({
            "system_id": system_id, "source": str(source), "source_relative": relative.as_posix(),
            "destination": str(destination), "destination_relative": destination_relative.as_posix(),
            "filename": source.name, "category": "translation_and_hack" if 'hack' in kind_values else "translation", "category_directory": item_directory,
            "language_tag": language_tag, "classification_basis": "user_attestation_and_unique_dat_member_name_size",
            "base_identity_evidence": {"dat_sha256": dat_sha256, "rom_name": base["rom_name"],
                                       "game_name": base["game_name"], "size_bytes": base["size_bytes"],
                                       "crc32": base["crc32"], "md5": base["md5"],
                                       "sha1": base["sha1"], "sha256": base["sha256"],
                                       "member_name_in_archive": str(member),
                                       "normalized_member_name": base_member_name,
                                       "candidate_method": base_method,
                                       "copier_prefix_evidence": prefix_evidence,
                                       "identity_limit": "patched payload does not match the DAT hash tuple"},
            "size_bytes": int(row["size_bytes"]), "sha256": str(row["source_file_sha256"]),
            "archive_member_count": 0 if raw_payload else 1, "non_rom_member_count": 0,
            "payload_member": str(member), "payload_size_bytes": int(dat_row["payload_size_bytes"]),
            "payload_crc32": str(dat_row.get("payload_crc32", "")),
            "payload_md5": str(dat_row.get("payload_md5", "")),
            "payload_sha1": str(dat_row.get("payload_sha1", "")),
            "validation_operation_id": verify_operation_id, "validation_status": validation_row['status'],
        })

    move_map = {item["filename"]: f"{item['category_directory']}/{item['filename']}" for item in planned}
    esde_home = Path(api.get("ESDE_HOME", Path(api["PROJECT_ROOT"]) / "frontend" / "ES-DE" / "UserData"))
    reference_files, reference_blockers, reference_exclusions = _path_refs_for_moves(
        move_map, system_id, project_root, esde_home)
    blockers.extend(reference_blockers)
    blocked_names = {str(item.get("filename", "")) for item in reference_blockers}
    if blocked_names:
        planned = [item for item in planned if item["filename"] not in blocked_names]
        move_map = {item["filename"]: f"{item['category_directory']}/{item['filename']}" for item in planned}
        reference_files, remaining, reference_exclusions = _path_refs_for_moves(
            move_map, system_id, project_root, esde_home)
        blockers.extend(remaining)
    if not planned:
        return _record_no_action_plan(
            system_id, f"No eligible {language_tag} translations under {system_id}",
            {"inventory_operation_id": inventory_operation_id,
             "dat_match_operation_id": dat_operation_id,
             "validation_operation_id": verify_operation_id,
             "hierarchy_operation_id": hierarchy_operation_id,
             "language_tag": language_tag, "already_placed": already_placed, "dat_source": {"path": str(dat_path), "sha256": dat_sha256}},
            considered, blockers, api,
            organization_type="user_attested_translation_placement",
            attestation={"source": "explicit_project_owner_instruction", "text": attestation.strip(),
                         "language_tag": language_tag},
            reference_exclusions=reference_exclusions)

    operation_id = f"rom-variant-organize-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = Path(api["operation_dir"](operation_id))
    opdir.mkdir(parents=True, exist_ok=False)
    rollback_dir = opdir / "rollback" / "references"
    proposal_dir = opdir / "proposal" / "references"
    rollback_dir.mkdir(parents=True)
    proposal_dir.mkdir(parents=True)
    reference_records = []
    for index, item in enumerate(reference_files, 1):
        path = Path(item.pop("path"))
        before_path = rollback_dir / f"{index:04d}-{path.name}.before"
        after_path = proposal_dir / f"{index:04d}-{path.name}.after"
        before_path.write_bytes(item.pop("before_bytes"))
        after_path.write_bytes(item.pop("after_bytes"))
        reference_records.append({**item, "path": str(path),
                                  "rollback_snapshot": str(before_path), "proposed_file": str(after_path)})
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "rom_variant_organization",
        "organization_type": "user_attested_translation_placement",
        "title": f"Place verified {language_tag} translations under {system_id}",
        "created_at": api["now"](), "status": "prepared", "system_id": system_id,
        "inventory_operation_id": inventory_operation_id, "dat_match_operation_id": dat_operation_id,
        "validation_operation_id": verify_operation_id, "hierarchy_operation_id": hierarchy_operation_id,
        "hierarchy_required_status": "verified", "candidate_paths_considered": considered, "already_placed": already_placed,
        "planned_file_count": len(planned),
        "category_counts": dict(Counter(item["category_directory"] for item in planned)),
        "blocked_candidate_count": len(blockers), "blockers": blockers, "items": planned,
        "frontend_reference_updates": reference_records,
        "excluded_frontend_references": reference_exclusions,
        "attestation": {"source": "explicit_project_owner_instruction", "text": attestation.strip(),
                        "language_tag": language_tag,
                        "scope": "only single-ROM raw/archive files with one explicit language candidate and a unique conservative DAT base candidate; copier prefix retained"},
        "dat_source": {"path": str(dat_path), "sha256": dat_sha256},
        "limits": ["The modified payload does not match the No-Intro CRC/MD5/SHA-1 tuple and is not renamed to a No-Intro basename.",
                   "DAT member-name plus logical-size agreement identifies the base candidate but does not prove byte identity.",
                   "Container integrity is distinct from a successful emulator launch; the operation does not claim every game boots."],
        "policy": {"one_level_only": True, "no_overwrite": True, "no_extraction": True,
                   "no_permanent_delete": True, "same_filesystem_rename_only": True,
                   "requires_verified_hierarchy": True, "requires_frontends_closed_for_apply_and_rollback": True,
                   "keeps_original_filename_and_translation_marker": True},
    }
    api["atomic_json"](opdir / "manifest.json", manifest)
    api["append_event"](operation_id, "translation_placement_plan_created",
                        action="rom_variant_user_attested_translation_plan", status="prepared",
                        details={"system_id": system_id, "language_tag": language_tag,
                                 "file_count": len(planned), "category_counts": manifest["category_counts"],
                                 "reference_file_count": len(reference_records),
                                 "excluded_reference_count": len(reference_exclusions),
                                 "blocked_candidate_count": len(blockers), "dat_sha256": dat_sha256})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": "prepared", "system_id": system_id,
            "language_tag": language_tag, "planned_files": len(planned),
            "category_counts": manifest["category_counts"],
            "reference_files": len(reference_records), "blocked_candidates": len(blockers),
            "manifest": str(opdir / "manifest.json")}


def _validate_story_file(path: Path) -> dict:
    data = path.read_bytes()
    if not data:
        raise ValueError(f"História vazia: {path}")
    decoded = None
    encoding = ""
    for candidate in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            decoded = data.decode(candidate)
            encoding = candidate
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if decoded is None:
        raise ValueError(f"Texto não decodificável: {path}")
    if any(ord(char) < 32 and char not in "\r\n\t\f" for char in decoded):
        raise ValueError(f"Texto contém controles binários: {path}")
    return {"sha256": _sha(data), "size_bytes": len(data), "text_encoding": encoding}


def plan_dat_name_alignment(system_id: str, inventory_operation_id: str, dat_operation_id: str,
                            verify_operation_id: str, api: dict) -> dict:
    """Prepare No-Intro basename corrections with exact media and gamelist alignment."""
    project_root: Path = api["PROJECT_ROOT"]
    inventory = _load_operation(inventory_operation_id, "rom_variant_inventory", api)
    dat = _load_operation(dat_operation_id, "dat_match", api)
    validation = _load_operation(verify_operation_id, "verify", api)
    if inventory.get("status") not in {"completed", "completed_with_blockers"}:
        raise ValueError("Inventário de variantes não está concluído")
    if dat.get("status") != "completed" or dat.get("evidence", {}).get("system_id") != system_id:
        raise ValueError("DAT match precisa estar concluído e corresponder à plataforma")
    if validation.get("status") not in {"completed", "completed_with_issues"}:
        raise ValueError("Validação de formato não está concluída")
    profile_extensions = _profile_extensions(system_id, project_root)
    inventory_by_path = {str(item.get("path", "")): item for item in inventory.get("items", [])
                         if item.get("system_id") == system_id}
    validation_by_path = {str(item.get("path", "")): item for item in validation.get("items", [])}
    hash_paths: dict[str, list[str]] = defaultdict(list)
    for item in validation.get("items", []):
        if item.get("sha256"):
            hash_paths[str(item["sha256"])].append(str(item.get("path", "")))
    planned: list[dict] = []
    media_items: list[dict] = []
    blockers: list[dict] = []
    planned_targets: set[str] = set()
    for match in dat.get("items", []):
        if match.get("system_id") != system_id or match.get("status") != "full_dat_match":
            continue
        suggested = str(match.get("suggested_container_name", ""))
        source_text = str(match.get("source_path", ""))
        if not suggested:
            continue
        try:
            relative = _safe_relative_path(source_text, system_id)
        except ValueError:
            continue
        source = project_root / relative
        if Path(suggested).name == source.name:
            continue
        considered = {"source": source_text, "suggested_container_name": suggested}
        inventory_item = inventory_by_path.get(source_text)
        if inventory_item and inventory_item.get("name_evidence", {}).get("kind_candidates"):
            blockers.append({**considered, "reason": "filename_has_variant_category_candidate"})
            continue
        if (str(match.get("matching_dat_records")) != "1"
                or str(match.get("archive_member_count")) != "1"
                or str(match.get("non_rom_member_count")) != "0"):
            blockers.append({**considered, "reason": "match_or_archive_structure_is_ambiguous"})
            continue
        member = PurePosixPath(str(match.get("payload_member", "")))
        if member.is_absolute() or len(member.parts) != 1 or any(part in {"", ".", ".."} for part in member.parts):
            blockers.append({**considered, "reason": "rom_member_not_at_archive_root"})
            continue
        target_name = Path(suggested).name
        if Path(target_name).suffix.casefold() != source.suffix.casefold():
            blockers.append({**considered, "reason": "dat_container_extension_changed"})
            continue
        ext = source.suffix.casefold().lstrip(".")
        if ext not in profile_extensions:
            blockers.append({**considered, "reason": "extension_not_supported_by_host_profile"})
            continue
        target = source.with_name(target_name)
        if target.exists() or target.is_symlink() or str(target) in planned_targets:
            blockers.append({**considered, "reason": "destination_collision", "destination": str(target)})
            continue
        if source.is_symlink() or not source.is_file():
            blockers.append({**considered, "reason": "source_missing_or_symlink"})
            continue
        validated = validation_by_path.get(str(source))
        if (not validated or validated.get("status") != "ok"
                or validated.get("sha256") is None
                or validated.get("member_count") != 1
                or str(validated.get("encrypted_member_count")) not in {"0", ""}):
            blockers.append({**considered, "reason": "format_validation_missing_or_changed"})
            continue
        sha = api["sha256_file"](source)
        if sha != validated["sha256"] or source.stat().st_size != int(validated["size_bytes"]):
            blockers.append({**considered, "reason": "source_snapshot_changed"})
            continue
        peers = sorted(set(path for path in hash_paths.get(sha, []) if path != str(source)))
        if peers:
            blockers.append({**considered, "reason": "duplicate_stored_container_hash", "other_paths": peers})
            continue

        item_media = []
        media_root = project_root / "media" / system_id
        if media_root.is_dir():
            for media_source in media_root.rglob("*"):
                if not media_source.is_file() or media_source.is_symlink() or media_source.stem.casefold() != source.stem.casefold():
                    continue
                media_relative = media_source.relative_to(media_root)
                if len(media_relative.parts) != 2:
                    blockers.append({**considered, "reason": "associated_media_path_not_canonical",
                                     "media_path": str(media_source)})
                    item_media = []
                    break
                media_validation = _validate_story_file(media_source) if media_source.suffix.casefold() == ".txt" else {
                    "sha256": api["sha256_file"](media_source), "size_bytes": media_source.stat().st_size,
                    "validation": "path_and_byte_hash_only"}
                media_destination = media_source.with_name(target.stem + media_source.suffix)
                if media_destination.exists() or media_destination.is_symlink():
                    blockers.append({**considered, "reason": "associated_media_destination_collision",
                                     "media_destination": str(media_destination)})
                    item_media = []
                    break
                item_media.append({"source": str(media_source), "destination": str(media_destination),
                                   "media_type": media_relative.parts[0], **media_validation})
        if any(item.get("source") == source_text or item.get("source") == str(source) for item in blockers):
            continue
        planned_targets.add(str(target))
        planned.append({
            "system_id": system_id, "source": str(source), "source_relative": relative.as_posix(),
            "destination": str(target), "destination_relative": target.relative_to(project_root).as_posix(),
            "filename": source.name, "canonical_nointro_filename": target.name,
            "size_bytes": source.stat().st_size, "sha256": sha,
            "dat_sha256": str(match["dat_sha256"]), "dat_rom_name": str(match["dat_rom_name"]),
            "dat_size_bytes": int(match["payload_size_bytes"]), "dat_crc32": str(match["payload_crc32"]),
            "dat_md5": str(match["payload_md5"]), "dat_sha1": str(match["payload_sha1"]),
            "dat_payload_sha256": str(match["payload_sha256"]), "dat_member": str(match["payload_member"]),
            "validation_operation_id": verify_operation_id, "validation_status": validated["status"],
            "media_items": item_media,
        })
        media_items.extend(item_media)
    if not planned:
        return _record_no_action_plan(system_id, f'No eligible No-Intro name corrections under {system_id}',
            {'inventory_operation_id':inventory_operation_id,'dat_match_operation_id':dat_operation_id,
             'validation_operation_id':verify_operation_id},
            sum(1 for r in dat.get('items',[]) if r.get('status')=='full_dat_match'),blockers,api,
            organization_type='nointro_name_alignment')
    move_map = {item["filename"]: item["canonical_nointro_filename"] for item in planned}
    esde_home = Path(api.get("ESDE_HOME", Path(api["PROJECT_ROOT"]) / "frontend" / "ES-DE" / "UserData"))
    reference_files, reference_blockers, reference_exclusions = _path_refs_for_moves(
        move_map, system_id, project_root, esde_home)
    blockers.extend(reference_blockers)
    blocked_names = {str(item.get("filename", "")) for item in reference_blockers if item.get("filename")}
    if blocked_names:
        planned = [item for item in planned if item["filename"] not in blocked_names]
        media_items = [media for item in planned for media in item["media_items"]]
        move_map = {item["filename"]: item["canonical_nointro_filename"] for item in planned}
        reference_files, remaining, reference_exclusions = _path_refs_for_moves(
            move_map, system_id, project_root, esde_home)
        blockers.extend(remaining)
    if not planned:
        raise ValueError("Todas as renomeações foram bloqueadas por referências sem regra conhecida")

    operation_id = f"rom-nointro-align-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = Path(api["operation_dir"](operation_id))
    opdir.mkdir(parents=True, exist_ok=False)
    rollback_dir = opdir / "rollback" / "references"
    proposal_dir = opdir / "proposal" / "references"
    rollback_dir.mkdir(parents=True)
    proposal_dir.mkdir(parents=True)
    reference_records = []
    for index, item in enumerate(reference_files, 1):
        path = Path(item["path"])
        before_path = rollback_dir / f"{index:04d}-{path.name}.before"
        after_path = proposal_dir / f"{index:04d}-{path.name}.after"
        before_path.write_bytes(item.pop("before_bytes"))
        after_path.write_bytes(item.pop("after_bytes"))
        reference_records.append({**item, "rollback_snapshot": str(before_path), "proposed_file": str(after_path)})
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "rom_variant_organization",
        "organization_type": "nointro_name_alignment",
        "title": f"Align verified {system_id} ROM and media basenames to No-Intro DAT",
        "created_at": api["now"](), "status": "prepared", "system_id": system_id,
        "inventory_operation_id": inventory_operation_id, "dat_match_operation_id": dat_operation_id,
        "validation_operation_id": verify_operation_id, "hierarchy_operation_id": "",
        "candidate_paths_considered": len(dat.get("items", [])), "planned_file_count": len(planned),
        "category_counts": {}, "blocked_candidate_count": len(blockers), "blockers": blockers,
        "items": planned, "media_items": media_items, "frontend_reference_updates": reference_records,
        "excluded_frontend_references": reference_exclusions,
        "policy": {"nointro_full_tuple_required": True, "no_overwrite": True, "no_extraction": True,
                   "no_permanent_delete": True, "same_filesystem_rename_only": True,
                   "associated_media_and_gamelists_atomic": True,
                   "requires_frontends_closed_for_apply_and_rollback": True},
    }
    api["atomic_json"](opdir / "manifest.json", manifest)
    api["append_event"](operation_id, "plan_created", action="rom_nointro_name_alignment_plan",
                        status="prepared", details={"system_id": system_id, "rom_count": len(planned),
                                                     "media_count": len(media_items),
                                                     "gamelist_count": len(reference_records),
                                                     "excluded_reference_count": len(reference_exclusions),
                                                     "blocked_candidate_count": len(blockers)})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": "prepared", "system_id": system_id,
            "rom_renames": len(planned), "media_renames": len(media_items),
            "gamelist_files": len(reference_records), "blocked_candidates": len(blockers),
            "manifest": str(opdir / "manifest.json")}


def _load_manifest(operation_id: str, api: dict) -> tuple[Path, dict]:
    path = Path(api["operation_dir"](operation_id)) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "rom_variant_organization" or manifest.get("operation_id") != operation_id:
        raise ValueError(f"Operação não é organização de variantes: {operation_id}")
    return path, manifest


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Referência ausente ou symlink: {path}")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _check_hierarchy(manifest: dict, api: dict) -> None:
    if manifest.get("organization_type") not in {
            "folder_placement", "user_attested_translation_placement"}:
        return
    hierarchy = _load_operation(manifest["hierarchy_operation_id"], "retrofe_rom_hierarchy", api)
    if (hierarchy.get("system_id") != manifest.get("system_id")
            or hierarchy.get("status") not in {"completed_verified", "verified"}):
        raise ValueError("RetroFE hierarchy precisa estar aplicada e verificada para a plataforma")


def _check_frontends_closed(api: dict) -> None:
    running = api["frontend_processes_running"]()
    active = ({name: processes for name, processes in running.items() if processes}
              if isinstance(running, dict) else running)
    if active:
        raise RuntimeError(f"Feche RetroFE e ES-DE normalmente antes de mover variantes: {active}")


def apply(operation_id: str, confirmed: bool, api: dict) -> dict:
    if not confirmed:
        raise ValueError("Aplicação exige --apply explícito")
    manifest_path, manifest = _load_manifest(operation_id, api)
    if manifest.get("status") != "prepared":
        raise ValueError(f"Plano não preparado: {manifest.get('status')}")
    _check_frontends_closed(api)
    _check_hierarchy(manifest, api)
    project_root: Path = api["PROJECT_ROOT"]
    for item in manifest["items"]:
        source, destination = Path(item["source"]), Path(item["destination"])
        if (source.is_symlink() or not source.is_file() or source.stat().st_size != item["size_bytes"]
                or api["sha256_file"](source) != item["sha256"]):
            raise ValueError(f"Origem mudou desde o plano: {source}")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Destino mudou desde o plano: {destination}")
        if not destination.is_relative_to(project_root / "roms" / manifest["system_id"]):
            raise ValueError(f"Destino saiu da raiz ROM permitida: {destination}")
        for component in destination.parents:
            if component == project_root / "roms" / manifest["system_id"]:
                break
            if component.is_symlink():
                raise ValueError(f"Componente de destino é symlink: {component}")
    for item in manifest.get("media_items", []):
        source, destination = Path(item["source"]), Path(item["destination"])
        if (source.is_symlink() or not source.is_file() or source.stat().st_size != item["size_bytes"]
                or api["sha256_file"](source) != item["sha256"]):
            raise ValueError(f"Mídia associada mudou desde o plano: {source}")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Destino de mídia mudou desde o plano: {destination}")
        if not destination.is_relative_to(project_root / "media" / manifest["system_id"]):
            raise ValueError(f"Destino de mídia saiu da raiz canônica: {destination}")
    for ref in manifest["frontend_reference_updates"]:
        path = Path(ref["path"])
        if path.is_symlink() or not path.is_file() or _sha(path.read_bytes()) != ref["sha256_before"]:
            raise ValueError(f"Gamelist mudou desde o plano: {path}")
        proposed = Path(ref["proposed_file"]).read_bytes()
        if _sha(proposed) != ref["sha256_after"]:
            raise ValueError(f"Arquivo proposto de frontend mudou: {ref['proposed_file']}")

    manifest["status"] = "applying"
    manifest["applying_at"] = api["now"]()
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "operation_started", action="rom_variant_organization_apply",
                        status="applying", details={"file_count": len(manifest["items"]),
                                                     "reference_file_count": len(manifest["frontend_reference_updates"])})
    created_dirs: set[Path] = set()
    moved: list[dict] = []
    moved_media: list[dict] = []
    updated_refs: list[dict] = []
    try:
        for item in manifest["items"]:
            source, destination = Path(item["source"]), Path(item["destination"])
            missing = []
            parent = destination.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            if source.stat().st_dev != parent.stat().st_dev:
                raise ValueError(f"Organização exige rename no mesmo dispositivo: {source} -> {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.update(missing)
            os.replace(source, destination)
            moved.append(item)
        for item in manifest.get("media_items", []):
            source, destination = Path(item["source"]), Path(item["destination"])
            if (source.is_symlink() or not source.is_file() or api["sha256_file"](source) != item["sha256"]
                    or os.path.lexists(destination)):
                raise ValueError(f"Mídia mudou durante a aplicação: {source}")
            if source.stat().st_dev != destination.parent.stat().st_dev:
                raise ValueError(f"Renomeação de mídia exige o mesmo dispositivo: {source} -> {destination}")
            os.replace(source, destination)
            moved_media.append(item)
        for ref in manifest["frontend_reference_updates"]:
            path = Path(ref["path"])
            if _sha(path.read_bytes()) != ref["sha256_before"]:
                raise ValueError(f"Gamelist mudou durante a aplicação: {path}")
            _atomic_write(path, Path(ref["proposed_file"]).read_bytes())
            updated_refs.append(ref)
        checks = _verify_items(manifest, api)
        if not all(checks.values()):
            raise RuntimeError(f"Pós-validação da organização falhou: {checks}")
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = api["now"]()
        manifest["created_directories"] = [str(path) for path in sorted(created_dirs, key=lambda x: len(x.parts))]
        manifest["last_verify"] = {"at": api["now"](), "checks": checks}
        api["atomic_json"](manifest_path, manifest)
        api["append_event"](operation_id, "operation_completed", action="rom_variant_organization_apply",
                            status="completed_verified", details={"file_count": len(moved),
                                                                 "media_file_count": len(moved_media),
                                                                 "reference_file_count": len(updated_refs),
                                                                 "category_counts": manifest["category_counts"]})
        api["rebuild_history_index"]()
        return {"operation_id": operation_id, "status": manifest["status"],
                "moved_files": len(moved), "updated_reference_files": len(updated_refs),
                "renamed_media_files": len(moved_media),
                "category_counts": manifest["category_counts"], "manifest": str(manifest_path)}
    except Exception:
        for ref in reversed(updated_refs):
            path = Path(ref["path"])
            if path.is_file() and not path.is_symlink() and _sha(path.read_bytes()) == ref["sha256_after"]:
                _atomic_write(path, Path(ref["rollback_snapshot"]).read_bytes())
        for item in reversed(moved):
            source, destination = Path(item["source"]), Path(item["destination"])
            if (destination.is_file() and not destination.is_symlink() and not os.path.lexists(source)
                    and api["sha256_file"](destination) == item["sha256"]):
                os.replace(destination, source)
        for item in reversed(moved_media):
            source, destination = Path(item["source"]), Path(item["destination"])
            if (destination.is_file() and not destination.is_symlink() and not os.path.lexists(source)
                    and api["sha256_file"](destination) == item["sha256"]):
                os.replace(destination, source)
        for directory in sorted(created_dirs, key=lambda x: len(x.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        manifest["status"] = "apply_failed_rolled_back"
        manifest["completed_at"] = api["now"]()
        api["atomic_json"](manifest_path, manifest)
        api["append_event"](operation_id, "operation_failed", action="rom_variant_organization_apply",
                            status="apply_failed_rolled_back")
        api["rebuild_history_index"]()
        raise


def _verify_items(manifest: dict, api: dict) -> dict:
    checks = {"all_destinations_match": True, "all_sources_absent": True,
              "all_frontend_references_match": True, "active_hierarchy_verified": True}
    for item in manifest.get("items", []):
        source, destination = Path(item["source"]), Path(item["destination"])
        if source.exists() or source.is_symlink():
            checks["all_sources_absent"] = False
        if (destination.is_symlink() or not destination.is_file()
                or destination.stat().st_size != item["size_bytes"]
                or api["sha256_file"](destination) != item["sha256"]):
            checks["all_destinations_match"] = False
    for item in manifest.get("media_items", []):
        source, destination = Path(item["source"]), Path(item["destination"])
        if source.exists() or source.is_symlink():
            checks["all_sources_absent"] = False
        if (destination.is_symlink() or not destination.is_file()
                or destination.stat().st_size != item["size_bytes"]
                or api["sha256_file"](destination) != item["sha256"]):
            checks["all_destinations_match"] = False
    for ref in manifest.get("frontend_reference_updates", []):
        path = Path(ref["path"])
        if (path.is_symlink() or not path.is_file() or _sha(path.read_bytes()) != ref["sha256_after"]):
            checks["all_frontend_references_match"] = False
    try:
        _check_hierarchy(manifest, api)
    except Exception:
        checks["active_hierarchy_verified"] = False
    return checks


def verify(operation_id: str, api: dict) -> dict:
    manifest_path, manifest = _load_manifest(operation_id, api)
    if manifest.get("status") not in {"completed_verified", "verified"}:
        raise ValueError(f"Operação ainda não está aplicada: {manifest.get('status')}")
    checks = _verify_items(manifest, api)
    status = "verified" if all(checks.values()) else "verification_failed"
    manifest["status"] = status
    manifest["last_verify"] = {"at": api["now"](), "checks": checks}
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "verification_completed", action="rom_variant_organization_verify",
                        status=status, details=checks)
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": status, "checks": checks,
            "manifest": str(manifest_path)}


def rollback(operation_id: str, confirmed: bool, api: dict) -> dict:
    if not confirmed:
        raise ValueError("Rollback exige --rollback explícito")
    manifest_path, manifest = _load_manifest(operation_id, api)
    if manifest.get("status") not in {"completed_verified", "verified"}:
        raise ValueError(f"Operação não está em estado verificado para rollback: {manifest.get('status')}")
    _check_frontends_closed(api)
    _check_hierarchy(manifest, api)
    for ref in manifest.get("frontend_reference_updates", []):
        path = Path(ref["path"])
        if path.is_symlink() or not path.is_file() or _sha(path.read_bytes()) != ref["sha256_after"]:
            raise ValueError(f"Gamelist mudou após a organização; rollback recusado: {path}")
    for item in manifest.get("items", []):
        source, destination = Path(item["source"]), Path(item["destination"])
        if (source.exists() or source.is_symlink() or destination.is_symlink() or not destination.is_file()
                or api["sha256_file"](destination) != item["sha256"]):
            raise ValueError(f"ROM mudou após a organização; rollback recusado: {destination}")
    for item in manifest.get("media_items", []):
        source, destination = Path(item["source"]), Path(item["destination"])
        if (source.exists() or source.is_symlink() or destination.is_symlink() or not destination.is_file()
                or api["sha256_file"](destination) != item["sha256"]):
            raise ValueError(f"Mídia mudou após a organização; rollback recusado: {destination}")
    for ref in reversed(manifest.get("frontend_reference_updates", [])):
        _atomic_write(Path(ref["path"]), Path(ref["rollback_snapshot"]).read_bytes())
    for item in reversed(manifest.get("items", [])):
        source, destination = Path(item["source"]), Path(item["destination"])
        source.parent.mkdir(parents=True, exist_ok=True)
        os.replace(destination, source)
    for item in reversed(manifest.get("media_items", [])):
        source, destination = Path(item["source"]), Path(item["destination"])
        os.replace(destination, source)
    for directory in sorted((Path(x) for x in manifest.get("created_directories", [])),
                            key=lambda x: len(x.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    for item in manifest.get("items", []):
        source = Path(item["source"])
        if source.is_symlink() or not source.is_file() or api["sha256_file"](source) != item["sha256"]:
            raise RuntimeError(f"Rollback não restaurou o arquivo: {source}")
    for item in manifest.get("media_items", []):
        source = Path(item["source"])
        if source.is_symlink() or not source.is_file() or api["sha256_file"](source) != item["sha256"]:
            raise RuntimeError(f"Rollback não restaurou a mídia: {source}")
    for ref in manifest.get("frontend_reference_updates", []):
        path = Path(ref["path"])
        if _sha(path.read_bytes()) != ref["sha256_before"]:
            raise RuntimeError(f"Rollback não restaurou a gamelist: {path}")
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = api["now"]()
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "operation_rolled_back", action="rom_variant_organization_rollback",
                        status=manifest["status"])
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": manifest["status"],
            "restored_files": len(manifest.get("items", [])),
            "restored_reference_files": len(manifest.get("frontend_reference_updates", [])),
            "manifest": str(manifest_path)}


def cancel_prepared(operation_id: str, reason: str, api: dict) -> dict:
    reason = reason.strip()
    if not reason:
        raise ValueError("O cancelamento exige uma justificativa registrada")
    manifest_path, manifest = _load_manifest(operation_id, api)
    if manifest.get("status") != "prepared":
        raise ValueError(f"Somente um plano preparado pode ser cancelado: {manifest.get('status')}")
    manifest["status"] = "cancelled_superseded"
    manifest["cancelled_at"] = api["now"]()
    manifest["cancellation_reason"] = reason
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "operation_cancelled", action="rom_variant_organization_cancel",
                        status=manifest["status"], details={"reason": reason})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": manifest["status"],
            "reason": reason, "manifest": str(manifest_path)}


def dispatch(args, api: dict) -> dict:
    if getattr(args, "esde_home", None):
        canonical = Path(api["PROJECT_ROOT"]) / "frontend" / "ES-DE" / "UserData"
        if Path(args.esde_home).expanduser().resolve() != canonical.resolve():
            raise ValueError(f"A raiz ES-DE deve permanecer em {canonical}")
        api = {**api, "ESDE_HOME": canonical}
    if getattr(args, "variants_command", "") == "names":
        if args.variant_name_command == "plan":
            return plan_dat_name_alignment(args.system_id, args.inventory_operation_id,
                                           args.dat_match_operation_id, args.verify_operation_id, api)
        if args.variant_name_command == "apply":
            return apply(args.operation_id, args.apply, api)
        if args.variant_name_command == "verify":
            return verify(args.operation_id, api)
        if args.variant_name_command == "rollback":
            return rollback(args.operation_id, args.rollback, api)
        raise ValueError(f"Comando de alinhamento de nomes desconhecido: {args.variant_name_command}")
    if args.variant_organization_command == "plan":
        return plan(args.system_id, args.inventory_operation_id, args.dat_match_operation_id,
                    args.verify_operation_id, args.hierarchy_operation_id, api)
    if args.variant_organization_command == "translation-plan":
        return plan_user_attested_translations(
            args.system_id, args.inventory_operation_id, args.dat_match_operation_id,
            args.verify_operation_id, args.hierarchy_operation_id, args.language,
            args.attestation, api)
    if args.variant_organization_command == "apply":
        return apply(args.operation_id, args.apply, api)
    if args.variant_organization_command == "cancel":
        return cancel_prepared(args.operation_id, args.reason, api)
    if args.variant_organization_command == "verify":
        return verify(args.operation_id, api)
    if args.variant_organization_command == "rollback":
        return rollback(args.operation_id, args.rollback, api)
    raise ValueError(f"Comando de organização de variantes desconhecido: {args.variant_organization_command}")
