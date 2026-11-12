"""Steam library discovery helpers used by the romtool Steam commands."""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath


SOFTWARE_GENRES = {
    "accounting", "animation & modeling", "audio production", "design & illustration",
    "game development", "photo editing", "software training", "utilities",
    "video production", "web publishing",
}
SKIP_DIRECTORY_NAMES = {
    "$recycle.bin", ".trash", ".trash-1000", "lost+found", "system volume information",
    "windows", "programdata", "node_modules", ".git", ".cache",
}
SKIP_FILESYSTEMS = {
    "autofs", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts", "devtmpfs",
    "efivarfs", "fusectl", "mqueue", "overlay", "proc", "pstore", "securityfs", "squashfs",
    "sysfs", "tmpfs", "tracefs",
}
BLOCK_FILESYSTEMS = {"btrfs", "exfat", "ext2", "ext3", "ext4", "fat", "fat32", "fuseblk",
                     "hfsplus", "msdos", "ntfs", "ntfs3", "vfat", "xfs"}
MAX_MOUNT_SEARCH_DEPTH = 6
MAX_MOUNT_SEARCH_DIRECTORIES = 25000
MOUNT_SCAN_TIMEOUT_SECONDS = 12
METADATA_CACHE_DAYS = 30
SYSTEM_MOUNT_PREFIXES = ("/var", "/usr", "/etc", "/boot")
AUTO_CLOUD_PROFILE_ROOTS = {
    "2": "Documents",
    "3": "AppData/Local",
    "4": "AppData/LocalLow",
    "5": "AppData/Roaming",
    "6": "Saved Games",
}


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _casefold_get(data: dict, key: str, default=None):
    target = key.casefold()
    for current_key, value in data.items():
        if str(current_key).casefold() == target:
            return value
    return default


def _vdf_tokens(text: str):
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = length if end < 0 else end + 1
            continue
        if char in "{}":
            yield char
            index += 1
            continue
        if char == '"':
            index += 1
            value = []
            while index < length:
                char = text[index]
                if char == '"':
                    index += 1
                    break
                if char == "\\" and index + 1 < length:
                    escaped = text[index + 1]
                    value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
                    index += 2
                else:
                    value.append(char)
                    index += 1
            else:
                raise ValueError("String VDF sem aspas de fechamento")
            yield "".join(value)
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in "{}\"":
            if text.startswith("//", index):
                break
            index += 1
        if index == start:
            raise ValueError(f"Token VDF inválido no byte {index}")
        yield text[start:index]


def parse_vdf(text: str) -> dict:
    """Parse the quoted KeyValues subset used by Steam .acf and libraryfolders files."""
    tokens = iter(_vdf_tokens(text))
    sentinel = object()
    lookahead = next(tokens, sentinel)

    def parse_object(nested: bool) -> dict:
        nonlocal lookahead
        result = {}
        while lookahead is not sentinel:
            if lookahead == "}":
                if not nested:
                    raise ValueError("Fechamento VDF sem abertura")
                lookahead = next(tokens, sentinel)
                return result
            key = lookahead
            if key == "{":
                raise ValueError("Objeto VDF sem chave")
            lookahead = next(tokens, sentinel)
            if lookahead is sentinel or lookahead == "}":
                raise ValueError(f"Valor ausente para chave VDF {key!r}")
            if lookahead == "{":
                lookahead = next(tokens, sentinel)
                value = parse_object(True)
            else:
                value = lookahead
                lookahead = next(tokens, sentinel)
            result[str(key)] = value
        if nested:
            raise ValueError("Objeto VDF sem chave de fechamento")
        return result

    return parse_object(False)


def read_vdf(path: Path) -> dict:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError(f"VDF excessivamente grande: {path}")
    return parse_vdf(path.read_text(encoding="utf-8-sig", errors="strict"))


def _unescape_mount_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def mounted_block_mountpoints(mountinfo: Path = Path("/proc/self/mountinfo")) -> list[dict]:
    """Return mounted block-backed filesystems, including USB, SD, NTFS and exFAT."""
    rows = {}
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        halves = line.split(" - ", 1)
        if len(halves) != 2:
            continue
        before, after = halves
        left, right = before.split(), after.split()
        if len(left) < 5 or len(right) < 2:
            continue
        mountpoint = Path(_unescape_mount_field(left[4]))
        fs_type, source = right[0].casefold(), _unescape_mount_field(right[1])
        if str(mountpoint) in {"/", "/home", "/boot", "/boot/efi", "/swapfile"}:
            continue
        try:
            if any(os.path.commonpath((str(mountpoint), prefix)) == prefix
                   for prefix in SYSTEM_MOUNT_PREFIXES):
                continue
        except ValueError:
            continue
        if fs_type in SKIP_FILESYSTEMS:
            continue
        if not source.startswith("/dev/") and fs_type not in BLOCK_FILESYSTEMS:
            continue
        if not mountpoint.is_dir():
            continue
        rows[str(mountpoint)] = {"mountpoint": str(mountpoint), "filesystem": fs_type,
                                 "source": source, "device_id": left[2]}
    return sorted(rows.values(), key=lambda row: row["mountpoint"].casefold())


def mount_signature() -> str:
    rows = mounted_block_mountpoints()
    return "\n".join(f"{row['mountpoint']}\0{row['source']}\0{row['filesystem']}" for row in rows)


def _mount_id_for(path: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> str | None:
    absolute = Path(os.path.abspath(path))
    matches = []
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        halves = line.split(" - ", 1)
        if len(halves) != 2:
            continue
        left = halves[0].split()
        if len(left) < 5:
            continue
        mountpoint = Path(_unescape_mount_field(left[4]))
        try:
            if os.path.commonpath((str(absolute), str(mountpoint))) == str(mountpoint):
                matches.append((len(str(mountpoint)), left[0]))
        except ValueError:
            continue
    return max(matches)[1] if matches else None


def same_rename_domain(first: Path, second: Path) -> bool:
    """Whether rename can stay within the same mounted filesystem/subvolume."""
    first_mount = _mount_id_for(first)
    second_mount = _mount_id_for(second)
    if first_mount is not None and second_mount is not None:
        return first_mount == second_mount
    try:
        return first.stat().st_dev == second.stat().st_dev
    except OSError:
        return False


def _steam_root_candidates(home: Path) -> list[Path]:
    candidates = []
    configured = os.environ.get("STEAM_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".steam/root",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/data/Steam",
        home / "snap/steam/common/.local/share/Steam",
    ])
    return candidates


def _find_child_casefold(parent: Path, wanted: str) -> Path | None:
    try:
        for child in parent.iterdir():
            if child.name.casefold() == wanted.casefold():
                return child
    except OSError:
        return None
    return None


def _steamapps_path(library: Path) -> Path | None:
    path = _find_child_casefold(library, "steamapps")
    return path if path and path.is_dir() and not path.is_symlink() else None


def _is_steam_root(root: Path) -> bool:
    if any((root / marker).is_file() for marker in ("steam.sh", "config/config.vdf")):
        return True
    steamapps = _steamapps_path(root)
    libraryfolders = _find_child_casefold(steamapps, "libraryfolders.vdf") if steamapps else None
    return bool(libraryfolders and libraryfolders.is_file() and not libraryfolders.is_symlink())


