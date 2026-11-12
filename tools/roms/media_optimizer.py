"""Media optimization workflows used only by the official romtool CLI.

The module keeps media inventory, planning, conversion, verification and rollback
in one audited workflow. It has no third-party Python dependencies; converters
are discovered from the host and checked against the active RetroFE runtime.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from functools import lru_cache
from pathlib import Path


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}
AUDIO_EXTS = {".mp3", ".ogg", ".flac", ".wav", ".opus", ".mo3"}
PROTECTED_NAMES = {".nomedia", ".directory"}


def _run(command: list[str], timeout: int = 300, *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, input=input_bytes, capture_output=True, timeout=timeout, check=False)


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=None)
def _tool_version(name: str, args: tuple[str, ...] = ("--version",)) -> str:
    executable = shutil.which(name)
    if not executable:
        return ""
    try:
        result = _run([executable, *args], timeout=15)
        output = _text(result.stdout or result.stderr).strip().splitlines()
        return output[0][:300] if output else f"{name}: version output unavailable"
    except Exception as exc:
        return f"{name}: {type(exc).__name__}: {exc}"


def _profile_tool_versions(profile: dict) -> dict:
    names = set(profile.get("tools", []))
    if profile.get("ffmpeg_encoder"):
        names.update({"ffmpeg", "ffprobe"})
    version_flags = {"ffmpeg": ("-version",), "ffprobe": ("-version",),
                     "cwebp": ("-version",), "jpegtran": ("-version",)}
    return {name: _tool_version(name, version_flags.get(name, ("--version",)))
            for name in sorted(names)}


def _config(api: dict) -> tuple[dict, Path, str]:
    path = api["TOOL_ROOT"] / "config" / "tooling.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    settings = document.get("media_optimization")
    if not isinstance(settings, dict) or not isinstance(settings.get("profiles"), list):
        raise ValueError("config/tooling.json não contém media_optimization.profiles")
    return settings, path, api["sha256_file"](path)


def _tool_source(api: dict) -> tuple[str, str]:
    path = api["TOOL_ROOT"] / "media_optimizer.py"
    return path.relative_to(api["PROJECT_ROOT"]).as_posix(), api["sha256_file"](path)


def _assert_tool_source_binding(manifest: dict, api: dict) -> str:
    _, current_hash = _tool_source(api)
    bound_hash = manifest.get("tool_source_sha256") or manifest.get("tool_source_attested_sha256")
    if not bound_hash or bound_hash != current_hash:
        raise ValueError("media_optimizer.py mudou desde o plano; gere outro plano ou use attest após revalidar compatibilidade")
    return current_hash


def _root(api: dict, value: str | None = None) -> Path:
    canonical = api["MEDIA_DEST_ROOT"].resolve(strict=True)
    root = Path(value) if value else canonical
    api["reject_symlink_components"](root)
    root = root.resolve(strict=True)
    if root != canonical:
        raise ValueError(f"A otimização só aceita a raiz canônica de mídia: {canonical}")
    if not root.is_dir():
        raise NotADirectoryError(root)
    return root


def _media_type(relative: Path) -> tuple[str, str]:
    parts = relative.parts
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def _walk(root: Path):
    for current, directories, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        for name in sorted(list(directories)):
            path = base / name
            if path.is_symlink():
                directories.remove(name)
                yield path
        for name in sorted(filenames):
            yield base / name


def _alias_index(api: dict) -> dict[str, list[dict]]:
    root = api["MEDIA_SOURCE_ROOT"]
    result: dict[str, list[dict]] = {}
    if not root.is_dir():
        return result
    for current, directories, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        directories[:] = [name for name in directories if not (base / name).is_symlink()]
        for name in filenames:
            alias = base / name
            if not alias.is_symlink():
                continue
            try:
                target = alias.resolve(strict=False)
                result.setdefault(str(target), []).append({
                    "path": str(alias), "relative_path": alias.relative_to(api["PROJECT_ROOT"]).as_posix(),
                    "target": os.readlink(alias),
                })
            except (OSError, ValueError):
                continue
    return result


def _probe_av(path: Path, count_frames: bool = False) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        raise FileNotFoundError("ffprobe não está disponível")
    command = [executable, "-v", "error"]
    if count_frames:
        command.append("-count_frames")
    command.extend(["-show_streams", "-show_format", "-of", "json", str(path)])
    result = _run(command, timeout=90)
    if result.returncode:
        raise ValueError(f"ffprobe falhou: {_text(result.stderr).strip()[:500]}")
    data = json.loads(_text(result.stdout))
    streams = data.get("streams", [])
    return {
        "format_name": data.get("format", {}).get("format_name", ""),
        "duration": data.get("format", {}).get("duration", ""),
        "bit_rate": data.get("format", {}).get("bit_rate", ""),
        "streams": [{key: stream.get(key) for key in (
            "index", "codec_type", "codec_name", "width", "height", "pix_fmt", "profile",
            "sample_aspect_ratio", "display_aspect_ratio", "avg_frame_rate", "r_frame_rate",
            "duration", "nb_frames", "nb_read_frames",
            "sample_rate", "channels", "channel_layout", "bits_per_sample", "bits_per_raw_sample",
            "color_range", "color_space", "color_transfer", "color_primaries", "disposition",
        ) if key in stream} for stream in streams],
    }


def _probe_image(path: Path) -> dict:
    executable = shutil.which("magick")
    if not executable:
        raise FileNotFoundError("ImageMagick não está disponível")
    result = _run([executable, "identify", "-ping", "-format", "%m|%w|%h|%[channels]|%[colorspace]|%z\n", str(path)], timeout=60)
    if result.returncode:
        raise ValueError(f"ImageMagick não reconheceu a imagem: {_text(result.stderr).strip()[:500]}")
    frames = []
    for line in _text(result.stdout).splitlines():
        fields = line.split("|", 5)
        if len(fields) == 6:
            frames.append({"format": fields[0], "width": fields[1], "height": fields[2],
                           "channels": fields[3], "colorspace": fields[4], "depth": fields[5]})
    if not frames:
        raise ValueError("ImageMagick não retornou dimensões")
    return {"frames": frames, "frame_count": len(frames)}


def _probe_pdf(path: Path) -> dict:
    executable = shutil.which("pdfinfo")
    if not executable:
        raise FileNotFoundError("pdfinfo não está disponível")
    result = _run([executable, str(path)], timeout=60)
    if result.returncode:
        raise ValueError(f"pdfinfo falhou: {_text(result.stderr).strip()[:500]}")
    values = {}
    for line in _text(result.stdout).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() in {"Pages", "Page size", "File size", "PDF version", "Encrypted"}:
                values[key.strip()] = value.strip()
    if "Pages" not in values:
        raise ValueError("pdfinfo não confirmou a quantidade de páginas")
    return values


@lru_cache(maxsize=8)
def _retrofe_sdl_image_library(project_root: str) -> Path:
    executable = Path(project_root) / "frontend" / "RetroFE" / "RetroFE"
    ldd = shutil.which("ldd")
    if not executable.is_file() or not ldd:
        raise FileNotFoundError("Não foi possível localizar o binário RetroFE e ldd para resolver SDL2_image")
    result = _run([ldd, str(executable)], timeout=30)
    output = _text(result.stdout) + _text(result.stderr)
    if result.returncode:
        raise RuntimeError(f"ldd falhou ao inspecionar o RetroFE: {output[-500:]}")
    for line in output.splitlines():
        match = re.match(r"\s*(libSDL2_image[^\s]*)\s+=>\s+(\S+)", line)
        if match:
            if match.group(2) == "not":
                break
            library = Path(match.group(2))
            if library.is_file():
                return library.resolve()
    raise FileNotFoundError("O binário RetroFE não resolve uma biblioteca SDL2_image carregável")


@lru_cache(maxsize=None)
def _image_feature(flag_name: str, library_path: str) -> tuple[bool, str]:
    flags = {"WEBP": 0x00000008, "AVIF": 0x00000020}
    if flag_name not in flags:
        return False, f"flag SDL_image desconhecida: {flag_name}"
    try:
        library = ctypes.CDLL(library_path)
        init = library.IMG_Init
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        actual = int(init(flags[flag_name]))
        available = (actual & flags[flag_name]) == flags[flag_name]
        quit_fn = getattr(library, "IMG_Quit", None)
        if quit_fn:
            quit_fn()
        return available, f"SDL2_image do binário RetroFE={library_path}; IMG_Init({flag_name})={actual:#x}"
    except Exception as exc:
        return False, f"SDL2_image probe falhou: {type(exc).__name__}: {exc}"


@lru_cache(maxsize=None)
def _gst_element(name: str) -> tuple[bool, str]:
    executable = shutil.which("gst-inspect-1.0")
    if not executable:
        return False, "gst-inspect-1.0 não está disponível"
    try:
        result = _run([executable, name], timeout=20)
        if result.returncode:
            return False, f"GStreamer não encontrou {name}"
        lines = _text(result.stdout).splitlines()
        long_name = next((line.strip() for line in lines if "Long-name" in line), name)
        version = next((match.group(1) for line in lines
                        if (match := re.match(r"\s*Version\s+(.+?)\s*$", line))), "unknown")
        return True, f"{long_name}; Version: {version}"
    except Exception as exc:
        return False, f"Probe GStreamer falhou: {type(exc).__name__}: {exc}"


@lru_cache(maxsize=None)
def _ffmpeg_encoder(name: str) -> tuple[bool, str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        return False, "ffmpeg não está disponível"
    result = _run([executable, "-hide_banner", "-encoders"], timeout=30)
    output = _text(result.stdout) + _text(result.stderr)
    found = name in output
    return found, (f"ffmpeg encoder {name} disponível" if found else f"ffmpeg encoder {name} ausente")


@lru_cache(maxsize=None)
def _ffmpeg_filter(name: str) -> tuple[bool, str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        return False, "ffmpeg não está disponível"
    result = _run([executable, "-hide_banner", "-filters"], timeout=30)
    output = _text(result.stdout) + _text(result.stderr)
    found = any(re.search(rf"^\s*[TSC. ]{{3}}\s*{re.escape(name)}(?:\s|$)", line) for line in output.splitlines())
    return found, (f"ffmpeg filter {name} disponível" if found else f"ffmpeg filter {name} ausente")


def _ready(profile: dict, source: Path, media: dict, api: dict) -> tuple[bool, str, dict]:
    profile_id = profile["id"]
    evidence: dict = {}
    required = profile.get("tools", [])
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        return False, f"Ferramentas ausentes: {', '.join(missing)}", evidence
    runtime = profile.get("runtime", {})
    if runtime.get("kind") == "sdl_image":
        try:
            library = _retrofe_sdl_image_library(str(api["PROJECT_ROOT"]))
            ok, note = _image_feature(runtime.get("feature", ""), str(library))
            if ok:
                evidence["runtime_library_path"] = str(library)
                evidence["runtime_library_sha256"] = api["sha256_file"](library)
        except Exception as exc:
            ok, note = False, f"Runtime SDL_image do RetroFE não foi resolvido: {type(exc).__name__}: {exc}"
        evidence["runtime_probe"] = note
        if not ok:
            return False, note, evidence
    elif runtime.get("kind") == "gstreamer":
        missing_elements = []
        for name in runtime.get("elements", []):
            ok, note = _gst_element(name)
            evidence.setdefault("runtime_elements", {})[name] = note
            if not ok:
                missing_elements.append(name)
        if missing_elements:
            return False, f"Runtime GStreamer sem: {', '.join(missing_elements)}", evidence
    encoder = profile.get("ffmpeg_encoder", "")
    if encoder:
        ok, note = _ffmpeg_encoder(encoder)
        evidence["encoder_probe"] = note
        if not ok:
            return False, note, evidence
    ffmpeg_filter = profile.get("ffmpeg_filter", "")
    if ffmpeg_filter:
        ok, note = _ffmpeg_filter(ffmpeg_filter)
        evidence["ffmpeg_filter_probe"] = note
        if not ok:
            return False, note, evidence
    if profile_id == "pdf-qpdf-lossless":
        required_pdf = ["qpdf", "pdfinfo", "pdftotext", "pdftoppm", "magick"]
        missing_pdf = [name for name in required_pdf if not shutil.which(name)]
        if missing_pdf:
            return False, f"Validação PDF requer: {', '.join(missing_pdf)}", evidence
    return True, "Ferramentas e formato de saída disponíveis no runtime configurado", evidence


def _profile_candidates(config: dict, extension: str, media_type: str) -> list[dict]:
    return [profile for profile in config.get("profiles", [])
            if extension in [value.casefold() for value in profile.get("source_extensions", [])]
            and (not profile.get("media_types") or media_type in profile["media_types"])]


def _scan_entries(root: Path, include_hash: bool, api: dict) -> list[dict]:
    rows = []
    for path in _walk(root):
        relative = path.relative_to(root)
        system_id, media_type = _media_type(relative)
        try:
            st = path.lstat()
            row = {"relative_path": relative.as_posix(), "system_id": system_id, "media_type": media_type,
                   "extension": path.suffix.casefold(), "name": path.name, "size_bytes": st.st_size,
                   "mtime_ns": st.st_mtime_ns, "device": st.st_dev, "inode": st.st_ino,
                   "sha256": "", "kind": "file", "status": "inventoried", "reason": ""}
            if path.is_symlink():
                row.update(kind="symlink", symlink_target=os.readlink(path), status="protected_symlink",
                           reason="Links simbólicos não são seguidos nem convertidos.")
            elif not path.is_file():
                row.update(kind="unsupported", status="unsupported_entry", reason="Entrada não é arquivo regular.")
            else:
                if include_hash:
                    row["sha256"] = api["sha256_file"](path)
                if path.name.casefold() in PROTECTED_NAMES:
                    row.update(status="protected_marker", reason="Marcador de escopo preservado.")
                elif media_type == "pending":
                    row.update(status="protected_staging", reason="media/pending é staging temporário, não biblioteca de conversão.")
                elif st.st_size == 0:
                    row.update(status="empty_preserved", reason="Arquivo vazio preservado; otimização não diagnostica nem descarta conteúdo.")
            rows.append(row)
        except OSError as exc:
            rows.append({"relative_path": relative.as_posix(), "system_id": system_id, "media_type": media_type,
                         "extension": path.suffix.casefold(), "name": path.name, "size_bytes": "", "sha256": "",
                         "kind": "unreadable", "status": "read_error", "reason": f"{type(exc).__name__}: {exc}"})
    return rows


def command_inventory(args, api: dict) -> dict:
    root = _root(api, args.root)
    rows = _scan_entries(root, args.hash, api)
    by_extension = Counter()
    size_by_extension = Counter()
    for row in rows:
        if row.get("kind") == "file":
            by_extension[row.get("extension") or "[sem extensão]"] += 1
            size_by_extension[row.get("extension") or "[sem extensão]"] += int(row.get("size_bytes") or 0)
    result = api["record_analysis"]("media_inventory", "Inventário de mídia canônica", rows,
                                    api["OUTPUT_ROOT"] / "analysis", {
        "root": str(root), "root_relative": root.relative_to(api["PROJECT_ROOT"]).as_posix(),
        "full_hashes_requested": bool(args.hash), "regular_files": sum(row.get("kind") == "file" for row in rows),
        "logical_bytes": sum(int(row.get("size_bytes") or 0) for row in rows if row.get("kind") == "file"),
        "extension_counts": dict(by_extension), "extension_bytes": dict(size_by_extension),
        "symlinks_followed": False,
    })
    result["regular_files"] = sum(row.get("kind") == "file" for row in rows)
    result["logical_bytes"] = sum(int(row.get("size_bytes") or 0) for row in rows if row.get("kind") == "file")
    result["extension_counts"] = dict(by_extension)
    return result


def _inventory_manifest(operation_id: str, api: dict) -> dict:
    path = api["operation_dir"](operation_id) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "media_inventory":
        raise ValueError(f"Operação não é inventário de mídia: {operation_id}")
    return manifest


def _profile_readiness(profile: dict, source: Path, relative: Path, runtime_meta: dict,
                       aliases: dict, config: dict, api: dict) -> tuple[str, str, dict]:
    extension = source.suffix.casefold()
    try:
        if extension in IMAGE_EXTS:
            image_meta = _probe_image(source)
            runtime_meta["image_probe"] = image_meta
            if image_meta["frame_count"] != 1:
                return "blocked_multiframe_image", "Perfil não aplica a imagens animadas ou multipágina.", runtime_meta
            if any(int(frame.get("depth", 0)) > 8 for frame in image_meta["frames"]):
                return "blocked_high_bit_depth", "Perfil inicial aceita somente imagens de até 8 bits por canal.", runtime_meta
        elif extension in VIDEO_EXTS or extension in AUDIO_EXTS:
            meta = _probe_av(source)
            runtime_meta["av"] = meta
        elif extension == ".pdf":
            meta = _probe_pdf(source)
            runtime_meta["pdf"] = meta
        else:
            meta = {}
    except Exception as exc:
        return "blocked_source_probe", f"{type(exc).__name__}: {exc}", runtime_meta

    ok, reason, evidence = _ready(profile, source, runtime_meta, api)
    runtime_meta.update(evidence)
    if not ok:
        return "blocked_runtime", reason, runtime_meta

    profile_id = profile["id"]
    if profile_id == "avi-matroska-h264-flac":
        streams = runtime_meta["av"].get("streams", [])
        video = [stream for stream in streams if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        other = [stream for stream in streams if stream.get("codec_type") not in {"video", "audio"}]
        attached = [stream for stream in streams if stream.get("disposition", {}).get("attached_pic")]
        if len(video) != 1 or len(audio) != 1 or other or attached:
            return "blocked_complex_streams", "Perfil inicial aceita um vídeo e um áudio, sem legendas/anexos/outros fluxos.", runtime_meta
        if video[0].get("codec_name") != "mjpeg" or audio[0].get("codec_name") != "pcm_s16le":
            return "blocked_codec_mismatch", "Perfil AVI é específico para MJPEG com PCM s16le; preservar outros codecs.", runtime_meta
        if video[0].get("pix_fmt") not in {"yuvj420p", "yuv420p"}:
            return "blocked_pixel_format", f"Formato de pixel requer perfil específico: {video[0].get('pix_fmt')}", runtime_meta
    elif profile_id == "mp4-av1-hq":
        streams = runtime_meta["av"].get("streams", [])
        video = [stream for stream in streams if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        other = [stream for stream in streams if stream.get("codec_type") not in {"video", "audio"}]
        attached = [stream for stream in streams if stream.get("disposition", {}).get("attached_pic")]
        if len(video) != 1 or len(audio) > 1 or other or attached:
            return "blocked_complex_streams", "Perfil MP4-AV1 aceita um vídeo, até um áudio e nenhum fluxo auxiliar.", runtime_meta
        if video[0].get("codec_name") != "h264" or video[0].get("pix_fmt") != "yuv420p":
            return "blocked_codec_mismatch", "Perfil MP4-AV1 só aceita vídeo H.264 8-bit yuv420p.", runtime_meta
        if video[0].get("color_transfer") in {"smpte2084", "arib-std-b67"}:
            return "blocked_hdr", "Perfil inicial SDR não transforma vídeo HDR.", runtime_meta
        if audio and audio[0].get("codec_name") not in {"aac", "mp3"}:
            return "blocked_audio_codec", "O perfil MP4-AV1 só copia faixas AAC/MP3; demais áudios ficam preservados.", runtime_meta
    elif profile_id == "wav-flac-lossless":
        streams = runtime_meta["av"].get("streams", [])
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        other = [stream for stream in streams if stream.get("codec_type") != "audio"]
        if len(audio) != 1 or other or audio[0].get("codec_name") not in {"pcm_s16le", "pcm_s24le", "pcm_s32le"}:
            return "blocked_codec_mismatch", "Perfil WAV-FLAC exige uma única faixa de PCM inteiro sem fluxos auxiliares.", runtime_meta
    elif profile_id == "pdf-qpdf-lossless":
        qpdf = shutil.which("qpdf")
        check = _run([qpdf, "--check", str(source)], timeout=90)
        if check.returncode:
            return "blocked_source_validation", f"qpdf --check: {_text(check.stderr).strip()[:300]}", runtime_meta

    target_ext = profile.get("target_extension", "same")
    destination = source if target_ext == "same" else source.with_suffix(target_ext)
    if destination != source and os.path.lexists(destination):
        return "blocked_destination_exists", f"Destino já existe; sem sobrescrita: {destination.name}", runtime_meta
    if destination != source and destination.name.casefold() == source.name.casefold():
        return "blocked_case_collision", "Destino colide com o nome de origem neste filesystem.", runtime_meta
    lossless = not bool(profile.get("lossy", False))
    status = "eligible_lossless" if lossless else "eligible_lossy_review"
    if destination != source:
        runtime_meta["legacy_aliases"] = aliases.get(str(source.resolve(strict=False)), [])
        runtime_meta["legacy_path_preserved_by_relative_symlink"] = True
    return status, profile.get("reason", reason), runtime_meta


def command_plan(args, api: dict) -> dict:
    root = _root(api)
    config, config_path, config_hash = _config(api)
    tool_source, tool_source_hash = _tool_source(api)
    inventory = _inventory_manifest(args.inventory_operation_id, api)
    if Path(inventory.get("evidence", {}).get("root", "")).resolve() != root:
        raise ValueError("O inventário não pertence à raiz canônica de mídia atual")
    selected_paths = set(getattr(args, "relative_path", None) or [])
    for value in selected_paths:
        rel = Path(value)
        if rel.is_absolute() or ".." in rel.parts or rel.as_posix() != value:
            raise ValueError(f"Seleção de mídia precisa ser relativa e normalizada: {value}")
    selected_profiles = set(getattr(args, "profile_id", None) or [])
    unknown_profiles = selected_profiles - {profile["id"] for profile in config["profiles"]}
    if unknown_profiles:
        raise ValueError(f"Perfis não configurados: {sorted(unknown_profiles)}")
    inventory_paths = {row.get("relative_path") for row in inventory.get("items", [])}
    if selected_paths - inventory_paths:
        raise ValueError("Seleção contém caminhos ausentes do inventário.")
    aliases = _alias_index(api)
    operation_id = f"media-optimization-{api['dt'].datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    items, audit_rows = [], []
    for entry in inventory.get("items", []):
        if selected_paths and entry.get("relative_path") not in selected_paths:
            continue
        relative = Path(entry.get("relative_path", ""))
        source = root / relative
        media_type = entry.get("media_type", "")
        extension = entry.get("extension", "").casefold()
        if entry.get("kind") != "file" or entry.get("status") not in {"inventoried", ""}:
            audit_rows.append({**entry, "profile_id": "", "candidate_id": "", "destination": "",
                               "plan_status": entry.get("status", "protected"), "plan_reason": entry.get("reason", "")})
            continue
        candidates = _profile_candidates(config, extension, media_type)
        if selected_profiles:
            candidates = [profile for profile in candidates if profile["id"] in selected_profiles]
        try:
            st = source.stat(follow_symlinks=False)
            if source.is_symlink() or st.st_size != int(entry.get("size_bytes", -1)) or st.st_mtime_ns != int(entry.get("mtime_ns", -1)):
                audit_rows.append({**entry, "profile_id": "", "candidate_id": "", "destination": "",
                                   "plan_status": "blocked_changed_since_inventory", "plan_reason": "Arquivo mudou desde o inventário; gere outro inventário."})
                continue
            source_hash = api["sha256_file"](source) if candidates else ""
        except Exception as exc:
            audit_rows.append({**entry, "profile_id": "", "candidate_id": "", "destination": "",
                               "plan_status": "blocked_source_read", "plan_reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not candidates:
            reason = "Sem perfil de conversão allowlist; preservado, não é indicação de lixo."
            if extension in {".mp3", ".mo3", ".flac", ".webp", ".avif"}:
                reason = "Formato já comprimido, lossless ou módulo; preservar sem nova geração de perda."
            audit_rows.append({**entry, "sha256": "", "profile_id": "", "candidate_id": "",
                               "destination": "", "plan_status": "no_profile_preserved", "plan_reason": reason})
            continue
        for profile in candidates:
            profile_id = profile["id"]
            metadata: dict = {}
            status, reason, metadata = _profile_readiness(profile, source, relative, metadata,
                                                           aliases, config, api)
            metadata["tool_versions"] = _profile_tool_versions(profile)
            target_ext = profile.get("target_extension", "same")
            destination = source if target_ext == "same" else source.with_suffix(target_ext)
            item_id = f"{operation_id}-{len(items) + 1:05d}" if status.startswith("eligible_") else ""
            audit_rows.append({
                **entry, "sha256": source_hash, "profile_id": profile_id, "candidate_id": item_id,
                "destination": str(destination), "destination_relative": destination.relative_to(root).as_posix(),
                "plan_status": status, "plan_reason": reason,
                "lossy": bool(profile.get("lossy", False)),
                "estimated_savings": "unknown_until_conversion",
            })
            if not item_id:
                continue
            st = source.stat(follow_symlinks=False)
            items.append({
                "item_id": item_id, "status": "prepared", "source": str(source),
                "source_relative": relative.as_posix(), "source_size_bytes": st.st_size,
                "source_sha256": source_hash, "source_mtime_ns": st.st_mtime_ns,
                "source_device": st.st_dev, "source_inode": st.st_ino,
                "destination": str(destination), "destination_relative": destination.relative_to(root).as_posix(),
                "source_extension": extension, "target_extension": target_ext,
                "system_id": entry.get("system_id", ""), "media_type": media_type,
                "item_identity": source.stem, "profile_id": profile_id,
                "lossy": bool(profile.get("lossy", False)), "profile": profile,
                "metadata": metadata, "reason": reason,
                "reference_check": "legacy path preserved; extension alias will remain relative",
                "frontend_aliases": metadata.get("legacy_aliases", []),
                "expected_validation": profile.get("validation", []),
                "rollback": "original retained at operation rollback path until explicit review",
            })
    opdir = api["operation_dir"](operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    audit_path = opdir / "media-optimization-audit.csv"
    audit_fields = ["relative_path", "system_id", "media_type", "extension", "size_bytes", "sha256",
                    "profile_id", "candidate_id", "destination", "plan_status", "plan_reason", "lossy",
                    "estimated_savings"]
    api["write_csv_atomic"](audit_path, audit_rows, audit_fields)
    counts = Counter(row.get("plan_status", "unknown") for row in audit_rows)
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "media_optimization",
        "title": "Plano allowlist de otimização da mídia canônica", "created_at": api["now"](),
        "status": "prepared", "read_only": True, "root": str(root),
        "root_relative": root.relative_to(api["PROJECT_ROOT"]).as_posix(),
        "inventory_operation_id": args.inventory_operation_id,
        "selection": {"relative_paths": sorted(selected_paths), "profile_ids": sorted(selected_profiles)},
        "profile_config": str(config_path), "profile_config_sha256": config_hash,
        "tool_source": tool_source, "tool_source_sha256": tool_source_hash,
        "audit_csv": str(audit_path), "audited_rows": len(audit_rows), "candidate_count": len(items),
        "items": items, "details": {
            "audit_status_counts": dict(counts), "estimated_savings": "unknown_until_per_item_conversion",
            "policy": {"canonical_root_only": True, "no_overwrite": True,
                       "symlinks_not_followed": True, "item_selection_required": True,
                       "lossy_requires_allow_lossy": True, "permanent_deletion": False,
                       "legacy_paths_preserved": True},
        },
    }
    api["atomic_json"](opdir / "manifest.json", manifest)
    api["append_event"](operation_id, "media_optimization_plan_created", action="media_optimize_plan",
                        status="prepared", details={"candidate_count": len(items), "audit_rows": len(audit_rows),
                                                     "audit_csv": str(audit_path), "status_counts": dict(counts)})
    covered = {str(root / row["relative_path"]) for row in audit_rows if row.get("relative_path")}
    superseded = api["supersede_prior_plans"](manifest, covers=lambda item: item.get("source") in covered)
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": manifest["kind"], "status": manifest["status"],
            "item_count": len(audit_rows), "candidate_count": len(items), "superseded_plans": superseded,
            "audit_status_counts": dict(counts), "manifest": str(opdir / "manifest.json"),
            "audit_csv": str(audit_path),
            "candidate_sample": [{"item_id": item["item_id"], "path": item["source_relative"],
                                  "profile": item["profile_id"], "lossy": item["lossy"]}
                                 for item in items[:25]]}


def _manifest(operation_id: str, api: dict) -> tuple[dict, Path]:
    path = api["operation_dir"](operation_id) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "media_optimization":
        raise ValueError(f"Operação não é um plano de otimização de mídia: {operation_id}")
    return manifest, path


def _validate_plan_item(item: dict, profile: dict, media_root: Path, api: dict) -> tuple[Path, Path]:
    source = Path(item["source"])
    destination = Path(item["destination"])
    if profile != item.get("profile") or profile.get("id") != item.get("profile_id"):
        raise ValueError(f"Perfil do item diverge da configuração allowlist: {item.get('item_id')}")
    source_relative = Path(item.get("source_relative", ""))
    if source_relative.is_absolute() or ".." in source_relative.parts:
        raise ValueError(f"Caminho relativo de origem inválido: {item.get('item_id')}")
    if source != media_root / source_relative or not api["is_within"](source, media_root):
        raise ValueError(f"Origem do item não corresponde à raiz e ao caminho relativo: {item.get('item_id')}")
    expected_destination = source if profile.get("target_extension", "same") == "same" else source.with_suffix(profile["target_extension"])
    if destination != expected_destination or not api["is_within"](destination, media_root):
        raise ValueError(f"Destino do item não corresponde ao perfil allowlist: {item.get('item_id')}")
    if item.get("destination_relative") != destination.relative_to(media_root).as_posix():
        raise ValueError(f"Caminho relativo de destino divergente: {item.get('item_id')}")
    return source, destination


def command_attest(args, api: dict) -> dict:
    manifest, manifest_path = _manifest(args.operation_id, api)
    if manifest.get("status") != "prepared" or any(item.get("status") != "prepared" for item in manifest.get("items", [])):
        raise ValueError("Só é possível atestar um plano prepared sem itens aplicados")
    if manifest.get("tool_source_sha256") or manifest.get("tool_source_attested_sha256"):
        raise ValueError("O plano já tem vínculo de código; gere um plano novo após mudanças posteriores")
    config, _, config_hash = _config(api)
    if manifest.get("profile_config_sha256") != config_hash:
        raise ValueError("config/tooling.json mudou desde o plano; não é seguro atestá-lo")
    profiles = {profile["id"]: profile for profile in config.get("profiles", [])}
    media_root = _root(api, manifest.get("root"))
    checked = 0
    for item in manifest.get("items", []):
        profile = profiles.get(item.get("profile_id"))
        if not profile:
            raise ValueError(f"Perfil mudou ou não está allowlisted: {item.get('item_id')}")
        source, _ = _validate_plan_item(item, profile, media_root, api)
        api["reject_symlink_components"](source)
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Origem não é arquivo regular: {item.get('item_id')}")
        st = source.stat(follow_symlinks=False)
        if (st.st_size != int(item["source_size_bytes"]) or st.st_mtime_ns != int(item["source_mtime_ns"])
                or st.st_dev != int(item["source_device"]) or st.st_ino != int(item["source_inode"])):
            raise ValueError(f"Origem mudou desde o plano: {item.get('item_id')}")
        current_versions = _profile_tool_versions(profile)
        if current_versions != item.get("metadata", {}).get("tool_versions", {}):
            raise ValueError(f"Versões das ferramentas mudaram: {item.get('item_id')}")
        ready, reason, runtime_evidence = _ready(profile, source, item.get("metadata", {}), api)
        if not ready:
            raise ValueError(f"Runtime não aceita mais o perfil {item.get('profile_id')}: {reason}")
        planned_metadata = item.get("metadata", {})
        changed_runtime = [key for key, value in runtime_evidence.items()
                           if key in planned_metadata and planned_metadata.get(key) != value]
        if changed_runtime:
            raise ValueError(f"Runtime divergiu do plano em {item.get('item_id')}: {', '.join(changed_runtime)}")
        checked += 1

    _, tool_hash = _tool_source(api)
    previous_manifest_sha256 = api["sha256_file"](manifest_path)
    attestation = {
        "attested_at": api["now"](), "reason": args.reason,
        "tool_source": "tools/roms/media_optimizer.py",
        "tool_source_sha256": tool_hash,
        "previous_manifest_sha256": previous_manifest_sha256,
        "checked_item_count": checked,
        "source_stat_rechecked": True,
        "source_sha256_rechecked": False,
        "tool_versions_and_runtime_rechecked": True,
        "note": "Apply rechecks each complete source SHA-256 immediately before conversion.",
    }
    manifest["tool_source_attested_sha256"] = tool_hash
    manifest["tool_source_attestation"] = attestation
    manifest["attested_at"] = attestation["attested_at"]
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "media_optimization_plan_attested", action="media_optimize_attest",
                        status=manifest["status"], details=attestation)
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": manifest["status"],
            "checked_items": checked, "tool_source_sha256": tool_hash,
            "previous_manifest_sha256": previous_manifest_sha256, "manifest": str(manifest_path)}


def _candidate_path(item: dict, operation_id: str, api: dict) -> Path:
    _validate_item_id(item.get("item_id", ""), operation_id)
    suffix = Path(item["source"]).suffix if item["target_extension"] == "same" else item["target_extension"]
    staging = api["operation_dir"](operation_id) / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    return staging / f"{item['item_id']}{suffix}"


def _validate_item_id(item_id: str, operation_id: str) -> None:
    if not re.fullmatch(re.escape(operation_id) + r"-[0-9]{5,}", item_id):
        raise ValueError(f"item_id inválido para a operação: {item_id}")


def _encode(profile: dict, source: Path, candidate: Path, item: dict) -> None:
    profile_id = profile["id"]
    if profile_id == "jpeg-lossless-repack":
        command = [shutil.which("jpegtran"), "-copy", "all", "-optimize", "-progressive",
                   "-outfile", str(candidate), str(source)]
    elif profile_id == "png-lossless-repack":
        command = [shutil.which("magick"), str(source), "-define", "png:compression-level=9",
                   "-define", "png:compression-filter=5", "-define", "png:compression-strategy=1", str(candidate)]
    elif profile_id == "png-webp-lossless":
        command = [shutil.which("cwebp"), "-lossless", "-exact", "-metadata", "all", "-m", "6",
                   str(source), "-o", str(candidate)]
    elif profile_id == "jpeg-webp-lossy":
        command = [shutil.which("cwebp"), "-q", str(profile.get("quality", 92)), "-m", "6",
                   "-metadata", "all", str(source), "-o", str(candidate)]
    elif profile_id == "avi-matroska-h264-flac":
        video = next((stream for stream in item.get("metadata", {}).get("av", {}).get("streams", [])
                      if stream.get("codec_type") == "video"), {})
        color = str(video.get("color_range", "")).casefold()
        range_args = ["-color_range", "pc"] if color in {"pc", "jpeg", "full"} else []
        command = [shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "0", "-map_chapters", "0",
                   "-c:v", "libx264", "-preset", str(profile.get("preset", "slow")),
                   "-crf", str(profile.get("crf", 18)), "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
                   *range_args, "-c:a", "flac", "-compression_level", str(profile.get("flac_level", 12)), str(candidate)]
    elif profile_id == "mp4-av1-hq":
        video = next((stream for stream in item.get("metadata", {}).get("av", {}).get("streams", [])
                      if stream.get("codec_type") == "video"), {})
        color = str(video.get("color_range", "")).casefold()
        range_args = ["-color_range", "pc"] if color in {"pc", "jpeg", "full"} else []
        command = [shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(source),
                   "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "0",
                   "-c:v", "libsvtav1", "-preset", str(profile.get("preset", 6)),
                   "-crf", str(profile.get("crf", 26)), "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
                   *range_args, "-c:a", "copy", "-movflags", "+faststart", str(candidate)]
    elif profile_id == "pdf-qpdf-lossless":
        command = [shutil.which("qpdf"), "--object-streams=generate", "--recompress-flate",
                   "--compression-level=9", str(source), str(candidate)]
    elif profile_id == "wav-flac-lossless":
        command = [shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(source),
                   "-map", "0:a:0", "-map_metadata", "0", "-c:a", "flac",
                   "-compression_level", str(profile.get("compression_level", 12)), str(candidate)]
    else:
        raise ValueError(f"Perfil sem conversor implementado: {profile_id}")
    if not command[0]:
        raise FileNotFoundError(f"Conversor ausente para o perfil {profile_id}")
    result = _run(command, timeout=int(profile.get("timeout_seconds", 7200)))
    if result.returncode:
        raise RuntimeError(f"Conversor falhou ({profile_id}): {_text(result.stderr).strip()[-1200:]}")
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise ValueError(f"Conversor não produziu candidato válido: {candidate}")


def _decode_video(path: Path) -> None:
    executable = shutil.which("ffmpeg")
    result = _run([executable, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
                   "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"], timeout=7200)
    if result.returncode:
        raise ValueError(f"Decodificação completa falhou ({path.name}): {_text(result.stderr).strip()[-1000:]}")


def _audio_pcm_sha256(path: Path, stream: dict) -> str:
    executable = shutil.which("ffmpeg")
    rate, channels = stream.get("sample_rate"), stream.get("channels")
    if not rate or not channels:
        raise ValueError("Não há taxa de amostragem/canais suficientes para comparar o áudio decodificado")
    try:
        source_bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 16)
    except (TypeError, ValueError):
        source_bits = 16
    pcm_codec, raw_format = ("pcm_s32le", "s32le") if source_bits > 16 else ("pcm_s16le", "s16le")
    command = [executable, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
               "-map", "0:a:0", "-ar", str(rate), "-ac", str(channels), "-c:a", pcm_codec, "-f", raw_format, "-"]
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file)
        assert proc.stdout is not None
        with proc.stdout:
            while block := proc.stdout.read(1024 * 1024):
                digest.update(block)
        try:
            code = proc.wait(timeout=7200)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        stderr_file.seek(0)
        stderr = stderr_file.read()
    if code:
        raise ValueError(f"Decodificação de áudio falhou: {_text(stderr).strip()[-1000:]}")
    return digest.hexdigest()


def _vmaf(source: Path, candidate: Path) -> float:
    executable = shutil.which("ffmpeg")
    result = _run([executable, "-hide_banner", "-nostdin", "-i", str(source), "-i", str(candidate),
                   "-lavfi", "[0:v:0][1:v:0]libvmaf=log_fmt=json", "-f", "null", "-"], timeout=7200)
    output = _text(result.stdout) + "\n" + _text(result.stderr)
    if result.returncode:
        raise ValueError(f"VMAF falhou: {output[-1000:]}")
    matches = re.findall(r"VMAF score:\s*([0-9]+(?:\.[0-9]+)?)", output, flags=re.IGNORECASE)
    if not matches:
        raise ValueError("FFmpeg não retornou a média VMAF")
    return float(matches[-1])


def _image_metric(source: Path, candidate: Path, metric: str) -> float:
    executable = shutil.which("magick")
    result = _run([executable, "compare", "-metric", metric, str(source), str(candidate), "null:"], timeout=600)
    output = _text(result.stderr or result.stdout).strip()
    match = re.search(r"(?:^|\s)([0-9]+(?:\.[0-9]+)?(?:e[-+]?[0-9]+)?)(?:\s|$)", output, flags=re.IGNORECASE)
    if result.returncode not in {0, 1} or not match:
        raise ValueError(f"ImageMagick compare falhou para métrica {metric}: {output[:500]}")
    return float(match.group(1))


def _pdf_render_files(path: Path, pages: int, temp: Path) -> list[Path]:
    executable = shutil.which("pdftoppm")
    out = temp / "page"
    result = _run([executable, "-r", "72", "-png", str(path), str(out)], timeout=1800)
    if result.returncode:
        raise ValueError(f"Render PDF falhou: {_text(result.stderr).strip()[-600:]}")
    rendered = sorted(temp.glob("page-*.png"))
    if len(rendered) != pages:
        raise ValueError(f"PDF renderizou {len(rendered)} páginas; esperado {pages}")
    return rendered


def _validate_pdf(source: Path, candidate: Path) -> dict:
    qpdf, pdfinfo, pdftotext = shutil.which("qpdf"), shutil.which("pdfinfo"), shutil.which("pdftotext")
    for path in (source, candidate):
        result = _run([qpdf, "--check", str(path)], timeout=180)
        if result.returncode:
            raise ValueError(f"qpdf --check falhou em {path.name}: {_text(result.stderr).strip()[-500:]}")
    source_meta, target_meta = _probe_pdf(source), _probe_pdf(candidate)
    pages = int(source_meta["Pages"])
    if int(target_meta["Pages"]) != pages:
        raise ValueError("A quantidade de páginas mudou")
    texts = []
    for path in (source, candidate):
        result = _run([pdftotext, "-raw", "-enc", "UTF-8", str(path), "-"], timeout=600)
        if result.returncode:
            raise ValueError(f"Extração de texto falhou em {path.name}")
        texts.append(hashlib.sha256(result.stdout).hexdigest())
    if texts[0] != texts[1]:
        raise ValueError("O texto extraído do PDF mudou")
    with tempfile.TemporaryDirectory(prefix="romtool-pdf-source-") as left, tempfile.TemporaryDirectory(prefix="romtool-pdf-target-") as right:
        left_pages = _pdf_render_files(source, pages, Path(left))
        right_pages = _pdf_render_files(candidate, pages, Path(right))
        if len(left_pages) != len(right_pages):
            raise ValueError("A quantidade de páginas renderizadas mudou")
        for source_page, target_page in zip(left_pages, right_pages):
            if _image_metric(source_page, target_page, "AE") != 0:
                raise ValueError(f"Render da página mudou: {source_page.name}")
        page_dimensions = []
        magick = shutil.which("magick")
        for page in left_pages:
            result = _run([magick, "identify", "-format", "%w|%h|%[channels]", str(page)], timeout=60)
            if result.returncode:
                raise ValueError(f"Não foi possível ler página renderizada: {page.name}")
            page_dimensions.append(_text(result.stdout).strip())
    return {"pages": pages, "text_sha256": texts[0], "page_render_dimensions": page_dimensions,
            "page_pixels_equal": True}


class NoMediaSavings(ValueError):
    """A larger candidate is discarded before expensive full comparison."""


def _validate_candidate(profile: dict, source: Path, candidate: Path, config: dict) -> dict:
    profile_id = profile["id"]
    evidence = {"source_size_bytes": source.stat().st_size, "candidate_size_bytes": candidate.stat().st_size}
    savings = source.stat().st_size - candidate.stat().st_size
    required = max(1, int(source.stat().st_size * float(config.get("minimum_savings_percent", 1.0)) / 100.0))
    evidence.update(savings_bytes=savings, savings_percent=round(100.0 * savings / max(1, source.stat().st_size), 4), minimum_savings_bytes=required)
    if savings < required:
        raise NoMediaSavings(f"Ganho inferior ao mínimo: {savings} bytes; requerido {required}")
    if profile_id in {"jpeg-lossless-repack", "png-lossless-repack", "png-webp-lossless", "jpeg-webp-lossy"}:
        source_meta, target_meta = _probe_image(source), _probe_image(candidate)
        extension_formats = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
        source_format = source_meta["frames"][0]["format"].casefold()
        target_format = target_meta["frames"][0]["format"].casefold()
        expected_source = extension_formats.get(source.suffix.casefold())
        expected_target = extension_formats.get(candidate.suffix.casefold())
        if expected_source and source_format != expected_source:
            raise ValueError(f"Conteúdo detectado {source_format} diverge da extensão {source.suffix}")
        if expected_target and target_format != expected_target:
            raise ValueError(f"Formato de saída detectado {target_format} diverge de {candidate.suffix}")
        if len(source_meta["frames"]) != len(target_meta["frames"]):
            raise ValueError("A quantidade de quadros da imagem mudou")
        for left, right in zip(source_meta["frames"], target_meta["frames"]):
            if (left["width"], left["height"]) != (right["width"], right["height"]):
                raise ValueError("As dimensões da imagem mudaram")
        metric = "AE" if not profile.get("lossy") else "SSIM"
        score = _image_metric(source, candidate, metric)
        evidence.update({"image_metric": metric, "image_metric_value": score,
                         "source_image": source_meta, "candidate_image": target_meta})
        if not profile.get("lossy") and score != 0:
            raise ValueError(f"Conversão declarada lossless alterou pixels (AE={score})")
        if profile_id == "png-lossless-repack":
            pixels = []
            for path in (source, candidate):
                raw = _run([shutil.which("magick"), str(path), "-alpha", "on", "-depth", "8", "rgba:-"], timeout=600)
                if raw.returncode:
                    raise ValueError("Não foi possível comparar RGBA integral do PNG")
                pixels.append(hashlib.sha256(raw.stdout).hexdigest())
            if pixels[0] != pixels[1]:
                raise ValueError("PNG alterou bytes RGBA, incluindo RGB de pixels transparentes")
            evidence["decoded_rgba_sha256"] = pixels[0]
        if profile.get("lossy") and score < float(config.get("minimum_image_ssim", 0.995)):
            raise ValueError(f"SSIM abaixo do limite: {score} < {config.get('minimum_image_ssim', 0.995)}")
    elif profile_id in {"avi-matroska-h264-flac", "mp4-av1-hq", "wav-flac-lossless"}:
        source_meta, target_meta = _probe_av(source, count_frames=True), _probe_av(candidate, count_frames=True)
        source_videos = [s for s in source_meta["streams"] if s.get("codec_type") == "video"]
        target_videos = [s for s in target_meta["streams"] if s.get("codec_type") == "video"]
        source_audios = [s for s in source_meta["streams"] if s.get("codec_type") == "audio"]
        target_audios = [s for s in target_meta["streams"] if s.get("codec_type") == "audio"]
        source_other = [s for s in source_meta["streams"] if s.get("codec_type") not in {"video", "audio"}]
        target_other = [s for s in target_meta["streams"] if s.get("codec_type") not in {"video", "audio"}]
        if target_other:
            raise ValueError("A conversão introduziu fluxos auxiliares inesperados")
        if profile_id == "avi-matroska-h264-flac":
            if len(source_videos) != 1 or len(target_videos) != 1 or len(source_audios) != 1 or len(target_audios) != 1 or source_other:
                raise ValueError("MKV precisa manter exatamente um vídeo e um áudio, sem fluxos auxiliares")
            if target_videos[0].get("codec_name") != "h264" or target_audios[0].get("codec_name") != "flac":
                raise ValueError("Saída AVI não corresponde ao perfil MKV H.264/FLAC")
            if "matroska" not in target_meta.get("format_name", ""):
                raise ValueError("Contêiner de saída não é Matroska")
        elif profile_id == "mp4-av1-hq":
            if len(source_videos) != 1 or len(target_videos) != 1 or len(source_audios) != len(target_audios) or len(source_audios) > 1 or source_other:
                raise ValueError("MP4 convertido mudou a quantidade de fluxos de áudio/vídeo")
            if target_videos[0].get("codec_name") != "av1" or "mp4" not in target_meta.get("format_name", ""):
                raise ValueError("Saída não é MP4 com vídeo AV1")
            if source_audios and target_audios[0].get("codec_name") != source_audios[0].get("codec_name"):
                raise ValueError("Codec de áudio copiado diverge da origem")
        elif profile_id == "wav-flac-lossless":
            if len(source_audios) != 1 or len(target_audios) != 1 or source_videos or source_other:
                raise ValueError("WAV convertido precisa manter uma única faixa de áudio sem fluxos auxiliares")
            if target_audios[0].get("codec_name") != "flac":
                raise ValueError("Saída WAV não contém áudio FLAC")
        source_video = next((s for s in source_meta["streams"] if s.get("codec_type") == "video"), None)
        target_video = next((s for s in target_meta["streams"] if s.get("codec_type") == "video"), None)
        if source_video and target_video:
            if (source_video.get("width"), source_video.get("height")) != (target_video.get("width"), target_video.get("height")):
                raise ValueError("A resolução do vídeo mudou")
            for key in ("sample_aspect_ratio", "display_aspect_ratio", "color_range",
                        "color_space", "color_transfer", "color_primaries"):
                if source_video.get(key) is not None and target_video.get(key) != source_video.get(key):
                    raise ValueError(f"Metadado de vídeo {key} mudou: {source_video.get(key)} -> {target_video.get(key)}")
            left_frames = source_video.get("nb_read_frames")
            right_frames = target_video.get("nb_read_frames")
            if not left_frames or not right_frames:
                raise ValueError("ffprobe não confirmou a contagem integral de quadros")
            if left_frames != right_frames:
                raise ValueError(f"A quantidade de quadros mudou: {left_frames} -> {right_frames}")
        try:
            source_duration = float(source_meta.get("duration") or 0)
            target_duration = float(target_meta.get("duration") or 0)
        except (TypeError, ValueError):
            source_duration = target_duration = 0
        if (profile_id != "wav-flac-lossless" and (not source_duration or not target_duration)):
            raise ValueError("ffprobe não confirmou a duração das duas versões de vídeo")
        if source_duration and target_duration and abs(source_duration - target_duration) > float(config.get("maximum_duration_delta_seconds", 0.05)):
            raise ValueError(f"Duração mudou: {source_duration:.6f}s -> {target_duration:.6f}s")
        _decode_video(source)
        _decode_video(candidate)
        evidence.update({"source_streams": source_meta["streams"], "candidate_streams": target_meta["streams"],
                         "full_decode": "passed_source_and_candidate"})
        source_audio = source_audios[0] if source_audios else None
        target_audio = target_audios[0] if target_audios else None
        if source_audio and target_audio:
            if (source_audio.get("sample_rate"), source_audio.get("channels")) != (target_audio.get("sample_rate"), target_audio.get("channels")):
                raise ValueError("Taxa de amostragem ou canais do áudio mudaram")
            if source_audio.get("channel_layout") != target_audio.get("channel_layout"):
                raise ValueError("Layout de canais do áudio mudou")
            source_pcm = _audio_pcm_sha256(source, source_audio)
            target_pcm = _audio_pcm_sha256(candidate, source_audio)
            evidence["source_audio_pcm_sha256"] = source_pcm
            evidence["candidate_audio_pcm_sha256"] = target_pcm
            if source_pcm != target_pcm:
                raise ValueError("O áudio decodificado mudou; perfil exige preservação exata do áudio")
        if profile.get("lossy"):
            score = _vmaf(source, candidate)
            evidence["vmaf_mean"] = score
            minimum = float(config.get("minimum_vmaf", 97.0))
            if score < minimum:
                raise ValueError(f"VMAF abaixo do limite: {score:.3f} < {minimum:.3f}")
    elif profile_id == "pdf-qpdf-lossless":
        evidence["pdf_validation"] = _validate_pdf(source, candidate)
    else:
        raise ValueError(f"Validação não implementada para o perfil {profile_id}")

    return evidence


def _commit_candidate(item: dict, candidate: Path, backup: Path) -> dict:
    source, destination = Path(item["source"]), Path(item["destination"])
    if source.stat().st_dev != backup.parent.stat().st_dev:
        raise OSError("Rollback e mídia estão em sistemas de arquivos diferentes; operação bloqueada")
    if os.path.lexists(backup):
        raise FileExistsError(backup)
    if destination != source and os.path.lexists(destination):
        raise FileExistsError(destination)
    source_mode = source.stat().st_mode & 0o777
    os.chmod(candidate, source_mode)
    os.replace(source, backup)
    link_temp = source.with_name(f".{source.name}.{item['item_id']}.linktmp")
    link_temp_created = False
    legacy_link_installed = False
    try:
        os.replace(candidate, destination)
        if destination != source:
            if os.path.lexists(link_temp):
                raise FileExistsError(link_temp)
            os.symlink(destination.name, link_temp)
            link_temp_created = True
            os.replace(link_temp, source)
            link_temp_created = False
            legacy_link_installed = True
    except Exception:
        if link_temp_created:
            link_temp.unlink(missing_ok=True)
        if legacy_link_installed:
            if not source.is_symlink() or os.readlink(source) != destination.name:
                raise RuntimeError("O link legado mudou durante o rollback automático do commit")
            source.unlink()
        if destination != source and os.path.lexists(destination):
            os.replace(destination, candidate)
        if os.path.lexists(backup) and not os.path.lexists(source):
            os.replace(backup, source)
        raise
    return {"destination": str(destination), "size_bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
            "legacy_path_is_relative_symlink": destination != source,
            "legacy_symlink_target": os.readlink(source) if destination != source else ""}


def command_apply(args, api: dict) -> dict:
    if not args.apply:
        raise ValueError("Aplicação requer --apply explícito")
    if not args.item_id:
        raise ValueError("Selecione pelo menos um --item-id; aplicação em massa não está disponível")
    if len(args.item_id) != 1:
        raise ValueError("Aplique um único --item-id por execução para evitar concorrência ou aplicação parcial")
    manifest, manifest_path = _manifest(args.operation_id, api)
    config, config_path, config_hash = _config(api)
    if manifest.get("profile_config_sha256") != config_hash:
        raise ValueError("config/tooling.json mudou desde o plano; gere outro plano")
    _assert_tool_source_binding(manifest, api)
    if (api["retrofe_processes_running"]()
            or (api.get("esde_processes_running") and api["esde_processes_running"]())):
        raise RuntimeError("Feche o RetroFE e ES-DE antes de substituir mídia ativa")
    selected = set(args.item_id)
    if len(selected) != len(args.item_id):
        raise ValueError("item-id repetido")
    items_by_id = {item["item_id"]: item for item in manifest.get("items", [])}
    unknown = selected - set(items_by_id)
    if unknown:
        raise ValueError(f"item-id não encontrado no plano: {', '.join(sorted(unknown))}")
    chosen = [items_by_id[item_id] for item_id in args.item_id]
    if any(item.get("status") != "prepared" for item in chosen):
        raise ValueError("Todos os itens escolhidos precisam estar no estado prepared")
    if any(item.get("lossy") for item in chosen) and not args.allow_lossy:
        raise ValueError("O lote contém conversão com perda; informe --allow-lossy após revisar os limites do perfil")
    resumable = {"prepared", "partially_applied", "applied_with_errors", "applied_pending_verification"}
    if manifest.get("status") not in resumable:
        raise ValueError(f"Plano não está prepared: {manifest.get('status')}")

    opdir = api["operation_dir"](args.operation_id)
    media_root = _root(api, manifest.get("root"))
    profile_map = {profile["id"]: profile for profile in config.get("profiles", [])}
    validated = []
    for item in chosen:
        profile = profile_map.get(item.get("profile_id"))
        if not profile:
            raise ValueError(f"Perfil não está allowlisted: {item.get('profile_id')}")
        source, destination = _validate_plan_item(item, profile, media_root, api)
        candidate = _candidate_path(item, args.operation_id, api)
        validated.append((item, source, destination, candidate, profile))
    rollback_root = opdir / "rollback" / "originals"
    rollback_root.mkdir(parents=True, exist_ok=True)
    manifest.update(status="running", read_only=False, apply_started_at=api["now"]())
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "media_optimization_apply_started", action="media_optimize_apply",
                        status="running", details={"selected_item_ids": args.item_id})
    failures = []
    for item, source, destination, candidate, profile in validated:
        backup = rollback_root / f"{item['item_id']}{source.suffix}"
        committed_state = False
        try:
            if not api["is_within"](source, media_root) or not api["is_within"](destination, media_root):
                raise ValueError("Origem/destino saiu da raiz canônica media/")
            api["ensure_allowed"](source)
            api["reject_symlink_components"](source)
            if source.is_symlink() or not source.is_file():
                raise ValueError("Origem não é arquivo regular")
            st = source.stat(follow_symlinks=False)
            if (st.st_size != int(item["source_size_bytes"]) or st.st_mtime_ns != int(item["source_mtime_ns"])
                    or st.st_dev != int(item["source_device"]) or st.st_ino != int(item["source_inode"])
                    or api["sha256_file"](source) != item["source_sha256"]):
                raise ValueError("A origem mudou desde o plano; gere plano novo")
            if os.path.lexists(candidate) or os.path.lexists(backup):
                raise FileExistsError("Candidato/backup já existe; nada será sobrescrito")
            if destination != source and os.path.lexists(destination):
                raise FileExistsError(f"Destino já existe: {destination}")
            ready, readiness_reason, readiness_evidence = _ready(profile, source, item.get("metadata", {}), api)
            if not ready:
                raise RuntimeError(f"Capacidade de runtime/conversor mudou: {readiness_reason}")
            planned_metadata = item.get("metadata", {})
            changed_runtime = [key for key, value in readiness_evidence.items()
                               if key in planned_metadata and planned_metadata.get(key) != value]
            if changed_runtime:
                raise RuntimeError(f"Runtime divergiu do plano: {', '.join(changed_runtime)}; gere outro plano")
            current_versions = _profile_tool_versions(profile)
            planned_versions = item.get("metadata", {}).get("tool_versions", {})
            if current_versions != planned_versions:
                raise RuntimeError("Versões das ferramentas mudaram desde o plano; gere outro plano")
            minimum_free = (int(st.st_size * float(profile.get("minimum_free_multiplier", 1.0)))
                            + int(config.get("minimum_free_bytes", 67108864)))
            free = shutil.disk_usage(source.parent).free
            if free < minimum_free:
                raise OSError(f"Espaço de staging insuficiente: livre {free}, mínimo {minimum_free}")
            if source.stat().st_dev != api["existing_path_device"](opdir):
                raise OSError("Pasta de operações e mídia estão em sistemas de arquivos diferentes")
            item.update(status="encoding", candidate_path=str(candidate), backup_path=str(backup),
                        converter_versions=current_versions, runtime_recheck=readiness_evidence)
            api["atomic_json"](manifest_path, manifest)
            api["append_event"](args.operation_id, "media_optimization_candidate_encoding", action=item["profile_id"],
                                status="running", source=item["source"], destination=item["destination"],
                                sha256_before=item["source_sha256"], size_before=item["source_size_bytes"],
                                details={"item_id": item["item_id"], "candidate": str(candidate),
                                         "converter_versions": current_versions})
            _encode(profile, source, candidate, item)
            validation = _validate_candidate(profile, source, candidate, config)
            item.update(status="candidate_validated", candidate_path=str(candidate), candidate_size_bytes=candidate.stat().st_size,
                        candidate_sha256=api["sha256_file"](candidate), validation=validation)
            api["atomic_json"](manifest_path, manifest)
            item.update(status="committing", commit_started_at=api["now"]())
            api["atomic_json"](manifest_path, manifest)
            api["append_event"](args.operation_id, "media_optimization_commit_started", action=item["profile_id"],
                                status="committing", source=item["source"], destination=item["destination"],
                                sha256_before=item["source_sha256"], sha256_after=item["candidate_sha256"],
                                details={"item_id": item["item_id"], "backup": str(backup)})
            committed = _commit_candidate(item, candidate, backup)
            committed_state = True
            for alternative in manifest.get("items", []):
                if (alternative.get("item_id") != item["item_id"] and alternative.get("source") == item["source"]
                        and alternative.get("status") == "prepared"):
                    alternative.update(status="superseded", superseded_by=item["item_id"],
                                       superseded_reason="another profile was applied to the same source")
            item.update(status="applied_unverified", backup_path=str(backup),
                        backup_sha256=item["source_sha256"], after=committed,
                        applied_at=api["now"](), validation=validation)
            api["append_event"](args.operation_id, "media_optimization_item_applied", action=item["profile_id"],
                                status=item["status"], source=item["source"], destination=item["destination"],
                                sha256_before=item["source_sha256"], sha256_after=committed["sha256"],
                                size_before=item["source_size_bytes"], size_after=committed["size_bytes"],
                                details={"item_id": item["item_id"], "backup": str(backup), "validation": validation})
        except NoMediaSavings as exc:
            candidate.unlink(missing_ok=True)
            item.update(status="skipped_no_gain", skip_reason=str(exc))
            api["append_event"](args.operation_id, "media_optimization_no_gain", action=item["profile_id"],
                                status="skipped_no_gain", source=item["source"], details={"item_id": item["item_id"], "reason": str(exc)})
        except Exception as exc:
            if committed_state:
                for alternative in manifest.get("items", []):
                    if (alternative.get("item_id") != item["item_id"] and alternative.get("source") == item["source"]
                            and alternative.get("status") == "prepared"):
                        alternative.update(status="superseded", superseded_by=item["item_id"],
                                           superseded_reason="another profile committed for the same source")
                item.update(status="applied_unverified", error=f"post_commit_recording: {type(exc).__name__}: {exc}")
            elif os.path.lexists(backup):
                item.update(status="committing_recovery_required",
                            error=f"commit interrompido; use rollback: {type(exc).__name__}: {exc}")
            else:
                candidate.unlink(missing_ok=True)
                item.update(status="apply_failed", error=f"{type(exc).__name__}: {exc}")
            failures.append({"item_id": item["item_id"], "error": item.get("error", "commit concluído; registro final pendente")})
            api["append_event"](args.operation_id, "media_optimization_item_failed", action=item["profile_id"],
                                status="apply_failed", source=item["source"], details=item["error"])
        api["atomic_json"](manifest_path, manifest)
    statuses = {item.get("status") for item in manifest.get("items", [])}
    if statuses & {"committing", "committing_recovery_required"}:
        manifest["status"] = "recovery_required"
    elif "prepared" in statuses:
        manifest["status"] = "applied_with_errors" if failures else "partially_applied"
    elif "apply_failed" in statuses or failures:
        manifest["status"] = "applied_with_errors"
    elif "applied_unverified" in statuses or "verification_failed" in statuses:
        manifest["status"] = "applied_pending_verification"
    else:
        manifest["status"] = "applied_verified"
    manifest["applied_at"] = api["now"]()
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "media_optimization_apply_finished", action="media_optimize_apply",
                        status=manifest["status"], details={"selected_count": len(chosen), "errors": failures})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": manifest["status"], "selected_count": len(chosen),
            "errors": failures, "manifest": str(manifest_path)}


def command_verify(args, api: dict) -> dict:
    manifest, manifest_path = _manifest(args.operation_id, api)
    config, _, config_hash = _config(api)
    if manifest.get("profile_config_sha256") != config_hash:
        raise ValueError("config/tooling.json mudou desde o plano; não é seguro reinterpretar os limites de validação")
    _assert_tool_source_binding(manifest, api)
    profile_map = {profile["id"]: profile for profile in config.get("profiles", [])}
    media_root = _root(api, manifest.get("root"))
    verified, failures = [], []
    for item in manifest.get("items", []):
        if item.get("status") in {"skipped_no_gain", "skipped_no_gain_verified"}:
            source = media_root / item["source_relative"]
            if source.is_symlink() or api["sha256_file"](source) != item["source_sha256"]:
                failures.append({"item_id": item["item_id"], "status": "verification_failed", "issues": ["Origem preservada sem ganho mudou"]})
                item["status"] = "verification_failed"
            else:
                item["status"] = "skipped_no_gain_verified"
                verified.append({"item_id": item["item_id"], "source": item["source_relative"],
                                 "destination": item["source_relative"], "status": "unchanged_no_gain",
                                 "sha256": item["source_sha256"], "size_bytes": source.stat().st_size})
            continue
        if item.get("status") not in {"applied_unverified", "verified", "verification_failed"}:
            continue
        _validate_item_id(item.get("item_id", ""), args.operation_id)
        source, destination = Path(item["source"]), Path(item["destination"])
        backup = Path(item.get("backup_path", ""))
        row = {"item_id": item["item_id"], "source": item["source_relative"],
               "destination": item["destination_relative"], "status": "verification_failed", "issues": []}
        try:
            profile = profile_map.get(item.get("profile_id"))
            if not profile:
                raise ValueError(f"Perfil não está allowlisted: {item.get('profile_id')}")
            source, destination = _validate_plan_item(item, profile, media_root, api)
            if not api["is_within"](backup, api["operation_dir"](args.operation_id) / "rollback" / "originals"):
                raise ValueError("Backup de rollback saiu da pasta da operação")
            if not destination.is_file() or destination.is_symlink():
                raise ValueError("Payload convertido não é arquivo regular")
            actual_hash = api["sha256_file"](destination)
            if actual_hash != item.get("after", {}).get("sha256"):
                raise ValueError("SHA-256 do payload convertido mudou")
            if not backup.is_file() or backup.is_symlink() or api["sha256_file"](backup) != item.get("source_sha256"):
                raise ValueError("Original de rollback ausente ou SHA-256 divergente")
            if destination != source:
                if not source.is_symlink() or os.readlink(source) != destination.name or source.resolve(strict=True) != destination.resolve(strict=True):
                    raise ValueError("Alias de extensão legado não é symlink relativo para o payload canônico")
            ready, reason, runtime_evidence = _ready(profile, destination, item.get("metadata", {}), api)
            if not ready:
                raise RuntimeError(f"Runtime atual não aceita a saída: {reason}")
            validation = _validate_candidate(profile, backup, destination, config)
            validation["runtime_recheck"] = runtime_evidence
            aliases = item.get("frontend_aliases", [])
            for alias_row in aliases:
                alias = Path(alias_row["path"])
                if not alias.is_symlink() or alias.resolve(strict=True) != destination.resolve(strict=True):
                    raise ValueError(f"Alias do RetroFE não resolve para o novo payload: {alias}")
            row.update(status="verified", sha256=actual_hash, size_bytes=destination.stat().st_size,
                       backup_sha256=api["sha256_file"](backup), frontend_alias_count=len(aliases),
                       validation=json.dumps(validation, ensure_ascii=False, sort_keys=True))
            item["status"] = "verified"
            item["verified_at"] = api["now"]()
            item["verification"] = validation
            verified.append(row)
        except Exception as exc:
            row["issues"].append(f"{type(exc).__name__}: {exc}")
            item["status"] = "verification_failed"
            item["verification_error"] = row["issues"][-1]
            failures.append(row)
        api["append_event"](args.operation_id, "media_optimization_item_verified", action=item["profile_id"],
                            status=row["status"], source=item["source"], destination=item["destination"],
                            sha256_after=row.get("sha256", ""), size_after=row.get("size_bytes", ""),
                            details={"item_id": item["item_id"], "issues": row["issues"]})
    if not verified and not failures:
        raise ValueError("Operação não possui itens aplicados para verificar")
    manifest["verification_status"] = "failed" if failures else "verified"
    manifest["verified_at"] = api["now"]()
    statuses = {item.get("status") for item in manifest.get("items", [])}
    if "prepared" in statuses:
        manifest["status"] = "applied_with_errors" if failures else "partially_applied"
    elif failures or "verification_failed" in statuses:
        manifest["status"] = "verification_failed"
    elif "applied_unverified" in statuses:
        manifest["status"] = "applied_pending_verification"
    else:
        manifest["status"] = "applied_verified"
    api["atomic_json"](manifest_path, manifest)
    verify_path = api["operation_dir"](args.operation_id) / "media-optimization-verification.csv"
    api["write_csv_atomic"](verify_path, verified + failures,
                            ["item_id", "source", "destination", "status", "sha256", "size_bytes",
                             "backup_sha256", "frontend_alias_count", "validation", "issues"])
    api["append_event"](args.operation_id, "media_optimization_verification_finished", action="media_optimize_verify",
                        status=manifest["verification_status"], details={"verified": len(verified),
                                                                          "failed": len(failures),
                                                                          "report": str(verify_path)})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": manifest["verification_status"],
            "verified": len(verified), "failed": len(failures), "report": str(verify_path),
            "manifest": str(manifest_path)}


def command_rollback(args, api: dict) -> dict:
    if not args.rollback:
        raise ValueError("Rollback requer --rollback explícito")
    if (api["retrofe_processes_running"]()
            or (api.get("esde_processes_running") and api["esde_processes_running"]())):
        raise RuntimeError("Feche o RetroFE e ES-DE antes de reverter mídia ativa")
    manifest, manifest_path = _manifest(args.operation_id, api)
    failures = []
    recoverable = {"applied_unverified", "verified", "verification_failed", "rollback_failed",
                   "encoding", "candidate_validated", "committing", "committing_recovery_required"}
    operation_dir = api["operation_dir"](args.operation_id)
    media_root = _root(api, manifest.get("root"))
    rollback_root = operation_dir / "rollback" / "originals"
    converted_root = operation_dir / "rollback" / "converted"
    converted_root.mkdir(parents=True, exist_ok=True)
    for item in reversed(manifest.get("items", [])):
        if item.get("status") not in recoverable:
            continue
        _validate_item_id(item.get("item_id", ""), args.operation_id)
        try:
            profile = item.get("profile")
            if not isinstance(profile, dict):
                raise ValueError("Perfil ausente no manifesto; rollback bloqueado")
            source, destination = _validate_plan_item(item, profile, media_root, api)
            backup = Path(item.get("backup_path") or rollback_root / f"{item['item_id']}{source.suffix}")
            candidate = _candidate_path(item, args.operation_id, api)
            expected_output = item.get("after", {}).get("sha256") or item.get("candidate_sha256", "")
            converted = converted_root / f"{item['item_id']}{destination.suffix}"
            if (not api["is_within"](source, media_root) or not api["is_within"](destination, media_root)
                    or not api["is_within"](backup, rollback_root) or not api["is_within"](candidate, operation_dir)):
                raise ValueError("Origem, destino ou rollback saiu dos diretórios canônicos")
            original_sha = item.get("source_sha256", "")
            if not os.path.lexists(backup):
                if source.is_symlink() or not source.is_file() or api["sha256_file"](source) != original_sha:
                    raise ValueError("Backup ausente e origem não contém o original intacto")
                if os.path.lexists(candidate):
                    if candidate.is_symlink():
                        raise ValueError("Candidato em staging virou symlink; rollback bloqueado")
                    candidate.unlink()
                item.update(status="rolled_back", rollback_noop=True, rolled_back_at=api["now"]())
                api["append_event"](args.operation_id, "media_optimization_item_rolled_back", action="rollback",
                                    status="rolled_back", source=str(source), sha256_after=original_sha,
                                    details={"item_id": item["item_id"], "restored_original": str(source),
                                             "reason": "interrupted before original was moved"})
                api["atomic_json"](manifest_path, manifest)
                continue

            if backup.is_symlink() or not backup.is_file() or api["sha256_file"](backup) != original_sha:
                raise ValueError("Original de rollback ausente ou SHA-256 divergente")
            if not expected_output:
                raise ValueError("Manifesto não tem hash do candidato para recuperar o commit")

            if source != destination:
                link_temp = source.with_name(f".{source.name}.{item['item_id']}.linktmp")
                if os.path.lexists(link_temp):
                    if not link_temp.is_symlink() or os.readlink(link_temp) != destination.name:
                        raise ValueError("Link temporário de commit mudou; rollback bloqueado")
                    link_temp.unlink()
                if os.path.lexists(source):
                    if not source.is_symlink() or os.readlink(source) != destination.name:
                        raise ValueError("Alias legado mudou desde a aplicação")
                    source.unlink()
                if os.path.lexists(destination):
                    if destination.is_symlink() or not destination.is_file() or api["sha256_file"](destination) != expected_output:
                        raise ValueError("Payload convertido mudou; rollback bloqueado")
                    if os.path.lexists(converted):
                        raise FileExistsError(converted)
                    os.replace(destination, converted)
            elif os.path.lexists(source):
                if source.is_symlink() or not source.is_file() or api["sha256_file"](source) != expected_output:
                    raise ValueError("Arquivo no caminho original não corresponde ao candidato aplicado")
                if os.path.lexists(converted):
                    raise FileExistsError(converted)
                os.replace(source, converted)

            if os.path.lexists(candidate):
                if candidate.is_symlink() or api["sha256_file"](candidate) != expected_output:
                    raise ValueError("Candidato de staging mudou; rollback bloqueado")
                staged = converted_root / f"{item['item_id']}.staged{candidate.suffix}"
                if os.path.lexists(staged):
                    raise FileExistsError(staged)
                os.replace(candidate, staged)
                item["rollback_staged_candidate_path"] = str(staged)
            if os.path.lexists(source):
                raise FileExistsError(f"Origem ocupada durante rollback: {source}")
            os.replace(backup, source)
            if api["sha256_file"](source) != original_sha:
                raise ValueError("SHA-256 restaurado diverge do original")
            item.update(status="rolled_back", rollback_converted_path=str(converted) if converted.exists() else "",
                        rolled_back_at=api["now"]())
            api["append_event"](args.operation_id, "media_optimization_item_rolled_back", action="rollback",
                                status="rolled_back", source=str(source), destination=str(converted),
                                sha256_before=expected_output, sha256_after=original_sha,
                                details={"item_id": item["item_id"], "restored_original": str(source)})
        except Exception as exc:
            item.update(status="rollback_failed", rollback_error=f"{type(exc).__name__}: {exc}")
            failures.append({"item_id": item["item_id"], "error": item["rollback_error"]})
        api["atomic_json"](manifest_path, manifest)
    manifest["status"] = "rollback_failed" if failures else "rolled_back"
    manifest["rolled_back_at"] = api["now"]()
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "media_optimization_rollback_finished", action="media_optimize_rollback",
                        status=manifest["status"], details={"errors": failures})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": manifest["status"],
            "errors": failures, "manifest": str(manifest_path)}


def dispatch(args, api: dict) -> dict:
    if args.media_optimize_command == "inventory":
        return command_inventory(args, api)
    if args.media_optimize_command == "plan":
        return command_plan(args, api)
    if args.media_optimize_command == "attest":
        return command_attest(args, api)
    if args.media_optimize_command == "apply":
        return command_apply(args, api)
    if args.media_optimize_command == "verify":
        return command_verify(args, api)
    if args.media_optimize_command == "rollback":
        return command_rollback(args, api)
    raise ValueError(f"Comando de otimização de mídia desconhecido: {args.media_optimize_command}")
