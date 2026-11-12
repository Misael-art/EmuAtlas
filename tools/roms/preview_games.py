"""Per-platform game listing and supervised launch for the Preview bridge.

Media is associated only by the exact basename of the ROM (rule 3 of AGENTS.md);
launching delegates to the canonical RetroFE host launcher, never to another emulator path.
"""
from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid

PROFILE = Path("frontend/RetroFE/Config/profiles/host-linux.json")
LAUNCHER = Path("frontend/RetroFE/Tools/launchers/emulators/host-emulator-launcher.py")
LAUNCH_LOGS = Path("outputs/preview/launches")
MEDIA_WRITE_LOCK = Path("outputs/roms-consolidacao-2026-10-24/operations/media-write.lock")
SYSTEM_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# Front cover first, then the remaining canonical categories in a stable reading order.
MEDIA_ORDER = ["cover-front", "cover", "box", "cover-back", "cover-spine", "cover-full", "cart-front", "cart-back",
               "physicalmedia", "screentitle", "screenshot", "video", "miximage", "clearlogo", "marquee",
               "background", "manual", "booklet", "map"]
PREVIEWABLE = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".mp4", ".webm", ".ogg"}
VIDEO = {".mp4", ".webm", ".ogg"}
IGNORED_SUFFIXES = {".txt", ".pdf", ".nfo", ".xml", ".json", ".md", ".jpg", ".png", ".db", ".ini", ".cfg"}