def _library_entries(root: Path, steamapps: Path) -> tuple[list[dict], list[dict]]:
    file = _find_child_casefold(steamapps, "libraryfolders.vdf")
    if not file or not file.is_file():
        return [], []
    try:
        parsed = read_vdf(file)
        content = _casefold_get(parsed, "libraryfolders", parsed)
        if not isinstance(content, dict):
            raise ValueError("Chave libraryfolders não é um objeto")
    except Exception as exc:
        return [], [{"path": str(file), "status": "libraryfolders_read_error", "error": str(exc)}]
    entries, errors = [], []
    for index, value in content.items():
        if not str(index).isdigit():
            continue
        if isinstance(value, dict):
            raw_path = _casefold_get(value, "path", "")
            apps = _casefold_get(value, "apps", {})
        else:
            raw_path, apps = value, {}
        if not isinstance(raw_path, str) or not raw_path:
            continue
        raw_path = raw_path.replace("\\\\", "\\").replace("\\", os.sep)
        library = Path(raw_path).expanduser()
        if not library.is_absolute():
            errors.append({"path": raw_path, "status": "library_path_not_absolute"})
            continue
        entry = {"library_path": str(library), "libraryfolders_path": str(file),
                 "registered_appids": sorted(str(x) for x in apps) if isinstance(apps, dict) else []}
        entries.append(entry)
    return entries, errors


def _walk_steamapps(mountpoint: Path) -> tuple[list[Path], list[str]]:
    found, errors = [], []
    base = mountpoint.resolve(strict=True)
    visited = 0
    for current, directories, _files in os.walk(base, topdown=True, followlinks=False):
        path = Path(current)
        depth = len(path.relative_to(base).parts)
        visited += 1
        if visited > MAX_MOUNT_SEARCH_DIRECTORIES:
            errors.append(f"search_limit_reached:{mountpoint}")
            break
        if path.name.casefold() == "steamapps":
            found.append(path)
            directories[:] = []
            continue
        if depth >= MAX_MOUNT_SEARCH_DEPTH:
            directories[:] = []
            continue
        kept = []
        for name in directories:
            child = path / name
            if name.casefold() in SKIP_DIRECTORY_NAMES or child.is_symlink():
                continue
            kept.append(name)
        directories[:] = kept
    return found, errors


def _walk_steamapps_worker(connection, mountpoint: Path) -> None:
    try:
        connection.send({"result": _walk_steamapps(mountpoint)})
    except BaseException as exc:
        connection.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        connection.close()


def _walk_steamapps_bounded(mountpoint: Path, timeout: int = MOUNT_SCAN_TIMEOUT_SECONDS) -> tuple[list[Path], list[str]]:
    """Isolate removable-filesystem traversal so an unresponsive FUSE mount cannot stall the watcher."""
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_walk_steamapps_worker, args=(sender, mountpoint), daemon=True)
    try:
        process.start()
        sender.close()
        if receiver.poll(timeout):
            try:
                response = receiver.recv()
            except EOFError:
                return [], [f"mount_scan_worker_exited_without_result:{mountpoint}"]
            process.join(timeout=1)
            if "error" in response:
                return [], [f"mount_scan_error:{mountpoint}:{response['error']}"]
            result, errors = response.get("result", ([], []))
            return result, errors
        process.terminate()
        process.join(timeout=1)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=1)
        return [], [f"mount_scan_timeout:{mountpoint}:{timeout}s"]
    finally:
        receiver.close()
        sender.close()
        if process.is_alive():
            process.terminate()


def discover_steam_libraries(home: Path | None = None, scan_mounts: bool = True) -> dict:
    home = (home or Path.home()).expanduser()
    roots = {}
    libraries = {}
    errors = []
    unmounted = {}

    def add_library(raw_library: Path, registered_by: str = "", registered_appids=(), root_marker=False,
                    libraryfolders_path: str = "") -> None:
        try:
            library = raw_library.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            unmounted[str(raw_library)] = {"library_path": str(raw_library), "status": "library_unmounted",
                                           "registered_by": registered_by,
                                           "libraryfolders_path": libraryfolders_path}
            return
        steamapps = _steamapps_path(library)
        if not steamapps:
            errors.append({"path": str(library), "status": "steamapps_missing"})
            return
        key = str(library)
        item = libraries.setdefault(key, {
            "library_path": key, "steamapps_path": str(steamapps), "registered_by": [],
            "registered_appids": [], "is_steam_root": False, "trusted": False,
            "libraryfolders_paths": [],
        })
        if registered_by and registered_by not in item["registered_by"]:
            item["registered_by"].append(registered_by)
        item["registered_appids"] = sorted(set(item["registered_appids"]) | {str(x) for x in registered_appids})
        item["is_steam_root"] = item["is_steam_root"] or root_marker or _is_steam_root(library)
        if libraryfolders_path and libraryfolders_path not in item["libraryfolders_paths"]:
            item["libraryfolders_paths"].append(libraryfolders_path)
        item["trusted"] = bool(item["registered_by"] or item["is_steam_root"])

    def add_root(raw_root: Path) -> None:
        try:
            root = raw_root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return
        if not root.is_dir() or not _is_steam_root(root):
            return
        root_key = str(root)
        roots[root_key] = {"root_path": root_key, "evidence": "Steam root marker"}
        steamapps = _steamapps_path(root)
        if not steamapps:
            return
        add_library(root, root_marker=True)
        entries, entry_errors = _library_entries(root, steamapps)
        errors.extend(entry_errors)
        for entry in entries:
            add_library(Path(entry["library_path"]), registered_by=root_key,
                        registered_appids=entry["registered_appids"], root_marker=False,
                        libraryfolders_path=entry["libraryfolders_path"])

    for candidate in _steam_root_candidates(home):
        add_root(candidate)

    mounts = mounted_block_mountpoints() if scan_mounts else []
    if scan_mounts:
        for mount in mounts:
            mountpoint = Path(mount["mountpoint"])
            steamapps_dirs, search_errors = _walk_steamapps_bounded(mountpoint)
            errors.extend({"path": str(mountpoint), "status": error} for error in search_errors)
            for steamapps in steamapps_dirs:
                library = steamapps.parent
                if _is_steam_root(library):
                    add_root(library)
                add_library(library)

    for item in libraries.values():
        item["registered_by"].sort(key=str.casefold)
        item["libraryfolders_paths"].sort(key=str.casefold)
        item["mountpoints"] = [mount["mountpoint"] for mount in mounts
                               if os.path.commonpath((item["library_path"], mount["mountpoint"])) == mount["mountpoint"]]
    return {
        "roots": sorted(roots.values(), key=lambda row: row["root_path"].casefold()),
        "libraries": sorted(libraries.values(), key=lambda row: row["library_path"].casefold()),
        "unmounted_libraries": sorted(unmounted.values(), key=lambda row: row["library_path"].casefold()),
        "mounts": mounts,
        "errors": errors,
    }


