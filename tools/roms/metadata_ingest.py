"""Ingest ES-DE and Skraper metadata into the canonical library and RetroFE.

The ES-DE gamelist is the structured source of truth after a conservative merge.
Skraper may export a gamelist only into the managed session staging directory;
this module merges it and does not leave RetroFE pointed at that staging.
HyperList ``description`` stays the game title. The synopsis, when the source
actually has one, is ``media/<system_id>/story/<exact-ROM-basename>.txt``.
"""
from __future__ import annotations

import base64
from datetime import datetime
import gzip
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

PROTECTED_GAMELIST_TAGS = {"favorite", "playcount", "lastplayed", "hidden", "kidgame", "path"}
TAG_RE = re.compile(r"<[^>]+>")
STORY_LINE_RE = re.compile(r"^[ \t]*media\.story[ \t]*=[ \t]*(\S*)[ \t]*$", re.MULTILINE)
SAFE_SYSTEM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_story(raw: str) -> str:
    """Turn provider markup into paragraphs. Tags are stripped, never interpreted."""
    if not raw or not raw.strip():
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*(?:p|div|li|h[1-6])\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*(?:p|div|li|h[1-6])(?:\s[^>]*)?>", "", text, flags=re.IGNORECASE)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = TAG_RE.sub("", text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    output = []
    blanks = 0
    for line in lines:
        if not line:
            blanks += 1
            if blanks == 1 and output:
                output.append("")
            continue
        blanks = 0
        output.append(line)
    return "\n".join(output).strip()


def snapshot_gamelists(project: Path) -> dict:
    root = Path(project) / "frontend/ES-DE/UserData/gamelists"
    found = {}
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/gamelist.xml")):
        if path.is_symlink() or not path.is_file():
            continue
        found[path.relative_to(project).as_posix()] = sha256_bytes(path.read_bytes())
    return found


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(game, name: str):
    for child in list(game):
        if _local(child.tag) == name:
            return child
    return None


def _text(game, name: str) -> str:
    node = _child(game, name)
    return (node.text or "").strip() if node is not None else ""


def resolve_rom(rom_root: Path, raw: str):
    """Return the ROM path, None when it is absent, or 'rejected' when unsafe."""
    text = (raw or "").strip()
    if not text:
        return None
    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return "rejected"
    relative = Path(normalized)
    if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
        return "rejected"
    candidate = rom_root.joinpath(*relative.parts)
    try:
        if candidate.is_symlink():
            target = candidate.resolve()
            target.relative_to(rom_root.resolve())
        elif candidate.exists():
            candidate.resolve().relative_to(rom_root.resolve())
    except (OSError, ValueError):
        return "rejected"
    if not candidate.is_file():
        return None
    return candidate


def _owned_maps(ops_root: Path):
    stories: dict[str, set[str]] = {}
    fields: dict[tuple, set[str]] = {}
    if not ops_root or not Path(ops_root).is_dir():
        return stories, fields
    for path in Path(ops_root).glob("*/metadata-ingest.json"):
        if path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in data.get("stories", []):
            if item.get("action") in {"created", "updated"} and item.get("relative") and item.get("sha256"):
                stories.setdefault(item["relative"], set()).add(item["sha256"])
        for item in data.get("owned_fields", []):
            key = (item.get("collection", ""), item.get("game", ""), item.get("field", ""))
            if item.get("sha256"):
                fields.setdefault(key, set()).add(item["sha256"])
    return stories, fields


