#!/usr/bin/env python3
"""Game media and metadata tasks for RetroFE: from Options or from a terminal.

Commands:
  status            missing media per platform and RetroFE metadata coverage (read-only)
  metadata          convert ES-DE gamelists to HyperList: plan, apply and verify
                    through materialize-esde-metadata.py (manifest + rollback)
  skraper           open Skraper through its official managed session
                    (tools/roms/skraper/launch.sh -> romtool media session run)
  request skraper   queue Skraper for the RetroFE supervisor (used by Options)
  consume-pending   run a queued task; called by the supervisor after RetroFE closed

Skraper and RetroFE share the single media lock, so Skraper never runs while the
frontend session is open: Options queues it, RetroFE exits normally, the supervisor
releases the lock, runs Skraper and reopens RetroFE. Skraper is a GUI: the user picks
systems and starts the scrape there. Its gamelist export is enabled only inside the
managed session staging directory; the session merges that export into the canonical
ES-DE gamelist and publishes stories. `metadata` repeats that ingest on demand.
Coverage uses the exact-basename association of tools/roms/preview_games.py; no media
is matched by title.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[2]
PROJECT = ROOT.parents[1]
PENDING_REL = Path("UserData/retrofe/media-task-pending.json")
METADATA_TOOL = ROOT / "Tools/materializers/materialize-esde-metadata.py"
SKRAPER = PROJECT / "tools/roms/skraper/launch.sh"
# Media a carousel/preview uses; cover counts either canonical front-cover category.
KEY_MEDIA = {"capa": ("cover-front", "cover"), "screenshot": ("screenshot",), "vídeo": ("video",),
             "logo": ("clearlogo", "marquee"), "manual": ("manual",)}
TASKS = {"skraper"}

sys.path.insert(0, str(PROJECT / "tools/roms"))
import preview_games  # noqa: E402


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def hyperlist_names(collection):
    path = ROOT / "meta/hyperlist" / f"{collection}.xml"
    try:
        return {game.get("name") for game in ET.parse(path).getroot().iter("game")}
    except (OSError, ET.ParseError):
        return set()


def platform_status(system_id):
    listing = preview_games.list_games(PROJECT, system_id)
    games = listing["games"]
    missing = {label: 0 for label in KEY_MEDIA}
    for game in games:
        present = {m["type"] for m in game["media"] if m["files"]}
        for label, types in KEY_MEDIA.items():
            if not present.intersection(types):
                missing[label] += 1
    names = hyperlist_names(listing["collection"]) if listing["collection"] else set()
    gamelist = PROJECT / "frontend/ES-DE/UserData/gamelists" / system_id / "gamelist.xml"
    return {"system_id": system_id, "collection": listing["collection"], "games": len(games), "missing": missing,
            "metadata": sum(1 for g in games if g["title"] in names), "esde_gamelist": gamelist.is_file()}


def status(systems=None):
    rows = []
    for folder in sorted((PROJECT / "roms").iterdir()):
        if systems and folder.name not in systems:
            continue
        try:
            row = platform_status(folder.name)
        except ValueError:
            continue
        if row["games"]:
            rows.append(row)
    total = {"games": sum(r["games"] for r in rows), "metadata": sum(r["metadata"] for r in rows),
             "missing": {label: sum(r["missing"][label] for r in rows) for label in KEY_MEDIA}}
    return {"platforms": rows, "total": total}


def summary_line(data):
    t = data["total"]
    missing = ", ".join(f"{count} sem {label}" for label, count in t["missing"].items() if label in ("capa", "vídeo", "screenshot"))
    return f"{t['games']} jogos: {missing}; metadados em {t['metadata']}"


def run_metadata(systems):
    import metadata_ingest
    api = {"PROJECT_ROOT": PROJECT, "OPS_ROOT": PROJECT / "outputs/roms-consolidacao-2026-10-24/operations",
           "operation_dir": lambda operation: PROJECT / "outputs/roms-consolidacao-2026-10-24/operations" / operation}
    result = metadata_ingest.ingest_project(api, systems=systems or None, client="retrofe-metadata")
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "ingestão de metadados falhou")
    if not result.get("gamelists_changed") and not result.get("stories_written") and not result.get("hyperlists"):
        return "Metadados já atualizados; nada a converter"
    return (f"{result['stories_written']} histórias e {result['hyperlists']} listas projetadas; "
            f"vale na próxima abertura (manifesto {result.get('manifest_relative') or 'sem alteração de bytes'})")


def run_skraper(extra=()):
    if not SKRAPER.is_file():
        raise RuntimeError(f"Launcher oficial do Skraper ausente: {SKRAPER}")
    return subprocess.run(["bash", str(SKRAPER), *extra], cwd=SKRAPER.parent).returncode


def request(task):
    if task not in TASKS:
        raise ValueError(f"Tarefa desconhecida: {task}")
    path = ROOT / PENDING_REL
    if path.exists():
        raise RuntimeError("Já existe uma tarefa de mídia aguardando o fechamento do RetroFE")
    write_json(path, {"schema_version": 1, "task": task, "requested_at_utc": utc_now()})
    return "Skraper agendado: o RetroFE fecha, o Skraper abre e o RetroFE volta ao terminar"


def consume_pending():
    path = ROOT / PENDING_REL
    if path.is_file():
        pending = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()  # consumed once; a failure is reported, never retried automatically
        if pending.get("task") != "skraper":
            raise RuntimeError(f"Tarefa pendente desconhecida: {pending.get('task')}")
        return run_skraper()
    import metadata_ingest
    metadata_ingest.consume_pending({
        "PROJECT_ROOT": PROJECT,
        "OPS_ROOT": PROJECT / "outputs/roms-consolidacao-2026-10-24/operations",
        "operation_dir": lambda operation: PROJECT / "outputs/roms-consolidacao-2026-10-24/operations" / operation,
    })
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status")
    s.add_argument("--system", action="append")
    s.add_argument("--json", action="store_true")
    s.add_argument("--summary", action="store_true", help="Uma linha (usada por Options)")
    m = sub.add_parser("metadata")
    m.add_argument("--system", action="append", default=[])
    sub.add_parser("skraper").add_argument("skraper_args", nargs=argparse.REMAINDER)
    sub.add_parser("request").add_argument("task", choices=sorted(TASKS))
    sub.add_parser("consume-pending")
    args = parser.parse_args()
    try:
        if args.command == "status":
            data = status(set(args.system or []))
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            elif args.summary:
                print(summary_line(data))
            else:
                print(f"{'plataforma':<16}{'jogos':>6}{'metadados':>10}  " + "  ".join(f"sem {k}" for k in KEY_MEDIA) + "  ES-DE")
                for r in data["platforms"]:
                    print(f"{r['system_id']:<16}{r['games']:>6}{r['metadata']:>10}  "
                          + "  ".join(f"{r['missing'][k]:>{len(k) + 4}}" for k in KEY_MEDIA) + ("  sim" if r["esde_gamelist"] else "  não"))
                print(summary_line(data))
            return 0
        if args.command == "metadata":
            print(run_metadata(args.system))
            return 0
        if args.command == "skraper":
            return run_skraper(args.skraper_args)
        if args.command == "request":
            print(request(args.task))
            return 0
        result = consume_pending()
        print("Nenhuma tarefa pendente" if result is None else f"Skraper encerrado (código {result})")
        return 0 if result in (None, 0) else result
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
