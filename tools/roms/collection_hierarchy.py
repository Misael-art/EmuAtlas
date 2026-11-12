"""Reversible materialization of RetroFE recursive ROM collection settings."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_unique_value(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", re.IGNORECASE)
    values = [match.group(1).strip() for line in text.splitlines()
              if (match := pattern.match(line)) and not line.lstrip().startswith("#")]
    if len(values) > 1:
        raise ValueError(f"Chave duplicada em settings.conf: {key}")
    return values[0] if values else None


def _set_unique_value(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=.*$", re.IGNORECASE)
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines)
               if pattern.match(line) and not line.lstrip().startswith("#")]
    if len(matches) > 1:
        raise ValueError(f"Chave duplicada em settings.conf: {key}")
    if matches:
        index = matches[0]
        indent = pattern.match(lines[index]).group(1)
        lines[index] = f"{indent}{key} = {value}"
    else:
        lines.append(f"{key} = {value}")
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\n", "\r")) or not text
    result = newline.join(lines)
    return result + (newline if trailing_newline else "")


def _config_target(system_id: str, api: dict) -> dict:
    root: Path = api["PROJECT_ROOT"]
    catalog_path = root / "frontend/RetroFE/Config/platform-catalog.json"
    profile_path = root / "frontend/RetroFE/Config/profiles/host-linux.json"
    main_menu = root / "frontend/RetroFE/collections/Main/menu.txt"
    settings_global = root / "frontend/RetroFE/settings.conf"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    platforms = catalog.get("systems", [])
    platform = next((item for item in platforms if item.get("id") == system_id
                     and item.get("kind") == "platform"), None)
    if not platform:
        raise ValueError(f"system_id não é uma plataforma ativa no catálogo: {system_id}")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    menu_names = {line.strip() for line in main_menu.read_text(encoding="utf-8-sig").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")}
    collection_matches = [name for name, item in profile.get("collections", {}).items()
                          if item.get("rom_subdir") == platform.get("rom_subdir")
                          and name in menu_names]
    if len(collection_matches) != 1:
        raise ValueError(
            f"A plataforma {system_id} precisa mapear para exatamente uma coleção ativa no Main; "
            f"encontradas: {collection_matches}"
        )
    collection = collection_matches[0]
    profile_collection = profile["collections"][collection]
    if profile_collection.get("rom_subdir") != platform.get("rom_subdir"):
        raise ValueError(f"rom_subdir diverge entre catálogo e perfil: {system_id}")
    rom_root_value = profile.get("rom_root", "")
    rom_root = Path(rom_root_value).expanduser()
    if not rom_root.is_absolute():
        rom_root = (root / "frontend/RetroFE" / rom_root).resolve()
    else:
        rom_root = rom_root.resolve()
    active_base = _read_unique_value(settings_global.read_text(encoding="utf-8"), "baseItemPath")
    if not active_base:
        raise ValueError("RetroFE settings.conf não declara baseItemPath")
    if Path(active_base).expanduser().resolve() != rom_root:
        raise ValueError(f"baseItemPath ativo não coincide com o perfil: {active_base} != {rom_root}")
    canonical_rom_dir = rom_root / platform["rom_subdir"]
    if canonical_rom_dir.is_symlink() or not canonical_rom_dir.is_dir():
        raise ValueError(f"Raiz ROM canônica não é diretório regular: {canonical_rom_dir}")
    settings_path = root / "frontend/RetroFE/collections" / collection / "settings.conf"
    if settings_path.is_symlink() or not settings_path.is_file():
        raise ValueError(f"settings.conf ativo ausente ou simbólico: {settings_path}")
    expected_list_path = f"%BASE_ITEM_PATH%/{platform['rom_subdir']}"
    related_names = {collection, *profile_collection.get("subcollections", [])}
    related_names.update(
        name for name, item in profile.get("subcollection_overrides", {}).items()
        if item.get("rom_subdir") == platform["rom_subdir"]
    )

    def path_targets_canonical(value: str, config_path: Path) -> bool:
        normalized = value.strip().replace("\\", "/")
        if normalized == expected_list_path:
            return True
        expanded = normalized.replace("%BASE_ITEM_PATH%", str(rom_root))
        if "%" in expanded:
            return False
        target = Path(expanded).expanduser()
        if not target.is_absolute():
            target = config_path.parent / target
        return target.resolve(strict=False) == canonical_rom_dir.resolve()

    settings_targets = []
    excluded_related_settings = []
    settings_root = root / "frontend/RetroFE/collections"
    for candidate in sorted(settings_root.glob("*/settings.conf"), key=lambda path: path.parent.name.casefold()):
        is_active = candidate == settings_path
        is_profile_related = candidate.parent.name in related_names
        if candidate.is_symlink():
            if is_active or is_profile_related:
                raise ValueError(f"settings.conf associado à plataforma é symlink: {candidate}")
            continue
        if not candidate.is_file():
            continue
        current_bytes = candidate.read_bytes()
        current = current_bytes.decode("utf-8")
        list_path = _read_unique_value(current, "list.path")
        targets_canonical = bool(list_path and path_targets_canonical(list_path, candidate))
        if is_active and not targets_canonical:
            raise ValueError(
                f"A coleção ativa {collection} aponta para outra raiz ROM: {list_path!r}; "
                f"esperada {expected_list_path!r}"
            )
        if not targets_canonical:
            if is_profile_related:
                excluded_related_settings.append({
                    "collection": candidate.parent.name,
                    "settings_path": str(candidate),
                    "list_path": list_path,
                    "reason": "profile_related_collection_points_to_another_rom_root",
                })
            continue
        proposed = _set_unique_value(current, "list.path", expected_list_path)
        proposed = _set_unique_value(proposed, "list.romHierarchy", "true")
        settings_targets.append({
            "collection": candidate.parent.name,
            "settings_path": candidate,
            "list_path_before": list_path,
            "before_bytes": current_bytes,
            "after_bytes": proposed.encode("utf-8"),
            "rom_hierarchy_before": _read_unique_value(current, "list.romHierarchy"),
        })
    if not any(item["settings_path"] == settings_path for item in settings_targets):
        raise ValueError(f"A coleção ativa não entrou no plano de hierarquia: {settings_path}")
    return {
        "system_id": system_id,
        "collection": collection,
        "rom_subdir": platform["rom_subdir"],
        "rom_root": str(rom_root),
        "canonical_rom_dir": str(canonical_rom_dir),
        "settings_path": settings_path,
        "expected_list_path": expected_list_path,
        "current_list_path": _read_unique_value(settings_path.read_bytes().decode("utf-8"), "list.path"),
        "current_hierarchy": _read_unique_value(settings_path.read_bytes().decode("utf-8"), "list.romHierarchy"),
        "settings_targets": settings_targets,
        "excluded_related_settings": excluded_related_settings,
    }


def _atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Arquivo de configuração ausente ou simbólico: {path}")
    permissions = mode if mode is not None else path.stat().st_mode & 0o777
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, permissions)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _load_manifest(operation_id: str, api: dict) -> tuple[Path, dict]:
    path = api["operation_dir"](operation_id) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_rom_hierarchy":
        raise ValueError(f"Operação não é materialização de hierarquia: {operation_id}")
    return path, manifest


def plan(system_id: str, api: dict) -> dict:
    target = _config_target(system_id, api)
    settings_targets = target["settings_targets"]
    changed_targets = [item for item in settings_targets if item["before_bytes"] != item["after_bytes"]]
    # A fresh read-only proof is needed when unrelated settings changed after an older operation.
    operation_id = f"retrofe-hierarchy-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = api["operation_dir"](operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    rollback_dir = opdir / "rollback"
    proposal_dir = opdir / "proposal"
    rollback_dir.mkdir()
    proposal_dir.mkdir()
    changes = []
    for index, item in enumerate(settings_targets, start=1):
        before = item["before_bytes"]
        after = item["after_bytes"]
        before_path = rollback_dir / f"{index:04d}-settings.conf.before"
        after_path = proposal_dir / f"{index:04d}-settings.conf.after"
        before_path.write_bytes(before)
        after_path.write_bytes(after)
        changes.append({
            "collection": item["collection"],
            "settings_path": str(item["settings_path"]),
            "list_path_before": item["list_path_before"],
            "list_path_after": target["expected_list_path"],
            "rom_hierarchy_before": item["rom_hierarchy_before"],
            "rom_hierarchy_after": "true",
            "sha256_before": _sha256(before),
            "sha256_after": _sha256(after),
            "size_before": len(before),
            "size_after": len(after),
            "rollback_snapshot": str(before_path),
            "proposed_config": str(after_path),
            "changed": before != after,
        })
    primary = next(item for item in changes if item["settings_path"] == str(target["settings_path"]))
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id,
        "kind": "retrofe_rom_hierarchy", "title": f"Enable nested ROM navigation for {system_id}",
        "created_at": api["now"](), "status": "prepared" if changed_targets else "verified", "system_id": system_id,
        "read_only": not bool(changed_targets), "apply_available": bool(changed_targets),
        "collection": target["collection"], "rom_subdir": target["rom_subdir"],
        "canonical_rom_dir": target["canonical_rom_dir"],
        "settings_path": str(target["settings_path"]),
        "list_path_before": primary["list_path_before"],
        "list_path_after": target["expected_list_path"],
        "rom_hierarchy_before": primary["rom_hierarchy_before"], "rom_hierarchy_after": "true",
        "sha256_before": primary["sha256_before"], "sha256_after": primary["sha256_after"],
        "size_before": primary["size_before"], "size_after": primary["size_after"],
        "rollback_snapshot": primary["rollback_snapshot"], "proposed_config": primary["proposed_config"],
        "settings_changes": changes,
        "excluded_related_settings": target["excluded_related_settings"],
        "changed_settings_count": len(changed_targets),
        "policy": {"atomic_replace": True, "permanent_delete": False,
                   "active_main_collection_required": True,
                   "all_collections_currently_pointing_to_canonical_rom_root": True,
                   "cross_platform_collection_paths_are_never_rewritten": True},
    }
    api["atomic_json"](opdir / "manifest.json", manifest)
    api["append_event"](operation_id, "plan_created" if changed_targets else "current_configuration_verified", action="retrofe_hierarchy_plan",
                        status=manifest["status"], source=str(target["settings_path"]),
                        sha256_before=manifest["sha256_before"],
                        sha256_after=manifest["sha256_after"],
                        details={"system_id": system_id, "collection": target["collection"],
                                 "rom_subdir": target["rom_subdir"],
                                 "collections_updated": [item["collection"] for item in changes if item["changed"]],
                                 "settings_count": len(changes)})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": manifest["status"], "system_id": system_id,
            "read_only": not bool(changed_targets), "apply_available": bool(changed_targets),
            "collection": target["collection"], "settings_path": str(target["settings_path"]),
            "affected_collections": [item["collection"] for item in changes],
            "excluded_related_collections": target["excluded_related_settings"],
            "changed_settings_count": len(changed_targets),
            "list_path_before": target["current_list_path"],
            "list_path_after": target["expected_list_path"],
            "rom_hierarchy_before": target["current_hierarchy"], "rom_hierarchy_after": "true",
            "manifest": str(opdir / "manifest.json"), "proposed_config": primary["proposed_config"]}


def apply(operation_id: str, confirmed: bool, api: dict) -> dict:
    if not confirmed:
        raise ValueError("Aplicação exige --apply explícito")
    manifest_path, manifest = _load_manifest(operation_id, api)
    if manifest.get("status") != "prepared":
        raise ValueError(f"Plano não preparado: {manifest.get('status')}")
    if api["retrofe_processes_running"]():
        raise RuntimeError("Feche RetroFE normalmente antes de alterar list.path/list.romHierarchy")
    target = _config_target(manifest["system_id"], api)
    changes = manifest.get("settings_changes")
    if not changes:
        changes = [{
            "collection": manifest["collection"], "settings_path": manifest["settings_path"],
            "list_path_before": manifest["list_path_before"], "list_path_after": manifest["list_path_after"],
            "rom_hierarchy_before": manifest.get("rom_hierarchy_before"), "rom_hierarchy_after": "true",
            "sha256_before": manifest["sha256_before"], "sha256_after": manifest["sha256_after"],
            "rollback_snapshot": manifest["rollback_snapshot"], "proposed_config": manifest["proposed_config"],
            "changed": True,
        }]
    actual_by_path = {str(item["settings_path"]): item for item in target["settings_targets"]}
    if set(actual_by_path) != {str(item["settings_path"]) for item in changes}:
        raise ValueError("Coleções que compartilham a raiz ROM mudaram desde o plano")
    for change in changes:
        current_target = actual_by_path[str(change["settings_path"])]
        current = current_target["before_bytes"]
        proposed = current_target["after_bytes"]
        if (_sha256(current) != change["sha256_before"]
                or _sha256(proposed) != change["sha256_after"]
                or current_target["collection"] != change["collection"]):
            raise ValueError(f"settings.conf ou proposta mudou desde o plano: {change['settings_path']}")
    manifest["status"] = "applying"
    manifest["applied_at"] = api["now"]()
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "operation_started", action="retrofe_hierarchy_apply",
                        status="applying", source=manifest["settings_path"], sha256_before=manifest["sha256_before"],
                        sha256_after=manifest["sha256_after"])
    applied_paths = []
    try:
        for change in changes:
            if not change.get("changed", True):
                continue
            path = Path(change["settings_path"])
            current_target = actual_by_path[str(path)]
            _atomic_write(path, current_target["after_bytes"])
            after = path.read_bytes()
            if _sha256(after) != change["sha256_after"]:
                raise RuntimeError(f"SHA-256 divergente após atualizar {path}")
            applied_paths.append(path)
        current_target = _config_target(manifest["system_id"], api)
        verified_by_path = {str(item["settings_path"]): item for item in current_target["settings_targets"]}
        for change in changes:
            actual = verified_by_path[str(change["settings_path"])]
            if (actual["list_path_before"] != manifest["list_path_after"]
                    or str(actual["rom_hierarchy_before"]).casefold() != "true"):
                raise RuntimeError(f"list.path/list.romHierarchy não corresponde ao destino planejado: {change['settings_path']}")
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = api["now"]()
        manifest["after_snapshot"] = {"settings": [
            {"path": change["settings_path"], "sha256": change["sha256_after"], "size_bytes": change.get("size_after")}
            for change in changes
        ]}
        api["atomic_json"](manifest_path, manifest)
        api["append_event"](operation_id, "operation_completed", action="retrofe_hierarchy_apply",
                            status="completed_verified", source=manifest["settings_path"],
                            sha256_before=manifest["sha256_before"], sha256_after=_sha256(after),
                            size_before=manifest["size_before"], size_after=manifest["size_after"],
                            details={"settings_updated": [str(path) for path in applied_paths]})
        api["rebuild_history_index"]()
        return {"operation_id": operation_id, "status": manifest["status"],
                "system_id": manifest["system_id"], "collection": manifest["collection"],
                "settings_path": manifest["settings_path"],
                "updated_settings_count": len(applied_paths),
                "updated_collections": [change["collection"] for change in changes if change.get("changed", True)]}
    except Exception:
        for path in reversed(applied_paths):
            change = next(item for item in changes if item["settings_path"] == str(path))
            if path.is_file() and _sha256(path.read_bytes()) == change["sha256_after"]:
                _atomic_write(path, Path(change["rollback_snapshot"]).read_bytes())
        manifest["status"] = "apply_failed_rolled_back"
        manifest["completed_at"] = api["now"]()
        api["atomic_json"](manifest_path, manifest)
        api["append_event"](operation_id, "operation_failed", action="retrofe_hierarchy_apply",
                            status="apply_failed_rolled_back", source=manifest["settings_path"],
                            details={"rollback_snapshots": [item["rollback_snapshot"] for item in changes]})
        api["rebuild_history_index"]()
        raise


def verify(operation_id: str, api: dict) -> dict:
    manifest_path, manifest = _load_manifest(operation_id, api)
    actual = _config_target(manifest["system_id"], api)
    changes = manifest.get("settings_changes") or [{
        "collection": manifest["collection"], "settings_path": manifest["settings_path"],
        "list_path_after": manifest["list_path_after"], "sha256_after": manifest["sha256_after"],
    }]
    actual_by_path = {str(item["settings_path"]): item for item in actual["settings_targets"]}
    per_settings = {}
    for change in changes:
        path = Path(change["settings_path"])
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"settings.conf ausente ou simbólico: {path}")
        current = path.read_bytes()
        current_target = actual_by_path.get(str(path))
        per_settings[str(path)] = {
            "sha256_matches": _sha256(current) == change["sha256_after"],
            "list_path_matches": current_target is not None
                and current_target["list_path_before"] == change["list_path_after"],
            "hierarchy_enabled": current_target is not None
                and str(current_target["rom_hierarchy_before"]).casefold() == "true",
        }
    checks = {
        "sha256_matches": all(item["sha256_matches"] for item in per_settings.values()),
        "list_path_matches": all(item["list_path_matches"] for item in per_settings.values()),
        "hierarchy_enabled": all(item["hierarchy_enabled"] for item in per_settings.values()),
        "active_collection_matches": actual["collection"] == manifest["collection"],
        "all_shared_collections_verified": bool(per_settings),
    }
    manifest["last_verify"] = {"at": api["now"](), "checks": checks}
    manifest["status"] = "verified" if all(checks.values()) else "verification_failed"
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "verification_completed", action="retrofe_hierarchy_verify",
                        status=manifest["status"], source=manifest["settings_path"],
                        sha256_after=manifest["sha256_after"], details={"checks": checks, "settings": per_settings})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": manifest["status"],
            "settings_path": manifest["settings_path"], "settings_count": len(per_settings),
            "settings_checks": per_settings, "checks": checks}


def rollback(operation_id: str, confirmed: bool, api: dict) -> dict:
    if not confirmed:
        raise ValueError("Rollback exige --rollback explícito")
    manifest_path, manifest = _load_manifest(operation_id, api)
    if manifest.get("status") not in {"completed_verified", "verified"}:
        raise ValueError(f"Operação não está em estado reversível verificado: {manifest.get('status')}")
    if manifest.get("read_only"):
        raise ValueError("Snapshot de hierarquia somente leitura; nenhuma configuração foi alterada para reverter")
    if api["retrofe_processes_running"]():
        raise RuntimeError("Feche RetroFE normalmente antes do rollback da configuração")
    changes = manifest.get("settings_changes") or [{
        "settings_path": manifest["settings_path"], "sha256_after": manifest["sha256_after"],
        "sha256_before": manifest["sha256_before"], "rollback_snapshot": manifest["rollback_snapshot"],
        "changed": True,
    }]
    for change in changes:
        path = Path(change["settings_path"])
        current = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        if _sha256(current) != change["sha256_after"]:
            raise ValueError(f"settings.conf mudou após a operação; rollback automático recusado: {path}")
        before = Path(change["rollback_snapshot"]).read_bytes()
        if _sha256(before) != change["sha256_before"]:
            raise ValueError(f"Snapshot de rollback divergente: {path}")
    for change in reversed(changes):
        if not change.get("changed", True):
            continue
        path = Path(change["settings_path"])
        before = Path(change["rollback_snapshot"]).read_bytes()
        _atomic_write(path, before)
        restored = path.read_bytes()
        if _sha256(restored) != change["sha256_before"]:
            raise RuntimeError(f"Rollback não restaurou o SHA-256 original: {path}")
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = api["now"]()
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "operation_rolled_back", action="retrofe_hierarchy_rollback",
                        status=manifest["status"], source=manifest["settings_path"],
                        sha256_before=manifest["sha256_after"], sha256_after=manifest["sha256_before"],
                        details={"settings_restored": [item["settings_path"] for item in changes if item.get("changed", True)]})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": manifest["status"],
            "settings_path": manifest["settings_path"],
            "restored_settings_count": sum(1 for item in changes if item.get("changed", True))}


def cancel_prepared(operation_id: str, reason: str, api: dict) -> dict:
    manifest_path, manifest = _load_manifest(operation_id, api)
    if manifest.get("status") != "prepared":
        raise ValueError(f"Só planos preparados podem ser cancelados: {manifest.get('status')}")
    if not reason.strip():
        raise ValueError("O motivo do cancelamento não pode ficar vazio")
    manifest["status"] = "cancelled_superseded"
    manifest["cancelled_at"] = api["now"]()
    manifest["cancellation_reason"] = reason.strip()
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](operation_id, "plan_cancelled", action="retrofe_hierarchy_cancel",
                        status=manifest["status"], source=manifest["settings_path"],
                        details={"reason": manifest["cancellation_reason"]})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": manifest["status"],
            "reason": manifest["cancellation_reason"], "manifest": str(manifest_path)}


def dispatch(args, api: dict) -> dict:
    if args.hierarchy_command == "plan":
        return plan(args.system_id, api)
    if args.hierarchy_command == "apply":
        return apply(args.operation_id, args.apply, api)
    if args.hierarchy_command == "verify":
        return verify(args.operation_id, api)
    if args.hierarchy_command == "rollback":
        return rollback(args.operation_id, args.rollback, api)
    if args.hierarchy_command == "cancel":
        return cancel_prepared(args.operation_id, args.reason, api)
    raise ValueError(args.hierarchy_command)