def _materializer():
    path = Path(__file__).resolve().parents[2] / "frontend/RetroFE/Tools/materializers/materialize-esde-metadata.py"
    spec = importlib.util.spec_from_file_location("metadata_ingest_hyperlist", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(temporary, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _file_record(project: Path, path: Path, before: bytes | None, after: bytes, kind: str, **extra) -> dict:
    relative = path.relative_to(project).as_posix()
    return {
        "path": relative, "kind": kind,
        "before_sha256": sha256_bytes(before) if before is not None else None,
        "after_sha256": sha256_bytes(after),
        "before_gzip_base64": base64.b64encode(gzip.compress(before)).decode() if before is not None else None,
        **extra,
    }


def merge_gamelist_bytes(canonical: bytes | None, incoming: bytes, rom_root: Path):
    """Fill empty canonical fields from a staging gamelist. Return (bytes or None, report)."""
    report = {"changed": False, "filled": 0, "conflicts": [], "added": 0, "error": ""}
    try:
        incoming_root = ET.fromstring(incoming)
    except ET.ParseError as exc:
        report["error"] = f"gamelist de entrada inválida: {exc}"
        return None, report
    if canonical is None:
        root = ET.Element("gameList")
        existing = {}
    else:
        try:
            root = ET.fromstring(canonical)
        except ET.ParseError as exc:
            report["error"] = f"gamelist canônica inválida; preservada: {exc}"
            return None, report
        existing = {_path_key(_text(game, "path")): game for game in root.iter("game") if _text(game, "path")}
    for game in list(incoming_root.iter("game")):
        key = _path_key(_text(game, "path"))
        if not key:
            continue
        target = existing.get(key)
        if target is None:
            resolved = resolve_rom(rom_root, _text(game, "path"))
            if resolved in {None, "rejected"}:
                continue
            root.append(game)
            existing[key] = game
            report["added"] += 1
            report["changed"] = True
            continue
        for child in list(game):
            tag = _local(child.tag)
            if tag in PROTECTED_GAMELIST_TAGS:
                continue
            value = (child.text or "").strip()
            if not value:
                continue
            current = _child(target, tag)
            if current is None:
                target.append(child)
                report["filled"] += 1
                report["changed"] = True
                continue
            current_text = (current.text or "").strip()
            if not current_text:
                current.text = child.text
                report["filled"] += 1
                report["changed"] = True
            elif current_text != value:
                report["conflicts"].append({"path": key, "field": tag, "kept": "canonical"})
    if not report["changed"]:
        return None, report
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), report


def _path_key(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def _language(game) -> str:
    node = _child(game, "desc")
    if node is not None and node.get("lang"):
        return node.get("lang")
    return game.get("{http://www.w3.org/XML/1998/namespace}lang") or "und"


def publish_stories(project: Path, system_id: str, gamelist: Path, provider: str, owned: dict, records: dict):
    rom_root = project / "roms" / system_id
    try:
        root = ET.parse(gamelist).getroot()
    except (ET.ParseError, OSError) as exc:
        records["notes"].append({"system_id": system_id, "status": "gamelist_invalida", "error": str(exc)})
        return
    grouped = {}
    for game in root.iter("game"):
        raw = _text(game, "path")
        resolved = resolve_rom(rom_root, raw)
        if resolved == "rejected":
            records["notes"].append({"system_id": system_id, "status": "caminho_recusado", "path": raw})
            continue
        if resolved is None:
            records["notes"].append({"system_id": system_id, "status": "rom_ausente", "path": raw})
            continue
        grouped.setdefault(resolved.stem, []).append((game, resolved))
    for stem, items in grouped.items():
        if len(items) != 1:
            records["notes"].append({"system_id": system_id, "status": "basename_ambiguo", "basename": stem, "count": len(items)})
            continue
        game, rom = items[0]
        story = normalize_story(_text(game, "desc"))
        relative = f"media/{system_id}/story/{stem}.txt"
        target = project / relative
        if not story:
            records["absent"].append({"system_id": system_id, "rom": rom.relative_to(project).as_posix(), "status": "sem_sinopse"})
            continue
        payload = (story + "\n").encode("utf-8")
        digest = sha256_bytes(payload)
        language = _language(game)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            records["conflicts"].append({"path": relative, "status": "destino_nao_regular"})
            continue
        before = target.read_bytes() if target.is_file() else None
        if before == payload:
            records["stories"].append({"relative": relative, "action": "reused", "sha256": digest, "provider": provider, "language": language})
            continue
        if before is not None and sha256_bytes(before) not in owned.get(relative, set()):
            records["conflicts"].append({"path": relative, "status": "edicao_manual_preservada", "provider": provider})
            continue
        _write_atomic(target, payload)
        action = "updated" if before is not None else "created"
        record = _file_record(project, target, before, payload, "story", action=action, provider=provider,
                              language=language, rom=rom.relative_to(project).as_posix(), sha256=digest, relative=relative)
        records["files"].append(record)
        records["stories"].append(record)
        owned.setdefault(relative, set()).add(digest)


def _list_system(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")) or not stripped.lower().startswith("list.path"):
            continue
        value = stripped.split("=", 1)[1].strip().replace("\\", "/").rstrip("/")
        return value.rsplit("/", 1)[-1]
    return ""


def point_story_settings(project: Path, system_id: str, records: dict) -> None:
    collections = project / "frontend/RetroFE/collections"
    if not collections.is_dir():
        return
    expected = f"../../media/{system_id}/story"
    for settings in sorted(collections.glob("*/settings.conf")):
        if settings.is_symlink() or not settings.is_file():
            continue
        text = settings.read_text(encoding="utf-8")
        if _list_system(text) != system_id:
            continue
        match = STORY_LINE_RE.search(text)
        if match:
            current = match.group(1).replace("\\", "/")
            if current == expected:
                continue
            if current and "medium_artwork" not in current:
                records["notes"].append({"system_id": system_id, "collection": settings.parent.name,
                                         "status": "media_story_personalizado_preservado", "current": current})
                continue
            updated = text[:match.start()] + f"media.story = {expected}" + text[match.end():]
        else:
            updated = text.rstrip() + f"\nmedia.story = {expected}\n"
        before = text.encode("utf-8")
        after = updated.encode("utf-8")
        if before == after:
            continue
        _write_atomic(settings, after)
        records["files"].append(_file_record(project, settings, before, after, "settings"))


def project_hyperlists(project: Path, system_id: str, records: dict, owned_fields: dict) -> list[dict]:
    profile = project / "frontend/RetroFE/Config/profiles/host-linux.json"
    gamelist = project / "frontend/ES-DE/UserData/gamelists" / system_id / "gamelist.xml"
    if not profile.is_file() or not gamelist.is_file():
        records["notes"].append({"system_id": system_id, "status": "hyperlist_nao_aplicavel"})
        return []
    module = _materializer()
    try:
        entries, skipped = module.esde_entries(system_id, project, gamelist.parents[1])
        mapping = module.collections_by_system(profile)
    except (ET.ParseError, OSError, ValueError) as exc:
        records["notes"].append({"system_id": system_id, "status": "hyperlist_fonte_invalida", "error": str(exc)})
        return []
    rows = []
    for collection in mapping.get(system_id, []):
        if "/" in collection or collection.startswith("."):
            records["notes"].append({"system_id": system_id, "collection": collection, "status": "nome_invalido"})
            continue
        path = project / "frontend/RetroFE/meta/hyperlist" / f"{collection}.xml"
        if path.is_symlink():
            records["notes"].append({"system_id": system_id, "collection": collection, "status": "symlink_preservado"})
            continue

        def owned(collection_name, game, field, current, _owned=owned_fields):
            return sha256_bytes(current.encode("utf-8")) in _owned.get((collection_name, game, field), set())

        try:
            after, counts = module.merge(path, collection, entries, owned)
        except (ET.ParseError, ValueError) as exc:
            records["notes"].append({"system_id": system_id, "collection": collection, "status": "hyperlist_invalida_preservada", "error": str(exc)})
            continue
        if after is None:
            continue
        before = path.read_bytes() if path.is_file() else None
        _write_atomic(path, after)
        records["files"].append(_file_record(project, path, before, after, "hyperlist", collection=collection, **counts))
        for name, values in entries.items():
            rows.append({"collection": collection, "name": name, "title": values.get("description", ""),
                         "year": values.get("year", ""), "manufacturer": values.get("manufacturer", ""),
                         "developer": values.get("developer", ""), "genre": values.get("genre", ""),
                         "players": values.get("players", ""), "score": values.get("score", "")})
    records["notes"].append({"system_id": system_id, "skipped": skipped, "status": "hyperlist_projetada"})
    return rows


def project_meta(project: Path, rows: list[dict], operation: Path, records: dict, owned_fields: dict) -> None:
    database = project / "frontend/RetroFE/meta.db"
    if not rows or not database.is_file() or database.is_symlink():
        records["meta_db"] = {"status": "absent" if not database.is_file() else "unchanged"}
        return
    before = database.read_bytes()
    preimage = operation / "rollback/meta.db.before"
    if not preimage.exists():
        preimage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(database, preimage)
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN")
        for row in rows:
            current = connection.execute(
                "SELECT title, year, manufacturer, developer, genre, players, score FROM Meta WHERE collectionName=? AND name=?",
                (row["collection"], row["name"])).fetchone()
            columns = ("title", "year", "manufacturer", "developer", "genre", "players", "score")
            if current is None:
                connection.execute(
                    "INSERT INTO Meta(collectionName, name, title, year, manufacturer, developer, genre, cloneOf, players, ctrltype, buttons, joyways, rating, score) "
                    "VALUES (?,?,?,?,?,?,?,'',?,'','','','',?)",
                    (row["collection"], row["name"], row["title"], row["year"], row["manufacturer"], row["developer"],
                     row["genre"], row["players"], row["score"]))
                continue
            updates = {}
            for column, old in zip(columns, current):
                new = row.get(column, "")
                old = old or ""
                if not new or new == old:
                    continue
                key = (row["collection"], row["name"], "description" if column == "title" else column)
                if not old.strip() or sha256_bytes(old.encode("utf-8")) in owned_fields.get(key, set()):
                    updates[column] = new
                else:
                    records["conflicts"].append({"collection": row["collection"], "game": row["name"], "field": column, "status": "meta_preservado"})
            if updates:
                assignment = ", ".join(f"{column}=?" for column in updates)
                connection.execute(
                    f"UPDATE Meta SET {assignment} WHERE collectionName=? AND name=?",
                    (*updates.values(), row["collection"], row["name"]))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = database.read_bytes()
    records["meta_db"] = {"status": "updated" if after != before else "unchanged",
                          "before_sha256": sha256_bytes(before), "after_sha256": sha256_bytes(after),
                          "preimage_relative": preimage.relative_to(operation).as_posix()}
    if after == before and preimage.exists() and not any(item.get("kind") == "hyperlist" for item in records["files"]):
        preimage.unlink()


def refresh_derivatives(project: Path, needed: bool) -> dict:
    script = project / "frontend/RetroFE/Tools/materializers/materialize-aeon-nox-navigation.py"
    database = project / "frontend/RetroFE/meta.db"
    if not needed or not script.is_file() or not database.is_file():
        return {"status": "skipped"}
    result = subprocess.run([sys.executable, str(script), "--apply"], cwd=project / "frontend/RetroFE",
                            capture_output=True, text=True)
    return {"status": "applied" if result.returncode == 0 else "failed", "returncode": result.returncode,
            "stderr": (result.stderr or "")[-500:]}


def _changed_systems(project: Path, snapshot, staging: Path) -> list[str]:
    systems = set()
    if staging.is_dir():
        for path in staging.glob("*/gamelist.xml"):
            if path.is_file() and not path.is_symlink() and SAFE_SYSTEM_RE.fullmatch(path.parent.name):
                systems.add(path.parent.name)
    if isinstance(snapshot, dict):
        current = snapshot_gamelists(project)
        for key, digest in current.items():
            if snapshot.get(key) != digest:
                system_id = Path(key).parent.name
                if SAFE_SYSTEM_RE.fullmatch(system_id):
                    systems.add(system_id)
    return sorted(systems)


def _merge_staging(project: Path, system_id: str, staging: Path, records: dict) -> None:
    incoming = staging / system_id / "gamelist.xml"
    if not incoming.is_file() or incoming.is_symlink():
        return
    canonical = project / "frontend/ES-DE/UserData/gamelists" / system_id / "gamelist.xml"
    if canonical.is_symlink():
        records["notes"].append({"system_id": system_id, "status": "gamelist_symlink_preservada"})
        return
    rom_root = project / "roms" / system_id
    before = canonical.read_bytes() if canonical.is_file() else None
    updated, report = merge_gamelist_bytes(before, incoming.read_bytes(), rom_root)
    records["conflicts"].extend({"system_id": system_id, **item} for item in report["conflicts"])
    if report["error"]:
        records["notes"].append({"system_id": system_id, "status": "merge_recusado", "error": report["error"]})
        return
    if updated is None:
        return
    _write_atomic(canonical, updated)
    records["files"].append(_file_record(project, canonical, before, updated, "gamelist"))
    records["gamelists_changed"] += 1


def ingest_systems(api, session: dict, systems: list[str], provider: str) -> dict:
    project = Path(api["PROJECT_ROOT"])
    operation = Path(api["operation_dir"](session["operation_id"]))
    operation.mkdir(parents=True, exist_ok=True)
    ops_root = Path(api.get("OPS_ROOT", operation.parent))
    story_owned, field_owned = _owned_maps(ops_root)
    records = {"files": [], "stories": [], "conflicts": [], "absent": [], "notes": [], "owned_fields": [],
               "gamelists_changed": 0, "meta_db": {"status": "unchanged"}}
    staging = operation / "staging/gamelists"
    meta_rows = []
    for system_id in systems:
        if not SAFE_SYSTEM_RE.fullmatch(system_id):
            records["notes"].append({"system_id": system_id, "status": "sistema_recusado"})
            continue
        rom_root = project / "roms" / system_id
        if rom_root.is_symlink() or not rom_root.is_dir():
            records["notes"].append({"system_id": system_id, "status": "rom_root_ausente"})
            continue
        _merge_staging(project, system_id, staging, records)
        gamelist = project / "frontend/ES-DE/UserData/gamelists" / system_id / "gamelist.xml"
        if not gamelist.is_file() or gamelist.is_symlink():
            records["notes"].append({"system_id": system_id, "status": "gamelist_ausente"})
            continue
        source = "skraper-gamelist" if (staging / system_id / "gamelist.xml").is_file() else provider
        publish_stories(project, system_id, gamelist, source if provider == "skraper" else "esde-gamelist", story_owned, records)
        meta_rows.extend(project_hyperlists(project, system_id, records, field_owned))
        if any(item.get("kind") == "story" and item.get("action") in {"created", "updated"} and f"/{system_id}/" in item["path"] for item in records["files"]):
            point_story_settings(project, system_id, records)
    project_meta(project, meta_rows, operation, records, field_owned)
    hyperlists = sum(item.get("kind") == "hyperlist" for item in records["files"])
    stories_written = sum(item.get("kind") == "story" and item.get("action") in {"created", "updated"} for item in records["files"])
    derivatives = refresh_derivatives(project, bool(hyperlists or records["meta_db"].get("status") == "updated"))
    document = {
        "schema_version": 1, "kind": "metadata_ingest", "operation_id": session["operation_id"],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"), "status": "applied",
        "provider": provider, "systems": systems, "files": records["files"], "stories": records["stories"],
        "conflicts": records["conflicts"], "absent": records["absent"], "notes": records["notes"],
        "owned_fields": records["owned_fields"], "meta_db": records["meta_db"], "derivatives": derivatives,
        "source_of_truth": "frontend/ES-DE/UserData/gamelists/<system_id>/gamelist.xml",
        "precedence": "Canonical ES-DE fields win. Skraper fills empty fields only. Manual story bytes that were not published by this flow are preserved.",
        "rollback": "Restore each preimage, or move a created file into the operation rollback, only while the current hash is after_sha256.",
    }
    changed = bool(records["files"]) or records["meta_db"].get("status") == "updated"
    relative = ""
    if changed or records["conflicts"] or records["absent"]:
        path = operation / "metadata-ingest.json"
        _write_atomic(path, json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
        relative = path.relative_to(project).as_posix() if path.is_relative_to(project) else str(path)
    return {"ok": True, "status": "applied" if changed else "unchanged", "stories_written": stories_written,
            "conflicts": len(records["conflicts"]), "hyperlists": hyperlists,
            "gamelists_changed": records["gamelists_changed"], "absent_stories": len(records["absent"]),
            "manifest_relative": relative, "derivatives": derivatives, "conflict_items": records["conflicts"],
            "absent_items": records["absent"]}


def ingest_session(api, manifest) -> dict:
    project = Path(api["PROJECT_ROOT"])
    operation = Path(api["operation_dir"](manifest["operation_id"]))
    systems = _changed_systems(project, manifest.get("gamelist_snapshot"), operation / "staging/gamelists")
    if not systems:
        return {"ok": True, "status": "unchanged", "stories_written": 0, "conflicts": 0, "hyperlists": 0,
                "gamelists_changed": 0, "absent_stories": 0}
    provider = "skraper" if manifest.get("client") == "skraper" else "esde-gamelist"
    return ingest_systems(api, manifest, systems, provider)


def ingest_project(api, systems=None, client="manual") -> dict:
    project = Path(api["PROJECT_ROOT"])
    if not systems:
        root = project / "frontend/ES-DE/UserData/gamelists"
        systems = sorted(path.name for path in root.iterdir() if (path / "gamelist.xml").is_file()) if root.is_dir() else []
    session = {"operation_id": "metadata-ingest-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8],
               "client": client, "gamelist_snapshot": None}
    return ingest_systems(api, session, list(systems), client)


def assert_rollback_allowed(project: Path, document: dict) -> None:
    for item in document.get("files", []):
        path = project / item["path"]
        current = sha256_bytes(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
        if current != item["after_sha256"]:
            raise ValueError(f"{item['path']} mudou após a ingestão; rollback recusado")
    meta = document.get("meta_db") or {}
    database = project / "frontend/RetroFE/meta.db"
    if meta.get("status") == "updated":
        current = sha256_bytes(database.read_bytes()) if database.is_file() else None
        if current != meta.get("after_sha256"):
            raise ValueError("meta.db mudou após a ingestão; rollback recusado")


def rollback(project: Path, document: dict, operation: Path) -> dict:
    assert_rollback_allowed(project, document)
    quarantine = operation / "rollback/created"
    for item in reversed(document.get("files", [])):
        target = project / item["path"]
        if item.get("before_gzip_base64") is None:
            destination = quarantine / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                os.replace(target, destination)
        else:
            _write_atomic(target, gzip.decompress(base64.b64decode(item["before_gzip_base64"])))
    meta = document.get("meta_db") or {}
    if meta.get("status") == "updated":
        preimage = operation / meta["preimage_relative"]
        database = project / "frontend/RetroFE/meta.db"
        os.replace(preimage, database)
    document["status"] = "rolled_back"
    _write_atomic(operation / "metadata-ingest.json", json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"status": "rolled_back", "files": len(document.get("files", []))}


def pending_path(api) -> Path:
    return Path(api["OPS_ROOT"]) / "metadata-ingest-pending.json"


def consume_pending(api) -> dict:
    path = pending_path(api)
    if not path.is_file():
        return {"ok": True, "status": "absent"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "status": "pending_unreadable"}
    fingerprint = snapshot_gamelists(Path(api["PROJECT_ROOT"]))
    if data.get("attempts", 0) >= 2 and data.get("fingerprint") == fingerprint:
        return {"ok": False, "status": "retry_exhausted", "error": data.get("error", "")}
    result = ingest_project(api, systems=data.get("systems"), client=data.get("client", "recovery"))
    if result.get("ok") and result.get("status") != "failed":
        path.unlink(missing_ok=True)
        return result
    data["attempts"] = int(data.get("attempts", 0)) + 1
    data["fingerprint"] = fingerprint
    data["error"] = result.get("error", "")
    _write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    return result
