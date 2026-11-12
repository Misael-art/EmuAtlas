#!/usr/bin/env python3
"""Portable static maintenance report and a session-scoped loopback execution bridge.

No library decisions or conversions live here: existing tools own their contracts.
"""
from __future__ import annotations

import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import fcntl
from functools import lru_cache
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import uuid
import webbrowser

if __package__:
    from . import preview_games
else:
    import preview_games

ROOT = Path(__file__).resolve().parents[2]
TOOL = Path(__file__).resolve()
REPORT = Path("outputs/preview/index.html")
BASE = Path("outputs/roms-consolidacao-2026-10-24")
OPS = BASE / "operations"
VERSION = "1.2"
MAX_BATCH = 100
READY_STATES = {"prepared", "planned"}
RT = "tools/roms/romtool.py"
PY = sys.executable


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(2**20), b""):
            value.update(chunk)
    return value.hexdigest()


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {} if default is None else default


def read_csv(path):
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def atomic(path, data, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_links(path)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            if private:
                os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def json_write(path, value, private=False):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(), private)


def reject_links(path):
    for part in (Path(path).absolute(), *Path(path).absolute().parents):
        if part.is_symlink():
            raise ValueError(f"Caminho simbólico não permitido: {part}")


@lru_cache(maxsize=8)
def migration_rules(root):
    return load(root / OPS / "path-migration-map.json")


def project_path(root, raw):
    """Rebase old project-absolute records without rewriting historical evidence."""
    path = Path(str(raw or ""))
    if path.is_absolute():
        migration = migration_rules(root)
        value = str(path)
        for rule in migration.get("mappings", []):
            old = rule.get("old_prefix", "")
            if not old or not (value.startswith(old) if old.endswith("/") else value == old):
                continue
            target = rule.get("new_project_relative")
            if target is None:
                try:
                    target = str(Path(rule["new_prefix"]).relative_to(migration["canonical_root"]))
                except (KeyError, ValueError):
                    continue
            return root / target / value[len(old):] if old.endswith("/") else root / target
        # A current absolute path may be below an ancestor named "emulation"
        # (including private disk-backed test/work directories). Keep that path
        # intact; only unmatched historical roots need the legacy name fallback.
        if path.is_relative_to(root):
            return path
        parts = path.parts
        if "emulation" in parts:
            path = Path(*parts[parts.index("emulation") + 1:])
        else:
            return path
    return root / path


def relative(root, path):
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return str(path)


def census(root):
    """A current stat census, never a second persistent inventory or a ROM test."""
    rows = collections.defaultdict(lambda: dict(files=0, bytes=0, links=0, extensions=collections.Counter(), samples=[]))
    errors = []
    if not root.is_dir():
        return rows, [f"Raiz indisponível: {root.name}"]
    for current, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: errors.append(str(e))):
        parent = Path(current)
        for name in list(dirs):
            path = parent / name
            if path.is_symlink():
                dirs.remove(name)
                key = path.relative_to(root).parts[0]
                rows[key]["links"] += 1
        for name in files:
            path = parent / name
            key = path.relative_to(root).parts[0]
            if path.is_symlink():
                rows[key]["links"] += 1
                continue
            try:
                stat = path.stat()
                rows[key]["files"] += 1
                rows[key]["bytes"] += stat.st_size
                rows[key]["extensions"][path.suffix.lower()] += 1
                if len(rows[key]["samples"]) < 6:
                    rows[key]["samples"].append(str(path.relative_to(root.parent)))
            except OSError as exc:
                errors.append(str(exc))
    return rows, errors


def action_id(*parts):
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()[:20]


def command(*args):
    return [PY, *map(str, args)]


def builtin_actions(platforms):
    actions = []
    def add(tab, key, title, argv, system="", effect="diagnosticar", reason="Consulta as ferramentas oficiais; não altera payloads."):
        actions.append(dict(id=key, tab=tab, title=title, system_id=system, effect=effect,
            count=1, ready=True, reason=reason, blocked_reason="", commands=[argv],
            mutation=False, source="", destination="", operation_id="", manifest="", items=[],
            manifest_sha256="", bytes=0, savings_bytes=None))
    add("roms", "roms-plan", "Gerar plano de compactação", command(RT, "compact", "plan"), effect="planejar")
    add("midias", "media-inventory", "Inventariar mídia canônica", command(RT, "media", "optimize", "inventory"))
    add("midias", "media-covers-audit", "Diagnosticar capas e encartes", command(RT, "media", "covers", "audit"))
    add("emuladores", "emulator-audit", "Auditar todos os emuladores", command("tools/roms/emutool.py", "--format", "json"))
    add("emuladores", "firmware-status", "Diagnosticar firmware PS3", command("tools/roms/emutool.py", "firmware", "status"))
    add("frontends", "frontend-audit", "Auditar mídia dos front-ends", command("tools/roms/materialize-collection-media-paths.py", "audit"))
    add("frontends", "assets-audit", "Verificar biblioteca de assets", command("frontend/RetroFE/Tools/materializers/organize-asset-library.py", "verify"))
    add("frontends", "frontend-path-plan", "Preparar plano de apontamentos", command("tools/roms/materialize-collection-media-paths.py", "runtime-plan"), effect="planejar")
    add("frontends", "check-retrofe", "Diagnosticar RetroFE", ["bash", "frontend/RetroFE/Tools/launchers/retrofe/launch-retrofe.sh", "--check"])
    add("frontends", "check-esde", "Diagnosticar ES-DE", ["env", "ESDE_NONINTERACTIVE=1", "bash", "frontend/ES-DE/launch.sh", "--diagnose-frontend"])
    add("frontends", "check-quiver", "Diagnosticar Quiver", command("frontend/QuiverLauncher/structure-frontend.py", "--check-startup"))
    for platform in platforms:
        system = platform["id"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", system):
            continue
        add("emuladores", "runtime-" + system, "Auditar " + platform["name"],
            command("tools/roms/emutool.py", "--system-id", system, "--format", "json"), system)
        add("roms", "plan-" + system, "Planejar compactação · " + platform["name"],
            command(RT, "compact", "plan", "--root", "roms/" + system), system, "planejar")
    return actions


def discard_actions(root, actions):
    """One explicit, reviewable withdrawal per pending plan; the official tools keep the manifest."""
    discards, seen = [], set()
    for action in actions:
        if not action.get("mutation") or not action.get("manifest") or action["manifest"] in seen:
            continue
        seen.add(action["manifest"])
        opid = action.get("operation_id", "")
        reason = "Descartado no Preview após revisão"
        if opid:
            argv = command(RT, "discard", "--operation-id", opid, "--reason", reason, "--apply")
            count = sum(a.get("count", 0) for a in actions if a.get("operation_id") == opid and a.get("mutation"))
        else:
            argv = command("tools/roms/materialize-collection-media-paths.py", "discard", "--manifest",
                           action["manifest"], "--reason", reason, "--apply")
            count = action.get("count", 0)
        action["discard_action_id"] = action_id("discard", action["manifest"])
        discards.append(dict(id=action["discard_action_id"], tab=action["tab"], title="Descartar plano " + (opid or Path(action["manifest"]).stem),
            system_id="", effect="descartar", count=count, ready=True, blocked_reason="", commands=[argv],
            reason="Retira o plano pendente sem apagar manifesto, evidências ou arquivos; nenhum item é aplicado.",
            mutation=False, discard_for=opid or action["manifest"], operation_id=opid, manifest=action["manifest"],
            manifest_sha256=action["manifest_sha256"], items=[], source="", destination="", bytes=0, savings_bytes=None))
    return discards


def retired_plan_rows(root, history):
    rows = [dict(operation_id=r.get("operation_id", ""), kind=r.get("kind", ""), status=r.get("status", ""),
                 created_at=r.get("created_at", ""), item_count=r.get("item_count", ""))
            for r in history if r.get("status") in {"superseded", "discarded"}]
    for path in (root / "frontend/RetroFE/Manifests").glob("collection-media-runtime-*.json"):
        manifest = load(path)
        if manifest.get("status") in {"superseded", "discarded"}:
            rows.append(dict(operation_id=path.stem, kind="frontend_runtime_plan", status=manifest["status"],
                             created_at=manifest.get("superseded_at") or manifest.get("discarded_at", ""), item_count=""))
    for row in rows:
        if row["kind"] != "frontend_runtime_plan":
            manifest = load(root / OPS / row["operation_id"] / "manifest.json")
            row.update(superseded_by=manifest.get("superseded_by", ""), reason=manifest.get("discard_reason", ""))
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:200]


