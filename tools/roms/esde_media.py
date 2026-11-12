"""Reconcile ES-DE's downloaded_media tree with the project's shared media root.

All payload changes are planned, hash-checked, reversible moves. The module is
invoked only through romtool so operations share the official journal/index.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


MEDIA_TYPE_MAP = {
    "3dboxes": "box",
    "backcovers": "cover-back",
    "covers": "cover-front",
    "fanart": "background",
    "manuals": "manual",
    "marquees": "marquee",
    "miximages": "miximage",
    "physicalmedia": "physicalmedia",
    "screenshots": "screenshot",
    "titlescreens": "screentitle",
    "videos": "video",
}
MEDIA_TYPE_ALIASES = {source: target for source, target in MEDIA_TYPE_MAP.items() if source != target}
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".mp4", ".pdf"}
OPERATION_PREFIX = "esde-media-migration-"


def appdata_root(project_root: Path, override: str | None = None) -> Path:
    """One relocatable root shared by the launcher and maintenance tools."""
    expected = Path(os.path.abspath(project_root)) / "frontend" / "ES-DE" / "UserData"
    if override:
        expanded = Path(os.path.expandvars(os.path.expanduser(override)))
        if not expanded.is_absolute() or Path(os.path.abspath(expanded)) != expected:
            raise ValueError(f"ESDE_APPDATA_DIR deve apontar somente para {expected}")
    _no_symlink_components(expected, Path(os.path.abspath(project_root)))
    return expected


def _userdata_snapshot(root: Path, media_root: Path) -> list[dict]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Dados ES-DE precisam ser um diretório real: {root}")
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        row = {"path": rel, "mode": path.lstat().st_mode & 0o777}
        if path.is_symlink():
            if rel != "downloaded_media" or path.resolve(strict=True) != media_root.resolve(strict=True):
                raise ValueError(f"Link de dados ES-DE sem destino autorizado: {path}")
            row.update(kind="symlink", target=os.readlink(path))
        elif path.is_dir():
            if rel == "downloaded_media" or rel.startswith("downloaded_media/"):
                raise ValueError("Centralize downloaded_media com media esde plan/apply antes de migrar os dados.")
            row.update(kind="directory")
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            row.update(kind="file", size_bytes=path.stat().st_size, sha256=digest.hexdigest())
        else:
            raise ValueError(f"Tipo de arquivo de dados ES-DE não suportado: {path}")
        rows.append(row)
    return rows


def command_userdata(args, api: dict) -> dict:
    """Explicit, same-device appdata relocation, separate from media payloads."""
    source = Path.home() / "ES-DE"
    destination = appdata_root(api["PROJECT_ROOT"])
    media_root = Path(api["MEDIA_DEST_ROOT"])
    command = args.esde_userdata_command
    if command == "plan":
        if os.path.lexists(destination):
            raise FileExistsError(f"Destino de dados ES-DE já existe; não mesclo árvores: {destination}")
        before = _userdata_snapshot(source, media_root)
        if source.stat().st_dev != destination.parent.stat().st_dev:
            raise ValueError("Migração de dados ES-DE requer rename no mesmo dispositivo.")
        operation_id = "esde-userdata-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        opdir = api["operation_dir"](operation_id)
        opdir.mkdir(parents=True, exist_ok=False)
        after = [dict(row) for row in before]
        for row in after:
            if row["kind"] == "symlink":
                row["target"] = os.path.relpath(media_root, destination)
        manifest = {"schema_version": 1, "kind": "esde_userdata_migration", "operation_id": operation_id,
                    "created_at": api["now"](), "status": "prepared", "source_root": str(source),
                    "destination_root": str(destination), "destination_project_relative": destination.relative_to(api["PROJECT_ROOT"]).as_posix(),
                    "reason": "Launcher e ferramentas usavam ~/ES-DE como raiz de dados, fora da estrutura portátil oficial.",
                    "before": before, "expected_after": after,
                    "items": [row for row in before if row["kind"] == "file"],
                    "rollback_policy": "Restore dependent configuration changes first; exact tree hashes must match before reverse rename; no overwrite or deletion.",
                    "summary": {"files": sum(r["kind"] == "file" for r in before),
                                "bytes": sum(r.get("size_bytes", 0) for r in before), "media_payloads_copied": 0}}
        path = opdir / "manifest.json"
        api["atomic_json"](path, manifest)
        api["append_event"](operation_id, "esde_userdata_planned", action="esde_userdata_plan", status="prepared",
                            source=str(source), destination=str(destination), details=manifest["summary"])
    else:
        operation_id = args.operation_id
        opdir = api["operation_dir"](operation_id)
        path = opdir / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (manifest.get("kind") != "esde_userdata_migration" or manifest.get("source_root") != str(source)
                or manifest.get("destination_root") != str(destination)):
            raise ValueError("Manifesto não corresponde à migração canônica dos dados ES-DE deste host.")
        if command in {"apply", "rollback"}:
            if not getattr(args, command, False):
                raise ValueError(f"A operação requer --{command} explícito.")
            running = _running_frontends(api)
            if running:
                raise RuntimeError("Feche os frontends normalmente: " + ", ".join(running))
        if command == "apply":
            if manifest["status"] != "prepared" or os.path.lexists(destination):
                raise ValueError("Plano não preparado ou destino já existente.")
            if _userdata_snapshot(source, media_root) != manifest["before"]:
                raise ValueError("Dados ES-DE mudaram desde o plano; prepare um novo plano.")
            api["append_event"](operation_id, "esde_userdata_apply_started", action="rename_tree", status="applying")
            os.rename(source, destination)
            try:
                alias = destination / "downloaded_media"
                if alias.is_symlink():
                    alias.unlink()
                    alias.symlink_to(os.path.relpath(media_root, destination), target_is_directory=True)
                if _userdata_snapshot(destination, media_root) != manifest["expected_after"]:
                    raise ValueError("A árvore migrada diverge do snapshot planejado.")
            except Exception:
                alias = destination / "downloaded_media"
                old = next((r for r in manifest["before"] if r["kind"] == "symlink"), None)
                if old:
                    if alias.is_symlink():
                        alias.unlink()
                    alias.symlink_to(old["target"], target_is_directory=True)
                os.rename(destination, source)
                api["append_event"](operation_id, "esde_userdata_apply_reverted", action="rename_tree", status="rolled_back_after_failure")
                raise
            manifest.update(status="applied", applied_at=api["now"]())
        elif command == "verify":
            if manifest["status"] not in {"applied", "verified"}:
                raise ValueError("Migração de dados ES-DE não aplicada.")
            if os.path.lexists(source) or _userdata_snapshot(destination, media_root) != manifest["expected_after"]:
                raise ValueError("Origem recriada ou snapshot de destino divergente.")
            manifest.update(status="verified", verified_at=api["now"]())
        elif command == "rollback":
            if manifest["status"] not in {"applied", "verified"} or os.path.lexists(source):
                raise ValueError("Estado não reversível ou origem já existente.")
            if _userdata_snapshot(destination, media_root) != manifest["expected_after"]:
                raise ValueError("Dados alterados após migração: restaure primeiro as mudanças dependentes registradas.")
            alias = destination / "downloaded_media"
            old = next((r for r in manifest["before"] if r["kind"] == "symlink"), None)
            if old:
                alias.unlink()
                alias.symlink_to(old["target"], target_is_directory=True)
            try:
                os.rename(destination, source)
            except Exception:
                if old:
                    alias.unlink()
                    alias.symlink_to(os.path.relpath(media_root, destination), target_is_directory=True)
                raise
            if _userdata_snapshot(source, media_root) != manifest["before"]:
                raise RuntimeError("Snapshot de rollback divergente; preserve a árvore e investigue.")
            manifest.update(status="rolled_back", rolled_back_at=api["now"]())
        api["atomic_json"](path, manifest)
        api["append_event"](operation_id, f"esde_userdata_{command}", action=f"esde_userdata_{command}",
                            status=manifest["status"], source=str(source), destination=str(destination), details=manifest["summary"])
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": manifest["kind"], "status": manifest["status"],
            "manifest": str(path), "summary": manifest["summary"]}


def _hash(path: Path, api: dict) -> str:
    return api["sha256_file"](path)


def _no_symlink_components(path: Path, stop: Path) -> None:
    absolute = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(stop))
    if absolute != boundary and boundary not in absolute.parents:
        raise ValueError(f"Caminho saiu da raiz autorizada: {path}")
    cursor = absolute
    while cursor != boundary:
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink recusado: {cursor}")
        cursor = cursor.parent
    if boundary.is_symlink():
        raise ValueError(f"Raiz symlink recusada: {boundary}")


def _home_source(api: dict, value: str | None) -> Path:
    raw = Path(value).expanduser() if value else Path.home() / "ES-DE" / "downloaded_media"
    if not raw.is_absolute():
        raise ValueError("A origem ES-DE precisa ser um caminho absoluto.")
    source = Path(os.path.abspath(raw))
    if source.name.casefold() != "downloaded_media" or source.parent.name.casefold() != "es-de":
        raise ValueError("A migração só aceita <home>/ES-DE/downloaded_media; não movo outras bibliotecas.")
    if source.parent.parent != Path.home().resolve():
        raise ValueError("A origem precisa estar diretamente sob a pasta ES-DE do usuário atual.")
    _no_symlink_components(source, source.parent.parent)
    if not source.is_dir():
        raise FileNotFoundError(f"Pasta de mídia ES-DE ausente: {source}")
    return source


def _platform_ids(project_root: Path) -> set[str]:
    catalog_path = project_root / "frontend" / "RetroFE" / "Config" / "platform-catalog.json"
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    return {row["id"] for row in data.get("systems", []) if isinstance(row, dict) and isinstance(row.get("id"), str)}


def _quality_decision(source: Path, destination: Path, api: dict) -> tuple[str, dict]:
    """Select a primary only when media metrics show a strict, comparable winner."""
    ext = source.suffix.casefold()
    evidence: dict[str, Any] = {"method": "conservative-metadata-comparison", "decision": "preserve_both"}
    try:
        optimizer = api["media_optimizer"]
        if ext in {".png", ".jpg", ".jpeg"}:
            sm = optimizer._probe_image(source)["frames"]
            dm = optimizer._probe_image(destination)["frames"]
            if len(sm) != 1 or len(dm) != 1:
                return "preserve_both", {**evidence, "reason": "multi-frame or multi-page image"}
            s, d = sm[0], dm[0]
            sw, sh, dw, dh = int(s["width"]), int(s["height"]), int(d["width"]), int(d["height"])
            sr, dr = sw / max(sh, 1), dw / max(dh, 1)
            comparable = abs(sr - dr) <= max(0.01, max(sr, dr) * 0.01)
            evidence.update(source_image=s, destination_image=d, same_aspect_ratio=comparable)
            if comparable and sw * sh != dw * dh:
                winner = "source_preferred" if sw * sh > dw * dh else "destination_preferred"
                evidence.update(decision=winner, reason="higher pixel count at comparable aspect ratio")
                return winner, evidence
            return "preserve_both", {**evidence, "reason": "resolution tie or aspect-ratio difference; neither file can be proven better"}
        if ext == ".mp4":
            sm = optimizer._probe_av(source)
            dm = optimizer._probe_av(destination)
            sv = next((x for x in sm.get("streams", []) if x.get("codec_type") == "video"), None)
            dv = next((x for x in dm.get("streams", []) if x.get("codec_type") == "video"), None)
            if not sv or not dv:
                return "preserve_both", {**evidence, "reason": "video stream not uniquely comparable"}
            sduration, dduration = float(sm.get("duration") or 0), float(dm.get("duration") or 0)
            close_duration = abs(sduration - dduration) <= max(1.0, max(sduration, dduration) * 0.02)
            def fps(value: str) -> float:
                try:
                    numerator, denominator = value.split("/", 1)
                    return float(numerator) / float(denominator)
                except Exception:
                    return 0.0
            s_metrics = (int(sv.get("width", 0)) * int(sv.get("height", 0)), fps(str(sv.get("avg_frame_rate", "0/1"))), int(sm.get("bit_rate") or 0))
            d_metrics = (int(dv.get("width", 0)) * int(dv.get("height", 0)), fps(str(dv.get("avg_frame_rate", "0/1"))), int(dm.get("bit_rate") or 0))
            evidence.update(source_video=sv, destination_video=dv, source_duration=sduration,
                            destination_duration=dduration, comparable_duration=close_duration,
                            source_quality_tuple=s_metrics, destination_quality_tuple=d_metrics)
            if close_duration and all(a >= b for a, b in zip(s_metrics, d_metrics)) and any(a > b for a, b in zip(s_metrics, d_metrics)):
                evidence.update(decision="source_preferred", reason="video metrics dominate at matching duration")
                return "source_preferred", evidence
            if close_duration and all(b >= a for a, b in zip(s_metrics, d_metrics)) and any(b > a for a, b in zip(s_metrics, d_metrics)):
                evidence.update(decision="destination_preferred", reason="video metrics dominate at matching duration")
                return "destination_preferred", evidence
            return "preserve_both", {**evidence, "reason": "different duration or quality metrics trade off"}
    except Exception as exc:
        return "preserve_both", {**evidence, "reason": f"quality probe unavailable: {type(exc).__name__}: {exc}"}
    return "preserve_both", {**evidence, "reason": "format has no safe automatic quality comparison"}


def _alt_path(path: Path, reserved: set[str]) -> Path:
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem} (Alt {index}){path.suffix}")
        key = str(candidate).casefold()
        if key not in reserved and not os.path.lexists(candidate):
            reserved.add(key)
            return candidate
        index += 1


def _running_frontends(api: dict) -> list[str]:
    running = []
    if api.get("esde_processes_running"):
        running.extend(f"ES-DE PID {row.get('pid', '?')}" for row in api["esde_processes_running"]())
    try:
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cmdline = (proc / "cmdline").read_bytes().decode(errors="replace")
            except OSError:
                continue
            if "ES-DE_x64_SteamDeck.AppImage" in cmdline and str(api["PROJECT_ROOT"] / "frontend" / "ES-DE") in cmdline:
                running.append(f"ES-DE PID {proc.name}")
    except OSError:
        pass
    if api["retrofe_processes_running"]():
        running.append("RetroFE")
    return running


def _operation(args, api: dict) -> tuple[dict, Path]:
    opdir = api["operation_dir"](args.operation_id)
    manifest_path = opdir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "esde_media_migration":
        raise ValueError(f"Operação não é migração de mídia ES-DE: {args.operation_id}")
    return manifest, manifest_path


def command_plan(args, api: dict) -> dict:
    source_root = _home_source(api, args.source_root)
    project_root = api["PROJECT_ROOT"].resolve(strict=True)
    media_root = api["MEDIA_DEST_ROOT"].resolve(strict=True)
    _no_symlink_components(media_root, project_root)
    valid_systems = _platform_ids(project_root)
    operation_id = OPERATION_PREFIX + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    operation_path = api["operation_dir"](operation_id)
    operation_path.mkdir(parents=True, exist_ok=False)
    rows: list[dict] = []
    reserved: set[str] = set()
    source_targets: dict[str, list[int]] = {}
    directories_to_create: set[tuple[str, str]] = set()

    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        rel = path.relative_to(source_root)
        row: dict[str, Any] = {"source_relative": rel.as_posix(), "source": str(path), "status": "blocked", "reason": ""}
        try:
            if path.is_symlink():
                row["reason"] = "symlink source is not followed or migrated"
            elif path.is_dir():
                continue
            elif not path.is_file():
                row["reason"] = "source is not a regular file"
            elif len(rel.parts) < 3:
                row["reason"] = "expected <system>/<ES-DE media type>/<file>"
            elif rel.parts[0] not in valid_systems:
                row["reason"] = f"system id absent from active platform catalog: {rel.parts[0]}"
            elif rel.parts[1] not in MEDIA_TYPE_MAP:
                row["reason"] = f"unmapped ES-DE media type: {rel.parts[1]}"
            elif path.suffix.casefold() not in ALLOWED_EXTENSIONS:
                row["reason"] = f"format is outside the reviewed ES-DE migration allowlist: {path.suffix}"
            else:
                canonical_type = MEDIA_TYPE_MAP[rel.parts[1]]
                target_rel = Path(rel.parts[0], canonical_type, *rel.parts[2:])
                target = media_root / target_rel
                _no_symlink_components(target, media_root)
                st = path.stat()
                if st.st_size <= 0:
                    row["reason"] = "empty media file remains at source for explicit review"
                else:
                    row.update(system_id=rel.parts[0], source_media_type=rel.parts[1], media_type=canonical_type,
                               destination_relative=target_rel.as_posix(), destination=str(target),
                               size_bytes=st.st_size, mtime_ns=st.st_mtime_ns, inode=st.st_ino,
                               device=st.st_dev, source_sha256=_hash(path, api), status="planned_move")
                    directories_to_create.add((rel.parts[0], canonical_type))
                    if os.path.lexists(target):
                        if target.is_symlink() or not target.is_file():
                            row.update(status="blocked", reason="destination exists but is not a regular file")
                        else:
                            target_st = target.stat()
                            row.update(destination_sha256=_hash(target, api), destination_size_bytes=target_st.st_size,
                                       destination_mtime_ns=target_st.st_mtime_ns, destination_inode=target_st.st_ino,
                                       destination_device=target_st.st_dev)
                            if row["source_sha256"] == row["destination_sha256"]:
                                row.update(status="deduplicate_exact_hash", quality_decision="identical bytes",
                                           rollback_relative=(Path("rollback") / "duplicates" / rel).as_posix())
                            else:
                                decision, evidence = _quality_decision(path, target, api)
                                row["quality_evidence"] = evidence
                                row["quality_decision"] = decision
                                if decision == "source_preferred":
                                    alt = _alt_path(target, reserved)
                                    row.update(status="source_replaces_primary", alternate_destination=str(alt),
                                               alternate_destination_relative=alt.relative_to(media_root).as_posix())
                                else:
                                    alt = _alt_path(target, reserved)
                                    row.update(status="preserve_as_alternate", alternate_destination=str(alt),
                                               alternate_destination_relative=alt.relative_to(media_root).as_posix())
                    target_key = str(target).casefold()
                    source_targets.setdefault(target_key, []).append(len(rows))
                    if row["status"] not in {"blocked", "deduplicate_exact_hash", "source_replaces_primary", "preserve_as_alternate"}:
                        row["status"] = "planned_move"
            rows.append(row)
        except Exception as exc:
            row.update(status="blocked", reason=f"{type(exc).__name__}: {exc}")
            rows.append(row)

    for indexes in source_targets.values():
        if len(indexes) > 1:
            for index in indexes:
                rows[index].update(status="blocked", reason="multiple source files map to the same canonical path")

    aliases: list[dict] = []
    systems = {system for system, _ in directories_to_create}
    for current in media_root.iterdir():
        if current.is_dir() and not current.is_symlink() and current.name != "pending":
            systems.add(current.name)
    for system_id in sorted(systems):
        categories = {child.name for child in (media_root / system_id).iterdir()
                      if child.is_dir() and not child.is_symlink()} if (media_root / system_id).is_dir() else set()
        categories.update(category for system, category in directories_to_create if system == system_id)
        for esde_type, canonical_type in sorted(MEDIA_TYPE_ALIASES.items()):
            if canonical_type not in categories:
                continue
            alias = media_root / system_id / esde_type
            target = media_root / system_id / canonical_type
            expected_link = os.path.relpath(target, alias.parent)
            if os.path.lexists(alias):
                if alias.is_symlink() and os.readlink(alias) == expected_link and alias.resolve(strict=False) == target:
                    aliases.append({"system_id": system_id, "esde_type": esde_type, "canonical_type": canonical_type,
                                    "path": str(alias), "target": expected_link, "status": "existing_managed"})
                else:
                    aliases.append({"system_id": system_id, "esde_type": esde_type, "canonical_type": canonical_type,
                                    "path": str(alias), "target": expected_link, "status": "blocked_existing_path"})
            else:
                aliases.append({"system_id": system_id, "esde_type": esde_type, "canonical_type": canonical_type,
                                "path": str(alias), "target": expected_link, "status": "planned_create"})

    blocked = sum(row.get("status") == "blocked" for row in rows)
    blocked_aliases = sum(row.get("status") == "blocked_existing_path" for row in aliases)
    manifest = {
        "schema_version": 1, "kind": "esde_media_migration", "operation_id": operation_id,
        "status": "prepared", "created_at": api["now"](), "project_root": str(project_root),
        "source_root": str(source_root), "media_root": str(media_root),
        "media_type_map": MEDIA_TYPE_MAP, "items": rows, "aliases": aliases,
        "source_root_alias": {"path": str(source_root), "target": os.path.relpath(media_root, source_root.parent),
                              "status": "planned_create" if blocked == 0 else "blocked_unclassified_sources"},
        "summary": {"source_files": len(rows), "planned_moves": sum(x.get("status") == "planned_move" for x in rows),
                    "exact_hash_duplicates": sum(x.get("status") == "deduplicate_exact_hash" for x in rows),
                    "quality_conflicts": sum(x.get("status") in {"source_replaces_primary", "preserve_as_alternate"} for x in rows),
                    "blocked_files": blocked, "aliases_to_create": sum(x.get("status") == "planned_create" for x in aliases),
                    "blocked_aliases": blocked_aliases,
                    "source_bytes": sum(int(x.get("size_bytes", 0)) for x in rows if x.get("status") != "blocked")},
        "rollback_policy": "same-filesystem rename; exact duplicates move to operation rollback; distinct conflicts remain as Alt variants",
    }
    api["atomic_json"](operation_path / "manifest.json", manifest)
    api["write_csv_atomic"](operation_path / "migration-plan.csv", rows,
        ["source_relative", "status", "system_id", "source_media_type", "media_type", "destination_relative",
         "size_bytes", "source_sha256", "destination_sha256", "quality_decision", "alternate_destination_relative", "reason"])
    api["append_event"](operation_id, "esde_media_migration_plan_prepared", action="esde_media_plan",
                        status="prepared", source=str(source_root), destination=str(media_root),
                        details=manifest["summary"] | {"manifest": str(operation_path / "manifest.json")})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": "esde_media_migration", "status": "prepared",
            "manifest": str(operation_path / "manifest.json"), "csv": str(operation_path / "migration-plan.csv"),
            "item_count": len(rows), "items": rows, "summary": manifest["summary"]}


def _move(source: Path, destination: Path, expected_hash: str, api: dict) -> None:
    if os.path.lexists(destination):
        raise FileExistsError(f"Destino já existe; nada foi sobrescrito: {destination}")
    if source.is_symlink() or not source.is_file() or _hash(source, api) != expected_hash:
        raise ValueError(f"Origem ausente, symlink ou hash alterado: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev != api["existing_path_device"](destination.parent):
        raise OSError(f"Movimento cross-device bloqueado para evitar cópia não planejada: {source} → {destination}")
    os.rename(source, destination)
    if _hash(destination, api) != expected_hash:
        raise IOError(f"Hash divergente depois do movimento: {destination}")


def _install_aliases(manifest: dict, api: dict) -> None:
    media_root = Path(manifest["media_root"])
    for alias in manifest["aliases"]:
        if alias["status"] in {"existing_managed", "created_by_operation"}:
            continue
        if alias["status"] != "planned_create":
            raise ValueError(f"Alias ES-DE bloqueado: {alias['path']}")
        path = Path(alias["path"])
        target = path.parent / alias["target"]
        _no_symlink_components(target, media_root)
        if not target.is_dir() or target.is_symlink():
            raise ValueError(f"Categoria canônica ausente ou symlink: {target}")
        if os.path.lexists(path):
            if not path.is_symlink() or os.readlink(path) != alias["target"] or path.resolve(strict=False) != target.resolve(strict=False):
                raise FileExistsError(path)
        else:
            os.symlink(alias["target"], path, target_is_directory=True)
        alias["status"] = "created_by_operation"
        api["append_event"](manifest["operation_id"], "esde_media_type_alias_created", action="relative_directory_symlink",
                            status="created", source=alias["path"], destination=str(target),
                            details={"system_id": alias["system_id"], "esde_type": alias["esde_type"],
                                     "canonical_type": alias["canonical_type"], "link_target": alias["target"]})


def _prune_empty_source(source_root: Path, media_root: Path) -> bool:
    if source_root.is_symlink():
        return source_root.resolve(strict=True) == media_root.resolve(strict=True)
    if not source_root.is_dir():
        return False
    for current, directories, files in os.walk(source_root, topdown=False, followlinks=False):
        path = Path(current)
        if files:
            continue
        if any((path / name).is_symlink() for name in directories):
            continue
        for name in directories:
            child = path / name
            try:
                child.rmdir()
            except OSError:
                pass
        if path != source_root:
            try:
                path.rmdir()
            except OSError:
                pass
    if source_root.is_dir() and not any(source_root.iterdir()):
        source_root.rmdir()
        link_target = os.path.relpath(media_root, source_root.parent)
        os.symlink(link_target, source_root, target_is_directory=True)
        return True
    return source_root.is_symlink() and source_root.resolve(strict=True) == media_root.resolve(strict=True)


def command_apply(args, api: dict) -> dict:
    if not args.apply:
        raise ValueError("Aplicação requer --apply explícito")
    manifest, manifest_path = _operation(args, api)
    if manifest.get("status") not in {"prepared", "applying", "apply_interrupted"}:
        raise ValueError(f"A operação não está preparada para aplicar: {manifest.get('status')}")
    running = _running_frontends(api)
    if running:
        raise RuntimeError("Feche os frontends antes da migração de mídia: " + ", ".join(running))
    source_root, media_root = Path(manifest["source_root"]), Path(manifest["media_root"])
    if media_root.is_symlink() or not media_root.is_dir():
        raise ValueError("Raízes mudaram desde o plano; gere um plano novo")
    if source_root.is_symlink() and source_root.resolve(strict=True) != media_root.resolve(strict=True):
        raise ValueError("O caminho legado mudou desde o plano; gere um plano novo")
    if not source_root.exists() and not source_root.is_symlink():
        raise ValueError("A origem desapareceu antes da migração")
    if any(item.get("status") == "blocked" for item in manifest["items"]) or any(a.get("status") == "blocked_existing_path" for a in manifest["aliases"]):
        raise ValueError("O plano tem itens ou aliases bloqueados; resolva-os e gere outro plano")
    opdir = api["operation_dir"](args.operation_id)
    manifest["status"] = "applying"
    api["atomic_json"](manifest_path, manifest)
    completed = {"moved_duplicate_to_rollback", "moved_to_canonical", "source_primary_existing_alternate",
                 "destination_primary_source_alternate"}
    try:
        source_alias_active = source_root.is_symlink()
        for index, item in enumerate(manifest["items"], 1):
            source = Path(item["source"])
            destination = media_root / item.get("destination_relative", "")
            prior_status = item.get("status")
            if prior_status in completed:
                continue
            if source_alias_active:
                if prior_status == "planned_move" and _hash(destination, api) == item["source_sha256"]:
                    item["status"] = "moved_to_canonical"
                elif prior_status == "deduplicate_exact_hash":
                    rollback = opdir / item["rollback_relative"]
                    if _hash(rollback, api) != item["source_sha256"] or _hash(destination, api) != item["destination_sha256"]:
                        raise ValueError(f"A origem ES-DE já aponta à raiz canônica, mas a duplicata não confere: {source}")
                    item.update(status="moved_duplicate_to_rollback", rollback_path=str(rollback))
                elif prior_status == "source_replaces_primary":
                    alternate = Path(item["alternate_destination"])
                    if _hash(destination, api) != item["source_sha256"] or _hash(alternate, api) != item["destination_sha256"]:
                        raise ValueError(f"A origem ES-DE já aponta à raiz canônica, mas o conflito não confere: {source}")
                    item["status"] = "source_primary_existing_alternate"
                elif prior_status == "preserve_as_alternate":
                    alternate = Path(item["alternate_destination"])
                    if _hash(destination, api) != item["destination_sha256"] or _hash(alternate, api) != item["source_sha256"]:
                        raise ValueError(f"A origem ES-DE já aponta à raiz canônica, mas a variante não confere: {source}")
                    item["status"] = "destination_primary_source_alternate"
                else:
                    raise ValueError(f"Origem já centralizada com estado inesperado: {prior_status}")
                continue
            if prior_status in {"planned_move", "moved_to_canonical"}:
                if source.exists():
                    _move(source, destination, item["source_sha256"], api)
                elif not (destination.is_file() and not destination.is_symlink() and _hash(destination, api) == item["source_sha256"]):
                    raise ValueError(f"Não foi possível recuperar o movimento planejado: {source}")
                item["status"] = "moved_to_canonical"
            elif prior_status in {"deduplicate_exact_hash", "moving_duplicate_to_rollback"}:
                rollback = opdir / item["rollback_relative"]
                if source.exists():
                    if _hash(destination, api) != item["destination_sha256"]:
                        raise ValueError(f"Destino duplicado mudou depois do plano: {destination}")
                    _move(source, rollback, item["source_sha256"], api)
                elif not (rollback.is_file() and _hash(rollback, api) == item["source_sha256"]
                          and destination.is_file() and _hash(destination, api) == item["destination_sha256"]):
                    raise ValueError(f"Não foi possível recuperar a deduplicação planejada: {source}")
                item.update(status="moved_duplicate_to_rollback", rollback_path=str(rollback))
            elif prior_status in {"source_replaces_primary", "moving_source_replaces_primary"}:
                alternate = Path(item["alternate_destination"])
                if source.exists():
                    if destination.exists() and _hash(destination, api) == item["destination_sha256"] and not os.path.lexists(alternate):
                        _move(destination, alternate, item["destination_sha256"], api)
                    elif not (not destination.exists() and alternate.is_file()
                              and _hash(alternate, api) == item["destination_sha256"]):
                        raise ValueError(f"Não foi possível recuperar a substituição planejada: {destination}")
                    if not destination.exists():
                        _move(source, destination, item["source_sha256"], api)
                if not (destination.is_file() and _hash(destination, api) == item["source_sha256"]
                        and alternate.is_file() and _hash(alternate, api) == item["destination_sha256"]):
                    raise ValueError(f"Substituição/alternativa não confere: {destination}")
                item["status"] = "source_primary_existing_alternate"
            elif prior_status in {"preserve_as_alternate", "moving_to_alternate"}:
                alternate = Path(item["alternate_destination"])
                if source.exists():
                    if _hash(destination, api) != item["destination_sha256"]:
                        raise ValueError(f"Destino mudou depois do plano: {destination}")
                    _move(source, alternate, item["source_sha256"], api)
                elif not (alternate.is_file() and _hash(alternate, api) == item["source_sha256"]
                          and destination.is_file() and _hash(destination, api) == item["destination_sha256"]):
                    raise ValueError(f"Não foi possível recuperar a variante planejada: {source}")
                item["status"] = "destination_primary_source_alternate"
            else:
                raise ValueError(f"Estado de migração sem ação segura: {prior_status}")
            api["append_event"](args.operation_id, "esde_media_item_moved", action=item["status"], status="moved",
                                source=item["source"], destination=item.get("destination", ""),
                                sha256_before=item["source_sha256"], sha256_after=item["source_sha256"],
                                size_before=item.get("size_bytes", ""), size_after=item.get("size_bytes", ""),
                                details={"source_relative": item["source_relative"],
                                         "destination_relative": item.get("destination_relative", ""),
                                         "alternate_destination_relative": item.get("alternate_destination_relative", ""),
                                         "quality_decision": item.get("quality_decision", "")})
            if index % 500 == 0:
                api["atomic_json"](manifest_path, manifest)
        _install_aliases(manifest, api)
        source_root_was_alias = source_root.is_symlink()
        if not _prune_empty_source(source_root, media_root):
            raise RuntimeError("A origem ainda contém entradas; mantive o caminho legado sem alias para não ocultá-las.")
        manifest["source_root_alias"]["status"] = "created_by_operation"
        manifest["status"] = "applied"
        manifest["applied_at"] = api["now"]()
    except Exception as exc:
        manifest["status"] = "apply_interrupted"
        manifest["last_apply_error"] = f"{type(exc).__name__}: {exc}"
        manifest["last_apply_error_at"] = api["now"]()
        api["atomic_json"](manifest_path, manifest)
        api["append_event"](args.operation_id, "esde_media_migration_interrupted", action="esde_media_apply",
                            status="apply_interrupted", details={"error": manifest["last_apply_error"]})
        raise
    api["atomic_json"](manifest_path, manifest)
    if not source_root_was_alias:
        api["append_event"](args.operation_id, "esde_media_legacy_alias_created", action="relative_directory_symlink",
                            status="created", source=str(source_root), destination=str(media_root),
                            details={"link_target": manifest["source_root_alias"]["target"]})
    api["append_event"](args.operation_id, "esde_media_migration_applied", action="esde_media_apply",
                        status="applied", source=str(source_root), destination=str(media_root),
                        details={"items": len(manifest["items"]), "aliases": len(manifest["aliases"])})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "kind": "esde_media_migration", "status": manifest["status"],
            "item_count": len(manifest["items"]), "manifest": str(manifest_path), "items": manifest["items"],
            "summary": {"moved": sum(x.get("status") in {"moved_to_canonical", "source_primary_existing_alternate", "destination_primary_source_alternate"} for x in manifest["items"]),
                        "exact_duplicates_preserved_for_rollback": sum(x.get("status") == "moved_duplicate_to_rollback" for x in manifest["items"]),
                        "aliases_created": sum(x.get("status") == "created_by_operation" for x in manifest["aliases"])}}


def command_verify(args, api: dict) -> dict:
    manifest, manifest_path = _operation(args, api)
    if manifest.get("status") not in {"applied", "verified", "rollback_failed"}:
        raise ValueError(f"A operação ainda não foi aplicada: {manifest.get('status')}")
    failures = []
    media_root = Path(manifest["media_root"])
    source_root = Path(manifest["source_root"])
    if not source_root.parent.exists() and appdata_root(api["PROJECT_ROOT"]).is_dir():
        raise ValueError("A raiz legada foi relocada para UserData. Consulte path-migration-map.json e a cadeia de otimização atual; não revalide um snapshot histórico contra bytes já transformados.")
    legacy_alias_active = (source_root.is_symlink()
                           and source_root.resolve(strict=True) == media_root.resolve(strict=True))
    opdir = api["operation_dir"](args.operation_id)
    for item in manifest["items"]:
        status = item.get("status")
        source = Path(item["source"])
        destination = media_root / item.get("destination_relative", "")
        try:
            if status == "moved_to_canonical":
                if (not legacy_alias_active and os.path.lexists(source)) or _hash(destination, api) != item["source_sha256"]:
                    raise ValueError("payload movido ausente, divergente ou ainda duplicado na origem")
            elif status == "moved_duplicate_to_rollback":
                rollback = Path(item["rollback_path"])
                if ((not legacy_alias_active and os.path.lexists(source)) or _hash(rollback, api) != item["source_sha256"]
                        or _hash(destination, api) != item["destination_sha256"]):
                    raise ValueError("duplicata idêntica ou payload canônico divergente")
            elif status == "source_primary_existing_alternate":
                alternate = Path(item["alternate_destination"])
                if ((not legacy_alias_active and os.path.lexists(source)) or _hash(destination, api) != item["source_sha256"]
                        or _hash(alternate, api) != item["destination_sha256"]):
                    raise ValueError("origem vencedora ou variante alternativa divergente")
            elif status == "destination_primary_source_alternate":
                alternate = Path(item["alternate_destination"])
                if ((not legacy_alias_active and os.path.lexists(source)) or _hash(destination, api) != item["destination_sha256"]
                        or _hash(alternate, api) != item["source_sha256"]):
                    raise ValueError("destino primário ou variante alternativa divergente")
            else:
                raise ValueError(f"estado de item inesperado: {status}")
        except Exception as exc:
            failures.append({"source": item.get("source_relative"), "error": f"{type(exc).__name__}: {exc}"})
    for alias in manifest["aliases"]:
        path = Path(alias["path"])
        target = path.parent / alias["target"]
        if alias["status"] in {"created_by_operation", "existing_managed"}:
            if not path.is_symlink() or os.readlink(path) != alias["target"] or path.resolve(strict=True) != target.resolve(strict=True):
                failures.append({"source": alias["path"], "error": "ES-DE media-type alias is not the planned relative symlink"})
    if manifest.get("source_root_alias", {}).get("status") != "blocked_unclassified_sources":
        expected = manifest["source_root_alias"]["target"]
        if not source_root.is_symlink() or os.readlink(source_root) != expected or source_root.resolve(strict=True) != media_root.resolve(strict=True):
            failures.append({"source": str(source_root), "error": "legacy downloaded_media path does not resolve to canonical media root"})
    if failures:
        manifest["status"] = "verification_failed"
    else:
        manifest["status"] = "verified"
    manifest["verified_at"] = api["now"]()
    manifest["verification_failures"] = failures
    api["atomic_json"](manifest_path, manifest)
    api["write_csv_atomic"](opdir / "migration-verification.csv", failures,
                            ["source", "error"])
    api["append_event"](args.operation_id, "esde_media_migration_verified", action="esde_media_verify",
                        status=manifest["status"], details={"failures": len(failures),
                                                            "report": str(opdir / "migration-verification.csv")})
    api["rebuild_history_index"]()
    verification_items = [{"status": "verification_failed" if failures else "verified"} for _ in manifest["items"]]
    return {"operation_id": args.operation_id, "kind": "esde_media_migration", "status": manifest["status"],
            "item_count": len(manifest["items"]), "items": verification_items,
            "summary": {"failures": len(failures)},
            "report": str(opdir / "migration-verification.csv"), "manifest": str(manifest_path)}


def command_rollback(args, api: dict) -> dict:
    if not args.rollback:
        raise ValueError("Rollback requer --rollback explícito")
    manifest, manifest_path = _operation(args, api)
    running = _running_frontends(api)
    if running:
        raise RuntimeError("Feche os frontends antes do rollback: " + ", ".join(running))
    if manifest.get("status") not in {"applied", "verified", "verification_failed", "rollback_failed",
                                       "applying", "apply_interrupted", "rollbacking", "rollback_interrupted"}:
        raise ValueError(f"A operação não está em estado reversível: {manifest.get('status')}")
    source_root, media_root = Path(manifest["source_root"]), Path(manifest["media_root"])
    if not source_root.parent.exists() and appdata_root(api["PROJECT_ROOT"]).is_dir():
        raise ValueError("Reverta primeiro otimizações dependentes, configuração e a migração UserData; não vou recriar ES-DE no home por uma operação histórica.")
    if source_root.is_symlink():
        expected = manifest["source_root_alias"]["target"]
        if os.readlink(source_root) != expected or source_root.resolve(strict=True) != media_root.resolve(strict=True):
            raise ValueError("Alias legado mudou; rollback bloqueado")
        source_root.unlink()
    source_root.mkdir(parents=True, exist_ok=True)
    manifest["status"] = "rollbacking"
    api["atomic_json"](manifest_path, manifest)
    try:
        for alias in reversed(manifest["aliases"]):
            if alias["status"] not in {"created_by_operation", "planned_create"}:
                continue
            path = Path(alias["path"])
            if os.path.lexists(path):
                if not path.is_symlink() or os.readlink(path) != alias["target"]:
                    raise ValueError(f"Alias mudou desde a aplicação; rollback bloqueado: {path}")
                path.unlink()
            alias["status"] = "removed_by_rollback"
        for index, item in enumerate(reversed(manifest["items"]), 1):
            status = item.get("status")
            source = Path(item["source"])
            destination = media_root / item.get("destination_relative", "")
            expected_source = item["source_sha256"]
            expected_destination = item.get("destination_sha256", "")
            alternate = Path(item["alternate_destination"]) if item.get("alternate_destination") else None
            rollback_path = Path(item["rollback_path"]) if item.get("rollback_path") else (
                api["operation_dir"](args.operation_id) / item["rollback_relative"] if item.get("rollback_relative") else None)

            def regular_hash(path: Path | None) -> str | None:
                if path is None or path.is_symlink() or not path.is_file():
                    return None
                return _hash(path, api)

            if status == "rolled_back":
                continue
            if status in {"planned_move", "moved_to_canonical", "moving_to_canonical"}:
                source_hash, destination_hash = regular_hash(source), regular_hash(destination)
                if source_hash == expected_source and destination_hash is None:
                    pass
                elif source_hash is None and destination_hash == expected_source:
                    _move(destination, source, expected_source, api)
                else:
                    raise ValueError(f"Move não pôde ser revertido sem sobrescrever dados: {source}")
            elif status in {"deduplicate_exact_hash", "moving_duplicate_to_rollback", "moved_duplicate_to_rollback"}:
                source_hash, backup_hash = regular_hash(source), regular_hash(rollback_path)
                if source_hash == expected_source and backup_hash is None:
                    pass
                elif source_hash is None and backup_hash == expected_source and rollback_path is not None:
                    _move(rollback_path, source, expected_source, api)
                else:
                    raise ValueError(f"Duplicata reversível não confere: {source}")
            elif status in {"source_replaces_primary", "moving_source_replaces_primary", "source_primary_existing_alternate"}:
                source_hash, destination_hash, alternate_hash = regular_hash(source), regular_hash(destination), regular_hash(alternate)
                if source_hash == expected_source and destination_hash == expected_destination and alternate_hash is None:
                    pass
                elif source_hash is None and destination_hash == expected_source and alternate_hash == expected_destination and alternate is not None:
                    _move(destination, source, expected_source, api)
                    _move(alternate, destination, expected_destination, api)
                elif source_hash == expected_source and destination_hash is None and alternate_hash == expected_destination and alternate is not None:
                    _move(alternate, destination, expected_destination, api)
                else:
                    raise ValueError(f"Conflito com alternativa não pôde ser revertido: {source}")
            elif status in {"preserve_as_alternate", "moving_to_alternate", "destination_primary_source_alternate"}:
                source_hash, destination_hash, alternate_hash = regular_hash(source), regular_hash(destination), regular_hash(alternate)
                if source_hash == expected_source and alternate_hash is None and destination_hash == expected_destination:
                    pass
                elif source_hash is None and alternate_hash == expected_source and destination_hash == expected_destination and alternate is not None:
                    _move(alternate, source, expected_source, api)
                else:
                    raise ValueError(f"Variante alternativa não pôde ser revertida: {source}")
            else:
                raise ValueError(f"Estado sem rollback implementado: {status}")
            item["status"] = "rolled_back"
            api["append_event"](args.operation_id, "esde_media_item_rolled_back", action="esde_media_rollback",
                                status="rolled_back", source=str(source), destination=str(destination),
                                sha256_before=expected_source, sha256_after=expected_source,
                                details={"source_relative": item["source_relative"]})
            if index % 500 == 0:
                api["atomic_json"](manifest_path, manifest)
        manifest["status"] = "rolled_back"
        manifest["rolled_back_at"] = api["now"]()
    except Exception as exc:
        manifest["status"] = "rollback_interrupted"
        manifest["last_rollback_error"] = f"{type(exc).__name__}: {exc}"
        manifest["last_rollback_error_at"] = api["now"]()
        api["atomic_json"](manifest_path, manifest)
        api["append_event"](args.operation_id, "esde_media_rollback_interrupted", action="esde_media_rollback",
                            status="rollback_interrupted", details={"error": manifest["last_rollback_error"]})
        raise
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "esde_media_migration_rolled_back", action="esde_media_rollback",
                        status="rolled_back", details={"items": len(manifest["items"])})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "kind": "esde_media_migration", "status": "rolled_back",
            "item_count": len(manifest["items"]), "items": manifest["items"], "manifest": str(manifest_path)}


def command_validate(args, api: dict) -> dict:
    """Read-only launch gate for the single shared media tree and its ES-DE view."""
    media_root = Path(api["MEDIA_DEST_ROOT"]).resolve(strict=True)
    issues: list[str] = []
    for system_dir in sorted(media_root.iterdir(), key=lambda path: path.name.casefold()):
        if system_dir.name == "pending" or not system_dir.is_dir() or system_dir.is_symlink():
            continue
        categories = {child.name for child in system_dir.iterdir() if child.is_dir() and not child.is_symlink()}
        for esde_type, canonical_type in MEDIA_TYPE_ALIASES.items():
            if canonical_type not in categories:
                continue
            alias = system_dir / esde_type
            expected = os.path.relpath(system_dir / canonical_type, system_dir)
            if not alias.is_symlink() or os.readlink(alias) != expected or alias.resolve(strict=False) != (system_dir / canonical_type).resolve(strict=False):
                issues.append(f"{system_dir.name}/{esde_type} precisa ser symlink relativo para {canonical_type}")
    source_root = Path.home() / "ES-DE" / "downloaded_media"
    if source_root.is_symlink():
        if source_root.resolve(strict=True) != media_root:
            issues.append("~/ES-DE/downloaded_media aponta para fora da raiz canônica media/")
    elif source_root.exists():
        issues.append("~/ES-DE/downloaded_media ainda é um diretório separado; execute media esde plan/apply")
    canonical_data = appdata_root(api["PROJECT_ROOT"])
    if canonical_data.exists() and (Path.home() / "ES-DE").exists():
        issues.append("Duas raízes de dados ES-DE presentes; reconcilie a migração userdata antes de iniciar")
    local_media = canonical_data / "downloaded_media"
    if os.path.lexists(local_media) and (not local_media.is_symlink() or os.path.isabs(os.readlink(local_media))
                                       or local_media.resolve(strict=True) != media_root):
        issues.append("UserData/downloaded_media precisa apontar por link relativo para media/")
    if issues:
        return {"status": "blocked", "issues": issues, "media_root": str(media_root)}
    return {"status": "ready", "media_root": str(media_root), "platform_directories": sum(
        child.is_dir() and not child.is_symlink() and child.name != "pending" for child in media_root.iterdir())}


def dispatch(args, api: dict) -> dict:
    if args.esde_media_command == "userdata":
        return command_userdata(args, api)
    if args.esde_media_command == "validate":
        return command_validate(args, api)
    if args.esde_media_command == "aliases":
        return dispatch_aliases(args, api)
    if args.esde_media_command == "plan":
        return command_plan(args, api)
    if args.esde_media_command == "apply":
        return command_apply(args, api)
    if args.esde_media_command == "verify":
        return command_verify(args, api)
    if args.esde_media_command == "rollback":
        return command_rollback(args, api)
    raise ValueError(f"Comando de reconciliação ES-DE desconhecido: {args.esde_media_command}")


# --- F02: managed ES-DE type aliases without a legacy source -----------------------------------
ALIAS_OPERATION_PREFIX = "esde-media-aliases-"
ALIAS_KIND = "esde_media_aliases"


def _alias_rows(media_root: Path) -> list[dict]:
    """Expected ES-DE aliases for every canonical category; paths relative to media_root."""
    rows = []
    for system_dir in sorted(media_root.iterdir(), key=lambda path: path.name.casefold()):
        if system_dir.name == "pending" or system_dir.is_symlink() or not system_dir.is_dir():
            continue
        categories = {child.name for child in system_dir.iterdir() if child.is_dir() and not child.is_symlink()}
        for esde_type, canonical_type in sorted(MEDIA_TYPE_ALIASES.items()):
            if canonical_type not in categories:
                continue
            alias = system_dir / esde_type
            row = {"system_id": system_dir.name, "esde_type": esde_type, "canonical_type": canonical_type,
                   "path_relative": alias.relative_to(media_root).as_posix(), "target": canonical_type}
            if os.path.lexists(alias):
                managed = (alias.is_symlink() and os.readlink(alias) == canonical_type
                           and alias.resolve(strict=False) == (system_dir / canonical_type).resolve(strict=False))
                row["status"] = "existing_managed" if managed else "blocked_existing_path"
                if not managed:
                    row["reason"] = ("symlink com alvo divergente" if alias.is_symlink()
                                     else "arquivo ou diretório real ocupa o nome do alias")
            else:
                row["status"] = "planned_create"
            rows.append(row)
    return rows


def _alias_operation(args, api: dict) -> tuple[dict, Path]:
    path = api["operation_dir"](args.operation_id) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != ALIAS_KIND:
        raise ValueError(f"Operação não é de aliases ES-DE: {args.operation_id}")
    return manifest, path


def command_aliases_plan(args, api: dict) -> dict:
    project_root = api["PROJECT_ROOT"].resolve(strict=True)
    media_root = api["MEDIA_DEST_ROOT"].resolve(strict=True)
    _no_symlink_components(media_root, project_root)
    rows = _alias_rows(media_root)
    summary = {status: sum(row["status"] == status for row in rows)
               for status in ("planned_create", "existing_managed", "blocked_existing_path")}
    if not summary["planned_create"]:
        return {"operation_id": None, "kind": ALIAS_KIND, "status": "blocked" if summary["blocked_existing_path"] else "ready",
                "summary": summary, "items": [row for row in rows if row["status"] != "existing_managed"],
                "issues": [f"{row['path_relative']}: {row['reason']}" for row in rows if row["status"] == "blocked_existing_path"]}
    operation_id = ALIAS_OPERATION_PREFIX + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    opdir = api["operation_dir"](operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "kind": ALIAS_KIND, "operation_id": operation_id, "status": "prepared",
                "created_at": api["now"](), "media_root_relative": os.path.relpath(media_root, project_root),
                "reason": "Criar somente aliases relativos ES-DE para categorias canônicas existentes; sem mover payload.",
                "items": rows, "summary": summary,
                "rollback_policy": "remove somente symlinks criados por esta operação cujo literal ainda é o registrado"}
    api["atomic_json"](opdir / "manifest.json", manifest)
    api["append_event"](operation_id, "esde_media_aliases_plan_prepared", action="esde_media_aliases_plan",
                        status="prepared", source=str(media_root), destination=str(media_root), details=summary)
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": ALIAS_KIND, "status": "prepared",
            "manifest": str(opdir / "manifest.json"), "summary": summary,
            "items": [row for row in rows if row["status"] != "existing_managed"]}


def command_aliases_apply(args, api: dict) -> dict:
    if not getattr(args, "apply", False):
        raise ValueError("Use --apply para criar os aliases revisados.")
    manifest, path = _alias_operation(args, api)
    if manifest.get("status") not in {"prepared", "apply_interrupted"}:
        raise ValueError(f"A operação não está preparada: {manifest.get('status')}")
    if any(row["status"] == "blocked_existing_path" for row in manifest["items"]):
        raise ValueError("O plano tem aliases bloqueados; resolva-os e gere outro plano")
    running = _running_frontends(api)
    if running:
        raise RuntimeError("Feche os frontends antes de aplicar: " + ", ".join(running))
    project_root = api["PROJECT_ROOT"].resolve(strict=True)
    media_root = (project_root / manifest["media_root_relative"]).resolve(strict=True)
    if media_root != api["MEDIA_DEST_ROOT"].resolve(strict=True):
        raise ValueError("A raiz de mídia mudou desde o plano")
    manifest["status"] = "applying"
    api["atomic_json"](path, manifest)
    try:
        for row in manifest["items"]:
            if row["status"] != "planned_create":
                continue
            alias = media_root / row["path_relative"]
            target = alias.parent / row["target"]
            _no_symlink_components(target, media_root)
            if not target.is_dir() or target.is_symlink():
                raise ValueError(f"Categoria canônica ausente ou symlink: {target}")
            if os.path.lexists(alias):
                raise FileExistsError(f"Alias ocupado desde o plano; nada sobrescrito: {alias}")
            os.symlink(row["target"], alias, target_is_directory=True)
            row["status"] = "created_by_operation"
            api["atomic_json"](path, manifest)
            api["append_event"](manifest["operation_id"], "esde_media_type_alias_created", action="relative_directory_symlink",
                                status="created", source=str(alias), destination=str(target),
                                details={k: row[k] for k in ("system_id", "esde_type", "canonical_type", "target")})
    except Exception:
        manifest["status"] = "apply_interrupted"
        api["atomic_json"](path, manifest)
        raise
    manifest["status"] = "applied"
    manifest["applied_at"] = api["now"]()
    api["atomic_json"](path, manifest)
    api["rebuild_history_index"]()
    return command_aliases_verify(args, api)


def command_aliases_verify(args, api: dict) -> dict:
    manifest, path = _alias_operation(args, api)
    media_root = (api["PROJECT_ROOT"].resolve(strict=True) / manifest["media_root_relative"]).resolve(strict=True)
    failed = []
    for row in manifest["items"]:
        if row["status"] not in {"created_by_operation", "existing_managed"}:
            continue
        alias = media_root / row["path_relative"]
        if not (alias.is_symlink() and os.readlink(alias) == row["target"] and alias.resolve(strict=True) == (alias.parent / row["target"]).resolve(strict=True)):
            failed.append(row["path_relative"])
    gate = command_validate(args, api)
    status = "verified" if not failed and gate["status"] == "ready" else ("rolled_back" if manifest["status"] == "rolled_back" else "failed")
    if manifest["status"] in {"applied", "verified"}:
        manifest["status"] = "verified" if status == "verified" else "verify_failed"
        manifest["verified_at"] = api["now"]()
        api["atomic_json"](path, manifest)
    return {"operation_id": manifest["operation_id"], "kind": ALIAS_KIND, "status": status, "failed": failed,
            "issues": gate.get("issues", []), "summary": manifest["summary"], "items": manifest["items"]}


def command_aliases_rollback(args, api: dict) -> dict:
    if not getattr(args, "rollback", False):
        raise ValueError("Use --rollback para remover os aliases criados por esta operação.")
    manifest, path = _alias_operation(args, api)
    if _running_frontends(api):
        raise RuntimeError("Feche os frontends antes de reverter")
    media_root = (api["PROJECT_ROOT"].resolve(strict=True) / manifest["media_root_relative"]).resolve(strict=True)
    for row in manifest["items"]:
        if row["status"] != "created_by_operation":
            continue
        alias = media_root / row["path_relative"]
        if not alias.is_symlink() or os.readlink(alias) != row["target"]:
            raise RuntimeError(f"Alias alterado depois da operação; rollback recusado: {alias}")
    for row in manifest["items"]:
        if row["status"] == "created_by_operation":
            (media_root / row["path_relative"]).unlink()
            row["status"] = "rolled_back"
    manifest["status"] = "rolled_back"
    manifest["rolled_back_at"] = api["now"]()
    api["atomic_json"](path, manifest)
    api["append_event"](manifest["operation_id"], "esde_media_aliases_rolled_back", action="esde_media_aliases_rollback",
                        status="rolled_back", source=str(media_root), destination=str(media_root), details={})
    api["rebuild_history_index"]()
    return {"operation_id": manifest["operation_id"], "kind": ALIAS_KIND, "status": "rolled_back", "items": manifest["items"]}


def command_aliases_ensure(args, api: dict) -> dict:
    """Idempotent launcher step: create only missing managed aliases; anything divergent blocks."""
    plan = command_aliases_plan(args, api)
    if plan["status"] != "prepared":
        return plan
    args.operation_id, args.apply = plan["operation_id"], True
    return command_aliases_apply(args, api)


def dispatch_aliases(args, api: dict) -> dict:
    return {"plan": command_aliases_plan, "apply": command_aliases_apply, "verify": command_aliases_verify,
            "rollback": command_aliases_rollback, "ensure": command_aliases_ensure}[args.esde_aliases_command](args, api)
