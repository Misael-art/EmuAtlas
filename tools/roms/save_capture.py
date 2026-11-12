"""Session-scoped, auditable save/state screenshot capture for romtool.

The watcher observes user-selected files only. A save/card write is a trigger,
not proof of the exact in-game button time. Native state thumbnails are copied
as-is; all other captures are explicitly labeled as active-window snapshots.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid
import zlib
from pathlib import Path


WATCH_KINDS = {"save", "state", "memory-card", "state-thumbnail"}
SYSTEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def add_parser(commands) -> None:
    capture = commands.add_parser("capture", help="audita e captura telas associadas a saves/states")
    subcommands = capture.add_subparsers(dest="capture_command", required=True)
    coverage = subcommands.add_parser("coverage", help="mapeia cobertura para todo o catálogo de plataformas")
    coverage.add_argument("--catalog", default="frontend/RetroFE/Config/platform-catalog.json")
    coverage.add_argument("--profile", default="frontend/RetroFE/Config/profiles/host-linux.json")
    watch = subcommands.add_parser("watch", help="observa uma sessão e salva capturas vinculadas ao jogo")
    watch.add_argument("--system-id", required=True)
    watch.add_argument("--rom", required=True, help="arquivo ou diretório do jogo dentro de roms/<system-id>/")
    watch.add_argument("--watch-root", action="append", required=True, metavar="TIPO=CAMINHO",
                       help="repita para save, state, memory-card ou state-thumbnail")
    watch.add_argument("--poll-seconds", type=float, default=3.0)
    watch.add_argument("--stable-seconds", type=float, default=1.5)
    watch.add_argument("--max-files", type=int, default=200000)
    watch.add_argument("--assume-emulator-foreground", action="store_true",
                       help="autoriza Spectacle a capturar a janela ativa; não comprova qual jogo está em foco")
    watch.add_argument("--apply", action="store_true", help="autoriza gravar PNGs em media/<system-id>/screenshot/")


def _utc_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if os.path.lexists(component) and component.is_symlink():
            raise ValueError(f"Caminho com componente symlink recusado: {component}")


def _parse_watch_roots(values: list[str], project_root: Path) -> list[dict]:
    roots = []
    seen = set()
    for value in values:
        kind, separator, raw_path = value.partition("=")
        if not separator or kind not in WATCH_KINDS or not raw_path:
            raise ValueError(f"Raiz inválida {value!r}; use TIPO=CAMINHO com um tipo de {sorted(WATCH_KINDS)}")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = project_root / path
        path = Path(os.path.abspath(path))
        _reject_symlink_components(path)
        if not (path.is_file() or path.is_dir()):
            raise ValueError(f"Raiz de observação precisa existir como arquivo regular ou diretório: {path}")
        if path.is_file() and not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f"Raiz de observação não é arquivo regular: {path}")
        if path == project_root or path in project_root.parents:
            raise ValueError(f"Não observe a raiz inteira do projeto nem um diretório que a contenha: {path}")
        media_root = project_root / "media"
        if path == media_root or path in media_root.parents or media_root in path.parents:
            raise ValueError(f"Não observe a própria biblioteca de mídia/capturas: {path}")
        key = (kind, str(path))
        if key in seen:
            raise ValueError(f"Raiz repetida: {value}")
        seen.add(key)
        roots.append({"kind": kind, "path": path})
    return roots


def _file_signature(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _scan_roots(roots: list[dict], max_files: int) -> tuple[dict[str, dict], dict]:
    found: dict[str, dict] = {}
    counters = {"regular_files": 0, "symlinks_skipped": 0, "special_files_skipped": 0}

    def add_file(path: Path, kind: str) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            counters["symlinks_skipped"] += 1
            return
        if not stat.S_ISREG(info.st_mode):
            counters["special_files_skipped"] += 1
            return
        if path.name.startswith(".romtool-capture-"):
            return
        counters["regular_files"] += 1
        if counters["regular_files"] > max_files:
            raise ValueError(f"Raízes excedem o limite --max-files={max_files}")
        key = str(path)
        item = found.setdefault(key, {"path": path, "kinds": set(), "signature": _file_signature(path)})
        item["kinds"].add(kind)

    for root in roots:
        path, kind = root["path"], root["kind"]
        if path.is_file():
            add_file(path, kind)
            continue

        def onerror(error):
            raise error

        for current, directories, filenames in os.walk(path, topdown=True, followlinks=False, onerror=onerror):
            current_path = Path(current)
            directories[:] = sorted(name for name in directories
                                    if not (current_path / name).is_symlink())
            for name in sorted(filenames):
                add_file(current_path / name, kind)
    return found, counters


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _validate_png(path: Path, max_bytes: int = 536870912) -> tuple[int, int]:
    """Validate the complete PNG chunk stream, CRCs, zlib stream, and scanlines."""
    if not path.is_file() or path.stat().st_size < 57 or path.stat().st_size > max_bytes:
        raise ValueError(f"PNG ausente, vazio ou acima do limite de {max_bytes} bytes: {path}")
    decompressor = zlib.decompressobj()
    compressed_bytes = 0
    raw_bytes = 0
    ihdr = None
    saw_idat = False
    saw_iend = False
    idat_closed = False
    offset = 8
    with path.open("rb") as stream:
        if stream.read(8) != PNG_SIGNATURE:
            raise ValueError(f"Assinatura PNG inválida: {path}")
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError(f"PNG truncado antes de IEND: {path}")
            length = int.from_bytes(header[:4], "big")
            chunk_type = header[4:]
            if length > max_bytes or offset + 12 + length > path.stat().st_size:
                raise ValueError(f"Chunk PNG fora dos limites: {path}")
            data = stream.read(length)
            crc_bytes = stream.read(4)
            if len(data) != length or len(crc_bytes) != 4:
                raise ValueError(f"Chunk PNG incompleto: {path}")
            expected_crc = int.from_bytes(crc_bytes, "big")
            actual_crc = zlib.crc32(chunk_type + data) & 0xffffffff
            if expected_crc != actual_crc:
                raise ValueError(f"CRC inválido no chunk {chunk_type!r}: {path}")
            offset += 12 + length
            if ihdr is None and chunk_type != b"IHDR":
                raise ValueError(f"IHDR não é o primeiro chunk: {path}")
            if chunk_type == b"IHDR":
                if ihdr is not None or length != 13:
                    raise ValueError(f"IHDR duplicado ou inválido: {path}")
                ihdr = data
                width = int.from_bytes(data[:4], "big")
                height = int.from_bytes(data[4:8], "big")
                bit_depth, color_type, compression, filtering, interlace = data[8:13]
                channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
                valid_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
                                4: {8, 16}, 6: {8, 16}}
                if (not width or not height or width > 32768 or height > 32768
                        or width * height > 268435456 or channels is None
                        or bit_depth not in valid_depths[color_type]
                        or compression != 0 or filtering != 0 or interlace not in {0, 1}):
                    raise ValueError(f"Cabeçalho PNG inválido ou excessivo: {path}")
            elif chunk_type == b"IDAT":
                if idat_closed:
                    raise ValueError(f"Chunks IDAT não contíguos: {path}")
                saw_idat = True
                compressed_bytes += length
                raw_bytes += len(decompressor.decompress(data, max_bytes - raw_bytes + 1))
                if raw_bytes > max_bytes:
                    raise ValueError(f"Dados PNG descomprimidos acima do limite: {path}")
            else:
                if saw_idat:
                    idat_closed = True
                if chunk_type == b"IEND":
                    if length != 0 or saw_iend:
                        raise ValueError(f"IEND inválido: {path}")
                    saw_iend = True
                    if stream.read(1):
                        raise ValueError(f"Dados extras após IEND: {path}")
                    break
            if chunk_type == b"IEND":
                break
    if ihdr is None or not saw_idat or not saw_iend or not compressed_bytes:
        raise ValueError(f"Estrutura PNG incompleta: {path}")
    try:
        raw_bytes += len(decompressor.flush(max_bytes - raw_bytes + 1))
    except zlib.error as exc:
        raise ValueError(f"Fluxo IDAT inválido: {path}: {exc}") from exc
    if not decompressor.eof or decompressor.unused_data or raw_bytes > max_bytes:
        raise ValueError(f"Fluxo IDAT incompleto ou inválido: {path}")
    width = int.from_bytes(ihdr[:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    depth, color_type, _compression, _filtering, interlace = ihdr[8:13]
    if interlace == 0:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        row_bytes = (width * channels * depth + 7) // 8
        if raw_bytes != height * (row_bytes + 1):
            raise ValueError(f"Dimensões e payload PNG não correspondem: {path}")
        with path.open("rb") as stream:
            data = stream.read()
        # The decompressed scanline filter byte is checked by a second bounded pass.
        decoder = zlib.decompressobj()
        scanlines = bytearray()
        position = 8
        while position < len(data):
            length = int.from_bytes(data[position:position + 4], "big")
            chunk_type = data[position + 4:position + 8]
            chunk = data[position + 8:position + 8 + length]
            position += length + 12
            if chunk_type == b"IDAT":
                scanlines.extend(decoder.decompress(chunk, max_bytes - len(scanlines) + 1))
        scanlines.extend(decoder.flush(max_bytes - len(scanlines) + 1))
        if len(scanlines) != raw_bytes or any(scanlines[row * (row_bytes + 1)] > 4 for row in range(height)):
            raise ValueError(f"Filtros ou scanlines PNG inválidos: {path}")
    return width, height


def _canonical_rom(args, project_root: Path, catalog_path: Path) -> tuple[Path, str, str]:
    if not SYSTEM_ID_RE.fullmatch(args.system_id):
        raise ValueError(f"system_id inválido: {args.system_id}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    matches = [row for row in catalog.get("systems", []) if row.get("id") == args.system_id]
    if len(matches) != 1:
        raise ValueError(f"system_id precisa ocorrer exatamente uma vez no catálogo: {args.system_id}")
    rom_root = project_root / "roms" / args.system_id
    _reject_symlink_components(rom_root)
    if not rom_root.is_dir():
        raise ValueError(f"Diretório canônico do sistema não existe: {rom_root}")
    rom = Path(args.rom).expanduser()
    if not rom.is_absolute():
        rom = project_root / rom
    rom = Path(os.path.abspath(rom))
    _reject_symlink_components(rom)
    try:
        rom.relative_to(rom_root)
    except ValueError as exc:
        raise ValueError(f"O jogo precisa estar dentro de roms/{args.system_id}/: {rom}") from exc
    if not (rom.is_file() or rom.is_dir()):
        raise ValueError(f"ROM precisa ser arquivo regular ou diretório de jogo: {rom}")
    if rom.is_file() and not stat.S_ISREG(rom.lstat().st_mode):
        raise ValueError(f"ROM não é arquivo regular: {rom}")
    stem = rom.stem if rom.is_file() else rom.name
    safe_stem = unicodedata.normalize("NFKC", stem)
    safe_stem = re.sub(r"[\x00-\x1f/\\]", "_", safe_stem).strip(" .") or args.system_id
    if len(safe_stem) > 176:
        suffix = _sha256_text(safe_stem)[:12]
        safe_stem = safe_stem[:155].rstrip() + "~" + suffix
    digest = _sha256(rom) if rom.is_file() else ""
    return rom, safe_stem, digest


def _sha256_text(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_screenshot_destination(project_root: Path, system_id: str, stem: str,
                                 kinds: list[str], event_id: str) -> Path:
    directory = project_root / "media" / system_id / "screenshot"
    _reject_symlink_components(directory)
    suffix = "-".join(re.sub(r"[^a-z0-9-]", "-", kind.casefold()) for kind in kinds)
    return directory / f"{stem}__{suffix}__{_utc_timestamp()}__{event_id[:8]}.png"


def _capture_spectacle(destination: Path) -> dict:
    executable = shutil.which("spectacle")
    if not executable:
        for candidate in ("/usr/bin/spectacle", "/usr/sbin/spectacle"):
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                executable = candidate
                break
    if not executable:
        raise FileNotFoundError("Spectacle não está instalado ou não está no PATH")
    result = subprocess.run([executable, "--activewindow", "--background", "--nonotify",
                             "--output", str(destination)], capture_output=True, text=True,
                            timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Spectacle retornou {result.returncode}: {result.stderr.strip()[:1000]}")
    return {"executable": executable, "stdout": result.stdout.strip()[:500],
            "stderr": result.stderr.strip()[:1000]}


def _show_capture_notification(system_id: str, rom_stem: str,
                               kinds: list[str]) -> dict:
    executable = shutil.which("notify-send")
    if not executable:
        for candidate in ("/usr/bin/notify-send", "/usr/sbin/notify-send"):
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                executable = candidate
                break
    if not executable:
        return {"status": "unavailable", "reason": "notify-send não está instalado ou não está no PATH"}
    display_title = rom_stem if len(rom_stem) <= 72 else rom_stem[:69].rstrip() + "…"
    title_markup = html.escape(display_title, quote=True)
    system_markup = html.escape(system_id, quote=True)
    kind_markup = html.escape(", ".join(kinds), quote=True)
    body = (
        f'<span font_desc="Noto Sans 12" weight="semibold" foreground="#F8FAFC">{title_markup}</span>\n'
        f'<span font_desc="Noto Sans 10" foreground="#CBD5E1">{system_markup} · captura salva</span>\n'
        f'<span font_desc="Noto Sans 9" foreground="#94A3B8">{kind_markup}</span>'
    )
    try:
        result = subprocess.run(
            [executable, "--urgency=low", "--expire-time=4000", "--transient",
             "--app-name=ROMTool", "--icon=camera-photo", "Captura concluída", body],
            capture_output=True, text=True, timeout=4, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "executable": executable,
                "reason": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"status": "failed", "executable": executable,
                "reason": result.stderr.strip()[:500] or f"notify-send retornou {result.returncode}"}
    return {"status": "shown", "executable": executable, "font": "Noto Sans",
            "duration_ms": 4000, "focus_stealing": False}


def _copy_stable_source(source: Path, destination: Path, expected_signature: tuple) -> None:
    if _file_signature(source) != expected_signature:
        raise RuntimeError(f"Thumbnail mudou antes da cópia: {source}")
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    if _file_signature(source) != expected_signature:
        raise RuntimeError(f"Thumbnail mudou durante a cópia: {source}")


def _commit_no_replace(temporary: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(destination.parent)
    os.link(temporary, destination)
    temporary.unlink()
    descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _coverage(args, project_root: Path, output_root: Path, record_analysis) -> dict:
    catalog_path = Path(args.catalog)
    if not catalog_path.is_absolute():
        catalog_path = project_root / catalog_path
    profile_path = Path(args.profile)
    if not profile_path.is_absolute():
        profile_path = project_root / profile_path
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    collections = profile.get("collections", {})
    fallback_systems = profile.get("systems", {})
    rows = []
    for system in sorted(catalog.get("systems", []), key=lambda row: row.get("id", "")):
        rom_subdir = system.get("rom_subdir", "")
        matches = [(name, entry, "collection") for name, entry in collections.items()
                   if entry.get("rom_subdir") == rom_subdir]
        if not matches:
            matches = [(name, entry, "system") for name, entry in fallback_systems.items()
                       if entry.get("rom_subdir") == rom_subdir]
        if len(matches) > 1:
            profile_status = "ambiguous_host_mapping"
            collection, entry, source = "", {}, ""
        elif len(matches) == 1:
            collection, entry, source = matches[0]
            profile_status = entry.get("status", "profile_mapping_present")
            if entry.get("adapter") == "unavailable" or profile_status == "unavailable_on_host":
                profile_status = "unavailable_on_host"
        else:
            collection, entry, source = "", {}, ""
            profile_status = "missing_host_mapping"
        platform_kind = system.get("kind", "unknown")
        rows.append({
            "status": profile_status,
            "system_id": system.get("id", ""),
            "display_name": system.get("display_name", ""),
            "catalog_kind": platform_kind,
            "rom_subdir": rom_subdir,
            "host_profile_mapping": source,
            "host_collection": collection,
            "adapter": entry.get("adapter", ""),
            "emulator": entry.get("launcher", ""),
            "app_id": entry.get("app_id", ""),
            "retroarch_core": entry.get("core", ""),
            "host_profile_status": profile_status,
            "session_watch_supported_by_tool": platform_kind == "platform",
            "watch_configuration": "operator_supplies_save_state_card_roots_per_session",
            "native_state_thumbnail": "copy_supported_when_explicit_state-thumbnail_root_is_supplied",
            "in_game_save_event_precision": "not_proven_by_filesystem_write",
            "screen_capture_precision": "native_state_thumbnail_or_approximate_active_window",
            "emulator_launch_validated": False,
        })
    evidence = {
        "catalog_path": str(catalog_path.relative_to(project_root)) if catalog_path.is_relative_to(project_root) else str(catalog_path),
        "catalog_sha256": _sha256(catalog_path),
        "host_profile_path": str(profile_path.relative_to(project_root)) if profile_path.is_relative_to(project_root) else str(profile_path),
        "host_profile_sha256": _sha256(profile_path),
        "capture_module_path": str(Path(__file__).resolve().relative_to(project_root)),
        "capture_module_sha256": _sha256(Path(__file__).resolve()),
        "romtool_entrypoint_sha256": _sha256(project_root / "tools" / "roms" / "romtool.py"),
        "catalog_entry_count": len(rows),
        "profile_mapping_is_not_runtime_launch_evidence": True,
        "tool_scope": "explicit foreground-session watch; no emulator configuration or background service installed",
    }
    result = record_analysis("capture_coverage", "Save/state screenshot coverage by platform catalog", rows,
                             output_root / "analysis", evidence)
    result["summary"] = {
        "catalog_entries": len(rows),
        "platform_entries": sum(row["catalog_kind"] == "platform" for row in rows),
        "profile_mapped": sum(bool(row["host_profile_mapping"]) for row in rows),
        "profile_mapped_available": sum(bool(row["host_profile_mapping"])
                                         and row["host_profile_status"] != "unavailable_on_host" for row in rows),
        "profile_unavailable": sum(row["host_profile_status"] == "unavailable_on_host" for row in rows),
        "profile_missing_or_ambiguous": sum(row["host_profile_status"] in {"missing_host_mapping", "ambiguous_host_mapping"} for row in rows),
        "all_rows_require_per_session_roots": True,
    }
    return result


def _watch(args, *, project_root: Path, ops_root: Path,
           catalog_path: Path, atomic_json, append_event, rebuild_history_index, now) -> dict:
    if not args.apply:
        raise ValueError("capture watch exige --apply para gravar screenshots; saves/states/cartões nunca são alterados")
    if args.poll_seconds < 1 or args.poll_seconds > 60:
        raise ValueError("--poll-seconds deve ficar entre 1 e 60")
    if args.stable_seconds < 0.5 or args.stable_seconds > 60:
        raise ValueError("--stable-seconds deve ficar entre 0,5 e 60")
    if args.max_files < 1 or args.max_files > 1000000:
        raise ValueError("--max-files deve ficar entre 1 e 1000000")
    rom, rom_stem, rom_sha256 = _canonical_rom(args, project_root, catalog_path)
    roots = _parse_watch_roots(args.watch_root, project_root)
    if not args.assume_emulator_foreground and not any(root["kind"] == "state-thumbnail" for root in roots):
        raise ValueError("Sem state-thumbnail, passe --assume-emulator-foreground para autorizar uma captura aproximada da janela ativa")

    output_directory = project_root / "media" / args.system_id / "screenshot"
    _reject_symlink_components(output_directory)
    operation_id = f"capture-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    operation_path = ops_root / operation_id
    operation_path.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "save_state_screenshot_capture",
        "title": f"Session capture for {args.system_id}/{rom_stem}", "created_at": now(),
        "status": "running", "read_only_sources": True, "project_files_changed": False,
        "project_media_may_be_written": True, "save_state_card_sources_modified": False,
        "system_id": args.system_id, "rom_path": str(rom), "rom_sha256": rom_sha256,
        "rom_identity_basis": "canonical project path and basename; No-Intro identity is not revalidated by capture",
        "destination_root": str(output_directory),
        "watch_roots": [{"kind": root["kind"], "path": str(root["path"])} for root in roots],
        "watch_root_association": "operator supplied for this session; profile does not prove root ownership or game identity",
        "session_validation": "emulator process and active title are not independently verified",
        "configuration": {"poll_seconds": args.poll_seconds, "stable_seconds": args.stable_seconds,
                          "max_files": args.max_files,
                          "active_window_foreground": "operator_assertion" if args.assume_emulator_foreground else "not_used"},
        "capture_accuracy": {
            "save_or_memory_card_write": "approximate; file stability indicates a disk write, not the exact in-game save action",
            "state_thumbnail": "native emulator thumbnail content; does not prove an in-game save action",
        },
        "items": [], "event_counts": {"observed": 0, "captured": 0, "skipped": 0, "failed": 0},
    }
    atomic_json(operation_path / "manifest.json", manifest)
    try:
        observed, scan_counts = _scan_roots(roots, args.max_files)
        signatures = {key: value["signature"] for key, value in observed.items()}
        pending: dict[str, dict] = {}
        content_hashes: dict[str, str] = {}
        print(json.dumps({"capture_watch": "started", "operation_id": operation_id,
                          "system_id": args.system_id, "rom": str(rom),
                          "baseline_files": len(signatures), "destination": str(output_directory),
                          "accuracy": manifest["capture_accuracy"]}, ensure_ascii=False), flush=True)
        while True:
            time.sleep(args.poll_seconds)
            current, scan_counts = _scan_roots(roots, args.max_files)
            monotonic_now = time.monotonic()
            removed = sorted(set(signatures) - set(current))
            for key in removed:
                source = Path(key)
                append_event(operation_id, "watched_file_removed", action="observe_only", status="observed",
                             source=key, details={"system_id": args.system_id, "rom": str(rom)})
                signatures.pop(key, None)
                content_hashes.pop(key, None)
                pending.pop(key, None)
                manifest["event_counts"]["observed"] += 1
            ready = []
            for key, item in current.items():
                signature = item["signature"]
                if signatures.get(key) != signature:
                    signatures[key] = signature
                    pending[key] = {"signature": signature, "first_seen": monotonic_now,
                                    "kinds": sorted(item["kinds"])}
                    continue
                candidate = pending.get(key)
                if candidate and candidate["signature"] == signature and monotonic_now - candidate["first_seen"] >= args.stable_seconds:
                    try:
                        digest = _sha256(item["path"])
                        if _file_signature(item["path"]) != signature:
                            pending[key] = {"signature": _file_signature(item["path"]),
                                            "first_seen": monotonic_now, "kinds": sorted(item["kinds"])}
                            signatures[key] = pending[key]["signature"]
                            continue
                    except OSError as exc:
                        pending.pop(key, None)
                        append_event(operation_id, "watched_file_hash_failed", action="hash", status="failed",
                                     source=key, details={"error": f"{type(exc).__name__}: {exc}"})
                        manifest["event_counts"]["failed"] += 1
                        continue
                    pending.pop(key, None)
                    if content_hashes.get(key) == digest:
                        continue
                    content_hashes[key] = digest
                    ready.append({"path": item["path"], "kinds": sorted(item["kinds"]),
                                  "signature": signature, "sha256": digest})
            if not ready:
                continue

            event_id = str(uuid.uuid4())
            event_kinds = sorted({kind for row in ready for kind in row["kinds"]})
            is_native_thumbnail = event_kinds == ["state-thumbnail"]
            source_thumbnail = next((row for row in ready if "state-thumbnail" in row["kinds"]), None)
            manifest["event_counts"]["observed"] += 1
            event = {
                "event_id": event_id, "observed_at": now(), "system_id": args.system_id,
                "rom_path": str(rom), "rom_sha256": rom_sha256, "trigger_kinds": event_kinds,
                "sources": [{"path": str(row["path"]), "kinds": row["kinds"],
                             "size_bytes": row["signature"][2], "sha256": row["sha256"]} for row in ready],
                "status": "capture_failed", "capture_method": "", "accuracy": "",
            }
            try:
                destination = _safe_screenshot_destination(project_root, args.system_id, rom_stem,
                                                          event_kinds, event_id)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _reject_symlink_components(destination.parent)
                descriptor, temporary_name = tempfile.mkstemp(prefix=".romtool-capture-", suffix=".png",
                                                               dir=destination.parent)
                os.close(descriptor)
                temporary = Path(temporary_name)
                temporary.unlink()
                backend_details = {}
                try:
                    if is_native_thumbnail:
                        thumbnail = source_thumbnail["path"]
                        _copy_stable_source(thumbnail, temporary, source_thumbnail["signature"])
                        event["capture_method"] = "emulator_native_state_thumbnail_copy"
                        event["accuracy"] = "native_state_thumbnail; not evidence of an in-game save action"
                    elif args.assume_emulator_foreground:
                        backend_details = _capture_spectacle(temporary)
                        event["capture_method"] = "spectacle_active_window"
                        event["accuracy"] = "approximate_post_write_active_window; foreground is operator asserted"
                    else:
                        raise RuntimeError("Evento sem thumbnail nativo e sem autorização de janela ativa")
                    width, height = _validate_png(temporary)
                    png_sha256 = _sha256(temporary)
                    png_size = temporary.stat().st_size
                    _commit_no_replace(temporary, destination)
                    event.update({"status": "captured", "destination": str(destination),
                                  "destination_relative": str(destination.relative_to(project_root)),
                                  "sha256": png_sha256, "size_bytes": png_size,
                                  "width": width, "height": height, "backend": backend_details})
                    notification = _show_capture_notification(args.system_id, rom_stem, event_kinds)
                    event["notification"] = notification
                    manifest["event_counts"]["captured"] += 1
                    append_event(operation_id, "save_state_screenshot_captured", action=event["capture_method"],
                                 status="completed", source=";".join(row["path"] for row in ready),
                                 destination=str(destination), sha256_after=png_sha256, size_after=png_size,
                                 details={"event_id": event_id, "system_id": args.system_id,
                                          "rom_path": str(rom), "rom_sha256": rom_sha256,
                                          "trigger_kinds": event_kinds, "sources": event["sources"],
                                          "accuracy": event["accuracy"], "width": width, "height": height,
                                          "notification": notification})
                finally:
                    if temporary.exists():
                        temporary.unlink()
            except Exception as exc:
                event["status"] = "capture_failed"
                event["error"] = f"{type(exc).__name__}: {exc}"
                manifest["event_counts"]["failed"] += 1
                append_event(operation_id, "save_state_screenshot_failed", action="capture", status="failed",
                             source=";".join(row["path"] for row in ready),
                             details={"event_id": event_id, "system_id": args.system_id,
                                      "rom_path": str(rom), "trigger_kinds": event_kinds,
                                      "error": event["error"]})
            manifest["items"].append(event)
            manifest["last_event_at"] = event["observed_at"]
            manifest["last_scan_counts"] = scan_counts
            atomic_json(operation_path / "manifest.json", manifest)
            print(json.dumps({"capture_event": event["status"], "operation_id": operation_id,
                              "event_id": event_id, "trigger_kinds": event_kinds,
                              "destination": event.get("destination", ""),
                              "accuracy": event.get("accuracy", ""),
                              "notification": event.get("notification", {}).get("status", "not_shown"),
                              "error": event.get("error", "")}, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        manifest["status"] = "completed"
        manifest["stop_reason"] = "user_interrupt"
        manifest["completed_at"] = now()
        manifest["last_scan_counts"] = scan_counts
        atomic_json(operation_path / "manifest.json", manifest)
        append_event(operation_id, "capture_watch_stopped", action="user_interrupt", status="completed",
                     details={"captured": manifest["event_counts"]["captured"],
                              "failed": manifest["event_counts"]["failed"]})
        rebuild_history_index()
        print(json.dumps({"capture_watch": "stopped", "operation_id": operation_id,
                          "manifest": str(operation_path / "manifest.json"),
                          "captured": manifest["event_counts"]["captured"]}, ensure_ascii=False), flush=True)
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["completed_at"] = now()
        atomic_json(operation_path / "manifest.json", manifest)
        append_event(operation_id, "capture_watch_failed", action="watch", status="failed",
                     details={"error": manifest["error"]})
        rebuild_history_index()
        raise
    return {"operation_id": operation_id, "kind": manifest["kind"], "status": manifest["status"],
            "manifest": str(operation_path / "manifest.json"), "items": manifest["items"],
            "event_counts": manifest["event_counts"]}


def dispatch(args, *, project_root: Path, output_root: Path, ops_root: Path,
             record_analysis, atomic_json, append_event,
             rebuild_history_index, now) -> dict:
    catalog_path = project_root / "frontend" / "RetroFE" / "Config" / "platform-catalog.json"
    if args.capture_command == "coverage":
        return _coverage(args, project_root, output_root, record_analysis)
    if args.capture_command == "watch":
        return _watch(args, project_root=project_root, ops_root=ops_root,
                      catalog_path=catalog_path, atomic_json=atomic_json, append_event=append_event,
                      rebuild_history_index=rebuild_history_index, now=now)
    raise ValueError(args.capture_command)