def frontend_plan_actions(root):
    """Only the existing media-path materializer owns frontend configuration writes."""
    actions = []
    manifests = root / "frontend/RetroFE/Manifests"
    for path in sorted(manifests.glob("collection-media-runtime-*.json"), reverse=True):
        manifest = load(path)
        if manifest.get("status") != "prepared" or manifest.get("operation") != "retrofe_collection_media_path_migration":
            continue
        files, links = manifest.get("files", []), manifest.get("links", [])
        if not files and not links:
            continue
        checks, blocked = [], ""
        for item in files:
            source = root / "frontend/RetroFE" / item["path"]
            checks.append(dict(source=relative(root, source), destination=relative(root, source),
                sha256=item["before_sha256"], size=source.stat().st_size if source.is_file() else 0))
            if not source.is_file() or digest(source) != item["before_sha256"]:
                blocked = "Configuração mudou desde o plano; prepare uma análise atual"
        for item in links:
            source = (root / "frontend/RetroFE" / item["source"]).resolve()
            destination = root / "frontend/RetroFE" / item["path"]
            checks.append(dict(source=relative(root, source), destination=relative(root, destination),
                sha256=item["sha256"], size=item.get("size_bytes", 0)))
            if not source.is_file() or os.path.lexists(destination):
                blocked = "Origem ou alias mudou desde o plano"
        rel = relative(root, path)
        actions.append(dict(id=action_id(rel), tab="frontends", title="Apontamentos e aliases de mídia · " + path.stem,
            system_id="", effect="configurar", count=len(files) + len(links), ready=not blocked,
            blocked_reason=blocked, reason=manifest.get("reason", "Associar caminhos oficiais e aliases por identidade exata"),
            commands=[command("tools/roms/materialize-collection-media-paths.py", "apply", "--manifest", rel, "--apply"),
                      command("tools/roms/materialize-collection-media-paths.py", "verify", "--manifest", rel)],
            mutation=True, operation_id="", manifest=rel, manifest_sha256=digest(path), items=checks,
            source=checks[0]["source"], destination=checks[0]["destination"], bytes=0, savings_bytes=None))
    return actions


def item_source(item):
    return item.get("source") or item.get("source_path") or ""