def _read_metadata_cache(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("apps", {}) if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _metadata_fresh(row: dict) -> bool:
    try:
        fetched = dt.datetime.fromisoformat(row.get("fetched_at", ""))
        age = (dt.datetime.now().astimezone() - fetched.astimezone()).total_seconds()
    except (ValueError, TypeError):
        return False
    lifetime = METADATA_CACHE_DAYS * 86400 if row.get("success") else 300
    return age <= lifetime


def _fetch_store_metadata(appid: str, timeout: float) -> dict:
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
    request = urllib.request.Request(url, headers={"User-Agent": "romtool-steam-centralizer/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(8 * 1024 * 1024 + 1)
    if len(body) > 8 * 1024 * 1024:
        raise ValueError("Steam Store metadata exceeded size limit")
    outer = json.loads(body.decode("utf-8"))
    result = outer.get(appid, {})
    if not result.get("success") or not isinstance(result.get("data"), dict):
        return {"success": False, "type": "", "name": "", "genres": [], "categories": [],
                "source": "Steam Store appdetails", "fetched_at": timestamp()}
    data = result["data"]
    return {
        "success": True,
        "type": str(data.get("type", "")).casefold(),
        "name": str(data.get("name", "")),
        "genres": sorted({str(x.get("description", "")) for x in (data.get("genres") or []) if isinstance(x, dict)}),
        "categories": sorted({str(x.get("description", "")) for x in (data.get("categories") or []) if isinstance(x, dict)}),
        "source": "Steam Store appdetails",
        "fetched_at": timestamp(),
    }


def _store_metadata(appid: str, cache: dict, online: bool, timeout: float) -> tuple[dict, bool]:
    existing = cache.get(appid, {})
    if existing and _metadata_fresh(existing):
        return existing, False
    if not online:
        return existing or {"success": False, "type": "", "name": "", "genres": [], "categories": [],
                            "source": "unavailable_offline", "fetched_at": ""}, False
    try:
        result = _fetch_store_metadata(appid, timeout)
    except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
        if existing:
            return existing, False
        result = {"success": False, "type": "", "name": "", "genres": [], "categories": [],
                  "source": "Steam Store appdetails", "fetched_at": timestamp(),
                  "error": f"{type(exc).__name__}: {exc}"}
    cache[appid] = result
    return result, True


def _safe_game_directory_name(name: str) -> str:
    value = unicodedata.normalize("NFC", name).strip().replace("/", " - ").replace("\\", " - ")
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip(" .")
    if not value or value in {".", ".."} or Path(value).name != value:
        raise ValueError(f"Nome Steam não pode ser usado como pasta: {name!r}")
    return value


def _registered_app_id(appid: str, library: dict) -> bool:
    return (library.get("is_steam_root")
            or appid in set(library.get("registered_appids", [])))


def _install_directory(library: Path, steamapps: Path, install_dir: str) -> Path | None:
    if not install_dir or install_dir in {".", ".."} or "/" in install_dir or "\\" in install_dir or "\x00" in install_dir:
        return None
    common = _find_child_casefold(steamapps, "common")
    if not common or not common.is_dir() or common.is_symlink():
        return None
    direct = common / install_dir
    if os.path.lexists(direct):
        return direct
    return _find_child_casefold(common, install_dir)


def _already_centralized(source: Path, destination: Path) -> bool:
    if not source.is_symlink() or not destination.exists():
        return False
    try:
        return source.resolve(strict=True) == destination.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def collect_installed_apps(project_root: Path, cache_path: Path, online: bool = True,
                           scan_mounts: bool = True, timeout: float = 6.0) -> tuple[list[dict], dict]:
    project_root = project_root.resolve()
    discovery = discover_steam_libraries(scan_mounts=scan_mounts)
    cache = _read_metadata_cache(cache_path)
    rows = []
    updated_cache = False
    canonical_root = project_root / "roms" / "steam"

    for library in discovery["libraries"]:
        steamapps = Path(library["steamapps_path"])
        try:
            manifests = sorted((p for p in steamapps.iterdir()
                                if re.fullmatch(r"appmanifest_(\d+)\.acf", p.name, re.IGNORECASE)),
                               key=lambda p: p.name.casefold())
        except OSError as exc:
            discovery["errors"].append({"path": str(steamapps), "status": "steamapps_read_error", "error": str(exc)})
            continue
        for manifest_path in manifests:
            match = re.fullmatch(r"appmanifest_(\d+)\.acf", manifest_path.name, re.IGNORECASE)
            appid = match.group(1)
            row = {
                "appid": appid, "library_path": library["library_path"],
                "steamapps_path": library["steamapps_path"], "manifest_path": str(manifest_path),
                "registered_by": list(library["registered_by"]),
                "library_is_steam_root": library.get("is_steam_root", False),
                "library_trusted": library.get("trusted", False),
                "registered_app_id": _registered_app_id(appid, library),
                "source_path": "", "destination_path": "", "manifest_name": "",
                "official_name": "", "app_type": "", "genres": [], "categories": [],
                "state_flags": None, "manifest_size_on_disk": None, "bytes_to_download": None,
                "status": "manifest_read_error", "eligible": False,
            }
            try:
                content = _casefold_get(read_vdf(manifest_path), "AppState", {})
                if not isinstance(content, dict):
                    raise ValueError("AppState ausente")
                manifest_appid = str(_casefold_get(content, "appid", ""))
                manifest_name = str(_casefold_get(content, "name", ""))
                install_dir = str(_casefold_get(content, "installdir", ""))
                state_flags = int(str(_casefold_get(content, "StateFlags", "0")), 10)
                size_value = _casefold_get(content, "SizeOnDisk", "")
                download_value = _casefold_get(content, "BytesToDownload", "")
                if manifest_appid != appid:
                    raise ValueError(f"AppID do manifesto {manifest_appid!r} diverge do nome {appid}")
                row.update({"manifest_name": manifest_name, "state_flags": state_flags,
                            "manifest_size_on_disk": int(size_value) if str(size_value).isdigit() else None,
                            "bytes_to_download": int(download_value) if str(download_value).isdigit() else None,
                            "install_dir": install_dir})
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                continue

            if not (row["state_flags"] & 4):
                row["status"] = "not_fully_installed"
                rows.append(row)
                continue
            if not row["registered_app_id"]:
                row["status"] = "library_not_registered_by_official_steam_root"
                rows.append(row)
                continue

            source = _install_directory(Path(library["library_path"]), steamapps, row["install_dir"])
            if source is None:
                row["status"] = "install_directory_missing_or_invalid"
                rows.append(row)
                continue
            source = source.absolute()
            row["source_path"] = str(source)
            metadata, changed = _store_metadata(appid, cache, online, timeout)
            updated_cache = updated_cache or changed
            row.update({"official_name": metadata.get("name", ""), "app_type": metadata.get("type", ""),
                        "genres": metadata.get("genres", []), "categories": metadata.get("categories", []),
                        "metadata_source": metadata.get("source", ""),
                        "metadata_fetched_at": metadata.get("fetched_at", ""),
                        "metadata_error": metadata.get("error", "")})
            display_name = row["official_name"] or row["manifest_name"]
            if not display_name:
                row["status"] = "official_name_unavailable"
                rows.append(row)
                continue
            try:
                directory_name = _safe_game_directory_name(display_name)
            except ValueError as exc:
                row["status"] = "unsafe_official_name"
                row["error"] = str(exc)
                rows.append(row)
                continue
            destination = canonical_root / directory_name
            row.update({"canonical_name": directory_name, "destination_path": str(destination)})

            if source.is_symlink():
                row["status"] = "already_centralized" if _already_centralized(source, destination) else "source_symlink_conflict"
                row["eligible"] = row["status"] == "already_centralized"
                rows.append(row)
                continue
            if not source.is_dir():
                row["status"] = "install_directory_missing"
                rows.append(row)
                continue
            if metadata.get("type") == "demo":
                row["status"] = "demo_excluded_from_game_migration"
                rows.append(row)
                continue
            if not metadata.get("success") or not metadata.get("type"):
                row["status"] = "store_type_unverified"
                rows.append(row)
                continue
            if metadata.get("type") != "game":
                row["status"] = f"non_game_app_type:{metadata.get('type')}"
                rows.append(row)
                continue
            software_genres = sorted({str(value).casefold() for value in metadata.get("genres", [])}
                                     & SOFTWARE_GENRES)
            if software_genres:
                row["status"] = "steam_software_genre_excluded"
                row["software_genres"] = software_genres
                rows.append(row)
                continue
            if not library.get("trusted"):
                row["status"] = "library_trust_unverified"
                rows.append(row)
                continue
            if os.path.lexists(destination):
                row["status"] = "canonical_destination_collision"
                rows.append(row)
                continue
            if canonical_root.is_dir():
                folded = {child.name.casefold() for child in canonical_root.iterdir()}
                if directory_name.casefold() in folded:
                    row["status"] = "canonical_casefold_collision"
                    rows.append(row)
                    continue
            row["status"] = "eligible_game"
            row["eligible"] = True
            rows.append(row)

    if updated_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(cache_path.name + ".tmp")
        temporary.write_text(json.dumps({"schema_version": "1.0", "updated_at": timestamp(),
                                        "apps": cache}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, cache_path)

    collisions = {}
    for row in rows:
        if row.get("status") == "eligible_game":
            collisions.setdefault(row.get("canonical_name", "").casefold(), []).append(row)
    for candidates in collisions.values():
        if len(candidates) < 2:
            continue
        paths = sorted({row["source_path"] for row in candidates}, key=str.casefold)
        for row in candidates:
            row["status"] = "duplicate_install_destination_ambiguous"
            row["eligible"] = False
            row["collision_source_paths"] = paths

    rows.sort(key=lambda row: ((row.get("official_name") or row.get("manifest_name") or "").casefold(),
                               row.get("library_path", "").casefold(), row.get("appid", "")))
    evidence = {
        "official_roots": discovery["roots"], "steam_libraries": discovery["libraries"],
        "unmounted_libraries": discovery["unmounted_libraries"], "mounted_block_devices": discovery["mounts"],
        "discovery_errors": discovery["errors"], "metadata_endpoint": "https://store.steampowered.com/api/appdetails?appids=<appid>",
        "metadata_policy": "only type=game without software-only genres is eligible; successful cache TTL 30 days, failed lookup TTL 5 minutes",
        "symlink_policy": "original Steam common/<installdir> remains as a symlink to roms/steam/<official title>",
    }
    return rows, evidence


def _safe_cloud_relative(value: str) -> PurePosixPath | None:
    normalized = str(value).replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    return PurePosixPath(*[part for part in relative.parts if part not in ("", ".")])


def _cloud_manifest_entries(path: Path, appid: str) -> list[dict]:
    try:
        content = read_vdf(path)
    except (OSError, ValueError, UnicodeError):
        return []
    content = _casefold_get(content, appid, content)
    entries = []

    def walk(value, prefix: str = "") -> None:
        if not isinstance(value, dict):
            return
        folded = {str(key).casefold(): child for key, child in value.items()}
        if {"root", "size", "sha"}.issubset(folded):
            relative = _safe_cloud_relative(prefix)
            size, digest = str(folded["size"]), str(folded["sha"])
            if relative and size.isdigit() and re.fullmatch(r"[0-9a-fA-F]{40}", digest):
                entries.append({"relative_path": relative.as_posix(), "root_id": str(folded["root"]),
                                "size_bytes": int(size), "sha1": digest.casefold(),
                                "sync_state": str(folded.get("syncstate", ""))})
            return
        for key, child in value.items():
            key_text = str(key).strip("/")
            next_prefix = f"{prefix.rstrip('/')}/{key_text}" if prefix else key_text
            walk(child, next_prefix)

    walk(content)
    return entries


def _sha1_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha1()
    with path.open("rb", buffering=0) as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _has_payload_files(root: Path, exclude_autocloud_marker: bool = False) -> bool:
    if not root.is_dir() or root.is_symlink():
        return False
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
        for filename in filenames:
            if exclude_autocloud_marker and filename.casefold() == "steam_autocloud.vdf":
                continue
            candidate = current_path / filename
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    return True
            except OSError:
                continue
    return False


def _candidate_save_row(project_root: Path, game: dict, source: Path, destination: Path,
                        source_kind: str, source_root: Path, source_relative_path: str,
                        evidence: dict) -> dict:
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    canonical_save_root = project_root / "saves" / "steam" / game["canonical_name"]
    status = "eligible_save"
    eligible = True
    if source.is_symlink():
        try:
            resolved = source.resolve(strict=True)
            if os.path.commonpath((str(resolved), str(canonical_save_root.resolve(strict=False)))) == str(canonical_save_root.resolve(strict=False)):
                status, eligible = "already_centralized", False
            else:
                status, eligible = "save_symlink_conflict", False
        except (OSError, RuntimeError, ValueError):
            status, eligible = "save_symlink_conflict", False
    elif not os.path.lexists(source) or not (source.is_file() or source.is_dir()):
        status, eligible = "save_source_missing_or_invalid", False
    elif os.path.lexists(destination):
        status, eligible = "save_destination_collision", False
    variant = f"Steam AppID {game['appid']} save {source_kind}:{source_relative_path}"
    steam_data = {
        "appid": str(game["appid"]), "official_name": game["official_name"],
        "app_type": game["app_type"], "library_path": game["library_path"],
        "steamapps_path": game["steamapps_path"], "manifest_path": game["manifest_path"],
        "manifest_sha256": evidence.get("manifest_sha256", ""),
        "manifest_name": game.get("manifest_name", ""), "install_dir": game["install_dir"],
        "canonical_game_path": game["destination_path"],
        "steam_root_path": evidence.get("steam_root_path", ""),
        "source_kind": source_kind, "source_root": str(source_root),
        "source_relative_path": source_relative_path,
        "cloud_manifest_path": evidence.get("cloud_manifest_path", ""),
        "cloud_account_id": evidence.get("cloud_account_id", ""),
        "cloud_root_id": evidence.get("cloud_root_id", ""),
        "cloud_relative_path": evidence.get("cloud_relative_path", ""),
        "cloud_size_bytes": evidence.get("cloud_size_bytes"),
        "cloud_sha1": evidence.get("cloud_sha1", ""),
        "autocloud_marker": evidence.get("autocloud_marker", ""),
        "catalog_key": variant,
    }
    return {
        "appid": str(game["appid"]), "official_name": game["official_name"],
        "library_path": game["library_path"], "steamapps_path": game["steamapps_path"],
        "manifest_path": game["manifest_path"], "source_path": str(source),
        "destination_path": str(destination), "source_kind": source_kind,
        "source_root": str(source_root), "source_relative_path": source_relative_path,
        "catalog_key": variant, "status": status, "eligible": eligible,
        "steam": steam_data,
    }


def discover_installed_saves(project_root: Path, games: list[dict], scan_mounts: bool = True) -> tuple[list[dict], dict]:
    """Find only Steam Cloud files and Windows Auto-Cloud roots tied to installed AppIDs."""
    import hashlib

    project_root = project_root.resolve()
    discovery = discover_steam_libraries(scan_mounts=scan_mounts)
    library_map = {row["library_path"]: row for row in discovery["libraries"]}
    official_root_paths = [Path(row["root_path"]) for row in discovery["roots"]]
    rows, issues = [], []

    def add_candidate(game: dict, source: Path, destination: Path, kind: str,
                      source_root: Path, evidence: dict) -> None:
        source = Path(os.path.abspath(source))
        try:
            relative = source.relative_to(source_root).as_posix()
        except ValueError:
            relative = source.as_posix()
        row = _candidate_save_row(project_root, game, source, destination, kind,
                                  source_root, relative, evidence)
        if not any(current["source_path"] == row["source_path"] for current in rows):
            rows.append(row)

    for game in games:
        if game.get("status") not in {"eligible_game", "already_centralized"} or not game.get("eligible"):
            continue
        appid = str(game["appid"])
        canonical_game = Path(game["destination_path"])
        if not canonical_game.is_dir() or canonical_game.is_symlink():
            issues.append({"appid": appid, "status": "game_not_centralized_yet",
                           "source_path": game.get("source_path", "")})
            continue
        library = library_map.get(game.get("library_path", ""), {})
        root_paths = {str(x) for x in library.get("registered_by", [])}
        if library.get("is_steam_root"):
            root_paths.add(str(library["library_path"]))
        root_paths.update(str(root) for root in official_root_paths
                          if str(root) in set(library.get("registered_by", [])))
        if not root_paths:
            issues.append({"appid": appid, "status": "steam_root_for_user_data_unavailable"})
            continue
        root_paths = sorted(root_paths, key=str.casefold)
        try:
            manifest_hash = __import__("hashlib").sha256(Path(game["manifest_path"]).read_bytes()).hexdigest()
        except OSError as exc:
            issues.append({"appid": appid, "status": "appmanifest_unreadable", "error": str(exc)})
            continue
        canonical_save_root = project_root / "saves" / "steam" / game.get("canonical_name", "")
        compat_root = Path(game["steamapps_path"]) / "compatdata" / appid
        profile_root = compat_root / "pfx" / "drive_c" / "users"

        # Steam Auto-Cloud places this marker in each configured root. Move only roots
        # inside the per-game Proton profile; install-tree roots are handled file by file below.
        marker_directories = []
        seen_profile_roots = set()
        if profile_root.is_dir() and not profile_root.is_symlink():
            try:
                profiles = [path for path in profile_root.iterdir() if path.is_dir() and not path.is_symlink()]
            except OSError:
                profiles = []
            for profile in profiles:
                for relative_root in ("Documents", "My Documents", "Saved Games", "AppData/Local",
                                      "AppData/LocalLow", "AppData/Roaming"):
                    candidate_root = profile / relative_root
                    try:
                        resolved_root = candidate_root.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    root_key = str(resolved_root)
                    if root_key in seen_profile_roots or not resolved_root.is_dir():
                        continue
                    seen_profile_roots.add(root_key)
                    for current, directories, filenames in os.walk(resolved_root, topdown=True, followlinks=False):
                        current_path = Path(current)
                        directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
                        if any(filename.casefold() == "steam_autocloud.vdf" for filename in filenames):
                            marker = current_path / next(filename for filename in filenames
                                                          if filename.casefold() == "steam_autocloud.vdf")
                            if _has_payload_files(current_path, exclude_autocloud_marker=True):
                                marker_directories.append((current_path, marker, resolved_root))
        marker_directories.sort(key=lambda value: str(value[0]).casefold())
        selected_marker_directories = []
        for marker_entry in marker_directories:
            marker_root = marker_entry[0]
            if any(os.path.commonpath((str(marker_root), str(parent[0]))) == str(parent[0])
                   for parent in selected_marker_directories):
                issues.append({"appid": appid, "status": "nested_autocloud_marker_covered_by_parent",
                               "source_path": str(marker_root)})
                continue
            selected_marker_directories.append(marker_entry)
        marker_directories = selected_marker_directories
        covered_marker_roots = [root for root, _marker, _profile in marker_directories]
        for marker_root, marker, _profile in marker_directories:
            try:
                relative = marker_root.relative_to(compat_root).as_posix()
            except ValueError:
                issues.append({"appid": appid, "status": "autocloud_root_outside_compatdata",
                               "source_path": str(marker_root)})
                continue
            destination = canonical_save_root / "proton" / appid / Path(relative)
            add_candidate(game, marker_root, destination, "proton_autocloud_directory", compat_root,
                          {"manifest_sha256": manifest_hash, "autocloud_marker": str(marker),
                           "steam_root_path": root_paths[0]})

        # Steam Cloud's local API cache is a per-account remote/ directory. Leave its
        # remotecache.vdf at Steam's original path, while redirecting the save payload.
        cloud_manifests = []
        covered_remote_roots = set()
        matched_cloud_sources = set()
        for root_path in root_paths:
            steam_root = Path(root_path)
            userdata = steam_root / "userdata"
            if not userdata.is_dir() or userdata.is_symlink():
                continue
            try:
                accounts = [path for path in userdata.iterdir() if path.is_dir() and not path.is_symlink()]
            except OSError:
                continue
            for account in accounts:
                user_app = account / appid
                cache_file = user_app / "remotecache.vdf"
                remote = user_app / "remote"
                cache_entries = _cloud_manifest_entries(cache_file, appid) if cache_file.is_file() else []
                cloud_manifests.extend((steam_root, account, cache_file, entry) for entry in cache_entries)
                root_tag = hashlib.sha256(str(steam_root).encode("utf-8", "surrogateescape")).hexdigest()[:8]
                target = canonical_save_root / "steam-cloud" / root_tag / account.name / appid / "remote"
                if remote.is_symlink():
                    add_candidate(game, remote, target, "steam_cloud_remote_directory", steam_root,
                                  {"manifest_sha256": manifest_hash, "steam_root_path": str(steam_root),
                                   "cloud_account_id": account.name,
                                   "cloud_manifest_path": str(cache_file)})
                elif _has_payload_files(remote):
                    root_zero = {}
                    for entry in cache_entries:
                        if entry["root_id"] == "0":
                            root_zero.setdefault(entry["relative_path"], []).append(entry)
                    payloads, all_cloud_matched = [], True
                    for current, directories, filenames in os.walk(remote, topdown=True, followlinks=False):
                        current_path = Path(current)
                        if any((current_path / name).is_symlink() for name in directories):
                            all_cloud_matched = False
                        directories[:] = [name for name in directories
                                          if not (current_path / name).is_symlink()]
                        for filename in filenames:
                            candidate = current_path / filename
                            if candidate.is_symlink() or not candidate.is_file():
                                all_cloud_matched = False
                                continue
                            relative = candidate.relative_to(remote).as_posix()
                            matches = [entry for entry in root_zero.get(relative, [])
                                       if entry["size_bytes"] == candidate.stat().st_size
                                       and entry["sha1"] == _sha1_file(candidate)]
                            if len(matches) != 1:
                                all_cloud_matched = False
                            else:
                                payloads.append(relative)
                    if all_cloud_matched and payloads:
                        covered_remote_roots.add(str(remote))
                        add_candidate(game, remote, target, "steam_cloud_remote_directory", steam_root,
                                      {"manifest_sha256": manifest_hash, "steam_root_path": str(steam_root),
                                       "cloud_account_id": account.name,
                                       "cloud_manifest_path": str(cache_file)})
                    else:
                        issues.append({"appid": appid, "status": "remote_directory_has_unmatched_files",
                                       "source_path": str(remote), "matched_cloud_files": payloads,
                                       "manifest": str(cache_file)})

        # Numeric root IDs only select candidate locations. A file is eligible only
        # when its local path, size, and SHA-1 match this AppID's remotecache entry.
        for steam_root, account, cache_file, entry in cloud_manifests:
            relative = _safe_cloud_relative(entry["relative_path"])
            if not relative:
                continue
            root_id = entry["root_id"]
            source_options = []
            source_kind = ""
            source_base = None
            if root_id == "0":
                source_base = account / appid / "remote"
                source_options = [source_base.joinpath(*relative.parts)]
                source_kind = "steam_cloud_remote_file"
            elif root_id == "1":
                source_base = canonical_game
                source_options = [source_base.joinpath(*relative.parts)]
                source_kind = "steam_cloud_app_install_file"
            elif root_id in AUTO_CLOUD_PROFILE_ROOTS and profile_root.is_dir():
                profile_relative = AUTO_CLOUD_PROFILE_ROOTS[root_id]
                for profile in profile_root.iterdir():
                    if not profile.is_dir() or profile.is_symlink():
                        continue
                    candidate_base = profile / profile_relative
                    try:
                        candidate_base = candidate_base.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if candidate_base.is_dir():
                        source_options.append(candidate_base.joinpath(*relative.parts))
                        source_base = compat_root
                source_kind = "steam_cloud_proton_file"
            elif root_id == "7":
                cloud_roots = (Path.home() / ".SteamCloud", Path.home() / ".steam/steam/SteamCloud",
                               Path.home() / ".local/share/Steam/SteamCloud")
                for cloud_root in cloud_roots:
                    try:
                        usernames = [path for path in cloud_root.iterdir() if path.is_dir() and not path.is_symlink()]
                    except OSError:
                        continue
                    for username in usernames:
                        for game_folder in username.iterdir():
                            if game_folder.is_dir() and not game_folder.is_symlink():
                                source_options.append(game_folder.joinpath(*relative.parts))
                                source_base = cloud_root
                source_kind = "steam_cloud_documents_file"
            else:
                issues.append({"appid": appid, "status": "cloud_root_id_unsupported",
                               "root_id": root_id, "relative_path": entry["relative_path"],
                               "manifest": str(cache_file)})
                continue

            matching = []
            already_linked = []
            for candidate in source_options:
                try:
                    resolved = candidate.resolve(strict=True)
                    canonical_target_root = canonical_save_root.resolve(strict=False)
                    if os.path.commonpath((str(resolved), str(canonical_target_root))) == str(canonical_target_root):
                        already_linked.append(candidate)
                        continue
                except (OSError, RuntimeError, ValueError):
                    pass
                if candidate.is_symlink():
                    continue
                if not candidate.is_file() or candidate.stat().st_size != entry["size_bytes"]:
                    continue
                try:
                    if _sha1_file(candidate) == entry["sha1"]:
                        resolved = candidate.resolve(strict=True)
                        if not any(existing.resolve(strict=True) == resolved for existing in matching):
                            matching.append(candidate)
                except (OSError, RuntimeError):
                    continue
            if not matching and already_linked:
                continue
            if len(matching) != 1:
                issues.append({"appid": appid,
                               "status": "cloud_file_source_missing_or_ambiguous",
                               "root_id": root_id, "relative_path": entry["relative_path"],
                               "matched_source_count": len(matching), "manifest": str(cache_file)})
                continue
            source = matching[0]
            try:
                matched_cloud_sources.add(str(source.resolve(strict=True)))
            except (OSError, RuntimeError):
                pass
            if any(os.path.commonpath((str(source), str(marker_root))) == str(marker_root)
                   for marker_root in covered_marker_roots):
                continue
            if source_kind == "steam_cloud_app_install_file":
                source_root = canonical_game
                destination = canonical_save_root / "install" / Path(*relative.parts)
            elif source_kind == "steam_cloud_proton_file":
                source_root = compat_root
                destination = canonical_save_root / "proton" / appid / source.relative_to(compat_root)
            elif source_kind == "steam_cloud_remote_file":
                remote_root = account / appid / "remote"
                if str(remote_root) in covered_remote_roots:
                    continue
                source_root = remote_root
                root_tag = hashlib.sha256(str(steam_root).encode("utf-8", "surrogateescape")).hexdigest()[:8]
                destination = (canonical_save_root / "steam-cloud" / root_tag / account.name / appid
                               / "remote" / Path(*relative.parts))
            else:
                source_root = source_base or Path.home()
                destination = canonical_save_root / "linux-cloud" / source.relative_to(source_root)
            add_candidate(game, source, destination, source_kind, source_root,
                          {"manifest_sha256": manifest_hash, "steam_root_path": str(steam_root),
                           "cloud_manifest_path": str(cache_file), "cloud_account_id": account.name,
                           "cloud_root_id": root_id, "cloud_relative_path": entry["relative_path"],
                           "cloud_size_bytes": entry["size_bytes"], "cloud_sha1": entry["sha1"]})

        # Inventory likely game-user data that has neither a marker nor an exact Cloud
        # manifest match. Preserve it as an explicit review item; filenames are not proof.
        if profile_root.is_dir() and not profile_root.is_symlink():
            try:
                profiles = [path for path in profile_root.iterdir()
                            if path.is_dir() and not path.is_symlink()]
            except OSError:
                profiles = []
            seen_unclassified = set()
            for profile in profiles:
                for relative_root in ("AppData/Roaming", "Documents", "Saved Games"):
                    local_root = profile / relative_root
                    if not local_root.is_dir() or local_root.is_symlink():
                        continue
                    for current, directories, filenames in os.walk(local_root, topdown=True, followlinks=False):
                        current_path = Path(current)
                        directories[:] = [name for name in directories
                                          if not (current_path / name).is_symlink()]
                        for filename in filenames:
                            candidate = current_path / filename
                            if candidate.is_symlink() or filename.casefold() == "steam_autocloud.vdf":
                                continue
                            if not candidate.is_file():
                                continue
                            try:
                                resolved = candidate.resolve(strict=True)
                                save_root = canonical_save_root.resolve(strict=False)
                                if os.path.commonpath((str(resolved), str(save_root))) == str(save_root):
                                    continue
                                if str(resolved) in matched_cloud_sources:
                                    continue
                                if any(os.path.commonpath((str(candidate), str(marker_root))) == str(marker_root)
                                       for marker_root in covered_marker_roots):
                                    continue
                                source_key = str(resolved)
                                if source_key in seen_unclassified:
                                    continue
                                seen_unclassified.add(source_key)
                                issues.append({
                                    "appid": appid, "status": "unclassified_local_profile_state",
                                    "source_path": str(candidate), "size_bytes": candidate.stat().st_size,
                                    "sha256": _sha256_file(candidate),
                                    "reason": "arquivo de perfil sem steam_autocloud.vdf e sem correspondência exata no remotecache.vdf; preservado para classificação",
                                })
                            except (OSError, RuntimeError, ValueError):
                                continue

    by_destination = {}
    for row in rows:
        by_destination.setdefault(row["destination_path"].casefold(), []).append(row)
    for matching in by_destination.values():
        if len(matching) > 1:
            for row in matching:
                if row["status"] == "eligible_save":
                    row["status"], row["eligible"] = "save_destination_collision", False
    rows.sort(key=lambda row: (row["official_name"].casefold(), row["appid"], row["source_path"].casefold()))
    evidence = {
        "steam_roots": discovery["roots"], "steam_libraries": discovery["libraries"],
        "mounted_block_devices": discovery["mounts"], "discovery_errors": discovery["errors"],
        "save_detection": "Steam userdata remote directories, AppID-specific Proton Auto-Cloud roots, and remotecache entries with exact local size/SHA-1 matches",
        "cloud_root_mapping": {"0": "local remote/ candidate; corroborated against the matching remotecache entry",
                               "1": "local install-tree candidate; corroborated against the matching remotecache entry",
                               **{key: "profile-root candidate; corroborated against the matching remotecache entry"
                                  for key in AUTO_CLOUD_PROFILE_ROOTS},
                               "7": "local SteamCloud candidate; corroborated against the matching remotecache entry"},
        "unsupported_or_unmatched": issues,
        "policy": "Do not move a whole compatdata prefix; do not infer local non-Cloud save paths from names.",
    }
    return rows, evidence


def app_metadata_cache_path(project_root: Path) -> Path:
    return project_root / "outputs" / "roms-consolidacao-2026-10-24" / "analysis" / "steam-app-types.json"


def validate_steam_action(action: dict, project_root: Path) -> dict:
    steam = action.get("steam")
    if not isinstance(steam, dict):
        raise ValueError("Ação Steam sem evidência steam estruturada")
    appid = str(steam.get("appid", ""))
    if not re.fullmatch(r"\d+", appid):
        raise ValueError("AppID Steam inválido")
    source = Path(action["source"]).absolute()
    destination = Path(action["destination"]).absolute()
    canonical_root = (project_root / "roms" / "steam").absolute()
    try:
        if os.path.commonpath((str(destination), str(canonical_root))) != str(canonical_root):
            raise ValueError("Destino Steam está fora de roms/steam")
    except ValueError as exc:
        raise ValueError("Destino Steam está fora de roms/steam") from exc
    if destination == canonical_root or destination.name in {"", ".", ".."}:
        raise ValueError("Destino Steam deve ser a pasta de um jogo")
    if action.get("system_id") != "steam" or action.get("classification") != "game":
        raise ValueError("A ação Steam precisa permanecer classificada como jogo")
    if steam.get("app_type") != "game" or steam.get("metadata_source") != "Steam Store appdetails":
        raise ValueError("Tipo de aplicação Steam não foi confirmado pela loja oficial")
    if {str(x).casefold() for x in steam.get("genres", [])} & SOFTWARE_GENRES:
        raise ValueError("Aplicativo Steam com gênero de software não pode ser movido como jogo")
    if source.name.casefold() != str(steam.get("install_dir", "")).casefold():
        raise ValueError("Origem não corresponde ao installdir do appmanifest")
    library = Path(str(steam.get("library_path", ""))).absolute()
    steamapps = Path(str(steam.get("steamapps_path", ""))).absolute()
    manifest = Path(str(steam.get("manifest_path", ""))).absolute()
    if steamapps.parent != library or manifest.parent != steamapps:
        raise ValueError("Caminhos do manifesto e da Steam library não correspondem")
    if source.parent.parent != steamapps:
        raise ValueError("A pasta de origem não está em steamapps/common/<installdir>")
    if not source.exists() or not source.is_dir() or source.is_symlink():
        raise ValueError(f"A origem do jogo não é uma pasta regular: {source}")
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError(f"Appmanifest Steam ausente ou simbólico: {manifest}")
    current_libraries = discover_steam_libraries(scan_mounts=False)["libraries"]
    current_library = next((row for row in current_libraries if row["library_path"] == str(library)), None)
    if not current_library or not current_library.get("trusted"):
        raise ValueError("Steam library não está associada a uma raiz oficial detectada")
    if not _registered_app_id(appid, current_library):
        raise ValueError("AppID não está listado como instalação oficial nessa Steam library")
    try:
        manifest_data = _casefold_get(read_vdf(manifest), "AppState", {})
        if str(_casefold_get(manifest_data, "appid", "")) != appid:
            raise ValueError("AppID do appmanifest divergiu do plano")
        if str(_casefold_get(manifest_data, "installdir", "")) != str(steam.get("install_dir", "")):
            raise ValueError("installdir do appmanifest mudou desde o plano")
        flags = int(str(_casefold_get(manifest_data, "StateFlags", "0")))
        if not flags & 4:
            raise ValueError("Steam não marca o aplicativo como instalado")
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"Appmanifest Steam inválido ou alterado: {exc}") from exc
    metadata = _read_metadata_cache(app_metadata_cache_path(project_root)).get(appid, {})
    if metadata.get("type") != "game" or metadata.get("source") != "Steam Store appdetails":
        raise ValueError("Cache local não confirma app type=game na Steam Store")
    if _safe_game_directory_name(str(metadata.get("name", ""))) != destination.name:
        raise ValueError("Nome do destino diverge do título oficial em cache")
    expected_manifest_hash = str(steam.get("manifest_sha256", ""))
    if not expected_manifest_hash:
        raise ValueError("Plano sem SHA-256 do appmanifest")
    import hashlib
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if digest != expected_manifest_hash:
        raise ValueError("Appmanifest mudou depois do planejamento")
    return {"appid": appid, "library": str(library), "manifest": str(manifest),
            "source": str(source), "destination": str(destination), "manifest_sha256": digest}


def _path_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path.absolute()), str(root.absolute()))) == str(root.absolute())
    except ValueError:
        return False


