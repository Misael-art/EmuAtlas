"""Managed scraper sessions: private CoW view, one writer, checked publication.

The view is temporary operation staging, never a second library. Conversion is
delegated to the existing media_optimizer allowlist; canonical files are never
opened by a scraper for writing. No daemon or filesystem-specific executable.
"""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid

_LOCAL = threading.local()
NATIVE = {".png": "png-lossless-repack", ".jpg": "jpeg-lossless-repack",
          ".jpeg": "jpeg-lossless-repack", ".pdf": "pdf-qpdf-lossless"}


class MediaBusy(RuntimeError):
    pass


@contextmanager
def write_lock(api):
    if getattr(_LOCAL, "depth", 0):
        _LOCAL.depth += 1
        try:
            yield _LOCAL.fd
        finally:
            _LOCAL.depth -= 1
        return
    path = api["OPS_ROOT"] / "media-write.lock"
    if path.is_symlink():
        raise ValueError("Lock de mídia por symlink recusado")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MediaBusy("Outra sessão está preparando/gravando mídia; aguarde seu encerramento normal.")
        _LOCAL.depth, _LOCAL.fd = 1, fd
        yield fd
    finally:
        _LOCAL.depth = 0
        os.close(fd)


def busy(api):
    try:
        with write_lock(api):
            return False
    except MediaBusy:
        return True