def recorded_results(root, history, prepared_media=None):
    """Project official terminal manifests into results; never mutate/cache a second catalog."""
    outcomes, relocations = {}, {}
    terminal = {"verified", "skipped_no_gain_verified", "completed_verified", "completed_no_savings"}
    for row in sorted(history, key=lambda r: r.get("created_at", "")):
        kind = row.get("kind")
        if kind not in {"media_optimization", "compression", "change"}:
            continue
        if row.get("status") in READY_STATES and kind != "media_optimization":
            continue
        opid = row.get("operation_id", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", opid):
            continue
        manifest = load(root / OPS / opid / "manifest.json")
        if kind == "media_optimization" and prepared_media is not None:
            todo = [i for i in manifest.get("items", []) if i.get("status") in READY_STATES]
            keys = {"item_id", "status", "source", "destination", "source_size_bytes", "source_sha256", "source_mtime_ns", "precondition", "system_id", "reason", "lossy", "action"}
            prepared_media[opid] = {**{k:v for k,v in manifest.items() if k in {
                "operation_id", "kind", "status", "title", "created_at", "details", "profile_config", "profile_config_sha256", "tool_source", "tool_source_sha256"}},
                "items": [{k:v for k,v in item.items() if k in keys} for item in todo]} if todo else None
        if kind == "media_optimization" and manifest.get("status") not in {"rolled_back", "cancelled"}:
            if config_precondition(root, manifest):
                selected = set()
            else:
                selected = set(manifest.get("selection", {}).get("relative_paths", []))
            if selected:
                for audit in read_csv(root / OPS / opid / "media-optimization-audit.csv"):
                    rel = audit.get("relative_path", "")
                    if rel not in selected or audit.get("plan_status", "").startswith("eligible"):
                        continue
                    path = root / "media" / rel
                    size = int(audit.get("size_bytes") or 0)
                    key = "media/" + rel
                    when = manifest.get("created_at", "")
                    if key in outcomes and outcomes[key]["completed_at"] > when:
                        continue
                    outcomes[key] = dict(id=action_id("diagnosis", opid, key), tab="midias", operation_id=opid,
                        system_id=audit.get("system_id", ""), title=path.name, source=key, destination=key,
                        status=audit.get("plan_status", "preserved"), current=path.is_file() and path.stat().st_size == size,
                        source_size_bytes=size, output_size_bytes=size, stored_size_delta_bytes=0,
                        profile_id=audit.get("profile_id", ""), sha256=audit.get("sha256", ""), source_sha256=audit.get("sha256", ""),
                        completed_at=when, backup_path="", source_retained=True, action="media_plan_audit",
                        proof=audit.get("plan_reason", "Diagnóstico oficial sem conversão"), physical_bytes_released=0)
            if manifest.get("status") in READY_STATES:
                continue
        for item in manifest.get("items", []):
            if item.get("status") not in terminal:
                continue
            if kind == "change" and item.get("action") == "quarantine":
                relocated = project_path(root, item.get("destination", ""))
                if relocated.is_relative_to(root / OPS) and relocated.is_file():
                    relocations[relative(root, project_path(root, item_source(item)))] = relative(root, relocated)
            if kind == "change" and item.get("action") not in {"pack_tree", "extract_tree"}:
                continue
            source = project_path(root, item_source(item))
            destination = project_path(root, item.get("destination") or item_source(item))
            after = item.get("after") or {}
            before = item.get("before") or item.get("precondition") or {}
            source_size = item.get("source_size_bytes", before.get("size_bytes", 0))
            size = after.get("size_bytes", item.get("output_size_bytes", source_size))
            try:
                current = destination.is_file() and destination.stat().st_size == size
            except OSError:
                current = False
            key = relative(root, source)
            outcomes[key] = dict(id=action_id("result", opid, item.get("item_id", key)),
                tab="midias" if kind == "media_optimization" else "roms", operation_id=opid,
                system_id=item.get("system_id", ""), title=destination.name,
                source=key, destination=relative(root, destination), status=item["status"],
                current=current, source_size_bytes=source_size, output_size_bytes=size,
                stored_size_delta_bytes=size-source_size, profile_id=item.get("profile_id", ""),
                sha256=after.get("sha256") or item.get("output_sha256") or
                    (item.get("source_sha256", "") if item["status"] in {"skipped_no_gain_verified", "completed_no_savings"} else ""),
                source_sha256=item.get("source_sha256", before.get("sha256", "")),
                completed_at=item.get("verified_at", manifest.get("verified_at", manifest.get("completed_at", manifest.get("applied_at", manifest.get("created_at", ""))))),
                backup_path=relative(root, project_path(root, item.get("backup_path", ""))) if item.get("backup_path") else "",
                source_retained=bool(item.get("source_preserved")) or bool(item.get("backup_path")),
                action=item.get("action", "media_optimize"),
                proof="member_hash_roundtrip" if item.get("validation", {}).get("zarchive_conversion", {}).get("roundtrip_byte_identical") else "official_manifest_verification",
                physical_bytes_released=0)
    for source, result in outcomes.items():
        candidates = [relocations.get(source), result["backup_path"]]
        if result["status"] in {"skipped_no_gain_verified", "completed_no_savings"} or source != result["destination"] or result["action"] == "media_plan_audit":
            candidates.append(source)
        result["retained_at"] = next((path for path in candidates if path and (root / path).is_file()), "")
        result["source_retained"] = bool(result["retained_at"])
    return outcomes


def shallow_precondition(root, item):
    source = project_path(root, item_source(item))
    if not item_source(item):
        return "Item sem origem comprovada"
    try:
        reject_links(source)
        stat = source.stat()
    except (OSError, ValueError):
        return "Origem ausente ou simbólica; plano requer reconciliação"
    if not source.is_file():
        return "Árvore de conteúdo exige seu fluxo especializado; não é ROM de arquivo único"
    pre = item.get("precondition", {})
    expected = item.get("source_size_bytes", pre.get("size_bytes"))
    if expected is not None and stat.st_size != expected:
        return "Tamanho da origem mudou desde o plano"
    mtime = item.get("source_mtime_ns", pre.get("mtime_ns"))
    if mtime is not None and stat.st_mtime_ns != mtime:
        return "Data da origem mudou desde o plano"
    destination = project_path(root, item.get("destination", ""))
    if item.get("destination") and destination != source and os.path.lexists(destination):
        return "Destino já ocupado; plano requer nova revisão"
    return ""


def config_precondition(root, manifest):
    pairs = []
    detail = manifest.get("details", {})
    for data, pathkey, hashkey in [(manifest, "profile_config", "profile_config_sha256"),
            (manifest, "tool_source", "tool_source_sha256"),
            (detail, "profile_config", "profile_config_sha256"),
            (detail, "runtime_profile", "runtime_profile_sha256")]:
        if data.get(pathkey) and data.get(hashkey):
            pairs.append((project_path(root, data[pathkey]), data[hashkey]))
    for path, expected in pairs:
        if not path.is_file() or digest(path) != expected:
            return "Ferramenta ou perfil mudou desde o plano; gere um plano atual"
    return ""


def plan_actions(root, history, outcomes=None, prepared_media=None):
    actions, plans, occupied, archived = [], [], set(), []
    outcomes = outcomes or {}
    rows = sorted((r for r in history if r.get("status") in READY_STATES or
        r.get("kind") == "media_optimization" and r.get("status") not in {"rolled_back", "cancelled"}),
        key=lambda r: r.get("created_at", ""), reverse=True)
    # The same source already moved/renamed/quarantined and verified by another operation (e.g. a corrected
    # duplicate plan); only counts while that source is still absent.
    applied_changes = {}
    for r in history:
        if r.get("kind") == "change" and r.get("status") == "completed_verified" and \
                re.fullmatch(r"[A-Za-z0-9._+-]+", r.get("operation_id", "")):
            for i in load(root / OPS / r["operation_id"] / "manifest.json").get("items", []):
                if i.get("status") == "completed_verified" and i.get("action") in {"move", "rename", "quarantine"}:
                    applied_changes[(i["action"], item_source(i))] = r["operation_id"]
    for record in rows:
        opid = record.get("operation_id", "")
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", opid):
            continue
        path = root / OPS / opid / "manifest.json"
        if record.get("kind") == "media_optimization" and prepared_media is not None:
            manifest = prepared_media.get(opid)
            if not manifest:
                continue
        else:
            manifest = load(path)
        if manifest.get("status") not in READY_STATES and manifest.get("kind") != "media_optimization":
            continue
        kind = manifest.get("kind", "")
        items = [x for x in manifest.get("items", []) if x.get("status", "prepared") in READY_STATES]
        if not items:
            continue
        plans.append(dict(operation_id=opid, kind=kind, count=len(items), created_at=manifest.get("created_at", "")))
        if kind not in {"compression", "media_optimization", "change"}:
            continue
        config_block = config_precondition(root, manifest)
        groups = [[item] for item in items] if kind == "media_optimization" else [items]
        manifest_hash = digest(path)
        for group in groups:
            first = group[0]
            source = item_source(first)
            completed_group = [outcomes.get(relative(root, project_path(root, item_source(item)))) for item in group]
            if all(result and result["current"] and manifest.get("created_at", "") <= result["completed_at"]
                   for result in completed_group):
                archived.extend(dict(operation_id=opid, source=result["source"],
                    result_operation_id=result["operation_id"], reason="Resultado posterior já registrado; proposta superada")
                    for result in completed_group)
                continue
            done_by = [not os.path.lexists(project_path(root, item_source(i))) and
                       applied_changes.get((i.get("action"), item_source(i))) for i in group]
            if kind == "change" and all(done_by):
                archived.extend(dict(operation_id=opid, source=relative(root, project_path(root, item_source(i))),
                    result_operation_id=result_op, reason="Mesma mudança já aplicada e verificada; proposta superada")
                    for i, result_op in zip(group, done_by))
                continue
            tab = "midias" if kind == "media_optimization" or "/media/" in source else "roms"
            effect = "compactar" if kind in {"compression", "media_optimization"} else {
                "move": "mover", "rename": "renomear", "quarantine": "isolar", "extract": "converter",
                "extract_tree": "converter", "pack_tree": "converter"}.get(first.get("action"), "revisar")
            blocked = config_block or next((problem for i in group if (problem := shallow_precondition(root, i))), "")
            if kind == "change" and any(i.get("action") not in {"move", "rename", "quarantine", "extract", "extract_tree", "pack_tree"} for i in group):
                blocked = blocked or "Fluxo especializado exige revisão na ferramenta responsável"
            if any(i.get("lossy") for i in group):
                blocked = "Perfil com perda requer revisão de qualidade fora deste painel"
            if any(not Path(project_path(root, item_source(i))).is_relative_to(root / "roms") and
                   not Path(project_path(root, item_source(i))).is_relative_to(root / "media") for i in group):
                blocked = blocked or "Origem fora da biblioteca canônica; revisão individual necessária"
            key = (tab, source, effect)
            if key in occupied:
                continue
            occupied.add(key)
            itemid = first.get("item_id", "")
            if kind == "compression":
                commands = [command(RT, "compact", "apply", "--operation-id", opid, "--apply"),
                            command(RT, "compact", "verify", "--operation-id", opid)]
            elif kind == "media_optimization":
                commands = [command(RT, "media", "optimize", "apply", "--operation-id", opid, "--item-id", itemid, "--apply"),
                            command(RT, "media", "optimize", "verify", "--operation-id", opid)]
            else:
                commands = [command(RT, "apply", "--operation-id", opid, "--apply")]
            clean_items = [dict(source=relative(root, project_path(root, item_source(i))),
                destination=relative(root, project_path(root, i["destination"])) if i.get("destination") else "",
                sha256=i.get("source_sha256", i.get("precondition", {}).get("sha256", "")),
                size=i.get("source_size_bytes", i.get("precondition", {}).get("size_bytes", 0))) for i in group]
            actions.append(dict(id=action_id(opid, itemid if kind == "media_optimization" else "plan"), tab=tab,
                title=Path(source).name if len(group) == 1 else manifest.get("title", opid),
                system_id=first.get("system_id", ""), effect=effect, count=len(group),
                ready=not blocked, blocked_reason=blocked, reason=str(first.get("reason", manifest.get("title", ""))),
                commands=commands, mutation=True, operation_id=opid, manifest=relative(root, path),
                manifest_sha256=manifest_hash, items=clean_items, source=clean_items[0]["source"],
                destination=clean_items[0]["destination"], bytes=sum(i["size"] for i in clean_items),
                savings_bytes=None, created_at=manifest.get("created_at", "")))
    return actions, plans, archived


def runtime_report(root):
    path = root / "tools/roms/emutool.py"
    if not path.is_file():
        return {}, "emutool indisponível"
    try:
        result = subprocess.run(command(path, "--format", "json"), cwd=root, capture_output=True,
                                text=True, timeout=45)
        if result.returncode:
            return {}, result.stderr[-500:] or "Auditoria de runtime falhou"
        report = json.loads(result.stdout)
        return report, ""
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {}, str(exc)


def portable_runtime_report(root):
    """Installer state from the official emutool command; recipes stay in its own config."""
    path = root / "tools/roms/emutool.py"
    try:
        result = subprocess.run(command(path, "install", "status"), cwd=root, capture_output=True, text=True, timeout=45)
        if result.returncode:
            return {}, result.stderr[-500:] or "Estado da instalação portátil indisponível"
        return json.loads(result.stdout), ""
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {}, str(exc)


def runtime_install_actions(report):
    actions = []
    for row in report.get("runtimes", []):
        installed = row["status"] in {"installed_recorded", "installed_size_matches"}
        blocked = ""
        host = "; ".join(f"Flatpak {h['app_id']} {h['version']}" if h["kind"] == "flatpak" else h["path"]
                         for h in row.get("host_sources", []))
        if row.get("decision", "").startswith("use_host"):
            blocked = (f"Já disponível no host ({host}); o ambiente usa essa instalação"
                       + (" pelo perfil" if row["decision"] == "use_host_configured" else ", mas nenhuma coleção está vinculada")
                       + ". Instalar duplicaria o emulador.")
        elif row["status"] == "blocked_symlink":
            blocked = "Caminho de instalação é symlink; preservado para revisão"
        elif row["manual_download"] and not row["incoming_ready"] and not installed:
            blocked = (f"Download de {row['source_size_bytes']:,} bytes é manual: baixe {row['source_url']} "
                       f"(SHA-256 {row['source_sha256']}) e coloque em {row['incoming_path']}; depois atualize e instale.")
        argv = command("tools/roms/emutool.py", "install", "apply", "--runtime-id", row["runtime_id"], "--apply")
        row["action_id"] = "runtime-install-" + row["runtime_id"]
        actions.append(dict(id=row["action_id"], tab="emuladores", effect="instalar", count=1, ready=not blocked,
            title=("Verificar " if installed else "Instalar ") + row["display_name"] + " " + row["version"],
            system_id="", blocked_reason=blocked, commands=[argv], mutation=False, operation_id="", manifest="",
            reason=("Instalação portátil independente em " + row["entry"] + ": release oficial fixada por tamanho e SHA-256, "
                    "instalação atômica, versão anterior preservada para rollback e registro no histórico. Não testa jogos."),
            items=[], source=row["source_url"], destination=row["entry"], bytes=row["source_size_bytes"], savings_bytes=None))
    for asset in report.get("assets", []):
        asset["action_id"] = "runtime-asset-" + asset["asset_id"]
        actions.append(dict(id=asset["action_id"], tab="emuladores", effect="instalar", count=1, ready=not asset["present"],
            title="Instalar " + asset["display_name"], system_id="", operation_id="", manifest="", mutation=False,
            blocked_reason="Já presente; guarda estado do usuário e não é substituído" if asset["present"] else "",
            commands=[command("tools/roms/emutool.py", "install", "asset", "--asset-id", asset["asset_id"], "--apply")],
            reason="Arquivo de suporte da release oficial, hash fixado, instalado sem sobrescrever em " + asset["destination"] + ".",
            items=[], source=asset["source_url"], destination=asset["destination"], bytes=asset["source_size_bytes"], savings_bytes=None))
    return actions


def cover_projection(root, history):
    """Read official diagnosis/plans; never create another media catalog."""
    audit, completed, actions = {}, {}, []
    for row in sorted(history, key=lambda r:r.get("created_at", "")):
        if row.get("kind") not in {"media_cover_audit", "media_cover_plan"}:
            continue
        opid = row.get("operation_id", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", opid):continue
        path = root / OPS / opid / "manifest.json"
        manifest = load(path)
        if manifest.get("kind") == "media_cover_audit":
            audit = manifest
            continue
        if manifest.get("status") == "rolled_back":continue
        for item in manifest.get("items", []):
            if item.get("status") == "verified":
                front = root / "media" / item["front_payload"]
                full = root / "media" / item["full_payload"]
                if front.is_file() and full.is_file():
                    completed[item["source"]] = dict(item, operation_id=opid)
            elif item.get("status") == "prepared" and manifest.get("status") in {"prepared", "applied_partial"}:
                adapted = {**item, "source":"media/"+item["source"], "destination":"media/"+item["front"]}
                blocked = config_precondition(root, manifest) or shallow_precondition(root, adapted)
                for key, logical in [("front_payload", "front"), ("full_payload", "full")]:
                    if item[key] != item[logical] and not (root/"media"/item[key]).is_file():
                        blocked = blocked or "Payload compartilhado aguarda o item anterior do plano"
                actions.append(dict(id=action_id(opid,item["item_id"]),tab="midias",title=Path(item["source"]).name,
                    system_id=item["system_id"],effect="converter",count=1,ready=not blocked,blocked_reason=blocked,
                    reason=item["reason"]+". Original: media/"+item["full_payload"]+"; frente: media/"+item["front"]+"; região "+str(item["front_proof"]["front_box"]),
                    commands=[command(RT,"media","covers","apply","--operation-id",opid,"--item-id",item["item_id"],"--apply"),
                              command(RT,"media","covers","verify","--operation-id",opid)],
                    mutation=True,operation_id=opid,manifest=relative(root,path),manifest_sha256=digest(path),
                    items=[dict(source=adapted["source"],destination=adapted["destination"],sha256=item["source_sha256"],size=item["source_size_bytes"])],
                    source=adapted["source"],destination=adapted["destination"],bytes=item["source_size_bytes"],savings_bytes=None))
    covers = []
    by_front = {i["front"]:i for i in completed.values()}
    audited_sources={row["source"] for row in audit.get("items",[])}
    for row in audit.get("items", []):
        item = completed.get(row["source"]) or by_front.get(row["source"])
        if item:
            if row["source"]==item["source"] and item["source"]!=item["front"] and item["front"] in audited_sources:
                continue  # legacy extension alias does not become a second corrected image
            covers.append(dict(row,classification="front_separated_verified",status="verified",needs_review=False,
                image="media/"+item["front"],full_image="media/"+item["full_payload"],dimensions=item["front_proof"]["dimensions"],
                reason="Frente separada com pixels conferidos; encarte original preservado",operation_id=item["operation_id"]))
        else:covers.append(row)
    return dict(operation_id=audit.get("operation_id"),created_at=audit.get("created_at"),
        summary={**audit.get("summary",{}),"corrected":len(completed),"remaining_review":sum(r.get("needs_review",False) for r in covers)},items=covers), actions


def media_replan_action(identifier, paths, title, system_id=""):
    return dict(id=identifier, tab="midias", title=title, system_id=system_id, effect="planejar",
        count=len(paths), ready=bool(paths), blocked_reason="Nenhuma pendência exige outro plano" if not paths else "",
        reason="Confere o inventário dos arquivos selecionados, atualiza quando necessário e gera um plano nativo lossless; não converte nem reaplica o histórico.",
        workflow={"kind": "media_replan", "relative_paths": paths}, commands=[], mutation=False,
        operation_id="", manifest="", manifest_sha256="", items=[], source="media/" + paths[0] if paths else "media/",
        destination="Novo plano para revisão", bytes=0, savings_bytes=None)


def media_inventory_matches(root, manifest, paths):
    """Check every selected file, including later scopes in the same job."""
    if manifest.get("kind") != "media_inventory" or manifest.get("evidence", {}).get("root_relative") != "media":
        return False
    wanted = set(paths)
    selected = {i.get("relative_path"): i for i in manifest.get("items", []) if i.get("relative_path") in wanted}
    if len(selected) != len(wanted):
        return False
    for relative_path, item in selected.items():
        path = root / "media" / relative_path
        try:
            reject_links(path)
            stat = path.stat()
            actual = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
            expected = tuple(item.get(key) for key in ("size_bytes", "mtime_ns", "device", "inode"))
            if not path.is_file() or actual != expected or item.get("sha256") and digest(path) != item["sha256"]:
                return False
        except (OSError, ValueError):
            return False
    return True


def reusable_media_inventory(root, paths):
    """Reuse official evidence only if all selected source fingerprints still match."""
    history = read_csv(root / OPS / "history-index.csv")
    inventories = sorted((r for r in history if r.get("kind") == "media_inventory"),
                         key=lambda r: r.get("created_at", ""), reverse=True)
    for row in inventories[:3]:
        opid = row.get("operation_id", "")
        if not re.fullmatch(r"media_inventory-[A-Za-z0-9._-]+", opid):
            continue
        manifest = load(root / OPS / opid / "manifest.json")
        if media_inventory_matches(root, manifest, paths):
            return opid
    return None


def build_snapshot(root=ROOT, probe_runtimes=True):
    started = now()
    platform_path = root / "frontend/RetroFE/Config/platform-catalog.json"
    platform_catalog = load(platform_path)
    roms, rom_errors = census(root / "roms")
    media, media_errors = census(root / "media")
    pending = read_csv(root / BASE / "pendencias-integridade-dependencias.csv")
    history = read_csv(root / OPS / "history-index.csv")
    asset_path = root / "frontend/RetroFE/Manifests/asset-catalog.json"
    assets = load(asset_path)
    media_audit_path = root / "frontend/RetroFE/Manifests/collection-media-path-audit.json"
    media_audit = load(media_audit_path)
    runtime_media = media_audit.get("runtime_media", {})
    profiles = load(root / "frontend/RetroFE/Config/profiles/host-linux.json")
    runtime, runtime_error = runtime_report(root) if probe_runtimes else ({}, "Auditoria ao vivo não solicitada")
    runtime_by_id = {row["system_id"]: row for row in runtime.get("systems", [])}
    collections_by_system = collections.defaultdict(list)
    for name, entry in profiles.get("collections", {}).items():
        collections_by_system[entry.get("rom_subdir", "")].append(name)
    artwork = collections.defaultdict(dict)
    missing_assets = []
    for alias in assets.get("main_artwork_aliases", []):
        target = root / "frontend/RetroFE" / alias.get("target", "")
        name = Path(alias.get("path", "")).stem
        typ = alias.get("source_type", "")
        if target.is_file() and typ in {"logo", "fanartsys", "fanart_coverart", "artwork_front", "system_artwork"}:
            artwork[name].setdefault(typ, relative(root, target))
        elif not target.is_file():
            missing_assets.append(dict(platform=name, type=typ, path=relative(root, target), reason="Alvo catalogado ausente"))
    for asset in assets.get("canonical_assets", []):
        path = root / "frontend/RetroFE" / asset.get("path", "")
        if not path.is_file():
            missing_assets.append(dict(platform="Biblioteca compartilhada", type=asset.get("category", ""), path=relative(root, path), reason="Payload canônico ausente"))
    coverage = collections.defaultdict(lambda: dict(games=0, covered=collections.Counter(), samples=[]))
    seen_games, stale_game_entries = set(), 0
    missing_game_media = []
    for game in runtime_media.get("games", []):
        sid = game.get("system_id", "")
        rom = project_path(root, game.get("rom", ""))
        if not game.get("rom") or not rom.is_file():
            stale_game_entries += 1
            continue
        game_key = (sid, str(rom.resolve()))
        if game_key in seen_games:
            continue
        seen_games.add(game_key)
        row = coverage[sid]
        row["games"] += 1
        sample = dict(title=game.get("basename", ""), media={}, system_id=sid)
        for typ, raw in game.get("media", {}).items():
            target = (root / "frontend/RetroFE" / raw).resolve() if raw else None
            if target and target.is_file() and target.is_relative_to(root / "media"):
                row["covered"][typ] += 1
                sample["media"][typ] = relative(root, target)
            else:
                missing_game_media.append(dict(platform=sid, title=sample["title"], type=typ,
                    path=relative(root, target) if target else "", reason="Arquivo ausente" if raw else "Sem correspondência no relatório oficial"))
        if sample["media"] and len(row["samples"]) < 24:
            row["samples"].append(sample)
    platforms = []
    ids = {p.get("id") for p in platform_catalog.get("systems", [])}
    source_platforms = platform_catalog.get("systems", []) + [dict(id=k, display_name=k, kind="unclassified") for k in sorted(set(roms) - ids)]
    for platform in source_platforms:
        sid = platform.get("id", "")
        if not sid:
            continue
        names = collections_by_system.get(platform.get("rom_subdir", sid), [])
        art = next((artwork[name] for name in names if artwork.get(name)), {})
        runtime_row = runtime_by_id.get(sid, {})
        platforms.append(dict(id=sid, name=platform.get("display_name", sid), kind=platform.get("kind", "platform"),
            roms=dict(roms.get(sid, {})), media=dict(media.get(sid, {})), collections=names, artwork=art,
            coverage=dict(coverage.get(sid, {})), runtime_status=runtime_row.get("status", "not_audited"),
            profiles=[{**p, "runtime": {k: v for k, v in p.get("runtime", {}).items()
                if k in {"available", "status", "version", "missing_bios", "launch_tested", "runtime"}}}
                for p in runtime_row.get("profiles", [])]))
    prepared_media = {}
    outcomes = recorded_results(root, history, prepared_media)
    actions, plans, archived_proposals = plan_actions(root, history, outcomes, prepared_media)
    actions.extend(frontend_plan_actions(root))
    actions.extend(discard_actions(root, actions))
    portable_runtimes, portable_runtime_error = portable_runtime_report(root) if probe_runtimes else ({}, "Auditoria ao vivo não solicitada")
    actions.extend(runtime_install_actions(portable_runtimes))
    retired_plans = retired_plan_rows(root, history)
    actions.extend(builtin_actions(platforms))
    cover_audit, cover_actions = cover_projection(root, history)
    actions.extend(cover_actions)
    replan_paths = set()
    for action in list(actions):
        if action["tab"] != "midias" or not action["mutation"] or action["ready"]:
            continue
        source = action["source"]
        path = root / source
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf"}:
            replan_paths.add(source.removeprefix("media/"))
            action["replan_action_id"] = action_id("replan-media", source)
            actions.append(media_replan_action(action["replan_action_id"], [source.removeprefix("media/")],
                "Reanalisar " + path.name, action["system_id"]))
    for result in outcomes.values():
        path = root / result["source"]
        if result["action"] == "media_plan_audit" and path.is_file() and not path.is_symlink() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf"}:
            result["replan_action_id"] = action_id("replan-media", result["source"])
            if not any(a["id"] == result["replan_action_id"] for a in actions):
                actions.append(media_replan_action(result["replan_action_id"], [result["source"].removeprefix("media/")],
                    "Reanalisar " + path.name, result["system_id"]))
    macro = media_replan_action("media-plan", sorted(replan_paths)[:100], "Reanalisar pendências de mídia (até 100)")
    macro["total_pending"] = len(replan_paths)
    actions.append(macro)
    missing_systems = []
    for p in platforms:
        if not p["roms"].get("files"):
            continue
        for typ in ("logo", "fanartsys"):
            if typ not in p["artwork"]:
                missing_systems.append(dict(platform=p["id"], title=p["name"], type=typ,
                    path="", reason="Sem associação exata nos aliases oficiais da plataforma"))
    frontend_list = [dict(id="retrofe", name="RetroFE", path="frontend/RetroFE", launcher="frontend/RetroFE/Tools/launchers/retrofe/launch-retrofe.sh"),
                     dict(id="esde", name="ES-DE", path="frontend/ES-DE", launcher="frontend/ES-DE/launch.sh"),
                     dict(id="quiver", name="Quiver Launcher", path="frontend/QuiverLauncher", launcher="frontend/QuiverLauncher/launch.sh")]
    for f in frontend_list:
        f["installed"] = (root / f["path"]).is_dir()
        f["launcher_exists"] = (root / f["launcher"]).is_file()
    sources = []
    for path in [platform_path, asset_path, media_audit_path, root / OPS / "history-index.csv",
                 root / BASE / "pendencias-integridade-dependencias.csv"]:
        sources.append(dict(path=relative(root, path), exists=path.is_file(),
            modified_at=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds") if path.is_file() else None))
    def total(rows, key):
        return sum(row.get(key, 0) for row in rows.values())
    result_rows = sorted(outcomes.values(), key=lambda r: r["completed_at"], reverse=True)
    media_results = [r for r in result_rows if r["tab"] == "midias"]
    return dict(version=VERSION, revision=uuid.uuid4().hex, generated_at=now(), census_started_at=started, project="emulation", report="outputs/preview/index.html",
        sources=sources, warnings=rom_errors + media_errors + ([runtime_error] if runtime_error else []),
        metrics=dict(roms=dict(files=total(roms, "files"), bytes=total(roms, "bytes"), links=total(roms, "links")),
            media=dict(files=total(media, "files"), bytes=total(media, "bytes"), links=total(media, "links")),
            platforms=len(platforms), catalogued_platforms=len(platform_catalog.get("systems", [])), assets=len(assets.get("canonical_assets", [])), pending=len(pending),
            runtime_ready=sum(p["runtime_status"] == "runtime_ready_unverified" for p in platforms),
            runtime_blocked=sum(p["runtime_status"] in {"runtime_unavailable", "profile_missing", "dependency_missing",
                                                         "installed_not_probed", "runtime_probe_failed", "onboarding_pending"} for p in platforms),
            physical_savings_estimate=None), platforms=platforms, actions=actions, plans=plans, retired_plans=retired_plans, portable_runtimes=portable_runtimes, portable_runtime_error=portable_runtime_error,
        results=result_rows, archived_proposals=archived_proposals, cover_audit=cover_audit,
        result_summary=dict(media_optimized=sum(r["status"] == "verified" for r in media_results),
            media_no_gain=sum(r["status"] in {"skipped_no_gain_verified", "completed_no_savings"} for r in media_results),
            media_diagnostics=sum(r["action"] == "media_plan_audit" for r in media_results),
            library_bytes_reduced=sum(max(0, -r["stored_size_delta_bytes"]) for r in media_results),
            physical_bytes_released=0, superseded_proposals=len(archived_proposals)),
        missing_assets=missing_assets + missing_systems, missing_game_media=missing_game_media,
        frontends=frontend_list, media_audit_at=media_audit.get("generated_at"), stale_game_entries=stale_game_entries,
        pending=pending, history=history[-30:], runtime_checked_at=runtime.get("generated_at_utc"),
        notes=["Censo atual conta arquivos regulares; partes de um conjunto não equivalem a jogos.",
               "Pronto indica precondições preliminares; a ferramenta oficial revalida hashes, dependências, runtime e referências ao executar.",
               "Economia de conversão depende do resultado validado; originais retidos para rollback continuam ocupando espaço.",
               "Assets são referências canônicas, sem cópias no relatório. Cobertura deriva das associações oficiais, sem aproximação de nomes."])


def write_report(snapshot, root=ROOT):
    source = (root / "tools/roms/preview-ui.html").read_text()
    if "/*__PREVIEW_CLIENT__*/" in source:
        source = source.replace("/*__PREVIEW_CLIENT__*/", (root / "tools/roms/preview-client.js").read_text())
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output = source.replace("/*__PREVIEW_DATA__*/null", encoded)
    atomic(root / REPORT, output.encode())
    return root / REPORT


def current_guard(root, action):
    if not action.get("ready"):
        raise ValueError(action.get("blocked_reason", "Ação bloqueada"))
    if action.get("manifest"):
        path = root / action["manifest"]
        reject_links(path)
        if digest(path) != action["manifest_sha256"]:
            raise ValueError("Manifesto mudou desde a visualização; atualize o relatório")
    if action.get("mutation"):
        for item in action.get("items", []):
            path = project_path(root, item["source"])
            reject_links(path)
            path = path.resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("Origem fora da raiz do projeto")
            if not item.get("sha256") or not path.is_file() or digest(path) != item["sha256"]:
                raise ValueError("Origem mudou desde o plano ou está sem SHA-256 comprovado")


def public_snapshot(snapshot):
    return snapshot


class PreviewApp:
    def __init__(self, root=ROOT, snapshot=None):
        self.root = root.resolve()
        self.snapshot = snapshot if snapshot is not None else build_snapshot(root)
        self.snapshot.setdefault("revision", uuid.uuid4().hex)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview-job")
        self.jobs = {}
        self.active = None
        self.launcher = preview_games.Launcher(self.root)

    def refresh(self):
        snapshot = build_snapshot(self.root)
        write_report(snapshot, self.root)
        self.snapshot = snapshot

    def enqueue(self, ids, refresh=False, revision=None):
        if not refresh and (not isinstance(ids, list) or not ids or len(ids) > MAX_BATCH or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError(f"Selecione entre 1 e {MAX_BATCH} ações distintas")
        with self.lock:
            if self.active:
                raise ValueError("Uma operação já está em execução; aguarde sua verificação")
            if self.launcher.running():
                raise ValueError("Há um jogo aberto pelo Preview; feche o emulador antes de alterar a biblioteca")
            if not refresh and revision is not None and revision != self.snapshot["revision"]:
                raise ValueError("O relatório mudou desde a revisão; atualize o estado e revise novamente")
            registry = {a["id"]: a for a in self.snapshot["actions"]}
            if not refresh and any(i not in registry for i in ids):
                raise ValueError("A proposta foi concluída ou superada; atualize o estado")
            selected = [] if refresh else json.loads(json.dumps([registry[i] for i in ids]))
            sources = set()
            for action in selected:
                if not action["ready"]:
                    raise ValueError(action["blocked_reason"])
                action_sources = [i["source"] for i in action.get("items", [])]
                action_sources += ["media/" + value for value in action.get("workflow", {}).get("relative_paths", [])]
                for source in action_sources:
                    if source in sources:
                        raise ValueError("Ações selecionadas disputam a mesma origem; revise separadamente")
                    sources.add(source)
            jobid = "preview-job-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            job = dict(operation_id=jobid, kind="preview_job", status="queued", created_at=now(),
                title="Atualizar relatório" if refresh else "Executar ações revisadas no Preview",
                actions=selected, refresh_only=refresh, results=[], reason="Solicitação explícita pela interface local Preview",
                rollback="As alterações pertencem às operações oficiais chamadas abaixo; consultar cada manifesto e seu rollback. O relatório é regenerável.")
            self.jobs[jobid] = job
            self.active = jobid
            json_write(self.root / OPS / jobid / "manifest.json", job)
            self.executor.submit(self.run_job, job)
            return jobid

    def run_job(self, job):
        opdir = self.root / OPS / job["operation_id"]
        logpath = opdir / "execution.log"
        try:
            job["status"] = "running"
            json_write(opdir / "manifest.json", job)
            with logpath.open("ab", buffering=0) as log:
                current_manifest_hashes = {}
                fresh_inventory_id = None
                for action in job["actions"]:
                    guard = dict(action)
                    if guard.get("manifest") in current_manifest_hashes:
                        guard["manifest_sha256"] = current_manifest_hashes[guard["manifest"]]
                    current_guard(self.root, guard)
                    if action.get("workflow"):
                        fresh_inventory_id = self.run_media_replan(action, job, log, fresh_inventory_id)
                        continue
                    for argv in action["commands"]:
                        log.write(("\n" + json.dumps(argv, ensure_ascii=False) + "\n").encode())
                        result = subprocess.run(argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                        job["results"].append(dict(action_id=action["id"], command=argv, exit_code=result.returncode, completed_at=now()))
                        json_write(opdir / "manifest.json", job)
                        if result.returncode:
                            # F03: surface the domain cause (last stderr/stdout lines), never an empty failure.
                            try:
                                tail = [line for line in logpath.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()][-6:]
                            except OSError:
                                tail = []
                            cause = " | ".join(tail)[-1200:] or "sem saída registrada"
                            raise RuntimeError(f"A ferramenta retornou código {result.returncode}; lote interrompido para revisão. Causa: {cause}")
                    self.validate_action_completion(action)
                    if action.get("manifest"):
                        current_manifest_hashes[action["manifest"]] = digest(self.root / action["manifest"])
            outcome = "completed_verified" if job["refresh_only"] else "completed_commands"
        except Exception as exc:
            outcome = "failed_preserved"
            job["error"] = str(exc)
        finally:
            job["status"] = "updating_report"
            json_write(opdir / "manifest.json", job)
            try:
                self.refresh()
            except Exception as exc:
                job["refresh_error"] = str(exc)
                if outcome.startswith("completed"):
                    outcome = "completed_refresh_failed"
            job["status"] = outcome
            job["snapshot_revision"] = self.snapshot.get("revision")
            job["snapshot_generated_at"] = self.snapshot.get("generated_at")
            job["finalizing"] = True
            job["completed_at"] = now()
            json_write(opdir / "manifest.json", job)
            # Reuse official journal/index functions; never write another history catalog.
            try:
                self.record_history(job)
            except Exception as exc:
                job["history_error"] = str(exc)
                json_write(opdir / "manifest.json", job)
            job["finalizing"] = False
            json_write(opdir / "manifest.json", job)
            with self.lock:
                self.active = None

    def run_media_replan(self, action, job, log, inventory_id):
        workflow = action["workflow"]
        paths = workflow.get("relative_paths", [])
        if workflow.get("kind") != "media_replan" or not paths or len(paths) > 100:
            raise ValueError("Reanálise exige de 1 a 100 caminhos explícitos de mídia")
        for raw in paths:
            value = Path(raw)
            if value.is_absolute() or ".." in value.parts or len(value.parts) < 3:
                raise ValueError("Caminho de mídia inválido")
            source = self.root / "media" / value
            reject_links(source)
            if not source.is_file() or not source.resolve().is_relative_to(self.root / "media"):
                raise ValueError("Mídia mudou antes da reanálise")
        if inventory_id is None:
            inventory_id = reusable_media_inventory(self.root, paths)
            if inventory_id:
                log.write(("Inventário oficial reutilizado após conferir os arquivos selecionados: " + inventory_id + "\n").encode())
                job["results"].append(dict(action_id=action["id"], action="reuse_validated_inventory",
                    inventory_operation_id=inventory_id, selected_files_checked=len(paths), completed_at=now()))
        if inventory_id is None:
            argv = command(RT, "media", "optimize", "inventory")
            log.write((json.dumps(argv) + "\n").encode())
            result = subprocess.run(argv, cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if result.returncode:
                log.write(result.stderr.encode())
                raise RuntimeError("Inventário atual de mídia falhou")
            answer = json.loads(result.stdout)
            inventory_id = answer.get("operation_id", "")
            if not re.fullmatch(r"media_inventory-[A-Za-z0-9._-]+", inventory_id):
                raise ValueError("A ferramenta não retornou uma operação de inventário válida")
            inventory = load(self.root / OPS / inventory_id / "manifest.json")
            if inventory.get("kind") != "media_inventory" or inventory.get("evidence", {}).get("root_relative") != "media":
                raise ValueError("Inventário fora da raiz canônica")
            log.write(("Inventário oficial: " + inventory_id + " (payload integral preservado no manifesto oficial)\n").encode())
            job["results"].append(dict(action_id=action["id"], command=argv, exit_code=0,
                completed_at=now(), generated_operation_id=inventory_id))
        if not media_inventory_matches(self.root, load(self.root / OPS / inventory_id / "manifest.json"), paths):
            raise RuntimeError("Mídia mudou desde o inventário; reanalise o escopo antes de criar outro plano")
        argv = command(RT, "media", "optimize", "plan", "--inventory-operation-id", inventory_id,
            "--profile-id", "png-lossless-repack", "--profile-id", "jpeg-lossless-repack", "--profile-id", "pdf-qpdf-lossless")
        for path in paths:
            argv += ["--relative-path", path]
        log.write((json.dumps(argv, ensure_ascii=False) + "\n").encode())
        result = subprocess.run(argv, cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode:
            log.write((result.stdout + result.stderr).encode())
            raise RuntimeError("Novo plano de mídia falhou; nenhum payload foi convertido")
        answer = json.loads(result.stdout)
        plan_id = answer.get("operation_id", "")
        if not re.fullmatch(r"media-optimization-[A-Za-z0-9._-]+", plan_id):
            raise ValueError("A ferramenta não retornou uma operação de plano válida")
        log.write((json.dumps({"new_plan":plan_id,"candidate_count":answer.get("candidate_count"),
            "audit":answer.get("details",{}).get("audit_status_counts",{})},ensure_ascii=False)+"\n").encode())
        job["results"].append(dict(action_id=action["id"], command=argv, exit_code=0,
            completed_at=now(), generated_operation_id=plan_id))
        json_write(self.root / OPS / job["operation_id"] / "manifest.json", job)
        return inventory_id

    def validate_action_completion(self, action):
        if not action.get("mutation") or not action.get("manifest"):
            return
        manifest = load(self.root / action["manifest"])
        if manifest.get("kind") in {"change", "compression"} and manifest.get("status") != "completed_verified":
            raise RuntimeError("O comando encerrou, mas o manifesto não confirmou a aplicação integral")
        if manifest.get("kind") == "media_optimization":
            argv = action["commands"][0]
            item_id = argv[argv.index("--item-id") + 1]
            item = next((i for i in manifest.get("items", []) if i.get("item_id") == item_id), {})
            if item.get("status") not in {"verified", "skipped_no_gain_verified"}:
                raise RuntimeError("O manifesto não confirmou a validação do item de mídia selecionado")
        if manifest.get("kind") == "media_cover_plan":
            argv=action["commands"][0]
            itemid=argv[argv.index("--item-id")+1]
            if not any(i.get("item_id")==itemid and i.get("status")=="verified" for i in manifest.get("items",[])):
                raise RuntimeError("O manifesto não confirmou a separação da capa selecionada")

    def get_job(self, jobid):
        if jobid in self.jobs:
            return self.jobs[jobid]
        if not re.fullmatch(r"preview-job-[A-Za-z0-9._-]+", jobid):
            raise KeyError("Job inválido")
        path = self.root / OPS / jobid / "manifest.json"
        reject_links(path)
        job = load(path)
        if job.get("kind") != "preview_job":
            raise KeyError("Job inexistente")
        if job.get("status") in {"queued", "running", "updating_report"}:
            return {**job, "status": "interrupted_needs_review", "finalizing": False, "error": "A sessão anterior terminou; confira a operação oficial antes de reexecutar"}
        return {**job, "finalizing": False}

    def record_history(self, job):
        sys.path.insert(0, str(self.root))
        from tools.roms import romtool
        romtool.append_event(job["operation_id"], "preview_job_finished", action="preview",
            status=job["status"], details={"results": job["results"], "error": job.get("error", "")})
        romtool.rebuild_history_index()
        self.snapshot["history"] = read_csv(self.root / OPS / "history-index.csv")[-30:]
        write_report(self.snapshot, self.root)

    def image_path(self, raw):
        if "\x00" in raw:
            raise ValueError("Caminho inválido")
        part = Path(raw)
        if part.is_absolute() or ".." in part.parts:
            raise ValueError("Caminho inválido")
        path = (self.root / part).resolve(strict=True)
        allowed = [self.root / "media", self.root / "frontend/RetroFE/Assets/shared"]
        if not any(path.is_relative_to(p) for p in allowed) or not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".mp4", ".webm", ".ogg"}:
            raise ValueError("Recurso fora das raízes e formatos permitidos")
        return path


class Handler(BaseHTTPRequestHandler):
    server_version = "Preview/" + VERSION

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def host_ok(self):
        host = self.headers.get("Host", "")
        return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def respond(self, code, value, typ="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=False).encode() if not isinstance(value, bytes) else value
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.host_ok():
            return self.respond(403, {"error": "Host não autorizado"})
        route = urlsplit(self.path).path
        try:
            if route in {"/", "/index.html"}:
                return self.respond(200, (self.app.root / REPORT).read_bytes(), "text/html; charset=utf-8")
            if route == "/api/health":
                return self.respond(200, dict(app="emulation-preview", version=VERSION,
                    project_id=hashlib.sha256(str(self.app.root).encode()).hexdigest()[:16]))
            if route == "/api/state":
                return self.respond(200, dict(active_job=self.app.active, revision=self.app.snapshot.get("revision"), version=VERSION))
            if route == "/api/snapshot":
                return self.respond(200, public_snapshot(self.app.snapshot))
            if route == "/api/launch":
                return self.respond(200, self.app.launcher.status())
            match = re.fullmatch(r"/api/platforms/([^/]+)/games", route)
            if match:
                return self.respond(200, preview_games.list_games(self.app.root, unquote(match.group(1))))
            if route.startswith("/api/jobs/"):
                jobid = route.rsplit("/", 1)[-1]
                job = self.app.get_job(jobid)
                path = self.app.root / OPS / jobid / "execution.log"
                tail = ""
                if path.is_file():
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - 32000))
                        tail = stream.read().decode(errors="replace")
                return self.respond(200, dict(operation_id=jobid, status="finalizing" if job.get("finalizing") else job["status"],
                    error=job.get("error"), refresh_error=job.get("refresh_error"), history_error=job.get("history_error"),
                    results=job["results"], snapshot_revision=job.get("snapshot_revision"), log=tail))
            if route.startswith("/resource/"):
                path = self.app.image_path(unquote(route[len("/resource/"):]))
                # Images stay canonical; do not copy or decode huge media into the report.
                size = path.stat().st_size
                if size > 64 * 2**20:
                    return self.respond(413, {"error": "Use o arquivo canônico para esta mídia acima de 64 MiB"})
                return self.respond(200, path.read_bytes(), mimetypes.guess_type(path)[0] or "application/octet-stream")
            return self.respond(404, {"error": "Recurso inexistente"})
        except (OSError, ValueError, KeyError) as exc:
            return self.respond(404, {"error": str(exc)})

    def do_POST(self):
        origin = self.headers.get("Origin")
        expected_origin = f"http://{self.headers.get('Host', '')}"
        if not self.host_ok() or origin != expected_origin or not secrets.compare_digest(self.headers.get("X-Preview-Token", ""), self.app.token):
            return self.respond(403, {"error": "Sessão local inválida; abra pelo launcher"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.respond(415, {"error": "JSON obrigatório"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16000:
                raise ValueError("Tamanho de requisição inválido")
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/launch":
                if not isinstance(data, dict) or set(data) - {"system_id", "path", "fullscreen", "snapshot_revision"}:
                    raise ValueError("A requisição de jogo aceita somente plataforma, caminho e tela cheia")
                busy = "Uma operação de manutenção está em execução; aguarde antes de jogar" if self.app.active else None
                launch_id = self.app.launcher.launch(data.get("system_id"), data.get("path"), data.get("fullscreen", False), busy)
                return self.respond(202, {"launch_id": launch_id})
            if not isinstance(data, dict) or set(data) - {"ids", "snapshot_revision"}:
                raise ValueError("A requisição aceita somente IDs de ações revisadas")
            if self.path == "/api/refresh":
                jobid = self.app.enqueue([], refresh=True)
            elif self.path == "/api/jobs":
                if data.get("snapshot_revision") != self.app.snapshot.get("revision"):
                    raise ValueError("A página está desatualizada; recarregue e revise o estado atual")
                jobid = self.app.enqueue(data.get("ids"), revision=data.get("snapshot_revision"))
            else:
                return self.respond(404, {"error": "Ação inexistente"})
            return self.respond(202, {"operation_id": jobid})
        except (ValueError, KeyError, OSError) as exc:
            return self.respond(409, {"error": str(exc)})


def open_browser(url):
    parsed = urlsplit(url)
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "preview_version=" + VERSION, parsed.fragment))
    opener = shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        webbrowser.open(url)


def serve(root, port, open_window, session_token=None):
    folder = root / "outputs/preview"
    folder.mkdir(parents=True, exist_ok=True)
    lockpath = folder / "preview.lock"
    reject_links(lockpath)
    lock = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        session = load(folder / "session.json")
        # Verify identity over loopback before trusting the private session file.
        from urllib.request import urlopen
        url = session.get("url", "")
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            raise RuntimeError("Sessão ocupada sem endereço local válido")
        with urlopen(f"http://127.0.0.1:{parsed.port}/api/health", timeout=2) as response:
            health = json.load(response)
        expected = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        if health.get("app") != "emulation-preview" or health.get("project_id") != expected:
            raise RuntimeError("Sessão ocupada por outra aplicação")
        if open_window:
            open_browser(url)
        print("Preview já aberto: " + url.split("#")[0], flush=True)
        os.close(lock)
        return
    app = PreviewApp(root)
    if session_token:
        app.token = session_token
    write_report(app.snapshot, root)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.app = app
    url = f"http://127.0.0.1:{server.server_port}/#session={app.token}"
    json_write(folder / "session.json", dict(pid=os.getpid(), url=url, created_at=now()), private=True)
    print(f"Preview: http://127.0.0.1:{server.server_port}/", flush=True)
    if open_window:
        open_browser(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.executor.shutdown(wait=True)
        (folder / "session.json").unlink(missing_ok=True)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "serve", "check"], nargs="?", default="serve")
    parser.add_argument("--open", action="store_true", help="abre o navegador padrão")
    parser.add_argument("--port", type=int, default=0, help="porta loopback; 0 escolhe uma livre")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == "check":
        print(json.dumps(dict(version=VERSION, root=str(ROOT), html=(ROOT / REPORT).is_file(),
            template=(ROOT / "tools/roms/preview-ui.html").is_file(), python=sys.version.split()[0],
            no_payload_changes=True), ensure_ascii=False))
    elif args.command == "build":
        snapshot = build_snapshot()
        path = write_report(snapshot)
        print(json.dumps(dict(report=str(path), generated_at=snapshot["generated_at"], metrics=snapshot["metrics"],
            ready=sum(a["ready"] and a["mutation"] for a in snapshot["actions"]),
            blocked=sum(not a["ready"] for a in snapshot["actions"])), ensure_ascii=False))
    else:
        serve(ROOT, args.port, args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
