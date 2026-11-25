#!/usr/bin/env python3
"""Open ROMs from the desktop with the emulator the library already configures.

The single sources are the platform catalog (`frontend/RetroFE/Config/platform-catalog.json`)
and the host profile (`frontend/RetroFE/Config/profiles/host-linux.json`). This module adds
no catalog: it projects them into the freedesktop/KDE association files of this user and
opens a file through the same launcher and controller session used by RetroFE and ES-DE.

- `plan` lists every ROM extension, its MIME type and the action (default, own type or only
  "Abrir com"); nothing is written.
- `apply` writes `emuatlas-rom.desktop`, a MIME package for ROM extensions that have no type,
  and the defaults in `~/.config/mimeapps.list`. The first previous default of each type is kept
  in `$XDG_STATE_HOME/emuatlas/rom-associations.json` for `remove`.
- `open FILE` resolves the platform (library folder first, then the extension's native platform,
  otherwise asks) and runs `host-emulator-launcher.py`, which starts the emulator inside the
  controller session (`steam-input.py session-run --system-id <id>`) and restores the Desktop.
- `status` compares the current defaults with the plan; `remove` restores the previous ones.

Generic containers (zip, 7z, iso, cue...) never become defaults: Ark and the user's choices stay,
and EmuAtlas appears in "Abrir com". Inside `roms/<id>/` the folder decides the platform.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG = Path("frontend/RetroFE/Config/platform-catalog.json")
# Native raw extensions per platform, curated with the lossless compression profiles.
NATIVE = Path("tools/roms/config/compression-profiles.json")
PROFILE = Path("frontend/RetroFE/Config/profiles/host-linux.json")
LAUNCHER = Path("frontend/RetroFE/Tools/launchers/emulators/host-emulator-launcher.py")
ICON = Path("frontend/RetroFE/RetroFE.png")
DESKTOP_ID = "emuatlas-rom.desktop"
MIME_PACKAGE = "emuatlas-roms.xml"
MARKER = "X-EmuAtlas-Managed=rom-associations"
OWN_TYPE = "application/x-emuatlas-rom-{}"
# Types that also hold non-game files: EmuAtlas is offered in "Abrir com", never the default.
GENERIC_TYPES = {
    "application/zip", "application/x-zip-compressed", "application/x-7z-compressed",
    "application/vnd.rar", "application/x-rar", "application/x-rar-compressed",
    "application/x-tar", "application/gzip", "application/x-xz", "application/zstd",
    "application/vnd.efi.iso", "application/x-cd-image", "application/x-iso9660-image",
    "application/vnd.efi.img", "application/x-raw-disk-image", "application/x-cue",
    "application/octet-stream", "application/x-zerosize", "application/json", "application/xml",
    "application/pdf", "application/x-executable", "application/x-sharedlib",
    "application/x-msdownload", "application/vnd.microsoft.portable-executable",
    "application/x-shellscript", "application/x-mpegurl", "audio/x-mpegurl", "audio/mpegurl",
}
GENERIC_PREFIXES = ("text/", "image/", "audio/", "video/", "inode/", "font/")
# Never claim these extensions with an own type: they are common outside ROM sets.
NEVER_OWN = {
    "bin", "img", "iso", "cue", "m3u", "dat", "txt", "xml", "json", "cfg", "ini", "sav", "srm",
    "state", "exe", "dll", "so", "elf", "sh", "bat", "cmd", "lst", "log", "pdf", "png", "jpg",
    "jpeg", "gif", "mp3", "mp4", "ogg", "wav", "html", "md", "zip", "7z", "rar", "gz", "xz",
    "zst", "tar", "ccd", "sub", "mds", "mdf", "toc", "nrg", "apk", "deb", "rpm", "sys", "rom",
}
NO_TYPE = {"application/octet-stream", "application/x-zerosize", ""}
# Only types that name a game format become defaults. Anything else with a known type (.desktop,
# certificates, archives, playlists, fonts, scientific .hdf, macOS .dmg...) stays in "Abrir com".
ROM_TYPE = re.compile(
    r"-rom(-smd)?$|\.rom$|^application/x-(gameboy|gba|nintendo-3ds|ctr-|gamecube|wii|mame-chd|compressed-iso|"
    r"fds-disk|gd-rom|discjuggler|commodore-|spectrum-|amiga-disk|atari-|neo-geo|virtual-boy|wonderswan|"
    r"sg1000|pc-engine|genesis|nes-|n64-|sms-|gamegear)")
NEVER_OWN |= {"app", "sfo", "sfb", "self", "prx", "exefs", "ps3dir", "dump", "dup", "axf"}
# Containers and disc images that hold games: EmuAtlas appears in "Abrir com" (never default).
# Other known types (.desktop, certificates, scripts, fonts...) are left alone entirely.
OPEN_WITH = {"7z", "zip", "iso", "cue", "bin", "img", "m3u", "ccd", "mds", "mdf", "nrg", "rom", "md", "wad"}
OPEN_WITH_REFUSED = {"application/x-executable", "application/x-sharedlib", "application/x-zerosize",
                     "application/octet-stream"}


# --------------------------------------------------------------------------- catalog view

def rom_systems(root: Path = PROJECT_ROOT) -> dict[str, dict]:
    """Platforms with a configured emulator: id -> collection, display name, extensions."""
    catalog = {s["id"]: s for s in json.loads((root / CATALOG).read_text(encoding="utf-8"))["systems"]}
    profile = json.loads((root / PROFILE).read_text(encoding="utf-8"))
    systems = {}
    for collection, entry in profile.get("collections", {}).items():
        sid = str(entry.get("rom_subdir", "")).strip()
        if not sid or entry.get("adapter") in (None, "unavailable"):
            continue
        definition = catalog.get(sid, {})
        extensions = {x.strip().lower().lstrip(".") for x in str(entry.get("extensions", "")).split(",")}
        extensions |= {str(x).strip().lower().lstrip(".") for x in definition.get("extensions", [])}
        extensions.discard("")
        engine = entry.get("core") or entry.get("app_id") or entry.get("launcher") or entry.get("command") or ""
        systems[sid] = {"collection": collection, "extensions": sorted(extensions),
                        "display_name": definition.get("display_name") or collection,
                        "adapter": entry.get("adapter"), "engine": f"{entry.get('adapter')}:{engine}"}
    return systems


def native_extensions(root: Path = PROJECT_ROOT) -> dict[str, set[str]]:
    """Extension -> platforms whose raw dump uses it (compression-profiles source_extensions_by_system)."""
    try:
        profiles = json.loads((root / NATIVE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    result: dict[str, set[str]] = {}

    def walk(value):
        if isinstance(value, dict):
            mapping = value.get("source_extensions_by_system")
            if isinstance(mapping, dict):
                for sid, extensions in mapping.items():
                    for ext in extensions if isinstance(extensions, list) else []:
                        result.setdefault(str(ext).lower().lstrip("."), set()).add(sid)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(profiles)
    return result


def extension_map(systems: dict[str, dict]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for sid, info in systems.items():
        for ext in info["extensions"]:
            result.setdefault(ext, []).append(sid)
    return {ext: sorted(sids) for ext, sids in sorted(result.items())}


def is_generic(mime: str) -> bool:
    return mime in GENERIC_TYPES or mime.startswith(GENERIC_PREFIXES)


def classify(ext: str, mime: str) -> tuple[str, str]:
    """(action, type): default = a game type the system knows; own = EmuAtlas registers a type for
    an extension without one; added = game container, only "Abrir com"; skip = not a game type."""
    if mime in NO_TYPE:
        return ("skip", "") if ext in NEVER_OWN else ("own", OWN_TYPE.format(ext))
    if not is_generic(mime) and ROM_TYPE.search(mime):
        return "default", mime
    # A shared type reaches every file of that kind: executables, empty files and text never.
    if ext in OPEN_WITH and mime not in OPEN_WITH_REFUSED and not mime.startswith("text/"):
        return "added", mime
    return "skip", mime


def system_mime(ext: str, *, own: set[str] | None = None) -> str:
    """Type the shared-mime-info database gives to `x.<ext>`, ignoring our own package."""
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "emuatlas"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=cache, prefix="mime-") as work:
        sample = Path(work) / f"amostra.{ext}"
        sample.write_bytes(b"")
        result = subprocess.run(["xdg-mime", "query", "filetype", str(sample)],
                                capture_output=True, text=True, timeout=30)
    mime = result.stdout.strip()
    return "" if own and mime in own else mime


def build_plan(root: Path = PROJECT_ROOT, mime_of=None) -> list[dict]:
    systems = rom_systems(root)
    owned = {OWN_TYPE.format(ext) for ext in extension_map(systems)}
    mime_of = mime_of or (lambda ext: system_mime(ext, own=owned))
    rows = []
    for ext, sids in extension_map(systems).items():
        action, mime = classify(ext, mime_of(ext))
        rows.append({"extension": ext, "mime": mime, "action": action, "systems": sids})
    return rows


# --------------------------------------------------------------------------- files we own

def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def state_file() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "emuatlas/rom-associations.json"


def exec_quote(value: str) -> str:
    if value and all(c.isalnum() or c in "/._-+" for c in value):
        return value
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in value) + '"'


def desktop_entry(root: Path, python: str, mimes: list[str]) -> str:
    command = " ".join(exec_quote(x) for x in (python, str(root / "tools/roms/rom_associations.py"), "open"))
    return ("[Desktop Entry]\n" + MARKER + "\nType=Application\nVersion=1.0\nName=EmuAtlas\n"
            "GenericName=Jogar no EmuAtlas\nComment=Abre a ROM no emulador configurado pelo EmuAtlas, com o controle\n"
            f"Exec={command} %f\nIcon={root / ICON}\nTerminal=false\nNoDisplay=true\n"
            "Categories=Game;Emulator;\nMimeType=" + "".join(m + ";" for m in mimes) + "\n")


def mime_package(rows: list[dict]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<!-- " + MARKER + " -->",
             '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">']
    for row in rows:
        if row["action"] != "own":
            continue
        lines += [f'  <mime-type type="{escape(row["mime"])}">',
                  f'    <comment>ROM .{escape(row["extension"])} (EmuAtlas)</comment>',
                  f'    <comment xml:lang="pt_BR">ROM .{escape(row["extension"])} (EmuAtlas)</comment>',
                  f'    <glob pattern="*.{escape(row["extension"])}"/>', "  </mime-type>"]
    return "\n".join(lines + ["</mime-info>", ""])


def read_sections(path: Path) -> list[list]:
    """mimeapps.list as [[section, [lines]]], keeping comments and unknown lines in place."""
    sections: list[list] = [[None, []]]
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("[") and line.rstrip().endswith("]"):
                sections.append([line.strip()[1:-1], []])
            else:
                sections[-1][1].append(line)
    return sections


def write_sections(path: Path, sections: list[list]) -> None:
    out = []
    for name, lines in sections:
        if name is not None:
            out.append(f"[{name}]")
        out.extend(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".emuatlas-tmp")
    temporary.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    temporary.replace(path)


def edit_entry(sections, section: str, key: str, update) -> None:
    """Apply update(current_list) -> new_list to `key` of `section`; empty removes the line."""
    found = next((s for s in sections if s[0] == section), None)
    if found is None:
        found = [section, []]
        sections.append(found)
    lines = found[1]
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key and "=" in line:
            values = [v for v in line.split("=", 1)[1].split(";") if v]
            new = update(values)
            if new:
                lines[index] = key + "=" + "".join(v + ";" for v in new)
            else:
                del lines[index]
            return
    new = update([])
    if new:
        position = len(lines)
        while position and not lines[position - 1].strip():
            position -= 1
        lines.insert(position, key + "=" + "".join(v + ";" for v in new))


def refresh_caches() -> None:
    for command in (["update-mime-database", str(data_home() / "mime")],
                    ["update-desktop-database", "-q", str(data_home() / "applications")],
                    ["kbuildsycoca6"]):
        if shutil.which(command[0]):
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)


def current_default(mime: str) -> str:
    result = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def apply(root: Path = PROJECT_ROOT, rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else build_plan(root)
    python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else shutil.which("python3") or sys.executable
    applications = data_home() / "applications"
    desktop = applications / DESKTOP_ID
    if desktop.is_symlink() or (desktop.exists() and MARKER not in desktop.read_text(encoding="utf-8")):
        raise ValueError(f"{desktop} não foi criado pelo EmuAtlas; nada foi alterado.")
    package = data_home() / "mime/packages" / MIME_PACKAGE
    if package.is_symlink() or (package.exists() and MARKER not in package.read_text(encoding="utf-8")):
        raise ValueError(f"{package} não foi criado pelo EmuAtlas; nada foi alterado.")
    mimes = sorted({r["mime"] for r in rows if r["mime"] and r["action"] != "skip"})
    applications.mkdir(parents=True, exist_ok=True)
    desktop.write_text(desktop_entry(root, python, mimes), encoding="utf-8")
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text(mime_package(rows), encoding="utf-8")
    refresh_caches()
    record_path = state_file()
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {"schema": 1, "previous_defaults": {}, "added": []}
    defaults = sorted({r["mime"] for r in rows if r["action"] in ("default", "own")})
    for mime in defaults:
        previous = current_default(mime)
        if previous != DESKTOP_ID:
            record["previous_defaults"].setdefault(mime, previous)
    mimeapps = config_home() / "mimeapps.list"
    sections = read_sections(mimeapps)
    for mime in defaults:
        edit_entry(sections, "Default Applications", mime, lambda values: [DESKTOP_ID])
    added = sorted({r["mime"] for r in rows if r["action"] == "added" and r["mime"]})
    for mime in added:
        edit_entry(sections, "Added Associations", mime,
                   lambda values: values + [DESKTOP_ID] if DESKTOP_ID not in values else values)
    write_sections(mimeapps, sections)
    record["added"] = sorted(set(record.get("added", [])) | set(added))
    record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    refresh_caches()
    return {"status": "configured", "defaults": len(defaults), "own_types": sum(r["action"] == "own" for r in rows),
            "open_with_only": len(added), "extensions": len(rows), "desktop": str(desktop)}


def remove() -> dict:
    record_path = state_file()
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {"previous_defaults": {}, "added": []}
    mimeapps = config_home() / "mimeapps.list"
    sections = read_sections(mimeapps)
    restored = 0
    for mime, previous in record.get("previous_defaults", {}).items():
        def back(values, previous=previous):
            if values and values[0] != DESKTOP_ID:
                return values  # The user chose something else after us: keep it.
            return [previous] if previous else []
        edit_entry(sections, "Default Applications", mime, back)
        restored += 1
    for mime in record.get("added", []):
        edit_entry(sections, "Added Associations", mime, lambda values: [v for v in values if v != DESKTOP_ID])
    write_sections(mimeapps, sections)
    for path in (data_home() / "applications" / DESKTOP_ID, data_home() / "mime/packages" / MIME_PACKAGE):
        if path.is_file() and not path.is_symlink() and MARKER in path.read_text(encoding="utf-8"):
            path.unlink()
    refresh_caches()
    record_path.unlink(missing_ok=True)
    return {"status": "removed", "restored_defaults": restored}


def status(root: Path = PROJECT_ROOT) -> dict:
    rows = build_plan(root)
    wrong = [r for r in rows if r["action"] in ("default", "own") and current_default(r["mime"]) != DESKTOP_ID]
    return {"desktop": (data_home() / "applications" / DESKTOP_ID).is_file(), "extensions": len(rows),
            "defaults_missing": [f".{r['extension']} ({r['mime']})" for r in wrong]}


# --------------------------------------------------------------------------- open a ROM

def library_count(root: Path, sid: str, ext: str) -> int:
    folder = root / "roms" / sid
    try:
        return sum(1 for path in folder.rglob("*." + ext) if path.is_file())
    except OSError:
        return 0


def resolve(root: Path, path: Path, systems: dict[str, dict], native: dict[str, set[str]] | None = None) -> dict:
    """Platform for a file: library folder; else the extension's native platform; regional variants
    that run on the same emulator pick the one with most games; only different emulators ask."""
    rom_root = Path(os.path.abspath(root / "roms"))
    absolute = Path(os.path.abspath(path))
    ext = absolute.suffix.lower().lstrip(".")
    if absolute.is_relative_to(rom_root):
        parts = absolute.relative_to(rom_root).parts
        if len(parts) > 1 and parts[0] in systems:
            return {"system": parts[0], "external": False, "candidates": [parts[0]]}
    native = native_extensions(root) if native is None else native
    candidates = sorted(sid for sid in native.get(ext, ()) if sid in systems)
    if not candidates:
        candidates = sorted(sid for sid, info in systems.items() if ext in info["extensions"])
    if len({systems[sid]["engine"] for sid in candidates}) == 1:
        best = max(candidates, key=lambda sid: (library_count(root, sid, ext), -candidates.index(sid)))
        return {"system": best, "external": True, "candidates": candidates}
    return {"system": None, "external": True, "candidates": candidates}


def ask_platform(path: Path, candidates: list[str], systems: dict[str, dict]) -> str | None:
    options = sorted(candidates, key=lambda sid: systems[sid]["display_name"].casefold())
    title = "EmuAtlas"
    text = f"Em qual plataforma abrir {path.name}?"
    if shutil.which("kdialog"):
        args = ["kdialog", "--title", title, "--menu", text]
        for sid in options:
            args += [sid, systems[sid]["display_name"]]
        result = subprocess.run(args, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() in systems else None
    if shutil.which("zenity"):
        args = ["zenity", "--list", "--title", title, "--text", text, "--column", "id", "--column", "Plataforma",
                "--hide-column=1", "--print-column=1"]
        for sid in options:
            args += [sid, systems[sid]["display_name"]]
        result = subprocess.run(args, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() in systems else None
    return None


def notify(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    if shutil.which("kdialog"):
        subprocess.run(["kdialog", "--title", "EmuAtlas", "--error", message], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "EmuAtlas", message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch_command(root: Path, path: Path, sid: str, systems: dict[str, dict], external: bool) -> list[str]:
    # The launcher itself runs the emulator inside `steam-input.py session-run --system-id` (the same
    # controller session as ES-DE: Gamepad during the game, Desktop after). Wrapping it again would
    # nest two sessions and the inner one cannot isolate the SDL ports.
    return [sys.executable, str(root / LAUNCHER), "--collection", systems[sid]["collection"],
            "--item", str(Path(os.path.abspath(path)))] + (["--external"] if external else [])


def open_rom(root: Path, raw: str) -> int:
    path = Path(raw[7:] if raw.startswith("file://") else raw).expanduser()
    if not path.is_file():
        notify(f"Arquivo não encontrado: {path}")
        return 2
    systems = rom_systems(root)
    found = resolve(root, path, systems)
    sid = found["system"]
    if sid is None and found["candidates"]:
        sid = ask_platform(path, found["candidates"], systems)
        if sid is None:
            return 1
    if sid is None:
        notify(f"O EmuAtlas não tem emulador configurado para arquivos .{path.suffix.lower().lstrip('.') or '?'}.")
        return 2
    command = launch_command(root, path, sid, systems, found["external"])
    print(f"EmuAtlas: {path.name} em {systems[sid]['display_name']}.", flush=True)
    os.chdir(root)
    os.execv(command[0], command)
    return 0  # not reached


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="lista extensões, tipos e ações sem gravar")
    commands.add_parser("apply", help="associa as extensões de ROM ao EmuAtlas neste usuário")
    commands.add_parser("status", help="confere se as associações padrão estão ativas")
    commands.add_parser("remove", help="restaura os padrões anteriores e retira os arquivos do EmuAtlas")
    opener = commands.add_parser("open", help="abre uma ROM no emulador da plataforma, com o controle")
    opener.add_argument("file")
    args = parser.parse_args(argv)
    if args.command == "open":
        return open_rom(PROJECT_ROOT, args.file)
    result = {"plan": lambda: build_plan(PROJECT_ROOT), "apply": lambda: apply(PROJECT_ROOT),
              "status": lambda: status(PROJECT_ROOT), "remove": remove}[args.command]()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