def cookie(path):
    s = path.lstat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def clone(source, destination, writable=True):
    """Fail closed rather than silently copy a whole media library."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as left, destination.open("xb") as right:
        fcntl.ioctl(right.fileno(), 0x40049409, left.fileno())  # Linux FICLONE
    shutil.copystat(source, destination)
    destination.chmod(stat.S_IMODE(source.stat().st_mode) | (stat.S_IWUSR if writable else 0))


def _track_paths(directory, *, include_playlists=False):
    """Find local members declared by valid disc descriptors and optional M3U sets."""
    try:
        from . import romtool
    except ImportError:
        import romtool
    tracks = set()
    root = directory.resolve()
    validators = {".cue": (romtool.verify_cue, "references"), ".gdi": (romtool.verify_gdi, "tracks")}
    if include_playlists:
        validators[".m3u"] = (romtool.verify_m3u, "references")
    for current, dirs, files in os.walk(directory, followlinks=False):
        parent = Path(current)
        dirs[:] = [name for name in dirs if not (parent / name).is_symlink()]
        for name in files:
            descriptor = parent / name
            validator = validators.get(descriptor.suffix.lower())
            if validator is None or descriptor.is_symlink() or descriptor.stat().st_size > 1024 * 1024:
                continue
            check, key = validator
            result = check(descriptor)
            if result["status"] != "valid":
                continue
            targets = []
            for reference in result[key]:
                target = Path(reference["name"])
                if not target.is_absolute():
                    target = descriptor.parent / target
                targets.append(Path(os.path.abspath(target)))
            if any(not target.resolve().is_relative_to(root) or not target.is_file() for target in targets):
                continue
            if descriptor.suffix.lower() == ".m3u" and any(target.suffix.lower() == ".m3u" for target in targets):
                continue
            tracks.update(targets)
    return tracks


def _rom_names(root, system):
    """Share the catalog-filtered physical-name index with stray promotion."""
    try:
        from . import media_stray
    except ImportError:
        import media_stray
    root = Path(root).resolve()
    directory = root / "roms" / system
    names = {}
    for stem, values in media_stray._rom_stems(root, system, allow_unconfigured=True).items():
        for value in values:
            path = root / value
            relative = path.relative_to(directory)
            names.setdefault(stem, set()).add(relative.as_posix())
    return names


def _syncdir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _save(manifest, api):
    api["atomic_json"](api["operation_dir"](manifest["operation_id"]) / "manifest.json", manifest)


def _view(manifest,api):
    expected=api["operation_dir"](manifest["operation_id"])/"staging/media"
    if api["PROJECT_ROOT"]/manifest["view_relative"]!=expected:
        raise ValueError("View fora do staging da operação")
    api["esde_media"]._no_symlink_components(expected,api["operation_dir"](manifest["operation_id"]))
    return expected


def _helper(api):
    path = api["PROJECT_ROOT"] / "frontend/ES-DE/configure-rom-directory.py"
    spec = importlib.util.spec_from_file_location("media_session_configuration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_view(client, runtime, api):
    root = api["MEDIA_DEST_ROOT"]
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Raiz canônica de mídia inválida")
    operation_id = "media-session-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    opdir = api["operation_dir"](operation_id)
    view = opdir / "staging/media"
    view.mkdir(parents=True)
    manifest = {"schema_version": 1, "kind": "media_session", "operation_id": operation_id,
        "created_at": api["now"](), "status": "preparing", "client": client, "runtime": runtime,
        "view_relative": view.relative_to(api["PROJECT_ROOT"]).as_posix(), "baseline": {}, "items": [],
        "policy": {"copy_fallback": False, "canonical_root": "media", "lossy": False,
                   "delete_canonical_files": False, "incoming_temporaries_cleaned_after_validation": True}}
    _save(manifest, api)
    begin = time.monotonic()
    try:
        if shutil.disk_usage(root).free < 256 * 1024 * 1024:
            raise ValueError("Espaço livre inferior à reserva de 256 MiB; sessão recusada")
        for current, dirs, files in os.walk(root, followlinks=False):
            parent = Path(current)
            relparent = parent.relative_to(root)
            (view / relparent).mkdir(parents=True, exist_ok=True)
            for name in list(dirs):
                p = parent / name
                if not p.is_symlink():
                    continue
                target = p.resolve(strict=True)
                if root.resolve() not in target.parents or os.path.isabs(os.readlink(p)):
                    raise ValueError(f"Alias de diretório não gerenciado: {p}")
                (view / relparent / name).symlink_to(os.readlink(p), target_is_directory=True)
                dirs.remove(name)
            for name in files:
                source = parent / name
                if not source.is_file():
                    raise ValueError(f"Entrada de mídia não regular: {source}")
                resolved = source.resolve(strict=True)
                if api["PROJECT_ROOT"].resolve() not in resolved.parents:
                    raise ValueError(f"Alias fora do projeto: {source}")
                target = view / relparent / name
                before = cookie(source)
                clone(resolved, target)  # materialize leaf aliases so writes cannot follow into canonical data
                if cookie(source) != before:
                    raise ValueError(f"Fonte mudou durante a clonagem: {source}")
                manifest["baseline"][target.relative_to(view).as_posix()] = {
                    "view_cookie": cookie(target), "source_cookie": before,
                    "source_target_cookie": cookie(resolved), "source_link": os.readlink(source) if source.is_symlink() else None}
        # ES-DE 3.5.0 repeats the ROM subfolders below each media type. Present
        # those paths only in the owned CoW view, keeping canonical media flat.
        projected = 0
        for system in root.iterdir():
            if not system.is_dir() or system.is_symlink(): continue
            names = _rom_names(api["PROJECT_ROOT"], system.name)
            for category in system.iterdir():
                if not category.is_dir() or category.is_symlink(): continue
                for source in category.iterdir():
                    if not source.is_file(): continue
                    paths = names.get(source.stem, set())
                    if len(paths) != 1: continue
                    rom = Path(next(iter(paths)))
                    if rom.parent == Path('.'): continue
                    canonical = source.relative_to(root).as_posix()
                    target = view/system.name/category.name/rom.parent/source.name
                    if os.path.lexists(target): continue
                    clone(source.resolve(), target)
                    baseline = dict(manifest["baseline"][canonical])
                    baseline.update(view_cookie=cookie(target),canonical_relative=canonical)
                    manifest["baseline"][target.relative_to(view).as_posix()] = baseline
                    projected += 1
        baseline = opdir / "staging/baseline.json"
        api["atomic_json"](baseline,manifest.pop("baseline"))
        manifest.update(status="prepared", preparation_seconds=round(time.monotonic() - begin, 3),
                        cloned_files=len(json.loads(baseline.read_text())), physical_payload_bytes_copied=0,
                        baseline_relative=baseline.relative_to(api["PROJECT_ROOT"]).as_posix(), baseline_sha256=api["sha256_file"](baseline))
        manifest["nested_media_projections"] = projected
        _save(manifest, api)
        api["append_event"](operation_id, "media_session_prepared", action="reflink_view", status="prepared",
                            details={k:manifest[k] for k in ("client", "cloned_files", "preparation_seconds", "physical_payload_bytes_copied")})
        return manifest
    except Exception as exc:
        shutil.rmtree(view)  # only owned temporary clones, never canonical payloads
        manifest.update(status="preparation_failed", error=f"{type(exc).__name__}: {exc}")
        _save(manifest, api)
        raise RuntimeError("Não foi possível criar a sessão CoW; mídia original preservada. Sem fallback de cópia integral: " + str(exc)) from exc


def _configure(manifest, restore, api):
    helper = _helper(api)
    view = _view(manifest,api)
    if manifest["client"] == "esde":
        path = api["PROJECT_ROOT"] / "frontend/ES-DE/UserData/settings/es_settings.xml"
        if path.is_symlink():raise ValueError("Configuração ES-DE por symlink recusada")
        original = path.read_bytes()
        text, _ = helper.updated_setting_text(original.decode(), "MediaDirectory", str(api["MEDIA_DEST_ROOT"] if restore else view))
        updated = text.encode()
    else:
        path = api["PROJECT_ROOT"] / "tools/roms/skraper/skraper.config.mono.json"
        if path.is_symlink():raise ValueError("Configuração Skraper por symlink recusada")
        original = path.read_bytes()
        data = json.loads(original)
        prefix = str(view)
        if manifest["runtime"] == "wine":
            prefix = "Z:" + prefix.replace("/", "\\")
        sep = "\\" if manifest["runtime"] == "wine" else "/"
        media_base = "%ROMROOTFOLDER%/../../media/"
        def restore_paths(value):
            if isinstance(value,dict):return {k:restore_paths(v) for k,v in value.items()}
            if isinstance(value,list):return [restore_paths(v) for v in value]
            if isinstance(value,str):
                normalized=value.replace("\\","/")
                stage=prefix.replace("\\","/")+"/"
                if normalized.startswith(stage):return (media_base+normalized[len(stage):]).replace("/",sep)
            return value
        if restore:data=restore_paths(data)
        data, _ = helper.skraper_settings(data, manifest["runtime"], api["PROJECT_ROOT"])
        # Steady config never exports gamelists. A live session may export only
        # into this operation's staging/gamelists, never into the canonical file.
        for row in data["Systems"]:row["GameList"]["Type"]=0
        def walk(value):
            if isinstance(value, dict):
                return {k:(False if k == "EmptyOutputFolder" else walk(v)) for k,v in value.items()}
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, str):
                normalized = value.replace("\\", "/")
                stage = prefix.replace("\\", "/") + "/"
                if not restore and normalized.startswith(media_base):
                    return prefix + sep + normalized[len(media_base):].replace("/", sep)
                if restore and normalized.startswith(stage):
                    return (media_base + normalized[len(stage):]).replace("/", sep)
            return value
        data = walk(data)
        gamelist_root = view.parent / "gamelists"
        if not restore:
            gamelist_root.mkdir(parents=True, exist_ok=True)
            gamelist_prefix = str(gamelist_root)
            if manifest["runtime"] == "wine":
                gamelist_prefix = "Z:" + gamelist_prefix.replace("/", "\\")
            for row in data["Systems"]:
                sid = row["GamesAndRoms"]["Path"].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                (gamelist_root / sid).mkdir(parents=True, exist_ok=True)
                row["GameList"]["Type"] = 1
                row["GameList"]["OutputFile"] = gamelist_prefix + sep + sid + sep + "gamelist.xml"
        data["AllSystems"]["GameList"]["Type"] = 0
        data["AllSystems"]["GameList"]["OutputFile"] = ""
        updated = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    if not restore and "config_before_sha256" not in manifest:
        backup = api["operation_dir"](manifest["operation_id"]) / "rollback/config-before"
        backup.parent.mkdir(exist_ok=True)
        clone(path,backup)
        backup.chmod(0o600)
        manifest["config_before_sha256"] = hashlib.sha256(original).hexdigest()
    if updated != original:
        helper.write_atomic(path, updated, stat.S_IMODE(path.stat().st_mode))
    manifest["config_restored" if restore else "config_redirected"] = {
        "path": path.relative_to(api["PROJECT_ROOT"]).as_posix(), "sha256": hashlib.sha256(updated).hexdigest()}
    _save(manifest, api)


def _validate(path, api, *, pdf_linearization_review=False):
    optimizer = api["media_optimizer"]
    ext = path.suffix.lower()
    if ext in {".png", ".jpg", ".jpeg"}:
        info = optimizer._probe_image(path)
        expected = "PNG" if ext == ".png" else "JPEG"
        if any(x["format"].upper() != expected for x in info["frames"]):
            raise ValueError("Assinatura da imagem diverge da extensão")
        result = optimizer._run([shutil.which("magick"), str(path), "null:"], timeout=300)
        if result.returncode:
            raise ValueError("Decodificação integral da imagem falhou")
        return info
    if ext == ".mp4":
        info = optimizer._probe_av(path)
        optimizer._decode_video(path)
        return info
    if ext == ".pdf":
        info = optimizer._probe_pdf(path)
        check = optimizer._run([shutil.which("qpdf"), "--check", str(path)], timeout=180)
        warning_prefix = f"WARNING: {path}: "
        warning_lines = [line for line in optimizer._text(check.stderr).splitlines() if line.startswith("WARNING:")]
        warnings = [line[len(warning_prefix):] for line in warning_lines if line.startswith(warning_prefix)]
        linearization_patterns = (
            r"linearized file contains an uncompressed object after a compressed one in a cross-reference stream",
            r"first shared object number mismatch:.*", r"page \d+ has shared identifier entries",
            r"page \d+: shared object \d+: in hint table but not computed list",
            r"object count mismatch for page \d+:.*", r"incorrect offset in outlines table:.*",
            r"incorrect object count in outline hint table", r"shared object \d+ length mismatch:.*",
            r"first page object offset mismatch", r"page length mismatch for page \d+:.*",
            r"end of first page section \(/E\) mismatch:.*", r"part 8 is empty but nshared_total > nshared_first_page",
        )
        reviewed_warnings = bool(pdf_linearization_review and check.returncode == 3 and warnings
                                 and len(warnings) == len(warning_lines)
                                 and all(any(re.fullmatch(pattern, line) for pattern in linearization_patterns) for line in warnings))
        if check.returncode and not reviewed_warnings:
            raise ValueError("qpdf não aprovou o PDF sem warnings")
        with tempfile.TemporaryDirectory(dir=path.parent, prefix=".pdf-validation-") as raw:
            result = optimizer._run([shutil.which("pdftoppm"), "-scale-to", "64", "-png", str(path), str(Path(raw)/"page")], timeout=1800)
            if result.returncode or len(list(Path(raw).glob("page-*.png"))) != int(info["Pages"]):
                raise ValueError("Nem todas as páginas PDF foram decodificadas")
        return {**info, "qpdf_exit_code": check.returncode, "qpdf_warnings": warnings,
                "full_page_render": True, "linearization_warnings_reviewed": reviewed_warnings}
    raise ValueError(f"Formato sem perfil de entrada: {ext}")


def _representations(api):
    """Reuse official historical proofs, not a second persistent catalogue."""
    result = {}
    index = api["OPS_ROOT"] / "history-index.csv"
    if not index.exists():
        return result
    import csv
    with index.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        if row.get("kind") not in {"media_optimization", "media_session"} or row.get("status") in {"prepared", "rolled_back", "preparation_failed"}:
            continue
        path = api["operation_dir"](row["operation_id"]) / "manifest.json"
        data = json.loads(path.read_text())
        for item in data.get("items", []):
            if row["kind"] == "media_optimization" and item.get("status") in {"verified", "applied_unverified"} and not item.get("lossy"):
                key = (item["destination_relative"], item["source_sha256"])
                result[key] = item["after"]["sha256"]
            elif row["kind"] == "media_session" and item.get("status") == "published":
                result[(item["destination_relative"], item["incoming_sha256"])] = item["output_sha256"]
            elif row["kind"]=="media_session" and item.get("status")=="blocked" and item.get("incoming_sha256") and item.get("preserved_relative"):
                result[("blocked",item["source_relative"],item["incoming_sha256"])]=item["preserved_relative"]
    return result


def reconcile(manifest, api):
    root = api["MEDIA_DEST_ROOT"]
    view = _view(manifest,api)
    optimizer = api["media_optimizer"]
    mapping = api["esde_media"].MEDIA_TYPE_MAP
    valid_types = set(api["MEDIA_TYPES"]) | set(mapping.values())
    systems = api["esde_media"]._platform_ids(api["PROJECT_ROOT"])
    known = _representations(api)
    rom_names = {}
    baseline_path=api["operation_dir"](manifest["operation_id"])/"staging/baseline.json"
    if api["sha256_file"](baseline_path)!=manifest["baseline_sha256"]:
        raise ValueError("Snapshot temporário da sessão mudou")
    baseline_map=json.loads(baseline_path.read_text())
    prior = {x["source_relative"]:x for x in manifest["items"]}
    manifest["status"] = "reconciling"
    _save(manifest, api)
    config, config_path, config_hash = optimizer._config(api)
    profiles = {p["id"]:p for p in config["profiles"]}
    if not view.is_dir():
        raise ValueError("View de sessão ausente; não reconstruir sem revisão")
    incoming = []
    for current, dirs, names in os.walk(view, followlinks=False):
        parent = Path(current)
        dirs[:] = [d for d in dirs if not (parent/d).is_symlink() and not d.startswith(".pdf-validation-")]
        for name in names:
            path = parent / name
            rel = path.relative_to(view).as_posix()
            baseline = baseline_map.get(rel)
            if baseline and not path.is_symlink() and cookie(path) == baseline["view_cookie"]:
                continue
            incoming.append((rel,path))
    for relative, source in incoming:
        if relative in prior and prior[relative].get("status") in {"published", "deduplicated", "blocked"}:
            continue
        item = prior.get(relative)
        if item is None:
            item = {"source_relative": relative, "status": "checking"}
            manifest["items"].append(item)
        else:
            item["status"] = "checking"
        candidate = None
        try:
            if source.is_file() and not source.is_symlink():
                incoming_hash=api["sha256_file"](source)
                item["incoming_sha256"]=incoming_hash
                preserved=known.get(("blocked",relative,incoming_hash))
                if preserved:
                    peer=api["PROJECT_ROOT"]/preserved
                    if peer.is_file() and not peer.is_symlink() and api["sha256_file"](peer)==incoming_hash:
                        item.update(status="preserved_duplicate",preserved_relative=preserved)
                        _save(manifest,api);source.unlink()
                        api["append_event"](manifest["operation_id"],"media_temporary_deduplicated",action="owned_temporary_cleanup",status=item["status"],details=item)
                        continue
            parts = Path(relative).parts
            if source.is_symlink() or len(parts) < 3 or parts[0] not in systems:
                raise ValueError("Entrada não regular ou sem plataforma comprovada")
            media_type = mapping.get(parts[1], parts[1])
            if media_type not in valid_types or parts[0] == "pending":
                raise ValueError("Categoria sem regra canônica")
            canonical_rel = Path(parts[0],media_type,parts[-1])
            if parts[0] not in rom_names:rom_names[parts[0]]=_rom_names(api["PROJECT_ROOT"],parts[0])
            names = rom_names[parts[0]]
            matches = names.get(source.stem, set())
            if len(matches) != 1:
                raise ValueError("Mídia exige entrada local única; dependências declaradas por CUE/GDI/M3U não são entradas de jogo")
            if len(parts) > 3:
                rom_relative = Path(*parts[2:]).with_suffix('').as_posix()
                matched = Path(next(iter(matches)))
                if not (api["PROJECT_ROOT"] / "roms" / parts[0] / matched).is_dir():
                    matched = matched.with_suffix("")
                if matched.as_posix() != rom_relative:
                    raise ValueError("Mídia aninhada sem ROM exata única; não achatar títulos ambíguos")
            destination = root / canonical_rel
            if any(p.is_symlink() for p in (root/parts[0],destination.parent)):
                raise ValueError("Destino canônico atravessa um symlink")
            baseline = baseline_map.get(canonical_rel.as_posix())
            item["precondition"] = baseline
            if baseline:
                if not os.path.lexists(destination) or cookie(destination) != baseline["source_cookie"] or cookie(destination.resolve()) != baseline["source_target_cookie"]:
                    raise ValueError("Mídia canônica mudou fora da sessão; preservar para reconciliação")
            elif os.path.lexists(destination):
                raise ValueError("Destino novo apareceu fora da sessão")
            incoming_hash = api["sha256_file"](source)
            item.update(system_id=parts[0], media_type=media_type, incoming_sha256=incoming_hash,
                        incoming_bytes=source.stat().st_size, destination_relative=canonical_rel.as_posix(),
                        original_destination_relative=canonical_rel.as_posix())
            family = [destination]
            if destination.parent.is_dir():
                prefix=destination.stem+" (Alt "
                family.extend(p for p in sorted(destination.parent.iterdir()) if p.suffix==destination.suffix and p.stem.startswith(prefix) and p.stem.endswith(")") and p.stem[len(prefix):-1].isdigit())
            equivalent = None
            for peer in family:
                if not peer.is_file():
                    continue
                peer_hash = api["sha256_file"](peer)
                if peer_hash == incoming_hash or known.get((peer.relative_to(root).as_posix(),incoming_hash)) == peer_hash or known.get((canonical_rel.as_posix(),incoming_hash)) == peer_hash:
                    equivalent = peer
                    break
            if equivalent:
                item.update(status="deduplicated", canonical_peer=equivalent.relative_to(root).as_posix())
                _save(manifest, api)
                source.unlink()  # fresh owned temporary; verified bytes/equivalence already retained canonically
                api["append_event"](manifest["operation_id"],"media_temporary_deduplicated",action="owned_temporary_cleanup",status=item["status"],details=item)
                continue
            item["source_validation"] = _validate(source, api)
            if destination.is_file():
                decision, evidence = api["esde_media"]._quality_decision(source,destination,api)
                item["quality_evidence"] = evidence
                if decision != "source_preferred":
                    destination = api["esde_media"]._alt_path(destination,set())
            profile = profiles.get(NATIVE.get(source.suffix.lower(),""))
            if profile and (profile.get("lossy") or profile.get("target_extension") != "same"):
                raise ValueError("Perfil automático precisa ser nativo e sem perda")
            payload = source
            candidate = api["operation_dir"](manifest["operation_id"]) / ("candidate-"+uuid.uuid4().hex+source.suffix)
            image = item["source_validation"]
            if source.suffix.lower() in {".png", ".jpg", ".jpeg"} and (image["frame_count"] != 1 or any(int(f["depth"]) > 8 for f in image["frames"])):
                profile = None
                item["optimization_preserved_reason"] = "Imagem multiframe/alta profundidade: perfil preservado sem conversão"
            if profile:
                if shutil.disk_usage(root).free < source.stat().st_size + 256 * 1024 * 1024:
                    raise ValueError("Reserva insuficiente para candidato temporário; download preservado")
                ready, reason, _ = optimizer._ready(profile,source,{},api)
                if ready:
                    item.update(status="encoding",candidate_relative=candidate.relative_to(api["PROJECT_ROOT"]).as_posix())
                    _save(manifest,api)
                    optimizer._encode(profile,source,candidate,{})
                    try:
                        item["optimization_validation"] = optimizer._validate_candidate(profile,source,candidate,config)
                        payload = candidate
                    except optimizer.NoMediaSavings:
                        candidate.unlink()
                    item.update(profile_id=profile["id"], profile_config_sha256=config_hash,
                                converter_versions=optimizer._profile_tool_versions(profile))
                else:
                    item["optimization_preserved_reason"] = reason
            item.update(output_sha256=api["sha256_file"](payload), output_bytes=payload.stat().st_size,
                        destination_relative=destination.relative_to(root).as_posix(), status="planned",
                        publish_source_relative=payload.relative_to(api["PROJECT_ROOT"]).as_posix())
            _save(manifest, api)  # reviewable per-item plan before canonical mutation
            destination.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(destination):
                old_hash = api["sha256_file"](destination)
                if destination.is_symlink():
                    item["previous_symlink"] = os.readlink(destination)
                else:
                    alternate = api["esde_media"]._alt_path(destination,set())
                    item["previous_payload_relative"] = alternate.relative_to(root).as_posix()
                item["previous_sha256"] = old_hash
            if "previous_payload_relative" in item:
                backup = api["operation_dir"](manifest["operation_id"])/"rollback"/(uuid.uuid4().hex+destination.suffix)
                item.update(status="preserving",preimage_relative=backup.relative_to(api["PROJECT_ROOT"]).as_posix())
                _save(manifest,api)
                backup.parent.mkdir(exist_ok=True)
                clone(destination,backup,writable=False)
                if api["sha256_file"](backup) != item["previous_sha256"]:
                    raise ValueError("Pré-imagem de rollback divergente")
            item["status"] = "publishing"
            _save(manifest, api)
            os.replace(payload,destination)
            _syncdir(destination.parent)
            if api["sha256_file"](destination) != item["output_sha256"]:
                raise ValueError("SHA-256 após publicação divergiu")
            item["status"] = "finishing"
            _save(manifest,api)
            if item.get("preimage_relative"):
                backup = api["PROJECT_ROOT"]/item["preimage_relative"]
                alternate = root/item["previous_payload_relative"]
                if os.path.lexists(alternate):
                    raise ValueError("Destino de pré-imagem apareceu durante a transação")
                os.rename(backup,alternate)
                _syncdir(alternate.parent)
            item["status"] = "published"
            _save(manifest, api)
            source.unlink(missing_ok=True)  # original download was never canonical; rollback retains pre-session state
        except Exception as exc:
            if item.get("status") in {"preserving","publishing","finishing"}:
                manifest.update(status="recovery_required",error=f"{type(exc).__name__}: {exc}")
                _save(manifest,api)
                raise
            if isinstance(candidate,Path) and candidate.is_file():
                candidate.unlink()
            item.update(status="blocked", reason=f"{type(exc).__name__}: {exc}")
        _save(manifest, api)
        api["append_event"](manifest["operation_id"], "media_session_item_reconciled", action="media_ingress",
                            status=item["status"], details=item)
    blocked = [item for item in manifest["items"] if item["status"] == "blocked"]
    repair_aliases(api,manifest)
    for item in blocked:
        source = view/item["source_relative"]
        destination = api["operation_dir"](manifest["operation_id"])/"preserved"/item["source_relative"]
        if os.path.lexists(source):
            destination.parent.mkdir(parents=True,exist_ok=True)
            os.rename(source,destination)
        if os.path.lexists(destination):
            item["preserved_relative"] = destination.relative_to(api["PROJECT_ROOT"]).as_posix()
            _save(manifest,api)
    manifest["status"]="cleanup_pending"
    _save(manifest,api)
    return _finish_cleanup(manifest,api)


def _finish_cleanup(manifest,api):
    view=_view(manifest,api)
    if view.exists():shutil.rmtree(view)
    (api["operation_dir"](manifest["operation_id"])/"staging/baseline.json").unlink(missing_ok=True)
    config_backup=api["operation_dir"](manifest["operation_id"])/"rollback/config-before"
    if config_backup.exists() and manifest.get("config_restored"):
        config_backup.unlink()  # owned temporary redirect backup; canonical settings have already been restored
    for directory in (view.parent,config_backup.parent):
        if directory.is_dir() and not any(directory.iterdir()):directory.rmdir()
    manifest.pop("error",None)
    manifest.update(status="completed_pending_records", completed_at=api["now"](),
                    deleted_canonical_files=0, view_cleaned=True)
    _save(manifest,api)
    _records(manifest,api)
    manifest["status"]="completed_with_preserved_cases" if any(x["status"]=="blocked" for x in manifest["items"]) else "completed"
    _save(manifest,api)
    api["rebuild_history_index"]()
    return manifest


_SESSION_TOKEN_ENV = "ROMTOOL_MEDIA_SESSION_TOKEN"


def _process_identity(pid):
    """Linux process identity, including group and start time (PID reuse guard)."""
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rpartition(") ")[2].split()
        return {"state": fields[0], "group": int(fields[2]), "start": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def _boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _session_members(manifest, api):
    """Find session-owned, still-running processes, even after reparenting.

    The private process group catches children that close inherited descriptors;
    the inherited token also catches children that move to another group. Known
    PID/start pairs survive reparenting and cannot match reused PIDs.
    """
    guard = manifest.get("process_guard")
    if not guard:
        return None
    if guard["boot_id"] != _boot_id():
        return []  # processes from another boot cannot survive
    token = (_SESSION_TOKEN_ENV + "=" + guard["token"]).encode()
    observed = guard.setdefault("observed", {})
    live = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        ident = _process_identity(proc.name)
        if ident is None or ident["state"] in {"Z", "X", "x"}:
            continue
        pid = int(proc.name)
        start = ident["start"]
        if start < guard["start_floor"]:
            continue
        owned = ident["group"] == guard.get("group") or observed.get(proc.name) == start
        if not owned:
            try:
                if proc.stat().st_uid == os.getuid():
                    owned = token in (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                pass  # a member of the private process group remains detectable
        if owned:
            live.append(pid)
            observed[proc.name] = start
    return sorted(live)


def _wait_for_session_exit(manifest, api):
    """Keep the redirected view until the entire owned process set exits."""
    previous = -1
    while True:
        live = _session_members(manifest, api)
        if live is None:
            raise RuntimeError("Sessão sem identidade de processos verificável")
        if not live:
            manifest["processes_exited"] = True
            _save(manifest, api)
            return
        count = len(manifest["process_guard"]["observed"])
        if count != previous:
            previous = count
            manifest["remaining_processes"] = live
            _save(manifest, api)
        time.sleep(0.2)


def _recover(api):
    recovered=False
    try:
        _metadata().consume_pending(api)
    except Exception:
        pass
    for path in sorted(api["OPS_ROOT"].glob("media-session-*/manifest.json")):
        manifest = json.loads(path.read_text())
        if manifest.get("kind")!="media_session" or manifest["status"] in {"completed","completed_with_preserved_cases","preparation_failed","rolled_back"}:
            continue
        recovered=True
        if manifest["status"]=="completed_pending_records":
            _records(manifest,api)
            manifest["status"]="completed_with_preserved_cases" if any(x["status"]=="blocked" for x in manifest["items"]) else "completed"
            _save(manifest,api)
            _ingest(manifest, api)
            continue
        # Recovery must never restore a redirected MediaDirectory while an
        # emulator or scraper descendant may still be using the private view.
        if manifest.get("process_guard"):
            live = _session_members(manifest, api)
            if live:
                manifest["recovery_blocked_reason"] = "Processos da sessão ainda ativos"
                manifest["remaining_processes"] = live
                _save(manifest, api)
                raise MediaBusy(f"Sessão {manifest['operation_id']} ainda possui processos ativos: {live}")
            manifest.pop("recovery_blocked_reason", None)
            manifest.pop("remaining_processes", None)
        elif manifest.get("child_pid") or manifest.get("command") or manifest["status"] in {"starting", "running", "waiting_descendants"}:
            manifest["recovery_blocked_reason"] = "Sessão antiga sem identidade verificável de todos os descendentes"
            _save(manifest, api)
            raise MediaBusy(f"Sessão {manifest['operation_id']} sem evidência suficiente para recuperação automática")
        if manifest["status"]=="cleanup_pending":
            _configure(manifest,True,api)
            _finish_cleanup(manifest,api)
            _ingest(manifest, api)
            continue
        _configure(manifest,True,api)
        if manifest["status"] == "preparing":
            view = _view(manifest,api)
            if view.is_dir():shutil.rmtree(view)
            (api["operation_dir"](manifest["operation_id"])/"staging/baseline.json").unlink(missing_ok=True)
            manifest.update(status="preparation_failed",reason="Clonagem interrompida antes de abrir o aplicativo; temporários próprios removidos")
            _save(manifest,api)
            continue
        for item in manifest["items"]:
            if item.get("status") in {"publishing","finishing"}:
                destination = api["MEDIA_DEST_ROOT"]/item["destination_relative"]
                if destination.is_file() and api["sha256_file"](destination) == item["output_sha256"]:
                    backup = api["PROJECT_ROOT"]/item["preimage_relative"] if item.get("preimage_relative") else None
                    if backup and backup.is_file():
                        alternate = api["MEDIA_DEST_ROOT"]/item["previous_payload_relative"]
                        if os.path.lexists(alternate):raise ValueError("Pré-imagem externa apareceu durante recovery")
                        os.rename(backup,alternate)
                    if item.get("previous_payload_relative"):
                        alternate=api["MEDIA_DEST_ROOT"]/item["previous_payload_relative"]
                        if not alternate.is_file() or api["sha256_file"](alternate)!=item["previous_sha256"]:
                            raise ValueError("Pré-imagem publicada não foi comprovada; preservar para revisão")
                    item["status"]="published"
                    (api["PROJECT_ROOT"]/manifest["view_relative"]/item["source_relative"]).unlink(missing_ok=True)
                    continue
            if item.get("status") in {"preserving","publishing","finishing"}:
                destination = api["MEDIA_DEST_ROOT"]/item["destination_relative"]
                absent=item.get("previous_sha256") is None and not os.path.lexists(destination)
                if not absent and (not destination.is_file() or api["sha256_file"](destination) != item.get("previous_sha256")):
                    raise ValueError("Recovery não pode provar o estado anterior; preservar tudo")
                if item.get("preimage_relative"):
                    backup=api["PROJECT_ROOT"]/item["preimage_relative"]
                    if backup.exists():backup.unlink()  # owned CoW preimage; canonical still has identical original bytes
                item["status"]="checking"
            if item.get("status") not in {"published","deduplicated","blocked"} and item.get("candidate_relative"):
                candidate=api["PROJECT_ROOT"]/item["candidate_relative"]
                if candidate.parent!=api["operation_dir"](manifest["operation_id"]) or not candidate.name.startswith("candidate-"):
                    raise ValueError("Candidato fora do staging gerenciado")
                candidate.unlink(missing_ok=True)
                item["status"]="checking"
        _save(manifest,api)
        reconcile(manifest,api)
        _ingest(manifest, api)
    if recovered:api["rebuild_history_index"]()


def _records(manifest,api):
    if "OUTPUT_ROOT" not in api:return
    import csv
    output=api["OUTPUT_ROOT"]
    published=[x for x in manifest["items"] if x["status"]=="published"]
    if published:
        path=output/"catalogo-arquivos.csv"
        with path.open(encoding="utf-8-sig",newline="") as stream:
            reader=csv.DictReader(stream);fields=list(reader.fieldnames);rows=list(reader)
        for field in ("current_sha256","current_size_bytes","current_operation_id"):
            if field not in fields:fields.append(field)
        targets={}
        for item in published:
            targets[str(api["MEDIA_DEST_ROOT"]/item["destination_relative"])]=item
            if item.get("previous_payload_relative"):
                targets[str(api["MEDIA_DEST_ROOT"]/item["previous_payload_relative"])]=item
        seen=set()
        for row in rows:
            current=row.get("current_absolute_path") or row.get("absolute_path")
            if current in targets:
                file=Path(current)
                row.update(current_sha256=api["sha256_file"](file),current_size_bytes=file.stat().st_size,current_operation_id=manifest["operation_id"])
                seen.add(current)
        for current in sorted(set(targets)-seen):
            item=targets[current];file=Path(current);digest=api["sha256_file"](file)
            data={"system_id":item["system_id"],"source":"managed_media_session","source_root":str(api["MEDIA_DEST_ROOT"]),
                  "relative_path":file.relative_to(api["MEDIA_DEST_ROOT"]).as_posix(),"absolute_path":current,"file_name":file.name,
                  "inventory_category":"media","function_hint":"mídia externa compartilhada","extension":file.suffix,
                  "size_bytes":file.stat().st_size,"sha256":digest,"hash_status":"SHA-256 integral verificado",
                  "hash_evidence":manifest["operation_id"],"current_absolute_path":current,"current_path_status":"canônico verificado",
                  "current_sha256":digest,"current_size_bytes":file.stat().st_size,"current_operation_id":manifest["operation_id"]}
            rows.append({key:value for key,value in data.items() if key in fields})
        api["write_csv_atomic"](path,rows,fields)
        catalog_rows = len(rows)
    cases=[x for x in manifest["items"] if x["status"] in {"blocked","preserved_duplicate"}]
    if cases:
        path=output/"pendencias-integridade-dependencias.csv"
        with path.open(encoding="utf-8-sig",newline="") as stream:
            reader=csv.DictReader(stream);fields=list(reader.fieldnames);rows=list(reader)
        for field in ("case_sha256","case_source_relative"):
            if field not in fields:fields.append(field)
        for item in cases:
            digest=item.get("incoming_sha256", "symlink:"+item["source_relative"])
            existing=next((r for r in rows if r["source"]=="managed-media-session" and r.get("case_sha256")==digest and r.get("case_source_relative")==item["source_relative"]),None)
            data={"type":"media_ingress","source":"managed-media-session","path":item.get("preserved_relative",manifest["operation_id"]),
                  "status":"preservado fora da biblioteca ativa; "+item.get("reason","repetição de caso já preservado"),
                  "dependency_or_impact":"O conteúdo exige referência/perfil/validação adicional; não é autorização de exclusão.",
                  "next_step":"Consultar manifesto da sessão e resolver a causa específica antes de publicar.","case_sha256":digest,"case_source_relative":item["source_relative"]}
            if existing:existing.update(data)
            else:rows.append(data)
        api["write_csv_atomic"](path,rows,fields)
    path=output/"estado-final-verificacao.json"
    state=json.loads(path.read_text())
    if published:state["current_catalog_rows"]=catalog_rows
    state["managed_media_session"]={"as_of":api["now"](),"operation_id":manifest["operation_id"],"client":manifest["client"],
        "published":len(published),"deduplicated":sum(x["status"]=="deduplicated" for x in manifest["items"]),
        "preserved_cases":len(cases),"view_cleaned":manifest.get("view_cleaned"),"physical_payload_bytes_copied":0,
        "preparation_seconds":manifest["preparation_seconds"],"canonical_root":"media"}
    api["atomic_json"](path,state)


def _rollback_records(manifest,api):
    if "OUTPUT_ROOT" not in api:return
    import csv
    path=api["OUTPUT_ROOT"]/"catalogo-arquivos.csv"
    with path.open(encoding="utf-8-sig",newline="") as stream:
        reader=csv.DictReader(stream);fields=list(reader.fieldnames);rows=list(reader)
    for field in ("current_sha256","current_size_bytes","current_operation_id"):
        if field not in fields:fields.append(field)
    touched=set()
    for item in manifest["items"]:
        if item["status"]!="rolled_back":continue
        touched.add(str(api["MEDIA_DEST_ROOT"]/item["destination_relative"]))
        if item.get("previous_payload_relative"):touched.add(str(api["MEDIA_DEST_ROOT"]/item["previous_payload_relative"]))
    for row in rows:
        current=row.get("current_absolute_path") or row.get("absolute_path")
        if current not in touched:continue
        file=Path(current)
        row.update(current_operation_id=manifest["operation_id"],current_path_status="restaurado por rollback" if file.is_file() else "retirado da biblioteca ativa por rollback",
                   current_sha256=api["sha256_file"](file) if file.is_file() else "",current_size_bytes=file.stat().st_size if file.is_file() else "")
    api["write_csv_atomic"](path,rows,fields)
    path=api["OUTPUT_ROOT"]/"estado-final-verificacao.json"
    state=json.loads(path.read_text());state["managed_media_session"]={"as_of":api["now"](),"operation_id":manifest["operation_id"],"status":"rolled_back","canonical_root":"media"}
    api["atomic_json"](path,state)


def _metadata():
    try:
        from . import metadata_ingest
    except ImportError:
        import metadata_ingest
    return metadata_ingest


def _ingest(manifest, api):
    if manifest.get("metadata_ingested"):
        return manifest
    try:
        result = _metadata().ingest_session(api, manifest)
    except Exception as exc:
        manifest["metadata_ingest_error"] = f"{type(exc).__name__}: {exc}"
        manifest["metadata_ingested"] = False
        _save(manifest, api)
        return manifest
    manifest["metadata_ingested"] = bool(result.get("ok"))
    manifest["metadata_ingest"] = {key: result.get(key) for key in
        ("status", "stories_written", "conflicts", "hyperlists", "gamelists_changed", "absent_stories")}
    if result.get("manifest_relative"):
        manifest["metadata_ingest_relative"] = result["manifest_relative"]
    if not result.get("ok"):
        manifest["metadata_ingest_error"] = result.get("error", "ingestão não concluída")
    _save(manifest, api)
    return manifest


def command_session(args,api):
    if not args.child_command:
        raise ValueError("Sessão exige o comando do frontend/scraper após --")
    command = args.child_command[1:] if args.child_command[0] == "--" else args.child_command
    with write_lock(api) as fd:
        _recover(api)
        repair_aliases(api)
        if args.client=="skraper":_helper(api).prepare_skraper(args.runtime)
        else:_helper(api).apply_configuration(False)
        manifest = prepare_view(args.client,args.runtime,api)
        _configure(manifest,False,api)
        try:
            manifest["gamelist_snapshot"] = _metadata().snapshot_gamelists(api["PROJECT_ROOT"])
        except Exception as exc:
            manifest["gamelist_snapshot"] = {}
            manifest["gamelist_snapshot_error"] = f"{type(exc).__name__}: {exc}"
        _save(manifest, api)
        try:
            manifest["process_guard"] = {"token": uuid.uuid4().hex, "boot_id": _boot_id(),
                                         "start_floor": _process_identity(os.getpid())["start"],
                                         "group": None, "observed": {}}
            manifest["status"]="starting"
            _save(manifest,api)
            env = dict(os.environ, **{_SESSION_TOKEN_ENV: manifest["process_guard"]["token"]})
            child = subprocess.Popen(command, pass_fds=(fd,), env=env, start_new_session=True)
            manifest["process_guard"]["group"] = child.pid
            ident = _process_identity(child.pid)
            if ident:
                manifest["process_guard"]["observed"][str(child.pid)] = ident["start"]
            manifest.update(status="running", child_pid=child.pid, command=command)
            _save(manifest,api)
            # Observe descendants while the frontend is still alive. A child
            # may leave the original process group before the frontend exits;
            # persisting its PID/start identity lets recovery recognize it even
            # after it changes parent and environment.
            while True:
                before = len(manifest["process_guard"]["observed"])
                live = _session_members(manifest, api)
                if len(manifest["process_guard"]["observed"]) != before:
                    manifest["remaining_processes"] = live
                    _save(manifest, api)
                try:
                    returncode = child.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as exc:
            manifest["launch_error"]=str(exc)
            returncode=127
        manifest["child_exit_code"] = returncode
        manifest["status"] = "waiting_descendants"
        _save(manifest, api)
        _wait_for_session_exit(manifest, api)
        _configure(manifest,True,api)
        result = reconcile(manifest,api)
        _ingest(result, api)
        api["append_event"](manifest["operation_id"], "media_session_completed", action="managed_scraper_session",
                            status=result["status"], details={"child_exit_code":returncode,"items":len(result["items"]),"view_cleaned":result["view_cleaned"]})
        return result


def rollback(operation_id,apply,api):
    if not apply:raise ValueError("Rollback exige --rollback")
    if any(api["frontend_processes_running"]().values()):raise RuntimeError("Feche os frontends normalmente antes do rollback")
    with write_lock(api):
        path=api["operation_dir"](operation_id)/"manifest.json"
        manifest=json.loads(path.read_text())
        if manifest.get("kind")!="media_session" or manifest["status"] not in {"completed","completed_with_preserved_cases"}:
            raise ValueError("Sessão não está em estado reversível")
        ingest_path = api["operation_dir"](operation_id) / "metadata-ingest.json"
        ingest_document = json.loads(ingest_path.read_text(encoding="utf-8")) if ingest_path.is_file() else None
        if ingest_document:
            _metadata().assert_rollback_allowed(api["PROJECT_ROOT"], ingest_document)
        for item in reversed(manifest["items"]):
            if item["status"]!="published":continue
            destination=api["MEDIA_DEST_ROOT"]/item["destination_relative"]
            if not destination.is_file() or destination.is_symlink() or api["sha256_file"](destination)!=item["output_sha256"]:
                raise ValueError("Mídia alterada após sessão; não sobrescrever mudanças posteriores")
            old=api["MEDIA_DEST_ROOT"]/item["previous_payload_relative"] if item.get("previous_payload_relative") else None
            if old and api["sha256_file"](old)!=item["previous_sha256"]:raise ValueError("Pré-imagem divergente")
            if item.get("previous_symlink"):
                original=destination.parent/item["previous_symlink"]
                if not original.is_file() or api["sha256_file"](original)!=item["previous_sha256"]:raise ValueError("Alvo antigo do alias mudou")
            backup=api["operation_dir"](operation_id)/"rollback/applied"/(uuid.uuid4().hex+destination.suffix)
            backup.parent.mkdir(parents=True,exist_ok=True)
            if old or item.get("previous_symlink"):
                clone(destination,backup)
                if old:os.replace(old,destination)
                else:
                    temporary=backup.with_suffix(".link")
                    temporary.symlink_to(item["previous_symlink"])
                    os.replace(temporary,destination)
            else:os.rename(destination,backup)
            item.update(status="rolled_back",applied_payload_preserved_relative=backup.relative_to(api["PROJECT_ROOT"]).as_posix())
            _save(manifest,api)
        manifest["status"]="rolled_back";_save(manifest,api)
        if ingest_document and ingest_document.get("status") == "applied":
            _metadata().rollback(api["PROJECT_ROOT"], ingest_document, api["operation_dir"](operation_id))
        _rollback_records(manifest,api)
        api["append_event"](operation_id,"media_session_rolled_back",action="media_session_rollback",status="verified",details={"permanent_deletion":False})
        api["rebuild_history_index"]()
        return manifest


def repair_aliases(api,manifest=None):
    missing=[]
    root=api["MEDIA_DEST_ROOT"]
    systems=api["esde_media"]._platform_ids(api["PROJECT_ROOT"])
    for system in root.iterdir():
        if system.name not in systems or system.is_symlink() or not system.is_dir():continue
        for alias,target in api["esde_media"].MEDIA_TYPE_ALIASES.items():
            canonical=system/target; link=system/alias
            if not canonical.is_dir() or canonical.is_symlink():continue
            if os.path.lexists(link):
                if not link.is_symlink() or os.readlink(link)!=target:
                    raise ValueError(f"Alias divergente preservado, sem sobrescrita automática: {link}")
            else:missing.append({"path":link.relative_to(root).as_posix(),"target":target})
    if not missing:return
    own=manifest is None
    if own:
        operation_id="esde-alias-repair-"+datetime.now().strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:8]
        api["operation_dir"](operation_id).mkdir(parents=True)
        manifest={"schema_version":1,"operation_id":operation_id,"kind":"esde_alias_repair","created_at":api["now"](),"status":"prepared","items":missing}
    else:manifest.setdefault("aliases_created",[]).extend(missing)
    _save(manifest,api)
    for row in missing:(root/row["path"]).symlink_to(row["target"],target_is_directory=True)
    api["append_event"](manifest["operation_id"],"media_aliases_materialized",action="relative_aliases",status="verified",details={"aliases":missing})
    if own:
        manifest["status"]="completed";_save(manifest,api);api["rebuild_history_index"]()


def dispatch(args,api):
    if args.media_session_command=="rollback":return rollback(args.operation_id,args.rollback,api)
    if args.media_session_command=="run":
        try:return command_session(args,api)
        except Exception as exc:
            if shutil.which("notify-send"):subprocess.run(["notify-send","Mídia: sessão não iniciada/concluída",str(exc)[:300]],check=False)
            raise
    if args.media_session_command=="recover":
        with write_lock(api):
            _recover(api)
            repair_aliases(api)
        return {"status":"ready"}
    if args.media_session_command=="status":
        return {"status":"busy" if busy(api) else "ready","evidence":{"clients":["esde","skraper"],"canonical_root":"media","copy_fallback":False}}
    if args.media_session_command=="ingest":
        with write_lock(api):
            _recover(api)
            return _metadata().ingest_project(api, systems=getattr(args, "system_id", None), client="manual")
    raise ValueError("Comando de sessão desconhecido")
