"""Project verified destinations into the current catalog without rewriting the operation journal."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import uuid
from pathlib import Path

try:
    from . import media_session
except ImportError:
    import media_session


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def _operation_id() -> str:
    return "catalog-project-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _save(api, manifest: dict) -> None:
    path = Path(api["operation_dir"](manifest["operation_id"])) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    api["atomic_json"](path, manifest)


def _evidence(project: Path, source_manifest: dict) -> list[dict]:
    rows = []
    for item in source_manifest.get("items", []):
        destination = Path(str(item.get("destination", "")))
        after = item.get("after") or {}
        expected = str(after.get("sha256", ""))
        size = after.get("size_bytes")
        record = {
            "item_id": item.get("item_id", ""),
            "system_id": item.get("system_id", ""),
            "classification": item.get("classification", ""),
            "source": str(item.get("source", "")),
            "destination": str(destination),
            "expected_sha256": expected,
            "expected_size_bytes": size,
            "journal_status": item.get("status", ""),
            "source_exists": Path(str(item.get("source", ""))).is_file() if item.get("source") else False,
        }
        inside = False
        try:
            inside = destination.resolve(strict=False).is_relative_to(project.resolve())
        except (OSError, ValueError):
            inside = False
        if item.get("status") != "completed_verified" or not expected or not inside:
            record.update(action="blocked", reason="item fora do estado verificado ou destino fora da raiz")
        elif destination.is_symlink() or not destination.is_file():
            record.update(action="blocked", reason="destino ausente ou symlink")
        elif destination.stat().st_size != size or _sha_file(destination) != expected:
            record.update(action="blocked", reason="hash ou tamanho atual diverge do diário")
        else:
            record.update(action="project", reason="destino presente com o SHA-256 registrado",
                          current_sha256=expected, current_size_bytes=size)
        rows.append(record)
    return rows


def _lists(api) -> dict[str, Path]:
    output = Path(api["OUTPUT_ROOT"])
    return {"catalog": output / "catalogo-arquivos.csv",
            "pending": output / "roms-pendentes-validacao-exclusao.csv",
            "state": output / "estado-final-verificacao.json",
            "integrity": output / "pendencias-integridade-dependencias.csv"}


def plan(api, source_operation_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", source_operation_id):
        raise ValueError("ID de operação inválido")
    project = Path(api["PROJECT_ROOT"])
    source_path = Path(api["operation_dir"](source_operation_id)) / "manifest.json"
    source_manifest = json.loads(source_path.read_text(encoding="utf-8"))
    if source_manifest.get("operation_id") != source_operation_id:
        raise ValueError("Manifesto de origem divergente")
    evidence = _evidence(project, source_manifest)
    lists = _lists(api)
    for path in lists.values():
        api['reject_symlink_components'](path)
        if path.exists() and not path.is_file():
            raise ValueError('Registro não é arquivo regular: ' + str(path))
    digests = {name: _sha_file(path) if path.is_file() and not path.is_symlink() else "" for name, path in lists.items()}
    matched = 0
    if lists["catalog"].is_file():
        _fields, rows = _read_csv(lists["catalog"])
        destinations = {item["destination"] for item in evidence if item["action"] == "project"}
        sources = {item["source"] for item in evidence if item["action"] == "project"}
        for row in rows:
            values = {row.get("absolute_path", ""), row.get("current_absolute_path", "")}
            if values & destinations or values & sources:
                matched += 1
    operation_id = _operation_id()
    manifest = {
        "schema_version": 1, "operation_id": operation_id, "kind": "catalog_projection",
        "created_at": api["now"](), "status": "prepared",
        "source_operation_id": source_operation_id,
        "source_manifest_sha256": _sha_file(source_path),
        "journal_rewritten": False, "permanent_deletion": False,
        "list_sha256_before": digests, "evidence": evidence,
        "summary": {"items": len(evidence), "project": sum(item["action"] == "project" for item in evidence),
                    "blocked": sum(item["action"] == "blocked" for item in evidence),
                    "catalog_rows_matching_before": matched,
                    "source_still_present": sum(item["source_exists"] for item in evidence)},
        "limits": ["O diário da operação de origem não é reescrito.",
                   "Bytes retidos em rollback de operações anteriores não são espaço liberado."],
    }
    _save(api, manifest)
    api["append_event"](operation_id, "catalog_projection_planned", action="catalog_projection_plan",
                        status="prepared", details={"summary": manifest["summary"], "source_operation_id": source_operation_id})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": "catalog_projection", "status": "prepared",
            "manifest": str(Path(api["operation_dir"](operation_id)) / "manifest.json"),
            "summary": manifest["summary"]}


def _load(api, operation_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", operation_id):
        raise ValueError("ID de operação inválido")
    path = Path(api["operation_dir"](operation_id)) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "catalog_projection" or manifest.get("operation_id") != operation_id:
        raise ValueError("Operação não é catalog_projection")
    return manifest


def _check_preconditions(manifest: dict, lists: dict[str, Path]) -> None:
    for name, path in lists.items():
        expected = manifest["list_sha256_before"].get(name, "")
        current = _sha_file(path) if path.is_file() and not path.is_symlink() else ""
        if current != expected:
            raise ValueError(f"Lista mudou desde o plano: {name}")


def _apply_catalog(rows: list[dict], fields: list[str], evidence: list[dict], source_operation_id: str,
                   project: Path) -> tuple[list[dict], int, int]:
    for field in ("current_sha256", "current_size_bytes", "current_operation_id", "current_path_status", "current_absolute_path"):
        if field not in fields:
            fields.append(field)
    updated = 0
    appended = 0
    by_destination = {item["destination"]: item for item in evidence if item["action"] == "project"}
    by_source = {item["source"]: item for item in evidence if item["action"] == "project" and item["source"]}
    seen_destinations = set()
    for row in rows:
        item = by_destination.get(row.get("current_absolute_path", "")) or by_destination.get(row.get("absolute_path", ""))
        item = item or by_source.get(row.get("current_absolute_path", "")) or by_source.get(row.get("absolute_path", ""))
        if not item:
            continue
        row.update(current_absolute_path=item["destination"], current_sha256=item["current_sha256"],
                   current_size_bytes=item["current_size_bytes"], current_operation_id=source_operation_id,
                   current_path_status="destino verificado; diário histórico preservado")
        seen_destinations.add(item["destination"])
        updated += 1
    for item in by_destination.values():
        if item["destination"] in seen_destinations:
            continue
        destination = Path(item["destination"])
        row = {field: "" for field in fields}
        row.update(system_id=item.get("system_id", ""), source="operacao_verificada",
                   file_name=destination.name, absolute_path=item["destination"],
                   size_bytes=item["current_size_bytes"], sha256=item["current_sha256"],
                   hash_status="SHA-256 integral verificado na projeção",
                   current_absolute_path=item["destination"], current_sha256=item["current_sha256"],
                   current_size_bytes=item["current_size_bytes"], current_operation_id=source_operation_id,
                   current_path_status="destino verificado; linha nova porque o catálogo não tinha este caminho")
        try:
            row["relative_path"] = destination.resolve().relative_to(project).as_posix()
        except ValueError:
            row["relative_path"] = destination.name
        rows.append({key: row.get(key, "") for key in fields})
        appended += 1
    return rows, updated, appended


def _apply_pending(rows: list[dict], evidence: list[dict], source_operation_id: str) -> tuple[list[dict], int]:
    projectable = {item["source"]: item for item in evidence if item["action"] == "project" and item["source"]}
    updated = 0
    for row in rows:
        current = row.get("current_path", "")
        item = projectable.get(current) or projectable.get(row.get("original_path", ""))
        if not item or item["source_exists"]:
            continue
        note = row.get("review_note", "")
        extra = f"Destino verificado {item['destination']} sha256 {item['current_sha256']}."
        if extra not in note:
            row["review_note"] = (note + " " + extra).strip()
        row["review_status"] = "DESTINO_VERIFICADO_PRESENTE"
        if not row.get("destination_or_canonical_peer"):
            row["destination_or_canonical_peer"] = item["destination"]
        if not row.get("operation_id"):
            row["operation_id"] = source_operation_id
        updated += 1
    return rows, updated


def _check_destinations(manifest: dict, api) -> None:
    source = Path(api["operation_dir"](manifest["source_operation_id"])) / 'manifest.json'
    expected = manifest.get('source_manifest_sha256')
    if expected and _sha_file(source) != expected:
        raise ValueError('Diário de origem mudou desde o plano')
    for item in manifest['evidence']:
        if item['action'] != 'project':
            continue
        destination = Path(item['destination'])
        api['reject_symlink_components'](destination)
        if (not destination.is_file() or destination.stat().st_size != item['expected_size_bytes']
                or _sha_file(destination) != item['expected_sha256']):
            raise ValueError('Destino divergiu do plano: ' + item['item_id'])


def _csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def _prepare_publication(manifest: dict, lists: dict[str, Path], api) -> None:
    _check_preconditions(manifest, lists)
    operation = Path(api['operation_dir'](manifest['operation_id']))
    before = {}
    modes = {}
    for name, path in lists.items():
        if not path.exists():
            continue
        before[name] = path.read_bytes()
        if _sha_bytes(before[name]) != manifest['list_sha256_before'][name]:
            raise ValueError('Lista mudou durante a preparação: ' + name)
        modes[name] = path.stat().st_mode & 0o777
        snapshot = operation / 'rollback' / path.name
        api['reject_symlink_components'](snapshot)
        if snapshot.exists():
            if _sha_file(snapshot) != manifest['list_sha256_before'][name]:
                raise ValueError('Preimagem divergente: ' + name)
        else:
            api['atomic_write_bytes'](snapshot, before[name], mode=modes[name])
    proposals = dict(before)
    catalog_updated = catalog_appended = pending_updated = 0
    if 'catalog' in before:
        fields, rows = _read_csv(lists['catalog'])
        rows, catalog_updated, catalog_appended = _apply_catalog(
            rows, fields, manifest['evidence'], manifest['source_operation_id'], Path(api['PROJECT_ROOT']))
        proposals['catalog'] = _csv_bytes(rows, fields)
    if 'pending' in before:
        fields, rows = _read_csv(lists['pending'])
        rows, pending_updated = _apply_pending(rows, manifest['evidence'], manifest['source_operation_id'])
        proposals['pending'] = _csv_bytes(rows, fields)
    if 'state' in before:
        state = json.loads(before['state'])
        state['catalog_projection'] = {
            'as_of': api['now'](), 'operation_id': manifest['operation_id'],
            'source_operation_id': manifest['source_operation_id'],
            'catalog_rows_updated': catalog_updated, 'catalog_rows_appended': catalog_appended,
            'pending_rows_marked': pending_updated, 'journal_rewritten': False, 'freed_bytes': 0}
        proposals['state'] = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()
    after = dict(manifest['list_sha256_before'])
    for name, data in proposals.items():
        snapshot = operation / 'proposed' / lists[name].name
        api['reject_symlink_components'](snapshot)
        api['atomic_write_bytes'](snapshot, data, mode=modes[name])
        after[name] = _sha_bytes(data)
    _check_preconditions(manifest, lists)
    manifest.update(list_sha256_after=after, list_modes=modes, publication_prepared=True,
                    summary={**manifest['summary'], 'catalog_rows_updated': catalog_updated,
                             'catalog_rows_appended': catalog_appended, 'pending_rows_marked': pending_updated})
    _save(api, manifest)


def _publish(manifest: dict, lists: dict[str, Path], api, *, restore: bool) -> None:
    operation = Path(api['operation_dir'](manifest['operation_id']))
    hashes = manifest['list_sha256_before' if restore else 'list_sha256_after']
    snapshots = operation / ('rollback' if restore else 'proposed')
    # Validate the entire metadata batch before publishing its first file.
    for name, path in lists.items():
        api['reject_symlink_components'](path)
        current = _sha_file(path) if path.is_file() else ''
        if current not in {manifest['list_sha256_before'][name], manifest['list_sha256_after'][name]}:
            raise ValueError('Lista mudou durante a projeção: ' + name)
        mode = manifest.get('list_modes', {}).get(name)
        if mode is not None and path.stat().st_mode & 0o777 != mode:
            raise ValueError('Permissões da lista mudaram: ' + name)
        if hashes[name]:
            snapshot = snapshots / path.name
            api['reject_symlink_components'](snapshot)
            if _sha_file(snapshot) != hashes[name]:
                raise ValueError('Snapshot divergente: ' + name)
    manifest['status'] = 'rolling_back' if restore else 'applying'
    _save(api, manifest)
    for name, path in lists.items():
        if not hashes[name] or _sha_file(path) == hashes[name]:
            continue
        manifest['publication_intent'] = {'list': name, 'sha256': hashes[name], 'restore': restore}
        _save(api, manifest)
        mode = manifest.get('list_modes', {}).get(name, path.stat().st_mode & 0o777)
        api['atomic_write_bytes'](path, (snapshots / path.name).read_bytes(), mode=mode)
        manifest.setdefault('publication_checkpoints', {})[name] = {'sha256': hashes[name], 'restore': restore}
        _save(api, manifest)
    manifest['status'] = 'rolled_back' if restore else 'completed_verified'
    manifest['completed_at'] = api['now']()
    _save(api, manifest)


def apply(operation_id: str, confirmed: bool, api) -> dict:
    with media_session.write_lock(api):
        return _apply(operation_id, confirmed, api)


def _apply(operation_id: str, confirmed: bool, api) -> dict:
    if not confirmed:
        raise ValueError("Aplicação exige --apply")
    manifest = _load(api, operation_id)
    if manifest["status"] not in {'prepared', 'applying'}:
        raise ValueError(f"Plano não aplicável: {manifest['status']}")
    lists = _lists(api)
    _check_destinations(manifest, api)
    operation = Path(api["operation_dir"](operation_id))
    if not manifest.get('publication_prepared'):
        if manifest['status'] != 'prepared':
            raise ValueError('Publicação sem staging durável; reconciliar operação')
        for path in lists.values():
            api['reject_symlink_components'](path)
        _prepare_publication(manifest, lists, api)
    _publish(manifest, lists, api, restore=False)
    api["append_event"](operation_id, "catalog_projection_applied", action="catalog_projection_apply",
                        status="completed_verified", details={"summary": manifest["summary"]})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": "catalog_projection", "status": "completed_verified",
            "manifest": str(operation / "manifest.json"), "summary": manifest["summary"]}


def verify(operation_id: str, api) -> dict:
    with media_session.write_lock(api):
        return _verify(operation_id, api)


def _verify(operation_id: str, api) -> dict:
    manifest = _load(api, operation_id)
    lists = _lists(api)
    for path in lists.values():
        api['reject_symlink_components'](path)
    failed = [name for name, path in lists.items()
              if (_sha_file(path) if path.is_file() else "") != manifest.get("list_sha256_after", {}).get(name, "")]
    bad = []
    for item in manifest["evidence"]:
        if item["action"] != "project":
            continue
        destination = Path(item["destination"])
        api['reject_symlink_components'](destination)
        if not destination.is_file() or destination.is_symlink() or _sha_file(destination) != item["expected_sha256"]:
            bad.append(item["item_id"])
    source = Path(api['operation_dir'](manifest['source_operation_id'])) / 'manifest.json'
    journal_changed = bool(manifest.get('source_manifest_sha256') and
                           _sha_file(source) != manifest['source_manifest_sha256'])
    status = ("verified" if manifest['status'] == 'completed_verified' and not failed and not bad
              and not journal_changed else "verify_failed")
    return {"operation_id": operation_id, "kind": "catalog_projection", "status": status,
            "list_mismatches": failed, "destination_mismatches": bad[:20], "source_journal_changed": journal_changed}


def rollback(operation_id: str, confirmed: bool, api) -> dict:
    with media_session.write_lock(api):
        return _rollback(operation_id, confirmed, api)


def _rollback(operation_id: str, confirmed: bool, api) -> dict:
    if not confirmed:
        raise ValueError("Rollback exige --rollback")
    manifest = _load(api, operation_id)
    if manifest.get("status") not in {'completed_verified', 'applying', 'rolling_back'}:
        raise ValueError("Rollback exige operação aplicada")
    lists = _lists(api)
    _check_destinations(manifest, api)
    _publish(manifest, lists, api, restore=True)
    api["append_event"](operation_id, "catalog_projection_rolled_back", action="catalog_projection_rollback",
                        status="rolled_back", details={"journal_rewritten": False})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": "catalog_projection", "status": "rolled_back"}


def dispatch(args, api) -> dict:
    command = args.project_catalog_command
    if command == "plan":
        return plan(api, args.operation_id)
    if command == "apply":
        return apply(args.operation_id, args.apply, api)
    if command == "verify":
        return verify(args.operation_id, api)
    if command == "rollback":
        return rollback(args.operation_id, args.rollback, api)
    raise ValueError(f"Comando de projeção desconhecido: {command}")