def load_profile(root):
    try:
        return json.loads((root / PROFILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def platform_profile(profile, system_id):
    """Collections whose ROM folder is exactly roms/<system_id>, with their declared extensions."""
    collections = [(name, value) for name, value in profile.get("collections", {}).items()
                   if str(value.get("rom_subdir", "")).strip("/") == system_id]
    extensions = set()
    for _name, value in collections:
        extensions |= {e.strip().lower().lstrip(".") for e in str(value.get("extensions", "")).split(",") if e.strip()}
    for value in profile.get("subcollection_overrides", {}).values():
        if str(value.get("rom_subdir", "")).strip("/") == system_id:
            extensions |= {e.strip().lower().lstrip(".") for e in str(value.get("extensions", "")).split(",") if e.strip()}
    return collections, extensions


def validate_system(root, system_id):
    if not isinstance(system_id, str) or not SYSTEM_ID.fullmatch(system_id):
        raise ValueError("Plataforma inválida")
    folder = root / "roms" / system_id
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("Plataforma sem pasta canônica em roms/")
    return folder


def media_index(root, system_id):
    """{media_type: {basename: [relative paths]}} for real category folders (ES-DE aliases are skipped)."""
    base = root / "media" / system_id
    index = {}
    if base.is_symlink() or not base.is_dir():
        return index
    for folder in sorted(base.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or folder.name.startswith("."):
            continue
        entries = {}
        for path in folder.iterdir():
            if path.name.startswith(".") or not (path.is_file() or path.is_symlink()):
                continue
            try:
                target = path.resolve(strict=True)
            except OSError:
                continue
            if not target.is_relative_to(root / "media") or not target.is_file():
                continue
            entries.setdefault(alt_base(path.stem), []).append(path.relative_to(root).as_posix())
        index[folder.name] = entries
    return index


def alt_base(stem):
    """`Game (Alt 2)` is a documented alternate of `Game` for the same type."""
    return re.sub(r" \(Alt \d+\)$", "", stem)


def media_types(index):
    present = [t for t in MEDIA_ORDER if t in index]
    return present + sorted(t for t in index if t not in MEDIA_ORDER)


def list_games(root, system_id):
    folder = validate_system(root, system_id)
    profile = load_profile(root)
    collections, extensions = platform_profile(profile, system_id)
    accepts_dirs = any(value.get("accepts_directories") for _name, value in collections)
    if collections and all(value.get("adapter") == "unavailable" for _name, value in collections):
        # Without a launchable profile the declared extensions say nothing; show the packages with Play blocked.
        extensions = set()
    games = []
    for current, dirs, files in os.walk(folder):
        current_path = Path(current)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (current_path / d).is_symlink())
        if accepts_dirs and current_path == folder:
            for name in dirs:
                games.append((current_path / name, True))
            dirs[:] = []
        for name in sorted(files):
            path = current_path / name
            suffix = path.suffix.lower().lstrip(".")
            if name.startswith(".") or path.is_symlink():
                continue
            if extensions and suffix not in extensions:
                continue
            if not extensions and "." + suffix in IGNORED_SUFFIXES:
                continue
            games.append((path, False))
    index = media_index(root, system_id)
    types = media_types(index)
    stems = {}
    for path, is_dir in games:
        stems.setdefault(path.name if is_dir else path.stem, []).append(path)
    rows = []
    for path, is_dir in sorted(games, key=lambda g: str(g[0]).casefold()):
        stem = path.name if is_dir else path.stem
        ambiguous = len(stems[stem]) > 1
        media = []
        for media_type in types:
            # Same exact basename as the frontends use; repeated names are flagged, never re-matched.
            files = sorted(index[media_type].get(stem, []))
            media.append(dict(type=media_type, files=[dict(path=f, kind=kind(f)) for f in files]))
        rel = path.relative_to(root).as_posix()
        rows.append(dict(path=rel, title=stem, folder=path.parent.relative_to(folder).as_posix(),
                         directory=is_dir, size=None if is_dir else path.stat().st_size,
                         ambiguous_basename=ambiguous, same_basename=len(stems[stem]), media=media))
    blocked = None
    if not collections:
        blocked = "Sem perfil de emulador para esta plataforma em host-linux.json"
    elif len(collections) > 1:
        blocked = "Mais de uma coleção usa esta pasta: " + ", ".join(sorted(n for n, _v in collections))
    elif collections[0][1].get("adapter") == "unavailable":
        blocked = collections[0][1].get("reason") or "Perfil marcado como emulador indisponível"
    return dict(system_id=system_id, collection=collections[0][0] if len(collections) == 1 else None,
                launch_blocked=blocked, media_types=types, extensions=sorted(extensions), games=rows)


def kind(relative_path):
    suffix = Path(relative_path).suffix.lower()
    if suffix in VIDEO:
        return "video"
    return "image" if suffix in PREVIEWABLE else "file"


def acquire_game_reservation(root):
    path = root / MEDIA_WRITE_LOCK
    if path.is_symlink():
        raise ValueError("Lock de mídia por symlink recusado")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise ValueError("Uma manutenção ou outra sessão está usando a biblioteca; aguarde seu encerramento antes de jogar")
    return fd


class Launcher:
    """One supervised game at a time; the Preview refuses maintenance jobs while it runs."""

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.current = None

    def running(self):
        with self.lock:
            return bool(self.current and self.current["process"].poll() is None)

    def status(self):
        with self.lock:
            if not self.current:
                return dict(status="idle")
            process = self.current["process"]
            code = process.poll()
            log = self.root / self.current["log"]
            tail = ""
            if log.is_file():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 8000))
                    tail = stream.read().decode(errors="replace")
            return dict(status="running" if code is None else "exited", exit_code=code, log=tail,
                        **{k: self.current[k] for k in ("launch_id", "game", "system_id", "display", "started_at")})

    def launch(self, system_id, rel_path, fullscreen, busy_reason=None):
        if busy_reason:
            raise ValueError(busy_reason)
        if not isinstance(rel_path, str) or "\x00" in rel_path or not isinstance(fullscreen, bool):
            raise ValueError("Requisição de jogo inválida")
        listing = list_games(self.root, system_id)
        if listing["launch_blocked"]:
            raise ValueError(listing["launch_blocked"])
        if rel_path not in {g["path"] for g in listing["games"]}:
            raise ValueError("O jogo não está na listagem atual da plataforma; atualize a página")
        item = self.root / rel_path
        part = item
        while part != self.root:
            if part.is_symlink():
                raise ValueError("Caminho do jogo contém symlink")
            part = part.parent
        with self.lock:
            if self.current and self.current["process"].poll() is None:
                raise ValueError("Já existe um jogo em execução pelo Preview; feche-o primeiro")
            launch_id = "launch-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
            log_rel = LAUNCH_LOGS / (launch_id + ".log")
            log_path = self.root / log_rel
            log_path.parent.mkdir(parents=True, exist_ok=True)
            display = "fullscreen" if fullscreen else "windowed"
            argv = [sys.executable, str(self.root / LAUNCHER), "--collection", listing["collection"],
                    "--item", str(item), "--display", display]
            # F07: the game tree holds the same reservation as maintenance writers. The lock is
            # acquired atomically (no check-then-start race) and inherited by the launcher; the
            # Preview closes its copy, so the reservation ends exactly when the game tree exits.
            lock_fd = acquire_game_reservation(self.root)
            try:
                with open(log_path, "xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write((json.dumps(dict(argv=argv), ensure_ascii=False) + "\n").encode())
                    stream.flush()
                    process = subprocess.Popen(argv, cwd=self.root / "frontend/RetroFE", stdin=subprocess.DEVNULL,
                                               stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
                                               pass_fds=(lock_fd,), env=dict(os.environ, EMULATION_MEDIA_LOCK_FD=str(lock_fd)))
            finally:
                os.close(lock_fd)
            self.current = dict(process=process, launch_id=launch_id, log=log_rel.as_posix(), game=rel_path,
                                system_id=system_id, display=display,
                                started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            return launch_id