def _cloud_entry_matches(steam: dict, expected_root: str, expected_relative: str,
                         source: Path, verify_payload: bool = True) -> dict:
    manifest_path = Path(str(steam.get("cloud_manifest_path", ""))).absolute()
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError(f"remotecache.vdf ausente ou simbólico: {manifest_path}")
    user_app = manifest_path.parent
    account_id = str(steam.get("cloud_account_id", ""))
    appid = str(steam.get("appid", ""))
    if user_app.name != appid or user_app.parent.name != account_id or user_app.parent.parent.name != "userdata":
        raise ValueError("remotecache.vdf não está na userdata/<conta>/<AppID> esperada")
    matches = [entry for entry in _cloud_manifest_entries(manifest_path, appid)
               if entry["root_id"] == expected_root and entry["relative_path"] == expected_relative]
    if len(matches) != 1:
        raise ValueError(f"remotecache.vdf não tem uma entrada única para root={expected_root}, path={expected_relative}")
    entry = matches[0]
    if (verify_payload and
            (source.stat().st_size != entry["size_bytes"] or _sha1_file(source) != entry["sha1"])):
        raise ValueError(f"Tamanho/SHA-1 local não coincide com remotecache.vdf: {source}")
    return entry


def validate_steam_save_action(action: dict, project_root: Path,
                               allow_centralized_source: bool = False,
                               verify_payload: bool = True) -> dict:
    steam = action.get("steam")
    if not isinstance(steam, dict):
        raise ValueError("Ação de save Steam sem evidência estruturada")
    appid = str(steam.get("appid", ""))
    if not re.fullmatch(r"\d+", appid):
        raise ValueError("AppID Steam inválido")
    source = Path(action["source"]).absolute()
    destination = Path(action["destination"]).absolute()
    project_root = project_root.resolve()
    save_root = project_root / "saves" / "steam"
    official_name = str(steam.get("official_name", ""))
    canonical_name = _safe_game_directory_name(official_name)
    canonical_save_root = save_root / canonical_name
    if action.get("action") != "steam_save_move_link" or action.get("system_id") != "steam":
        raise ValueError("Ação de save precisa ser steam_save_move_link do sistema Steam")
    if action.get("classification") != "support":
        raise ValueError("Save Steam deve permanecer classificado como dado de suporte")
    if steam.get("app_type") != "game":
        raise ValueError("AppID Steam não está confirmado como jogo")
    if not _path_within(destination, canonical_save_root) or destination == canonical_save_root:
        raise ValueError("Destino deve ficar dentro de saves/steam/<título oficial>")
    if source.is_symlink():
        if not allow_centralized_source:
            raise ValueError(f"Origem de save já é symlink: {source}")
        try:
            if source.resolve(strict=True) != destination.resolve(strict=True):
                raise ValueError(f"Symlink de save não aponta ao destino canônico: {source}")
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Symlink de save não resolve: {source}") from exc
    elif not os.path.lexists(source) or not (source.is_file() or source.is_dir()):
        raise ValueError(f"Origem de save ausente ou inválida: {source}")
    for component in (source, *source.parents):
        if component == source and allow_centralized_source and source.is_symlink():
            continue
        if os.path.lexists(component) and component.is_symlink():
            raise ValueError(f"Caminho de origem de save contém symlink inesperado: {component}")

    library = Path(str(steam.get("library_path", ""))).absolute()
    steamapps = Path(str(steam.get("steamapps_path", ""))).absolute()
    manifest = Path(str(steam.get("manifest_path", ""))).absolute()
    if steamapps.parent != library or manifest.parent != steamapps:
        raise ValueError("Appmanifest e biblioteca Steam não correspondem")
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError(f"Appmanifest Steam ausente ou simbólico: {manifest}")
    libraries = discover_steam_libraries(scan_mounts=False)["libraries"]
    current_library = next((row for row in libraries if row["library_path"] == str(library)), None)
    if not current_library or not current_library.get("trusted") or not _registered_app_id(appid, current_library):
        raise ValueError("AppID não está registrado em uma Steam library oficial ativa")
    manifest_data = _casefold_get(read_vdf(manifest), "AppState", {})
    if (str(_casefold_get(manifest_data, "appid", "")) != appid
            or str(_casefold_get(manifest_data, "installdir", "")) != str(steam.get("install_dir", ""))
            or not int(str(_casefold_get(manifest_data, "StateFlags", "0"))) & 4):
        raise ValueError("Appmanifest não confirma o jogo instalado usado no plano")
    import hashlib
    current_manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if current_manifest_hash != str(steam.get("manifest_sha256", "")):
        raise ValueError("Appmanifest mudou após a descoberta do save")
    metadata = _read_metadata_cache(app_metadata_cache_path(project_root)).get(appid, {})
    if (metadata.get("type") != "game" or metadata.get("source") != "Steam Store appdetails"
            or _safe_game_directory_name(str(metadata.get("name", ""))) != canonical_name):
        raise ValueError("Cache da Steam Store não confirma título e type=game")

    kind = str(steam.get("source_kind", ""))
    compat_root = steamapps / "compatdata" / appid
    canonical_game = Path(str(steam.get("canonical_game_path", ""))).absolute()
    if not canonical_game.is_dir() or canonical_game.is_symlink():
        raise ValueError(f"Árvore canônica do jogo não está disponível: {canonical_game}")
    if kind == "proton_autocloud_directory":
        profile_root = compat_root / "pfx" / "drive_c" / "users"
        if not _path_within(source, profile_root) or source == profile_root:
            raise ValueError("Raiz Auto-Cloud está fora do perfil Proton do AppID")
        marker = source / "steam_autocloud.vdf"
        if not marker.is_file() or marker.is_symlink():
            raise ValueError("Marcador steam_autocloud.vdf não está na raiz de save")
    elif kind == "steam_cloud_remote_directory":
        manifest_path = Path(str(steam.get("cloud_manifest_path", ""))).absolute()
        expected_remote = manifest_path.parent / "remote"
        if source != expected_remote or source.parent.name != appid:
            raise ValueError("Diretório remote não corresponde a userdata/<conta>/<AppID>/remote")
        if not (source.is_symlink() and allow_centralized_source):
            payloads = []
            for current, directories, filenames in os.walk(source, topdown=True, followlinks=False):
                current_path = Path(current)
                if any((current_path / name).is_symlink() for name in directories):
                    raise ValueError(f"remote contém diretório simbólico: {current_path}")
                directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
                for filename in filenames:
                    candidate = current_path / filename
                    if candidate.is_symlink() or not candidate.is_file():
                        raise ValueError(f"remote contém link ou tipo especial: {candidate}")
                    relative = candidate.relative_to(source).as_posix()
                    _cloud_entry_matches(steam, "0", relative, candidate,
                                         verify_payload=verify_payload)
                    payloads.append(relative)
            if not payloads:
                raise ValueError("remote não contém payloads locais confirmados pelo manifesto")
    elif kind in {"steam_cloud_app_install_file", "steam_cloud_proton_file",
                  "steam_cloud_remote_file", "steam_cloud_documents_file"}:
        cloud_path = _safe_cloud_relative(str(steam.get("cloud_relative_path", "")))
        if not cloud_path or not source.is_file() or (source.is_symlink() and not allow_centralized_source):
            raise ValueError("Arquivo Cloud precisa ser regular e ter caminho relativo seguro")
        root_id = str(steam.get("cloud_root_id", ""))
        if kind == "steam_cloud_app_install_file":
            if root_id != "1" or not _path_within(source, canonical_game):
                raise ValueError("Arquivo Steam Cloud App Install fora da árvore canônica")
            expected_relative = source.relative_to(canonical_game).as_posix()
            if expected_relative != cloud_path.as_posix():
                raise ValueError("Caminho relativo do arquivo diverge do remotecache.vdf")
        elif kind == "steam_cloud_proton_file":
            if root_id not in AUTO_CLOUD_PROFILE_ROOTS or not _path_within(source, compat_root):
                raise ValueError("Arquivo Steam Cloud Proton fora do compatdata esperado")
            profile_root = compat_root / "pfx" / "drive_c" / "users"
            expected_sources = set()
            if profile_root.is_dir() and not profile_root.is_symlink():
                for profile in profile_root.iterdir():
                    if profile.is_dir() and not profile.is_symlink():
                        root = profile / AUTO_CLOUD_PROFILE_ROOTS[root_id]
                        try:
                            root = root.resolve(strict=False)
                        except (OSError, RuntimeError):
                            continue
                        expected_sources.add(root.joinpath(*cloud_path.parts))
            if source not in expected_sources:
                raise ValueError("Arquivo Proton não corresponde à raiz de perfil indicada pelo remotecache.vdf")
        elif kind == "steam_cloud_remote_file":
            remote_root = Path(str(steam.get("cloud_manifest_path", ""))).parent / "remote"
            if (root_id != "0" or not _path_within(source, remote_root)
                    or source.relative_to(remote_root).as_posix() != cloud_path.as_posix()):
                raise ValueError("Arquivo Steam Cloud remote fora do remote/ esperado")
        else:
            source_root = Path(str(steam.get("source_root", ""))).absolute()
            if root_id != "7" or not _path_within(source, source_root):
                raise ValueError("Arquivo SteamCloudDocuments sem root 7 local comprovável")
            relative = source.relative_to(source_root)
            if len(relative.parts) <= len(cloud_path.parts) or relative.parts[-len(cloud_path.parts):] != cloud_path.parts:
                raise ValueError("Arquivo SteamCloudDocuments não corresponde ao caminho relativo do manifesto")
        _cloud_entry_matches(steam, root_id, cloud_path.as_posix(), source,
                             verify_payload=verify_payload and not
                             (source.is_symlink() and allow_centralized_source))
    else:
        raise ValueError(f"Origem de save Steam não suportada: {kind}")

    return {"appid": appid, "source": str(source), "destination": str(destination),
            "manifest_sha256": current_manifest_hash, "source_kind": kind,
            "canonical_save_root": str(canonical_save_root)}


