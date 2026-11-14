#!/usr/bin/env python3
"""Convert ES-DE gamelist metadata into the HyperList files RetroFE imports.

RetroFE reads `meta/hyperlist/<collection>.xml` into `meta.db` on start (fields
description, manufacturer, developer, year, genre, rating, score, players...). The
source of truth stays `frontend/ES-DE/UserData/gamelists/<system_id>/gamelist.xml`;
this tool only projects it:

* the target collections are those whose `rom_subdir` in `Config/profiles/host-linux.json`
  is exactly the gamelist's system_id (main and subcollection overrides);
* an entry is converted only when its `<path>` resolves to an existing item in
  `roms/<system_id>/`; the HyperList key is that item's exact basename (no title matching);
* existing HyperList games and non-empty fields are preserved; ES-DE fills missing games
  and empty fields only. Story text (`<desc>`) is not a HyperList field: `description`
  stays the game title. Stories are published by tools/roms/metadata_ingest.py.

plan writes a manifest with embedded gzip preimages; apply requires unchanged preimages
and writes atomically; verify checks the after-hashes; rollback restores the preimages
only when the files still carry the after-hashes. No deletion, no ROM/media change.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT.parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
PROFILE = ROOT / "Config/profiles/host-linux.json"
GAMELISTS = PROJECT / "frontend/ES-DE/UserData/gamelists"
HYPERLIST = ROOT / "meta/hyperlist"
MANIFESTS = ROOT / "Manifests"
FIELDS = ["description", "cloneof", "crc", "manufacturer", "developer", "year", "genre", "rating", "score", "players", "enabled"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text(node, tag):
    value = node.findtext(tag)
    return value.strip() if value else ""


def rating_to_score(raw: str) -> str:
    """ES-DE rating is 0..1. Explicit 0 is a score; an empty value is absence."""
    try:
        value = float(raw)
    except ValueError:
        return ""
    if 0 <= value <= 1:
        return f"{value * 5:.1f}"
    return ""


def collections_by_system(profile=None):
    profile = json.loads((PROFILE if profile is None else profile).read_text(encoding="utf-8"))
    result = {}
    for group in ("collections", "subcollection_overrides"):
        for name, value in profile.get(group, {}).items():
            system = str(value.get("rom_subdir", "")).strip("/")
            if system:
                result.setdefault(system, []).append(name)
    return result


def esde_entries(system, project=None, gamelists=None):
    """{exact basename: fields} for gamelist entries whose path is an existing ROM item."""
    project = PROJECT if project is None else Path(project)
    gamelists = GAMELISTS if gamelists is None else Path(gamelists)
    rom_root = (project / "roms" / system).resolve()
    from tools.roms.media_session import _track_paths

    tracks = _track_paths(rom_root, include_playlists=True)
    tree = ET.parse(gamelists / system / "gamelist.xml")
    entries, banned, skipped = {}, set(), {"missing_item": 0, "outside_root": 0, "duplicate_basename": 0, "dependent_track": 0}
    for game in tree.getroot().iter("game"):
        raw = text(game, "path")
        if not raw:
            continue
        item = (rom_root / raw).resolve()
        if not item.is_relative_to(rom_root):
            skipped["outside_root"] += 1
            continue
        if not item.exists():
            skipped["missing_item"] += 1
            continue
        if item in tracks:
            skipped["dependent_track"] += 1
            continue
        key = item.stem if item.is_file() else item.name
        if key in entries or key in banned:
            skipped["duplicate_basename"] += 1
            entries.pop(key, None)
            banned.add(key)
            continue
        date = text(game, "releasedate")
        score = rating_to_score(text(game, "rating"))  # ES-DE 0..1 → RetroFE score 0..5; never the age rating
        entries[key] = {
            "description": text(game, "name"),
            "manufacturer": text(game, "publisher") or text(game, "developer"),
            "developer": text(game, "developer"),
            "year": date[:4] if date[:4].isdigit() else "",
            "genre": text(game, "genre"),
            "score": score,
            "players": text(game, "players"),
        }
    return entries, skipped


def merge(path, collection, entries, owned_fields=None):
    """Return (new bytes or None when unchanged, counts).

    Non-empty fields stay unless owned_fields(collection, game, field, current)
    says the current value is a previously published generated value.
    """
    counts = {"games_added": 0, "fields_filled": 0, "fields_updated": 0, "games_preserved": 0}
    if path.exists():
        root = ET.parse(path).getroot()
        if root.tag != "menu":
            raise ValueError(f"{path} não é HyperList (<menu> ausente)")
    else:
        root = ET.Element("menu")
        header = ET.SubElement(root, "header")
        ET.SubElement(header, "listname").text = collection
        ET.SubElement(header, "exporterversion").text = "materialize-esde-metadata.py (ES-DE gamelist)"
    existing = {game.get("name"): game for game in root.findall("game")}
    counts["games_preserved"] = len(existing)
    for name in sorted(entries, key=str.casefold):
        values = entries[name]
        game = existing.get(name)
        if game is None:
            game = ET.SubElement(root, "game", name=name, index="", image="")
            for field in FIELDS:
                ET.SubElement(game, field).text = values.get(field, "Yes" if field == "enabled" else "")
            counts["games_added"] += 1
            continue
        for field, value in values.items():
            if not value:
                continue
            node = game.find(field)
            if node is None:
                ET.SubElement(game, field).text = value
                counts["fields_filled"] += 1
            elif not (node.text or "").strip():
                node.text = value
                counts["fields_filled"] += 1
            elif owned_fields and owned_fields(collection, name, field, (node.text or "").strip()) and (node.text or "").strip() != value:
                node.text = value
                counts["fields_updated"] += 1
    if not counts["games_added"] and not counts["fields_filled"] and not counts["fields_updated"]:
        return None, counts
    ET.indent(root, space="\t")
    return b'<?xml version="1.0"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n", counts


def plan(systems):
    mapping = collections_by_system()
    available = sorted(d.name for d in GAMELISTS.iterdir() if (d / "gamelist.xml").is_file())
    files, notes = [], []
    for system in available:
        if systems and system not in systems:
            continue
        targets = mapping.get(system, [])
        if not targets:
            notes.append({"system_id": system, "status": "sem_colecao_retrofe"})
            continue
        entries, skipped = esde_entries(system)
        for collection in targets:
            if "/" in collection or collection.startswith("."):
                notes.append({"system_id": system, "collection": collection, "status": "nome_invalido"})
                continue
            path = HYPERLIST / f"{collection}.xml"
            if path.is_symlink():
                notes.append({"system_id": system, "collection": collection, "status": "symlink_preservado"})
                continue
            before = path.read_bytes() if path.exists() else None
            try:
                after, counts = merge(path, collection, entries)
            except (ET.ParseError, ValueError) as exc:
                # A malformed third-party list is preserved byte-for-byte, never rewritten.
                notes.append({"system_id": system, "collection": collection, "status": "hyperlist_invalida_preservada", "error": str(exc)})
                continue
            row = {"system_id": system, "collection": collection, "esde_entries": len(entries), "skipped": skipped, **counts}
            if after is None:
                notes.append({**row, "status": "sem_mudanca"})
                continue
            files.append({**row, "path": path.relative_to(ROOT).as_posix(),
                          "before_sha256": sha256(before) if before is not None else None,
                          "before_gzip_base64": base64.b64encode(gzip.compress(before)).decode() if before is not None else None,
                          "after_sha256": sha256(after),
                          "after_gzip_base64": base64.b64encode(gzip.compress(after)).decode()})
    operation = "esde-metadata-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return {"operation_id": operation, "tool": "Tools/materializers/materialize-esde-metadata.py", "status": "planned",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": "Projeção dos metadados ES-DE para HyperList do RetroFE; preserva jogos e campos existentes.",
            "summary": {"files": len(files), "games_added": sum(f["games_added"] for f in files),
                        "fields_filled": sum(f["fields_filled"] for f in files)},
            "files": files, "notes": notes,
            "rollback": {"method": "Restaurar cada preimagem (ou remover arquivo criado para a quarentena reversível) somente se o hash atual for after_sha256.",
                         "permanent_deletion": False}}


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(temporary, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def current_sha(path):
    return sha256(path.read_bytes()) if path.exists() else None


def load(manifest):
    return json.loads(Path(manifest).read_text(encoding="utf-8"))


def save(manifest, data):
    write_atomic(Path(manifest), json.dumps(data, ensure_ascii=False, indent=2).encode())


def apply(manifest):
    data = load(manifest)
    if data["status"] != "planned":
        raise SystemExit(f"Manifesto em estado {data['status']}; nada aplicado")
    for item in data["files"]:
        if current_sha(ROOT / item["path"]) != item["before_sha256"]:
            raise SystemExit(f"{item['path']} mudou desde o plano; gere um plano novo")
    for item in data["files"]:
        write_atomic(ROOT / item["path"], gzip.decompress(base64.b64decode(item["after_gzip_base64"])))
    data.update(status="applied", applied_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    save(manifest, data)
    return verify(manifest)


def verify(manifest):
    data = load(manifest)
    bad = [i["path"] for i in data["files"] if current_sha(ROOT / i["path"]) != i["after_sha256"]]
    for item in data["files"]:
        if not bad and ET.parse(ROOT / item["path"]).getroot().tag != "menu":
            bad.append(item["path"])
    data["verification"] = {"status": "failed" if bad else "passed", "mismatched": bad,
                            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    save(manifest, data)
    return data["verification"]


def rollback(manifest):
    data = load(manifest)
    if data["status"] != "applied":
        raise SystemExit("Só operações aplicadas podem ser revertidas")
    for item in data["files"]:
        if current_sha(ROOT / item["path"]) != item["after_sha256"]:
            raise SystemExit(f"{item['path']} mudou após o apply; rollback recusado")
    quarantine = MANIFESTS / f"{data['operation_id']}-rollback-created"
    for item in data["files"]:
        target = ROOT / item["path"]
        if item["before_gzip_base64"] is None:
            quarantine.mkdir(parents=True, exist_ok=True)
            os.replace(target, quarantine / target.name)  # created file: reversible quarantine, never deleted
        else:
            write_atomic(target, gzip.decompress(base64.b64decode(item["before_gzip_base64"])))
    data.update(status="rolled_back", rolled_back_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    save(manifest, data)
    return {"status": "rolled_back", "files": len(data["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--system", action="append", help="system_id ES-DE (repetível; padrão: todos)")
    p.add_argument("--manifest", type=Path)
    for name in ("apply", "verify", "rollback"):
        sub.add_parser(name).add_argument("--manifest", type=Path, required=True)
    c = sub.add_parser("check", help="Somente leitura: mostra o que um plano faria")
    c.add_argument("--system", action="append")
    args = parser.parse_args()
    if args.command in ("plan", "check"):
        data = plan(set(args.system or []))
        if args.command == "check":
            print(json.dumps({"summary": data["summary"], "files": [{k: f[k] for k in ("collection", "games_added", "fields_filled")}
                                                                  for f in data["files"]]}, ensure_ascii=False, indent=2))
            return 0
        path = args.manifest or MANIFESTS / f"{data['operation_id']}.json"
        if path.exists():
            raise SystemExit(f"Manifesto já existe: {path}")
        save(path, data)
        print(json.dumps({"manifest": str(path.relative_to(ROOT)), **data["summary"]}, ensure_ascii=False))
        return 0
    result = {"apply": apply, "verify": verify, "rollback": rollback}[args.command](args.manifest)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
