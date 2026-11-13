#!/usr/bin/env python3
"""Auditable ROM-library inventory, validation, planning, and reversible changes."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import concurrent.futures
import contextlib
from functools import lru_cache
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
import zlib
from collections import defaultdict
from pathlib import Path, PurePosixPath


def _unify_module_names(module_name: str) -> None:
    """Register one module object under its script name and package name.

    The tools run as scripts (``import romtool``) and as the ``tools.roms``
    namespace package (tests).  Without this, mixing both import styles in one
    process creates two independent ``romtool`` instances: patching
    ``tools.roms.romtool.OPS_ROOT`` would not affect the copy used by the
    other importer, which then writes into the real project history (F01).
    """
    if module_name == "__main__":
        return
    short = module_name.rsplit(".", 1)[-1]
    module = sys.modules.get(module_name)
    if module is None:
        return
    for alias in (short, "tools.roms." + short):
        sys.modules.setdefault(alias, module)
    package = sys.modules.get("tools.roms")
    if package is not None and sys.modules.get("tools.roms." + short) is module:
        setattr(package, short, module)


_unify_module_names(__name__)
if __name__ == "__main__":
    for _alias in ("romtool", "tools.roms.romtool"):
        sys.modules.setdefault(_alias, sys.modules[__name__])

try:
    if os.name == 'nt':
        raise ModuleNotFoundError('Feature modules are Linux-only; operation recording is portable')
    from . import steam_centralizer
    from . import media_optimizer
    from . import media_session
    from . import esde_media
    from . import media_covers
    from . import save_capture
    from . import variant_metadata
    from . import variant_organization
    from . import collection_hierarchy
    from . import media_stray
    from . import catalog_projection
    from . import storage_dedupe
    from . import steam_shortcuts
except ImportError:
    if os.name == 'nt':
        # Native Windows launchers reuse the official journal/record functions.
        # Organization/conversion features retain their Linux runtime gates.
        steam_centralizer = media_optimizer = media_session = esde_media = None
        media_covers = save_capture = variant_metadata = variant_organization = None
        collection_hierarchy = media_stray = catalog_projection = storage_dedupe = steam_shortcuts = None
    else:
        import steam_centralizer
        import media_optimizer
        import media_session
        import esde_media
        import media_covers
        import save_capture
        import variant_metadata
        import variant_organization
        import collection_hierarchy
        import media_stray
        import catalog_projection
        import storage_dedupe
        import steam_shortcuts

for _submodule in (steam_centralizer, media_optimizer, media_session, esde_media, media_covers,
                   save_capture, variant_metadata, variant_organization, collection_hierarchy,
                   media_stray, catalog_projection, storage_dedupe, steam_shortcuts):
    if _submodule is not None:
        _unify_module_names(_submodule.__name__)

TOOL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TOOL_ROOT.parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "roms-consolidacao-2026-10-24"
OPS_ROOT = OUTPUT_ROOT / "operations"
POLICY_PATH = TOOL_ROOT / "config" / "policy.json"
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".lha"}
VERIFY_EXTS = ARCHIVE_EXTS | {".chd", ".exfat", ".nsp", ".cue", ".m3u", ".gdi", ".sfo", ".adf", ".hdf"}
MEDIA_ACTIONS = {"media_move_link", "media_alias_link", "media_repair_link", "media_rekey_link"}
MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".jfif",
    ".mp4", ".mkv", ".avi", ".mov", ".mp3", ".ogg", ".flac", ".mo3", ".pdf", ".txt",
}
MEDIA_TYPES = {
    "background", "box", "clearlogo", "cover", "cover-front", "cover-back",
    "cover-spine", "cart-front", "screentitle", "screenshot", "video", "music",
    "manual", "booklet", "story", "map", "banner", "marquee", "loadingscreen", "numberplayers", "color", "patch",
    "physicalmedia", "miximage", "cover-full",
}
ESDE_MEDIA_TYPE_MAP = esde_media.MEDIA_TYPE_MAP if esde_media is not None else {}
ESDE_MEDIA_TYPE_ALIASES = esde_media.MEDIA_TYPE_ALIASES if esde_media is not None else {}
MEDIA_SOURCE_ROOT = PROJECT_ROOT / "frontend" / "RetroFE" / "collections"
MEDIA_DEST_ROOT = PROJECT_ROOT / "media"
METADATA_DEST_ROOT = PROJECT_ROOT / "metadata"
DAT_RAW_EXTS = {
    ".a26", ".a52", ".a78", ".bin", ".nes", ".unf", ".fds", ".gb", ".gbc", ".gba",
    ".nds", ".3ds", ".sms", ".md", ".sfc", ".smc", ".int", ".sg", ".rom", ".gg", ".pce",
    ".vb", ".ws", ".wsc", ".col", ".d64", ".adf", ".gen", ".32x", ".min", ".dsk",
    ".st", ".msa", ".j64", ".n64", ".z64", ".v64",
}
ROM_PAYLOAD_EXTS = DAT_RAW_EXTS | {
    ".3ds", ".3dsx", ".cia", ".cso", ".dsk", ".fds", ".gcm", ".gcz", ".gdi",
    ".hdf", ".iso", ".ipf", ".mdf", ".n64", ".nds", ".nsp", ".pbp", ".pkg",
    ".rvz", ".wbfs", ".wud", ".wux", ".xci", ".xiso", ".z64",
}
ARCHIVE_SIGNATURE_SCAN_BYTES = 1024 * 1024
HOST_PROFILE_PATH = PROJECT_ROOT / "frontend" / "RetroFE" / "Config" / "profiles" / "host-linux.json"
ZARCHIVE_TOOL_PATH = TOOL_ROOT / "runtimes" / "zarchive" / "bin" / "zarchive"


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path_abs, root_abs = os.path.abspath(path), os.path.abspath(root)
        return os.path.commonpath((path_abs, root_abs)) == root_abs
    except (ValueError, OSError):
        return False


def reject_symlink_components(path: Path, allow_leaf_symlink: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if allow_leaf_symlink and component == absolute:
            continue
        if os.path.lexists(component) and component.is_symlink():
            raise ValueError(f"Caminho com link simbólico recusado: {component}")


def portable_config(path: Path) -> dict:
    """Expand only the installation root; preserve legacy absolute configurations."""
    def expand(value):
        if isinstance(value, str):
            return value.replace("${WORKSPACE}", str(PROJECT_ROOT))
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value
    return expand(json.loads(path.read_text(encoding="utf-8")))


def load_policy() -> dict:
    return portable_config(POLICY_PATH)


def ensure_allowed(path: Path, key: str = "allowed_roots", allow_leaf_symlink: bool = False) -> None:
    reject_symlink_components(path, allow_leaf_symlink=allow_leaf_symlink)
    roots = [Path(x) for x in load_policy().get(key, [])]
    if not any(is_within(path, root) for root in roots):
        raise ValueError(f"Caminho fora das raízes autorizadas ({key}): {path}")


def existing_path_device(path: Path) -> int:
    cursor = path
    while not os.path.lexists(cursor):
        parent = cursor.parent
        if parent == cursor:
            raise FileNotFoundError(path)
        cursor = parent
    return cursor.lstat().st_dev if cursor.is_symlink() else cursor.stat().st_dev


def operation_dir(operation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", operation_id):
        raise ValueError(f"ID de operação inválido: {operation_id}")
    return OPS_ROOT / operation_id


def atomic_json(path: Path, value: dict) -> None:
    """Publish complete JSON after syncing its bytes, then sync POSIX directories."""
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


def atomic_write_bytes(path: Path, payload: bytes, mode: int | None = None) -> None:
    """Publish bytes with exclusive staging and durable POSIX directory entries."""
    missing_parents = []
    parent = path.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    temp = path.with_name(f".romtool-{uuid.uuid4().hex}.tmp")
    stream = temp.open("xb")
    try:
        with stream:
            stream.write(payload)
            stream.flush()
            if mode is not None:
                os.chmod(temp, mode)
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            directories = [path.parent]
            directories.extend(parent.parent for parent in missing_parents)
            for directory in directories:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    finally:
        temp.unlink(missing_ok=True)


def append_event(operation_id: str, event: str, **values) -> None:
    record = {
        "event_id": str(uuid.uuid4()), "timestamp": now(), "operation_id": operation_id,
        "event": event, "action": values.pop("action", ""), "status": values.pop("status", ""),
        "source": values.pop("source", ""), "destination": values.pop("destination", ""),
        "sha256_before": values.pop("sha256_before", ""), "sha256_after": values.pop("sha256_after", ""),
        "size_before": values.pop("size_before", ""), "size_after": values.pop("size_after", ""),
        "details": values,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    for path in (OPS_ROOT / "journal.jsonl", operation_dir(operation_id) / "events.jsonl"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())


def path_snapshot(path: Path, include_hash: bool = True, allow_leaf_symlink: bool = False) -> dict:
    if path.is_symlink():
        if not allow_leaf_symlink:
            raise ValueError(f"Link simbólico recusado: {path}")
        st = path.lstat()
        target = os.readlink(path)
        link_bytes = os.fsencode(target)
        return {"kind": "symlink", "symlink_target": target, "size_bytes": st.st_size,
                "sha256": hashlib.sha256(link_bytes).hexdigest() if include_hash else "",
                "device": st.st_dev, "inode": st.st_ino, "mtime_ns": st.st_mtime_ns}
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        st = path.stat()
        return {"kind": "file", "size_bytes": st.st_size,
                "sha256": sha256_file(path) if include_hash else "",
                "device": st.st_dev, "inode": st.st_ino, "mtime_ns": st.st_mtime_ns}
    if not path.is_dir():
        raise ValueError(f"Tipo de caminho não suportado: {path}")
    rows, total = [], 0
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise ValueError(f"Diretório simbólico recusado: {current_path / name}")
        for name in files:
            item = current_path / name
            if item.is_symlink():
                raise ValueError(f"Arquivo simbólico recusado: {item}")
            st = item.stat()
            rel = item.relative_to(path).as_posix()
            rows.append({"relative_path": rel, "size_bytes": st.st_size,
                         "sha256": sha256_file(item) if include_hash else "",
                         "device": st.st_dev, "inode": st.st_ino, "mtime_ns": st.st_mtime_ns})
    rows.sort(key=lambda row: row["relative_path"].casefold())
    digest = hashlib.sha256()
    for row in rows:
        total += row["size_bytes"]
        digest.update(row["relative_path"].encode("utf-8", "surrogateescape") + b"\0")
        digest.update(str(row["size_bytes"]).encode() + b"\0")
        digest.update(row["sha256"].encode() + b"\n")
    root_stat = path.stat()
    return {"kind": "directory", "file_count": len(rows), "size_bytes": total,
            "sha256": digest.hexdigest() if include_hash else "", "tree_files": rows if include_hash else [],
            "device": root_stat.st_dev, "inode": root_stat.st_ino, "mtime_ns": root_stat.st_mtime_ns}


def steam_save_link_is_registered(source: Path, target: Path, appid: str) -> bool:
    try:
        state = json.loads((OUTPUT_ROOT / "estado-final-verificacao.json").read_text(encoding="utf-8"))
        game = state.get("steam_centralization", {}).get("games", {}).get(str(appid), {})
        saves = game.get("saves", {})
        return any(Path(row.get("source_path", "")).absolute() == source.absolute()
                   and Path(row.get("canonical_path", "")).resolve(strict=False) == target.resolve(strict=False)
                   and row.get("status") == "centralized"
                   for row in saves.values())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def steam_game_has_centralized_saves(appid: str, game_root: Path) -> bool:
    try:
        state = json.loads((OUTPUT_ROOT / "estado-final-verificacao.json").read_text(encoding="utf-8"))
        game = state.get("steam_centralization", {}).get("games", {}).get(str(appid), {})
        for save in game.get("saves", {}).values():
            if save.get("status") != "centralized":
                continue
            if is_within(Path(save.get("source_path", "")), game_root):
                return True
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return False


def steam_tree_snapshot(path: Path, allow_save_symlinks: bool = False,
                        logical_snapshot: dict | None = None, appid: str = "") -> dict:
    """Hash a Steam game tree; optionally treat save symlinks as their regular payload."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Steam install precisa ser uma pasta real: {path}")
    root_stat = path.lstat()
    files, directories = [], []
    digest = hashlib.sha256()
    total = 0
    logical_files = {row["relative_path"]: row for row in (logical_snapshot or {}).get("tree_files", [])}
    for current, names, filenames in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        names.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for name in names:
            item = current_path / name
            st = item.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise ValueError(f"Steam tree contém link ou tipo especial não suportado: {item}")
            rel = item.relative_to(path).as_posix()
            mode = stat.S_IMODE(st.st_mode)
            directories.append({"relative_path": rel, "mode": mode})
        for name in filenames:
            item = current_path / name
            st = item.lstat()
            payload = item
            rel = item.relative_to(path).as_posix()
            expected_file = logical_files.get(rel)
            if stat.S_ISLNK(st.st_mode) and allow_save_symlinks:
                try:
                    payload = item.resolve(strict=True)
                    target_stat = payload.stat()
                except (OSError, RuntimeError) as exc:
                    raise ValueError(f"Steam save symlink quebrado: {item}") from exc
                save_root = (PROJECT_ROOT / "saves" / "steam").resolve(strict=False)
                if not is_within(payload, save_root) or not stat.S_ISREG(target_stat.st_mode):
                    raise ValueError(f"Steam tree contém symlink que não aponta a save canônico: {item}")
                if (not expected_file or not appid
                        or not steam_save_link_is_registered(item, payload, appid)):
                    raise ValueError(f"Symlink de save não está registrado para este AppID: {item}")
                st = target_stat
                logical_size = int(expected_file["size_bytes"])
                mode = int(expected_file["mode"])
                file_hash = str(expected_file["sha256"])
            else:
                mode = stat.S_IMODE(st.st_mode)
                logical_size = st.st_size
                file_hash = sha256_file(payload)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"Steam tree contém link ou tipo especial não suportado: {item}")
            files.append({"relative_path": rel, "size_bytes": logical_size, "mode": mode,
                          "sha256": file_hash, "device": st.st_dev, "inode": st.st_ino,
                          "mtime_ns": st.st_mtime_ns})
            total += logical_size
    directories.sort(key=lambda row: row["relative_path"].casefold())
    files.sort(key=lambda row: row["relative_path"].casefold())
    entries = [("D", row["relative_path"], row["mode"], 0, "") for row in directories]
    entries.append(("D", "", stat.S_IMODE(root_stat.st_mode), 0, ""))
    entries.extend(("F", row["relative_path"], row["mode"], row["size_bytes"], row["sha256"]) for row in files)
    for kind, rel, mode, size, file_hash in sorted(entries, key=lambda row: (row[1].casefold(), row[0])):
        digest.update(f"{kind}\0{rel}\0{mode:o}\0{size}\0{file_hash}\n".encode("utf-8", "surrogateescape"))
    return {"kind": "directory", "file_count": len(files), "directory_count": len(directories),
            "size_bytes": total, "sha256": digest.hexdigest(), "tree_files": files,
            "tree_directories": directories, "device": root_stat.st_dev,
            "inode": root_stat.st_ino, "mtime_ns": root_stat.st_mtime_ns,
            "mode": stat.S_IMODE(root_stat.st_mode)}


def operation_snapshot(path: Path, item: dict, allow_leaf_symlink: bool = False) -> dict:
    if item.get("action") == "steam_move_link":
        return steam_tree_snapshot(path, allow_save_symlinks=True,
                                   logical_snapshot=item.get("before") or item.get("precondition"),
                                   appid=str(item.get("steam", {}).get("appid", "")))
    if item.get("action") == "steam_save_move_link":
        return steam_payload_snapshot(path)
    return path_snapshot(path, include_hash=True, allow_leaf_symlink=allow_leaf_symlink)


def steam_payload_snapshot(path: Path) -> dict:
    if path.is_symlink():
        return path_snapshot(path, include_hash=True, allow_leaf_symlink=True)
    if path.is_dir():
        return steam_tree_snapshot(path)
    return path_snapshot(path, include_hash=True)


def parse_sfo(data: bytes) -> dict:
    if len(data) < 20 or data[:4] != b"\x00PSF":
        return {}
    _, _, key_start, data_start, count = struct.unpack_from("<IIIII", data, 0)
    if count > 10000 or 20 + count * 16 > len(data):
        return {}
    result = {}
    for index in range(count):
        key_offset, fmt, length, _, value_offset = struct.unpack_from("<HHIII", data, 20 + 16 * index)
        key_pos = key_start + key_offset
        if key_pos >= len(data):
            continue
        key_end = data.find(b"\0", key_pos)
        if key_end < 0:
            continue
        key = data[key_pos:key_end].decode("utf-8", "replace")
        raw = data[data_start + value_offset:data_start + value_offset + length]
        result[key] = raw.split(b"\0", 1)[0].decode("utf-8", "replace") if fmt in (0x0204, 0x0004) else raw.hex()
    return result


def verify_pfs0(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(16)
        if len(header) != 16 or header[:4] != b"PFS0":
            return {"status": "invalid_header", "error": "PFS0 magic/header missing"}
        count, strings_size, _ = struct.unpack_from("<III", header, 4)
        if count > 2_000_000:
            return {"status": "invalid_header", "error": f"entry count unreasonable: {count}"}
        table_start, strings_start = 16, 16 + count * 0x18
        payload_start = strings_start + strings_size
        if payload_start > size:
            return {"status": "truncated_table", "entry_count": count}
        stream.seek(strings_start)
        names = stream.read(strings_size)
        issues, sample = [], []
        stream.seek(table_start)
        for index in range(count):
            entry = stream.read(0x18)
            if len(entry) != 0x18:
                issues.append(f"entry {index}: truncated")
                break
            offset, length, name_offset, _ = struct.unpack("<QQII", entry)
            if name_offset >= len(names):
                issues.append(f"entry {index}: name offset out of range")
                name = ""
            else:
                end = names.find(b"\0", name_offset)
                if end < 0:
                    issues.append(f"entry {index}: unterminated name")
                    name = ""
                else:
                    name = names[name_offset:end].decode("utf-8", "replace")
            if payload_start + offset + length > size:
                issues.append(f"entry {index}: payload exceeds file")
            if index < 20:
                sample.append(name)
    return {"status": "valid" if not issues else "invalid", "entry_count": count,
            "string_table_bytes": strings_size, "payload_start": payload_start,
            "entries_checked": count, "issues": issues[:100], "member_names_sample": sample}


def verify_cue(path: Path) -> dict:
    references = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', line, re.IGNORECASE)
        if match:
            name = match.group(1) or match.group(2)
            target = Path(name)
            if not target.is_absolute():
                target = path.parent / target
            references.append({"name": name, "exists": target.is_file()})
    missing = [x["name"] for x in references if not x["exists"]]
    return {"status": "valid" if references and not missing else "invalid_or_unresolved",
            "reference_count": len(references), "references": references, "missing": missing}


def verify_m3u(path: Path) -> dict:
    references = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        target = Path(name)
        if not target.is_absolute():
            target = path.parent / target
        references.append({"name": name, "exists": target.is_file()})
    missing = [item["name"] for item in references if not item["exists"]]
    return {"status": "valid" if references and not missing else "invalid_or_unresolved",
            "reference_count": len(references), "references": references, "missing": missing}


def verify_gdi(path: Path) -> dict:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
             if line.strip()]
    issues, tracks = [], []
    declared_count = None
    if not lines:
        return {"status": "invalid_or_unresolved", "declared_track_count": 0,
                "track_count": 0, "tracks": [], "issues": ["empty GDI"]}
    try:
        declared_count = int(lines[0])
        if declared_count < 1:
            issues.append("declared track count must be positive")
    except ValueError:
        issues.append("first line is not an integer track count")
    for line_number, line in enumerate(lines[1:], start=2):
        try:
            fields = shlex.split(line, posix=True)
        except ValueError as exc:
            issues.append(f"line {line_number}: {exc}")
            continue
        if len(fields) < 6:
            issues.append(f"line {line_number}: expected six track fields")
            continue
        name = fields[4]
        target = Path(name)
        if not target.is_absolute():
            target = path.parent / target
        tracks.append({"line": line_number, "track": fields[0], "lba": fields[1],
                       "type": fields[2], "sector_size": fields[3], "name": name,
                       "exists": target.is_file()})
    if declared_count is not None and declared_count != len(tracks):
        issues.append(f"declared {declared_count} tracks but parsed {len(tracks)}")
    missing = [track["name"] for track in tracks if not track["exists"]]
    if missing:
        issues.append("one or more referenced track files are missing")
    return {"status": "valid" if tracks and not issues else "invalid_or_unresolved",
            "declared_track_count": declared_count, "track_count": len(tracks),
            "tracks": tracks, "missing": missing, "issues": issues}


def run_validator(argv: list[str], full_output: bool = False, *, env: dict[str, str] | None = None,
                  timeout: float | None = None) -> dict:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False,
                                env=env, timeout=timeout)
        stdout, stderr = result.stdout, result.stderr
        if not full_output:
            stdout, stderr = stdout[-12000:], stderr[-4000:]
        return {"returncode": result.returncode, "stdout": stdout, "stderr": stderr}
    except FileNotFoundError:
        return {"returncode": None, "error": f"validator unavailable: {argv[0]}"}
    except subprocess.TimeoutExpired as error:
        return {"returncode": None, "timed_out": True,
                "stdout": (error.stdout or "")[-12000:], "stderr": (error.stderr or "")[-4000:]}


def host_runtime_probe_environment(row: dict) -> dict[str, str] | None:
    templates = row.get("environment", {})
    if not templates:
        return None
    if not isinstance(templates, dict):
        raise ValueError("O environment do perfil command precisa ser um objeto")
    env = os.environ.copy()
    userdata_subdir = row.get("userdata_subdir")
    userdata_root = None
    if userdata_subdir:
        relative = Path(str(userdata_subdir))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("userdata_subdir inválido durante a sonda de runtime")
        userdata_base = PROJECT_ROOT / "emulator" / "data"
        userdata_root = (userdata_base / relative).resolve()
        if not is_within(userdata_root, userdata_base.resolve()):
            raise ValueError("userdata_subdir saiu de UserData durante a sonda de runtime")
        cursor = userdata_base
        if cursor.is_symlink():
            raise ValueError("UserData é symlink durante a sonda de runtime")
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"Componente symlink em userdata_subdir: {cursor}")
    values = {"userdata": str(userdata_root)} if userdata_root else {}
    for name, template in templates.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Variável inválida no ambiente do perfil: {name!r}")
        if not isinstance(template, str):
            raise ValueError(f"Valor do ambiente precisa ser texto: {name}")
        try:
            value = template.format_map(values)
        except KeyError as error:
            raise ValueError(f"Placeholder não reconhecido no ambiente do perfil: {error}") from error
        if name in {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"}:
            if userdata_root is None or not is_within(Path(value).expanduser().resolve(), userdata_root):
                raise ValueError(f"{name} precisa permanecer dentro de userdata_subdir")
        env[name] = value
    return env


def detect_archive_format(path: Path) -> dict:
    """Detect supported archive formats from their bytes, never from the suffix."""
    try:
        if zipfile.is_zipfile(path):
            return {"format": "zip", "method": "zipfile end-record/signature check",
                    "signature": "PK archive structure", "offset": 0}
        with path.open("rb") as stream:
            data = stream.read(ARCHIVE_SIGNATURE_SCAN_BYTES + 16)
    except OSError as exc:
        return {"format": "unknown", "method": "read_failed", "signature": "",
                "offset": None, "error": str(exc)}

    signatures = (
        (b"7z\xbc\xaf\x27\x1c", "7z", "7-Zip signature"),
        (b"Rar!\x1a\x07\x01\x00", "rar5", "RAR 5 signature"),
        (b"Rar!\x1a\x07\x00", "rar4", "RAR 4 signature"),
    )
    for signature, archive_format, label in signatures:
        offset = data.find(signature, 0, ARCHIVE_SIGNATURE_SCAN_BYTES + len(signature))
        if offset >= 0:
            return {"format": archive_format, "method": "magic_signature",
                    "signature": label, "offset": offset}

    # LHA has no single fixed file signature. Its first header contains a
    # bounded header size and a method tag such as -lh5- or -lz4-.
    for offset in range(min(ARCHIVE_SIGNATURE_SCAN_BYTES, max(0, len(data) - 7))):
        header_size = data[offset]
        method = data[offset + 2:offset + 7]
        if 7 <= header_size <= 255 and re.fullmatch(rb"-(?:lh|lz|pm|p[mp])[0-7]-", method):
            return {"format": "lha", "method": "lha_header_method",
                    "signature": method.decode("ascii", "replace"), "offset": offset}
    return {"format": "unknown", "method": "no_supported_signature",
            "signature": "", "offset": None}


def classify_archive_contents(source_paths: list[str], members: list[dict]) -> dict:
    """Classify archive structure from its saved member listing and source labels."""
    files = [member for member in members if not member.get("directory")]
    member_paths = [str(member.get("path", "")).replace("\\", "/") for member in files]
    normalized_paths = [value.casefold() for value in member_paths]
    extensions = sorted({Path(value).suffix.casefold() for value in member_paths if Path(value).suffix})
    source_text = "\n".join(source_paths).casefold()
    evidence: list[dict] = []
    classification = "unclassified"
    confidence = "low"

    descriptor_suffixes = {".cue", ".gdi", ".m3u", ".ccd", ".toc"}
    descriptors = [name for name in member_paths if Path(name).suffix.casefold() in descriptor_suffixes]
    track_candidates = [name for name in member_paths
                        if Path(name).suffix.casefold() in {".bin", ".img", ".iso", ".wav", ".ogg", ".flac", ".sub"}]
    if descriptors:
        classification, confidence = "dependent_set", "high"
        evidence.append({"kind": "descriptor_member", "paths": descriptors[:12]})
        evidence.append({"kind": "possible_track_members", "paths": track_candidates[:20]})
        evidence.append({"kind": "dependency_limit",
                         "detail": "archive listing identifies likely members; descriptor references still require parsing before extraction"})
    else:
        mod_markers = []
        if re.search(r"(?i)(?:^|[/\\ _-])(?:mods?|patch(?:es|ed)?|translation|translated|tradu[cç][aã]o|hack)(?:$|[/\\ _-])", source_text):
            mod_markers.append("source path/name contains mod, patch, translation, or hack marker")
        if any(marker in source_text for marker in ("/mods/", "_mod's", "tradução", "traducao", "translated", "translation")):
            mod_markers.append("source path is in a mod/translation review location")
        patch_extensions = sorted({Path(name).suffix.casefold() for name in member_paths
                                   if Path(name).suffix.casefold() in {".ips", ".bps", ".ups", ".ppf", ".xdelta", ".vcdiff"}})
        if patch_extensions:
            mod_markers.append("patch payload extension(s): " + ", ".join(patch_extensions))
        if any("/atmosphere/contents/" in name or "/romfs/" in name for name in normalized_paths):
            mod_markers.append("member path contains a layered/mod content tree")
        nested_archives = [name for name in member_paths if Path(name).suffix.casefold() in ARCHIVE_EXTS]
        install_guides = [name for name in member_paths
                          if any(marker in Path(name).name.casefold()
                                 for marker in ("readme", "install", "instrução", "instrucao", "tutorial"))
                          and Path(name).suffix.casefold() in {".txt", ".md", ".nfo"}]
        if nested_archives and install_guides:
            mod_markers.append("nested archive member(s) accompanied by installation/readme instructions")
        if mod_markers:
            classification, confidence = "package_or_mod", "high"
            evidence.extend({"kind": "mod_or_patch_marker", "detail": value} for value in dict.fromkeys(mod_markers))
        else:
            app_tree_paths = [name for name in member_paths if (
                re.search(r"(?:^|/)[a-z]{4}\d{5,6}-app0(?:/|$)", name.casefold())
                or "/sce_sys/param.sfo" in name.casefold()
                or name.casefold().endswith("/eboot.bin")
                or name.casefold() in {"app0", "sce_sys/param.json"}
            )]
            executable_paths = [name for name in member_paths
                                if Path(name).suffix.casefold() in {".exe", ".dll", ".msi", ".bat", ".com", ".so"}]
            if app_tree_paths:
                classification, confidence = "installation_payload", "high"
                evidence.append({"kind": "application_tree_marker", "paths": app_tree_paths[:12]})
            elif executable_paths:
                classification, confidence = "installation_payload", "high"
                evidence.append({"kind": "executable_and_data_package",
                                 "executables": executable_paths[:12], "file_count": len(files),
                                 "extensions": extensions})
            elif len(files) == 1 and Path(files[0].get("path", "")).suffix.casefold() in ROM_PAYLOAD_EXTS:
                classification, confidence = "single_rom_candidate", "medium"
                evidence.append({"kind": "single_rom_like_member", "path": member_paths[0],
                                 "extension": Path(member_paths[0]).suffix.casefold()})
                evidence.append({"kind": "identity_limit",
                                 "detail": "single payload structure does not prove title identity, completeness, or launchability"})
            elif len(files) > 1:
                evidence.append({"kind": "multiple_files_without_proven_relationship",
                                 "file_count": len(files), "extensions": extensions})
            elif files:
                evidence.append({"kind": "single_member_extension_not_recognized_as_rom",
                                 "path": member_paths[0], "extension": Path(member_paths[0]).suffix.casefold()})

    encrypted = sum(bool(member.get("encrypted")) for member in members)
    if encrypted:
        classification, confidence = "unclassified", "low"
        evidence.append({"kind": "encrypted_members", "count": encrypted,
                         "detail": "content cannot be structurally classified without credentials"})
    return {
        "classification": classification,
        "confidence": confidence,
        "evidence": evidence,
        "member_count": len(members),
        "file_count": len(files),
        "directory_count": len(members) - len(files),
        "extensions": extensions,
        "identity_established": False,
        "emulator_compatibility_established": False,
        "launchability_established": False,
    }


def verify_one(path: Path, list_archive_contents: bool = False) -> dict:
    before = path_snapshot(path, include_hash=True)
    suffix = path.suffix.lower()
    result = {"path": str(path), "kind": before["kind"], "size_bytes": before["size_bytes"],
              "sha256": before["sha256"], "validator": "sha256_only", "status": "identity_recorded"}
    if path.is_dir():
        result.update({"validator": "tree_sha256", "status": "tree_hashed", "file_count": before["file_count"]})
    elif before["kind"] == "file" and (detected := detect_archive_format(path))["format"] != "unknown":
        result["archive_signature_detection"] = detected
        result["detected_archive_format"] = detected["format"]
        try:
            if detected["format"] == "zip" or list_archive_contents:
                listing = archive_members(path)
                members = listing["members"]
                result.update({"validator": listing["validator"], "status": "ok",
                               "member_count": len(members),
                               "encrypted_member_count": sum(bool(member.get("encrypted")) for member in members),
                               "uncompressed_bytes": sum(int(member.get("size_bytes", 0))
                                                         for member in members if not member.get("directory")),
                               "content_classification": listing["content_classification"]})
                if list_archive_contents:
                    result["archive_members"] = members
            else:
                checked = run_validator(["7z", "t", str(path)])
                result.update({"validator": "7z_test", "status": "ok" if checked.get("returncode") == 0 else "archive_error",
                               "returncode": checked.get("returncode"), "output": checked.get("stdout", ""),
                               "error": checked.get("error", checked.get("stderr", ""))})
        except Exception as exc:
            result.update({"validator": "archive_test_and_list", "status": "archive_error",
                           "error": f"{type(exc).__name__}: {exc}"})
    elif before["kind"] == "file" and suffix in ARCHIVE_EXTS:
        detected = detect_archive_format(path)
        result.update({"validator": "archive_signature_detection", "status": "unsupported_archive_signature",
                       "archive_signature_detection": detected,
                       "error": "A extensão indica archive, mas a assinatura não corresponde a ZIP/7z/RAR/LHA suportado."})
    elif suffix == ".chd":
        info = run_validator(["chdman", "info", "-i", str(path)], full_output=True)
        checked = run_validator(["chdman", "verify", "-i", str(path)])
        info_output = info.get("stdout", "")
        logical_match = re.search(r"^Logical size:\s*([\d,]+)\s+bytes\s*$", info_output, re.MULTILINE)
        hunks_match = re.search(r"^Total Hunks:\s*([\d,]+)\s*$", info_output, re.MULTILINE)
        units_match = re.search(r"^Total Units:\s*([\d,]+)\s*$", info_output, re.MULTILINE)
        logical_size = int(logical_match.group(1).replace(",", "")) if logical_match else None
        if info.get("returncode") == 0 and logical_size == 0:
            chd_status = "empty_chd"
        elif info.get("returncode") == 0 and logical_size is not None and checked.get("returncode") == 0:
            chd_status = "ok"
        elif logical_size is None:
            chd_status = "chd_info_unparsed"
        else:
            chd_status = "chd_error_or_validator_unavailable"
        result.update({"validator": "chdman_info_and_verify", "status": chd_status,
                       "returncode": checked.get("returncode"),
                       "logical_size_bytes": logical_size,
                       "total_hunks": int(hunks_match.group(1).replace(",", "")) if hunks_match else None,
                       "total_units": int(units_match.group(1).replace(",", "")) if units_match else None,
                       "info_returncode": info.get("returncode"), "info_output": info_output[-4000:],
                       "output": checked.get("stdout", ""),
                       "error": checked.get("error", checked.get("stderr", "")) or info.get("error", info.get("stderr", ""))})
    elif suffix in {".adf", ".hdf"}:
        scanner = TOOL_ROOT / "bin" / "xdfscan"
        checked = run_validator([str(scanner), "-v", str(path)], full_output=True)
        output = (checked.get("stdout", "") + "\n" + checked.get("stderr", "")).replace("\r", "\n")
        escaped_path = re.escape(str(path))
        if checked.get("returncode") is None:
            amiga_status = "amiga_validator_unavailable"
        elif checked.get("returncode") != 0:
            amiga_status = "amiga_scan_failed"
        elif re.search(rf"(?m)^\s*ok\s+{escaped_path}\s*$", output):
            amiga_status = "ok"
        elif re.search(r"(?m)\sNOK\s+|\bE\d{3}\b|\bw\d{3}\b", output):
            amiga_status = "amiga_filesystem_warning_or_error"
        elif re.search(r"\b(?:nofs|NDOS|BLKDEV\?)\b", output):
            amiga_status = "no_amiga_ofs_ffs_filesystem"
        else:
            amiga_status = "amiga_scan_unparsed"
        result.update({"validator": "amitools_xdfscan_read_only", "validator_version": "amitools 0.8.1",
                       "status": amiga_status, "returncode": checked.get("returncode"),
                       "output": checked.get("stdout", ""), "error": checked.get("error", checked.get("stderr", ""))})
    elif suffix == ".exfat":
        checked = run_validator(["fsck.exfat", "-n", str(path)])
        result.update({"validator": "fsck_exfat_read_only",
                       "status": "ok" if checked.get("returncode") == 0 else "filesystem_error_or_validator_unavailable",
                       "returncode": checked.get("returncode"), "output": checked.get("stdout", ""),
                       "error": checked.get("error", checked.get("stderr", ""))})
    elif suffix == ".nsp":
        result["validator"] = "pfs0_structure"
        result.update(verify_pfs0(path))
    elif suffix == ".cue":
        result["validator"] = "cue_reference_check"
        result.update(verify_cue(path))
    elif suffix == ".m3u":
        result["validator"] = "m3u_reference_check"
        result.update(verify_m3u(path))
    elif suffix == ".gdi":
        result["validator"] = "gdi_track_reference_check"
        result.update(verify_gdi(path))
    elif suffix == ".sfo":
        metadata = parse_sfo(path.read_bytes())
        result.update({"validator": "param_sfo_metadata",
                       "status": "metadata_read" if metadata else "invalid_sfo",
                       "metadata": metadata})
    else:
        result["status"] = "not_validated_by_format_specific_tool"
    after = path_snapshot(path, include_hash=False)
    result["metadata_unchanged_during_validation"] = (
        before["kind"] == after["kind"] and before["size_bytes"] == after["size_bytes"]
        and (before.get("mtime_ns") == after.get("mtime_ns") if before["kind"] == "file" else True)
        and (before.get("inode") == after.get("inode") if before["kind"] == "file" else True)
    )
    if not result["metadata_unchanged_during_validation"]:
        result["status"] = "changed_during_read_only_validation"
    return result


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    if fields is None:
        fields = list(rows[0]) if rows else []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_csv_atomic(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def record_analysis(kind: str, title: str, rows: list[dict], output_dir: Path,
                    evidence: dict | None = None) -> dict:
    operation_id = f"{kind}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = operation_dir(operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": kind,
        "title": title, "created_at": now(), "completed_at": now(),
        "status": "completed", "read_only": True, "item_count": len(rows),
        "evidence": evidence or {}, "items": rows,
    }
    atomic_json(opdir / "manifest.json", manifest)
    csv_path = output_dir / f"{operation_id}.csv"
    write_csv(csv_path, rows)
    append_event(operation_id, "analysis_results_registered", action=kind, status="completed",
                 details={"item_count": len(rows), "evidence": evidence or {}})
    for row in rows:
        append_event(operation_id, f"{kind}_item", action=kind, status=row.get("status", ""),
                     source=row.get("path", row.get("source_path", "")),
                     sha256_before=row.get("sha256", row.get("payload_sha256", "")),
                     size_before=row.get("size_bytes", row.get("payload_size_bytes", "")),
                     details=row)
    append_event(operation_id, f"{kind}_completed", action=kind, status="completed",
                 destination=str(csv_path), details={"manifest": str(opdir / "manifest.json")})
    rebuild_history_index()
    return {"operation_id": operation_id, "kind": kind, "status": "completed", "item_count": len(rows),
            "evidence": evidence or {}, "manifest": str(opdir / "manifest.json"),
            "csv": str(csv_path), "items": rows}


def infer_system_id(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return root.name
    if root.name.casefold() in {"roms", "roms-review", "roms_review"} and relative.parts:
        return relative.parts[0]
    return root.name


def duplicate_audit(root: Path, output_dir: Path, forced_system: str = "") -> dict:
    if not root.is_dir():
        raise ValueError(f"Duplicate audit requires a directory: {root}")
    size_buckets = defaultdict(list)
    for current, _, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                size_buckets[(forced_system or infer_system_id(path, root), path.stat().st_size)].append(path)
            except OSError:
                continue
    hashes = defaultdict(list)
    compared = 0
    for (system_id, size), paths in size_buckets.items():
        if len(paths) < 2:
            continue
        for path in paths:
            compared += 1
            digest = sha256_file(path)
            hashes[(system_id, size, digest)].append(path)
    rows = []
    for (system_id, size, digest), paths in sorted(hashes.items()):
        if len(paths) < 2:
            continue
        group_id = hashlib.sha256(f"{system_id}\\0{size}\\0{digest}".encode()).hexdigest()[:16]
        for path in sorted(paths, key=lambda x: str(x).casefold()):
            rows.append({
                "group_id": group_id, "system_id": system_id, "path": str(path),
                "size_bytes": size, "sha256": digest, "copies_in_group": len(paths),
                "logical_duplicate_bytes": size * (len(paths) - 1),
                "status": "exact_same_system_hash_match_review_only",
                "action": "do_not_delete_automatically",
            })
    evidence = {
        "root": str(root), "forced_system_id": forced_system or "",
        "same_size_files_hashed": compared,
        "duplicate_groups": len({x["group_id"] for x in rows}),
        "duplicate_files": len(rows),
        "logical_duplicate_bytes": sum(x["size_bytes"] * (x["copies_in_group"] - 1)
                                       for x in {x["group_id"]: x for x in rows}.values()),
        "method": "SHA-256 equality grouped only within the inferred/declared system; no deletion",
    }
    return record_analysis("duplicate_audit", "Exact same-system file comparison", rows, output_dir, evidence)


def logical_duplicate_audit(root: Path, output_dir: Path, forced_system: str = "") -> dict:
    """Compare one ROM per container within each platform; preserve auxiliary members."""
    if not root.is_dir(): raise ValueError(f'Duplicate audit requires a directory: {root}')
    root=root.resolve(strict=True)
    profiles, _, roots = load_compression_data()
    canonical = Path(roots["canonical_rom_root"]).resolve()
    raw_by_system = defaultdict(set)
    for profile in profiles.get('profiles', []):
        if profile.get('conversion_kind') == 'single_file_archive':
            for sid, extensions in profile.get('source_extensions_by_system', {}).items():
                raw_by_system[sid].update(str(ext).casefold() for ext in extensions)
    rows, groups = [], defaultdict(list)
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(n for n in directories if not (Path(current)/n).is_symlink())
        for name in sorted(names):
            source = Path(current)/name
            sid = (source.relative_to(canonical).parts[0] if is_within(source,canonical)
                   else forced_system or infer_system_id(source, root))
            if forced_system and sid != forced_system:
                continue
            allowed = raw_by_system.get(sid, set())
            row = {'path':str(source), 'system_id':sid, 'status':'unprofiled_preserved'}
            if source.is_symlink() or not source.is_file() or source.suffix.casefold() not in allowed | {'.zip','.7z'} or not allowed:
                rows.append(row); continue
            try:
                before = path_snapshot(source, include_hash=True)
                member_name = ''
                if source.suffix.casefold() in {'.zip','.7z'}:
                    listing = archive_members(source)
                    members = listing['members']
                    rom_members=[m for m in members if not m['directory'] and Path(m['path']).suffix.casefold() in allowed]
                    if len(rom_members)!=1 or any(m.get('encrypted') for m in members):
                        raise ValueError('Contêiner não contém exatamente uma ROM suportada sem criptografia')
                    member = rom_members[0];member_name = member['path']
                    row['auxiliary_member_count']=sum(not m['directory'] and m is not member for m in members)
                    row['auxiliary_member_names']=[m['path'] for m in members if not m['directory'] and m is not member]
                    payload = hash_archive_member(source, listing['archive_format'], member)
                else:
                    payload = file_integrity(source)
                after = path_snapshot(source, include_hash=True)
                if before != after: raise ValueError('Origem mudou durante a auditoria')
                row.update(status='validated_payload',size_bytes=before['size_bytes'],sha256=before['sha256'],payload_member=member_name,payload_size_bytes=payload['size_bytes'],payload_sha256=payload['sha256'])
                groups[(sid,payload['size_bytes'],payload['sha256'])].append(row)
            except Exception as exc:
                row.update(status='preserved_unverified',reason=f'{type(exc).__name__}: {exc}')
            rows.append(row)
    duplicates = 0
    for (sid,size,digest), peers in groups.items():
        if len(peers)<2: continue
        duplicates += 1
        group = hashlib.sha256(f'{sid}\0{size}\0{digest}'.encode()).hexdigest()[:16]
        for row in peers: row.update(group_id=group,copies_in_group=len(peers),status='same_payload_review_only',action='review_references_and_auxiliary_members; preserve_complete_original_if_quarantined; no automatic deletion')
    return record_analysis('logical_duplicate_audit','Same-platform ROM payload comparison across raw/ZIP/7z',rows,output_dir,{'root':str(root),'forced_system_id':forced_system,'duplicate_groups':duplicates,'method':'Container integrity plus full payload SHA-256 and size; exactly one supported ROM. Auxiliary members are recorded and must be preserved; equal ROMs do not imply identical bundles.','permanent_deletion':False})


def stream_hashes(stream) -> dict:
    crc = 0
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    while block := stream.read(8 * 1024 * 1024):
        size += len(block)
        crc = zlib.crc32(block, crc)
        md5.update(block); sha1.update(block); sha256.update(block)
    return {"size_bytes": size, "crc32": f"{crc & 0xffffffff:08x}",
            "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}


def read_dat_index(dat_path: Path) -> tuple[dict, str]:
    digest = sha256_file(dat_path)
    root = ET.parse(dat_path).getroot()
    index = defaultdict(list)
    for parent in root.iter():
        if parent.tag.lower() not in {"game", "machine"}:
            continue
        game_name = parent.attrib.get("name", "")
        description = parent.attrib.get("description", "")
        display_name = description or game_name
        for rom in parent.findall(".//rom"):
            try:
                size = int(rom.attrib.get("size", "-1"))
            except ValueError:
                continue
            crc, md5, sha1 = (rom.attrib.get(k, "").lower() for k in ("crc", "md5", "sha1"))
            if size < 0 or not crc or not md5 or not sha1:
                continue
            identity = (size, crc, md5, sha1)
            index[identity].append({
                "game_name": game_name, "description": description,
                "display_name": display_name, "rom_name": rom.attrib.get("name", ""),
            })
    return index, digest


def list_7z_members(path: Path) -> list[dict]:
    listed = run_validator(["7z", "l", "-slt", "-ba", str(path)], full_output=True)
    if listed.get("returncode") != 0:
        raise RuntimeError(listed.get("error", listed.get("stderr", "7z listing failed")))
    members = []
    for block in re.split(r"\r?\n\s*\r?\n", listed.get("stdout", "")):
        fields = {}
        for line in block.splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
                fields[key] = value
        name = fields.get("Path", "")
        if not name or fields.get("Folder") == "+" or "Size" not in fields:
            continue
        try:
            size = int(fields["Size"])
        except ValueError:
            continue
        members.append({"name": name, "size": size, "crc32": fields.get("CRC", "").lower()})
    return members


def hash_7z_member(path: Path, member_name: str) -> tuple[dict, int]:
    process = subprocess.Popen(
        ["7z", "x", "-so", "-spd", str(path), member_name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        hashes = stream_hashes(process.stdout)
    finally:
        process.stdout.close()
    return hashes, process.wait()


def dat_match(roots: list[Path], dat_path: Path, system_id: str, output_dir: Path,
              raw_extensions: list[str] | None = None,
              candidate_paths: list[Path] | None = None) -> dict:
    roots = [root for root in roots if root.is_dir()]
    if (not roots and not candidate_paths) or not dat_path.is_file():
        raise ValueError("DAT match requires at least one input directory and a DAT file")
    index, dat_digest = read_dat_index(dat_path)
    extensions = {x.lower() if x.startswith(".") else "." + x.lower()
                  for x in (raw_extensions or DAT_RAW_EXTS)}
    rows = []
    candidate_files = {}
    for root in roots:
        for current, _, names in os.walk(root, followlinks=False):
            for name in sorted(names, key=str.casefold):
                path = Path(current) / name
                if not path.is_symlink() and path.is_file():
                    candidate_files.setdefault(os.path.abspath(path), path)
    for path in candidate_paths or []:
        if not path.is_symlink() and path.is_file():
            candidate_files.setdefault(os.path.abspath(path), path)
    for path in sorted(candidate_files.values(), key=lambda item: str(item).casefold()):
                suffix = path.suffix.lower()
                if suffix == ".zip":
                    try:
                        with zipfile.ZipFile(path) as archive:
                            infos = [x for x in archive.infolist() if not x.is_dir()]
                            data_infos = [x for x in infos if Path(x.filename).suffix.lower() in extensions]
                            for info in data_infos:
                                try:
                                    with archive.open(info) as stream:
                                        hashes = stream_hashes(stream)
                                except Exception as exc:
                                    rows.append({"system_id": system_id, "source_path": str(path),
                                                 "payload_member": info.filename, "status": "archive_member_error",
                                                 "error": str(exc), "dat_sha256": dat_digest})
                                    continue
                                matches = index.get((hashes["size_bytes"], hashes["crc32"], hashes["md5"], hashes["sha1"]), [])
                                first = matches[0] if matches else {}
                                rows.append({
                                    "system_id": system_id, "source_path": str(path), "payload_member": info.filename,
                                    **{f"payload_{k}": v for k, v in hashes.items()},
                                    "status": "full_dat_match" if matches else "no_match_retain",
                                    "dat_game": first.get("display_name", ""),
                                    "dat_game_internal_name": first.get("game_name", ""),
                                    "dat_rom_name": first.get("rom_name", ""),
                                    "matching_dat_records": len(matches),
                                    "archive_member_count": len(infos),
                                    "non_rom_member_count": len(infos) - len(data_infos),
                                    "non_rom_member_names": [x.filename for x in infos if x not in data_infos],
                                    "suggested_container_name": f"{first.get('display_name')}.zip" if len(infos) == 1 and len(data_infos) == 1 and len(matches) == 1 else "",
                                    "dat_sha256": dat_digest,
                                })
                    except Exception as exc:
                        rows.append({"system_id": system_id, "source_path": str(path), "payload_member": "",
                                     "status": "archive_error_retain", "error": str(exc), "dat_sha256": dat_digest})
                elif suffix in {".7z", ".rar", ".lha"}:
                    try:
                        members = list_7z_members(path)
                        data_members = [m for m in members if Path(m["name"]).suffix.lower() in extensions]
                        for member in data_members:
                            try:
                                hashes, returncode = hash_7z_member(path, member["name"])
                                if returncode != 0 or hashes["size_bytes"] != member["size"]:
                                    raise RuntimeError(f"7z extraction status={returncode}, size={hashes['size_bytes']}/{member['size']}")
                                if member["crc32"] and hashes["crc32"] != member["crc32"]:
                                    raise RuntimeError(f"7z member CRC mismatch {hashes['crc32']} != {member['crc32']}")
                            except Exception as exc:
                                rows.append({"system_id": system_id, "source_path": str(path),
                                             "payload_member": member["name"], "status": "archive_member_error",
                                             "error": str(exc), "dat_sha256": dat_digest})
                                continue
                            matches = index.get((hashes["size_bytes"], hashes["crc32"], hashes["md5"], hashes["sha1"]), [])
                            first = matches[0] if matches else {}
                            rows.append({
                                "system_id": system_id, "source_path": str(path), "payload_member": member["name"],
                                **{f"payload_{k}": v for k, v in hashes.items()},
                                "status": "full_dat_match" if matches else "no_match_retain",
                                "dat_game": first.get("display_name", ""),
                                "dat_game_internal_name": first.get("game_name", ""),
                                "dat_rom_name": first.get("rom_name", ""),
                                "matching_dat_records": len(matches),
                                "archive_member_count": len(members),
                                "non_rom_member_count": len(members) - len(data_members),
                                "non_rom_member_names": [m["name"] for m in members if m not in data_members],
                                "suggested_container_name": f"{first.get('display_name')}{path.suffix}" if len(members) == 1 and len(data_members) == 1 and len(matches) == 1 else "",
                                "dat_sha256": dat_digest,
                            })
                    except Exception as exc:
                        rows.append({"system_id": system_id, "source_path": str(path), "payload_member": "",
                                     "status": "archive_error_retain", "error": str(exc), "dat_sha256": dat_digest})
                elif suffix in extensions:
                    try:
                        with path.open("rb", buffering=0) as stream:
                            hashes = stream_hashes(stream)
                        matches = index.get((hashes["size_bytes"], hashes["crc32"], hashes["md5"], hashes["sha1"]), [])
                        first = matches[0] if matches else {}
                        rows.append({
                            "system_id": system_id, "source_path": str(path), "payload_member": "",
                            **{f"payload_{k}": v for k, v in hashes.items()},
                            "status": "full_dat_match" if matches else "no_match_retain",
                            "dat_game": first.get("display_name", ""),
                            "dat_game_internal_name": first.get("game_name", ""),
                            "dat_rom_name": first.get("rom_name", ""),
                            "matching_dat_records": len(matches),
                            "suggested_filename": (first.get("rom_name") or f"{first.get('display_name')}{path.suffix}") if len(matches) == 1 else "",
                            "dat_sha256": dat_digest,
                        })
                    except Exception as exc:
                        rows.append({"system_id": system_id, "source_path": str(path), "payload_member": "",
                                     "status": "read_error_retain", "error": str(exc), "dat_sha256": dat_digest})
    statuses = defaultdict(int)
    for row in rows:
        statuses[row.get("status", "unknown")] += 1
    evidence = {"roots": [str(root) for root in roots], "explicit_candidate_path_count": len(candidate_paths or []),
                "unique_candidate_file_count": len(candidate_files),
                "system_id": system_id, "dat_path": str(dat_path),
                "dat_sha256": dat_digest, "dat_record_tuples": sum(map(len, index.values())),
                "results": dict(statuses),
                "method": "exact size + CRC32 + MD5 + SHA-1; ZIP, 7z/RAR/LHA stream members, and raw payloads; records SHA-256 evidence; no file changes"}
    return record_analysis("dat_match", "No-Intro full-tuple matching", rows, output_dir, evidence)


def record_validation(paths: list[Path], output_dir: Path, kind: str = "verify", jobs: int = 1,
                      list_archive_contents: bool = False) -> dict:
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    operation_id = f"{kind}-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = operation_dir(operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": "1.0", "operation_id": operation_id, "kind": kind,
                "created_at": now(), "status": "running", "read_only": True,
                "jobs": jobs, "archive_contents_listed": list_archive_contents, "items": []}
    atomic_json(opdir / "manifest.json", manifest)
    append_event(operation_id, "validation_started", action=kind, status="running",
                 details={"item_count": len(paths)})
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    futures = {executor.submit(verify_one, path, list_archive_contents): path for path in paths}
    try:
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {"path": str(path), "status": "validation_exception", "error": str(exc)}
            manifest["items"].append(row)
            manifest["updated_at"] = now()
            atomic_json(opdir / "manifest.json", manifest)
            append_event(operation_id, "item_validated", action=row.get("validator", "unknown"),
                         status=row.get("status", "unknown"), source=str(path),
                         sha256_before=row.get("sha256", ""), size_before=row.get("size_bytes", ""),
                         details={"metadata_unchanged": row.get("metadata_unchanged_during_validation"),
                                  "archive_member_count": row.get("member_count", ""),
                                  "detected_archive_format": row.get("detected_archive_format", ""),
                                  "content_classification": row.get("content_classification", {}),
                                  "error": row.get("error", "")})
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        manifest["status"] = "interrupted"
        manifest["completed_at"] = now()
        manifest["interruption_reason"] = "keyboard interrupt; completed item results preserved"
        atomic_json(opdir / "manifest.json", manifest)
        append_event(operation_id, "validation_interrupted", action=kind, status="interrupted",
                     details={"completed_items": len(manifest["items"]), "cancelled": True})
        rebuild_history_index()
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    success_states = {"ok", "valid", "tree_hashed", "metadata_read", "identity_recorded"}
    manifest["status"] = "completed_with_issues" if any(
        x.get("status") not in success_states for x in manifest["items"]
    ) else "completed"
    manifest["completed_at"] = now()
    atomic_json(opdir / "manifest.json", manifest)
    csv_path = output_dir / f"{operation_id}.csv"
    write_csv(csv_path, manifest["items"])
    append_event(operation_id, "validation_completed", action=kind, status=manifest["status"],
                 destination=str(csv_path), details={"manifest": str(opdir / "manifest.json")})
    rebuild_history_index()
    return {"manifest": str(opdir / "manifest.json"), "csv": str(csv_path), **manifest}


def discover_paths(args) -> list[Path]:
    paths = [Path(value) for value in args.path]
    if args.root:
        root = Path(args.root)
        if root.is_file():
            paths.append(root)
        elif root.is_dir():
            extensions = {x.lower() if x.startswith(".") else "." + x.lower() for x in (args.extensions or [])}
            for current, _, files in os.walk(root, followlinks=False):
                for name in files:
                    path = Path(current) / name
                    if path.is_symlink():
                        continue
                    if extensions and path.suffix.lower() not in extensions:
                        continue
                    if not extensions and path.suffix.lower() not in VERIFY_EXTS:
                        continue
                    paths.append(path)
        else:
            raise FileNotFoundError(root)
    unique = {os.path.abspath(path): Path(os.path.abspath(path)) for path in paths}
    return sorted(unique.values(), key=lambda x: str(x).casefold())


def run_inventory(roots: list[Path], output_dir: Path, with_hash: bool) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    operation_id = f"inventory-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = operation_dir(operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    rows = []
    for root in roots:
        if not root.exists():
            rows.append({"root": str(root), "relative_path": "", "status": "root_missing"})
            continue
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for current, dirs, files in os.walk(root, followlinks=False):
                current_path = Path(current)
                candidates.extend(current_path / name for name in sorted(files))
                candidates.extend(current_path / name for name in sorted(dirs) if (current_path / name).is_symlink())
        for path in candidates:
            try:
                st = path.lstat()
                symlink = stat.S_ISLNK(st.st_mode)
                rows.append({
                    "root": str(root), "relative_path": "." if path == root else path.relative_to(root).as_posix(),
                    "absolute_path": str(path), "kind": "symlink" if symlink else "file",
                    "extension": path.suffix.lower(), "size_bytes": st.st_size,
                    "mtime_ns": st.st_mtime_ns, "device": st.st_dev, "inode": st.st_ino,
                    "sha256": sha256_file(path) if with_hash and not symlink else "",
                    "status": "symlink_not_followed" if symlink else "inventoried",
                })
            except OSError as exc:
                rows.append({"root": str(root), "absolute_path": str(path), "status": "read_error", "error": str(exc)})
    manifest = {"schema_version": "1.0", "operation_id": operation_id, "kind": "inventory",
                "created_at": now(), "status": "completed", "roots": [str(x) for x in roots],
                "hashes_requested": with_hash, "entry_count": len(rows),
                "errors": sum(x.get("status") == "read_error" for x in rows)}
    atomic_json(opdir / "manifest.json", manifest)
    csv_path = output_dir / f"{operation_id}.csv"
    write_csv(csv_path, rows)
    append_event(operation_id, "inventory_completed", action="inventory", status="completed",
                 destination=str(csv_path), details=manifest)
    rebuild_history_index()
    return {**manifest, "csv": str(csv_path), "manifest": str(opdir / "manifest.json")}


def archive_members(path: Path) -> dict:
    detected = detect_archive_format(path)
    archive_format = detected["format"]
    if archive_format == "zip":
        members = []
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise ValueError(f"ZIP não passou CRC no membro: {bad}")
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                rel = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0xFFFF
                if rel.is_absolute() or ".." in rel.parts or stat.S_ISLNK(mode):
                    raise ValueError(f"Membro ZIP inseguro: {name}")
                members.append({"path": name.rstrip("/"), "directory": info.is_dir(),
                                "size_bytes": info.file_size, "packed_size_bytes": info.compress_size,
                                "crc32": f"{info.CRC:08x}", "encrypted": bool(info.flag_bits & 0x1)})
        return {"validator": "python_zipfile_crc", "status": "ok", "archive_format": "zip",
                "archive_signature_detection": detected, "members": members,
                "content_classification": classify_archive_contents([str(path)], members)}
    if archive_format not in {"7z", "rar4", "rar5", "lha"}:
        raise ValueError(f"Assinatura não corresponde a ZIP/7z/RAR/LHA suportado: {path} ({detected})")
    tested = run_validator(["7z", "t", str(path)])
    if tested.get("returncode") != 0:
        raise ValueError(f"Archive não passou 7z t: {tested}")
    listed = run_validator(["7z", "l", "-slt", str(path)], full_output=True)
    if listed.get("returncode") != 0:
        raise ValueError(f"Não foi possível listar archive: {listed}")
    blocks, current, archive_header = [], {}, {}
    for line in listed["stdout"].splitlines():
        if not line.strip():
            if current.get("Path") == str(path):
                archive_header = current
            elif current.get("Path"):
                blocks.append(current)
            current = {}
        elif " = " in line:
            key, value = line.split(" = ", 1)
            current[key] = value
    if current.get("Path") == str(path):
        archive_header = current
    elif current.get("Path"):
        blocks.append(current)
    members = []
    for block in blocks:
        name = block.get("Path", "").replace("\\", "/")
        rel = PurePosixPath(name)
        if not name or rel.is_absolute() or ".." in rel.parts or re.match(r"^[A-Za-z]:", name):
            raise ValueError(f"Membro archive inseguro: {name}")
        if "L" in block.get("Attributes", "") or block.get('Symbolic Link') or block.get('Hard Link'):
            raise ValueError(f"Link simbólico recusado no archive: {name}")
        try:
            packed_size = int(block.get("Packed Size", "0"))
        except ValueError:
            packed_size = None
        members.append({"path": name, "directory": block.get("Folder") == "+" or block.get('Attributes','').strip().startswith('D'),
                        "size_bytes": int(block.get("Size", "0")),
                        "packed_size_bytes": packed_size,
                        "crc32": block.get("CRC", "").lower(),
                        "encrypted": block.get("Encrypted", "-") == "+"})
    reader_type = archive_header.get("Type", "")
    return {"validator": "7z_test_and_list", "status": "ok",
            "archive_format": archive_format, "reader_type": reader_type,
            "archive_signature_detection": detected, "members": members,
            "content_classification": classify_archive_contents([str(path)], members)}


def file_integrity(path: Path) -> dict:
    digest = hashlib.sha256()
    crc = 0
    size = 0
    with path.open("rb", buffering=0) as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
            crc = zlib.crc32(block, crc)
            size += len(block)
    return {"size_bytes": size, "sha256": digest.hexdigest(), "crc32": f"{crc & 0xffffffff:08x}"}


def hash_archive_member(path: Path, archive_format: str, member: dict) -> dict:
    """Stream one approved member to a hash; never write its payload to disk."""
    if member.get("directory") or member.get("encrypted"):
        raise ValueError("Membro candidato precisa ser arquivo e não criptografado")
    expected_crc = str(member.get("crc32", "")).casefold().zfill(8)
    if not re.fullmatch(r"[0-9a-f]{8}", expected_crc):
        raise ValueError(f"CRC listado ausente/inválido para o membro: {member.get('path')}")
    digest = hashlib.sha256()
    crc = 0
    size = 0

    def consume(stream) -> None:
        nonlocal crc, size
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
            crc = zlib.crc32(block, crc)
            size += len(block)

    if archive_format == "zip":
        with zipfile.ZipFile(path) as archive, archive.open(member["path"]) as stream:
            consume(stream)
    else:
        with tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                ["7z", "x", "-so", "-spd", str(path), member["path"]],
                stdout=subprocess.PIPE, stderr=stderr_file,
            )
            try:
                if process.stdout is None:
                    raise RuntimeError("7z stdout indisponível para hash do membro")
                consume(process.stdout)
                process.stdout.close()
                returncode = process.wait()
            except Exception:
                process.kill()
                process.wait()
                raise
            if returncode != 0:
                stderr_file.seek(0)
                error = stderr_file.read()[-4000:].decode("utf-8", "replace")
                raise RuntimeError(f"7z não conseguiu transmitir o membro ({returncode}): {error}")
    result = {"path": member["path"], "size_bytes": size, "sha256": digest.hexdigest(),
              "crc32": f"{crc & 0xffffffff:08x}", "listed_size_bytes": int(member.get("size_bytes", -1)),
              "listed_crc32": expected_crc}
    if size != result["listed_size_bytes"] or result["crc32"] != expected_crc:
        raise ValueError(f"Membro transmitido diverge de tamanho/CRC listado: {result}")
    return result


def host_runtime_format_evidence(system_id: str, extension: str, *, probe: bool = False,
                                 input_kind: str = "file") -> dict:
    if not HOST_PROFILE_PATH.is_file():
        return {"supported": False, "status": "host_profile_missing", "system_id": system_id}
    profile = json.loads(HOST_PROFILE_PATH.read_text(encoding="utf-8"))
    candidates = []
    for section in ("collections", "systems"):
        for name, row in profile.get(section, {}).items():
            if row.get("rom_subdir") == system_id:
                candidates.append({"section": section, "name": name, **row})
    if not candidates:
        return {"supported": False, "status": "no_host_system_profile", "system_id": system_id,
                "profile_path": str(HOST_PROFILE_PATH)}
    unique = {(row.get("adapter", ""), row.get("extensions", ""), row.get("status", ""),
               row.get("command", ""), tuple(row.get("launch_inputs", []))) for row in candidates}
    if len(unique) != 1:
        return {"supported": False, "status": "ambiguous_host_system_profile", "system_id": system_id,
                "candidates": [{"name": row.get("name"), "adapter": row.get("adapter"),
                                "extensions": row.get("extensions"), "status": row.get("status")} for row in candidates]}
    row = candidates[0]
    adapter = str(row.get("adapter", "")).casefold()
    accepted = {"." + value.strip().casefold().lstrip(".")
                for value in str(row.get("extensions", "")).split(",") if value.strip()}
    evidence = {"system_id": system_id, "collection": row.get("name"), "profile_section": row["section"],
                "profile_path": str(HOST_PROFILE_PATH), "adapter": adapter,
                "profile_status": row.get("status", ""), "accepted_extensions": sorted(accepted),
                "launch_inputs": row.get("launch_inputs", []), "requested_extension": extension.casefold(),
                "requested_input_kind": input_kind, "launchability_tested": False}
    if not adapter or adapter == "unavailable" or row.get("status") == "unavailable_on_host":
        return {**evidence, "supported": False, "status": "runtime_unavailable_on_host"}
    if input_kind == "game_directory":
        if extension or not row.get("accepts_directories") or "game_directory" not in row.get("launch_inputs", []):
            return {**evidence, "supported": False, "status": "game_directory_not_profiled"}
    elif input_kind == "zar":
        if extension.casefold() != ".zar" or "zar" not in row.get("launch_inputs", []):
            return {**evidence, "supported": False, "status": "zar_not_profiled"}
        if extension.casefold() not in accepted:
            return {**evidence, "supported": False, "status": "format_not_listed_by_host_profile"}
    elif input_kind != "file":
        return {**evidence, "supported": False, "status": "unsupported_input_kind"}
    elif not extension:
        if probe:
            return {**evidence, "supported": False, "status": "runtime_probe_requires_a_content_extension"}
        return {**evidence, "supported": True, "status": "runtime_profile_configured"}
    elif extension.casefold() not in accepted:
        return {**evidence, "supported": False, "status": "format_not_listed_by_host_profile"}
    if probe:
        probe_env = None
        if adapter in {"mame", "mame_named"}:
            executable = shutil.which(str(profile.get("mame_executable", "mame")))
            argv = [executable, "-version"] if executable else []
        elif adapter == "retroarch":
            app_id = str(profile.get("retroarch", {}).get("app_id", ""))
            argv = ["flatpak", "info", app_id] if app_id else []
        elif adapter == "flatpak":
            app_id = str(row.get("app_id", ""))
            argv = ["flatpak", "info", app_id] if app_id else []
        elif adapter == "command":
            configured = Path(os.path.expandvars(os.path.expanduser(str(row.get("command", "")))))
            if not configured.is_absolute():
                configured = PROJECT_ROOT / "frontend/RetroFE" / configured
            executable = configured.resolve()
            probe_args = row.get("probe_args", ["--help"])
            argv = ([str(executable), *probe_args]
                    if executable.is_file() and os.access(executable, os.X_OK)
                    and isinstance(probe_args, list) and all(isinstance(x, str) for x in probe_args)
                    else [])
            probe_env = host_runtime_probe_environment(row)
            evidence["runtime_probe_environment_keys"] = sorted((row.get("environment") or {}).keys())
        else:
            argv = []
        if not argv:
            return {**evidence, "supported": False, "status": "runtime_probe_not_configured"}
        checked = run_validator(argv, env=probe_env, timeout=30)
        evidence["runtime_probe"] = argv
        evidence["runtime_probe_returncode"] = checked.get("returncode")
        evidence["runtime_probe_output"] = checked.get("stdout", "")[:1000]
        if checked.get("timed_out"):
            return {**evidence, "supported": False, "status": "runtime_probe_timed_out"}
        if checked.get("returncode") != 0:
            return {**evidence, "supported": False, "status": "runtime_probe_failed"}
        if adapter == "retroarch" and row.get("core"):
            core_root = Path(str(profile.get("retroarch", {}).get("core_root", ""))).expanduser()
            core_path = core_root / str(row["core"])
            evidence["runtime_core_path"] = str(core_path)
            evidence["runtime_core_exists"] = core_path.is_file()
            if not evidence["runtime_core_exists"]:
                return {**evidence, "supported": False, "status": "runtime_core_unavailable"}
    return {**evidence, "supported": True,
            "status": "runtime_and_format_configured" if probe else "runtime_and_format_profiled"}


def validate_archive_extraction(action: dict, source: Path, destination: Path,
                                validation: dict) -> dict:
    classification = validation.get("content_classification", {})
    if classification.get("classification") != "single_rom_candidate":
        raise ValueError("Extração bloqueada: somente um membro classificado como single_rom_candidate é elegível; "
                         f"classificação observada={classification.get('classification', 'missing')}")
    members = validation.get("members", [])
    files = [member for member in members if not member.get("directory")]
    if len(files) != 1 or len(members) != 1 or any(member.get("encrypted") for member in members):
        raise ValueError("Extração bloqueada: exige exatamente um único membro, sem diretórios adicionais ou criptografia")
    member = files[0]
    member_path = PurePosixPath(str(member.get("path", "")))
    if len(member_path.parts) != 1:
        raise ValueError("Extração bloqueada: o único payload deve estar na raiz do archive")
    extension = member_path.suffix.casefold()
    if not extension:
        raise ValueError("Extração bloqueada: membro sem extensão de conteúdo")
    system_id = str(action.get("system_id", ""))
    system_root = PROJECT_ROOT / "roms" / system_id
    if not system_id or destination.parent.absolute() != system_root.absolute():
        raise ValueError(f"Extração bloqueada: destino precisa ser uma pasta-nome de jogo diretamente em {system_root}")
    runtime = host_runtime_format_evidence(system_id, extension, probe=True)
    if not runtime.get("supported"):
        raise ValueError("Extração bloqueada: runtime/format não estão confirmados no perfil host: "
                         f"{runtime.get('status')}")
    payload = hash_archive_member(source, validation["archive_format"], member)
    return {"content_classification": classification, "member": member["path"],
            "member_payload": payload, "canonical_destination_root": str(system_root),
            "runtime_compatibility": runtime, "source_preserved": True,
            "rollback": "romtool rollback remove a árvore extraída somente após correspondência exata de caminhos, tamanhos e SHA-256"}


def load_reviewed_archive_listing(operation_id: str, source: Path, precondition: dict) -> dict:
    """Load the already-reviewed archive listing; never rerun 7z test/list here."""
    source_dir = operation_dir(operation_id)
    manifest_path = source_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    details = manifest.get("details", {})
    members_path = Path(str(details.get("archive_members_file", "")))
    if not members_path.is_absolute():
        members_path = PROJECT_ROOT / members_path
    items = manifest.get("items", [])
    if (manifest.get("operation_id") != operation_id or manifest.get("kind") != "verify"
            or manifest.get("status") != "completed_verified" or not manifest.get("read_only")
            or int(details.get("integrity_failure_count", -1)) != 0
            or int(details.get("integrity_pass_count", -1)) != len(items)
            or not members_path.is_file() or not is_within(members_path, OPS_ROOT)):
        raise ValueError("Extração em árvore exige operação de archive concluída, integral e somente leitura")
    source_text = str(source.absolute())
    reviewed = [row for row in items if str(row.get("source", "")) == source_text]
    if (len(reviewed) != 1 or reviewed[0].get("status") != "integrity_verified"
            or not reviewed[0].get("source_unchanged")
            or reviewed[0].get("sha256") != precondition.get("sha256")
            or reviewed[0].get("size_bytes") != precondition.get("size_bytes")):
        raise ValueError("A fonte atual não corresponde ao caminho/hash/tamanho do inventário archive revisado")
    rows = []
    with members_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("sha256") == precondition.get("sha256") and source_text in row.get("source_paths", []):
                rows.append(row)
    if len(rows) != 1:
        raise ValueError("A listagem existente não tem exatamente um registro para a fonte e o SHA-256")
    archive_record = rows[0]
    archive_format = str(archive_record.get("detected_format", "")).casefold().replace(" ", "")
    if archive_format not in {"rar5", "rar4", "7z", "lha", "zip"}:
        raise ValueError(f"Formato real do archive não pode ser extraído com segurança: {archive_format}")
    members = archive_record.get("members", [])
    if (not isinstance(members, list) or len(members) != int(archive_record.get("member_count", -1))
            or int(archive_record.get("unsafe_member_count", -1)) != 0
            or int(archive_record.get("encrypted_member_count", -1)) != 0):
        raise ValueError("A listagem revisada contém links/caminhos inseguros, criptografia ou contagem inconsistente")
    normalized, seen = [], set()
    for member in members:
        name = str(member.get("path", "")).replace("\\", "/")
        rel = PurePosixPath(name)
        if (not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or rel.as_posix() != name or member.get("unsafe_path_or_link")
                or member.get("unsafe_member_or_link") or member.get("encrypted")):
            raise ValueError(f"Membro archive inseguro/criptografado na listagem revisada: {name!r}")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Membros duplicados ou incompatíveis entre sistemas de arquivos: {name}")
        seen.add(key)
        is_directory = bool(member.get("directory"))
        size = int(member.get("size_bytes", -1))
        if size < 0 or (is_directory and size != 0):
            raise ValueError(f"Tamanho de membro inválido: {name}")
        crc = str(member.get("crc32", "")).casefold()
        if not is_directory and not re.fullmatch(r"[0-9a-f]{8}", crc):
            raise ValueError(f"CRC ausente/inválido na listagem revisada: {name}")
        normalized.append({"path": name, "directory": is_directory, "size_bytes": size,
                           "packed_size_bytes": member.get("packed_size_bytes"), "crc32": crc,
                           "encrypted": False})
    file_count = sum(not row["directory"] for row in normalized)
    directory_count = sum(row["directory"] for row in normalized)
    unpacked = sum(row["size_bytes"] for row in normalized if not row["directory"])
    if (file_count != int(archive_record.get("file_count", -1))
            or directory_count != int(archive_record.get("directory_count", -1))
            or unpacked != int(archive_record.get("unpacked_bytes", -1))):
        raise ValueError("Contagem ou tamanho lógico diverge do resumo archive já registrado")
    return {"archive_format": archive_format, "members": normalized, "file_count": file_count,
            "directory_count": directory_count, "unpacked_bytes": unpacked,
            "review_operation_id": operation_id, "review_manifest": str(manifest_path),
            "review_manifest_sha256": sha256_file(manifest_path),
            "archive_members_file": str(members_path), "archive_members_sha256": sha256_file(members_path),
            "reviewed_source_sha256": precondition["sha256"]}


def validate_archive_tree_extraction(action: dict, source: Path, destination: Path,
                                     validation: dict) -> dict:
    if str(action.get("system_id", "")) != "ps5":
        raise ValueError("extract_tree exige perfil PS5 com identidade interna param.json confirmada")
    if action.get("classification") != "game":
        raise ValueError("Árvore PS5 só pode ser preparada quando classificada como jogo; mod/pacote permanece bloqueado")
    system_root = PROJECT_ROOT / "roms" / "ps5"
    if destination.parent.absolute() != system_root.absolute():
        raise ValueError(f"Destino precisa ser uma pasta de jogo diretamente em {system_root}")
    identity = action.get("payload_identity", {})
    required = ("display_name", "title_id", "content_id", "content_version", "payload_directory",
                "param_json_member", "eboot_member")
    if not isinstance(identity, dict) or any(not str(identity.get(key, "")).strip() for key in required):
        raise ValueError("payload_identity exige título, Title ID, Content ID, versão, param.json e eboot.bin")
    display_name = str(identity["display_name"]).strip()
    title_id = str(identity["title_id"]).strip().upper()
    content_id = str(identity["content_id"]).strip()
    content_version = str(identity["content_version"]).strip()
    payload_root = str(identity["payload_directory"])
    if (not display_name or any(char in display_name for char in "/\\\0")
            or not re.fullmatch(r"PPSA[0-9]{5}", title_id)
            or title_id not in content_id.upper()
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", content_version)):
        raise ValueError("Identidade PS5 precisa conter título seguro, Title ID PPSA, Content ID correspondente e versão tripla")
    output_format = str(action.get("output_format", "directory")).casefold()
    if output_format not in {"directory", "zar"}:
        raise ValueError(f"Formato de saída de árvore PS5 não suportado: {output_format}")
    expected_basenames = {f"{display_name} ({title_id})",
                          f"{display_name} ({title_id}) (v{content_version})"}
    valid_destination_name = (
        destination.name in expected_basenames if output_format == "directory"
        else destination.name in {f"{basename}.zar" for basename in expected_basenames}
    )
    if not valid_destination_name:
        raise ValueError("Basename de destino deve corresponder ao título e Title ID confirmados internamente")
    root_path = PurePosixPath(payload_root)
    if (root_path.is_absolute() or len(root_path.parts) != 1
            or root_path.parts[0] in {"", ".", ".."}
            or root_path.name.casefold() != f"{title_id}-app0".casefold()):
        raise ValueError("A árvore PS5 precisa ter uma única raiz <TitleID>-app0 confirmada na listagem")
    members = validation.get("members", [])
    paths = {member["path"] for member in members}
    root = payload_root
    if (root not in paths or str(identity["eboot_member"]) not in paths
            or str(identity["param_json_member"]) not in paths
            or str(identity["eboot_member"]) != f"{root}/eboot.bin"
            or str(identity["param_json_member"]) != f"{root}/sce_sys/param.json"):
        raise ValueError("A listagem revisada não contém a raiz app0, eboot.bin e param.json esperados")
    roots = {PurePosixPath(member["path"]).parts[0] for member in members}
    if roots != {root}:
        raise ValueError(f"Esperava uma única árvore de jogo {root}; encontrados: {sorted(roots)}")
    if output_format == "zar":
        folded_paths = {}
        for member in members:
            member_path = str(member.get("path", ""))
            try:
                member_path.encode("cp1252")
            except UnicodeEncodeError as error:
                raise ValueError(f"ZArchive não pode preservar fielmente o caminho fora de Windows-1252: {member_path}") from error
            folded = member_path.casefold()
            previous = folded_paths.get(folded)
            if previous is not None:
                raise ValueError(f"Colisão de caminhos sem distinção de maiúsculas no ZArchive: {previous} / {member_path}")
            folded_paths[folded] = member_path
    input_kind = "zar" if output_format == "zar" else "game_directory"
    extension = ".zar" if output_format == "zar" else ""
    runtime = host_runtime_format_evidence("ps5", extension, probe=True, input_kind=input_kind)
    if not runtime.get("supported"):
        raise ValueError(f"Emulador não declara entrada {input_kind} carregável: {runtime.get('status')}")
    zarchive_tool = None
    if output_format == "zar":
        if (ZARCHIVE_TOOL_PATH.is_symlink() or not ZARCHIVE_TOOL_PATH.is_file()
                or not os.access(ZARCHIVE_TOOL_PATH, os.X_OK)):
            raise FileNotFoundError(f"Ferramenta ZArchive portátil ausente/inválida: {ZARCHIVE_TOOL_PATH}")
        probe = run_validator([str(ZARCHIVE_TOOL_PATH)])
        if probe.get("returncode") != 0 or "zarchive.exe input_path" not in probe.get("stdout", ""):
            raise RuntimeError(f"Probe do ZArchive falhou: {probe}")
        zarchive_tool = {"path": str(ZARCHIVE_TOOL_PATH.relative_to(PROJECT_ROOT)),
                         "sha256": sha256_file(ZARCHIVE_TOOL_PATH), "probe_returncode": 0,
                         "zstd_version": "1.5.7 static", "source_tag": "Exzap/ZArchive v0.1.2",
                         "source_commit": "d2c717730092c7bf8cbb033b12fd4001b7c4d932"}
    needed = (int(validation["unpacked_bytes"]) * (3 if output_format == "zar" else 1)
              + int(validation.get("source_size_bytes", 0)) + 64 * 1024 * 1024)
    free = shutil.disk_usage(destination.parent if destination.parent.exists() else system_root.parent).free
    if free < needed:
        raise OSError(f"Espaço insuficiente para snapshot + árvore PS5: requerido={needed}, livre={free}")
    return {"content_classification": "PS5 application tree; not a single ROM member",
            "payload_identity": identity, "payload_directory": root,
            "archive_review_operation_id": validation["review_operation_id"],
            "archive_members_sha256": validation["archive_members_sha256"],
            "output_format": output_format, "zarchive_tool": zarchive_tool,
            "runtime_compatibility": runtime, "source_preserved": True,
            "rollback": ("romtool rollback remove o ZAR somente após confirmar SHA-256 do arquivo e validar a leitura integral contra os membros registrados"
                         if output_format == "zar" else
                         "romtool rollback remove a árvore somente após caminhos, tamanhos e SHA-256 dos membros coincidirem")}


def command_archive_classify(args) -> dict:
    """Create a new classification record from an existing completed archive listing."""
    source_operation_id = args.operation_id
    source_dir = operation_dir(source_operation_id)
    source_manifest_path = source_dir / "manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    details = source_manifest.get("details", {})
    members_path = Path(details.get("archive_members_file", source_dir / "archive-members.jsonl"))
    if not members_path.is_absolute():
        members_path = PROJECT_ROOT / members_path
    if (source_manifest.get("status") != "completed_verified"
            or not source_manifest.get("read_only")
            or not members_path.is_file()):
        raise ValueError("Classificação exige operação de archive concluída, somente leitura e com listagem salva")
    if int(details.get("integrity_pass_count", -1)) != len(source_manifest.get("items", [])):
        raise ValueError("A operação-fonte não comprova teste integral aprovado para todos os caminhos")

    by_sha: dict[str, list[dict]] = defaultdict(list)
    by_path: dict[str, dict] = {}
    for item in source_manifest.get("items", []):
        if item.get("status") != "integrity_verified" or not item.get("source_unchanged"):
            raise ValueError(f"Item de origem sem teste/estabilidade confirmados: {item.get('source')}")
        digest = str(item.get("sha256", ""))
        source_path = str(item.get("source", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not source_path:
            raise ValueError("Manifesto-fonte não contém SHA-256/caminho por archive")
        by_sha[digest].append(item)
        by_path[source_path] = item

    listing_by_sha = {}
    with members_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            digest = str(record.get("sha256", ""))
            if digest in listing_by_sha:
                raise ValueError(f"Listagem duplicou conteúdo SHA-256 na linha {line_number}: {digest}")
            if digest not in by_sha:
                raise ValueError(f"Listagem contém hash ausente da operação-fonte: {digest}")
            if int(record.get("listing_return_code", -1)) != 0:
                raise ValueError(f"Listagem não aprovada para conteúdo {digest}")
            listed_paths = sorted(record.get("source_paths", []), key=str.casefold)
            manifest_paths = sorted((item["source"] for item in by_sha[digest]), key=str.casefold)
            if listed_paths != manifest_paths:
                raise ValueError(f"Caminhos/hash da listagem divergem do manifesto-fonte: {digest}")
            if int(record.get("unsafe_member_count", -1)) != 0:
                raise ValueError(f"Conteúdo com membro inseguro não entra na classificação: {digest}")
            listing_by_sha[digest] = record
    if set(listing_by_sha) != set(by_sha):
        raise ValueError(f"Listagem incompleta: {len(listing_by_sha)} hashes únicos para {len(by_sha)} hashes do manifesto")

    rows = []
    classification_counts: dict[str, int] = defaultdict(int)
    for digest, record in sorted(listing_by_sha.items()):
        source_paths = sorted(record.get("source_paths", []), key=str.casefold)
        members = record.get("members", [])
        classification = classify_archive_contents(source_paths, members)
        source_items = by_sha[digest]
        systems = sorted({system for item in source_items for system in item.get("catalog_system_ids", [])})
        sizes = sorted({int(item.get("size_bytes", -1)) for item in source_items})
        if len(sizes) != 1 or sizes[0] < 0:
            raise ValueError(f"Tamanho externo incoerente para SHA-256 {digest}: {sizes}")
        runtime_evidence = [host_runtime_format_evidence(system, "", probe=False) for system in systems]
        if classification["classification"] == "single_rom_candidate":
            member_extension = Path(next(member["path"] for member in members if not member.get("directory"))).suffix.casefold()
            formats = [host_runtime_format_evidence(system, member_extension, probe=False) for system in systems]
            if not formats:
                formats = [{"supported": False, "status": "catalog_system_id_missing"}]
            runtime_evidence = [{"system_profile": base, "format": formatted}
                                for base, formatted in zip(runtime_evidence or [{}], formats)]
        classification_counts[classification["classification"]] += 1
        representative = source_paths[0] if source_paths else ""
        rows.append({
            "source_path": representative,
            "source_paths": json.dumps(source_paths, ensure_ascii=False),
            "source_operation_id": source_operation_id,
            "source_manifest": str(source_manifest_path.relative_to(PROJECT_ROOT)),
            "source_manifest_sha256": sha256_file(source_manifest_path),
            "members_listing": str(members_path.relative_to(PROJECT_ROOT)),
            "members_listing_sha256": sha256_file(members_path),
            "sha256": digest,
            "size_bytes": sizes[0],
            "archive_format": record.get("detected_format", "unknown"),
            "archive_test_status": "passed_in_source_operation",
            "archive_test_return_code": 0,
            "listing_status": "listed_in_source_operation",
            "member_count": int(record.get("member_count", len(members))),
            "file_count": int(record.get("file_count", sum(not member.get("directory") for member in members))),
            "directory_count": int(record.get("directory_count", sum(bool(member.get("directory")) for member in members))),
            "unpacked_bytes": int(record.get("unpacked_bytes", 0)),
            "encrypted_member_count": int(record.get("encrypted_member_count", 0)),
            "unsafe_member_count": int(record.get("unsafe_member_count", 0)),
            "catalog_system_ids": json.dumps(systems),
            "classification": classification["classification"],
            "classification_confidence": classification["confidence"],
            "classification_evidence": json.dumps(classification["evidence"], ensure_ascii=False),
            "member_path_evidence": json.dumps([member.get("path", "") for member in members[:20]], ensure_ascii=False),
            "member_extensions": json.dumps(record.get("extension_counts", {}), ensure_ascii=False),
            "runtime_format_evidence": json.dumps(runtime_evidence, ensure_ascii=False),
            "source_archives_opened": False,
            "source_hashes_recalculated": False,
            "status": "classified_from_saved_listing",
        })
    evidence = {
        "source_operation_id": source_operation_id,
        "source_operation_status": source_manifest["status"],
        "source_path_count": len(source_manifest.get("items", [])),
        "unique_sha256_count": len(by_sha),
        "duplicate_copy_count": sum(len(items) - 1 for items in by_sha.values()),
        "archive_tests_passed": int(details["integrity_pass_count"]),
        "archive_listing_path": str(members_path.relative_to(PROJECT_ROOT)),
        "archive_listing_sha256": sha256_file(members_path),
        "source_archives_opened": False,
        "source_hashes_recalculated": False,
        "classification_counts": dict(classification_counts),
        "classification_method": "member paths, member extensions, explicit source path markers, and saved archive metadata; no identity or runtime launch inference",
        "runtime_profile_path": str(HOST_PROFILE_PATH.relative_to(PROJECT_ROOT)),
    }
    return record_analysis("archive_classification", "Archive contents classified from saved listing", rows,
                           OUTPUT_ROOT / "analysis", evidence)


def write_manifest(manifest: dict) -> Path:
    path = operation_dir(manifest["operation_id"]) / "manifest.json"
    atomic_json(path, manifest)
    return path


def create_destination_parents(parent: Path, operation_id: str, item: dict) -> None:
    missing = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    created = []
    for directory in reversed(missing):
        directory.mkdir()
        created.append(str(directory))
        append_event(operation_id, "destination_directory_created", action="mkdir", status="completed",
                     destination=str(directory), details={"item_id": item["item_id"]})
    item["created_directories"] = list(dict.fromkeys([*item.get("created_directories", []), *created]))


def retrofe_processes_running() -> list[dict]:
    """Find the frontend by executable path without matching this command's text."""
    running = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return running
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            executable = os.readlink(process / "exe")
        except OSError:
            continue
        if Path(executable).name.casefold().startswith("retrofe"):
            running.append({"pid": int(process.name), "executable": executable})
    return running


def esde_processes_running() -> list[dict]:
    """Find ES-DE by executable/cmdline without matching this tool's command text."""
    running = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return running
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            executable = os.readlink(process / "exe")
            command = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        names = {Path(executable.removesuffix(" (deleted)")).name.casefold()}
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
            names.add(Path(os.fsdecode(argv[0])).name.casefold())
            if Path(executable).name == "AppImageLauncher" and len(argv) > 1:
                names.add(Path(os.fsdecode(argv[1])).name.casefold())
        except (OSError, ValueError):
            continue
        if any(name in {"es-de", "emulationstation-de", "emulationstation"}
               or (name.startswith("es-de_") and name.endswith(".appimage")) for name in names):
            running.append({"pid": int(process.name), "executable": executable})
    return running


def frontend_processes_running() -> dict[str, list[dict]]:
    return {"retrofe": retrofe_processes_running(), "esde": esde_processes_running()}


def validate_game_media_source(path: Path, *, allow_leaf_symlink: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(MEDIA_SOURCE_ROOT)
    except ValueError as exc:
        raise ValueError(f"Mídia de jogo fora das coleções RetroFE: {path}") from exc
    if len(relative.parts) < 3 or relative.parts[1].casefold() != "medium_artwork":
        raise ValueError(f"Origem não está em collections/<coleção>/medium_artwork/: {path}")
    if relative.parts[0].casefold() in {"main", "_common", "_setup"}:
        raise ValueError(f"Asset comum/modelo do RetroFE fora do escopo de mídia de jogo: {path}")
    if path.suffix.casefold() not in MEDIA_EXTS:
        raise ValueError(f"Extensão não suportada para mídia de jogo: {path}")
    reject_symlink_components(path, allow_leaf_symlink=allow_leaf_symlink)


def validate_game_media_destination(path: Path, *, system_id: str = "", media_type: str = "",
                                   require_matching_type: bool = True) -> None:
    absolute = Path(os.path.abspath(path))
    is_patch_metadata = media_type.casefold() == "patch"
    destination_root = METADATA_DEST_ROOT if is_patch_metadata else MEDIA_DEST_ROOT
    try:
        relative = absolute.relative_to(destination_root)
    except ValueError as exc:
        expected = "metadata/<sistema>/patch/" if is_patch_metadata else "media/<sistema>/<tipo>/"
        raise ValueError(f"Destino fora de {expected}: {path}") from exc
    if len(relative.parts) != 3:
        raise ValueError(f"Destino deve ter exatamente sistema/tipo/arquivo: {path}")
    target_system, target_type, filename = relative.parts
    if is_patch_metadata and target_type != "patch":
        raise ValueError(f"Notas de patch devem ficar em metadata/<sistema>/patch/: {path}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", target_system):
        raise ValueError(f"system_id não canônico no destino de mídia: {target_system}")
    if target_type.casefold() not in MEDIA_TYPES:
        raise ValueError(f"Tipo semântico não permitido para mídia: {target_type}")
    if not (PROJECT_ROOT / "roms" / target_system).is_dir():
        raise ValueError(f"system_id não existe em roms/: {target_system}")
    if require_matching_type and (target_system != system_id or target_type != media_type):
        raise ValueError(f"Destino não corresponde a system_id/media_type do plano: {path}")
    if not filename or filename in {".", ".."} or Path(filename).suffix.casefold() not in MEDIA_EXTS:
        raise ValueError(f"Nome/extensão inválido para mídia: {path}")
    reject_symlink_components(path)


def media_destination_parts(path: Path, media_type: str) -> tuple[str, str]:
    root = METADATA_DEST_ROOT if media_type.casefold() == "patch" else MEDIA_DEST_ROOT
    relative = Path(os.path.abspath(path)).relative_to(root)
    return relative.parts[0], relative.parts[1]


def media_title_key(value: str) -> str:
    """Normalize a media title while treating deterministic Alt suffixes as variants."""
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+\(Alt \d+\)$", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip().casefold()


def validate_media_operation_quarantine(path: Path, operation_id: str) -> None:
    quarantine_root = operation_dir(operation_id) / "quarantine"
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(quarantine_root)
    except ValueError as exc:
        raise ValueError(f"Backup reversível de mídia deve ficar dentro de {quarantine_root}: {path}") from exc
    if len(relative.parts) != 2 or not relative.parts[0] or not relative.parts[1]:
        raise ValueError(f"Caminho de quarentena de mídia inválido: {path}")
    reject_symlink_components(path)


def relative_media_symlink(source: Path, destination: Path) -> str:
    """Create and verify the relocatable frontend alias to a canonical media payload."""
    if os.path.lexists(source):
        raise FileExistsError(f"Caminho de alias de mídia já existe: {source}")
    target_text = os.path.relpath(destination, source.parent)
    os.symlink(target_text, source)
    if not source.is_symlink() or os.readlink(source) != target_text:
        raise RuntimeError(f"Alias relativo de mídia foi criado com alvo inesperado: {source}")
    if source.resolve(strict=True) != destination.resolve(strict=True):
        raise RuntimeError(f"Alias relativo de mídia não resolve ao payload canônico: {source}")
    return target_text


def media_link_matches(source: Path, destination: Path) -> bool:
    if not source.is_symlink():
        return False
    expected = os.path.relpath(destination, source.parent)
    try:
        return os.readlink(source) == expected and source.resolve(strict=True) == destination.resolve(strict=True)
    except OSError:
        return False


def media_item_alias(item: dict) -> Path:
    media = item.get("media", {})
    if item.get("action") == "media_repair_link":
        return Path(media["replaced_frontend_source"])
    return Path(item["source"])


def apply_media_action(item: dict, operation_id: str) -> None:
    """Centralize a game-media payload while retaining the old frontend path."""
    kind = item["action"]
    source, destination = Path(item["source"]), Path(item["destination"])
    alias = media_item_alias(item)
    media = item["media"]
    expected_hash = item["precondition"]["sha256"]
    quarantine = Path(item["quarantine_destination"]) if item.get("quarantine_destination") else None

    if retrofe_processes_running():
        raise RuntimeError("Feche o RetroFE antes de alterar caminhos de mídia consumidos pelo frontend")
    current_source = path_snapshot(source, include_hash=True)
    if current_source["kind"] != "file" or current_source["sha256"] != expected_hash:
        raise ValueError(f"Payload de mídia mudou desde o plano: {source}")

    if kind == "media_move_link":
        if os.path.lexists(destination) or source.is_symlink() or not source.is_file():
            raise FileExistsError(f"Conflito ao centralizar mídia: {source} -> {destination}")
        os.rename(source, destination)
        try:
            item["frontend_link_target"] = relative_media_symlink(source, destination)
        except Exception:
            if source.is_symlink():
                source.unlink()
            if destination.exists() and not os.path.lexists(source):
                os.rename(destination, source)
            raise
        item["transfer_method"] = "rename_payload_then_relative_frontend_symlink"
        item["source_preserved"] = "relative_symlink"
    elif kind == "media_rekey_link":
        if (source.is_symlink() or not source.is_file() or source == destination):
            raise FileExistsError(f"Conflito ao renomear payload canônico de mídia: {source} -> {destination}")
        if source.stat().st_dev != existing_path_device(destination.parent):
            raise ValueError(f"Renomeação canônica de mídia exige o mesmo dispositivo: {source} -> {destination}")
        if media.get("reuses_existing_payload") or media.get("reuses_planned_payload"):
            if quarantine is None or os.path.lexists(quarantine):
                raise FileExistsError(f"Quarentena reversível da mídia rekey ausente/ocupada: {quarantine}")
            canonical = path_snapshot(destination, include_hash=True)
            if canonical.get("kind") != "file" or canonical.get("sha256") != expected_hash:
                raise ValueError(f"Payload canônico reutilizado mudou: {destination}")
            os.rename(source, quarantine)
            try:
                item["frontend_link_target"] = relative_media_symlink(source, destination)
            except Exception:
                if source.is_symlink():
                    source.unlink()
                if quarantine.exists() and not os.path.lexists(source):
                    os.rename(quarantine, source)
                raise
            item["quarantine_after"] = path_snapshot(quarantine, include_hash=True)
            item["transfer_method"] = "duplicate_canonical_quarantined_and_old_path_preserved_as_relative_alias"
        else:
            if os.path.lexists(destination):
                raise FileExistsError(f"Destino de payload canônico já existe: {destination}")
            os.rename(source, destination)
            try:
                item["frontend_link_target"] = relative_media_symlink(source, destination)
            except Exception:
                if source.is_symlink():
                    source.unlink()
                if destination.exists() and not os.path.lexists(source):
                    os.rename(destination, source)
                raise
            item["transfer_method"] = "canonical_payload_renamed_with_relative_old_path_alias"
        item["source_preserved"] = "relative_symlink_alias_at_previous_canonical_path"
    elif kind == "media_alias_link":
        if quarantine is None or os.path.lexists(quarantine) or not os.path.lexists(source):
            raise FileExistsError(f"Quarentena ou origem de duplicata em conflito: {source}")
        canonical = path_snapshot(destination, include_hash=True)
        if canonical["kind"] != "file" or canonical["sha256"] != expected_hash:
            raise ValueError(f"Payload canônico de alias ausente ou divergente: {destination}")
        os.rename(source, quarantine)
        try:
            item["frontend_link_target"] = relative_media_symlink(source, destination)
        except Exception:
            if source.is_symlink():
                source.unlink()
            if quarantine.exists() and not os.path.lexists(source):
                os.rename(quarantine, source)
            raise
        item["quarantine_after"] = path_snapshot(quarantine, include_hash=True)
        item["transfer_method"] = "duplicate_quarantined_then_relative_frontend_symlink"
        item["source_preserved"] = "relative_symlink_and_reversible_quarantine"
    elif kind == "media_repair_link":
        canonical_created = bool(media.get("canonical_payload_created", media.get("canonical_payload", True)))
        if quarantine is None or alias.is_symlink() or not alias.is_file():
            raise FileExistsError(f"Conflito ao repor mídia defeituosa: {alias} -> {destination}")
        alias_before = path_snapshot(alias, include_hash=True)
        if (alias_before["kind"] != "file"
                or alias_before["sha256"] != media.get("replaced_frontend_sha256")):
            raise ValueError(f"Alias de mídia defeituoso mudou desde o plano: {alias}")
        temp = destination.with_name(f".{destination.name}.media-copy-{uuid.uuid4().hex[:8]}") if canonical_created else None
        try:
            if canonical_created:
                if os.path.lexists(destination):
                    raise FileExistsError(f"Destino canônico de reparo foi ocupado: {destination}")
                shutil.copy2(source, temp)
                staged = path_snapshot(temp, include_hash=True)
                if staged["sha256"] != expected_hash or staged["size_bytes"] != current_source["size_bytes"]:
                    raise RuntimeError(f"Cópia de reposição divergiu do backup validado: {source}")
                os.rename(temp, destination)
            else:
                canonical = path_snapshot(destination, include_hash=True)
                if canonical["kind"] != "file" or canonical["sha256"] != expected_hash:
                    raise ValueError(f"Payload canônico reaproveitado mudou: {destination}")
            os.rename(alias, quarantine)
            try:
                item["frontend_link_target"] = relative_media_symlink(alias, destination)
            except Exception:
                if alias.is_symlink():
                    alias.unlink()
                if quarantine.exists() and not os.path.lexists(alias):
                    os.rename(quarantine, alias)
                raise
        except Exception:
            if temp is not None and temp.exists():
                temp.unlink()
            if quarantine.exists() and not os.path.lexists(alias):
                os.rename(quarantine, alias)
            if (canonical_created and destination.exists() and destination.is_file()
                    and sha256_file(destination) == expected_hash):
                destination.unlink()
            raise
        item["alias_before"] = alias_before
        item["quarantine_after"] = path_snapshot(quarantine, include_hash=True)
        item["transfer_method"] = "verified_backup_copy_then_bad_source_quarantine_and_relative_symlink"
        item["source_preserved"] = "external_backup_retained; bad_frontend_file_quarantined"
    else:
        raise ValueError(f"Ação de mídia desconhecida: {kind}")

    item["after"] = path_snapshot(destination, include_hash=True)
    item["frontend_alias_after"] = path_snapshot(alias, include_hash=True, allow_leaf_symlink=True)
    if item["after"]["sha256"] != expected_hash or not media_link_matches(alias, destination):
        raise RuntimeError(f"Payload ou symlink de mídia divergiu após centralização: {alias} -> {destination}")
    append_event(operation_id, "game_media_centralized", action=kind, status="completed",
                 source=str(source), destination=str(destination), sha256_before=expected_hash,
                 sha256_after=item["after"]["sha256"], size_before=item["precondition"]["size_bytes"],
                 size_after=item["after"]["size_bytes"],
                 details={"frontend_alias": str(alias), "relative_link": item["frontend_link_target"],
                          "media_type": media.get("media_type", ""),
                          "quarantine": str(quarantine) if quarantine else ""})


def rollback_media_action(item: dict, operation_id: str) -> str:
    """Restore the original frontend file/link state without touching shared payloads."""
    if retrofe_processes_running():
        raise RuntimeError("Feche o RetroFE antes do rollback dos caminhos de mídia")
    kind = item["action"]
    source, destination = Path(item["source"]), Path(item["destination"])
    alias = media_item_alias(item)
    media = item["media"]
    expected_hash = item.get("after", {}).get("sha256") or item["precondition"]["sha256"]
    if not media_link_matches(alias, destination):
        raise ValueError(f"Rollback de mídia recusado; alias mudou: {alias}")
    canonical = path_snapshot(destination, include_hash=True)
    if canonical["kind"] != "file" or canonical["sha256"] != expected_hash:
        raise ValueError(f"Payload canônico mudou; rollback bloqueado: {destination}")

    if kind == "media_move_link":
        if source != alias or os.path.lexists(source) is False:
            raise ValueError(f"Caminho original de mídia foi alterado: {source}")
        source.unlink()
        try:
            os.rename(destination, source)
        except Exception:
            relative_media_symlink(source, destination)
            raise
        rollback = "canonical_payload_renamed_back_to_original_path"
    elif kind == "media_rekey_link":
        if source.is_symlink() is False or not media_link_matches(source, destination):
            raise ValueError(f"Rollback de renomeação de mídia recusado; alias antigo mudou: {source}")
        source.unlink()
        if media.get("reuses_existing_payload") or media.get("reuses_planned_payload"):
            quarantine = Path(item["quarantine_destination"])
            if not quarantine.is_file() or sha256_file(quarantine) != item["precondition"]["sha256"]:
                relative_media_symlink(source, destination)
                raise ValueError(f"Payload original preservado da duplicata ausente/divergente: {quarantine}")
            os.rename(quarantine, source)
            rollback = "duplicate_payload_restored_from_quarantine_and_old_alias_removed"
        else:
            try:
                os.rename(destination, source)
            except Exception:
                relative_media_symlink(source, destination)
                raise
            rollback = "canonical_payload_restored_to_previous_media_path"
    elif kind == "media_alias_link":
        quarantine = Path(item["quarantine_destination"])
        if source != alias or os.path.lexists(source) is False or not quarantine.is_file():
            raise ValueError(f"Duplicata/quarentena ausente para rollback: {quarantine}")
        if sha256_file(quarantine) != item["precondition"]["sha256"]:
            raise ValueError(f"Hash da duplicata em quarentena mudou: {quarantine}")
        source.unlink()
        try:
            os.rename(quarantine, source)
        except Exception:
            relative_media_symlink(source, destination)
            raise
        rollback = "duplicate_restored_from_verified_quarantine_shared_payload_kept"
    elif kind == "media_repair_link":
        quarantine = Path(item["quarantine_destination"])
        if not quarantine.is_file() or sha256_file(quarantine) != media.get("replaced_frontend_sha256"):
            raise ValueError(f"Original defeituoso em quarentena ausente ou divergente: {quarantine}")
        alias.unlink()
        try:
            os.rename(quarantine, alias)
        except Exception:
            relative_media_symlink(alias, destination)
            raise
        if media.get("canonical_payload"):
            destination.unlink()
        rollback = "bad_frontend_file_restored_and_replacement_payload_removed"
    else:
        raise ValueError(f"Ação de mídia desconhecida no rollback: {kind}")
    append_event(operation_id, "game_media_rollback_completed", action=kind, status="verified",
                 source=str(destination), destination=str(alias),
                 sha256_before=canonical["sha256"], sha256_after=item["precondition"]["sha256"],
                 size_before=canonical["size_bytes"], size_after=item["precondition"]["size_bytes"],
                 details={"rollback": rollback,
                          "quarantine": item.get("quarantine_destination", "")})
    return rollback


def configured_rom_extensions(system_id: str) -> set[str]:
    extensions: set[str] = set()
    catalog_paths = (
        PROJECT_ROOT / "frontend" / "RetroFE" / "Config" / "platform-catalog.json",
        PROJECT_ROOT / "frontend" / "RetroFE" / "Config" / "systems.json",
    )
    for config_path in catalog_paths:
        if not config_path.is_file():
            continue
        try:
            document = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = document.get("systems", [])
        if isinstance(records, dict):
            records = list(records.values())
        for record in records:
            if not isinstance(record, dict):
                continue
            if str(record.get("id", "")) != system_id and str(record.get("rom_subdir", "")) != system_id:
                continue
            raw = record.get("extensions", [])
            if isinstance(raw, str):
                values = re.split(r"[,\s]+", raw)
            elif isinstance(raw, list):
                values = raw
            else:
                values = []
            extensions.update(
                str(value).strip().casefold().lstrip(".")
                for value in values
                if str(value).strip()
            )
    return extensions


def validate_runtime_support_relocation(
    action: dict, source: Path, destination: Path, operation_id: str,
) -> dict[str, object]:
    """Allow only reversible, hash-checked support moves out of a platform ROM tree."""
    system_id = str(action.get("system_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", system_id):
        raise ValueError(f"ID de plataforma inválido para mover suporte: {system_id!r}")
    platform_root = PROJECT_ROOT / "roms" / system_id
    if not is_within(source, platform_root) or os.path.abspath(source) == os.path.abspath(platform_root):
        raise ValueError(f"Suporte deve estar abaixo de roms/<system_id>, sem mover a raiz da plataforma: {source}")
    if source.is_symlink() or not os.path.lexists(source):
        raise ValueError(f"Suporte ausente ou simbólico não pode ser movido por esta exceção: {source}")

    mode = str(action.get("action", ""))
    classification = str(action.get("classification", ""))
    if mode == "move":
        if classification != "support":
            raise ValueError("Mover suporte para tools exige classification=support")
        runtime_root = PROJECT_ROOT / 'emulator'
        if not is_within(destination, runtime_root) or os.path.abspath(destination) == os.path.abspath(runtime_root):
            raise ValueError(f"Suporte de ROM só pode ser movido para emulator/<linux|windows|data>/<runtime>/: {destination}")
        runtime_parts = Path(os.path.relpath(destination, runtime_root)).parts
        if (len(runtime_parts) < 3 or runtime_parts[0] not in ('linux', 'windows', 'data')
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", runtime_parts[1])):
            raise ValueError(f"O destino deve ficar dentro de um diretório de runtime nomeado: {destination}")
        destination_root = runtime_root
    elif mode == "quarantine":
        if classification != "duplicate":
            raise ValueError("Quarentena de suporte exige classification=duplicate")
        quarantine_root = operation_dir(operation_id) / "quarantine"
        if not is_within(destination, quarantine_root) or os.path.abspath(destination) == os.path.abspath(quarantine_root):
            raise ValueError(f"Duplicata de suporte só pode ir para a quarentena da operação: {destination}")
        duplicate_of = Path(str(action.get("duplicate_of", "")))
        if not duplicate_of.is_file() or duplicate_of.is_symlink():
            raise ValueError(f"A duplicata precisa apontar para um arquivo regular de comparação: {duplicate_of}")
        source_hash = sha256_file(source) if source.is_file() else ""
        peer_hash = sha256_file(duplicate_of)
        if not source_hash or source_hash != peer_hash:
            raise ValueError(f"Duplicata de suporte não tem hash idêntico ao arquivo de comparação: {source} -> {duplicate_of}")
        destination_root = quarantine_root
    else:
        raise ValueError("runtime_support_relocation requer ação move ou quarantine")

    game_extensions = configured_rom_extensions(system_id)
    if not game_extensions:
        raise ValueError(f"Não há extensões de ROM configuradas para validar {system_id}; suporte preservado")
    if source.is_file():
        files = [source]
    elif source.is_dir():
        files = []
        for current, directories, names in os.walk(source, followlinks=False):
            current_path = Path(current)
            if any((current_path / name).is_symlink() for name in directories + names):
                raise ValueError(f"Árvore de suporte com symlink recusada: {current_path}")
            files.extend(current_path / name for name in names)
    else:
        raise ValueError(f"Suporte não é arquivo/pasta regular: {source}")
    game_files = [path for path in files if path.suffix.casefold().lstrip(".") in game_extensions]
    if game_files:
        raise ValueError(f"A origem contém extensão de ROM configurada; não pode ser movida como suporte: {game_files[:5]}")

    result: dict[str, object] = {
        "scope": "runtime_support_relocation",
        "platform_source_root": str(platform_root),
        "destination_root": str(destination_root),
        "game_extensions_checked": sorted(game_extensions),
        "source_file_count": len(files),
        "source_contains_configured_rom_extensions": False,
    }
    if mode == "quarantine":
        result["duplicate_of"] = str(duplicate_of)
        result["duplicate_sha256"] = peer_hash
    return result


def validate_platform_metadata_relocation(
    action: dict, source: Path, destination: Path,
) -> dict[str, object]:
    """Allow only known platform metadata to leave roms/<system_id>."""
    system_id = str(action.get("system_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9._-]+", system_id):
        raise ValueError(f"ID de plataforma inválido para mover metadados: {system_id!r}")
    if action.get("action") != "move" or action.get("classification") != "support":
        raise ValueError("platform_metadata_relocation aceita somente move com classification=support")
    platform_root = PROJECT_ROOT / "roms" / system_id
    if not is_within(source, platform_root) or os.path.abspath(source) == os.path.abspath(platform_root):
        raise ValueError(f"Metadado deve estar abaixo de roms/<system_id>: {source}")
    if source.is_symlink() or not os.path.lexists(source):
        raise ValueError(f"Metadado ausente ou simbólico recusado: {source}")

    relative = Path(os.path.relpath(source, platform_root))
    metadata_root = PROJECT_ROOT / "metadata" / system_id / "platform"
    expected_destination = metadata_root / relative
    if os.path.abspath(destination) != os.path.abspath(expected_destination):
        raise ValueError(
            f"Metadado deve manter caminho relativo em metadata/<system_id>/platform/: "
            f"{source} -> {destination}; esperado {expected_destination}"
        )

    allowed_names = {".directory", "metadata.txt", "systeminfo.txt", "rpcs3_guide.txt", "vita3k_guide.txt"}
    if source.is_file():
        files = [source]
        if source.name.casefold() not in allowed_names:
            raise ValueError(f"Nome não pertence à allowlist de metadados de plataforma: {source}")
    elif source.is_dir():
        if system_id != "ps4" or relative.as_posix() != "shortcuts":
            raise ValueError(f"Somente o diretório de metadados PS4 shortcuts pode ser movido como árvore: {source}")
        files = []
        for current, directories, names in os.walk(source, followlinks=False):
            current_path = Path(current)
            if directories:
                raise ValueError(f"Diretório de metadados PS4 shortcuts não pode ter subpastas: {current_path}")
            if any((current_path / name).is_symlink() for name in directories + names):
                raise ValueError(f"Diretório de metadados contém symlink: {current_path}")
            for name in names:
                path = current_path / name
                if name.casefold() not in allowed_names | {"readme.txt"}:
                    raise ValueError(f"Arquivo inesperado na árvore de atalhos PS4: {path}")
                files.append(path)
        if not files:
            raise ValueError(f"Árvore de metadados vazia recusada: {source}")
    else:
        raise ValueError(f"Metadado precisa ser arquivo ou árvore regular: {source}")

    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Metadado não é arquivo regular: {path}")
        if path.suffix.casefold() not in {"", ".txt"}:
            raise ValueError(f"Extensão inesperada para metadado de plataforma: {path}")

    reference_mode = str(action.get("reference_check", "")).casefold()
    catalog_reference = ""
    if relative.as_posix() == "systeminfo.txt":
        catalog_path = PROJECT_ROOT / "frontend" / "RetroFE" / "Config" / "platform-catalog.json"
        if catalog_path.is_file():
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            record = next((entry for entry in catalog.get("systems", [])
                           if entry.get("id") == system_id), None)
            if record and record.get("source_metadata") == "systeminfo.txt":
                catalog_reference = str(catalog_path.relative_to(PROJECT_ROOT))
        if catalog_reference:
            if reference_mode != "coordinated_metadata_source":
                raise ValueError(f"systeminfo.txt referenciado pelo catálogo; registrar rematerialização: {source}")
            expected_reference = (
                f"{catalog_reference}#systems[{system_id}].source_metadata",
                "frontend/RetroFE/Tools/materializers/materialize-platform-catalog.py#metadata_source_for",
            )
            recorded_references = set(str(value) for value in action.get("frontend_references", []))
            if not set(expected_reference).issubset(recorded_references):
                raise ValueError(f"Referências de materialização não foram registradas para {source}")
        elif reference_mode not in {"checked_none", "not_applicable"}:
            raise ValueError(f"Referência de systeminfo não confirmada para {source}")
    elif reference_mode not in {"checked_none", "not_applicable"}:
        raise ValueError(f"Referência não é aplicável a este metadado: {source}")

    return {
        "scope": "platform_metadata_relocation",
        "system_id": system_id,
        "platform_source_root": str(platform_root),
        "metadata_destination_root": str(metadata_root),
        "source_relative_path": relative.as_posix(),
        "file_count": len(files),
        "catalog_source_metadata_reference": catalog_reference,
        "catalog_rematerialization_required": bool(catalog_reference),
    }


def validate_launcher_bundle_relocation(
    action: dict, source: Path, destination: Path,
) -> dict[str, object]:
    """Move the known Pegasus emulator-launcher bundle from roms/ into tools/."""
    expected_source = PROJECT_ROOT / "roms" / "emulators"
    expected_destination = PROJECT_ROOT / "tools" / "roms" / "launchers"
    if action.get("action") != "move" or action.get("classification") != "support":
        raise ValueError("launcher_bundle_relocation aceita somente move com classification=support")
    if str(action.get("system_id", "")) != "emulators":
        raise ValueError("Bundle de launchers exige system_id=emulators")
    if os.path.abspath(source) != os.path.abspath(expected_source):
        raise ValueError(f"Bundle de launchers fora da origem conhecida: {source}")
    if os.path.abspath(destination) != os.path.abspath(expected_destination):
        raise ValueError(f"Bundle de launchers deve ir para tools/roms/launchers: {destination}")
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"Origem de launchers não é uma pasta regular: {source}")

    allowed_names = {"metadata.txt", "systeminfo.txt"}
    files: list[Path] = []
    for current, directories, names in os.walk(source, followlinks=False):
        current_path = Path(current)
        if directories:
            raise ValueError(f"Bundle de launchers não pode conter subpastas: {current_path}")
        if any((current_path / name).is_symlink() for name in names):
            raise ValueError(f"Bundle de launchers contém symlink: {current_path}")
        for name in names:
            path = current_path / name
            if name.casefold() not in allowed_names and path.suffix.casefold() != ".sh":
                raise ValueError(f"Arquivo fora do bundle esperado de launchers: {path}")
            if not path.is_file():
                raise ValueError(f"Arquivo de launcher não é regular: {path}")
            files.append(path)
    if not files or not any(path.suffix.casefold() == ".sh" for path in files):
        raise ValueError(f"Bundle sem scripts de launcher: {source}")
    if str(action.get("reference_check", "")).casefold() != "coordinated_launcher_path":
        raise ValueError("Bundle de launchers requer referência Pegasus coordenada")
    game_dirs_path = PROJECT_ROOT / "frontend" / "pegasus-frontend" / "game_dirs.txt"
    expected_reference = f"{game_dirs_path.relative_to(PROJECT_ROOT)}#launcher_collection"
    recorded_references = set(str(value) for value in action.get("frontend_references", []))
    if expected_reference not in recorded_references:
        raise ValueError("Referência do diretório de launchers não foi registrada")
    if not game_dirs_path.is_file() or str(destination) not in game_dirs_path.read_text(encoding="utf-8").splitlines():
        raise ValueError("Pegasus game_dirs.txt não aponta para o destino dos launchers")
    return {
        "scope": "launcher_bundle_relocation",
        "source_root": str(expected_source),
        "destination_root": str(expected_destination),
        "file_count": len(files),
        "launcher_count": sum(path.suffix.casefold() == ".sh" for path in files),
        "metadata_files": sorted(path.name for path in files if path.name.casefold() in allowed_names),
        "pegasus_game_dirs_reference": expected_reference,
    }


def validate_bios_broken_link_quarantine(
    action: dict, source: Path, destination: Path, operation_id: str,
) -> dict[str, object]:
    """Allow quarantining only the stale roms/bios/media leaf link."""
    expected_source = PROJECT_ROOT / "roms" / "bios" / "media"
    expected_destination = operation_dir(operation_id) / "quarantine" / "roms" / "bios" / "media"
    canonical_bios_root = PROJECT_ROOT / "bios"
    if action.get("action") != "quarantine" or action.get("classification") != "support":
        raise ValueError("bios_broken_link_quarantine aceita apenas quarantine com classification=support")
    if os.path.abspath(source) != os.path.abspath(expected_source):
        raise ValueError(f"Só o link BIOS legado conhecido pode usar esta exceção: {source}")
    if os.path.abspath(destination) != os.path.abspath(expected_destination):
        raise ValueError(f"O link BIOS deve ir à quarentena desta operação: {destination}")
    if not source.is_symlink() or source.exists():
        raise ValueError(f"A origem precisa continuar sendo o symlink folha quebrado esperado: {source}")
    if any(parent.is_symlink() for parent in (source.parent, source.parent.parent)):
        raise ValueError(f"Componente pai simbólico impede a quarentena BIOS: {source}")
    target_literal = os.readlink(source)
    target = Path(target_literal)
    if (
        not target.is_absolute()
        or target.name.casefold() != "bios"
        or target.parent.name.casefold() != "downloaded_media"
        or "tools" not in {part.casefold() for part in target.parts}
        or os.path.lexists(target)
    ):
        raise ValueError(f"Alvo do link não corresponde ao padrão legado inexistente: {target_literal}")
    if canonical_bios_root.is_symlink() or not canonical_bios_root.is_dir():
        raise ValueError(f"A raiz BIOS canônica precisa existir em {canonical_bios_root}")
    if str(canonical_bios_root) not in str(action.get("dependency_check", "")):
        raise ValueError("O plano deve registrar a raiz BIOS canônica independente de roms/bios")
    if str(action.get("reference_check", "")).casefold() != "checked_none":
        raise ValueError("Quarentena do link BIOS exige busca de referências concluída sem resultados")
    return {
        "scope": "bios_broken_link_quarantine",
        "source": str(expected_source),
        "destination": str(expected_destination),
        "symlink_target_literal": target_literal,
        "target_exists": False,
        "canonical_bios_root": str(canonical_bios_root),
    }


SWITCH_LEGACY_FIRMWARE_FILES = {"Firmware.22.5.0.zip", "noload.txt", "ProdKeys.NET-v22.5.0/prod.keys",
                                 "ProdKeys.NET-v22.5.0/title.keys"}


def validate_switch_legacy_firmware_relocation(action: dict, source: Path, destination: Path) -> dict[str, object]:
    """F12: move the unreferenced legacy system/switch/Firmware files, byte-identical, into bios/switch/.
    Key files are hashed for identity only; their content is never parsed or logged."""
    legacy_root = PROJECT_ROOT / "system" / "switch" / "Firmware"
    bios_root = PROJECT_ROOT / "bios" / "switch"
    try:
        relative = Path(os.path.abspath(source)).relative_to(legacy_root).as_posix()
    except ValueError as exc:
        raise ValueError("Relocação Switch limitada a system/switch/Firmware") from exc
    if relative not in SWITCH_LEGACY_FIRMWARE_FILES or os.path.abspath(destination) != os.path.abspath(bios_root / relative):
        raise ValueError(f"Arquivo/destino fora da lista revisada do firmware Switch: {source} -> {destination}")
    if action.get("action") != "move" or action.get("classification") != "support" or action.get("system_id") != "switch":
        raise ValueError("Relocação Switch aceita somente move/support do sistema switch")
    if source.is_symlink() or not source.is_file() or os.path.lexists(destination):
        raise ValueError("Origem ausente/simbólica ou destino já ocupado")
    reject_symlink_components(source)
    precondition = action.get("precondition") or {}
    expected_sha256 = str(action.get("expected_sha256") or precondition.get("sha256", "")).casefold()
    expected_size = int(action.get("expected_size", precondition.get("size_bytes", -1)))
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or sha256_file(source) != expected_sha256:
        raise ValueError("SHA-256 diverge do plano revisado")
    if expected_size >= 0 and expected_size != source.stat().st_size:  # apply revalidates by full SHA-256
        raise ValueError("Tamanho diverge do plano revisado")
    if str(action.get("reference_check", "")).casefold() != "checked_none":
        raise ValueError("Relocação exige busca de referências concluída sem resultados")
    integrity = "hash_only_restricted_content_not_read"
    if source.suffix.casefold() == ".zip":
        with zipfile.ZipFile(source) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP de firmware com CRC inválido")
            integrity = f"zip_crc_ok_members_{len(archive.infolist())}"
    return {"scope": "bios_firmware_relocation", "system_id": "switch", "source_relative": f"system/switch/Firmware/{relative}",
            "destination_relative": f"bios/switch/{relative}", "source_sha256": expected_sha256,
            "source_size_bytes": source.stat().st_size, "integrity": integrity,
            "rollback": "romtool rollback devolve o arquivo após validar caminho, tamanho e SHA-256"}


def validate_bios_firmware_relocation(action: dict, source: Path, destination: Path) -> dict[str, object]:
    """Allow only the confirmed PS3 update PUP to move from roms/ into bios/."""
    if str(action.get("system_id", "")) == "switch":
        return validate_switch_legacy_firmware_relocation(action, source, destination)
    expected_source = PROJECT_ROOT / "roms" / "ps3" / "pkg" / "PS3UPDAT.pup"
    expected_destination = PROJECT_ROOT / "bios" / "ps3" / "PS3UPDAT.pup"
    if (action.get("action") != "move" or action.get("classification") != "support"
            or str(action.get("system_id", "")) != "ps3"):
        raise ValueError("bios_firmware_relocation aceita somente move/support do firmware PS3")
    if (os.path.abspath(source) != os.path.abspath(expected_source)
            or os.path.abspath(destination) != os.path.abspath(expected_destination)):
        raise ValueError(f"Relocação BIOS limitada ao PUP PS3 confirmado: {source} -> {destination}")
    if (source.is_symlink() or not source.is_file() or source.stat().st_size != 206197916
            or destination.is_symlink()):
        raise ValueError("PUP PS3 ausente, simbólico ou com tamanho diferente da evidência confirmada")
    reject_symlink_components(source)
    reject_symlink_components(destination.parent)
    expected_sha256 = str(action.get("expected_sha256", "")).casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("O plano precisa registrar o SHA-256 integral confirmado do PUP")
    actual_sha256 = sha256_file(source)
    if actual_sha256 != expected_sha256:
        raise ValueError("SHA-256 do PUP PS3 diverge do firmware que foi validado/instalado")
    with source.open("rb") as stream:
        signature = stream.read(5)
    if signature != b"SCEUF":
        raise ValueError("Assinatura interna não corresponde a um arquivo de atualização PS3 SCEUF")
    if str(action.get("reference_check", "")).casefold() != "checked_none":
        raise ValueError("Relocação BIOS exige busca de referências concluída sem resultados")
    return {
        "scope": "bios_firmware_relocation",
        "system_id": "ps3",
        "source": str(expected_source),
        "destination": str(expected_destination),
        "source_size_bytes": source.stat().st_size,
        "source_sha256": actual_sha256,
        "internal_signature_hex": signature.hex(),
        "canonical_bios_root": str(PROJECT_ROOT / "bios"),
        "firmware_runtime_installation_record": "environment-20261026T062023-640fe20b",
        "rollback": "romtool rollback move o mesmo PUP de volta somente após validar caminhos, tamanho e SHA-256",
    }


def plan_actions(plan_path: Path) -> dict:
    request = json.loads(plan_path.read_text(encoding="utf-8"))
    actions = request.get("actions", [])
    if not actions:
        raise ValueError("Plano sem actions")
    operation_id = request.get("operation_id") or f"change-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    prepared = []
    policy = load_policy()
    allowed_roots = [Path(x) for x in policy.get("allowed_roots", [])]
    protected_components = {x.casefold() for x in policy.get("protected_path_components", [])}
    protected_extensions = {x.casefold() for x in policy.get("protected_extensions", [])}
    protected_names = {x.casefold() for x in policy.get("protected_names", [])}
    allowed_classifications = {
        "game", "update", "dlc", "mod", "media", "support",
        "corrupt_candidate", "duplicate", "container", "set",
    }
    planned_media_targets = {}
    planned_media_payloads = set()
    for index, action in enumerate(actions, start=1):
        kind = action.get("action")
        if kind not in {"move", "rename", "quarantine", "extract", "extract_tree", "pack_tree",
                        "steam_move_link", "steam_save_move_link", *MEDIA_ACTIONS}:
            raise ValueError(f"Ação não suportada/destrutiva: {kind}")
        source, destination = Path(action["source"]), Path(action["destination"])
        is_steam_move = kind == "steam_move_link"
        is_steam_save_move = kind == "steam_save_move_link"
        is_media = kind in MEDIA_ACTIONS
        active_media_quarantine = kind == "quarantine" and is_within(source, MEDIA_SOURCE_ROOT)
        support_relocation = None
        platform_metadata_relocation = None
        launcher_bundle_relocation = None
        bios_broken_link_quarantine = None
        bios_firmware_relocation = None
        special_support_scopes = sum(bool(action.get(key)) for key in (
            "runtime_support_relocation", "platform_metadata_relocation", "launcher_bundle_relocation",
            "bios_broken_link_quarantine", "bios_firmware_relocation",
        ))
        if special_support_scopes > 1:
            raise ValueError(f"Ação não pode combinar escopos especiais de suporte: {source}")
        if action.get("runtime_support_relocation"):
            support_relocation = validate_runtime_support_relocation(action, source, destination, operation_id)
        if action.get("platform_metadata_relocation"):
            platform_metadata_relocation = validate_platform_metadata_relocation(action, source, destination)
        if action.get("launcher_bundle_relocation"):
            launcher_bundle_relocation = validate_launcher_bundle_relocation(action, source, destination)
        if action.get("bios_broken_link_quarantine"):
            bios_broken_link_quarantine = validate_bios_broken_link_quarantine(
                action, source, destination, operation_id
            )
        if action.get("bios_firmware_relocation"):
            bios_firmware_relocation = validate_bios_firmware_relocation(action, source, destination)
        source_is_symlink = source.is_symlink()
        if active_media_quarantine:
            if source_is_symlink:
                if not policy.get("quarantine_leaf_symlink", False) or source.exists():
                    raise ValueError(f"Só symlink folha obsoleto e quebrado pode ir à quarentena: {source}")
                validate_game_media_source(source, allow_leaf_symlink=True)
            else:
                validate_game_media_source(source)
            status = str(action.get("validation_result", "")).casefold()
            allowed_invalid = ("symlink_review", "invalid") if source_is_symlink else ("empty", "invalid", "type_mismatch")
            if action.get("classification") != "corrupt_candidate" or not any(marker in status for marker in allowed_invalid):
                raise ValueError(f"Mídia ativa só pode ser isolada após validação vazia/inválida: {source}")
            validate_media_operation_quarantine(destination, operation_id)
            ensure_allowed(source)
            ensure_allowed(destination)
        elif is_media:
            if source_is_symlink:
                raise ValueError(f"Origem de mídia já é symlink/conflito: {source}")
            if kind in {"media_move_link", "media_alias_link"}:
                validate_game_media_source(source)
                ensure_allowed(source)
            elif kind == "media_rekey_link":
                media_type = str(action.get("media_type", ""))
                validate_game_media_destination(
                    source, system_id=str(action.get("system_id", "")), media_type=media_type,
                    require_matching_type=True)
                ensure_allowed(source)
            else:
                if is_within(source, MEDIA_SOURCE_ROOT) or source.name != Path(action["alias_source"]).name:
                    raise ValueError(f"Reposição deve vir de uma fonte de revisão com o mesmo nome, não da origem ativa: {source}")
                if "medium_artwork" not in {part.casefold() for part in source.parts}:
                    raise ValueError(f"Fonte de reposição não está em uma árvore medium_artwork de revisão: {source}")
                ensure_allowed(source)
                reject_symlink_components(source)
                if source.suffix.casefold() not in MEDIA_EXTS:
                    raise ValueError(f"Fonte de reposição não é um formato de mídia aceito: {source}")
                alias_source = Path(action["alias_source"])
                validate_game_media_source(alias_source)
                alias_empty_validation = "empty" in str(action.get("alias_validation_result", "")).casefold()
                if (alias_source.is_symlink() or not alias_source.is_file()
                        or (alias_source.stat().st_size <= 0 and not alias_empty_validation)):
                    raise ValueError(f"Origem defeituosa a substituir mudou ou não tem validação de vazio: {alias_source}")
            media_type = str(action.get("media_type", ""))
            if media_type.casefold() not in MEDIA_TYPES:
                raise ValueError(f"Tipo semântico inválido no plano: {media_type}")
            validate_game_media_destination(
                destination, system_id=str(action.get("system_id", "")), media_type=media_type,
                require_matching_type=True)
            game_title_hint = str(action.get("game_title_hint", source.stem))
            if media_title_key(destination.stem) != media_title_key(game_title_hint):
                raise ValueError(f"Basename canônico de mídia não corresponde ao jogo associado: {destination} != {game_title_hint}")
            ensure_allowed(destination)
        elif is_steam_move or is_steam_save_move:
            if source_is_symlink:
                raise ValueError(f"A origem Steam já é um link ou está em conflito: {source}")
            if is_steam_move:
                steam_centralizer.validate_steam_action(action, PROJECT_ROOT)
            else:
                steam_centralizer.validate_steam_save_action(action, PROJECT_ROOT)
            reject_symlink_components(source)
            ensure_allowed(destination)
        else:
            ensure_allowed(source, allow_leaf_symlink=source_is_symlink)
            ensure_allowed(destination)
        if source_is_symlink and (kind != "quarantine" or not policy.get("quarantine_leaf_symlink", False)):
            raise ValueError(f"Symlink só pode ser isolado por quarentena reversível habilitada: {source}")
        if (not is_media and not active_media_quarantine and
                any(is_within(source, Path(p)) or is_within(destination, Path(p)) for p in policy.get("protected_roots", []))):
            raise ValueError(f"Caminho protegido; operação recusada: {source} -> {destination}")
        if not (is_steam_move or is_steam_save_move or is_media or active_media_quarantine):
            special_support = (support_relocation is not None or platform_metadata_relocation is not None
                               or launcher_bundle_relocation is not None
                               or bios_broken_link_quarantine is not None
                               or bios_firmware_relocation is not None)
            if ((source.suffix.casefold() in protected_extensions or source.name.casefold() in protected_names)
                    and not special_support):
                raise ValueError(f"Tipo/nome de suporte protegido; não mover como ROM: {source}")
            if (not special_support
                    and any(part.casefold() in protected_components for part in source.parts + destination.parts)):
                raise ValueError(f"Caminho de suporte protegido: {source} -> {destination}")
            if any(os.path.abspath(source) == os.path.abspath(root) for root in allowed_roots):
                raise ValueError(f"Não é permitido operar sobre uma raiz inteira: {source}")
        if action.get("frontend_references") and not (
                is_media or platform_metadata_relocation or launcher_bundle_relocation):
            raise ValueError(f"Item com referência de frontend recusado: {source}")
        required_evidence = ("system_id", "item_identity", "classification", "reason",
                             "reference_check", "validation_result", "dependency_check")
        missing_evidence = [key for key in required_evidence if not str(action.get(key, "")).strip()]
        if missing_evidence:
            raise ValueError(f"Plano sem evidência obrigatória ({', '.join(missing_evidence)}): {source}")
        if action["classification"] not in allowed_classifications:
            raise ValueError(f"Classificação não reconhecida: {action['classification']}")
        allowed_reference_checks = ({"preserved_by_relative_symlink"} if is_media
                                    else {"checked_none", "not_applicable"})
        if platform_metadata_relocation and platform_metadata_relocation["catalog_rematerialization_required"]:
            allowed_reference_checks.add("coordinated_metadata_source")
        if launcher_bundle_relocation:
            allowed_reference_checks.add("coordinated_launcher_path")
        if action["reference_check"].casefold() not in allowed_reference_checks:
            raise ValueError(f"Referência de frontend não liberada para movimento: {source}")
        if active_media_quarantine and action["reference_check"].casefold() != "checked_none":
            raise ValueError(f"Quarentena de mídia inválida requer busca de referência documentada: {source}")
        if not os.path.lexists(source):
            raise FileNotFoundError(f"Origem ausente: {source}")
        if is_media:
            if not source.is_file() or source.stat().st_size <= 0:
                raise ValueError(f"Arquivo de mídia regular e não vazio é obrigatório: {source}")
            if kind == "media_move_link" and os.path.lexists(destination):
                raise FileExistsError(f"Destino de payload já existe: {source} -> {destination}")
            if kind == "media_rekey_link" and os.path.lexists(destination) and not action.get("reuses_existing_payload"):
                raise FileExistsError(f"Destino de payload canônico já existe: {source} -> {destination}")
        elif os.path.lexists(destination):
            raise FileExistsError(f"Destino já existe: {source} -> {destination}")
        if is_steam_move:
            pre = steam_tree_snapshot(source, allow_save_symlinks=True)
        elif is_steam_save_move:
            pre = steam_payload_snapshot(source)
        else:
            pre = path_snapshot(source, include_hash=True, allow_leaf_symlink=source_is_symlink)
        media_details = None
        alias_precondition = None
        quarantine_destination = None
        if is_media:
            if pre.get("kind") != "file" or pre.get("size_bytes", 0) <= 0:
                raise ValueError(f"Mídia precisa ser um arquivo regular não vazio: {source}")
            if kind == "media_move_link":
                if pre["device"] != existing_path_device(destination.parent):
                    raise ValueError(f"Symlink do frontend exige rename no mesmo dispositivo: {source} -> {destination}")
                payload_key = (str(action.get("system_id", "")), media_type.casefold(), pre["sha256"],
                               media_title_key(str(action.get("game_title_hint", source.stem))))
                if payload_key in planned_media_payloads:
                    raise ValueError(f"Payload SHA-256 do mesmo título já tem destino no plano; use media_alias_link: {source}")
                if str(destination) in planned_media_targets:
                    raise ValueError(f"Destino canônico aparece mais de uma vez no plano: {destination}")
                media_details = {"media_type": action["media_type"], "payload_sha256": pre["sha256"],
                                 "link_mode": "relative", "canonical_payload": True,
                                 "source_collection": source.relative_to(MEDIA_SOURCE_ROOT).parts[0],
                                 "game_title_hint": action.get("game_title_hint", source.stem),
                                 "identity_evidence": action.get("identity_evidence", ""),
                                 "media_type_evidence": action.get("media_type_evidence", ""),
                                 "media_validation": action.get("media_validation", ""),
                                 "frontend_references": action.get("frontend_references", [])}
                planned_media_payloads.add(payload_key)
                planned_media_targets[str(destination)] = (pre["sha256"], payload_key[3])
            elif kind == "media_rekey_link":
                if pre["device"] != existing_path_device(destination.parent):
                    raise ValueError(f"Renomeação canônica de mídia exige o mesmo dispositivo: {source} -> {destination}")
                payload_key = (str(action.get("system_id", "")), media_type.casefold(), pre["sha256"],
                               media_title_key(str(action.get("game_title_hint", destination.stem))))
                reuses_existing = bool(action.get("reuses_existing_payload"))
                reuses_planned = bool(action.get("reuses_planned_payload"))
                planned_target = planned_media_targets.get(str(destination))
                if reuses_planned and planned_target != (pre["sha256"], payload_key[3]):
                    raise ValueError(f"Payload planejado para rekey não confere em hash/título: {destination}")
                if payload_key in planned_media_payloads and not (reuses_existing or reuses_planned):
                    raise ValueError(f"Payload canônico rekey do mesmo título já aparece no plano: {source}")
                if str(destination) in planned_media_targets and not (reuses_existing or reuses_planned):
                    raise ValueError(f"Destino canônico aparece mais de uma vez no plano: {destination}")
                if reuses_existing:
                    if not destination.is_file() or destination.is_symlink() or sha256_file(destination) != pre["sha256"]:
                        raise ValueError(f"Payload canônico existente não tem hash idêntico para rekey: {destination}")
                if reuses_existing or reuses_planned:
                    item_id = f"{operation_id}-{index:04d}"
                    quarantine_destination = operation_dir(operation_id) / "quarantine" / item_id / source.name
                    validate_media_operation_quarantine(quarantine_destination, operation_id)
                    if os.path.lexists(quarantine_destination):
                        raise FileExistsError(f"Quarentena reversível já contém destino: {quarantine_destination}")
                    if pre["device"] != existing_path_device(quarantine_destination.parent):
                        raise ValueError(f"Quarentena rekey exige o mesmo dispositivo: {source}")
                media_details = {"media_type": action["media_type"], "payload_sha256": pre["sha256"],
                                 "link_mode": "relative", "canonical_payload": True,
                                 "reuses_existing_payload": reuses_existing,
                                 "reuses_planned_payload": reuses_planned,
                                 "source_collection": "existing canonical media payload",
                                 "previous_canonical_path": str(source),
                                 "game_title_hint": action.get("game_title_hint", destination.stem),
                                 "identity_evidence": action.get("identity_evidence", ""),
                                 "media_type_evidence": action.get("media_type_evidence", ""),
                                 "media_validation": action.get("media_validation", ""),
                                 "frontend_references": action.get("frontend_references", [])}
                if not (reuses_existing or reuses_planned):
                    planned_media_payloads.add(payload_key)
                    planned_media_targets[str(destination)] = (pre["sha256"], payload_key[3])
                elif reuses_existing:
                    planned_media_payloads.add(payload_key)
                    planned_media_targets[str(destination)] = (pre["sha256"], payload_key[3])
            elif kind == "media_alias_link":
                target_identity = ("", "")
                if os.path.lexists(destination):
                    if destination.is_symlink() or not destination.is_file():
                        raise ValueError(f"Destino de alias precisa ser payload regular, não symlink: {destination}")
                    target_identity = (sha256_file(destination), media_title_key(destination.stem))
                else:
                    target_identity = planned_media_targets.get(str(destination), ("", ""))
                expected_identity = (pre["sha256"], media_title_key(str(action.get("game_title_hint", source.stem))))
                if target_identity != expected_identity:
                    raise ValueError(f"Alias de mídia sem payload canônico anterior de mesmo hash e título: {source} -> {destination}")
                item_id = f"{operation_id}-{index:04d}"
                quarantine_destination = operation_dir(operation_id) / "quarantine" / item_id / source.name
                validate_media_operation_quarantine(quarantine_destination, operation_id)
                if os.path.lexists(quarantine_destination):
                    raise FileExistsError(f"Quarentena reversível já contém destino: {quarantine_destination}")
                if pre["device"] != existing_path_device(quarantine_destination.parent):
                    raise ValueError(f"Quarentena reversível de alias exige o mesmo dispositivo: {source}")
                media_details = {"media_type": action["media_type"], "payload_sha256": pre["sha256"],
                                 "link_mode": "relative", "canonical_payload": False,
                                 "canonical_destination": str(destination),
                                 "canonical_system_id": media_destination_parts(destination, media_type)[0],
                                 "canonical_media_type": media_destination_parts(destination, media_type)[1],
                                 "source_collection": source.relative_to(MEDIA_SOURCE_ROOT).parts[0],
                                 "duplicate_preserved_in_operation_quarantine": True,
                                 "game_title_hint": action.get("game_title_hint", source.stem),
                                 "identity_evidence": action.get("identity_evidence", ""),
                                 "media_type_evidence": action.get("media_type_evidence", ""),
                                 "media_validation": action.get("media_validation", ""),
                                 "frontend_references": action.get("frontend_references", [])}
            else:
                alias_source = Path(action["alias_source"])
                alias_precondition = path_snapshot(alias_source, include_hash=True)
                expected_alias_hash = str(action.get("alias_sha256", ""))
                if not expected_alias_hash or alias_precondition["sha256"] != expected_alias_hash:
                    raise ValueError(f"Hash da mídia defeituosa mudou desde a revisão: {alias_source}")
                if pre["device"] != existing_path_device(destination.parent):
                    destination_parent = destination.parent
                    while not destination_parent.exists():
                        destination_parent = destination_parent.parent
                    required = pre["size_bytes"] + max(pre["size_bytes"] // 20, 8 * 1024 * 1024)
                    free = shutil.disk_usage(destination_parent).free
                    if free < required:
                        raise OSError(f"Espaço insuficiente para copiar mídia válida: requerido={required}, livre={free}")
                existing_destination_hash = ""
                if os.path.lexists(destination):
                    if destination.is_symlink() or not destination.is_file():
                        raise ValueError(f"Destino de reparo precisa ser payload regular: {destination}")
                    existing_destination_hash = sha256_file(destination)
                planned_destination = planned_media_targets.get(str(destination), ("", ""))
                planned_destination_hash = planned_destination[0]
                reusable_hash = existing_destination_hash or planned_destination_hash
                if reusable_hash and reusable_hash != pre["sha256"]:
                    raise ValueError(f"Payload canônico existente/plano diverge do backup validado: {destination}")
                expected_title = media_title_key(str(action.get("game_title_hint", source.stem)))
                if existing_destination_hash and media_title_key(destination.stem) != expected_title:
                    raise ValueError(f"Payload existente de mesmo hash tem outro título associado: {destination}")
                if planned_destination_hash and planned_destination[1] != expected_title:
                    raise ValueError(f"Payload planejado de mesmo hash tem outro título associado: {destination}")
                canonical_payload_created = not bool(reusable_hash)
                item_id = f"{operation_id}-{index:04d}"
                quarantine_destination = operation_dir(operation_id) / "quarantine" / item_id / alias_source.name
                validate_media_operation_quarantine(quarantine_destination, operation_id)
                if os.path.lexists(quarantine_destination):
                    raise FileExistsError(f"Quarentena reversível já contém destino: {quarantine_destination}")
                if alias_precondition["device"] != existing_path_device(quarantine_destination.parent):
                    raise ValueError(f"Quarentena da origem defeituosa exige o mesmo dispositivo: {alias_source}")
                media_details = {"media_type": action["media_type"], "payload_sha256": pre["sha256"],
                                 "link_mode": "relative", "canonical_payload": canonical_payload_created,
                                 "canonical_payload_created": canonical_payload_created,
                                 "canonical_payload_reused": not canonical_payload_created,
                                 "source_collection": alias_source.relative_to(MEDIA_SOURCE_ROOT).parts[0],
                                 "replacement_source": str(source), "replacement_source_preserved": True,
                                 "replaced_frontend_source": str(alias_source),
                                 "replaced_frontend_sha256": alias_precondition["sha256"],
                                 "replaced_frontend_validation": action.get("alias_validation_result", ""),
                                 "game_title_hint": action.get("game_title_hint", alias_source.stem),
                                 "identity_evidence": action.get("identity_evidence", ""),
                                 "media_type_evidence": action.get("media_type_evidence", ""),
                                 "media_validation": action.get("media_validation", ""),
                                 "frontend_references": action.get("frontend_references", [])}
                if canonical_payload_created:
                    planned_media_targets[str(destination)] = (pre["sha256"], expected_title)
                    payload_key = (str(action.get("system_id", "")), media_type.casefold(), pre["sha256"], expected_title)
                    if payload_key in planned_media_payloads:
                        raise ValueError(f"Reposição duplica outro payload canônico do mesmo sistema/tipo/título: {destination}")
                    planned_media_payloads.add(payload_key)
        if source_is_symlink and pre.get("kind") != "symlink":
            raise ValueError(f"A origem deixou de ser o link simbólico planejado: {source}")
        if source_is_symlink and pre["device"] != existing_path_device(destination.parent):
            raise ValueError(f"Quarentena de symlink exige o mesmo dispositivo para não seguir/copiar o alvo: {source} -> {destination}")
        if active_media_quarantine and pre["device"] != existing_path_device(destination.parent):
            raise ValueError(f"Quarentena de mídia ativa requer rename no mesmo dispositivo: {source} -> {destination}")
        if is_steam_move:
            if pre.get("file_count", 0) == 0:
                raise ValueError(f"Pasta Steam vazia recusada: {source}")
            if not os.access(source.parent, os.W_OK):
                raise PermissionError(f"Steam library não permite criar o symlink de compatibilidade: {source.parent}")
            if not steam_centralizer.same_rename_domain(source, destination.parent):
                existing_parent = destination.parent
                while not existing_parent.exists():
                    existing_parent = existing_parent.parent
                required = pre["size_bytes"] + max(pre["size_bytes"] // 20, 512 * 1024 * 1024)
                free = shutil.disk_usage(existing_parent).free
                if free < required:
                    raise OSError(f"Espaço insuficiente para cópia Steam cross-device: requerido={required}, livre={free}")
        if is_steam_save_move:
            steam_centralizer.probe_symlink_support(source.parent)
            if pre["kind"] == "directory" and pre.get("file_count", 0) == 0:
                raise ValueError(f"Diretório de save Steam sem payload recusado: {source}")
            if not steam_centralizer.same_rename_domain(source, destination.parent):
                existing_parent = destination.parent
                while not existing_parent.exists():
                    existing_parent = existing_parent.parent
                required = pre["size_bytes"] + max(pre["size_bytes"] // 20, 32 * 1024 * 1024)
                free = shutil.disk_usage(existing_parent).free
                if free < required:
                    raise OSError(f"Espaço insuficiente para cópia de save Steam: requerido={required}, livre={free}")
        validation = archive_members(source) if kind == "extract" else {}
        if kind == "extract":
            if pre.get("kind") != "file":
                raise ValueError(f"Extração exige archive como arquivo regular: {source}")
            if action.get("classification") != "game":
                raise ValueError("Extração bloqueada: o item precisa estar classificado como game; mod/pacote/update/DLC não são ROM simples")
            validation["source_sha256"] = pre["sha256"]
            validation["source_size_bytes"] = pre["size_bytes"]
            validation["extraction_compatibility"] = validate_archive_extraction(
                action, source, destination, validation
            )
            if sha256_file(source) != pre["sha256"]:
                raise ValueError(f"Archive mudou durante teste/listagem/hash do payload: {source}")
        elif kind in {"extract_tree", "pack_tree"}:
            if pre.get("kind") != "file":
                raise ValueError(f"Extração de árvore exige archive como arquivo regular: {source}")
            if action.get("classification") != "game":
                raise ValueError("Extração em árvore bloqueada: item precisa estar classificado como game")
            review_id = str(action.get("archive_review_operation_id", ""))
            if not review_id:
                raise ValueError("extract_tree exige archive_review_operation_id; não repetiremos teste/listagem")
            review = load_reviewed_archive_listing(review_id, source, pre)
            validation.update(review)
            validation["source_sha256"] = pre["sha256"]
            validation["source_size_bytes"] = pre["size_bytes"]
            validation["listing_source"] = "completed_read_only_archive_review"
            validation["extraction_compatibility"] = validate_archive_tree_extraction(
                action, source, destination, validation
            )
            if kind == "pack_tree" and validation["extraction_compatibility"].get("output_format") != "zar":
                raise ValueError("pack_tree exige output_format=zar")
            if sha256_file(source) != pre["sha256"]:
                raise ValueError(f"Archive mudou enquanto a listagem revisada era vinculada ao plano: {source}")
        prepared_item = {"item_id": f"{operation_id}-{index:04d}", "action": kind,
                         "source": str(source), "destination": str(destination),
                         "system_id": action["system_id"], "item_identity": action["item_identity"],
                         "classification": action["classification"], "reason": action["reason"],
                         "reference_check": action["reference_check"],
                         "validation_result": action["validation_result"],
                         "dependency_check": action["dependency_check"], "precondition": pre,
                         "validation": validation, "status": "prepared"}
        if is_media:
            prepared_item["media"] = media_details
            if quarantine_destination:
                prepared_item["quarantine_destination"] = str(quarantine_destination)
            if alias_precondition:
                prepared_item["alias_precondition"] = alias_precondition
        if is_steam_move:
            prepared_item["steam"] = dict(action["steam"])
            prepared_item["link_target"] = str(destination)
        if is_steam_save_move:
            prepared_item["steam"] = dict(action["steam"])
            prepared_item["link_target"] = str(destination)
        if support_relocation is not None:
            prepared_item["runtime_support_relocation"] = support_relocation
        if platform_metadata_relocation is not None:
            prepared_item["platform_metadata_relocation"] = platform_metadata_relocation
            if action.get("frontend_references"):
                prepared_item["frontend_references"] = list(action["frontend_references"])
        if launcher_bundle_relocation is not None:
            prepared_item["launcher_bundle_relocation"] = launcher_bundle_relocation
            if action.get("frontend_references"):
                prepared_item["frontend_references"] = list(action["frontend_references"])
        if bios_broken_link_quarantine is not None:
            prepared_item["bios_broken_link_quarantine"] = bios_broken_link_quarantine
        if bios_firmware_relocation is not None:
            prepared_item["bios_firmware_relocation"] = bios_firmware_relocation
        prepared.append(prepared_item)
    opdir = operation_dir(operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": "1.0", "operation_id": operation_id, "kind": "change",
                "title": request.get("title", "ROM library change"), "created_at": now(),
                "status": "prepared", "items": prepared, "source_plan": str(plan_path),
                "analysis": request.get("analysis", {}),
                "policy": {"no_overwrite": True, "permanent_delete": False, "frontend_config_edit": False}}
    manifest_path = write_manifest(manifest)
    append_event(operation_id, "plan_created", action="plan", status="prepared",
                 destination=str(manifest_path), details={"item_count": len(prepared), "title": manifest["title"]})
    # A plan that re-plans every pending source of an older change plan replaces it.
    sources = {item["source"] for item in prepared}
    def replanned(old):
        pending = {item.get("source") for item in old.get("items", []) if item.get("status") in PENDING_PLAN_STATES}
        return bool(pending) and pending <= sources
    manifest["superseded_plans"] = supersede_prior_plans(manifest, whole=replanned)
    if manifest["superseded_plans"]:
        write_manifest(manifest)
    rebuild_history_index()
    return manifest


def safe_extract(source: Path, destination: Path, validation: dict,
                 operation_id: str, item_id: str, *, reuse_reviewed_listing: bool = False,
                 tree_mode: bool = False, pack_tree: bool = False) -> list[dict]:
    action_name = "pack_tree" if pack_tree else ("extract_tree" if tree_mode else "extract")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.extract-{uuid.uuid4().hex}")
    temp.mkdir(mode=0o700)
    archive_stage = None
    roundtrip = None
    snapshot_path = None
    snapshot_cleanup_logged = False
    append_event(operation_id, "extraction_staging_created", action=action_name, status="running",
                 source=str(source), destination=str(temp), details={"item_id": item_id})
    try:
        archive_format = validation.get("archive_format")
        expected_source_sha = str(validation.get("source_sha256", "")).casefold()
        expected_source_size = int(validation.get("source_size_bytes", -1))
        expected_members = validation.get("members", [])
        if not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha) or expected_source_size < 0:
            raise ValueError("Extração bloqueada: o plano não tem SHA-256/tamanho válidos para o archive-fonte")
        if not isinstance(expected_members, list) or not expected_members:
            raise ValueError("Extração bloqueada: o plano não tem uma listagem de membros validada")

        # Extract a private, read-only snapshot whose bytes match the reviewed plan.
        # This keeps a later replacement/change of the original path out of 7z's
        # input and makes the extractor's member list refer to the bytes it reads.
        with tempfile.TemporaryDirectory(prefix=f".{destination.name}.archive-",
                                         dir=destination.parent) as snapshot_directory:
            snapshot_path = Path(snapshot_directory) / "source.archive"
            free_bytes = shutil.disk_usage(snapshot_directory).free
            if free_bytes < expected_source_size:
                raise OSError(f"Espaço insuficiente para snapshot do archive: requerido={expected_source_size}, livre={free_bytes}")
            with source.open("rb", buffering=0) as src, snapshot_path.open("xb") as dst:
                shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            snapshot_size = snapshot_path.stat().st_size
            snapshot_sha = sha256_file(snapshot_path)
            if snapshot_size != expected_source_size or snapshot_sha != expected_source_sha:
                raise RuntimeError("Snapshot do archive diverge do tamanho/SHA-256 preparado; extração cancelada")
            snapshot_path.chmod(stat.S_IRUSR)
            append_event(operation_id, "archive_snapshot_verified", action=action_name, status="completed",
                         source=str(source), destination=str(snapshot_path),
                         sha256_before=expected_source_sha, sha256_after=snapshot_sha,
                         size_before=expected_source_size, size_after=snapshot_size,
                         details={"item_id": item_id, "private_directory": True, "read_only": True})

            if reuse_reviewed_listing:
                if validation.get("listing_source") != "completed_read_only_archive_review":
                    raise ValueError("Reutilização da listagem só é válida para uma operação archive review verificada")
                snapshot_validation = {"archive_format": archive_format, "members": expected_members}
            else:
                snapshot_validation = archive_members(snapshot_path)
            member_fields = ("path", "directory", "size_bytes", "packed_size_bytes", "crc32", "encrypted")

            def member_signature(rows):
                return [tuple(row.get(key) for key in member_fields) for row in rows]
            if (snapshot_validation.get("archive_format") != archive_format
                    or member_signature(snapshot_validation.get("members", []))
                    != member_signature(expected_members)):
                raise RuntimeError("Assinatura ou listagem do snapshot diverge do plano validado")
            # Recheck immediately before invoking the external extractor.
            if sha256_file(snapshot_path) != expected_source_sha:
                raise RuntimeError("Snapshot do archive mudou após a listagem; extração cancelada")

            if archive_format == "zip":
                with zipfile.ZipFile(snapshot_path) as archive:
                    for info in archive.infolist():
                        name = info.filename.replace("\\", "/")
                        rel = PurePosixPath(name)
                        if rel.is_absolute() or ".." in rel.parts or stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                            raise ValueError(f"Membro ZIP inseguro: {name}")
                        target = temp.joinpath(*[part for part in rel.parts if part not in ("", ".")])
                        if not is_within(target, temp):
                            raise ValueError(f"Destino fora do staging: {name}")
                        if info.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(info) as src, target.open("wb") as dst:
                                shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
            elif archive_format in {"7z", "rar4", "rar5", "lha"}:
                result = run_validator(["7z", "x", "-y", f"-o{temp}", str(snapshot_path)])
                if result.get("returncode") != 0:
                    raise RuntimeError(f"Falha de extração 7z: {result}")
            else:
                raise ValueError(f"Formato detectado não é extraível: {archive_format}")

            if sha256_file(snapshot_path) != expected_source_sha:
                raise RuntimeError("Snapshot do archive mudou durante a extração; staging será descartado")
        extracted = []
        extracted_directories = set()
        for path in sorted(temp.rglob("*"), key=lambda p: p.as_posix().casefold()):
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"Extração criou link simbólico: {path}")
            if stat.S_ISDIR(st.st_mode):
                extracted_directories.add(path.relative_to(temp).as_posix())
            elif stat.S_ISREG(st.st_mode):
                integrity = file_integrity(path)
                extracted.append({"relative_path": path.relative_to(temp).as_posix(), **integrity})
            else:
                raise ValueError(f"Extração criou arquivo especial não suportado: {path}")
        expected = {m["path"] for m in validation["members"] if not m.get("directory")}
        actual = {x["relative_path"] for x in extracted}
        if expected != actual:
            raise RuntimeError(f"Conteúdo extraído diverge; ausentes={sorted(expected-actual)[:20]}; extras={sorted(actual-expected)[:20]}")
        if tree_mode:
            expected_directories = {m["path"] for m in validation["members"] if m.get("directory")}
            if expected_directories != extracted_directories:
                raise RuntimeError("Diretórios extraídos divergem da lista revisada: "
                                   f"ausentes={sorted(expected_directories-extracted_directories)[:20]}; "
                                   f"extras={sorted(extracted_directories-expected_directories)[:20]}")
        expected_rows = {m["path"]: m for m in validation["members"] if not m.get("directory")}
        for row in extracted:
            listed = expected_rows[row["relative_path"]]
            listed_crc = str(listed.get("crc32", "")).casefold().zfill(8)
            if (row["size_bytes"] != int(listed.get("size_bytes", -1))
                    or not re.fullmatch(r"[0-9a-f]{8}", listed_crc)
                    or row["crc32"] != listed_crc):
                raise RuntimeError(f"Membro extraído diverge de tamanho/CRC listado: {row['relative_path']}")
        if tree_mode:
            identity = validation.get("extraction_compatibility", {}).get("payload_identity", {})
            param_path = temp.joinpath(*PurePosixPath(identity.get("param_json_member", "")).parts)
            eboot_path = temp.joinpath(*PurePosixPath(identity.get("eboot_member", "")).parts)
            try:
                params = json.loads(param_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"param.json extraído não é JSON legível: {error}") from error
            if not isinstance(params, dict):
                raise RuntimeError("param.json extraído não é um objeto JSON")
            normalized_params = {str(key).casefold(): str(value) for key, value in params.items()}
            for key, expected_value in (("titleid", identity["title_id"]),
                                        ("contentid", identity["content_id"]),
                                        ("contentversion", identity["content_version"])):
                if normalized_params.get(key, "").casefold() != str(expected_value).casefold():
                    raise RuntimeError(f"param.json não confirma {key}={expected_value}")
            if not eboot_path.is_file() or eboot_path.is_symlink() or eboot_path.stat().st_size <= 0:
                raise RuntimeError("eboot.bin ausente/vazio após extração")
            validation["extracted_identity_check"] = {
                "title_id": identity["title_id"], "content_id": identity["content_id"],
                "content_version": identity["content_version"],
                "eboot": {"relative_path": identity["eboot_member"], **file_integrity(eboot_path)},
                "param_json": {"relative_path": identity["param_json_member"], **file_integrity(param_path)},
            }
            # Kyty opens a game directory only when eboot.bin is at its root.
            # Keep the archive's full member paths in the validation evidence,
            # then flatten its single confirmed app0 wrapper for the runtime.
            payload_root_name = str(identity.get("payload_directory", ""))
            payload_root = temp / payload_root_name
            if (not payload_root_name or not payload_root.is_dir()
                    or {child.name for child in temp.iterdir()} != {payload_root_name}):
                raise RuntimeError("Árvore PS5 não contém exatamente a raiz app0 confirmada")
            for child in sorted(payload_root.iterdir(), key=lambda path: path.name.casefold()):
                target = temp / child.name
                if os.path.lexists(target):
                    raise RuntimeError(f"Colisão ao preparar diretório PS5 carregável: {target.name}")
                os.rename(child, target)
            payload_root.rmdir()
            extracted = [
                {**row, "relative_path": row["relative_path"][len(payload_root_name) + 1:]}
                for row in extracted
            ]
            extracted_directories = sorted(
                name[len(payload_root_name) + 1:]
                for name in extracted_directories
                if name.startswith(payload_root_name + "/")
            )
            validation["extracted_directories"] = extracted_directories
            output_format = validation.get("extraction_compatibility", {}).get("output_format", "directory")
            runtime_payload = {
                "entrypoint": "eboot.bin", "source_archive_wrapper_removed": payload_root_name,
                "file_count": len(extracted), "directory_count": len(extracted_directories),
            }
            if output_format == "zar":
                validation["runtime_game_archive"] = {"path": str(destination), **runtime_payload}
            else:
                validation["runtime_game_directory"] = {"path": str(destination), **runtime_payload}
        else:
            payload_evidence = validation.get("extraction_compatibility", {}).get("member_payload", {})
            if len(extracted) != 1 or not payload_evidence:
                raise RuntimeError("Extração sem hash de payload único preparado no plano")
            if (extracted[0]["sha256"] != payload_evidence.get("sha256")
                    or extracted[0]["size_bytes"] != payload_evidence.get("size_bytes")):
                raise RuntimeError("SHA-256/tamanho do payload extraído diverge da leitura planejada por stream")
        if not expected_source_sha or sha256_file(source) != expected_source_sha:
            raise RuntimeError("SHA-256 do archive-fonte mudou durante a extração; staging será descartado")
        if snapshot_path is not None and not snapshot_path.exists():
            append_event(operation_id, "archive_snapshot_removed", action="cleanup", status="completed",
                         source=str(snapshot_path), details={"item_id": item_id})
            snapshot_cleanup_logged = True
        if destination.exists():
            raise FileExistsError(destination)
        output_format = validation.get("extraction_compatibility", {}).get("output_format", "directory")
        if pack_tree:
            if not tree_mode or output_format != "zar":
                raise ValueError("pack_tree interno exige árvore validada e saída ZArchive")
            if ZARCHIVE_TOOL_PATH.is_symlink() or not ZARCHIVE_TOOL_PATH.is_file():
                raise FileNotFoundError(f"Ferramenta ZArchive ausente durante a aplicação: {ZARCHIVE_TOOL_PATH}")
            archive_stage = destination.with_name(f".{destination.name}.zarchive-{uuid.uuid4().hex}")
            roundtrip = destination.with_name(f".{destination.name}.roundtrip-{uuid.uuid4().hex}")
            roundtrip.mkdir(mode=0o700)
            pack_result = compression_run([str(ZARCHIVE_TOOL_PATH), str(temp), str(archive_stage)])
            if not archive_stage.is_file() or archive_stage.is_symlink() or archive_stage.stat().st_size <= 0:
                raise RuntimeError("ZArchive não criou um arquivo regular, não vazio e isolado")
            archive_size = archive_stage.stat().st_size
            archive_sha = sha256_file(archive_stage)
            append_event(operation_id, "zarchive_pack_completed", action=action_name, status="completed",
                         source=str(temp), destination=str(archive_stage), sha256_after=archive_sha,
                         size_after=archive_size,
                         details={"item_id": item_id, "tool": str(ZARCHIVE_TOOL_PATH),
                                  "tool_sha256": sha256_file(ZARCHIVE_TOOL_PATH),
                                  "stdout_tail": pack_result.stdout[-2000:]})
            extract_result = compression_run([str(ZARCHIVE_TOOL_PATH), str(archive_stage), str(roundtrip)])
            created_snapshot = path_snapshot(temp, include_hash=True)
            roundtrip_snapshot = path_snapshot(roundtrip, include_hash=True)
            created_dirs = sorted(path.relative_to(temp).as_posix() for path in temp.rglob("*") if path.is_dir())
            roundtrip_dirs = sorted(path.relative_to(roundtrip).as_posix() for path in roundtrip.rglob("*") if path.is_dir())
            if (created_snapshot.get("kind") != "directory"
                    or roundtrip_snapshot.get("kind") != "directory"
                    or created_snapshot.get("sha256") != roundtrip_snapshot.get("sha256")
                    or created_snapshot.get("file_count") != roundtrip_snapshot.get("file_count")
                    or created_snapshot.get("size_bytes") != roundtrip_snapshot.get("size_bytes")
                    or created_dirs != roundtrip_dirs):
                raise RuntimeError("Round-trip integral do ZArchive divergiu em caminho, diretório, tamanho ou SHA-256")
            validation["zarchive_conversion"] = {
                "tool": str(ZARCHIVE_TOOL_PATH.relative_to(PROJECT_ROOT)),
                "tool_sha256": sha256_file(ZARCHIVE_TOOL_PATH),
                "tool_source": "Exzap/ZArchive v0.1.2; zstd 1.5.7 statically linked",
                "pack_returncode": 0, "pack_stdout_tail": pack_result.stdout[-2000:],
                "extract_returncode": 0, "extract_stdout_tail": extract_result.stdout[-2000:],
                "archive_sha256": archive_sha, "archive_size_bytes": archive_size,
                "source_tree_sha256": created_snapshot["sha256"],
                "source_tree_size_bytes": created_snapshot["size_bytes"],
                "verified_tree_sha256": roundtrip_snapshot["sha256"],
                "verified_file_count": roundtrip_snapshot["file_count"],
                "verified_directory_count": len(roundtrip_dirs),
                "roundtrip_byte_identical": True,
                "rollback": "fonte RAR preservada; o ZAR só pode ser removido após conferir SHA-256 registrado",
            }
            shutil.rmtree(roundtrip)
            roundtrip_path = str(roundtrip)
            roundtrip = None
            shutil.rmtree(temp)
            append_event(operation_id, "zarchive_staging_trees_removed_after_roundtrip", action="cleanup",
                         status="completed", source=str(temp),
                         details={"item_id": item_id, "roundtrip_directory": roundtrip_path,
                                  "tree_sha256": created_snapshot["sha256"]})
            os.rename(archive_stage, destination)
            archive_stage = None
            append_event(operation_id, "zarchive_output_committed", action=action_name, status="completed",
                         source=str(source), destination=str(destination), sha256_after=archive_sha,
                         size_after=archive_size,
                         details={"item_id": item_id, "verified_tree_sha256": created_snapshot["sha256"],
                                  "source_preserved": True})
        else:
            os.rename(temp, destination)
        append_event(operation_id, "extraction_staging_committed", action=action_name, status="completed",
                     source=str(temp), destination=str(destination),
                     details={"item_id": item_id, "output_format": output_format})
        return extracted
    except Exception:
        if snapshot_path is not None and not snapshot_cleanup_logged:
            snapshot_removed = not snapshot_path.exists()
            append_event(operation_id, "archive_snapshot_cleanup", action="cleanup",
                         status="completed" if snapshot_removed else "failed",
                         source=str(snapshot_path), details={"item_id": item_id,
                                                            "snapshot_removed": snapshot_removed})
        if temp.exists():
            shutil.rmtree(temp)
            append_event(operation_id, "extraction_staging_removed_after_failure", action="cleanup",
                         status="completed", source=str(temp), details={"item_id": item_id})
        if roundtrip is not None and roundtrip.exists():
            shutil.rmtree(roundtrip)
            append_event(operation_id, "zarchive_roundtrip_removed_after_failure", action="cleanup",
                         status="completed", source=str(roundtrip), details={"item_id": item_id})
        if archive_stage is not None and archive_stage.exists():
            archive_stage.unlink()
            append_event(operation_id, "zarchive_output_removed_after_failure", action="cleanup",
                         status="completed", source=str(archive_stage), details={"item_id": item_id})
        raise


def move_steam_install(item: dict, operation_id: str) -> None:
    source, destination = Path(item["source"]), Path(item["destination"])
    if steam_centralizer.steam_processes_running():
        raise RuntimeError("Feche o Steam e todos os jogos antes de centralizar instalações")
    steam_centralizer.validate_steam_action(item, PROJECT_ROOT)
    steam_centralizer.probe_symlink_support(source.parent)
    current = steam_tree_snapshot(source, allow_save_symlinks=True,
                                  logical_snapshot=item.get("before") or item.get("precondition"),
                                  appid=str(item.get("steam", {}).get("appid", "")))
    if current["sha256"] != item["before"]["sha256"]:
        raise ValueError(f"Árvore Steam mudou desde o plano: {source}")
    if os.path.lexists(destination):
        raise FileExistsError(f"Destino Steam já existe: {destination}")
    same_device = steam_centralizer.same_rename_domain(source, destination.parent)
    backup = None
    if same_device:
        os.rename(source, destination)
        item["transfer_method"] = "rename_same_device"
        append_event(operation_id, "steam_tree_moved_same_device", action="steam_move_link", status="completed",
                     source=str(source), destination=str(destination),
                     sha256_before=current["sha256"], size_before=current["size_bytes"],
                     details={"appid": item["steam"]["appid"]})
    else:
        temp = destination.with_name(f".{destination.name}.steam-copy-{uuid.uuid4().hex[:8]}")
        item["temporary_path"] = str(temp)
        try:
            shutil.copytree(source, temp, symlinks=True, copy_function=shutil.copy2)
        except Exception:
            if temp.exists():
                shutil.rmtree(temp)
            raise
        staged = steam_tree_snapshot(temp, allow_save_symlinks=True,
                                     logical_snapshot=item.get("before") or item.get("precondition"),
                                     appid=str(item.get("steam", {}).get("appid", "")))
        append_event(operation_id, "steam_cross_device_tree_staged", action="steam_move_link", status="staged",
                     source=str(source), destination=str(temp), sha256_before=current["sha256"],
                     sha256_after=staged["sha256"], size_before=current["size_bytes"],
                     size_after=staged["size_bytes"], details={"appid": item["steam"]["appid"]})
        if staged["sha256"] != current["sha256"]:
            shutil.rmtree(temp)
            raise RuntimeError(f"Árvore Steam divergiu na cópia cross-device: {source}")
        os.rename(temp, destination)
        append_event(operation_id, "steam_cross_device_tree_committed", action="steam_move_link", status="completed",
                     source=str(temp), destination=str(destination), sha256_after=staged["sha256"],
                     size_after=staged["size_bytes"], details={"appid": item["steam"]["appid"]})
        appid = item["steam"]["appid"]
        backup = source.with_name(f".{source.name}.romtool-steam-backup-{appid}-{operation_id[-8:]}")
        if os.path.lexists(backup):
            shutil.rmtree(destination)
            raise FileExistsError(f"Backup temporário Steam já existe: {backup}")
        try:
            os.rename(source, backup)
        except Exception:
            shutil.rmtree(destination)
            raise
        item["backup_path"] = str(backup)
        item["transfer_method"] = "copy_verify_then_same_device_rename"

    try:
        os.symlink(str(destination), source, target_is_directory=True)
        link_state = path_snapshot(source, include_hash=True, allow_leaf_symlink=True)
        if link_state.get("kind") != "symlink" or link_state.get("symlink_target") != str(destination):
            raise RuntimeError(f"Steam symlink criado com alvo inesperado: {source}")
        if source.resolve(strict=True) != destination.resolve(strict=True):
            raise RuntimeError(f"Steam symlink não resolve ao destino canônico: {source}")
    except Exception:
        if source.is_symlink():
            source.unlink()
        if backup and os.path.lexists(backup):
            os.rename(backup, source)
            shutil.rmtree(destination)
        elif os.path.lexists(destination):
            if steam_centralizer.same_rename_domain(destination, source.parent):
                os.rename(destination, source)
            else:
                restore_temp = source.with_name(f".{source.name}.steam-restore-{uuid.uuid4().hex[:8]}")
                shutil.copytree(destination, restore_temp, symlinks=True, copy_function=shutil.copy2)
                restored = steam_tree_snapshot(restore_temp, allow_save_symlinks=True,
                                               logical_snapshot=item.get("before") or item.get("precondition"),
                                               appid=str(item.get("steam", {}).get("appid", "")))
                if restored["sha256"] != item["before"]["sha256"]:
                    shutil.rmtree(restore_temp)
                    raise RuntimeError(f"Falha ao restaurar a instalação Steam original: {source}")
                os.rename(restore_temp, source)
                shutil.rmtree(destination)
        raise

    if backup and os.path.lexists(backup):
        try:
            shutil.rmtree(backup)
            item["backup_cleanup_status"] = "removed_after_verified_move"
        except OSError as exc:
            item["retained_backup"] = str(backup)
            item["backup_cleanup_status"] = "retained_for_recovery"
            item.setdefault("warnings", []).append(f"Backup removível permaneceu em {backup}: {exc}")
            append_event(operation_id, "steam_source_backup_retained", action="steam_move_link", status="warning",
                         source=str(backup), destination=str(destination), sha256_after=current["sha256"],
                         size_after=current["size_bytes"], details={"appid": item["steam"]["appid"], "error": str(exc)})

    item["steam_link_after"] = path_snapshot(source, include_hash=True, allow_leaf_symlink=True)
    item["after"] = steam_tree_snapshot(destination, allow_save_symlinks=True,
                                        logical_snapshot=item.get("before") or item.get("precondition"),
                                        appid=str(item.get("steam", {}).get("appid", "")))
    if item["after"]["sha256"] != item["before"]["sha256"]:
        raise RuntimeError(f"Hash da árvore Steam divergiu depois da centralização: {destination}")
    if item["steam_link_after"].get("symlink_target") != str(destination):
        raise RuntimeError(f"Link Steam perdeu o alvo canônico esperado: {source}")


def restore_steam_install(item: dict, operation_id: str) -> str:
    source, destination = Path(item["source"]), Path(item["destination"])
    if not source.is_symlink() or os.readlink(source) != str(destination):
        raise ValueError(f"Rollback Steam recusado; link original mudou: {source}")
    current = steam_tree_snapshot(destination, allow_save_symlinks=True,
                                  logical_snapshot=item.get("after") or item.get("before"),
                                  appid=str(item.get("steam", {}).get("appid", "")))
    if current["sha256"] != item.get("after", {}).get("sha256"):
        raise ValueError(f"Conteúdo canônico mudou desde a centralização: {destination}")
    steam = item["steam"]
    manifest = Path(steam["manifest_path"])
    if not manifest.is_file() or sha256_file(manifest) != steam.get("manifest_sha256"):
        raise ValueError(f"Appmanifest mudou ou está indisponível; rollback bloqueado: {manifest}")
    library_rows = steam_centralizer.discover_steam_libraries(scan_mounts=True)["libraries"]
    library = next((row for row in library_rows if row["library_path"] == steam["library_path"]), None)
    if not library or not library.get("trusted") or not steam_centralizer._registered_app_id(str(steam["appid"]), library):
        raise ValueError(f"Steam library não está montada e registrada para rollback: {steam['library_path']}")
    if steam_centralizer.steam_processes_running():
        raise RuntimeError("Feche o Steam e todos os jogos antes do rollback")

    backup = Path(item["retained_backup"]) if item.get("retained_backup") else None
    if backup and backup.is_dir() and not backup.is_symlink():
        try:
            backup_state = steam_tree_snapshot(backup, allow_save_symlinks=True,
                                               logical_snapshot=item.get("precondition"),
                                               appid=str(item.get("steam", {}).get("appid", "")))
        except Exception:
            backup_state = {}
        if backup_state.get("sha256") == item["precondition"]["sha256"]:
            source.unlink()
            os.rename(backup, source)
            shutil.rmtree(destination)
            return "restored_from_verified_retained_backup"

    source.unlink()
    if steam_centralizer.same_rename_domain(destination, source.parent):
        os.rename(destination, source)
        return "renamed_back_after_tree_hash_match"

    temp = source.with_name(f".{source.name}.steam-rollback-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copytree(destination, temp, symlinks=True, copy_function=shutil.copy2)
        restored = steam_tree_snapshot(temp, allow_save_symlinks=True,
                                       logical_snapshot=item.get("precondition"),
                                       appid=str(item.get("steam", {}).get("appid", "")))
        if restored["sha256"] != item["precondition"]["sha256"]:
            raise RuntimeError(f"Árvore restaurada divergiu do original: {source}")
        os.rename(temp, source)
        shutil.rmtree(destination)
        if backup and os.path.lexists(backup):
            try:
                shutil.rmtree(backup)
            except OSError:
                pass
        return "copied_back_verified_then_removed_canonical_tree"
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        if not os.path.lexists(source) and destination.exists():
            os.symlink(str(destination), source, target_is_directory=True)
        raise


def move_steam_save(item: dict, operation_id: str) -> None:
    source, destination = Path(item["source"]), Path(item["destination"])
    if steam_centralizer.steam_processes_running():
        raise RuntimeError("Feche o Steam e todos os jogos antes de centralizar saves")
    steam_centralizer.validate_steam_save_action(item, PROJECT_ROOT)
    steam_centralizer.probe_symlink_support(source.parent)
    current = steam_payload_snapshot(source)
    if current["sha256"] != item["before"]["sha256"]:
        raise ValueError(f"Save Steam mudou desde o plano: {source}")
    if os.path.lexists(destination):
        raise FileExistsError(f"Destino de save Steam já existe: {destination}")
    is_directory = current["kind"] == "directory"
    same_device = steam_centralizer.same_rename_domain(source, destination.parent)
    backup = None
    if same_device:
        os.rename(source, destination)
        item["transfer_method"] = "rename_same_device"
        append_event(operation_id, "steam_save_moved_same_device", action="steam_save_move_link",
                     status="completed", source=str(source), destination=str(destination),
                     sha256_before=current["sha256"], size_before=current["size_bytes"],
                     details={"appid": item["steam"]["appid"], "source_kind": item["steam"]["source_kind"]})
    else:
        temp = destination.with_name(f".{destination.name}.steam-save-copy-{uuid.uuid4().hex[:8]}")
        item["temporary_path"] = str(temp)
        try:
            if is_directory:
                shutil.copytree(source, temp, symlinks=True, copy_function=shutil.copy2)
            else:
                shutil.copy2(source, temp)
        except Exception:
            if temp.is_dir() and not temp.is_symlink():
                shutil.rmtree(temp)
            else:
                temp.unlink(missing_ok=True)
            raise
        staged = steam_payload_snapshot(temp)
        append_event(operation_id, "steam_save_cross_device_staged", action="steam_save_move_link",
                     status="staged", source=str(source), destination=str(temp),
                     sha256_before=current["sha256"], sha256_after=staged["sha256"],
                     size_before=current["size_bytes"], size_after=staged["size_bytes"],
                     details={"appid": item["steam"]["appid"]})
        if staged["sha256"] != current["sha256"]:
            if temp.is_dir():
                shutil.rmtree(temp)
            else:
                temp.unlink(missing_ok=True)
            raise RuntimeError(f"Hash do save Steam divergiu na cópia cross-device: {source}")
        os.rename(temp, destination)
        backup = source.with_name(f".{source.name}.romtool-steam-save-backup-{item['steam']['appid']}-{operation_id[-8:]}")
        if os.path.lexists(backup):
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            raise FileExistsError(f"Backup temporário de save Steam já existe: {backup}")
        try:
            os.rename(source, backup)
        except Exception:
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
            raise
        item["backup_path"] = str(backup)
        item["transfer_method"] = "copy_verify_then_same_device_rename"

    try:
        os.symlink(str(destination), source, target_is_directory=is_directory)
        link_state = path_snapshot(source, include_hash=True, allow_leaf_symlink=True)
        if link_state.get("kind") != "symlink" or link_state.get("symlink_target") != str(destination):
            raise RuntimeError(f"Symlink do save criado com alvo inesperado: {source}")
        if source.resolve(strict=True) != destination.resolve(strict=True):
            raise RuntimeError(f"Symlink do save não resolve ao destino canônico: {source}")
    except Exception:
        if source.is_symlink():
            source.unlink()
        if backup and os.path.lexists(backup):
            os.rename(backup, source)
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
        elif os.path.lexists(destination):
            if steam_centralizer.same_rename_domain(destination, source.parent):
                os.rename(destination, source)
            elif destination.is_dir():
                restore_temp = source.with_name(f".{source.name}.steam-save-restore-{uuid.uuid4().hex[:8]}")
                shutil.copytree(destination, restore_temp, symlinks=True, copy_function=shutil.copy2)
                if steam_payload_snapshot(restore_temp)["sha256"] != item["before"]["sha256"]:
                    shutil.rmtree(restore_temp)
                    raise RuntimeError(f"Falha ao restaurar diretório de save Steam: {source}")
                os.rename(restore_temp, source)
                shutil.rmtree(destination)
            else:
                restore_temp = source.with_name(f".{source.name}.steam-save-restore-{uuid.uuid4().hex[:8]}")
                shutil.copy2(destination, restore_temp)
                if sha256_file(restore_temp) != item["before"]["sha256"]:
                    restore_temp.unlink(missing_ok=True)
                    raise RuntimeError(f"Falha ao restaurar arquivo de save Steam: {source}")
                os.rename(restore_temp, source)
                destination.unlink()
        raise

    if backup and os.path.lexists(backup):
        try:
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
            item["backup_cleanup_status"] = "removed_after_verified_move"
        except OSError as exc:
            item["retained_backup"] = str(backup)
            item["backup_cleanup_status"] = "retained_for_recovery"
            item.setdefault("warnings", []).append(f"Backup recuperável permaneceu em {backup}: {exc}")

    item["steam_link_after"] = path_snapshot(source, include_hash=True, allow_leaf_symlink=True)
    item["after"] = steam_payload_snapshot(destination)
    if item["after"]["sha256"] != item["before"]["sha256"]:
        raise RuntimeError(f"Hash do save Steam divergiu depois da centralização: {destination}")
    if item["steam_link_after"].get("symlink_target") != str(destination):
        raise RuntimeError(f"Link do save Steam perdeu o alvo canônico: {source}")


def restore_steam_save(item: dict, operation_id: str) -> str:
    source, destination = Path(item["source"]), Path(item["destination"])
    if not source.is_symlink() or os.readlink(source) != str(destination):
        raise ValueError(f"Rollback de save Steam recusado; link original mudou: {source}")
    if steam_centralizer.steam_processes_running():
        raise RuntimeError("Feche o Steam e todos os jogos antes de reverter saves")
    steam_centralizer.validate_steam_save_action(item, PROJECT_ROOT, allow_centralized_source=True)
    current = steam_payload_snapshot(destination)
    if current["sha256"] != item.get("after", {}).get("sha256"):
        raise ValueError(f"Conteúdo canônico do save mudou desde a centralização: {destination}")
    backup = Path(item["retained_backup"]) if item.get("retained_backup") else None
    if backup and os.path.lexists(backup):
        backup_state = steam_payload_snapshot(backup)
        if backup_state.get("sha256") == item["precondition"]["sha256"]:
            source.unlink()
            os.rename(backup, source)
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
            return "restored_from_verified_retained_backup"

    source.unlink()
    source.parent.mkdir(parents=True, exist_ok=True)
    if steam_centralizer.same_rename_domain(destination, source.parent):
        os.rename(destination, source)
        return "renamed_back_after_save_hash_match"
    temp = source.with_name(f".{source.name}.steam-save-rollback-{uuid.uuid4().hex[:8]}")
    try:
        if destination.is_dir():
            shutil.copytree(destination, temp, symlinks=True, copy_function=shutil.copy2)
        else:
            shutil.copy2(destination, temp)
        restored = steam_payload_snapshot(temp)
        if restored["sha256"] != item["precondition"]["sha256"]:
            if temp.is_dir():
                shutil.rmtree(temp)
            else:
                temp.unlink(missing_ok=True)
            raise RuntimeError(f"Hash divergiu ao restaurar save Steam: {source}")
        os.rename(temp, source)
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
        return "copied_back_verified_then_removed_canonical_save"
    except Exception:
        if os.path.lexists(temp):
            if temp.is_dir() and not temp.is_symlink():
                shutil.rmtree(temp)
            else:
                temp.unlink(missing_ok=True)
        if not os.path.lexists(source) and destination.exists():
            os.symlink(str(destination), source, target_is_directory=destination.is_dir())
        raise


def apply_operation(operation_id: str, confirmed: bool) -> dict:
    if not confirmed:
        raise ValueError("Aplicação exige --apply explícito")
    manifest_path = operation_dir(operation_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "prepared":
        raise ValueError(f"Plano não preparado: {manifest.get('status')}")
    policy = load_policy()
    media_items = [item for item in manifest["items"] if item.get("action") in MEDIA_ACTIONS]
    extraction_items = [item for item in manifest["items"]
                        if item.get("action") in {"extract", "extract_tree", "pack_tree"}]
    active_media_quarantines = [item for item in manifest["items"]
                                if item.get("action") == "quarantine"
                                and is_within(Path(item["source"]), MEDIA_SOURCE_ROOT)]
    bios_firmware_items = [item for item in manifest["items"] if item.get("bios_firmware_relocation")]
    if (media_items or active_media_quarantines or extraction_items or bios_firmware_items) and retrofe_processes_running():
        raise RuntimeError("Feche o RetroFE antes de alterar mídia, jogos ou firmware da biblioteca")
    planned_media_hashes = {}
    for item in manifest["items"]:
        source, destination = Path(item["source"]), Path(item["destination"])
        kind = item.get("action", "")
        if item.get("runtime_support_relocation"):
            support_action = {
                "action": kind,
                "system_id": item["system_id"],
                "classification": item["classification"],
                "runtime_support_relocation": True,
                "duplicate_of": item["runtime_support_relocation"].get("duplicate_of", ""),
            }
            current_support = validate_runtime_support_relocation(
                support_action, source, destination, operation_id
            )
            if current_support != item["runtime_support_relocation"]:
                raise ValueError(f"Escopo/evidência de movimento de suporte mudou: {source}")
        if item.get("platform_metadata_relocation"):
            metadata_action = {
                "action": kind,
                "system_id": item["system_id"],
                "classification": item["classification"],
                "reference_check": item["reference_check"],
                "frontend_references": item.get("frontend_references", []),
                "platform_metadata_relocation": True,
            }
            current_metadata = validate_platform_metadata_relocation(
                metadata_action, source, destination
            )
            if current_metadata != item["platform_metadata_relocation"]:
                raise ValueError(f"Escopo/evidência de movimento de metadados mudou: {source}")
        if item.get("launcher_bundle_relocation"):
            launcher_action = {
                "action": kind,
                "system_id": item["system_id"],
                "classification": item["classification"],
                "reference_check": item["reference_check"],
                "frontend_references": item.get("frontend_references", []),
                "launcher_bundle_relocation": True,
            }
            current_launcher_bundle = validate_launcher_bundle_relocation(
                launcher_action, source, destination
            )
            if current_launcher_bundle != item["launcher_bundle_relocation"]:
                raise ValueError(f"Escopo/evidência do bundle de launchers mudou: {source}")
        if item.get("bios_broken_link_quarantine"):
            bios_action = {
                "action": kind,
                "classification": item["classification"],
                "reference_check": item["reference_check"],
                "dependency_check": item["dependency_check"],
                "bios_broken_link_quarantine": True,
            }
            current_bios_scope = validate_bios_broken_link_quarantine(
                bios_action, source, destination, operation_id
            )
            if current_bios_scope != item["bios_broken_link_quarantine"]:
                raise ValueError(f"Escopo/evidência da quarentena BIOS mudou: {source}")
        if item.get("bios_firmware_relocation"):
            firmware_action = {
                "action": kind,
                "system_id": item["system_id"],
                "classification": item["classification"],
                "reference_check": item["reference_check"],
                "expected_sha256": item["bios_firmware_relocation"].get("source_sha256", ""),
                "bios_firmware_relocation": True,
            }
            current_firmware_scope = validate_bios_firmware_relocation(
                firmware_action, source, destination
            )
            if current_firmware_scope != item["bios_firmware_relocation"]:
                raise ValueError(f"Escopo/evidência da relocação de firmware mudou: {source}")
        if kind in MEDIA_ACTIONS:
            media = item.get("media", {})
            alias = media_item_alias(item)
            media_type = str(media.get("media_type", ""))
            validate_game_media_destination(destination, system_id=str(item.get("system_id", "")),
                                           media_type=media_type)
            ensure_allowed(destination)
            if kind in {"media_move_link", "media_alias_link"}:
                validate_game_media_source(source)
                ensure_allowed(source)
            elif kind == "media_rekey_link":
                validate_game_media_destination(source, system_id=str(item.get("system_id", "")),
                                                media_type=media_type)
                ensure_allowed(source)
                reject_symlink_components(source)
            else:
                ensure_allowed(source)
                reject_symlink_components(source)
                if source.suffix.casefold() not in MEDIA_EXTS or "medium_artwork" not in {
                        part.casefold() for part in source.parts}:
                    raise ValueError(f"Backup de mídia fora de escopo: {source}")
                validate_game_media_source(alias)
                ensure_allowed(alias)
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"Origem de mídia não é arquivo regular: {source}")
            current = path_snapshot(source, include_hash=True)
            if (current.get("kind") != item.get("precondition", {}).get("kind")
                    or current.get("sha256") != item.get("precondition", {}).get("sha256")):
                raise ValueError(f"Hash/tipo da origem de mídia mudou após o plano: {source}")
            if kind == "media_move_link":
                if os.path.lexists(destination):
                    raise FileExistsError(f"Destino de payload já existe: {destination}")
                planned_media_hashes[str(destination)] = current["sha256"]
            elif kind == "media_rekey_link":
                if media.get("reuses_existing_payload"):
                    if (destination.is_symlink() or not destination.is_file()
                            or sha256_file(destination) != current["sha256"]):
                        raise ValueError(f"Payload canônico reaproveitado não confere: {destination}")
                elif media.get("reuses_planned_payload"):
                    if planned_media_hashes.get(str(destination)) != current["sha256"]:
                        raise ValueError(f"Payload canônico anterior do plano não confere: {destination}")
                else:
                    if os.path.lexists(destination):
                        raise FileExistsError(f"Destino de payload já existe: {destination}")
                    planned_media_hashes[str(destination)] = current["sha256"]
            elif kind == "media_repair_link":
                canonical_created = bool(media.get("canonical_payload_created", media.get("canonical_payload", True)))
                if os.path.lexists(destination):
                    if (canonical_created or destination.is_symlink() or not destination.is_file()
                            or sha256_file(destination) != current["sha256"]):
                        raise ValueError(f"Payload canônico de reparo existente não confere: {destination}")
                elif str(destination) in planned_media_hashes:
                    if planned_media_hashes[str(destination)] != current["sha256"] or canonical_created:
                        raise ValueError(f"Payload canônico planejado para reparo não confere: {destination}")
                elif canonical_created:
                    planned_media_hashes[str(destination)] = current["sha256"]
                else:
                    raise ValueError(f"Payload canônico reutilizado não existe nem foi planejado: {destination}")
            else:
                if os.path.lexists(destination):
                    if destination.is_symlink() or not destination.is_file():
                        raise ValueError(f"Payload canônico do alias não é arquivo regular: {destination}")
                    canonical_hash = sha256_file(destination)
                else:
                    canonical_hash = planned_media_hashes.get(str(destination), "")
                if canonical_hash != current["sha256"]:
                    raise ValueError(f"Payload canônico do alias não confere: {source} -> {destination}")
            if kind == "media_repair_link":
                alias_before = path_snapshot(alias, include_hash=True)
                if (alias.is_symlink() or alias_before["sha256"] != media.get("replaced_frontend_sha256")
                        or alias_before["sha256"] != item.get("alias_precondition", {}).get("sha256")):
                    raise ValueError(f"Mídia defeituosa/alias mudou após o plano: {alias}")
            quarantine = item.get("quarantine_destination")
            if quarantine:
                quarantine_path = Path(quarantine)
                validate_media_operation_quarantine(quarantine_path, operation_id)
                ensure_allowed(quarantine_path)
                if os.path.lexists(quarantine_path):
                    raise FileExistsError(f"Quarentena já ocupada: {quarantine_path}")
                guarded_source = alias if kind == "media_repair_link" else source
                if existing_path_device(guarded_source) != existing_path_device(quarantine_path.parent):
                    raise ValueError(f"Quarentena de mídia requer rename no mesmo dispositivo: {guarded_source}")
            continue
        source_is_symlink = item.get("precondition", {}).get("kind") == "symlink"
        active_media_quarantine = kind == "quarantine" and is_within(source, MEDIA_SOURCE_ROOT)
        if active_media_quarantine:
            if source_is_symlink:
                if not policy.get("quarantine_leaf_symlink", False) or source.exists():
                    raise ValueError(f"Só symlink folha obsoleto e quebrado pode ir à quarentena: {source}")
                validate_game_media_source(source, allow_leaf_symlink=True)
            else:
                validate_game_media_source(source)
            validate_media_operation_quarantine(destination, operation_id)
            status = str(item.get("validation_result", "")).casefold()
            allowed_invalid = ("symlink_review", "invalid") if source_is_symlink else ("empty", "invalid", "type_mismatch")
            if item.get("classification") != "corrupt_candidate" or not any(marker in status for marker in allowed_invalid):
                raise ValueError(f"Quarentena de mídia ativa perdeu sua evidência de arquivo inválido: {source}")
            if existing_path_device(source) != existing_path_device(destination.parent):
                raise ValueError(f"Quarentena de mídia ativa requer rename no mesmo dispositivo: {source}")
        is_steam_move = item.get("action") == "steam_move_link"
        is_steam_save_move = item.get("action") == "steam_save_move_link"
        if source_is_symlink and (item.get("action") != "quarantine" or not policy.get("quarantine_leaf_symlink", False)):
            raise ValueError(f"Symlink só pode ser isolado por quarentena reversível habilitada: {source}")
        if is_steam_move or is_steam_save_move:
            if is_steam_move:
                steam_centralizer.validate_steam_action(item, PROJECT_ROOT)
            else:
                steam_centralizer.validate_steam_save_action(item, PROJECT_ROOT)
            ensure_allowed(destination)
        else:
            ensure_allowed(source, allow_leaf_symlink=source_is_symlink)
            ensure_allowed(destination)
        if not os.path.lexists(source) or os.path.lexists(destination):
            raise FileExistsError(f"Preflight falhou: {source} -> {destination}")
        current = operation_snapshot(source, item, allow_leaf_symlink=source_is_symlink)
        if current.get("kind") != item["precondition"].get("kind"):
            raise ValueError(f"Tipo da origem mudou após o plano: {source}")
        if source_is_symlink and current.get("symlink_target") != item["precondition"].get("symlink_target"):
            raise ValueError(f"Alvo textual do symlink mudou após o plano: {source}")
        if current.get("sha256") != item["precondition"].get("sha256"):
            raise ValueError(f"Hash mudou após o plano: {source}")
    manifest["status"] = "applying"
    manifest["applied_at"] = now()
    write_manifest(manifest)
    append_event(operation_id, "operation_started", action="apply", status="applying",
                 details={"item_count": len(manifest["items"])})
    try:
        for item in manifest["items"]:
            source, destination = Path(item["source"]), Path(item["destination"])
            source_is_symlink = item.get("precondition", {}).get("kind") == "symlink"
            item["before"] = operation_snapshot(source, item, allow_leaf_symlink=source_is_symlink)
            if item["before"].get("kind") != item["precondition"].get("kind") or item["before"].get("sha256") != item["precondition"].get("sha256"):
                raise ValueError(f"Precondição da origem mudou durante a aplicação: {source}")
            if source_is_symlink and item["before"].get("symlink_target") != item["precondition"].get("symlink_target"):
                raise ValueError(f"Alvo textual do symlink mudou durante a aplicação: {source}")
            append_event(operation_id, "item_change_started", action=item["action"], status="running",
                         source=str(source), destination=str(destination),
                         sha256_before=item["before"].get("sha256", ""),
                         size_before=item["before"].get("size_bytes", ""),
                         details={"reason": item.get("reason", ""), "system_id": item["system_id"],
                                  "item_identity": item["item_identity"]})
            try:
                create_destination_parents(destination.parent, operation_id, item)
                if item["action"] in MEDIA_ACTIONS:
                    if item.get("quarantine_destination"):
                        create_destination_parents(Path(item["quarantine_destination"]).parent,
                                                   operation_id, item)
                    apply_media_action(item, operation_id)
                elif item["action"] == "steam_move_link":
                    move_steam_install(item, operation_id)
                elif item["action"] == "steam_save_move_link":
                    move_steam_save(item, operation_id)
                elif item["action"] in {"extract", "extract_tree", "pack_tree"}:
                    item["extracted_files"] = safe_extract(source, destination, item["validation"],
                                                           operation_id, item["item_id"],
                                                           reuse_reviewed_listing=item["action"] in {"extract_tree", "pack_tree"},
                                                           tree_mode=item["action"] in {"extract_tree", "pack_tree"},
                                                           pack_tree=item["action"] == "pack_tree")
                    item["source_preserved"] = True
                    item["transfer_method"] = (
                        "reviewed_archive_tree_to_kyty_zarchive_zstd_max_level_with_full_roundtrip"
                        if item["action"] == "pack_tree" else
                        "safe_extract_reviewed_archive_tree_flattened_for_runtime"
                        if item["action"] == "extract_tree" else "safe_extract_to_new_directory")
                    for member in item["extracted_files"]:
                        append_event(operation_id, "archive_member_extracted", action=item["action"], status="completed",
                                     source=str(source),
                                     destination=(str(destination) if item["action"] == "pack_tree"
                                                  else str(destination / member["relative_path"])),
                                     sha256_after=member["sha256"], size_after=member["size_bytes"],
                                     details={"relative_path": member["relative_path"], "item_id": item["item_id"]})
                elif source_is_symlink:
                    if item["action"] != "quarantine" or source.lstat().st_dev != destination.parent.stat().st_dev:
                        raise ValueError(f"Quarentena de symlink exige ação quarantine no mesmo dispositivo: {source} -> {destination}")
                    os.rename(source, destination)
                    item["transfer_method"] = "rename_symlink_same_device_no_follow"
                elif source.stat().st_dev == destination.parent.stat().st_dev:
                    os.rename(source, destination)
                    item["transfer_method"] = "rename_same_device"
                elif source.is_file():
                    temp = destination.with_name(f".{destination.name}.copy-{uuid.uuid4().hex[:8]}")
                    item["temporary_path"] = str(temp)
                    shutil.copy2(source, temp)
                    temp_hash = sha256_file(temp)
                    append_event(operation_id, "cross_device_copy_staged", action="move", status="staged",
                                 source=str(source), destination=str(temp),
                                 sha256_before=item["before"]["sha256"], sha256_after=temp_hash,
                                 size_before=item["before"]["size_bytes"], size_after=temp.stat().st_size)
                    if temp_hash != item["before"]["sha256"]:
                        temp.unlink(missing_ok=True)
                        append_event(operation_id, "cross_device_copy_discarded", action="cleanup",
                                     status="completed", source=str(temp),
                                     sha256_before=temp_hash, size_before=item["before"]["size_bytes"],
                                     details={"reason": "SHA-256 diverged from source"})
                        raise RuntimeError(f"Hash divergente na cópia: {source}")
                    os.replace(temp, destination)
                    append_event(operation_id, "cross_device_copy_committed", action="move", status="completed",
                                 source=str(temp), destination=str(destination),
                                 sha256_after=temp_hash, size_after=item["before"]["size_bytes"])
                    source.unlink()
                    item["transfer_method"] = "copy_verify_unlink"
                    append_event(operation_id, "source_removed_after_copy_verified", action="move", status="completed",
                                 source=str(source), destination=str(destination),
                                 sha256_before=item["before"]["sha256"], size_before=item["before"]["size_bytes"])
                else:
                    temp = destination.with_name(f".{destination.name}.copy-{uuid.uuid4().hex[:8]}")
                    item["temporary_path"] = str(temp)
                    shutil.copytree(source, temp, symlinks=False)
                    temp_state = path_snapshot(temp, include_hash=True)
                    append_event(operation_id, "cross_device_tree_staged", action="move", status="staged",
                                 source=str(source), destination=str(temp),
                                 sha256_before=item["before"]["sha256"], sha256_after=temp_state["sha256"],
                                 size_before=item["before"]["size_bytes"], size_after=temp_state["size_bytes"])
                    if temp_state["sha256"] != item["before"]["sha256"]:
                        shutil.rmtree(temp)
                        append_event(operation_id, "cross_device_tree_discarded", action="cleanup",
                                     status="completed", source=str(temp),
                                     sha256_before=temp_state["sha256"], size_before=temp_state["size_bytes"],
                                     details={"reason": "tree SHA-256 diverged from source"})
                        raise RuntimeError(f"Hash de árvore divergente: {source}")
                    os.rename(temp, destination)
                    append_event(operation_id, "cross_device_tree_committed", action="move", status="completed",
                                 source=str(temp), destination=str(destination),
                                 sha256_after=temp_state["sha256"], size_after=temp_state["size_bytes"])
                    shutil.rmtree(source)
                    item["transfer_method"] = "copy_verify_unlink_tree"
                    append_event(operation_id, "source_removed_after_copy_verified", action="move", status="completed",
                                 source=str(source), destination=str(destination),
                                 sha256_before=item["before"]["sha256"], size_before=item["before"]["size_bytes"])
                if "after" not in item:
                    item["after"] = operation_snapshot(destination, item, allow_leaf_symlink=source_is_symlink)
                extraction_action = item["action"] in {"extract", "extract_tree", "pack_tree"}
                if not extraction_action and item["after"].get("sha256") != item["before"].get("sha256"):
                    raise RuntimeError(f"Hash divergente após mudança: {destination}")
                if item["action"] == "pack_tree" and (
                        item["after"].get("kind") != "file" or item["after"].get("size_bytes", 0) <= 0):
                    raise RuntimeError(f"Saída ZArchive ausente/vazia: {destination}")
                if extraction_action and item["action"] != "pack_tree" and not item["after"].get("file_count", 0):
                    raise RuntimeError(f"Extração sem arquivos: {destination}")
                item["status"] = "completed_verified"
                write_manifest(manifest)
                append_event(operation_id, "item_change_completed", action=item["action"], status=item["status"],
                             source=str(source), destination=str(destination),
                             sha256_before=item["before"].get("sha256", ""),
                             sha256_after=item["after"].get("sha256", ""),
                             size_before=item["before"].get("size_bytes", ""),
                             size_after=item["after"].get("size_bytes", ""),
                             details={"transfer_method": item["transfer_method"],
                                      "extracted_file_count": len(item.get("extracted_files", []))})
            except Exception as exc:
                item["error"] = str(exc)
                if os.path.lexists(destination):
                    try:
                        item["after"] = operation_snapshot(destination, item,
                                                            allow_leaf_symlink=source_is_symlink)
                        item["status"] = "failed_after_partial_change"
                    except Exception as snapshot_error:
                        item["status"] = "failed_destination_unreadable"
                        item["snapshot_error"] = str(snapshot_error)
                else:
                    item["status"] = "failed_no_change"
                write_manifest(manifest)
                append_event(operation_id, "item_change_failed", action=item["action"], status=item["status"],
                             source=str(source), destination=str(destination),
                             sha256_before=item["before"].get("sha256", ""),
                             sha256_after=item.get("after", {}).get("sha256", ""),
                             size_before=item["before"].get("size_bytes", ""),
                             size_after=item.get("after", {}).get("size_bytes", ""),
                             details={"error": str(exc)})
                raise
    except Exception as exc:
        manifest["status"] = "failed_partial" if any(
            x.get("status") in {"completed_verified", "failed_after_partial_change"} for x in manifest["items"]
        ) else "failed"
        manifest["error"] = str(exc)
        manifest["failed_at"] = now()
        write_manifest(manifest)
        append_event(operation_id, "operation_failed", action="apply", status=manifest["status"],
                     details={"error": str(exc)})
        rebuild_history_index()
        raise
    manifest["status"] = "completed_verified"
    manifest["completed_at"] = now()
    write_manifest(manifest)
    append_event(operation_id, "operation_completed", action="apply", status=manifest["status"],
                 details={"item_count": len(manifest["items"])})
    rebuild_history_index()
    if any(item.get("action") in {"steam_move_link", "steam_save_move_link"}
           for item in manifest["items"]):
        update_steam_project_records(manifest, rolled_back=False)
    if any(item.get("action") in MEDIA_ACTIONS for item in manifest["items"]):
        update_game_media_project_records(manifest, rolled_back=False)
    return manifest


def rollback_operation(operation_id: str, confirmed: bool) -> dict:
    if not confirmed:
        raise ValueError("Rollback exige --rollback explícito")
    manifest_path = operation_dir(operation_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"completed_verified", "failed_partial"}:
        raise ValueError(f"Operação sem mudanças reversíveis: {manifest.get('status')}")
    active_media_quarantines = [item for item in manifest.get("items", [])
                                if item.get("action") == "quarantine"
                                and is_within(Path(item["source"]), MEDIA_SOURCE_ROOT)]
    extraction_items = [item for item in manifest.get("items", [])
                        if item.get("action") in {"extract", "extract_tree", "pack_tree"}]
    if (active_media_quarantines or extraction_items) and retrofe_processes_running():
        raise RuntimeError("Feche o RetroFE antes de reverter mídia ou conteúdo da árvore de ROMs")
    for item in reversed(manifest["items"]):
        if item.get("status") not in {"completed_verified", "failed_after_partial_change"}:
            continue
        destination, source = Path(item["destination"]), Path(item["source"])
        is_steam_move = item.get("action") == "steam_move_link"
        is_steam_save_move = item.get("action") == "steam_save_move_link"
        source_is_symlink = item.get("precondition", {}).get("kind") == "symlink"
        if item.get("action") in MEDIA_ACTIONS:
            alias = media_item_alias(item)
            ensure_allowed(destination)
            ensure_allowed(alias, allow_leaf_symlink=True)
            if item.get("action") == "media_repair_link":
                ensure_allowed(source)
            validate_game_media_destination(destination, system_id=str(item.get("system_id", "")),
                                           media_type=str(item.get("media", {}).get("media_type", "")))
            rollback = rollback_media_action(item, operation_id)
            remaining_directories = []
            for name in reversed(item.get("created_directories", [])):
                directory = Path(name)
                try:
                    directory.rmdir()
                    append_event(operation_id, "created_directory_removed_by_rollback", action="rmdir",
                                 status="completed", source=str(directory),
                                 details={"item_id": item["item_id"]})
                except OSError:
                    remaining_directories.append(str(directory))
            item["remaining_created_directories"] = list(reversed(remaining_directories))
            item["rollback_status"] = "verified"
            item["rollback"] = rollback
            write_manifest(manifest)
            append_event(operation_id, "item_rollback_completed", action=item["action"], status="verified",
                         source=str(destination), destination=str(alias),
                         sha256_before=item.get("after", {}).get("sha256", item["precondition"].get("sha256", "")),
                         sha256_after=item["precondition"].get("sha256", ""),
                         size_before=item.get("after", {}).get("size_bytes", ""),
                         size_after=item["precondition"].get("size_bytes", ""),
                         details={"rollback": rollback})
            continue
        if is_steam_move or is_steam_save_move:
            ensure_allowed(destination)
            if not source.is_symlink() or os.readlink(source) != str(destination):
                raise ValueError(f"Rollback Steam recusado; caminho original não aponta ao destino registrado: {source}")
            if is_steam_move:
                pass
            else:
                steam_centralizer.validate_steam_save_action(item, PROJECT_ROOT,
                                                             allow_centralized_source=True)
        else:
            ensure_allowed(destination, allow_leaf_symlink=source_is_symlink)
            ensure_allowed(source)
        source_collision = os.path.lexists(source) and item["action"] not in {
            "extract", "extract_tree", "pack_tree", "steam_move_link", "steam_save_move_link"}
        if not os.path.lexists(destination) or source_collision:
            raise FileExistsError(f"Rollback recusado: {destination} -> {source}")
        current = operation_snapshot(destination, item, allow_leaf_symlink=source_is_symlink)
        if current.get("kind") != item.get("after", {}).get("kind"):
            raise ValueError(f"Tipo do destino mudou; rollback bloqueado: {destination}")
        if source_is_symlink and current.get("symlink_target") != item.get("after", {}).get("symlink_target"):
            raise ValueError(f"Alvo textual do symlink mudou; rollback bloqueado: {destination}")
        if is_steam_move:
            if current.get("sha256") != item.get("after", {}).get("sha256"):
                raise ValueError(f"Árvore Steam canônica mudou; rollback bloqueado: {destination}")
            appid = str(item.get("steam", {}).get("appid", ""))
            if steam_game_has_centralized_saves(appid, destination):
                raise ValueError(
                    f"Rollback do jogo bloqueado enquanto houver saves centralizados; reverta saves primeiro: {destination}")
            rollback = restore_steam_install(item, operation_id)
        elif is_steam_save_move:
            if current.get("sha256") != item.get("after", {}).get("sha256"):
                raise ValueError(f"Save canônico mudou; rollback bloqueado: {destination}")
            rollback = restore_steam_save(item, operation_id)
        elif item["action"] == "pack_tree":
            expected_source = item.get("precondition", {})
            if (current.get("kind") != "file"
                    or current.get("sha256") != item.get("after", {}).get("sha256")
                    or current.get("size_bytes") != item.get("after", {}).get("size_bytes")):
                raise ValueError(f"ZArchive mudou; rollback bloqueado: {destination}")
            if (not source.is_file() or source.is_symlink()
                    or source.stat().st_size != expected_source.get("size_bytes")
                    or sha256_file(source) != expected_source.get("sha256")):
                raise ValueError(f"RAR original ausente ou alterado; rollback do ZAR bloqueado: {source}")
            verify_root = operation_dir(operation_id) / "rollback" / f"{item['item_id']}-zarchive-verify"
            if os.path.lexists(verify_root):
                raise FileExistsError(f"Diretório temporário de rollback já existe: {verify_root}")
            verify_root.mkdir(parents=True, mode=0o700)
            try:
                result = compression_run([str(ZARCHIVE_TOOL_PATH), str(destination), str(verify_root)])
                observed = path_snapshot(verify_root, include_hash=True)
                expected_tree = item.get("validation", {}).get("zarchive_conversion", {})
                expected_dirs = set(item.get("validation", {}).get("extracted_directories", []))
                observed_dirs = {path.relative_to(verify_root).as_posix()
                                 for path in verify_root.rglob("*") if path.is_dir()}
                if (observed.get("kind") != "directory"
                        or observed.get("sha256") != expected_tree.get("source_tree_sha256")
                        or observed.get("file_count") != len(item.get("extracted_files", []))
                        or observed_dirs != expected_dirs):
                    raise ValueError(f"Conteúdo lido do ZArchive diverge do manifesto; rollback bloqueado: {destination}")
                shutil.rmtree(verify_root)
                destination.unlink()
                rollback = "zarchive_roundtrip_revalidated_and_removed_while_original_rar_hash_matches"
            except Exception:
                if verify_root.exists():
                    shutil.rmtree(verify_root)
                raise
        elif item["action"] in {"extract", "extract_tree"}:
            expected = {(x["relative_path"], x["size_bytes"], x["sha256"]) for x in item.get("extracted_files", [])}
            observed = {(x["relative_path"], x["size_bytes"], x["sha256"]) for x in current.get("tree_files", [])}
            if current.get("kind") != "directory" or expected != observed:
                raise ValueError(f"Árvore extraída mudou; rollback bloqueado: {destination}")
            if item["action"] == "extract_tree":
                expected_directories = set(item.get("validation", {}).get("extracted_directories", []))
                observed_directories = {x["relative_path"] for x in current.get("tree_directories", [])}
                if expected_directories != observed_directories:
                    raise ValueError(f"Diretórios da árvore PS5 mudaram; rollback bloqueado: {destination}")
            shutil.rmtree(destination)
            rollback = ("tool_extraction_tree_removed_after_exact_paths_sizes_hashes_and_directories_match"
                        if item["action"] == "extract_tree"
                        else "tool_extraction_removed_after_exact_tree_match")
        else:
            if current.get("sha256") != item.get("after", {}).get("sha256"):
                raise ValueError(f"Conteúdo mudou; rollback bloqueado: {destination}")
            source.parent.mkdir(parents=True, exist_ok=True)
            if source_is_symlink:
                if item["action"] != "quarantine" or destination.lstat().st_dev != source.parent.stat().st_dev:
                    raise ValueError(f"Rollback de symlink exige mesma partição e ação quarantine: {destination}")
                os.rename(destination, source)
                rollback = "renamed_symlink_back_after_target_hash_match"
            elif destination.stat().st_dev == source.parent.stat().st_dev:
                os.rename(destination, source)
                rollback = "renamed_back_after_hash_match"
            elif destination.is_file():
                temp = source.with_name(f".{source.name}.rollback-{uuid.uuid4().hex[:8]}")
                shutil.copy2(destination, temp)
                if sha256_file(temp) != item["precondition"]["sha256"]:
                    temp.unlink(missing_ok=True)
                    raise RuntimeError(f"Hash divergente ao restaurar cópia: {source}")
                os.replace(temp, source)
                destination.unlink()
                rollback = "copied_back_verified_then_removed_destination"
            else:
                temp = source.with_name(f".{source.name}.rollback-{uuid.uuid4().hex[:8]}")
                shutil.copytree(destination, temp, symlinks=False)
                if path_snapshot(temp, include_hash=True)["sha256"] != item["precondition"]["sha256"]:
                    shutil.rmtree(temp)
                    raise RuntimeError(f"Hash divergente ao restaurar árvore: {source}")
                os.rename(temp, source)
                shutil.rmtree(destination)
                rollback = "tree_copied_back_verified_then_removed_destination"
        remaining_directories = []
        for name in reversed(item.get("created_directories", [])):
            directory = Path(name)
            try:
                directory.rmdir()
                append_event(operation_id, "created_directory_removed_by_rollback", action="rmdir",
                             status="completed", source=str(directory),
                             details={"item_id": item["item_id"]})
            except OSError:
                remaining_directories.append(str(directory))
        item["remaining_created_directories"] = list(reversed(remaining_directories))
        item["rollback_status"] = "verified"
        item["rollback"] = rollback
        write_manifest(manifest)
        append_event(operation_id, "item_rollback_completed", action=item["action"], status="verified",
                     source=str(destination), destination=str(source),
                     sha256_before=current.get("sha256", ""), sha256_after=item["precondition"].get("sha256", ""),
                     size_before=current.get("size_bytes", ""), size_after=item["precondition"].get("size_bytes", ""),
                     details={"rollback": rollback})
    manifest["status"] = "rolled_back_verified"
    manifest["rollback_status"] = "completed"
    manifest["rolled_back_at"] = now()
    write_manifest(manifest)
    append_event(operation_id, "operation_rolled_back", action="rollback", status="completed",
                 details={"item_count": len(manifest["items"])})
    rebuild_history_index()
    if any(item.get("action") in {"steam_move_link", "steam_save_move_link"}
           for item in manifest["items"]):
        update_steam_project_records(manifest, rolled_back=True)
    if any(item.get("action") in MEDIA_ACTIONS for item in manifest["items"]):
        update_game_media_project_records(manifest, rolled_back=True)
    return manifest


@lru_cache(maxsize=4096)
def _history_index_entry(path_string: str, mtime_ns: int, size: int, legacy: bool) -> dict:
    """Cache only a small index row; changed manifest stat always invalidates it."""
    path = Path(path_string)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        data = {"kind": "unreadable operation", "status": f"parse_error: {exc}"}
    if legacy:
        return {"operation_id": path.name.removesuffix("-manifesto.json"),
                "created_at": data.get("timestamp", data.get("prepared_local", "")),
                "kind": data.get("operation", data.get("title", "legacy operation")),
                "status": data.get("status", "legacy; consult manifest"), "manifest": str(path),
                "item_count": len(data.get("files", data.get("items", [])))}
    return {"operation_id": data.get("operation_id", path.parent.name),
            "created_at": data.get("created_at", ""), "kind": data.get("kind", ""),
            "status": data.get("status", ""), "manifest": str(path),
            "journal": str(path.parent / "events.jsonl"),
            "item_count": len(data.get("items", data.get("files", [])))}


def rebuild_history_index() -> None:
    rows = []
    for path in sorted(OUTPUT_ROOT.glob("operacao-*-manifesto.json")):
        st = path.stat()
        row = dict(_history_index_entry(str(path), st.st_mtime_ns, st.st_size, True))
        stem = path.name.removesuffix("-manifesto.json")
        journals = sorted(OUTPUT_ROOT.glob(stem + "*journal.jsonl"))
        row["journal"] = str(journals[0]) if journals else ""
        rows.append(row)
    if OPS_ROOT.exists():
        for path in sorted(OPS_ROOT.glob("*/manifest.json")):
            st = path.stat()
            rows.append(dict(_history_index_entry(str(path), st.st_mtime_ns, st.st_size, False)))
    rows.sort(key=lambda row: (row["created_at"], row["operation_id"]))
    write_csv_atomic(OPS_ROOT / "history-index.csv", rows,
              ["operation_id", "created_at", "kind", "status", "manifest", "journal", "item_count"])


PENDING_PLAN_STATES = {"prepared", "planned"}


def _retire_plan_items(manifest: dict, status: str, covers, **marks) -> list[str]:
    retired = []
    for item in manifest.get("items", []):
        if item.get("status") in PENDING_PLAN_STATES and covers(item):
            item.update(status=status, **marks)
            retired.append(item.get("item_id", ""))
    return retired


def supersede_prior_plans(new_manifest: dict, whole=None, covers=None) -> list[dict]:
    """Mark older pending plans of the same kind as superseded by a new plan; never deletes manifests.

    whole(old_manifest) -> bool retires the entire plan (a new full audit of the same scope).
    covers(old_item) -> bool retires only pending items the new plan re-evaluated."""
    new_id, kind, created = new_manifest["operation_id"], new_manifest["kind"], new_manifest["created_at"]
    marks = dict(superseded_by=new_id, superseded_at=now())
    retired = []
    for path in sorted(OPS_ROOT.glob("*/manifest.json")):
        if path.parent.name == new_id:
            continue
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if old.get("kind") != kind or old.get("created_at", "") >= created:
            continue
        if whole and old.get("status") in PENDING_PLAN_STATES and whole(old):
            items = _retire_plan_items(old, "superseded", lambda item: True, **marks)
        elif covers:
            items = _retire_plan_items(old, "superseded", covers, **marks)
            if not items:
                continue
        else:
            continue
        if old.get("status") in PENDING_PLAN_STATES and not any(
                item.get("status") in PENDING_PLAN_STATES for item in old.get("items", [])):
            old.update(status="superseded", **marks)
        atomic_json(path, old)
        append_event(old["operation_id"], "plan_superseded", action="plan_supersede", status=old["status"],
                     details={"superseded_by": new_id, "item_ids": items})
        retired.append({"operation_id": old["operation_id"], "items": len(items), "status": old["status"]})
    return retired


def discard_plan(operation_id: str, reason: str, confirmed: bool) -> dict:
    """Withdraw a pending plan by explicit review; manifests and evidence are preserved."""
    if not confirmed:
        raise ValueError("Descarte exige --apply explícito")
    if not reason.strip():
        raise ValueError("Informe o motivo do descarte")
    path = operation_dir(operation_id) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    marks = dict(discarded_at=now(), discard_reason=reason.strip())
    items = _retire_plan_items(manifest, "discarded", lambda item: True, **marks)
    if manifest.get("status") in PENDING_PLAN_STATES:
        manifest.update(status="discarded", **marks)
    elif not items:
        raise ValueError(f"Plano sem itens pendentes: {manifest.get('status')}")
    atomic_json(path, manifest)
    append_event(operation_id, "plan_discarded", action="plan_discard", status=manifest["status"],
                 details={"reason": reason.strip(), "item_ids": items})
    rebuild_history_index()
    return {"operation_id": operation_id, "status": manifest["status"], "discarded_items": len(items)}


COMPRESSION_PROFILES_PATH = TOOL_ROOT / "config" / "compression-profiles.json"
HOST_PROFILE_PATH = PROJECT_ROOT / "frontend" / "RetroFE" / "Config" / "profiles" / "host-linux.json"


def load_compression_data() -> tuple[dict, dict, dict]:
    profiles_doc = json.loads(COMPRESSION_PROFILES_PATH.read_text(encoding="utf-8"))
    host_doc = json.loads(HOST_PROFILE_PATH.read_text(encoding="utf-8"))
    roots_doc = portable_config(TOOL_ROOT / "config" / "roots.json")
    if profiles_doc.get("schema_version") != "1.0":
        raise ValueError("Versão de config de compressão não suportada")
    return profiles_doc, host_doc, roots_doc


def compression_runtime_map(host_doc: dict) -> dict:
    collections = host_doc.get("collections", host_doc.get("systems", {}))
    return {str(value.get("rom_subdir", "")): value
            for value in collections.values() if value.get("rom_subdir")}


def compression_profile_for(profiles_doc: dict, system_id: str, extension: str) -> dict | None:
    for profile in profiles_doc.get("profiles", []):
        system_extensions = profile.get("source_extensions_by_system", {}).get(system_id)
        if system_extensions is None and system_id in profile.get("systems", []):
            system_extensions = profile.get("source_extensions", [])
        if system_extensions is not None and extension.casefold() in {
                str(item).casefold() for item in system_extensions}:
            return profile
    return None


def compression_name_exclusion(profiles_doc: dict, filename: str) -> dict | None:
    for rule in profiles_doc.get("preserve_name_markers", []):
        if rule.get("id") == "modified-or-translated-content":
            parsed = variant_metadata.parse_variant_filename(
                filename, variant_metadata._load_policy(TOOL_ROOT)
            )
            if parsed.get("candidate_by_name"):
                return {**rule, "reason": "Marcador estruturado de variante no nome; preservar para revisão.",
                        "name_evidence": parsed}
            continue
        try:
            if re.search(str(rule.get("regex", "")), filename):
                return rule
        except re.error as exc:
            raise ValueError(f"Regex de exclusão inválido ({rule.get('id')}): {exc}") from exc
    return None


def compression_command(argv_template: list[str], **values) -> list[str]:
    return [str(part).format_map(values) for part in argv_template]


def compression_convert(profile: dict, source: Path, destination: Path, *,
                        archive_member: str = "", rollback: bool = False) -> None:
    if profile.get('conversion_kind')=='zip_rom_repack' and not rollback:
        evidence=compression_archive_tree(profile,source)
        work=PROJECT_ROOT/'work/roms-consolidation'
        work.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='rom-repack-',dir=work) as staging:
            stage=Path(staging)
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    target=stage/PurePosixPath(info.filename.replace('\\','/'))
                    if info.is_dir(): target.mkdir(parents=True,exist_ok=True);continue
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with archive.open(info) as reader,target.open('xb') as writer:shutil.copyfileobj(reader,writer)
            compression_run([str(profile.get('archive_tool','7z')),'a','-t7z','-mx=9','-m0=lzma2','-md=64m','-ms=off',str(destination.resolve()),'.'],cwd=stage)
        if compression_archive_tree(profile,destination)['digest']!=evidence['digest']:
            raise ValueError('Membros/bytes do contêiner divergiram após recompressão')
        return
    if rollback and profile.get("rollback_mode") == "archive_member":
        compression_extract_archive_member(profile, source, archive_member, destination)
        return
    key = "rollback_convert" if rollback else "convert"
    argv_template = profile.get(key, [])
    if not argv_template:
        raise ValueError(f"Conversão não configurada no perfil {profile.get('id')}: {key}")
    argv = compression_command(argv_template, source=str(source), destination=str(destination),
                               path=str(source), source_name=archive_member or source.name,
                               archive_member=archive_member)
    cwd = source.parent if profile.get("working_directory") == "source_parent" else None
    compression_run(argv, cwd=cwd)


def compression_run(argv: list[str], timeout: int = 21600,
                    cwd: Path | None = None) -> subprocess.CompletedProcess:
    if not argv or not shutil.which(argv[0]):
        raise FileNotFoundError(f"Conversor/validador não encontrado: {argv[0] if argv else '(comando vazio)'}")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                            cwd=cwd, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "sem saída")[-5000:]
        raise RuntimeError(f"Comando falhou ({result.returncode}): {shlex.join(argv)}\n{detail}")
    return result


def compression_validate(profile: dict, path: Path, *, phase: str = "target",
                         rollback: bool = False) -> None:
    if rollback:
        key = "rollback_validate"
    elif phase == "source":
        key = "source_validate" if "source_validate" in profile else "validate"
    else:
        key = "validate"
    argv_template = profile.get(key, [])
    if argv_template:
        compression_run(compression_command(argv_template, path=str(path)))


def compression_archive_tree(profile: dict,path: Path) -> dict:
    """Bounded ROM bundle: preserve every file byte and empty directory."""
    listing=archive_members(path)
    if listing['archive_format'] not in {'zip','7z'}:raise ValueError('Repack aceita somente ZIP/7z')
    members=listing['members']
    if len(members)>int(profile.get('maximum_members',512)):raise ValueError('Pacote excede limite de membros')
    total=sum(m['size_bytes'] for m in members if not m['directory'])
    if total>int(profile.get('maximum_expanded_bytes',67108864)):raise ValueError('Pacote excede limite expandido')
    allowed={x.casefold() for x in profile.get('payload_extensions_by_system',{}).get(profile.get('_system_id'),[])}
    auxiliary=set(profile.get('auxiliary_extensions',[]))
    roms=[];files=[];directories=set();seen=set()
    for member in members:
        name=PurePosixPath(member['path']).as_posix().rstrip('/')
        if any(':' in part for part in PurePosixPath(name).parts):raise ValueError('Nome de membro não portável')
        if not name or name.casefold() in seen or member.get('encrypted'):raise ValueError('Membro duplicado/criptografado/inválido')
        seen.add(name.casefold())
        for parent in PurePosixPath(name).parents:
            if str(parent)!='.':directories.add(str(parent))
        if member['directory']:directories.add(name);continue
        ext=PurePosixPath(name).suffix.casefold()
        special=PurePosixPath(name).name.casefold() in {n.casefold() for n in profile.get('auxiliary_filenames',[])}
        if ext not in allowed|auxiliary and not special:raise ValueError(f'Membro não autorizado no pacote ROM: {name}')
        hashes=hash_archive_member(path,listing['archive_format'],member)
        row={'path':name,'size_bytes':hashes['size_bytes'],'sha256':hashes['sha256']};files.append(row)
        if ext in allowed:roms.append(row)
    if len(roms)!=1 or roms[0]['size_bytes']==0:raise ValueError('Repack exige exatamente um payload ROM suportado e não vazio')
    _,host,_=load_compression_data()
    runtime=compression_runtime_map(host).get(profile.get('_system_id'),{})
    extracts={'.'+str(e).lstrip('.').casefold() for e in runtime.get('extract_extensions',[])}
    if (len(files)>1 and runtime.get('adapter')=='retroarch'
            and profile.get('target_extension') in extracts):
        raise ValueError('O launcher RetroArch desta plataforma exige um único arquivo extraído; preservar pacote com auxiliares')
    content={'files':sorted(files,key=lambda r:r['path']),'directories':sorted(directories)}
    return {'digest':hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest(),'rom':roms[0],'expanded_bytes':total,'members':content}


def compression_archive_member_digest(profile: dict, path: Path, member: str) -> str:
    tool = str(profile.get("archive_tool", "7z"))
    executable = shutil.which(tool)
    if not executable:
        raise FileNotFoundError(f"Leitor de arquivo compactado não encontrado: {tool}")
    argv = [executable, "x", "-so", "-spd", "-bd", "-bb0", str(path), member]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    digest = hashlib.sha256()
    total = 0
    assert process.stdout is not None
    while True:
        chunk = process.stdout.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    return_code = process.wait()
    process.stdout.close()
    if process.stderr is not None:process.stderr.close()
    if return_code != 0:
        raise RuntimeError(
            f"Extração do membro falhou ({return_code}): {path}!/{member}\n"
            f"{stderr.decode(errors='replace')[-3000:]}"
        )
    if total == 0:
        raise ValueError(f"Membro extraído vazio: {path}!/{member}")
    return digest.hexdigest()


def compression_extract_archive_member(profile: dict, archive: Path, member: str,
                                        destination: Path) -> None:
    tool = str(profile.get("archive_tool", "7z"))
    executable = shutil.which(tool)
    if not executable:
        raise FileNotFoundError(f"Extrator de arquivo compactado não encontrado: {tool}")
    argv = [executable, "x", "-so", "-bd", "-bb0", str(archive), member]
    with destination.open("wb") as output:
        process = subprocess.Popen(argv, stdout=output, stderr=subprocess.PIPE)
        stderr = process.communicate()[1] or b""
    if process.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"Extração do membro falhou ({process.returncode}): {archive}!/{member}\n"
            f"{stderr.decode(errors='replace')[-3000:]}"
        )
    if not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise ValueError(f"Membro extraído vazio: {archive}!/{member}")


def compression_digest(profile: dict, path: Path, operation_id: str, *, phase: str,
                       rollback: bool = False, archive_member: str = "") -> str:
    if phase == "source":
        kind = profile.get("source_digest_kind", "command" if profile.get("digest") else "sha256_file")
        argv_template = profile.get("digest", [])
    elif phase == "rollback":
        kind = profile.get("rollback_digest_kind", "sha256_file")
        argv_template = profile.get("rollback_digest", [])
    else:
        kind = profile.get("digest_kind", "command" if profile.get("digest") else "sha256_file")
        argv_template = profile.get("digest", [])

    if kind == "sha256_file":
        return sha256_file(path)
    if kind == 'sha256_archive_tree':
        return compression_archive_tree(profile,path)['digest']
    if kind == "sha256_archive_member":
        member = archive_member or str(profile.get("archive_member", ""))
        if member == "source_name":
            member = path.stem + str(profile.get("source_member_extension", ""))
        if not member:
            raise ValueError(f"Membro interno não definido no perfil {profile.get('id')}")
        return compression_archive_member_digest(profile, path, member)
    if kind == "command":
        result = compression_run(compression_command(argv_template, path=str(path)))
        candidates = re.findall(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40,64}(?![0-9A-Fa-f])",
                                result.stdout + "\n" + result.stderr)
        if not candidates:
            raise ValueError(f"Não consegui extrair o digest lógico de {path}: {result.stdout[-1000:]}")
        return candidates[-1].lower()
    if kind == "sha256_extracted_raw":
        work = operation_dir(operation_id) / "staging"
        work.mkdir(parents=True, exist_ok=True)
        extracted = work / f"digest-{uuid.uuid4().hex}.raw"
        try:
            argv = compression_command(profile["digest_extract"], path=str(path),
                                       source=str(path), destination=str(extracted),
                                       digest_output=str(extracted))
            compression_run(argv)
            if not extracted.is_file() or extracted.stat().st_size == 0:
                raise ValueError(f"Extração de validação vazia: {path}")
            return sha256_file(extracted)
        finally:
            extracted.unlink(missing_ok=True)
    raise ValueError(f"Tipo de digest não suportado no perfil {profile.get('id')}: {kind}")


def compression_header(profile: dict, path: Path) -> dict:
    argv_template = profile.get("header", [])
    if not argv_template:
        return {}
    result = compression_run(compression_command(argv_template, path=str(path)))
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Cabeçalho do jogo não é JSON válido: {path}") from exc
    return value if isinstance(value, dict) else {}


def compression_header_comparison(profile: dict, expected: dict, actual: dict) -> dict:
    """Compare configured disc identity fields, excluding target-container metadata."""
    fields = profile.get("header_identity_fields")
    if fields is None:
        fields = sorted(set(expected) | set(actual))
    if not isinstance(fields, list) or not fields or any(not isinstance(field, str) for field in fields):
        raise ValueError(f"Lista header_identity_fields inválida no perfil {profile.get('id')}")
    missing_expected = [field for field in fields if field not in expected]
    missing_actual = [field for field in fields if field not in actual]
    expected_identity = {field: expected[field] for field in fields if field in expected}
    actual_identity = {field: actual[field] for field in fields if field in actual}
    return {
        "fields": fields,
        "expected": expected_identity,
        "actual": actual_identity,
        "missing_expected": missing_expected,
        "missing_actual": missing_actual,
        "matched": not missing_expected and not missing_actual and expected_identity == actual_identity,
    }


def compression_reference_index(paths: list[Path], root: Path) -> dict[str, dict]:
    unique_paths = sorted({str(path): path for path in paths}.values(), key=str)
    if not unique_paths:
        return {}
    if not shutil.which("rg"):
        return {str(path): {"status": "unavailable", "path_matches": [],
                            "note": "rg ausente; mudança não pode ser aplicada sem revisão de referência."}
                for path in unique_paths}
    roots_doc = portable_config(TOOL_ROOT / "config" / "roots.json")
    reference_roots = [Path(item["path"]) for item in roots_doc.get("known_roots", [])
                       if item.get("role") in {"protected_frontend", "protected_hakchi_data"}]
    reference_roots = [candidate for candidate in reference_roots if candidate.exists()]
    esde_lists=PROJECT_ROOT/'frontend/ES-DE/UserData/gamelists'
    if esde_lists.is_dir():reference_roots.append(esde_lists)
    missing_roots = [item["path"] for item in roots_doc.get("known_roots", [])
                     if item.get("role") in {"protected_frontend", "protected_hakchi_data"}
                     and not Path(item["path"]).exists()]
    if not reference_roots:
        return {str(path): {"status": "unavailable", "path_matches": [],
                            "note": "Nenhuma raiz de frontend/Hakchi configurada está acessível."}
                for path in unique_paths}

    owners: dict[str, set[str]] = defaultdict(set)
    for path in unique_paths:
        if not is_within(path, root):
            continue
        owners[str(path)].add(str(path))
        owners[path.relative_to(root).as_posix()].add(str(path))
        owners[path.name].add(str(path))
    found: dict[str, set[str]] = defaultdict(set)
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="romtool-reference-",
                                         suffix=".txt", delete=False) as stream:
            pattern_path = Path(stream.name)
            stream.write("\n".join(owners))
            stream.write("\n")
        try:
            for reference_root in reference_roots:
                checked = subprocess.run(
                    ["rg", "--json", "--hidden", "--fixed-strings", "--file", str(pattern_path),
                     *[arg for ext in ['xml','txt','conf','cfg','ini','json','yaml','yml','m3u','m3u8','cue','gdi','desktop','sh','py','bat','cmd'] for arg in ('--glob','*.'+ext)],
                     "--glob", "!log.txt", "--glob", "!**/*.png", "--glob", "!**/*.jpg",
                     "--glob", "!**/*.jpeg", "--glob", "!**/Docs/**", "--glob", "!**/Manifests/**",
                     "--glob", "!**/logs/**", "--glob", "!**/cache/**", "--glob", "!**/Cache/**", "--glob", "!**/Backups/**",
                     "--glob", "!**/*.log","--glob","!**/*.backup.*","--glob","!**/*.bak.*",str(reference_root)],
                    capture_output=True, text=True, check=False)
                if checked.returncode not in (0, 1):
                    raise RuntimeError((checked.stderr or checked.stdout)[-2000:])
                if checked.returncode == 1:
                    continue
                for line in checked.stdout.splitlines():
                    event = json.loads(line)
                    if event.get("type") != "match":
                        continue
                    data = event.get("data", {})
                    ref_file = data.get("path", {}).get("text", "")
                    for submatch in data.get("submatches", []):
                        needle = submatch.get("match", {}).get("text", "")
                        for owner in owners.get(needle, set()):
                            found[owner].add(ref_file)
        finally:
            pattern_path.unlink(missing_ok=True)
    except Exception as exc:
        return {str(path): {"status": "error", "path_matches": [],
                            "note": f"{type(exc).__name__}: {exc}"}
                for path in unique_paths}

    results = {}
    note = "Busca literal em lote do caminho absoluto/relativo nas raízes protegidas acessíveis de frontends/launchers e Hakchi."
    for path in unique_paths:
        matches = sorted(found.get(str(path), set()))
        status = "referenced" if matches else "checked_none" if not missing_roots else "partial"
        results[str(path)] = {"status": status, "path_matches": matches,
                              "missing_roots": missing_roots, "note": note}
    return results


def compression_reference_check(path: Path, root: Path) -> dict:
    return compression_reference_index([path], root).get(str(path), {
        "status": "unavailable", "path_matches": [], "note": "Checagem de referência sem resultado."
    })


def compression_reference_updates(item: dict) -> tuple[list[dict],list[dict],list[dict]]:
    sid=item['system_id'];source=Path(item['source']);destination=Path(item['destination'])
    platform=PROJECT_ROOT/'roms'/sid
    return variant_organization._path_refs_for_moves(
        {source.name:destination.relative_to(platform).as_posix()},sid,PROJECT_ROOT,
        PROJECT_ROOT/'frontend/ES-DE/UserData',
        {source.name:source.relative_to(platform).as_posix()})


@lru_cache(maxsize=128)
def compression_descriptor_index(system_root: str) -> tuple[tuple[str, str, bytes | None], ...]:
    descriptor_extensions = {".cue", ".m3u", ".m3u8", ".gdi", ".ccd", ".toc"}
    entries = []
    root = Path(system_root)
    if root.is_dir():
        for current, directories, filenames in os.walk(root, followlinks=False):
            base = Path(current)
            directories[:] = [name for name in directories if not (base / name).is_symlink()]
            for filename in filenames:
                descriptor = base / filename
                if descriptor.suffix.casefold() not in descriptor_extensions or descriptor.is_symlink():
                    continue
                try:
                    content = descriptor.read_bytes().lower()
                except OSError:
                    content = None
                entries.append((str(descriptor), descriptor.stem.casefold(), content))
    return tuple(entries)


def compression_sidecar_check(path: Path, root: Path) -> dict:
    source_name = path.name.casefold()
    system_root = root / path.relative_to(root).parts[0]
    hits = [descriptor for descriptor, stem, content in compression_descriptor_index(str(system_root))
            if stem == path.stem.casefold() or content is None
            or source_name.encode(errors="ignore") in content]
    return {"status": "has_sidecar_reference" if hits else "checked_none",
            "matches": sorted(set(hits)),
            "note": "Checagem de descritores CUE/M3U/GDI/CCD/TOC no sistema; não segue symlinks."}


def compression_profile_readiness(profile: dict, system_id: str, runtime_map: dict,
                                  probe_cache: dict) -> tuple[str, str, str]:
    runtime = runtime_map.get(system_id)
    if not runtime:
        return "blocked_runtime_profile_missing", "Sistema ausente no host-linux.json", ""
    if runtime.get("adapter") == "unavailable" or runtime.get("status") == "unavailable_on_host":
        return "blocked_runtime_unavailable", str(runtime.get("reason", "emulador indisponível")), ""
    expected_adapters = profile.get("runtime_adapters", [profile.get("runtime_adapter", "")])
    if runtime.get("adapter") not in expected_adapters:
        return "blocked_runtime_adapter_mismatch", f"adapter={runtime.get('adapter')}", ""
    required_app = profile.get("runtime_app_id", "")
    if required_app and runtime.get("app_id") != required_app:
        return "blocked_runtime_app_mismatch", f"app_id={runtime.get('app_id')}", ""
    extensions = {"." + item.strip().lower().lstrip(".")
                  for item in str(runtime.get("extensions", "")).split(",") if item.strip()}
    target_ext = str(profile.get("target_extension", "")).lower()
    if target_ext not in extensions:
        return "blocked_runtime_format_not_enabled", f"host profile does not list {target_ext}", ""
    tool = profile.get("tool_executable", "")
    if tool and not shutil.which(tool):
        return "blocked_converter_unavailable", f"Executável ausente: {tool}", ""
    probe = profile.get("runtime_probe", profile.get("tool_probe", []))
    probe_key = json.dumps(probe, sort_keys=True)
    if probe and probe_key not in probe_cache:
        try:
            probe_cache[probe_key] = compression_run(probe).stdout.strip()
        except Exception as exc:
            probe_cache[probe_key] = f"ERROR: {type(exc).__name__}: {exc}"
    observed = probe_cache.get(probe_key, f"{tool} disponível" if tool else "")
    if observed.startswith("ERROR:"):
        return "blocked_converter_unavailable", observed, ""
    required_output = str(profile.get("runtime_probe_must_contain", ""))
    if required_output and required_output not in observed:
        return "blocked_runtime_capability_unavailable", f"Probe sem capacidade esperada: {required_output}", observed
    return "eligible", "Formato de destino aceito pelo emulador configurado", observed


def command_compact_plan(args) -> dict:
    profiles_doc, host_doc, roots_doc = load_compression_data()
    canonical_root = Path(roots_doc["canonical_rom_root"]).resolve(strict=True)
    root = Path(args.root).resolve(strict=True)
    ensure_allowed(root)
    is_canonical = is_within(root,canonical_root)
    selected_systems=set(getattr(args,'system_id',[]) or [])
    runtime_map = compression_runtime_map(host_doc)
    probe_cache: dict[str, str] = {}
    operation_id = f"compression-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    candidates, audit_rows = [], []
    known_native_formats = {
        ".chd", ".rvz", ".gcz", ".cso", ".zso", ".nsz", ".xcz", ".wbfs",
        ".adz", ".hdz", ".d6z", ".d7z", ".d8z"
    }
    package_formats = {
        ".zip", ".7z", ".rar", ".pkg", ".nsp", ".xci", ".exfat", ".cia",
        ".vpk", ".pbp", ".wad"
    }
    protected_names = {name.casefold() for name in load_policy().get("protected_names", [])}

    for current, directories, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        directories[:] = sorted(name for name in directories if not (base / name).is_symlink())
        if base==canonical_root and selected_systems:
            directories[:]=[name for name in directories if name in selected_systems]
        for filename in sorted(filenames):
            source = base / filename
            relative = source.relative_to(root).as_posix()
            system_id = source.relative_to(canonical_root).parts[0] if is_canonical else infer_system_id(source,root)
            if selected_systems and system_id not in selected_systems:continue
            ext = source.suffix.casefold()
            row = {"source": relative, "system_id": system_id, "extension": ext,
                   "size_bytes": "", "sha256": "", "status": "unprofiled_preserved",
                   "reason": "Sem perfil de conversão direta confirmado; preservado. Isso não indica corrupção nem lixo.",
                   "profile_id": "", "destination": ""}
            if source.is_symlink():
                row.update(status="protected_symlink", reason="Links simbólicos não são seguidos nem convertidos.")
            elif not source.is_file():
                row.update(status="unsupported_file_type", reason="Não é arquivo regular.")
            else:
                row["size_bytes"] = source.stat().st_size
                if filename.casefold() in protected_names or filename.casefold() == ".directory":
                    row.update(status="project_support_file", reason="Metadado de plataforma preservado.")
                elif ext in known_native_formats:
                    row.update(status="already_native_or_compressed", reason="Formato já comprimido/nativo; não recomprimir como arquivo genérico.")
                elif ext in package_formats and not (ext=='.zip' and getattr(args,'normalize_archives',True) and compression_profile_for(profiles_doc,system_id,ext)):
                    row.update(status="package_or_archive_not_recompressed",
                               reason="Pacote/arquivo já comprimido; isso não prova carregamento direto nem identidade do jogo.")
                else:
                    profile = compression_profile_for(profiles_doc, system_id, ext)
                    exclusion = compression_name_exclusion(profiles_doc, filename)
                    if profile and profile.get('conversion_kind') in {'single_file_archive','zip_rom_repack'}:
                        exclusion=None  # Exact-byte storage conversion does not classify the variant.
                    if exclusion:
                        profile = None
                        row.update(status="blocked_modified_content_review",
                                   reason=exclusion.get("reason", exclusion.get("id", "Conteúdo exige revisão.")))
                    else:
                        profile = compression_profile_for(profiles_doc, system_id, ext)
                    if profile:
                        profile={**profile,'_system_id':system_id}
                        row["profile_id"] = profile["id"]
                        target = source.with_suffix(profile["target_extension"])
                        row["destination"] = target.relative_to(root).as_posix()
                        if not is_canonical:
                            row.update(status="blocked_noncanonical_root", reason="A auditoria aceita outra raiz; a aplicação só altera a biblioteca canônica.")
                        elif source.is_symlink() or source.stat().st_size == 0:
                            row.update(status="blocked_invalid_source", reason="Origem simbólica ou vazia.")
                        elif os.path.lexists(target) and profile.get("conversion_kind") not in {"single_file_archive","zip_rom_repack"}:
                            row.update(status="blocked_destination_exists", reason="Destino já existe; sem sobrescrita.")
                        else:
                            readiness, reason, tool_version = compression_profile_readiness(
                                profile, system_id, runtime_map, probe_cache)
                            row.update(status=readiness, reason=reason)
                            if readiness == "eligible":
                                try:
                                    source_sha = sha256_file(source)
                                    compression_validate(profile, source, phase="source")
                                    source_digest = compression_digest(profile, source, operation_id, phase="source")
                                    bundle=compression_archive_tree(profile,source) if profile.get('conversion_kind')=='zip_rom_repack' else None
                                    reuse_existing=os.path.lexists(target)
                                    destination_precondition=None
                                    if reuse_existing:
                                        reject_symlink_components(target)
                                        if target.is_symlink() or not target.is_file():raise ValueError('Destino existente não é um arquivo regular')
                                        destination_precondition=path_snapshot(target,include_hash=True)
                                        compression_validate(profile,target)
                                        if profile.get('conversion_kind')=='single_file_archive':
                                            members=archive_members(target)['members']
                                            if sum(not m['directory'] for m in members)!=1:
                                                raise ValueError('Destino existente possui auxiliares; revisão de pacote necessária')
                                        target_digest=compression_digest(profile,target,operation_id,phase='target',archive_member=source.name)
                                        if target_digest!=source_digest:raise ValueError('Destino existente tem conteúdo lógico diferente; não sobrescrever')
                                        if path_snapshot(target,include_hash=True)!=destination_precondition:raise ValueError('Destino existente mudou durante a análise')
                                    row["sha256"] = source_sha
                                    header = compression_header(profile, source)
                                    sidecars = compression_sidecar_check(source, canonical_root if is_canonical else root) if profile.get("single_file_only") else {"status": "checked_none", "matches": []}
                                    if sidecars["status"] != "checked_none":
                                        row.update(status="blocked_set_dependency",
                                                   reason=f"Descritor/conjunto associado: {sidecars['matches']}")
                                    else:
                                        free = shutil.disk_usage(source.parent).free
                                        minimum_free = (max(source.stat().st_size * int(profile.get("minimum_free_source_multiplier", 1)),bundle['expanded_bytes']*2 if bundle else 0)
                                                        + 64 * 1024 * 1024)
                                        if free < minimum_free:
                                            row.update(status="blocked_insufficient_space",
                                                       reason=f"Livre {free}; mínimo conservador {minimum_free} bytes.")
                                        else:
                                            candidates.append({
                                                "item_id": f"{operation_id}-{len(candidates) + 1:04d}",
                                                "action": "compress_replace", "status": "prepared",
                                                "system_id": system_id, "profile_id": profile["id"],
                                                "source": str(source), "destination": str(target),
                                                "source_relative": relative,
                                                "destination_relative": target.relative_to(root).as_posix(),
                                                "source_sha256": source_sha,
                                                "source_size_bytes": source.stat().st_size,
                                                "reuse_existing_destination": reuse_existing,
                                                "destination_precondition": destination_precondition,
                                                "archive_member": source.name,
                                                "rom_payload_sha256":bundle['rom']['sha256'] if bundle else source_digest,
                                                "expanded_size_bytes":bundle['expanded_bytes'] if bundle else source.stat().st_size,
                                                "logical_digest": source_digest,
                                                "header": header, "reference_check": {"status": "deferred"},
                                                "runtime_profile_status": runtime_map[system_id].get("status", ""),
                                                "converter_version": tool_version,
                                                "compression": profile.get("compression", {}),
                                                "reason": profile.get("reason", "Conversão lossless para formato diretamente aceito pelo runtime configurado."),
                                                "validation_result": profile.get("source_validation_result", "Origem validada pelo perfil e digest lógico registrado."),
                                                "dependency_check": profile.get("dependency_check", "Dependências do formato avaliadas pelo perfil."),
                                                "reclaimed_bytes": 0
                                            })
                                            row.update(status="pending_reference_batch", reason="Conversão elegível; referências serão verificadas em lote.")
                                except Exception as exc:
                                    row.update(status="blocked_source_validation",
                                               reason=f"{type(exc).__name__}: {exc}")
            audit_rows.append(row)

    # A raw ROM and ZIP may propose the same destination. Prefer the bundle
    # only after proving the ROM bytes equal; preserve the other encoding.
    by_destination=defaultdict(list)
    for item in candidates:by_destination[item['destination']].append(item)
    selected=[]
    rows_by_source={row['source']:row for row in audit_rows}
    for peers in by_destination.values():
        if len(peers)==1:selected.extend(peers);continue
        digests={item['rom_payload_sha256'] for item in peers}
        if len(digests)==1:
            chosen=min(peers,key=lambda i:(Path(i['source']).suffix.casefold()!='.zip',i['source_size_bytes']))
            selected.append(chosen)
            for item in peers:
                if item is not chosen:rows_by_source[item['source_relative']].update(status='equivalent_source_requires_dedup_review',reason='Mesmo payload com outro contêiner planejado; preservar até revisar referências e quarentena.')
        else:
            for item in peers:rows_by_source[item['source_relative']].update(status='blocked_destination_competition',reason='Mesmo destino proposto para ROMs de bytes diferentes; não sobrescrever.')
    candidates=selected
    reference_results = compression_reference_index([Path(item["source"]) for item in candidates], canonical_root if is_canonical else root)
    approved_candidates = []
    rows_by_source = {row["source"]: row for row in audit_rows}
    for item in candidates:
        refs = reference_results.get(item["source"], {"status": "unavailable", "path_matches": [],
                                                       "note": "Resultado de referência ausente."})
        updates,blockers,excluded=compression_reference_updates(item)
        handled={r['path'] for r in updates}|{r['path'] for r in excluded}
        unknown=set(refs.get('path_matches',[]))-handled
        item['frontend_reference_updates']=[{k:v for k,v in r.items() if k not in {'before_bytes','after_bytes'}} for r in updates]
        item['frontend_reference_exclusions']=excluded
        if not blockers and not unknown and refs.get('status') in {'checked_none','referenced'}:
            refs={**refs,'status':'checked_none' if not updates else 'checked_transactional','note':'Referências ativas resolvidas por proprietário e caminho; histórico/documentação não são consumidores.'}
        item["reference_check"] = refs
        row = rows_by_source[item["source_relative"]]
        if refs.get("status") in {"checked_none","checked_transactional"} and not blockers:
            row.update(status="planned", reason="Elegível; referências RetroFE/Pegasus/Hakchi verificadas em lote.")
            approved_candidates.append(item)
        else:
            row.update(status="blocked_reference_check",
                       reason=json.dumps({'blockers':blockers,'unhandled_reference_files':sorted(unknown),
                                          'scan_status':refs.get('status'),'scan_note':refs.get('note','')},ensure_ascii=False))
    candidates = approved_candidates

    opdir = operation_dir(operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    audit_path = opdir / "compression-audit.csv"
    write_csv_atomic(audit_path, audit_rows,
                     ["source", "system_id", "extension", "size_bytes", "sha256", "status",
                      "reason", "profile_id", "destination"])
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "compression",
        "title": "ROM audit and emulator-native lossless compaction plan",
        "created_at": now(), "status": "prepared", "read_only": True,
        "root": str(root), "items": candidates,
        "selected_systems":sorted(selected_systems),"normalize_archives":bool(getattr(args,'normalize_archives',True)),
        "details": {
            "profile_config": str(COMPRESSION_PROFILES_PATH),
            "profile_config_sha256": sha256_file(COMPRESSION_PROFILES_PATH),
            "runtime_profile": str(HOST_PROFILE_PATH),
            "runtime_profile_sha256": sha256_file(HOST_PROFILE_PATH),
            "audit_csv": str(audit_path), "audited_files": len(audit_rows),
            "candidate_count": len(candidates),
            "audit_status_counts": dict(__import__("collections").Counter(x["status"] for x in audit_rows)),
            "policy": {"no_overwrite": True, "no_generic_archives": True,
                       "no_unprofiled_conversion": True, "only_canonical_root_mutable": True,
                       "replace_source_only_after_output_validation": True}
        }
    }
    write_manifest(manifest)
    append_event(operation_id, "compression_plan_created", action="compress_plan", status="prepared",
                 details={"candidate_count": len(candidates), "audited_files": len(audit_rows),
                          "audit_csv": str(audit_path)})
    superseded = supersede_prior_plans(manifest, whole=lambda old: old.get("root") == manifest["root"])
    rebuild_history_index()
    return {"operation_id": operation_id, "kind": "compression", "status": "prepared",
            "candidate_count": len(candidates), "audited_files": len(audit_rows),
            "candidate_source_bytes": sum(int(x["source_size_bytes"]) for x in candidates),
            "superseded_plans": superseded,
            "audit_status_counts": manifest["details"]["audit_status_counts"],
            "manifest": str(opdir / "manifest.json"), "audit_csv": str(audit_path),
            "candidate_sample": [{"source": x["source_relative"], "destination": x["destination_relative"],
                                  "profile": x["profile_id"], "source_bytes": x["source_size_bytes"]}
                                 for x in candidates[:20]]}


def compression_manifest(operation_id: str) -> tuple[dict, Path]:
    path = operation_dir(operation_id) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "compression":
        raise ValueError(f"Operação não é de compressão: {operation_id}")
    return manifest, path


def command_compact_apply(args) -> dict:
    if not args.apply:
        raise ValueError("Aplicação da compactação exige --apply explícito")
    manifest, manifest_path = compression_manifest(args.operation_id)
    if manifest.get("status") != "prepared":
        raise ValueError(f"Plano de compactação não está preparado: {manifest.get('status')}")
    profiles_doc, host_doc, roots_doc = load_compression_data()
    if sha256_file(COMPRESSION_PROFILES_PATH) != manifest["details"]["profile_config_sha256"]:
        raise ValueError("Perfis de conversão mudaram desde o plano; gere um plano novo")
    if sha256_file(HOST_PROFILE_PATH) != manifest["details"]["runtime_profile_sha256"]:
        raise ValueError("Perfil do emulador mudou desde o plano; gere um plano novo")
    canonical_root = Path(roots_doc["canonical_rom_root"]).resolve(strict=True)
    if not is_within(Path(manifest["root"]).resolve(strict=True),canonical_root):
        raise ValueError("Só a raiz canônica pode receber conversões")
    if esde_processes_running():raise RuntimeError('Feche o ES-DE normalmente antes de trocar extensões/referências de ROMs')
    active_frontends = retrofe_processes_running()
    allow_active_frontend = bool(getattr(args, "allow_active_frontend", False))
    active_frontend_reason = str(getattr(args, "active_frontend_reason", "")).strip()
    if active_frontends and not allow_active_frontend:
        raise RuntimeError("Feche o RetroFE antes de trocar extensões de ROM em uso pelo menu")
    if active_frontends and allow_active_frontend and not active_frontend_reason:
        raise ValueError("Override com RetroFE ativo exige --active-frontend-reason para o diário")
    if not active_frontends and allow_active_frontend:
        raise ValueError("--allow-active-frontend só é válido quando um processo RetroFE está ativo")
    if active_frontend_reason and not allow_active_frontend:
        raise ValueError("--active-frontend-reason exige --allow-active-frontend")
    if active_frontends and any(i.get('frontend_reference_updates') for i in manifest.get('items',[])):
        raise RuntimeError('Override não permite alterar referências com RetroFE ativo; feche-o normalmente')
    runtime_map = compression_runtime_map(host_doc)
    probe_cache: dict[str, str] = {}
    prepared_items = [item for item in manifest.get("items", []) if item.get("status") == "prepared"]
    apply_reference_results = compression_reference_index(
        [Path(item["source"]) for item in prepared_items], canonical_root)
    reference_expected={r['path']:r['sha256_before'] for item in prepared_items for r in item.get('frontend_reference_updates',[])}
    for ref,digest in reference_expected.items():
        if sha256_file(Path(ref))!=digest:raise ValueError(f'Referência mudou desde o plano: {ref}')
    manifest["status"] = "running"
    manifest["read_only"] = False
    manifest["started_at"] = now()
    if active_frontends:
        manifest["active_frontend_override"] = {
            "explicit_user_authorization": True,
            "reason": active_frontend_reason,
            "processes_observed": active_frontends,
            "recorded_at": now(),
            "scope": "this compression operation only",
        }
    atomic_json(manifest_path, manifest)
    if active_frontends:
        append_event(args.operation_id, "active_frontend_override_accepted", action="compact_apply",
                     status="explicit_user_authorized", details=manifest["active_frontend_override"])
    failures = []

    for item in manifest.get("items", []):
        if item.get("status") != "prepared":
            continue
        profile = next((x for x in profiles_doc.get("profiles", []) if x.get("id") == item["profile_id"]), None)
        if not profile:
            item.update(status="failed", error="Perfil desapareceu.")
            failures.append(item["error"])
            continue
        profile={**profile,'_system_id':item['system_id']}
        source, destination = Path(item["source"]), Path(item["destination"])
        temp = destination.with_name(f".{destination.name}.{args.operation_id}.partial")
        backup = source.with_name(f".{source.name}.{args.operation_id}.rollback")
        reuse_existing=bool(item.get("reuse_existing_destination"))
        if profile.get('rollback_mode')=='preserved_original' or reuse_existing:
            backup=manifest_path.parent/'rollback/originals'/source.relative_to(canonical_root)
        reference_changes=[];reference_committed=[];reference_recovery_blocked=False
        try:
            ensure_allowed(source)
            ensure_allowed(destination)
            reject_symlink_components(source)
            reject_symlink_components(destination)
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"Origem não é arquivo regular: {source}")
            if sha256_file(source) != item["source_sha256"]:
                raise ValueError(f"SHA-256 mudou desde o plano: {source}")
            exclusion = compression_name_exclusion(profiles_doc, source.name)
            if exclusion and profile.get('conversion_kind') not in {'single_file_archive','zip_rom_repack'}:
                raise ValueError(exclusion.get("reason", "Conteúdo exige revisão de identidade/categoria."))
            expected_profile = compression_profile_for(profiles_doc, item["system_id"], source.suffix)
            expected_destination = source.with_suffix(str(profile.get("target_extension", "")))
            if expected_profile is None or expected_profile.get("id") != profile.get("id"):
                raise ValueError(f"Perfil não corresponde mais à plataforma/formato: {source}")
            if (destination != expected_destination or not is_within(source, canonical_root)
                    or not is_within(destination, canonical_root)):
                raise ValueError(f"Origem/destino não correspondem ao caminho canônico esperado: {source}")
            source_parts = source.relative_to(canonical_root).parts
            if not source_parts or source_parts[0] != item.get("system_id"):
                raise ValueError(f"Sistema no manifesto diverge do caminho da origem: {source}")
            if reuse_existing:
                if destination.is_symlink() or not destination.is_file() or path_snapshot(destination,include_hash=True)!=item['destination_precondition']:
                    raise ValueError('Destino canônico existente mudou desde o plano')
            elif os.path.lexists(destination):
                raise FileExistsError(f'Destino surgiu desde o plano: {destination}')
            if os.path.lexists(temp) or os.path.lexists(backup):
                raise FileExistsError(f"Temporário/rollback já existe para {source}")
            readiness, reason, _ = compression_profile_readiness(profile, item["system_id"], runtime_map, probe_cache)
            if readiness != "eligible":
                raise ValueError(f"Perfil/runtime deixou de estar elegível: {readiness}: {reason}")
            compression_validate(profile, source, phase="source")
            current_digest = compression_digest(profile, source, args.operation_id, phase="source")
            if current_digest != item["logical_digest"]:
                raise ValueError(f"Digest lógico mudou desde o plano: {source}")
            refs = apply_reference_results.get(str(source), {"status": "unavailable", "path_matches": [],
                                                               "note": "Resultado de referência ausente."})
            item["apply_reference_check"] = refs
            reference_changes,ref_blockers,ref_exclusions=compression_reference_updates(item)
            allowed_refs={r['path'] for r in reference_changes}|{r['path'] for r in ref_exclusions}
            if ref_blockers or set(refs.get('path_matches',[]))-allowed_refs or refs.get('status') not in {'checked_none','referenced'}:
                raise ValueError(f"Referência RetroFE surgiu ou a checagem falhou: {refs}")
            if {r['path'] for r in reference_changes}!={r['path'] for r in item.get('frontend_reference_updates',[])}:
                raise ValueError('Conjunto de referências mudou desde o plano')
            for ref in reference_changes:
                if ref['sha256_before']!=reference_expected[ref['path']]:raise ValueError('Referência foi alterada durante a operação')
            if profile.get("single_file_only"):
                sidecars = compression_sidecar_check(source, canonical_root)
                if sidecars.get("status") != "checked_none":
                    raise ValueError(f"Descritor/conjunto associado surgiu ou foi alterado: {sidecars}")
            free = shutil.disk_usage(source.parent).free
            minimum_free = (max(source.stat().st_size * int(profile.get("minimum_free_source_multiplier", 1)),int(item.get('expanded_size_bytes',0))*2)
                            + 64 * 1024 * 1024)
            if free < minimum_free:
                raise OSError(f"Espaço livre caiu abaixo do mínimo: {free} < {minimum_free}")
            item.update(status="converting", partial_path=str(temp), rollback_path=str(backup))
            atomic_json(manifest_path, manifest)
            append_event(args.operation_id, "compression_started", action="compress_replace", status="running",
                         source=str(source), destination=str(destination),
                         sha256_before=item["source_sha256"], size_before=item["source_size_bytes"],
                         details={"profile": profile["id"], "compression": profile.get("compression", {})})
            output_path=destination if reuse_existing else temp
            if not reuse_existing:
                compression_convert(profile, source, temp,archive_member=item.get("archive_member",source.name))
            if not output_path.is_file() or output_path.stat().st_size==0:
                raise ValueError(f'Conversor/destino sem imagem válida: {output_path}')
            compression_validate(profile,output_path)
            output_digest=compression_digest(profile,output_path,args.operation_id,phase='target',archive_member=item.get('archive_member',source.name))
            if output_digest!=item['logical_digest']:
                raise ValueError('Digest lógico do destino difere da origem; preservar ambos')
            output_header = compression_header(profile, output_path)
            header_check = (compression_header_comparison(profile, item["header"], output_header)
                            if item.get("header") else None)
            if header_check is not None:
                item["header_after"] = output_header
                item["header_identity_check"] = header_check
                atomic_json(manifest_path, manifest)
            if header_check is not None and not header_check["matched"]:
                raise ValueError(f"Identidade do disco diverge após conversão: {source} -> {temp}")
            output_size = output_path.stat().st_size
            if not reuse_existing and output_size >= item["source_size_bytes"]:
                temp.unlink()
                item.update(status="completed_no_savings", output_size_bytes=output_size,
                            output_sha256="", reclaimed_bytes=0,
                            result="Conversão íntegra, mas não menor; origem mantida.")
                append_event(args.operation_id, "compression_no_savings", action="compress_replace", status="skipped",
                             source=str(source), destination=str(destination),
                             sha256_before=item["source_sha256"], size_before=item["source_size_bytes"],
                             size_after=output_size, details={"profile": profile["id"]})
                atomic_json(manifest_path, manifest)
                continue

            if sha256_file(source)!=item['source_sha256']:raise ValueError('Origem mudou durante a conversão')
            backup.parent.mkdir(parents=True,exist_ok=True)
            actual_refs=[]
            for index,ref in enumerate(reference_changes):
                saved=manifest_path.parent/'rollback/references'/f"{item['item_id']}-{index:03d}.before"
                saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(ref['before_bytes'])
                actual_refs.append({k:v for k,v in ref.items() if k not in {'before_bytes','after_bytes'}}|{'rollback_snapshot':str(saved)})
            item['applied_reference_updates']=actual_refs

            item.update(status="committing", staged_sha256=sha256_file(output_path),
                        output_size_bytes=output_size, rollback_path=str(backup))
            atomic_json(manifest_path, manifest)
            if reuse_existing and path_snapshot(destination,include_hash=True)!=item['destination_precondition']:
                raise ValueError('Destino canônico mudou antes do commit')
            os.replace(source, backup)
            try:
                if not reuse_existing:os.replace(temp, destination)
                after = path_snapshot(destination, include_hash=True)
                if after["sha256"] != item["staged_sha256"]:
                    raise ValueError(f"Destino mudou durante a confirmação: {destination}")
                for ref in reference_changes:
                    if sha256_file(Path(ref['path']))!=ref['sha256_before']:raise ValueError('Referência mudou antes do commit')
                    variant_organization._atomic_write(Path(ref['path']),ref['after_bytes'])
                    reference_committed.append(ref)
                    reference_expected[ref['path']]=ref['sha256_after']
            except Exception:
                for ref in reversed(reference_committed):
                    if sha256_file(Path(ref['path']))!=ref['sha256_after']:
                        reference_recovery_blocked=True
                        raise RuntimeError('Referência mudou durante recuperação; originais preservados')
                    try:variant_organization._atomic_write(Path(ref['path']),ref['before_bytes'])
                    except Exception:
                        reference_recovery_blocked=True
                        raise
                    reference_expected[ref['path']]=ref['sha256_before']
                reference_committed=[]
                if not reuse_existing and destination.exists() and destination.is_file():
                    if sha256_file(destination) != item.get("staged_sha256"):
                        raise RuntimeError(
                            f"Destino mudou durante a recuperação; preservados {destination} e {backup}"
                        )
                    destination.unlink()
                if not os.path.lexists(source):
                    os.replace(backup, source)
                raise
            item.update(status="completed_verified", after=after,
                        reclaimed_bytes=0 if reuse_existing or profile.get('rollback_mode')=='preserved_original' else item["source_size_bytes"]-after["size_bytes"],
                        active_library_reduction_bytes=item['source_size_bytes'] if reuse_existing else item['source_size_bytes']-after['size_bytes'],
                        net_disk_bytes_freed=0 if reuse_existing else -after['size_bytes'] if profile.get('rollback_mode')=='preserved_original' else item['source_size_bytes']-after['size_bytes'],
                        logical_digest_after=output_digest, header_after=output_header,
                        result="Representação canônica validada; referências coordenadas e rollback registrado.")
            atomic_json(manifest_path, manifest)
            append_event(args.operation_id, "rom_compressed_native_format", action="compress_replace",
                         status="completed_verified", source=str(source), destination=str(destination),
                         sha256_before=item["source_sha256"], sha256_after=after["sha256"],
                         size_before=item["source_size_bytes"], size_after=after["size_bytes"],
                         details={"profile": profile["id"], "logical_digest": output_digest,
                                  "reclaimed_bytes": item["reclaimed_bytes"], "rollback": "reverse converter"})
            if profile.get('rollback_mode')=='preserved_original' or reuse_existing:
                item['preserved_original']={'path':str(backup),'sha256':item['source_sha256'],'size_bytes':item['source_size_bytes']}
            else:backup.unlink()
        except Exception as exc:
            if os.path.lexists(backup) and not os.path.lexists(source) and not reference_recovery_blocked:
                try:
                    if not reuse_existing and os.path.lexists(destination):
                        if not destination.is_file() or sha256_file(destination) != item.get("staged_sha256"):
                            raise RuntimeError(f"Destino inesperado; rollback preservado em {backup}")
                        destination.unlink()
                    os.replace(backup, source)
                except Exception as recovery_exc:
                    item["recovery_error"] = f"{type(recovery_exc).__name__}: {recovery_exc}"
            if os.path.lexists(temp):
                temp.unlink(missing_ok=True)
            item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            failures.append(item["error"])
            append_event(args.operation_id, "compression_failed", action="compress_replace", status="failed",
                         source=str(source), destination=str(destination),
                         details={"error": item["error"]})
        atomic_json(manifest_path, manifest)

    manifest["completed_at"] = now()
    manifest['reference_final_hashes']=reference_expected
    manifest["status"] = "failed_partial" if failures else "completed_verified"
    atomic_json(manifest_path, manifest)
    append_event(args.operation_id, "compression_operation_finished", action="compress_replace",
                 status=manifest["status"], details={"failures": failures})
    rebuild_history_index()
    return {"operation_id": args.operation_id, "kind": "compression", "status": manifest["status"],
            "item_status_counts": dict(__import__("collections").Counter(x.get("status", "unknown") for x in manifest.get("items", []))),
            "reclaimed_bytes": sum(int(x.get("reclaimed_bytes", 0)) for x in manifest.get("items", [])),
            "active_library_reduction_bytes":sum(int(x.get('active_library_reduction_bytes',0)) for x in manifest.get('items',[])),
            "net_disk_bytes_freed":sum(int(x.get('net_disk_bytes_freed',0)) for x in manifest.get('items',[])),
            "errors": failures, "manifest": str(manifest_path)}


def command_compact_verify(args) -> dict:
    manifest, manifest_path = compression_manifest(args.operation_id)
    profiles_doc, _, _ = load_compression_data()
    profiles = {x["id"]: x for x in profiles_doc.get("profiles", [])}
    checks = []
    for item in manifest.get("items", []):
        status = item.get("status")
        if status == "completed_no_savings":
            source = Path(item["source"])
            ok = source.is_file() and sha256_file(source) == item["source_sha256"]
            checks.append({"source": item["source_relative"], "status": "verified_no_savings" if ok else "source_changed"})
            continue
        if status != "completed_verified":
            checks.append({"source": item.get("source_relative", item.get("source", "")), "status": f"not_applicable:{status}"})
            continue
        profile = {**profiles[item["profile_id"]],'_system_id':item['system_id']}
        destination = Path(item["destination"])
        try:
            if destination.is_symlink() or not destination.is_file():
                raise ValueError("Destino ausente/não regular")
            current = path_snapshot(destination, include_hash=True)
            if current["sha256"] != item["after"]["sha256"]:
                raise ValueError("SHA-256 do destino mudou")
            compression_validate(profile, destination)
            digest = compression_digest(profile, destination, args.operation_id, phase="target",
                                       archive_member=item.get("archive_member", ""))
            if digest != item["logical_digest"]:
                raise ValueError("Digest lógico mudou")
            original=item.get('preserved_original')
            if original and (not Path(original['path']).is_file() or sha256_file(Path(original['path']))!=original['sha256']):
                raise ValueError('Original preservado para rollback ausente/divergente')
            header = compression_header(profile, destination)
            header_check = (compression_header_comparison(profile, item["header"], header)
                            if item.get("header") else None)
            if header_check is not None and not header_check["matched"]:
                raise ValueError("Cabeçalho/identidade mudou")
            checks.append({"source": item["source_relative"], "destination": item["destination_relative"],
                           "status": "verified", "size_bytes": current["size_bytes"],
                           "sha256": current["sha256"], "logical_digest": digest,
                           "header_identity_check": header_check})
        except Exception as exc:
            checks.append({"source": item.get("source_relative", ""), "status": "failed",
                           "error": f"{type(exc).__name__}: {exc}"})
    for path,digest in manifest.get('reference_final_hashes',{}).items():
        if not Path(path).is_file() or sha256_file(Path(path))!=digest:
            checks.append({'source':path,'status':'failed','error':'Referência atual mudou; reverter operações dependentes antes do rollback.'})
    failed = [x for x in checks if x["status"] in {"failed", "source_changed"}]
    manifest["last_verification"] = {"at": now(), "status": "failed" if failed else "completed_verified",
                                     "checks": checks}
    atomic_json(manifest_path, manifest)
    append_event(args.operation_id, "compression_verification_finished", action="compress_verify",
                 status=manifest["last_verification"]["status"], details={"checks": checks})
    rebuild_history_index()
    return {"operation_id": args.operation_id, "status": manifest["last_verification"]["status"],
            "checks": checks, "manifest": str(manifest_path)}


def command_compact_rollback(args) -> dict:
    if not args.rollback:
        raise ValueError("Rollback de compactação exige --rollback explícito")
    manifest, manifest_path = compression_manifest(args.operation_id)
    if manifest.get("status") not in {"completed_verified", "failed_partial"}:
        raise ValueError(f"Operação não está disponível para rollback: {manifest.get('status')}")
    profiles_doc, _, _ = load_compression_data()
    profiles = {x["id"]: x for x in profiles_doc.get("profiles", [])}
    if any(frontend_processes_running().values()):
        raise RuntimeError("Feche RetroFE e ES-DE antes de reverter extensões de ROM")
    failures = []
    for item in reversed(manifest.get("items", [])):
        if item.get("status") != "completed_verified":
            continue
        profile = {**profiles[item["profile_id"]],'_system_id':item['system_id']}
        source, destination = Path(item["source"]), Path(item["destination"])
        temp = source.with_name(f".{source.name}.{args.operation_id}.rollback-partial")
        backup = destination.with_name(f".{destination.name}.{args.operation_id}.rollback")
        try:
            ensure_allowed(source)
            ensure_allowed(destination)
            reject_symlink_components(source)
            reject_symlink_components(destination)
            if os.path.lexists(source) or destination.is_symlink() or not destination.is_file():
                raise ValueError("Rollback exige origem ausente e destino comprimido regular")
            if sha256_file(destination) != item["after"]["sha256"]:
                raise ValueError("Destino mudou desde a conversão; rollback bloqueado")
            reference_data=[]
            for ref in item.get('applied_reference_updates',[]):
                ref_path=Path(ref['path']);before=Path(ref['rollback_snapshot']).read_bytes()
                if sha256_file(ref_path)!=ref['sha256_after'] or hashlib.sha256(before).hexdigest()!=ref['sha256_before']:
                    raise ValueError('Referência/snapshot mudou; reverter dependências primeiro')
                reference_data.append((ref_path,before,ref_path.read_bytes()))
            if item.get('reuse_existing_destination'):
                original=Path(item['preserved_original']['path'])
                if original.is_symlink() or not original.is_file() or sha256_file(original)!=item['source_sha256']:
                    raise ValueError('Duplicata original preservada ausente/divergente')
                os.replace(original,source)
                try:
                    for ref_path,before,_ in reference_data:variant_organization._atomic_write(ref_path,before)
                except Exception:
                    for ref_path,_,after_data in reference_data:variant_organization._atomic_write(ref_path,after_data)
                    os.replace(source,original)
                    raise
                item.update(status='rolled_back',restored=path_snapshot(source,include_hash=True),rolled_back_at=now(),canonical_peer_retained=True)
                atomic_json(manifest_path,manifest)
                append_event(args.operation_id,'rom_duplicate_restored',action='compress_replace',status='rolled_back',source=str(original),destination=str(source),sha256_after=item['source_sha256'])
                continue
            if profile.get('rollback_mode')=='preserved_original':
                original=Path(item['preserved_original']['path'])
                if not original.is_file() or sha256_file(original)!=item['source_sha256']:
                    raise ValueError('Original ZIP preservado não corresponde ao plano')
                applied=manifest_path.parent/'rollback/applied'/destination.relative_to(PROJECT_ROOT/'roms')
                if applied.exists():raise FileExistsError(applied)
                applied.parent.mkdir(parents=True,exist_ok=True)
                os.replace(destination,applied)
                try:
                    os.replace(original,source)
                    for ref_path,before,_ in reference_data:variant_organization._atomic_write(ref_path,before)
                except Exception:
                    for ref_path,_,after_data in reference_data:variant_organization._atomic_write(ref_path,after_data)
                    if source.exists():os.replace(source,original)
                    os.replace(applied,destination)
                    raise
                item.update(status='rolled_back',restored=path_snapshot(source,include_hash=True),rolled_back_at=now(),preserved_conversion=str(applied))
                atomic_json(manifest_path,manifest)
                append_event(args.operation_id,'rom_archive_restored',action='compress_replace',status='rolled_back',source=str(destination),destination=str(source),sha256_after=item['source_sha256'])
                continue
            if os.path.lexists(temp) or os.path.lexists(backup):
                raise FileExistsError("Temporário/rollback já existe")
            item.update(status="rolling_back", rollback_partial_path=str(temp), rollback_backup_path=str(backup))
            atomic_json(manifest_path, manifest)
            free = shutil.disk_usage(source.parent).free
            minimum_free = (int(item["source_size_bytes"]) + 64 * 1024 * 1024)
            if free < minimum_free:
                raise OSError(f"Espaço insuficiente para rollback: {free} < {minimum_free}")
            compression_convert(profile, destination, temp,
                                archive_member=item.get("archive_member", source.name), rollback=True)
            if not temp.is_file() or temp.stat().st_size == 0:
                raise ValueError("Conversor de rollback não gerou uma imagem")
            compression_validate(profile, temp, rollback=True)
            digest = compression_digest(profile, temp, args.operation_id, phase="rollback", rollback=True)
            if digest != item["logical_digest"]:
                raise ValueError("Digest lógico diverge no rollback")
            if item.get("header"):
                rollback_header = compression_header(profile, temp)
                rollback_header_check = compression_header_comparison(profile, item["header"], rollback_header)
                item["rollback_header_identity_check"] = rollback_header_check
                atomic_json(manifest_path, manifest)
                if not rollback_header_check["matched"]:
                    raise ValueError("Identidade diverge no rollback")
            os.replace(destination, backup)
            try:
                os.replace(temp, source)
                restored = path_snapshot(source, include_hash=True)
                if restored["sha256"] != item["source_sha256"]:
                    raise ValueError("Rollback lógico preservou o digest, mas não reconstruiu os bytes originais")
                for ref_path,before,_ in reference_data:variant_organization._atomic_write(ref_path,before)
            except Exception:
                for ref_path,_,after_data in reference_data:variant_organization._atomic_write(ref_path,after_data)
                if os.path.lexists(source) and source.is_file():
                    if sha256_file(source) != item.get("source_sha256"):
                        raise RuntimeError(
                            f"Origem restaurada diverge; preservados {source} e {backup}"
                        )
                    source.unlink()
                os.replace(backup, destination)
                raise
            backup.unlink()
            item.update(status="rolled_back", restored=restored, rolled_back_at=now())
            append_event(args.operation_id, "rom_compression_rolled_back", action="compress_replace",
                         status="rolled_back", source=str(destination), destination=str(source),
                         sha256_after=restored["sha256"], size_after=restored["size_bytes"],
                         details={"logical_digest": digest, "original_sha256_reconstructed":
                                  restored["sha256"] == item["source_sha256"]})
        except Exception as exc:
            if os.path.lexists(backup) and not os.path.lexists(destination):
                os.replace(backup, destination)
            if os.path.lexists(temp):
                temp.unlink(missing_ok=True)
            item.update(status="rollback_failed", rollback_error=f"{type(exc).__name__}: {exc}")
            failures.append(item["rollback_error"])
        atomic_json(manifest_path, manifest)
    manifest["status"] = "rollback_failed" if failures else "rolled_back"
    manifest["rolled_back_at"] = now()
    atomic_json(manifest_path, manifest)
    append_event(args.operation_id, "compression_rollback_finished", action="compress_rollback",
                 status=manifest["status"], details={"errors": failures})
    rebuild_history_index()
    return {"operation_id": args.operation_id, "status": manifest["status"],
            "errors": failures, "manifest": str(manifest_path)}


def command_record_change(args) -> dict:
    details = json.loads(args.details_json)
    operation_id = f"environment-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = operation_dir(operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "environment_change",
        "created_at": now(), "completed_at": now(), "status": "completed",
        "change_type": args.change_type, "reason": args.reason, "details": details,
        "project_files_changed": False,
    }
    atomic_json(opdir / "manifest.json", manifest)
    append_event(operation_id, "environment_change_recorded", action=args.change_type,
                 status="completed", details={"reason": args.reason, **details})
    rebuild_history_index()
    return manifest


def command_record_artifacts(args) -> dict:
    entries = json.loads(args.artifacts_json)
    if not isinstance(entries, list) or not entries:
        raise ValueError("artifacts-json deve ser uma lista não vazia")
    artifacts = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict) or not entry.get("path"):
            raise ValueError(f"Registro de artefato inválido: {entry!r}")
        path = Path(entry["path"])
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        reject_symlink_components(path)
        if not is_within(path, PROJECT_ROOT) or not path.is_file():
            raise ValueError(f"Artefato precisa ser um arquivo existente dentro do projeto: {path}")
        st = path.stat()
        artifacts.append({
            "path": str(path.relative_to(PROJECT_ROOT)),
            "change": entry.get("change", "created_or_updated"),
            "size_bytes": st.st_size,
            "sha256": sha256_file(path),
        })
    operation_id = f"artifacts-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = operation_dir(operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "change",
        "title": args.change_type, "created_at": now(), "completed_at": now(),
        "status": "completed", "read_only": True, "project_files_changed": True,
        "reason": args.reason, "items": artifacts,
        "details": {"change_type": args.change_type, "artifact_count": len(artifacts)},
    }
    defer_history_index = getattr(args, "defer_history_index", False)
    if defer_history_index:
        manifest["history_index_refresh"] = "deferred"
    atomic_json(opdir / "manifest.json", manifest)
    append_event(operation_id, "project_artifacts_recorded", action=args.change_type,
                 status="completed", details={"reason": args.reason, "artifacts": artifacts})
    if not defer_history_index:
        rebuild_history_index()
    return manifest


def steam_reference_paths(source: Path, install_dir: str) -> list[str]:
    rg = shutil.which("rg")
    if not rg:
        raise FileNotFoundError("ripgrep é necessário para verificar referências antes da mudança de caminho")
    search_roots = [path for path in (PROJECT_ROOT / "frontend", PROJECT_ROOT / "roms") if path.exists()]
    if not search_roots:
        return []
    command = [rg, "--hidden", "--no-messages", "--fixed-strings", "--files-with-matches",
               "--max-count", "1", "-e", str(source), "-e", f"steamapps/common/{install_dir}"]
    for pattern in ("*.xml", "*.conf", "*.cfg", "*.ini", "*.json", "*.m3u", "*.cue",
                    "*.gdi", "*.sub", "*.desktop", "*.sh", "*.txt", "*.vdf", "*.acf", "*.csv"):
        command.extend(["--glob", pattern])
    command.extend(str(path) for path in search_roots)
    result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"Busca de referências Steam falhou: {result.stderr.strip()}")
    return sorted({str(Path(value).resolve().relative_to(PROJECT_ROOT)) for value in result.stdout.splitlines()},
                  key=str.casefold)[:100]


def steam_action_for_row(row: dict) -> dict:
    if row.get("status") != "eligible_game" or not row.get("eligible"):
        raise ValueError(f"App Steam não elegível para migração: {row.get('appid')} ({row.get('status')})")
    source, destination = Path(row["source_path"]), Path(row["destination_path"])
    manifest = Path(row["manifest_path"])
    references = steam_reference_paths(source, row["install_dir"])
    steam_evidence = {
        "appid": row["appid"], "library_path": row["library_path"],
        "steamapps_path": row["steamapps_path"], "manifest_path": row["manifest_path"],
        "install_dir": row["install_dir"], "manifest_name": row["manifest_name"],
        "official_name": row["official_name"], "app_type": row["app_type"],
        "genres": row.get("genres", []), "categories": row.get("categories", []),
        "metadata_source": row.get("metadata_source", ""),
        "metadata_fetched_at": row.get("metadata_fetched_at", ""),
        "registered_by": row.get("registered_by", []),
        "library_is_steam_root": row.get("library_is_steam_root", False),
        "manifest_sha256": sha256_file(manifest),
        "project_references_preserved_by_symlink": references,
    }
    return {
        "action": "steam_move_link", "source": str(source), "destination": str(destination),
        "system_id": "steam", "item_identity": f"{row['official_name']} (Steam AppID {row['appid']})",
        "classification": "game",
        "reason": (f"AppID {row['appid']} consta em appmanifest instalado de Steam library registrada; "
                   f"Steam Store confirma type=game e título {row['official_name']!r}."),
        "reference_check": "not_applicable",
        "validation_result": "AppID, StateFlags instalado, diretório common/<installdir>, tipo e nome oficial conferidos; árvore SHA-256 registrada no plano.",
        "dependency_check": ("Manter appmanifest, libraryfolders, compatdata/Proton, shadercache, workshop, "
                             "saves e dados do usuário nos caminhos Steam originais; o caminho common/<installdir> "
                             "permanece como symlink para o destino canônico."),
        "steam": steam_evidence,
    }


def prepare_steam_plan(rows: list[dict], title: str) -> dict:
    if not rows:
        raise ValueError("Nenhum jogo Steam elegível para planejar")
    if steam_centralizer.steam_processes_running():
        raise RuntimeError("Feche o Steam e todos os jogos antes de hashear e planejar as pastas de instalação")
    actions = [steam_action_for_row(row) for row in rows]
    plan_path = OUTPUT_ROOT / "analysis" / f"steam-plan-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    atomic_json(plan_path, {"title": title, "actions": actions})
    return plan_actions(plan_path)


def steam_save_action_for_row(row: dict) -> dict:
    if row.get("status") != "eligible_save" or not row.get("eligible"):
        raise ValueError(f"Save Steam não elegível: {row.get('appid')} ({row.get('status')})")
    source, destination = Path(row["source_path"]), Path(row["destination_path"])
    steam = dict(row["steam"])
    steam["catalog_key"] = row["catalog_key"]
    return {
        "action": "steam_save_move_link", "source": str(source), "destination": str(destination),
        "system_id": "steam", "item_identity": f"{row['official_name']} save (Steam AppID {row['appid']})",
        "classification": "support",
        "reason": (f"Save associado ao jogo Steam AppID {row['appid']} por marcador Auto-Cloud ou entrada "
                   "remotecache.vdf corroborada por caminho, tamanho e SHA-1; centralizar sem mudar o caminho "
                   "que o Steam/jogo usa."),
        "reference_check": "not_applicable",
        "validation_result": (f"Origem {row['source_kind']} identificada; AppID, appmanifest, tipo de jogo e "
                              "marcador/manifesto Steam Cloud serão revalidados no plano e na aplicação."),
        "dependency_check": ("O appmanifest, remotecache.vdf e perfil compatdata permanecem no Steam; a origem "
                             "fica como symlink absoluto para saves/steam/<título oficial>. Não mover o prefixo Proton."),
        "steam": steam,
    }


def record_steam_save_inventory(rows: list[dict], evidence: dict) -> dict:
    recorded = record_analysis("inventory", "Steam Cloud and Auto-Cloud saves", rows,
                               OUTPUT_ROOT / "analysis", evidence)
    catalog_path = OUTPUT_ROOT / "catalogo-arquivos.csv"
    with catalog_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        catalog_rows = list(reader)
    if not fields:
        raise ValueError(f"Cabeçalho do catálogo global está ausente: {catalog_path}")
    state_path = OUTPUT_ROOT / "estado-final-verificacao.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    steam_state = state.setdefault("steam_centralization", {
        "canonical_root": "roms/steam", "canonical_saves_root": "saves/steam",
        "updated_at": "", "last_operation_id": "", "games": {}})
    steam_state["canonical_saves_root"] = "saves/steam"
    pending = steam_state.setdefault("pending_saves", {})
    now_value = now()
    changed_catalog = False
    eligible_count = 0
    for save in rows:
        if save.get("status") != "eligible_save" or not save.get("eligible"):
            continue
        source = Path(save["source_path"])
        destination = Path(save["destination_path"])
        snapshot = steam_payload_snapshot(source)
        steam = save["steam"]
        source_root = Path(save["source_root"])
        try:
            relative = source.relative_to(source_root).as_posix()
        except ValueError:
            relative = source.as_posix()
        row = {
            "system_id": "steam", "source": "steam_cloud_or_autocloud",
            "source_root": str(source_root), "relative_path": relative,
            "absolute_path": str(source), "file_name": source.name,
            "game_or_set_hint": save.get("official_name", ""),
            "variant_hint": save["catalog_key"],
            "function_hint": "Steam Cloud/Auto-Cloud save; candidate inventoried before centralization",
            "inventory_category": "steam_save_cloud_candidate",
            "extension": "[directory]" if snapshot["kind"] == "directory" else source.suffix,
            "size_bytes": str(snapshot["size_bytes"]), "sha256": snapshot["sha256"],
            "hash_status": "SHA-256 integral do arquivo/árvore",
            "hash_evidence": f"romtool Steam save discovery {recorded['operation_id']}",
            "dependencies": (f"Steam AppID {save['appid']}; {save['source_kind']}; manifesto de Cloud/Auto-Cloud "
                             "preservado; caminho original será mantido por symlink"),
            "integrity_status": "origem e conteúdo inventariados; ainda não movido",
            "destination_proposal": str(destination),
            "action": "inventariado; plano de centralização pendente",
            "metadata_recheck": f"Steam Store type={steam.get('app_type')}; AppID {save['appid']}",
            "current_absolute_path": str(source),
            "current_path_status": "candidato confirmado por Auto-Cloud/Steam Cloud; origem preservada",
        }
        existing = next((candidate for candidate in catalog_rows
                         if candidate.get("system_id") == "steam"
                         and candidate.get("variant_hint") == save["catalog_key"]), None)
        if existing is None:
            catalog_rows.append(row)
            changed_catalog = True
        elif any(existing.get(key, "") != value for key, value in row.items()):
            existing.update(row)
            changed_catalog = True
        pending[save["catalog_key"]] = {
            "appid": save["appid"], "name": save["official_name"],
            "source_path": str(source), "destination_path": str(destination),
            "source_kind": save["source_kind"], "sha256": snapshot["sha256"],
            "size_bytes": snapshot["size_bytes"], "status": "candidate_inventoried",
            "analysis_operation_id": recorded["operation_id"], "updated_at": now_value,
        }
        eligible_count += 1

    known_games = steam_state.get("games", {})
    for issue in evidence.get("unsupported_or_unmatched", []):
        raw_source = str(issue.get("source_path", ""))
        source = Path(raw_source) if raw_source else None
        if not source or not os.path.lexists(source) or source.is_symlink():
            continue
        try:
            snapshot = steam_payload_snapshot(source)
        except (OSError, ValueError):
            snapshot = {"kind": "unclassified", "size_bytes": 0, "sha256": ""}
        appid = str(issue.get("appid", ""))
        game = known_games.get(appid, {})
        source_root = source.parent
        variant = f"Steam AppID {appid} pending {issue.get('status', 'unclassified')}:{source}"
        row = {
            "system_id": "steam", "source": "steam_local_state_pending",
            "source_root": str(source_root), "relative_path": source.name,
            "absolute_path": str(source), "file_name": source.name,
            "game_or_set_hint": game.get("name", f"Steam AppID {appid}"),
            "variant_hint": variant,
            "function_hint": "estado local Steam sem prova suficiente para classificar como save; preservado",
            "inventory_category": "steam_save_unclassified",
            "extension": "[directory]" if snapshot.get("kind") == "directory" else source.suffix,
            "size_bytes": str(snapshot.get("size_bytes", 0)), "sha256": snapshot.get("sha256", ""),
            "hash_status": "SHA-256 de inventário" if snapshot.get("sha256") else "hash pendente; origem preservada",
            "hash_evidence": f"romtool Steam save discovery {recorded['operation_id']}",
            "dependencies": str(issue.get("reason", "")),
            "integrity_status": "não movido; classificação manual pendente",
            "destination_proposal": "",
            "action": "preservado; sem plano de movimento",
            "metadata_recheck": f"Steam AppID {appid}",
            "current_absolute_path": str(source),
            "current_path_status": f"não classificado: {issue.get('status', 'unclassified')}",
        }
        existing = next((candidate for candidate in catalog_rows
                         if candidate.get("system_id") == "steam"
                         and candidate.get("variant_hint") == variant), None)
        if existing is None:
            catalog_rows.append(row)
            changed_catalog = True
        elif any(existing.get(key, "") != value for key, value in row.items()):
            existing.update(row)
            changed_catalog = True

    steam_state["unresolved_save_evidence"] = evidence.get("unsupported_or_unmatched", [])
    steam_state["last_save_discovery_operation_id"] = recorded["operation_id"]
    steam_state["last_save_discovery_at"] = now_value
    if changed_catalog:
        catalog_tmp = catalog_path.with_name(catalog_path.name + ".tmp")
        with catalog_tmp.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(catalog_rows)
        os.replace(catalog_tmp, catalog_path)
    atomic_json(state_path, state)
    changed = [str(state_path.relative_to(PROJECT_ROOT))]
    if changed_catalog:
        changed.append(str(catalog_path.relative_to(PROJECT_ROOT)))
    artifact_record = command_record_artifacts(argparse.Namespace(
        change_type="steam-save-discovery-records",
        reason=f"Inventário prévio dos saves Steam Cloud/Auto-Cloud; análise {recorded['operation_id']}.",
        artifacts_json=json.dumps(changed, ensure_ascii=False)))
    return {"analysis": recorded, "eligible_save_count": eligible_count,
            "candidate_catalog_rows_changed": changed_catalog,
            "project_artifact_record_id": artifact_record["operation_id"],
            "unresolved_save_evidence": evidence.get("unsupported_or_unmatched", [])}


def discover_steam_save_rows(offline: bool, scan_mounts: bool) -> tuple[list[dict], dict]:
    apps, app_evidence = steam_centralizer.collect_installed_apps(
        PROJECT_ROOT, steam_centralizer.app_metadata_cache_path(PROJECT_ROOT),
        online=not offline, scan_mounts=scan_mounts)
    rows, evidence = steam_centralizer.discover_installed_saves(PROJECT_ROOT, apps,
                                                                scan_mounts=scan_mounts)
    evidence["installed_app_status_counts"] = dict(
        __import__("collections").Counter(row.get("status", "unknown") for row in apps))
    evidence["installed_app_discovery"] = {
        "official_roots": app_evidence.get("official_roots", []),
        "libraries": app_evidence.get("steam_libraries", []),
        "offline_metadata": bool(offline),
    }
    evidence["status_counts"] = dict(__import__("collections").Counter(
        row.get("status", "unknown") for row in rows))
    evidence["eligible_save_count"] = sum(row.get("status") == "eligible_save" for row in rows)
    return rows, evidence


def prepare_steam_save_plan(rows: list[dict], title: str) -> dict:
    candidates = [row for row in rows if row.get("status") == "eligible_save" and row.get("eligible")]
    if not candidates:
        raise ValueError("Nenhum save Steam confirmado e elegível para planejar")
    if steam_centralizer.steam_processes_running():
        raise RuntimeError("Feche o Steam e todos os jogos antes de hashear e planejar saves")
    actions = [steam_save_action_for_row(row) for row in candidates]
    plan_path = OUTPUT_ROOT / "analysis" / f"steam-saves-plan-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    atomic_json(plan_path, {"title": title, "actions": actions})
    return plan_actions(plan_path)


def command_steam_saves_discover(args) -> dict:
    rows, evidence = discover_steam_save_rows(args.offline, not args.no_mounted_scan)
    inventory = record_steam_save_inventory(rows, evidence)
    return {"operation_id": inventory["analysis"]["operation_id"],
            "status": inventory["analysis"]["status"], "item_count": len(rows),
            "eligible_save_count": inventory["eligible_save_count"],
            "status_counts": evidence["status_counts"],
            "unsupported_or_unmatched": evidence.get("unsupported_or_unmatched", []),
            "manifest": inventory["analysis"]["manifest"],
            "csv": inventory["analysis"]["csv"],
            "project_artifact_record_id": inventory["project_artifact_record_id"]}


def command_steam_saves_plan(args) -> dict:
    rows, evidence = discover_steam_save_rows(args.offline, not args.no_mounted_scan)
    candidates = [row for row in rows if row.get("status") == "eligible_save"]
    if args.app_id:
        candidates = [row for row in candidates if row.get("appid") == str(args.app_id)]
        if args.library:
            chosen_library = str(Path(args.library).expanduser().resolve())
            candidates = [row for row in candidates if row.get("library_path") == chosen_library]
        if len({row.get("library_path") for row in candidates}) > 1:
            raise ValueError(f"AppID {args.app_id} aparece em várias libraries; informe --library")
    inventory = record_steam_save_inventory(rows, evidence)
    if not candidates:
        return {"status": "no_eligible_saves", "requested_app_id": args.app_id or "all",
                "eligible_save_count": 0, "candidate_rows": rows,
                "discovery_operation_id": inventory["analysis"]["operation_id"]}
    title = ("Steam Cloud/Auto-Cloud saves centralization" if args.all_games else
             f"Steam AppID {args.app_id} saves centralization")
    planned = prepare_steam_save_plan(candidates, title)
    planned["discovery_operation_id"] = inventory["analysis"]["operation_id"]
    planned["unresolved_save_evidence"] = evidence.get("unsupported_or_unmatched", [])
    write_manifest(planned)
    return planned


def command_steam_discover(args) -> dict:
    cache_path = steam_centralizer.app_metadata_cache_path(PROJECT_ROOT)
    rows, evidence = steam_centralizer.collect_installed_apps(
        PROJECT_ROOT, cache_path, online=not args.offline,
        scan_mounts=not args.no_mounted_scan)
    evidence["status_counts"] = dict(__import__("collections").Counter(row.get("status", "unknown") for row in rows))
    evidence["eligible_game_count"] = sum(row.get("status") == "eligible_game" for row in rows)
    recorded = record_analysis("inventory", "Steam roots, libraries and installed apps", rows,
                               OUTPUT_ROOT / "analysis", evidence)
    return {key: recorded[key] for key in ("operation_id", "status", "item_count", "manifest", "csv")} | {
        "eligible_game_count": evidence["eligible_game_count"],
        "status_counts": evidence["status_counts"],
        "official_roots": evidence["official_roots"],
        "steam_libraries": evidence["steam_libraries"],
        "unmounted_libraries": evidence["unmounted_libraries"],
        "mounted_block_devices": evidence["mounted_block_devices"],
        "discovery_errors": evidence["discovery_errors"],
    }


def command_steam_plan(args) -> dict:
    rows, evidence = steam_centralizer.collect_installed_apps(
        PROJECT_ROOT, steam_centralizer.app_metadata_cache_path(PROJECT_ROOT),
        online=not args.offline, scan_mounts=not args.no_mounted_scan)
    candidates = [row for row in rows if row.get("status") == "eligible_game"]
    if args.app_id:
        candidates = [row for row in candidates if row.get("appid") == str(args.app_id)]
        if args.library:
            chosen_library = str(Path(args.library).expanduser().resolve())
            candidates = [row for row in candidates if row.get("library_path") == chosen_library]
        if len(candidates) > 1:
            raise ValueError(f"AppID {args.app_id} aparece em várias libraries; informe --library")
    if not candidates:
        return {"status": "no_eligible_games", "requested_app_id": args.app_id or "all",
                "eligible_count": 0, "candidates": rows, "evidence": evidence}
    title = "Steam game installs centralization" if args.all_games else f"Steam AppID {args.app_id} centralization"
    return prepare_steam_plan(candidates, title)


def _write_atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def update_steam_project_records(manifest: dict, rolled_back: bool) -> dict:
    items = [item for item in manifest.get("items", [])
             if item.get("action") in {"steam_move_link", "steam_save_move_link"}]
    if not items:
        return {"changed": []}
    catalog_path = OUTPUT_ROOT / "catalogo-arquivos.csv"
    with catalog_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        catalog_rows = list(reader)
    if not fields:
        raise ValueError(f"Cabeçalho do catálogo global está ausente: {catalog_path}")

    now_value = now()
    for item in items:
        steam = item["steam"]
        appid = str(steam["appid"])
        source, destination = Path(item["source"]), Path(item["destination"])
        is_save = item["action"] == "steam_save_move_link"
        current_path = source if rolled_back else destination
        if is_save:
            current_root = Path(steam["source_root"]) if rolled_back else (PROJECT_ROOT / "saves")
        else:
            current_root = Path(steam["library_path"]) if rolled_back else (PROJECT_ROOT / "roms")
        relative = current_path.relative_to(current_root).as_posix()
        tree = item.get("before" if rolled_back else "after", item.get("precondition", {}))
        state = ("rollback verificado; caminho original restaurado" if rolled_back else
                 "centralizado; caminho original mantido por symlink")
        catalog_key = steam.get("catalog_key", f"Steam AppID {appid}") if is_save else f"Steam AppID {appid}"
        row = {
            "system_id": "steam",
            "source": "steam_cloud_or_autocloud_save" if is_save else "instalacao_steam_oficial",
            "source_root": str(current_root), "relative_path": relative,
            "absolute_path": str(current_path), "file_name": current_path.name,
            "game_or_set_hint": steam.get("official_name", item.get("item_identity", "")),
            "variant_hint": catalog_key,
            "function_hint": ("save Steam Cloud/Auto-Cloud centralizado com caminho Steam preservado" if is_save
                              else "jogo Steam instalado oficialmente; pasta de instalação centralizada"),
            "inventory_category": "steam_save_payload" if is_save else "steam_game_directory",
            "extension": ("[directory]" if tree.get("kind") == "directory" else current_path.suffix),
            "size_bytes": str(tree.get("size_bytes", 0)), "sha256": tree.get("sha256", ""),
            "hash_status": ("SHA-256 integral do arquivo/árvore; caminhos, modos, tamanhos e conteúdo"
                            if is_save else "SHA-256 integral da árvore; caminhos, modos, tamanhos e conteúdo"),
            "hash_evidence": f"romtool operation {manifest['operation_id']}",
            "dependencies": (f"AppID {appid}; {steam.get('source_kind', '')}; appmanifest preservado em "
                             f"{steam['manifest_path']}; remotecache/compatdata preservados; origem via symlink"
                             if is_save else
                             f"AppID {appid}; appmanifest preservado em {steam['manifest_path']}; "
                             "compatdata, Proton, shadercache, workshop e saves preservados"),
            "integrity_status": ("conteúdo conferido por SHA-256 antes/depois; sem teste de gravação do jogo"
                                 if is_save else "árvore conferida por SHA-256 antes/depois; lançamento interativo não executado"),
            "destination_proposal": str(destination),
            "action": "rollback verificado" if rolled_back else "move + symlink Steam",
            "metadata_recheck": f"Steam Store type={steam.get('app_type')}; AppID {appid}",
            "current_absolute_path": str(current_path), "current_path_status": state,
        }
        existing = next((candidate for candidate in catalog_rows
                         if candidate.get("system_id") == "steam"
                         and (candidate.get("variant_hint") == catalog_key
                              or candidate.get("current_absolute_path") in {str(source), str(destination)})), None)
        if existing is None:
            catalog_rows.append(row)
        else:
            existing.update(row)

    catalog_tmp = catalog_path.with_name(catalog_path.name + ".tmp")
    with catalog_tmp.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(catalog_rows)
    os.replace(catalog_tmp, catalog_path)

    state_path = OUTPUT_ROOT / "estado-final-verificacao.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    steam_state = state.setdefault("steam_centralization", {
        "canonical_root": "roms/steam", "updated_at": "", "last_operation_id": "", "games": {}})
    steam_state["updated_at"] = now_value
    steam_state["last_operation_id"] = manifest["operation_id"]
    games = steam_state.setdefault("games", {})
    for item in items:
        steam = item["steam"]
        appid = str(steam["appid"])
        if item["action"] == "steam_save_move_link":
            game_state = games.setdefault(appid, {
                "appid": appid, "name": steam.get("official_name", ""),
                "app_type": steam.get("app_type", ""), "steam_library": steam.get("library_path", ""),
                "saves": {},
            })
            save_key = steam.get("catalog_key", item["source"])
            save_rows = game_state.setdefault("saves", {})
            save_rows[save_key] = {
                "source_path": item["source"], "canonical_path": item["destination"],
                "current_path": item["source"] if rolled_back else item["destination"],
                "source_kind": steam.get("source_kind", ""),
                "status": "rollback_verified" if rolled_back else "centralized",
                "sha256": item.get("before" if rolled_back else "after", {}).get("sha256", ""),
                "size_bytes": item.get("before" if rolled_back else "after", {}).get("size_bytes", 0),
                "operation_id": manifest["operation_id"], "updated_at": now_value,
                "retained_backup": item.get("retained_backup", ""),
            }
            steam_state.setdefault("pending_saves", {}).pop(save_key, None)
            game_state["updated_at"] = now_value
        else:
            games[appid] = {
                "appid": appid, "name": steam.get("official_name", ""),
                "app_type": steam.get("app_type", ""),
                "steam_library": steam.get("library_path", ""),
                "steam_install_path": item["source"],
                "canonical_path": item["destination"],
                "current_path": item["source"] if rolled_back else item["destination"],
                "status": "rollback_verified" if rolled_back else "centralized",
                "tree_sha256": item.get("before" if rolled_back else "after", {}).get("sha256", ""),
                "size_bytes": item.get("before" if rolled_back else "after", {}).get("size_bytes", 0),
                "operation_id": manifest["operation_id"],
                "updated_at": now_value,
                "retained_backup": item.get("retained_backup", ""),
            }
    atomic_json(state_path, state)

    report_path = OUTPUT_ROOT / "RELATORIO-CONSOLIDACAO.md"
    report = report_path.read_text(encoding="utf-8")
    contains_saves = any(item["action"] == "steam_save_move_link" for item in items)
    marker = f"<!-- steam-operation:{manifest['operation_id']}{':saves' if contains_saves else ''}{':rollback' if rolled_back else ''} -->"
    if marker not in report:
        heading = ("rollback de saves" if rolled_back else "centralização de saves") if contains_saves else ("rollback" if rolled_back else "centralização")
        subject = "saves" if contains_saves else "jogos instalados"
        lines = ["", marker, f"## Steam — {heading} de {subject} ({now_value})", "",
                 f"- Operação: `{manifest['operation_id']}`; itens: {len(items)}.",
                 ("- Cada origem Steam permaneceu disponível por symlink para `saves/steam/<título oficial>`; "
                  "appmanifest, remotecache.vdf e o restante do prefixo Proton ficaram nos caminhos Steam." if contains_saves else
                  "- O appmanifest e os dados Proton/compatdata, shader cache, Workshop e saves permaneceram na biblioteca Steam."),
                 ("- Saves confirmados por marcador Auto-Cloud ou pelo remotecache.vdf com caminho, tamanho e SHA-1 local." if contains_saves else
                  "- Os caminhos `steamapps/common/<installdir>` continuam apontando para os jogos canônicos por symlink."),
                 "- O conteúdo foi comparado por SHA-256 antes e depois; nenhum save não confirmado foi movido."]
        if contains_saves:
            for issue in manifest.get("unresolved_save_evidence", []):
                lines.append(f"- AppID `{issue.get('appid', '')}` pendente `{issue.get('status', 'unclassified')}`: "
                             f"`{issue.get('source_path', issue.get('relative_path', 'caminho não localizado'))}`; "
                             "preservado sem movimento.")
        for item in items:
            steam = item["steam"]
            label = f"AppID `{steam['appid']}` — {steam.get('official_name', item['item_identity'])}"
            if contains_saves:
                label += f" ({steam.get('source_kind', 'save')})"
            lines.append(f"- {label}: "
                         f"`{item['source']}` → `{item['destination']}`; "
                         f"sha256 `{item.get('after', {}).get('sha256', item.get('precondition', {}).get('sha256', ''))}`.")
            if item.get("retained_backup"):
                lines.append(f"  - Backup de recuperação preservado: `{item['retained_backup']}`.")
        report = report.rstrip() + "\n" + "\n".join(lines) + "\n"
        _write_atomic_text(report_path, report)
    unresolved = manifest.get("unresolved_save_evidence", []) if contains_saves else []
    pending_marker = f"<!-- steam-save-pending:{manifest['operation_id']} -->"
    if unresolved and pending_marker not in report:
        pending_lines = ["", pending_marker,
                         f"### Evidências de saves Steam preservados para classificação ({now_value})", "",
                         f"- Análise de descoberta: `{manifest.get('discovery_operation_id', 'não registrada')}`."]
        for issue in unresolved:
            pending_lines.append(f"- AppID `{issue.get('appid', '')}` — `{issue.get('status', 'unclassified')}`: "
                                 f"`{issue.get('source_path', issue.get('relative_path', 'caminho não localizado'))}`; "
                                 "preservado sem movimento.")
        report = report.rstrip() + "\n" + "\n".join(pending_lines) + "\n"
        _write_atomic_text(report_path, report)

    changed = [str(catalog_path.relative_to(PROJECT_ROOT)), str(state_path.relative_to(PROJECT_ROOT)),
               str(report_path.relative_to(PROJECT_ROOT))]
    args = argparse.Namespace(
        change_type="steam-centralization-catalog-update",
        reason=f"Atualização dos registros globais da operação Steam {manifest['operation_id']}.",
        artifacts_json=json.dumps(changed, ensure_ascii=False),
    )
    artifact_record = command_record_artifacts(args)
    manifest["project_artifact_record_id"] = artifact_record["operation_id"]
    manifest["project_records_updated"] = changed
    write_manifest(manifest)
    return {"changed": changed, "artifact_operation_id": artifact_record["operation_id"]}


def update_game_media_rekey_records(manifest: dict, rolled_back: bool) -> dict:
    items = [item for item in manifest.get("items", []) if item.get("action") == "media_rekey_link"]
    if not items:
        return {"changed": []}
    catalog_path = OUTPUT_ROOT / "catalogo-arquivos.csv"
    with catalog_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        rows = list(reader)
    if not fields:
        raise ValueError(f"Cabeçalho do catálogo global está ausente: {catalog_path}")
    updated_paths = []
    for item in items:
        previous = Path(item["source"])
        canonical = Path(item["destination"])
        old_path, new_path = (canonical, previous) if rolled_back else (previous, canonical)
        count = 0
        for row in rows:
            if row.get("source") != "RetroFE_game_media" or row.get("destination_proposal") != str(old_path):
                continue
            row["destination_proposal"] = str(new_path)
            row["current_path_status"] = (
                f"rollback verified; payload restored to {new_path}" if rolled_back else
                f"canonical payload renamed to {new_path}; old media path remains a relative symlink")
            row["metadata_recheck"] = (str(row.get("metadata_recheck", "")) +
                                        f"; canonical media rename operation {manifest['operation_id']} "
                                        f"({item.get('media', {}).get('identity_evidence', '')})").strip(" ;")
            count += 1
        updated_paths.append({"previous_path": str(previous), "canonical_path": str(canonical),
                              "catalog_rows_updated": count, "sha256": item.get("after", {}).get(
                                  "sha256", item.get("precondition", {}).get("sha256", ""))})
    write_csv_atomic(catalog_path, rows, fields)

    state_path = OUTPUT_ROOT / "estado-final-verificacao.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    media_state = state.get("game_media_centralization_20261025", {})
    state_items = media_state.get("items", [])
    for item in items:
        previous, canonical = str(Path(item["source"])), str(Path(item["destination"]))
        old_path, new_path = (canonical, previous) if rolled_back else (previous, canonical)
        for row in state_items:
            if row.get("canonical_path") == old_path:
                row["canonical_path"] = new_path
        rekeys = media_state.setdefault("canonical_path_renames", [])
        rekeys[:] = [row for row in rekeys if row.get("previous_path") != previous]
        rekeys.append({"previous_path": previous, "canonical_path": canonical,
                       "system_id": item.get("system_id", ""),
                       "media_type": item.get("media", {}).get("media_type", ""),
                       "game_title_hint": item.get("media", {}).get("game_title_hint", ""),
                       "sha256": item.get("after", {}).get("sha256", item.get("precondition", {}).get("sha256", "")),
                       "status": "rolled_back" if rolled_back else "renamed_and_alias_preserved",
                       "operation_id": manifest["operation_id"]})
    state["game_media_centralization_20261025"] = media_state
    atomic_json(state_path, state)

    report_path = OUTPUT_ROOT / "RELATORIO-CONSOLIDACAO.md"
    report = report_path.read_text(encoding="utf-8")
    marker = f"<!-- game-media-rekey:{manifest['operation_id']}:{'rollback' if rolled_back else 'apply'} -->"
    if marker not in report:
        title = "Rollback verificado de nomes canônicos de mídia" if rolled_back else "Alinhamento de nomes canônicos de mídia"
        section = ["", marker, f"## {title} ({now()})", "",
                   f"- Operação `{manifest['operation_id']}`: {len(items)} payloads de mídia renomeados; "
                   "o caminho canônico antigo permanece como symlink relativo.",
                   "- As referências do RetroFE continuam resolvendo sem alterar configurações; SHA-256 foi conferido antes e depois.",
                   "- Esta etapa só renomeia payloads cuja associação de título foi validada; mídia sem título único mantém basename de origem."]
        if rolled_back:
            section[1] = marker
        report = report.rstrip() + "\n" + "\n".join(section) + "\n"
        _write_atomic_text(report_path, report)

    changed = [catalog_path, state_path, report_path]
    args = argparse.Namespace(
        change_type="game-media-canonical-rekey-records",
        reason=f"Atualização incremental dos caminhos canônicos de mídia da operação {manifest['operation_id']}.",
        artifacts_json=json.dumps([str(path.relative_to(PROJECT_ROOT)) for path in changed], ensure_ascii=False),
    )
    artifact_record = command_record_artifacts(args)
    manifest["project_artifact_record_id"] = artifact_record["operation_id"]
    manifest["project_records_updated"] = [str(path.relative_to(PROJECT_ROOT)) for path in changed]
    write_manifest(manifest)
    return {"changed": [str(path.relative_to(PROJECT_ROOT)) for path in changed],
            "artifact_operation_id": artifact_record["operation_id"], "catalog_rows_updated": updated_paths}


def update_game_media_project_records(manifest: dict, rolled_back: bool) -> dict:
    items = [item for item in manifest.get("items", []) if item.get("action") in MEDIA_ACTIONS]
    if not items:
        return {"changed": []}
    if all(item.get("action") == "media_rekey_link" for item in items):
        return update_game_media_rekey_records(manifest, rolled_back)
    analysis = manifest.get("analysis", {})
    pending = [dict(row) for row in analysis.get("pending_items", [])]
    if rolled_back:
        for item in items:
            alias = media_item_alias(item)
            media = item.get("media", {})
            original = item.get("alias_precondition", {}) if item["action"] == "media_repair_link" else item.get("precondition", {})
            repaired_item = item["action"] == "media_repair_link"
            pending.append({
                "path": str(alias), "source_collection": alias.relative_to(MEDIA_SOURCE_ROOT).parts[0],
                "file_name": alias.name, "system_id": item.get("system_id", ""),
                "system_candidates": [item.get("system_id", "")], "media_type": media.get("media_type", ""),
                "size_bytes": original.get("size_bytes", 0), "sha256": original.get("sha256", ""),
                "validation_status": ("invalid_original_restored_after_rollback" if repaired_item
                                       else "valid_payload_centralization_rolled_back"),
                "status": "pending_centralization_after_rollback",
                "reason": ("Rollback verificado restaurou o original que falhou na validação; o backup válido permanece preservado." if repaired_item
                           else "Rollback verificado restaurou o arquivo de mídia original; ele permanece fora da raiz canônica."),
                "next_step": "Refazer o plano de mídia após conferir o estado do frontend e do destino canônico.",
            })
    now_value = now()
    source_key = "RetroFE_game_media"
    catalog_path = OUTPUT_ROOT / "catalogo-arquivos.csv"
    with catalog_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        catalog_rows = list(reader)
    if not fields:
        raise ValueError(f"Cabeçalho do catálogo global está ausente: {catalog_path}")

    media_catalog_rows = []
    grouped_by_system = defaultdict(list)
    grouped_by_title_type = defaultdict(list)
    resolved_aliases = set()
    for item in items:
        source, destination = Path(item["source"]), Path(item["destination"])
        alias = media_item_alias(item)
        media = item.get("media", {})
        media_type = str(media.get("media_type", ""))
        original = item.get("alias_precondition", {}) if item["action"] == "media_repair_link" else item.get("precondition", {})
        current_payload = original if rolled_back else item.get("after", original)
        current_path = alias if rolled_back else destination
        current_status = ("rollback verificado; arquivo e caminho RetroFE restaurados" if rolled_back else
                          f"centralizado; caminho RetroFE preservado por symlink relativo para {destination}")
        refs = media.get("frontend_references", [])
        dependency_note = ("Referência de frontend preservada no caminho original por symlink relativo. "
                           + ("Configurações: " + "; ".join(
                               str(row.get("path", row)) if isinstance(row, dict) else str(row)
                               for row in refs)
                              if refs else "A busca por caminho de arquivo não é requisito porque nenhuma configuração foi editada."))
        if item["action"] == "media_repair_link":
            dependency_note += f" Backup validado preservado em {source}; bytes defeituosos originais em quarentena reversível."
        row = {
            "system_id": item.get("system_id", ""), "source": source_key,
            "source_root": str(MEDIA_SOURCE_ROOT),
            "relative_path": alias.relative_to(MEDIA_SOURCE_ROOT).as_posix(),
            "absolute_path": str(alias), "file_name": alias.name,
            "game_or_set_hint": media.get("game_title_hint", destination.stem),
            "variant_hint": (f"origem={alias.name}; canônico={destination.name}" if alias.name != destination.name
                             else "basename preservado; identidade sem renomeação pendente"),
            "function_hint": (f"documentação de patch/tradução relacionada ao jogo ({media_type})" if media_type == "patch"
                              else f"mídia RetroFE do jogo/coleção ({media_type or 'não classificada'})"),
            "inventory_category": "patch_metadata" if media_type == "patch" else "game_media_candidate",
            "extension": alias.suffix.casefold(),
            "size_bytes": str(current_payload.get("size_bytes", 0)),
            "sha256": current_payload.get("sha256", ""),
            "hash_status": "SHA-256 integral do payload canônico; origem validada antes/depois",
            "hash_evidence": f"romtool operation {manifest['operation_id']}",
            "dependencies": dependency_note,
            "integrity_status": ("arquivo restaurado por rollback; validação original preservada no manifesto" if rolled_back
                                 else media.get("media_validation", "validado antes da migração; SHA-256 preservado")),
            "destination_proposal": str(destination),
            "action": "rollback verificado" if rolled_back else item["action"],
            "metadata_recheck": f"tipo={media_type}; identidade: {media.get('identity_evidence', '')}",
            "current_absolute_path": str(current_path), "current_path_status": current_status,
        }
        media_catalog_rows.append(row)
        if row["system_id"]:
            grouped_by_system[row["system_id"]].append(row)
            grouped_by_title_type[(row["system_id"], row["game_or_set_hint"], media.get("media_type", ""))].append(row)
        resolved_aliases.add(str(alias))

    pending_by_path = {}
    for row in pending:
        path = str(Path(row.get("path", "")))
        if path:
            pending_by_path[path] = row
    for row in pending_by_path.values():
        path = Path(row["path"])
        if not path.is_absolute():
            raise ValueError(f"Pendência de mídia precisa de caminho absoluto de origem: {path}")
        media_type = str(row.get("media_type", ""))
        system_id = str(row.get("system_id", ""))
        size = int(row.get("size_bytes", 0) or 0)
        digest = str(row.get("sha256", ""))
        candidates = row.get("system_candidates", [])
        proposed = ""
        if system_id and media_type:
            destination_root = METADATA_DEST_ROOT if media_type == "patch" else MEDIA_DEST_ROOT
            proposed = str(destination_root / system_id / media_type / path.name)
        pending_state = str(row.get("status", "pending"))
        reason = str(row.get("reason", "Classificação/validação de mídia pendente."))
        validation = str(row.get("validation_status", "não validado"))
        dependencies = ("Arquivo preservado no caminho atual para manter a referência do RetroFE; nenhuma alteração de configuração. "
                       + ("Sistemas candidatos: " + ", ".join(map(str, candidates)) + ". " if candidates else "")
                       + str(row.get("next_step", "")))
        catalog_row = {
            "system_id": system_id, "source": source_key,
            "source_root": str(MEDIA_SOURCE_ROOT),
            "relative_path": path.relative_to(MEDIA_SOURCE_ROOT).as_posix() if is_within(path, MEDIA_SOURCE_ROOT) else str(path),
            "absolute_path": str(path), "file_name": row.get("file_name", path.name),
            "game_or_set_hint": row.get("game_title_hint", path.stem),
            "variant_hint": "origem preservada; classificação/validação pendente",
            "function_hint": f"mídia RetroFE pendente ({media_type or 'tipo não resolvido'})",
            "inventory_category": "empty_media_placeholder" if size == 0 else "game_media_candidate",
            "extension": path.suffix.casefold(), "size_bytes": str(size), "sha256": digest,
            "hash_status": "SHA-256 integral" if digest else "hash indisponível",
            "hash_evidence": str(analysis.get("evidence_path", manifest.get("source_plan", ""))),
            "dependencies": dependencies, "integrity_status": validation,
            "destination_proposal": proposed, "action": pending_state,
            "metadata_recheck": reason,
            "current_absolute_path": str(path), "current_path_status": "preservado na origem; sem link ou destino canônico",
        }
        media_catalog_rows.append(catalog_row)
        if system_id:
            grouped_by_system[system_id].append(catalog_row)
            grouped_by_title_type[(system_id, catalog_row["game_or_set_hint"], media_type)].append(catalog_row)

    # Upsert only the game-media rows; all historical ROM catalog entries remain untouched.
    other_rows = [row for row in catalog_rows
                  if not (row.get("source") == source_key and row.get("inventory_category") in
                  {"game_media_candidate", "empty_media_placeholder", "patch_metadata"})]
    catalog_rows = other_rows + media_catalog_rows
    write_csv_atomic(catalog_path, catalog_rows, fields)

    systems_path = OUTPUT_ROOT / "catalogo-sistemas.csv"
    with systems_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        system_fields = reader.fieldnames or []
        system_rows = list(reader)
    if not system_fields:
        raise ValueError(f"Cabeçalho do catálogo de sistemas está ausente: {systems_path}")
    system_rows = [row for row in system_rows
                   if not (row.get("source") == source_key and row.get("source_root") == str(MEDIA_SOURCE_ROOT))]
    for system_id, rows in sorted(grouped_by_system.items()):
        type_counts = __import__("collections").Counter(row["function_hint"].split("(")[-1].rstrip(")") for row in rows)
        system_rows.append({
            "system_id": system_id, "source": source_key, "source_root": str(MEDIA_SOURCE_ROOT),
            "file_count": str(len(rows)),
            "logical_bytes": str(sum(int(row.get("size_bytes", 0) or 0) for row in rows)),
            "functions": "; ".join(f"{key}: {value}" for key, value in sorted(type_counts.items())),
            "sample_paths": "; ".join(row["absolute_path"] for row in rows[:6]),
        })
    write_csv_atomic(systems_path, system_rows, system_fields)

    sets_path = OUTPUT_ROOT / "catalogo-conjuntos.csv"
    with sets_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        set_fields = reader.fieldnames or []
        set_rows = list(reader)
    if not set_fields:
        raise ValueError(f"Cabeçalho do catálogo de conjuntos está ausente: {sets_path}")
    media_set_keys = {(system_id, title, f"media_type={media_type}")
                      for system_id, title, media_type in grouped_by_title_type}
    set_rows = [row for row in set_rows if not (
        row.get("sources", "") == source_key and
        (row.get("system_id", ""), row.get("game_or_set_hint", ""), row.get("variant_hint", "")) in media_set_keys)]
    for (system_id, title, media_type), rows in sorted(grouped_by_title_type.items()):
        set_rows.append({
            "system_id": system_id, "game_or_set_hint": title,
            "variant_hint": f"media_type={media_type}",
            "function_hint": f"mídia de jogo RetroFE ({media_type})",
            "file_count": str(len(rows)),
            "logical_bytes": str(sum(int(row.get("size_bytes", 0) or 0) for row in rows)),
            "sources": source_key, "sha256_known_files": str(sum(bool(row.get("sha256")) for row in rows)),
            "sample_paths": "; ".join(row["absolute_path"] for row in rows[:6]),
            "all_paths_in": "catalogo-arquivos.csv; filtrar source=RetroFE_game_media e system_id",
        })
    write_csv_atomic(sets_path, set_rows, set_fields)

    pending_path = OUTPUT_ROOT / "roms-pendentes-validacao-exclusao.csv"
    with pending_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        pending_fields = reader.fieldnames or []
        pending_rows = list(reader)
    if not pending_fields:
        raise ValueError(f"Cabeçalho da lista global de pendências está ausente: {pending_path}")
    pending_rows = [row for row in pending_rows if not (
        row.get("source") == source_key and row.get("current_path", "") in resolved_aliases)]
    existing_media_pending = {row.get("current_path", ""): row for row in pending_rows
                              if row.get("source") == source_key}
    def next_review_number() -> int:
        numbers = [int(match.group(1)) for row in pending_rows
                   if (match := re.fullmatch(r"R(\d+)", row.get("review_id", "")))]
        return max(numbers, default=0) + 1
    for row in pending_by_path.values():
        path = str(Path(row["path"]))
        catalog_row = next(value for value in media_catalog_rows
                           if value.get("absolute_path") == path)
        pending_row = {
            "review_id": existing_media_pending.get(path, {}).get("review_id", f"R{next_review_number():05d}"),
            "review_status": row.get("status", "PENDENTE_CLASSIFICACAO_MIDIA"),
            "deletion_candidate": "NÃO; mídia preservada; exclusão permanente não autorizada",
            "source": source_key, "system_id": catalog_row["system_id"],
            "inventory_category": catalog_row["inventory_category"],
            "function_hint": catalog_row["function_hint"], "file_name": catalog_row["file_name"],
            "current_path": path, "source_root": str(MEDIA_SOURCE_ROOT),
            "size_bytes": catalog_row["size_bytes"], "file_sha256": catalog_row["sha256"],
            "verified_rom_sha256": "", "hash_status": catalog_row["hash_status"],
            "integrity_status": catalog_row["integrity_status"],
            "destination_or_canonical_peer": catalog_row["destination_proposal"],
            "dependencies": catalog_row["dependencies"],
            "review_note": f"{row.get('reason', '')} Próxima etapa: {row.get('next_step', 'revisar identidade e validar substituto')} ",
        }
        prior = existing_media_pending.get(path)
        if prior is not None:
            prior.update(pending_row)
        else:
            pending_rows.append(pending_row)
            existing_media_pending[path] = pending_row
    write_csv_atomic(pending_path, pending_rows, pending_fields)

    summary_path = OUTPUT_ROOT / "roms-pendentes-validacao-exclusao-resumo.md"
    summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else "# Resumo global de pendências\n"
    summary_marker = f"<!-- game-media-pending:{manifest['operation_id']}:{'rollback' if rolled_back else 'apply'} -->"
    if summary_marker not in summary:
        pending_counts = __import__("collections").Counter(row.get("review_status", "") for row in pending_by_path.values())
        section = ["", summary_marker, f"## Mídia de jogos RetroFE — pendências ({now_value})", "",
                   f"- Operação: `{manifest['operation_id']}`; caminhos pendentes: {len(pending_by_path)}.",
                   "- Arquivos permanecem nas coleções para preservar o caminho de runtime; exclusão permanente: nenhuma."]
        section.extend(f"- `{status}`: {count}." for status, count in sorted(pending_counts.items()))
        summary = summary.rstrip() + "\n" + "\n".join(section) + "\n"
        _write_atomic_text(summary_path, summary)

    state_path = OUTPUT_ROOT / "estado-final-verificacao.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    current_items = []
    by_type = __import__("collections").Counter()
    by_system = __import__("collections").Counter()
    repairs = aliases = 0
    for item in items:
        media = item.get("media", {})
        by_type[media.get("media_type", "")] += 1
        by_system[item.get("system_id", "")] += 1
        repairs += item["action"] == "media_repair_link"
        aliases += item["action"] == "media_alias_link"
        current_digest = (item.get("alias_precondition", {}).get("sha256")
                          if rolled_back and item["action"] == "media_repair_link"
                          else item.get("precondition", {}).get("sha256", "") if rolled_back
                          else item.get("after", {}).get("sha256", item.get("precondition", {}).get("sha256", "")))
        current_items.append({"source": str(media_item_alias(item)), "canonical_path": item["destination"],
                              "system_id": item.get("system_id", ""), "media_type": media.get("media_type", ""),
                              "sha256": current_digest,
                              "status": "rollback_verified" if rolled_back else "centralized",
                              "operation_id": manifest["operation_id"]})
    state["game_media_centralization_20261025"] = {
        "updated_at": now_value, "operation_id": manifest["operation_id"],
        "inventory_id": analysis.get("inventory_id", ""),
            "canonical_root": "media/<system_id>/<media_type>/",
            "patch_documentation_root": "metadata/<system_id>/patch/",
        "source_root": "frontend/RetroFE/collections/<collection>/medium_artwork/",
        "status": "rolled_back_verified" if rolled_back else "centralized_with_explicit_pending_items",
        "source_media_file_count": analysis.get("source_media_file_count", len(items) + len(pending_by_path)),
        "operation_source_paths": len(items),
        "centralized_source_paths": 0 if rolled_back else len(items),
        "relative_frontend_aliases": 0 if rolled_back else len(items),
        "duplicate_aliases_in_reversible_quarantine": aliases,
        "repaired_from_validated_external_backups": repairs,
        "pending_source_paths": len(pending_by_path),
        "pending_status_counts": dict(__import__("collections").Counter(row.get("status", "pending") for row in pending_by_path.values())),
        "centralized_by_system": dict(by_system), "centralized_by_media_type": dict(by_type),
        "permanent_deletions": 0, "rom_or_retrofe_configuration_changed": False,
        "no_intro_basename_alignment": "deferred; original media basenames preserved",
        "pending_items": list(pending_by_path.values()), "items": current_items,
    }
    state["global_pending_rows"] = len(pending_rows)
    state["pending_exclusion_review_rows"] = len(pending_rows)
    state["pending_deletion_candidate_counts"] = dict(__import__("collections").Counter(
        row.get("deletion_candidate", "") for row in pending_rows))
    missing = [row for row in pending_rows if row.get("current_path") and not os.path.lexists(row["current_path"])]
    inside_roms = [row for row in pending_rows if row.get("current_path") and
                   is_within(Path(row["current_path"]), PROJECT_ROOT / "roms")]
    state["pending_paths_missing"] = len(missing)
    state["global_pending_paths_missing_current"] = len(missing)
    state["pending_paths_inside_canonical_roms"] = len(inside_roms)
    atomic_json(state_path, state)

    report_path = OUTPUT_ROOT / "RELATORIO-CONSOLIDACAO.md"
    report = report_path.read_text(encoding="utf-8")
    report_marker = f"<!-- game-media-centralization:{manifest['operation_id']}:{'rollback' if rolled_back else 'apply'} -->"
    if report_marker not in report:
        title = "Rollback verificado da mídia de jogos" if rolled_back else "Centralização da mídia de jogos RetroFE"
        counts_by_type = __import__("collections").Counter(item.get("media", {}).get("media_type", "") for item in items)
        counts_by_system = __import__("collections").Counter(item.get("system_id", "") for item in items)
        validation_counts = __import__("collections").Counter(
            item.get("media", {}).get("media_validation", "validado") for item in items)
        lines = ["", report_marker, f"## {title} ({now_value})", "",
                 f"- Operação `{manifest['operation_id']}`: {len(items)} caminhos de mídia processados; "
                 f"{len(pending_by_path)} caminhos preservados em pendência global.",
                 f"- {aliases} duplicatas idênticas foram preservadas em quarentena reversível; "
                 f"{repairs} arquivos defeituosos foram substituídos por cópias de backup validadas.",
                 ("- Cada caminho usado pelo RetroFE continua no mesmo local como symlink relativo para o destino registrado no manifesto; documentos de patch usam `metadata/<system_id>/patch/`." if not rolled_back else
                  "- Os caminhos antigos foram restaurados após conferir hashes e quarentenas; nenhum alias desta operação permanece."),
                 "- Os nomes-base foram preservados; o alinhamento No-Intro continua para uma etapa coordenada posterior.",
                 "- Os arquivos foram validados por formato antes da mudança; hashes foram conferidos no destino e no alias.",
                 "- Nenhuma configuração do RetroFE foi editada, nenhum arquivo foi apagado permanentemente e `media/pending/` não recebeu staging.",
                 "- Tipos: " + "; ".join(f"{key}={value}" for key, value in sorted(counts_by_type.items())) + ".",
                 "- Sistemas: " + "; ".join(f"{key}={value}" for key, value in sorted(counts_by_system.items())) + ".",
                 f"- Inventário de origem `{analysis.get('inventory_id', '')}`; evidência detalhada de pendências em `{analysis.get('evidence_path', '')}`."]
        if pending_by_path:
            counts = __import__("collections").Counter(row.get("status", "pending") for row in pending_by_path.values())
            lines.append("- Pendências por motivo: " + "; ".join(f"{key}={value}" for key, value in sorted(counts.items())) + ".")
        report = report.rstrip() + "\n" + "\n".join(lines) + "\n"
        _write_atomic_text(report_path, report)

    changed = [catalog_path, systems_path, sets_path, pending_path, summary_path, state_path, report_path]
    artifacts = [str(path.relative_to(PROJECT_ROOT)) for path in changed]
    args = argparse.Namespace(
        change_type="game-media-global-catalog-update",
        reason=f"Atualização do catálogo global, estado, relatório e pendências da mídia de jogos {manifest['operation_id']}.",
        artifacts_json=json.dumps(artifacts, ensure_ascii=False),
    )
    artifact_record = command_record_artifacts(args)
    manifest["project_artifact_record_id"] = artifact_record["operation_id"]
    manifest["project_records_updated"] = artifacts
    write_manifest(manifest)
    return {"changed": artifacts, "artifact_operation_id": artifact_record["operation_id"]}


def command_steam_verify(args) -> dict:
    manifest_path = operation_dir(args.operation_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = []
    for item in manifest.get("items", []):
        if item.get("action") != "steam_move_link":
            continue
        source, destination = Path(item["source"]), Path(item["destination"])
        row = {"appid": item.get("steam", {}).get("appid", ""),
               "name": item.get("steam", {}).get("official_name", item.get("item_identity", "")),
               "source_path": str(source), "canonical_path": str(destination),
               "status": "verification_failed", "launch_tested": False, "issues": []}
        try:
            if not source.is_symlink() or os.readlink(source) != str(destination):
                raise ValueError("symlink de compatibilidade ausente ou alterado")
            if source.resolve(strict=True) != destination.resolve(strict=True):
                raise ValueError("symlink não resolve ao diretório canônico")
            tree = steam_tree_snapshot(destination, allow_save_symlinks=True,
                                       logical_snapshot=item.get("before") or item.get("precondition"),
                                       appid=str(item.get("steam", {}).get("appid", "")))
            if tree["sha256"] != item.get("after", {}).get("sha256"):
                raise ValueError("hash da árvore canônica divergiu do manifesto da operação")
            steam = item["steam"]
            manifest_file = Path(steam["manifest_path"])
            if not manifest_file.is_file() or sha256_file(manifest_file) != steam.get("manifest_sha256"):
                raise ValueError("appmanifest Steam ausente ou alterado")
            row.update({"status": "verified_path_and_tree", "tree_sha256": tree["sha256"],
                        "file_count": tree["file_count"], "directory_count": tree["directory_count"],
                        "size_bytes": tree["size_bytes"], "manifest_sha256": steam["manifest_sha256"]})
        except Exception as exc:
            row["issues"].append(f"{type(exc).__name__}: {exc}")
        rows.append(row)
    if not rows:
        raise ValueError(f"Operação sem itens Steam: {args.operation_id}")
    return record_analysis("verify", "Steam canonical tree and compatibility symlink", rows,
                           OUTPUT_ROOT / "analysis", {
                               "operation_id": args.operation_id,
                               "launch_tested": False,
                               "limitation": "path, appmanifest and full tree hash checked; no game process launched",
                           })


def later_preserved_source_path(source: Path, expected_hash: str, current_operation_id: str) -> Path | None:
    """Follow later verified moves/quarantines when a repair source was subsequently centralized."""
    index_path = OPS_ROOT / "history-index.csv"
    if not index_path.is_file():
        return None
    with index_path.open(newline="", encoding="utf-8-sig") as stream:
        operations = list(csv.DictReader(stream))
    current = next((row for row in operations if row.get("operation_id") == current_operation_id), None)
    if not current:
        return None
    later = [row for row in operations
             if row.get("created_at", "") > current.get("created_at", "")
             and row.get("kind") == "change" and row.get("status") == "completed_verified"]
    later.sort(key=lambda row: (row.get("created_at", ""), row.get("operation_id", "")))
    current_path = source
    for row in later:
        manifest_path = Path(row.get("manifest", ""))
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in manifest.get("items", []):
            if (item.get("source") != str(current_path)
                    or item.get("action") not in {"move", "rename", "quarantine"}):
                continue
            before = item.get("precondition", item.get("before", {}))
            after = item.get("after", {})
            if before.get("sha256") != expected_hash or after.get("sha256") != expected_hash:
                continue
            current_path = Path(item["destination"])
    if current_path.is_file() and not current_path.is_symlink() and sha256_file(current_path) == expected_hash:
        return current_path
    return None


def media_payload_snapshot_for_verify(destination: Path, expected_hash: str) -> dict:
    """Verify a regular canonical payload or a tightly scoped same-directory leaf alias.

    This exception is read-only and is used only by ``media verify``. Mutation
    planning still rejects symlinked payloads and symlinked path components.
    """
    if not destination.is_symlink():
        return path_snapshot(destination, include_hash=True)

    absolute = Path(os.path.abspath(destination))
    if not is_within(absolute, MEDIA_DEST_ROOT):
        raise ValueError(f"Link canônico de mídia fora de media/: {destination}")
    for component in (absolute.parent, *absolute.parent.parents):
        if os.path.lexists(component) and component.is_symlink():
            raise ValueError(f"Componente pai simbólico recusado: {component}")
        if component == PROJECT_ROOT:
            break
    else:
        raise ValueError(f"Caminho canônico não está sob a raiz do projeto: {destination}")

    link_target = os.readlink(destination)
    target_name = Path(link_target)
    if target_name.is_absolute() or target_name.name != link_target or link_target in {"", ".", ".."}:
        raise ValueError(f"O link canônico precisa apontar por basename relativo: {destination}")

    target = destination.parent / target_name
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"Alvo do link canônico não é arquivo regular: {target}")
    parent_real = destination.parent.resolve(strict=True)
    target_real = target.resolve(strict=True)
    if target_real.parent != parent_real:
        raise ValueError(f"Alvo do link canônico saiu da mesma pasta: {destination}")

    snapshot = path_snapshot(target, include_hash=True)
    if snapshot.get("kind") != "file" or snapshot.get("sha256") != expected_hash:
        raise ValueError(f"Payload do link canônico ausente ou com SHA-256 divergente: {destination}")
    snapshot["canonical_symlink_target"] = link_target
    snapshot["canonical_symlink_verified"] = True
    return snapshot


def command_media_verify(args) -> dict:
    manifest_path = operation_dir(args.operation_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = []
    for item in manifest.get("items", []):
        if item.get("action") not in MEDIA_ACTIONS:
            continue
        source, destination = Path(item["source"]), Path(item["destination"])
        media = item.get("media", {})
        alias = media_item_alias(item)
        expected_hash = item.get("after", {}).get("sha256", item.get("precondition", {}).get("sha256", ""))
        row = {"system_id": item.get("system_id", ""), "media_type": media.get("media_type", ""),
               "item_identity": item.get("item_identity", ""), "source_path": str(source),
               "frontend_alias": str(alias), "canonical_path": str(destination),
               "expected_sha256": expected_hash, "status": "verification_failed", "issues": []}
        try:
            canonical = media_payload_snapshot_for_verify(destination, expected_hash)
            if canonical["kind"] != "file" or canonical["sha256"] != expected_hash:
                raise ValueError("payload canônico ausente ou com SHA-256 divergente")
            if not media_link_matches(alias, destination):
                raise ValueError("alias do frontend não é symlink relativo para o destino canônico")
            if Path(os.readlink(alias)).is_absolute():
                raise ValueError("alias do frontend deveria ser relativo")
            if item["action"] == "media_alias_link":
                quarantine = Path(item["quarantine_destination"])
                if not quarantine.is_file() or sha256_file(quarantine) != item["precondition"]["sha256"]:
                    raise ValueError("duplicata reversível em quarentena ausente ou divergente")
                row["quarantine_path"] = str(quarantine)
            elif item["action"] == "media_rekey_link" and (
                    media.get("reuses_existing_payload") or media.get("reuses_planned_payload")):
                quarantine = Path(item["quarantine_destination"])
                if not quarantine.is_file() or sha256_file(quarantine) != item["precondition"]["sha256"]:
                    raise ValueError("payload canônico anterior preservado em quarentena ausente ou divergente")
                row["quarantine_path"] = str(quarantine)
            elif item["action"] == "media_repair_link":
                quarantine = Path(item["quarantine_destination"])
                replacement_hash = item["precondition"]["sha256"]
                if source.is_file() and not source.is_symlink() and sha256_file(source) == replacement_hash:
                    replacement_path = source
                else:
                    replacement_path = later_preserved_source_path(source, replacement_hash, args.operation_id)
                if replacement_path is None:
                    raise ValueError("backup de reposição ausente ou SHA-256 divergente na origem/quarentena posterior")
                if (not quarantine.is_file()
                        or sha256_file(quarantine) != media.get("replaced_frontend_sha256")):
                    raise ValueError("arquivo defeituoso preservado em quarentena ausente ou divergente")
                row["quarantine_path"] = str(quarantine)
                row["replacement_source_current_path"] = str(replacement_path)
                row["replacement_source_preserved"] = True
            row.update({"status": "verified_relative_alias_and_payload", "size_bytes": canonical["size_bytes"],
                        "sha256": canonical["sha256"], "relative_link": os.readlink(alias),
                        "action": item["action"]})
            if canonical.get("canonical_symlink_verified"):
                row["canonical_payload_link_target"] = canonical["canonical_symlink_target"]
        except Exception as exc:
            row["issues"].append(f"{type(exc).__name__}: {exc}")
        rows.append(row)
    if not rows:
        raise ValueError(f"Operação sem ações de mídia de jogo: {args.operation_id}")
    return record_analysis("media_verify", "Game media canonical payloads and RetroFE aliases", rows,
                           OUTPUT_ROOT / "analysis", {
                               "operation_id": args.operation_id,
                               "relative_symlinks_required": True,
                               "frontend_running_checked_before_apply": True,
                               "media_format_validation_source": "pre-apply game-media-validation evidence; SHA-256 preserved",
                           })


def command_steam_saves_verify(args) -> dict:
    manifest_path = operation_dir(args.operation_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = []
    for item in manifest.get("items", []):
        if item.get("action") != "steam_save_move_link":
            continue
        source, destination = Path(item["source"]), Path(item["destination"])
        steam = item.get("steam", {})
        row = {"appid": steam.get("appid", ""), "name": steam.get("official_name", ""),
               "source_kind": steam.get("source_kind", ""), "source_path": str(source),
               "canonical_path": str(destination), "status": "verification_failed", "issues": []}
        try:
            if not source.is_symlink() or os.readlink(source) != str(destination):
                raise ValueError("symlink Steam original ausente ou com alvo alterado")
            if source.resolve(strict=True) != destination.resolve(strict=True):
                raise ValueError("symlink Steam original não resolve ao save canônico")
            steam_centralizer.validate_steam_save_action(item, PROJECT_ROOT,
                                                         allow_centralized_source=True,
                                                         verify_payload=False)
            payload = steam_payload_snapshot(destination)
            expected_hash = item.get("after", {}).get("sha256", "")
            manifest_file = Path(steam["manifest_path"])
            if not manifest_file.is_file() or sha256_file(manifest_file) != steam.get("manifest_sha256"):
                raise ValueError("appmanifest Steam ausente ou alterado")
            row.update({"status": ("verified_symlink_cloud_evidence_and_content"
                                   if payload["sha256"] == expected_hash
                                   else "verified_symlink_payload_changed_since_centralization"),
                        "sha256": payload["sha256"], "size_bytes": payload["size_bytes"],
                        "kind": payload["kind"], "initial_sha256": expected_hash,
                        "appmanifest_sha256": steam["manifest_sha256"]})
        except Exception as exc:
            row["issues"].append(f"{type(exc).__name__}: {exc}")
        rows.append(row)
    if not rows:
        raise ValueError(f"Operação sem itens de save Steam: {args.operation_id}")
    return record_analysis("verify", "Steam canonical saves and compatibility symlinks", rows,
                           OUTPUT_ROOT / "analysis", {
                               "operation_id": args.operation_id,
                               "validation": "symlink target, AppID appmanifest, cloud evidence and full payload SHA-256",
                               "game_launch_or_save_write_tested": False,
                           })


def command_steam_watch(args) -> dict:
    if args.interval < 15:
        raise ValueError("Intervalo do watcher deve ser pelo menos 15 segundos")
    if args.interval > 3600:
        raise ValueError("Intervalo do watcher não pode exceder 3600 segundos")
    watch_path = OUTPUT_ROOT / "analysis" / "steam-watch-state.json"
    try:
        state = json.loads(watch_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"schema_version": "1.0", "items": {}}
    state.setdefault("items", {})
    state.setdefault("saves", {})
    state.pop("stopped_at", None)
    state["started_at"] = now()
    atomic_json(watch_path, state)
    last_signature = None
    last_full_mount_scan = 0.0
    print(json.dumps({"watch": "started", "interval_seconds": args.interval,
                      "auto_apply_new_games": bool(args.apply_new_games)}, ensure_ascii=False), flush=True)
    try:
        while True:
            signature = steam_centralizer.mount_signature()
            monotonic_now = time.monotonic()
            scan_mounts = (signature != last_signature
                           or monotonic_now - last_full_mount_scan >= max(300, args.interval))
            last_signature = signature
            if scan_mounts:
                last_full_mount_scan = monotonic_now
            rows, _evidence = steam_centralizer.collect_installed_apps(
                PROJECT_ROOT, steam_centralizer.app_metadata_cache_path(PROJECT_ROOT),
                online=True, scan_mounts=scan_mounts)
            running = steam_centralizer.steam_processes_running()
            eligible = [row for row in rows if row.get("status") == "eligible_game"]
            for row in eligible:
                key = f"{row['appid']}|{row['source_path']}"
                manifest_hash = sha256_file(Path(row["manifest_path"]))
                saved = state["items"].get(key, {})
                same_manifest = saved.get("manifest_sha256") == manifest_hash
                if same_manifest and saved.get("status") in {"centralized", "apply_failed"}:
                    continue
                if same_manifest and saved.get("status") == "prepared":
                    if args.apply_new_games and not running and saved.get("operation_id"):
                        try:
                            result = apply_operation(saved["operation_id"], True)
                            saved["status"] = "centralized" if result.get("status") == "completed_verified" else result.get("status")
                        except Exception as exc:
                            saved["status"] = "apply_failed"
                            saved["error"] = f"{type(exc).__name__}: {exc}"
                        saved["updated_at"] = now()
                        state["items"][key] = saved
                        print(json.dumps({"appid": row["appid"], "name": row["official_name"],
                                          "operation_id": saved["operation_id"], "status": saved["status"],
                                          "error": saved.get("error", "")}, ensure_ascii=False), flush=True)
                    continue
                if running:
                    saved.update({"appid": row["appid"], "source_path": row["source_path"],
                                  "manifest_sha256": manifest_hash, "status": "deferred_steam_running",
                                  "updated_at": now()})
                    state["items"][key] = saved
                    continue
                try:
                    prepared = prepare_steam_plan([row], f"Automatic Steam game centralization: AppID {row['appid']}")
                except Exception as exc:
                    saved.update({"appid": row["appid"], "name": row["official_name"],
                                  "source_path": row["source_path"], "manifest_sha256": manifest_hash,
                                  "status": "plan_failed", "error": f"{type(exc).__name__}: {exc}",
                                  "updated_at": now()})
                    state["items"][key] = saved
                    print(json.dumps({"appid": row["appid"], "name": row["official_name"],
                                      "status": saved["status"], "error": saved["error"]},
                                     ensure_ascii=False), flush=True)
                    continue
                saved = {"appid": row["appid"], "name": row["official_name"],
                         "source_path": row["source_path"], "manifest_sha256": manifest_hash,
                         "operation_id": prepared["operation_id"], "status": "prepared", "updated_at": now()}
                state["items"][key] = saved
                if args.apply_new_games:
                    try:
                        result = apply_operation(prepared["operation_id"], True)
                        saved["status"] = "centralized" if result.get("status") == "completed_verified" else result.get("status")
                    except Exception as exc:
                        saved["status"] = "apply_failed"
                        saved["error"] = f"{type(exc).__name__}: {exc}"
                    saved["updated_at"] = now()
                    state["items"][key] = saved
                print(json.dumps({"appid": row["appid"], "name": row["official_name"],
                                  "operation_id": prepared["operation_id"], "status": saved["status"],
                                  "error": saved.get("error", "")}, ensure_ascii=False), flush=True)
            if not running:
                try:
                    save_rows, save_evidence = steam_centralizer.discover_installed_saves(
                        PROJECT_ROOT, rows, scan_mounts=scan_mounts)
                    eligible_saves = [row for row in save_rows
                                      if row.get("status") == "eligible_save" and row.get("eligible")]
                    pending_saves = []
                    for save in eligible_saves:
                        key = f"{save['appid']}|{save['source_path']}"
                        saved = state["saves"].get(key, {})
                        current_hash = steam_payload_snapshot(Path(save["source_path"]))["sha256"]
                        appmanifest_hash = save["steam"].get("manifest_sha256", "")
                        same_candidate = (saved.get("manifest_sha256") == appmanifest_hash
                                          and saved.get("save_sha256") == current_hash)
                        if same_candidate and saved.get("status") in {
                                "centralized", "apply_failed", "plan_failed"}:
                            continue
                        if same_candidate and saved.get("status") == "prepared":
                            if args.apply_new_games and saved.get("operation_id"):
                                try:
                                    result = apply_operation(saved["operation_id"], True)
                                    saved["status"] = ("centralized" if result.get("status") == "completed_verified"
                                                        else result.get("status"))
                                except Exception as exc:
                                    saved["status"] = "apply_failed"
                                    saved["error"] = f"{type(exc).__name__}: {exc}"
                                saved["updated_at"] = now()
                                state["saves"][key] = saved
                                print(json.dumps({"appid": save["appid"], "name": save["official_name"],
                                                  "operation_id": saved["operation_id"],
                                                  "save_status": saved["status"],
                                                  "error": saved.get("error", "")},
                                                 ensure_ascii=False), flush=True)
                            continue
                        pending_saves.append((key, save, current_hash, appmanifest_hash))
                    save_inventory = None
                    if pending_saves:
                        save_inventory = record_steam_save_inventory(save_rows, {
                            **save_evidence,
                            "watch_mode": True,
                            "watch_policy": "plan/apply only while Steam and game processes are closed",
                        })
                    for key, save, current_hash, appmanifest_hash in pending_saves:
                        saved = state["saves"].get(key, {})
                        try:
                            prepared = prepare_steam_save_plan(
                                [save], f"Automatic Steam save centralization: AppID {save['appid']}")
                            prepared["discovery_operation_id"] = (
                                save_inventory["analysis"]["operation_id"] if save_inventory else "")
                            prepared["unresolved_save_evidence"] = save_evidence.get("unsupported_or_unmatched", [])
                            write_manifest(prepared)
                            saved = {"appid": save["appid"], "name": save["official_name"],
                                     "source_path": save["source_path"],
                                     "manifest_sha256": appmanifest_hash,
                                     "save_sha256": current_hash,
                                     "operation_id": prepared["operation_id"],
                                     "status": "prepared", "updated_at": now()}
                            if args.apply_new_games:
                                result = apply_operation(prepared["operation_id"], True)
                                saved["status"] = ("centralized" if result.get("status") == "completed_verified"
                                                    else result.get("status"))
                        except Exception as exc:
                            saved.update({"appid": save["appid"], "name": save["official_name"],
                                          "source_path": save["source_path"],
                                          "manifest_sha256": appmanifest_hash,
                                          "save_sha256": current_hash,
                                          "status": "apply_failed" if saved.get("operation_id") else "plan_failed",
                                          "error": f"{type(exc).__name__}: {exc}"})
                        saved["updated_at"] = now()
                        state["saves"][key] = saved
                        print(json.dumps({"appid": save["appid"], "name": save["official_name"],
                                          "operation_id": saved.get("operation_id", ""),
                                          "save_status": saved["status"],
                                          "error": saved.get("error", "")},
                                         ensure_ascii=False), flush=True)
                except Exception as exc:
                    state["last_save_scan_error"] = f"{type(exc).__name__}: {exc}"
                    print(json.dumps({"save_scan": "failed", "error": state["last_save_scan_error"]},
                                     ensure_ascii=False), flush=True)
            state["last_scan"] = now()
            state["last_mount_signature"] = signature
            atomic_json(watch_path, state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        state["stopped_at"] = now()
        atomic_json(watch_path, state)
        print(json.dumps({"watch": "stopped", "state": str(watch_path)}, ensure_ascii=False), flush=True)
    return {"status": "watch_stopped", "state": str(watch_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventário e manutenção auditável de ROMs")
    commands = parser.add_subparsers(dest="command", required=True)
    storage_dedupe.add_parser(commands)
    inventory = commands.add_parser("inventory", help="inventaria raízes sem alterar arquivos")
    inventory.add_argument("--root", action="append", required=True)
    inventory.add_argument("--out", default=str(OUTPUT_ROOT / "inventories"))
    inventory.add_argument("--hash", action="store_true")
    verify = commands.add_parser("verify", help="valida com ferramentas específicas por formato")
    verify.add_argument("--path", action="append", default=[])
    verify.add_argument("--root")
    verify.add_argument("--extensions", nargs="*")
    verify.add_argument("--jobs", type=int, default=2)
    verify.add_argument("--list-archive-contents", action="store_true",
                        help="lista membros, tamanhos e CRC de ZIP/7z/RAR/LHA sem extrair; para 7z/RAR/LHA também executa 7z t")
    verify.add_argument("--out", default=str(OUTPUT_ROOT / "validations"))
    archive_classify = commands.add_parser("archive-classify",
                                           help="classifica conteúdo de uma listagem já salva sem reler archives")
    archive_classify.add_argument("--operation-id", required=True,
                                  help="operação verify concluída que contém archive-members.jsonl")
    discs = commands.add_parser("discs", help="playlists M3U de conjuntos identificados e ordenados")
    discs_commands = discs.add_subparsers(dest="discs_command", required=True)
    discs_plan = discs_commands.add_parser("plan")
    discs_plan.add_argument("--file", required=True)
    for action in ("apply", "verify", "rollback"):
        command = discs_commands.add_parser(action)
        command.add_argument("--operation-id", required=True)
        if action in {"apply", "rollback"}:
            command.add_argument("--" + action, action="store_true")
    plan = commands.add_parser("plan", help="pré-valida um plano JSON; não altera a biblioteca")
    plan.add_argument("--file", required=True)
    apply = commands.add_parser("apply", help="executa um plano preparado; exige --apply")
    apply.add_argument("--operation-id", required=True)
    apply.add_argument("--apply", action="store_true")
    project_catalog = commands.add_parser(
        "project-catalog", help="projeta destinos verificados no catálogo atual sem reescrever o diário")
    project_catalog_commands = project_catalog.add_subparsers(dest="project_catalog_command", required=True)
    project_catalog_plan = project_catalog_commands.add_parser("plan")
    project_catalog_plan.add_argument("--operation-id", required=True)
    project_catalog_apply = project_catalog_commands.add_parser("apply")
    project_catalog_apply.add_argument("--operation-id", required=True)
    project_catalog_apply.add_argument("--apply", action="store_true")
    project_catalog_verify = project_catalog_commands.add_parser("verify")
    project_catalog_verify.add_argument("--operation-id", required=True)
    project_catalog_rollback = project_catalog_commands.add_parser("rollback")
    project_catalog_rollback.add_argument("--operation-id", required=True)
    project_catalog_rollback.add_argument("--rollback", action="store_true")
    discard = commands.add_parser("discard", help="retira um plano pendente por revisão; preserva manifesto e evidências")
    discard.add_argument("--operation-id", required=True)
    discard.add_argument("--reason", required=True)
    discard.add_argument("--apply", action="store_true")
    rollback = commands.add_parser("rollback", help="reverte movimento/rename ou extração verificada")
    rollback.add_argument("--operation-id", required=True)
    rollback.add_argument("--rollback", action="store_true")
    compact = commands.add_parser("compact", help="planeja e valida conversões lossless nativas do emulador")
    compact_commands = compact.add_subparsers(dest="compact_command", required=True)
    compact_plan = compact_commands.add_parser("plan", help="audita todos os arquivos e cria plano sem alterar ROMs")
    compact_plan.add_argument("--root", default=str(PROJECT_ROOT / "roms"))
    compact_plan.add_argument('--system-id',action='append',help='limita a plataformas explícitas sem perder a raiz canônica; repetível')
    archive_mode=compact_plan.add_mutually_exclusive_group()
    archive_mode.add_argument('--normalize-archives',dest='normalize_archives',action='store_true',default=True,help='inclui ZIPs por perfil ROM explícito (padrão); plano somente leitura e apply revisado')
    archive_mode.add_argument('--preserve-archives',dest='normalize_archives',action='store_false',help='mantém ZIPs fora deste plano')
    compact_apply = compact_commands.add_parser("apply", help="aplica conversões verificadas; exige --apply")
    compact_apply.add_argument("--operation-id", required=True)
    compact_apply.add_argument("--apply", action="store_true")
    compact_apply.add_argument("--allow-active-frontend", action="store_true",
                               help="override pontual autorizado pelo usuário para manter o RetroFE ativo")
    compact_apply.add_argument("--active-frontend-reason", default="",
                               help="motivo explícito gravado no manifesto quando o RetroFE está ativo")
    compact_verify = compact_commands.add_parser("verify", help="valida saída, SHA-256 e digest lógico")
    compact_verify.add_argument("--operation-id", required=True)
    compact_rollback = compact_commands.add_parser("rollback", help="reconverte o formato de origem; exige --rollback")
    compact_rollback.add_argument("--operation-id", required=True)
    compact_rollback.add_argument("--rollback", action="store_true")
    duplicates = commands.add_parser("duplicate-audit", help="encontra hashes iguais sem apagar")
    duplicates.add_argument("--root", required=True)
    duplicates.add_argument("--system-id", default="")
    duplicates.add_argument("--logical", action="store_true", help="compara payload de ROM unitária íntegra em raw/ZIP/7z; somente revisão")
    duplicates.add_argument("--out", default=str(OUTPUT_ROOT / "analysis"))
    dat = commands.add_parser("dat-match", help="compara payloads com DAT por tupla integral")
    dat.add_argument("--root", action="append", default=[],
                     help="raiz a analisar; pode repetir para reunir caminhos do mesmo sistema")
    dat.add_argument("--path", action="append", default=[],
                     help="arquivo candidato explícito; pode repetir para fontes sem raiz uniforme")
    dat.add_argument("--dat", required=True)
    dat.add_argument("--system-id", required=True)
    dat.add_argument("--raw-extensions", nargs="*")
    dat.add_argument("--out", default=str(OUTPUT_ROOT / "analysis"))
    commands.add_parser("history", help="reconstrói o índice de manifestos antigos e novos")
    close = commands.add_parser("close-interrupted", help="fecha uma operação interrompida sem apagar resultados")
    close.add_argument("--operation-id", required=True)
    close.add_argument("--reason", required=True)
    record = commands.add_parser("record-change", help="registra instalação/alteração externa no diário")
    record.add_argument("--change-type", required=True)
    record.add_argument("--reason", required=True)
    record.add_argument("--details-json", required=True)
    artifacts = commands.add_parser("record-artifacts", help="registra hashes de arquivos do projeto no histórico")
    artifacts.add_argument("--change-type", required=True)
    artifacts.add_argument("--reason", required=True)
    artifacts.add_argument("--artifacts-json", required=True)
    artifacts.add_argument("--defer-history-index", action="store_true",
                           help="registra artefatos e diário agora; adia a reconstrução integral para history")
    steam_shortcuts_parser = commands.add_parser(
        "steam-shortcuts", help="cadastra RetroFE/ES-DE como jogos não-Steam e lança via Big Picture")
    steam_shortcuts_commands = steam_shortcuts_parser.add_subparsers(
        dest="steam_shortcuts_command", required=True)
    for _name in ("plan", "status"):
        _sub = steam_shortcuts_commands.add_parser(_name)
        _sub.add_argument("--frontend", choices=sorted(steam_shortcuts.FRONTENDS))
    steam_shortcuts_apply = steam_shortcuts_commands.add_parser("apply")
    steam_shortcuts_apply.add_argument("--operation-id", required=True)
    steam_shortcuts_apply.add_argument("--apply", action="store_true")
    steam_shortcuts_verify = steam_shortcuts_commands.add_parser("verify")
    steam_shortcuts_verify.add_argument("--operation-id", required=True)
    steam_shortcuts_rollback = steam_shortcuts_commands.add_parser("rollback")
    steam_shortcuts_rollback.add_argument("--operation-id", required=True)
    steam_shortcuts_rollback.add_argument("--rollback", action="store_true")
    steam_shortcuts_launch = steam_shortcuts_commands.add_parser(
        "launch", help="abre Big Picture e inicia o frontend pelo Steam, sem reiniciar o cliente")
    steam_shortcuts_launch.add_argument("--frontend", choices=sorted(steam_shortcuts.FRONTENDS),
                                        required=True)
    steam_shortcuts_launch.add_argument("--bp-wait", type=float, default=5.0,
                                        help="espera entre abrir Big Picture e iniciar o jogo (segundos)")
    media = commands.add_parser("media", help="verifica mídia de jogo centralizada e aliases do RetroFE")
    media_commands = media.add_subparsers(dest="media_command", required=True)
    media_verify = media_commands.add_parser("verify", help="confere conteúdo canônico e links relativos")
    media_verify.add_argument("--operation-id", required=True)
    covers = media_commands.add_parser("covers", help="distingue frente/encarte e separa somente recortes revisados")
    cover_commands = covers.add_subparsers(dest="media_covers_command", required=True)
    cover_audit = cover_commands.add_parser("audit", help="diagnóstico de capas sem modificar payloads")
    cover_audit.add_argument("--system-id", action="append", help="limita plataforma; padrão cobre toda a mídia canônica")
    cover_review = cover_commands.add_parser("review", help="registra classificação visual por SHA sem alterar imagens")
    cover_review.add_argument("--audit-operation-id", required=True)
    cover_review.add_argument("--reviews-json", required=True)
    cover_plan = cover_commands.add_parser("plan", help="plano por revisão visual vinculada a SHA e coordenadas")
    cover_plan.add_argument("--audit-operation-id", required=True)
    cover_plan.add_argument("--reviews-json", required=True)
    cover_attest = cover_commands.add_parser("attest", help="revalida código atualizado sem reescrever plano histórico")
    cover_attest.add_argument("--operation-id", required=True)
    cover_attest.add_argument("--reason", required=True)
    cover_apply = cover_commands.add_parser("apply", help="separa um item; preserva original e aliases")
    cover_apply.add_argument("--operation-id", required=True)
    cover_apply.add_argument("--item-id", required=True)
    cover_apply.add_argument("--apply", action="store_true")
    cover_verify = cover_commands.add_parser("verify", help="confere originais, regiões e aliases de capas")
    cover_verify.add_argument("--operation-id", required=True)
    cover_rollback = cover_commands.add_parser("rollback", help="restaura originais sem descartar frentes geradas")
    cover_rollback.add_argument("--operation-id", required=True)
    cover_rollback.add_argument("--rollback", action="store_true")
    media_optimize = media_commands.add_parser("optimize", help="analisa, planeja e otimiza mídia com rollback")
    media_optimize_commands = media_optimize.add_subparsers(dest="media_optimize_command", required=True)
    media_optimize_inventory = media_optimize_commands.add_parser("inventory", help="inventário somente leitura da mídia canônica")
    media_optimize_inventory.add_argument("--root", default=str(MEDIA_DEST_ROOT))
    media_optimize_inventory.add_argument("--hash", action="store_true", help="calcula SHA-256 de todos os arquivos regulares")
    media_optimize_plan = media_optimize_commands.add_parser("plan", help="gera auditoria allowlist sem converter arquivos")
    media_optimize_plan.add_argument("--inventory-operation-id", required=True)
    media_optimize_plan.add_argument("--relative-path", action="append", help="seleciona caminho exato de media/; repetível, sem seguir aliases")
    media_optimize_plan.add_argument("--profile-id", action="append", help="seleciona perfil allowlist; repetível")
    media_optimize_attest = media_optimize_commands.add_parser("attest", help="vincula código atual a um plano preparado após rechecagens sem refazer inventário")
    media_optimize_attest.add_argument("--operation-id", required=True)
    media_optimize_attest.add_argument("--reason", required=True)
    media_optimize_apply = media_optimize_commands.add_parser("apply", help="aplica somente itens selecionados; exige --apply")
    media_optimize_apply.add_argument("--operation-id", required=True)
    media_optimize_apply.add_argument("--item-id", action="append", required=True, help="ID exato do manifesto; use um por execução")
    media_optimize_apply.add_argument("--apply", action="store_true")
    media_optimize_apply.add_argument("--allow-lossy", action="store_true", help="autoriza os perfis com perda declarados no plano")
    media_optimize_verify = media_optimize_commands.add_parser("verify", help="revalida payload, decodificação, alias e rollback")
    media_optimize_verify.add_argument("--operation-id", required=True)
    media_optimize_rollback = media_optimize_commands.add_parser("rollback", help="restaura originais preservados; exige --rollback")
    media_optimize_rollback.add_argument("--operation-id", required=True)
    media_optimize_rollback.add_argument("--rollback", action="store_true")
    sessions=media_commands.add_parser("session",help="sessões de scraper CoW, exclusão mútua e reconciliação automática")
    session_commands=sessions.add_subparsers(dest="media_session_command",required=True)
    session_commands.add_parser("status")
    session_commands.add_parser("recover")
    session_rollback=session_commands.add_parser("rollback")
    session_rollback.add_argument("--operation-id",required=True)
    session_rollback.add_argument("--rollback",action="store_true")
    session_run=session_commands.add_parser("run")
    session_run.add_argument("--client",choices=["esde","skraper"],required=True)
    session_run.add_argument("--runtime",choices=["wine","mono"],default="wine")
    session_run.add_argument("child_command",nargs=argparse.REMAINDER)
    session_ingest = session_commands.add_parser("ingest", help="reexecuta a ingestão idempotente de gamelists já canônicas")
    session_ingest.add_argument("--system-id", action="append", default=None)
    media_esde = media_commands.add_parser("esde", help="centraliza downloaded_media em media/ com conflitos e rollback registrados")
    media_esde_commands = media_esde.add_subparsers(dest="esde_media_command", required=True)
    media_esde_commands.add_parser("validate", help="preflight somente leitura da raiz compartilhada e dos aliases ES-DE")
    esde_aliases = media_esde_commands.add_parser("aliases", help="cria/verifica/reverte somente aliases relativos ES-DE gerenciados")
    esde_aliases_commands = esde_aliases.add_subparsers(dest="esde_aliases_command", required=True)
    esde_aliases_commands.add_parser("plan")
    esde_aliases_commands.add_parser("ensure", help="plan+apply idempotente usado pelo launcher; divergências bloqueiam")
    for _name in ("apply", "verify", "rollback"):
        _sub = esde_aliases_commands.add_parser(_name)
        _sub.add_argument("--operation-id", required=True)
        if _name != "verify":
            _sub.add_argument("--" + _name, action="store_true")
    esde_userdata = media_esde_commands.add_parser("userdata", help="migra dados ES-DE do home para frontend/ES-DE/UserData sem copiar mídia")
    esde_userdata_commands = esde_userdata.add_subparsers(dest="esde_userdata_command", required=True)
    esde_userdata_commands.add_parser("plan")
    for userdata_command in ("apply", "verify", "rollback"):
        userdata_parser = esde_userdata_commands.add_parser(userdata_command)
        userdata_parser.add_argument("--operation-id", required=True)
        if userdata_command != "verify":
            userdata_parser.add_argument("--" + userdata_command, action="store_true")
    media_esde_plan = media_esde_commands.add_parser("plan", help="gera e planeja a migração ES-DE sem mover arquivos")
    media_esde_plan.add_argument("--source-root", default="", help="padrão ~/ES-DE/downloaded_media")
    media_esde_apply = media_esde_commands.add_parser("apply", help="aplica apenas um plano ES-DE revisado")
    media_esde_apply.add_argument("--operation-id", required=True)
    media_esde_apply.add_argument("--apply", action="store_true")
    media_esde_verify = media_esde_commands.add_parser("verify", help="confere hashes, aliases e a raiz legada")
    media_esde_verify.add_argument("--operation-id", required=True)
    media_esde_rollback = media_esde_commands.add_parser("rollback", help="reverte movimentos ES-DE preservados")
    media_esde_rollback.add_argument("--operation-id", required=True)
    media_esde_rollback.add_argument("--rollback", action="store_true")
    media_stray_parser = media_commands.add_parser(
        "stray", help="reconcilia roms/media com a mídia canônica, sem apagar e sem deixar symlink")
    media_stray_commands = media_stray_parser.add_subparsers(dest="media_stray_command", required=True)
    media_stray_plan = media_stray_commands.add_parser("plan")
    media_stray_plan.add_argument("--promote", action="store_true",
                                  help="valida integralmente e planeja promover ausentes/diferentes (Alt N) com basename único de ROM")
    media_stray_plan.add_argument("--system-id", help="limita o plano a roms/media/<system_id>")
    media_stray_plan.add_argument("--nested-variants", action="store_true",
                                  help="achata <sistema>/<categoria>/<variante>/<arquivo> quando a ROM está em roms/<sistema>/<variante>/")
    media_stray_plan.add_argument("--create-category", action="append", default=[], metavar="SYSTEM/TYPE",
                                  help="seleção explícita de categoria canônica ausente; exige --promote, validação integral e ROM única")
    media_stray_plan.add_argument("--nested-game-paths", action="store_true",
                                  help="achata mídia aninhada somente com pasta relativa de ROM existente e associação exata nesse escopo")
    media_stray_plan.add_argument("--pdf-linearization-review", action="store_true",
                                  help="aceita somente warnings conhecidos de linearização após render integral de todas as páginas; bytes preservados")
    media_stray_apply = media_stray_commands.add_parser("apply")
    media_stray_apply.add_argument("--operation-id", required=True)
    media_stray_apply.add_argument("--apply", action="store_true")
    media_stray_verify = media_stray_commands.add_parser("verify")
    media_stray_verify.add_argument("--operation-id", required=True)
    media_stray_status = media_stray_commands.add_parser("status", help="consulta manifesto, eventos e presença dos caminhos selecionados")
    media_stray_status.add_argument("--operation-id", required=True)
    media_stray_rollback = media_stray_commands.add_parser("rollback")
    media_stray_rollback.add_argument("--operation-id", required=True)
    media_stray_rollback.add_argument("--rollback", action="store_true")
    variants = commands.add_parser("variants", help="interpreta nomes de variantes sem inferir fatos; registry reversível")
    variants_commands = variants.add_subparsers(dest="variants_command", required=True)
    variants_inventory = variants_commands.add_parser("inventory", help="audita nomes, hash de arquivos candidatos e cobertura do catálogo")
    variants_inventory.add_argument("--hash", action="store_true",
                                    help="garante SHA-256 para todos os candidatos; reutiliza hashes anteriores se snapshot permanecer idêntico")
    variants_inventory.add_argument("--reuse-hashes-from", metavar="OPERATION_ID",
                                     help="reutiliza hashes de inventário anterior somente se stat/snapshot do arquivo ainda for idêntico")
    variants_plan = variants_commands.add_parser("plan", help="prepara atualização revisável do registry JSONL")
    variants_plan.add_argument("--inventory-operation-id", required=True)
    variants_apply = variants_commands.add_parser("apply", help="grava somente metadata/rom-variants.jsonl com --apply")
    variants_apply.add_argument("--operation-id", required=True)
    variants_apply.add_argument("--apply", action="store_true")
    variants_cancel = variants_commands.add_parser("cancel", help="marca um plano preparado como supersedido e preserva sua proposta")
    variants_cancel.add_argument("--operation-id", required=True)
    variants_cancel.add_argument("--reason", required=True)
    variants_verify = variants_commands.add_parser("verify", help="confere registry, snapshots e opcionalmente refaz hashes")
    variants_verify.add_argument("--operation-id", required=True)
    variants_verify.add_argument("--rehash", action="store_true",
                                 help="refaz SHA-256 integral de cada arquivo candidato")
    variants_rollback = variants_commands.add_parser("rollback", help="restaura o registry anterior com --rollback")
    variants_rollback.add_argument("--operation-id", required=True)
    variants_rollback.add_argument("--rollback", action="store_true")
    variants_organize = variants_commands.add_parser(
        "organize", help="organiza apenas variantes com evidência e atualiza gamelists em transação reversível")
    variants_organize_commands = variants_organize.add_subparsers(
        dest="variant_organization_command", required=True)
    variants_organize_plan = variants_organize_commands.add_parser(
        "plan", help="planeja pastas de uma plataforma a partir de inventário, DAT, validação e hierarquia")
    variants_organize_plan.add_argument("--system-id", required=True)
    variants_organize_plan.add_argument("--inventory-operation-id", required=True)
    variants_organize_plan.add_argument("--dat-match-operation-id", required=True)
    variants_organize_plan.add_argument("--verify-operation-id", required=True)
    variants_organize_plan.add_argument("--hierarchy-operation-id", required=True)
    variants_organize_plan.add_argument("--esde-home", help="raiz canônica frontend/ES-DE/UserData")
    variants_translation_plan = variants_organize_commands.add_parser(
        "translation-plan", help="planeja traduções completas atestadas com base DAT única, sem chamá-las de dumps originais")
    variants_translation_plan.add_argument("--system-id", required=True)
    variants_translation_plan.add_argument("--inventory-operation-id", required=True)
    variants_translation_plan.add_argument("--dat-match-operation-id", required=True)
    variants_translation_plan.add_argument("--verify-operation-id", required=True)
    variants_translation_plan.add_argument("--hierarchy-operation-id", required=True)
    variants_translation_plan.add_argument("--language", required=True)
    variants_translation_plan.add_argument("--attestation", required=True,
                                           help="decisão do responsável a registrar no manifesto")
    variants_translation_plan.add_argument("--esde-home", help="raiz canônica frontend/ES-DE/UserData")
    variants_organize_apply = variants_organize_commands.add_parser(
        "apply", help="move arquivos confirmados e atualiza gamelists; exige frontends fechados e --apply")
    variants_organize_apply.add_argument("--operation-id", required=True)
    variants_organize_apply.add_argument("--apply", action="store_true")
    variants_organize_cancel = variants_organize_commands.add_parser(
        "cancel", help="cancela um plano de organização preparado e preserva sua proposta")
    variants_organize_cancel.add_argument("--operation-id", required=True)
    variants_organize_cancel.add_argument("--reason", required=True)
    variants_organize_verify = variants_organize_commands.add_parser(
        "verify", help="confere destinos, hashes, gamelists e hierarquia RetroFE")
    variants_organize_verify.add_argument("--operation-id", required=True)
    variants_organize_rollback = variants_organize_commands.add_parser(
        "rollback", help="restaura arquivos e gamelists exatos; exige frontends fechados e --rollback")
    variants_organize_rollback.add_argument("--operation-id", required=True)
    variants_organize_rollback.add_argument("--rollback", action="store_true")
    variants_names = variants_commands.add_parser(
        "names", help="alinha basenames a um DAT exato com mídia e gamelists em transação reversível")
    variants_names_commands = variants_names.add_subparsers(dest="variant_name_command", required=True)
    variants_names_plan = variants_names_commands.add_parser(
        "plan", help="prepara renomes No-Intro por match DAT único e alinhamento de mídia/referências")
    variants_names_plan.add_argument("--system-id", required=True)
    variants_names_plan.add_argument("--inventory-operation-id", required=True)
    variants_names_plan.add_argument("--dat-match-operation-id", required=True)
    variants_names_plan.add_argument("--verify-operation-id", required=True)
    variants_names_plan.add_argument("--esde-home", help="raiz canônica frontend/ES-DE/UserData")
    variants_names_apply = variants_names_commands.add_parser(
        "apply", help="renomeia ROM/mídia e atualiza gamelists; exige frontends fechados e --apply")
    variants_names_apply.add_argument("--operation-id", required=True)
    variants_names_apply.add_argument("--apply", action="store_true")
    variants_names_verify = variants_names_commands.add_parser(
        "verify", help="confere nomes, hashes, mídia associada e paths de gamelist")
    variants_names_verify.add_argument("--operation-id", required=True)
    variants_names_rollback = variants_names_commands.add_parser(
        "rollback", help="restaura nomes, mídia e gamelists exatos; exige frontends fechados e --rollback")
    variants_names_rollback.add_argument("--operation-id", required=True)
    variants_names_rollback.add_argument("--rollback", action="store_true")
    hierarchy = commands.add_parser("hierarchy", help="materializa navegação ROM aninhada no RetroFE com plano e rollback")
    hierarchy_commands = hierarchy.add_subparsers(dest="hierarchy_command", required=True)
    hierarchy_plan = hierarchy_commands.add_parser("plan", help="planeja list.path/list.romHierarchy para a coleção ativa")
    hierarchy_plan.add_argument("--system-id", required=True)
    hierarchy_apply = hierarchy_commands.add_parser("apply", help="aplica configuração planejada; exige --apply")
    hierarchy_apply.add_argument("--operation-id", required=True)
    hierarchy_apply.add_argument("--apply", action="store_true")
    hierarchy_verify = hierarchy_commands.add_parser("verify", help="verifica configuração materializada")
    hierarchy_verify.add_argument("--operation-id", required=True)
    hierarchy_rollback = hierarchy_commands.add_parser("rollback", help="restaura configuração exata anterior; exige --rollback")
    hierarchy_rollback.add_argument("--operation-id", required=True)
    hierarchy_rollback.add_argument("--rollback", action="store_true")
    hierarchy_cancel = hierarchy_commands.add_parser(
        "cancel", help="cancela um plano de hierarquia preparado e preserva a proposta")
    hierarchy_cancel.add_argument("--operation-id", required=True)
    hierarchy_cancel.add_argument("--reason", required=True)
    save_capture.add_parser(commands)
    steam = commands.add_parser("steam", help="descobre e centraliza jogos instalados pelo Steam")
    steam_commands = steam.add_subparsers(dest="steam_command", required=True)
    steam_discover = steam_commands.add_parser("discover", help="varre bibliotecas Steam oficiais e discos montados")
    steam_discover.add_argument("--offline", action="store_true", help="usa apenas metadados já em cache")
    steam_discover.add_argument("--no-mounted-scan", action="store_true", help="não percorre discos removíveis montados")
    steam_plan = steam_commands.add_parser("plan", help="prepara migração revisável sem mover dados")
    steam_choice = steam_plan.add_mutually_exclusive_group(required=True)
    steam_choice.add_argument("--all-games", action="store_true", help="planeja todos os jogos elegíveis")
    steam_choice.add_argument("--app-id", help="planeja um AppID instalado")
    steam_plan.add_argument("--library", help="seleciona uma Steam library quando o AppID aparece mais de uma vez")
    steam_plan.add_argument("--offline", action="store_true", help="usa apenas metadados já em cache")
    steam_plan.add_argument("--no-mounted-scan", action="store_true", help="não percorre discos removíveis montados")
    steam_verify = steam_commands.add_parser("verify", help="confere symlink, manifesto e hash da árvore centralizada")
    steam_verify.add_argument("--operation-id", required=True)
    steam_rollback = steam_commands.add_parser("rollback", help="restaura a pasta Steam original após validação")
    steam_rollback.add_argument("--operation-id", required=True)
    steam_rollback.add_argument("--rollback", action="store_true")
    steam_saves = steam_commands.add_parser("saves", help="descobre e centraliza saves Steam Cloud/Auto-Cloud")
    steam_saves_commands = steam_saves.add_subparsers(dest="steam_saves_command", required=True)
    steam_saves_discover = steam_saves_commands.add_parser("discover", help="inventa saves sem alterar as origens")
    steam_saves_discover.add_argument("--offline", action="store_true")
    steam_saves_discover.add_argument("--no-mounted-scan", action="store_true")
    steam_saves_plan = steam_saves_commands.add_parser("plan", help="prepara links de saves em plano revisável")
    steam_saves_choice = steam_saves_plan.add_mutually_exclusive_group(required=True)
    steam_saves_choice.add_argument("--all-games", action="store_true")
    steam_saves_choice.add_argument("--app-id", help="limita a um AppID Steam")
    steam_saves_plan.add_argument("--library", help="seleciona uma Steam library se o AppID for ambíguo")
    steam_saves_plan.add_argument("--offline", action="store_true")
    steam_saves_plan.add_argument("--no-mounted-scan", action="store_true")
    steam_saves_verify = steam_saves_commands.add_parser("verify", help="valida symlinks e conteúdo dos saves")
    steam_saves_verify.add_argument("--operation-id", required=True)
    steam_saves_rollback = steam_saves_commands.add_parser("rollback", help="restaura os caminhos originais dos saves")
    steam_saves_rollback.add_argument("--operation-id", required=True)
    steam_saves_rollback.add_argument("--rollback", action="store_true")
    steam_watch = steam_commands.add_parser("watch", help="vigia conexões de discos e novos jogos instalados")
    steam_watch.add_argument("--interval", type=int, default=60)
    steam_watch.add_argument("--apply-new-games", action="store_true",
                             help="aplica planos aprovados automaticamente quando Steam estiver fechado")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "storage":
            if args.storage_action in {"apply", "resume"}:
                with media_session.write_lock(globals()):
                    result = storage_dedupe.dispatch(args, globals())
            else:
                result = storage_dedupe.dispatch(args, globals())
        elif args.command == "discs":
            if __package__:
                from . import disc_sets
            else:
                import disc_sets
            result = disc_sets.dispatch(args, globals())
        elif args.command == "inventory":
            result = run_inventory([Path(x) for x in args.root], Path(args.out), args.hash)
        elif args.command == "verify":
            paths = discover_paths(args)
            if not paths:
                raise ValueError("Nenhum caminho selecionado")
            result = record_validation(paths, Path(args.out), jobs=args.jobs,
                                       list_archive_contents=args.list_archive_contents)
        elif args.command == "plan":
            result = plan_actions(Path(args.file))
        elif args.command == "apply":
            # F07: every library mutation shares the single media/library write reservation.
            with media_session.write_lock(globals()):
                result = apply_operation(args.operation_id, args.apply)
        elif args.command == "discard":
            result = discard_plan(args.operation_id, args.reason, args.apply)
        elif args.command == "project-catalog":
            result = catalog_projection.dispatch(args, globals())
        elif args.command == "rollback":
            with media_session.write_lock(globals()):
                result = rollback_operation(args.operation_id, args.rollback)
        elif args.command == "compact":
            if args.compact_command == "plan":
                result = command_compact_plan(args)
            elif args.compact_command == "apply":
                if getattr(args,'allow_active_frontend',False):
                    result=command_compact_apply(args)
                else:
                    if any(frontend_processes_running().values()):raise RuntimeError('Feche RetroFE e ES-DE normalmente antes de aplicar a compactação')
                    with media_session.write_lock(globals()):result=command_compact_apply(args)
            elif args.compact_command == "verify":
                result = command_compact_verify(args)
            elif args.compact_command == "rollback":
                if any(frontend_processes_running().values()):raise RuntimeError('Feche RetroFE e ES-DE normalmente antes do rollback')
                with media_session.write_lock(globals()):result=command_compact_rollback(args)
            else:
                raise ValueError(args.compact_command)
        elif args.command == "duplicate-audit":
            auditor = logical_duplicate_audit if args.logical else duplicate_audit
            result = auditor(Path(args.root), Path(args.out), args.system_id)
        elif args.command == "dat-match":
            result = dat_match([Path(x) for x in args.root], Path(args.dat), args.system_id,
                               Path(args.out), args.raw_extensions, [Path(x) for x in args.path])
        elif args.command == "history":
            rebuild_history_index()
            result = {"index": str(OPS_ROOT / "history-index.csv")}
        elif args.command == "archive-classify":
            result = command_archive_classify(args)
        elif args.command == "close-interrupted":
            manifest_path = operation_dir(args.operation_id) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "running":
                raise ValueError(f"Operação não está running: {manifest.get('status')}")
            manifest["status"] = "interrupted"
            manifest["completed_at"] = now()
            manifest["interruption_reason"] = args.reason
            atomic_json(manifest_path, manifest)
            append_event(args.operation_id, "validation_interrupted", action=manifest.get("kind", "verify"),
                         status="interrupted", details={"reason": args.reason,
                                                        "completed_items": len(manifest.get("items", []))})
            rebuild_history_index()
            result = {"operation_id": args.operation_id, "status": "interrupted",
                      "completed_items": len(manifest.get("items", [])), "manifest": str(manifest_path)}
        elif args.command == "record-change":
            result = command_record_change(args)
        elif args.command == "record-artifacts":
            result = command_record_artifacts(args)
        elif args.command == "media":
            if args.media_command == "verify":
                result = command_media_verify(args)
            elif args.media_command == "covers":
                if args.media_covers_command in {"apply", "rollback"}:
                    with media_session.write_lock(globals()):
                        result = media_covers.dispatch(args, globals())
                else:
                    result = media_covers.dispatch(args, globals())
            elif args.media_command == "optimize":
                if args.media_optimize_command in {"apply","rollback"}:
                    with media_session.write_lock(globals()):
                        result = media_optimizer.dispatch(args, globals())
                else:
                    result = media_optimizer.dispatch(args, globals())
            elif args.media_command == "session":
                result = media_session.dispatch(args,globals())
            elif args.media_command == "stray":
                if args.media_stray_command in {"apply", "rollback"}:
                    with media_session.write_lock(globals()):
                        result = media_stray.dispatch(args, globals())
                else:
                    result = media_stray.dispatch(args, globals())
            elif args.media_command == "esde":
                if args.esde_media_command in {"apply","rollback"} or (args.esde_media_command=="userdata" and args.esde_userdata_command in {"apply","rollback"}) or (args.esde_media_command=="aliases" and args.esde_aliases_command in {"apply","rollback","ensure"}):
                    with media_session.write_lock(globals()):result = esde_media.dispatch(args, globals())
                else:result = esde_media.dispatch(args, globals())
            else:
                raise ValueError(args.media_command)
        elif args.command == "variants":
            if args.variants_command in {"organize", "names"}:
                mutating = any(getattr(args, attr, None) in {"apply", "rollback"} for attr in vars(args) if attr.endswith("_command"))
                with (media_session.write_lock(globals()) if mutating else contextlib.nullcontext()):
                    result = variant_organization.dispatch(args, globals())
            else:
                result = variant_metadata.dispatch(args, globals())
        elif args.command == "hierarchy":
            with (media_session.write_lock(globals()) if args.hierarchy_command in {"apply", "rollback"} else contextlib.nullcontext()):
                result = collection_hierarchy.dispatch(args, globals())
        elif args.command == "capture":
            result = save_capture.dispatch(
                args, project_root=PROJECT_ROOT, output_root=OUTPUT_ROOT, ops_root=OPS_ROOT,
                record_analysis=record_analysis,
                atomic_json=atomic_json, append_event=append_event,
                rebuild_history_index=rebuild_history_index, now=now)
        elif args.command == "steam":
            if args.steam_command == "discover":
                result = command_steam_discover(args)
            elif args.steam_command == "plan":
                result = command_steam_plan(args)
            elif args.steam_command == "verify":
                result = command_steam_verify(args)
            elif args.steam_command == "rollback":
                result = rollback_operation(args.operation_id, args.rollback)
            elif args.steam_command == "watch":
                result = command_steam_watch(args)
            elif args.steam_command == "saves":
                if args.steam_saves_command == "discover":
                    result = command_steam_saves_discover(args)
                elif args.steam_saves_command == "plan":
                    result = command_steam_saves_plan(args)
                elif args.steam_saves_command == "verify":
                    result = command_steam_saves_verify(args)
                elif args.steam_saves_command == "rollback":
                    result = rollback_operation(args.operation_id, args.rollback)
                else:
                    raise ValueError(args.steam_saves_command)
            else:
                raise ValueError(args.steam_command)
        elif args.command == "steam-shortcuts":
            result = steam_shortcuts.dispatch(args, globals())
        else:
            raise ValueError(args.command)
        if args.command in {"verify", "inventory"}:
            statuses = {}
            for item in result.get("items", []):
                key = item.get("status", "unknown")
                statuses[key] = statuses.get(key, 0) + 1
            summary = {key: result.get(key) for key in
                       ("operation_id", "kind", "status", "entry_count", "errors", "csv", "manifest")}
            summary["items"] = len(result.get("items", []))
            summary["item_status_counts"] = statuses
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command in {"duplicate-audit", "dat-match", "archive-classify", "media", "capture"}:
            items = result.get("items", [])
            summary = {key: result.get(key) for key in
                       ("operation_id", "kind", "status", "item_count", "candidate_count", "manifest", "csv",
                        "audit_csv", "report", "verified", "failed", "errors", "regular_files", "logical_bytes",
                        "extension_counts", "audit_status_counts", "summary", "event_counts", "child_exit_code")}
            # F03: never drop the domain diagnosis (issues/blockers) from the CLI projection.
            for key in ("issues", "blockers", "reason", "media_root", "platform_directories"):
                if key in result:
                    summary[key] = result[key]
            summary["items"] = len(items)
            summary["item_status_counts"] = dict(
                __import__("collections").Counter(x.get("status", "unknown") for x in items)
            )
            summary["evidence"] = result.get("evidence", {})
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command == "compact":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "media" and getattr(args,"media_command",None) == "session" and getattr(args,"media_session_command",None) == "run":
            code=result.get("child_exit_code",0)
            return code if 0 <= code <= 255 else 1
        if args.command == "storage" and result.get("status") in {"partial", "failed", "interrupted"}:
            return 1
        if args.command == "steam-shortcuts" and result.get("status") == "diverged":
            return 1
        if (args.command == "media" and getattr(args, "media_command", None) == "stray"
                and getattr(args, "media_stray_command", None) == "status"
                and result.get("status") == "reconciliation_required"):
            return 3
        if (args.command == "media" and getattr(args, "media_command", None) == "esde"
                and (getattr(args, "esde_media_command", None) == "validate"
                     or (getattr(args, "esde_media_command", None) == "aliases" and getattr(args, "esde_aliases_command", None) in {"ensure", "verify"}))
                and result.get("status") not in {"ready", "verified"}):
            # F03: a launch gate that is not ready must not look like success to launchers/Preview.
            for issue in result.get("issues") or [f"status {result.get('status')!r} sem causa registrada"]:
                print(f"romtool: media esde {args.esde_media_command} bloqueado: {issue}", file=sys.stderr)
            return 3
        return 0
    except Exception as exc:
        print(f"romtool: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