def steam_processes_running() -> list[dict]:
    """Detect Steam itself and games launched from its libraries under /proc."""
    found = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip().casefold()
            exe = os.path.basename(os.readlink(entry / "exe")).casefold()
            raw = (entry / "cmdline").read_bytes().split(b"\0")
            args = [part.decode("utf-8", "replace") for part in raw if part]
        except (OSError, PermissionError):
            continue
        joined = " ".join(args).casefold()
        if "tools/roms/romtool.py steam" in joined:
            continue
        if comm in {"steam", "steamwebhelper", "steamservice", "steam.exe"} or exe in {
            "steam", "steam.sh", "steamwebhelper", "steamservice", "steam.exe",
        }:
            found.append({"pid": int(entry.name), "comm": comm, "exe": exe})
            continue
        try:
            executable_path = Path(os.readlink(entry / "exe")).resolve(strict=False)
        except OSError:
            continue
        path_text = str(executable_path).casefold()
        if "/steamapps/common/" in path_text or "/roms/steam/" in path_text:
            found.append({"pid": int(entry.name), "comm": comm, "exe": exe})
    return found


def probe_symlink_support(parent: Path) -> None:
    """Verify the source filesystem supports a real symlink before moving game data."""
    probe = parent / f".romtool-symlink-probe-{os.getpid()}-{time.time_ns()}"
    target = parent
    created = False
    try:
        os.symlink(str(target), probe, target_is_directory=True)
        created = True
        if not probe.is_symlink() or os.readlink(probe) != str(target):
            raise OSError("o sistema de arquivos não preservou um link simbólico real")
    except (OSError, NotImplementedError) as exc:
        raise OSError(f"Steam library não permite symlink funcional em {parent}: {exc}") from exc
    finally:
        if created:
            probe.unlink(missing_ok=True)
