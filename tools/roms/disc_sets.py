"""Create relative M3U playlists from explicitly identified, ordered disc sets."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid


def _path(root, relative):
    if not isinstance(relative, str) or not relative or relative != relative.strip() or "\\" in relative:
        raise ValueError("Caminho relativo de disco inválido")
    value = Path(relative)
    if value.is_absolute() or any(part in {".", ".."} for part in value.parts):
        raise ValueError("O conjunto deve permanecer na plataforma canônica")
    for part in value.parts:
        if (re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((" ", "."))
                or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)):
            raise ValueError("Nome incompatível com Windows")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Disco fora da plataforma")
    current = path
    while current != root.parent:
        if current.is_symlink():
            raise ValueError("O conjunto exige caminhos físicos sem aliases")
        current = current.parent
    return path


def _playlist(root, filename):
    target = _path(root, filename)
    if target.parent != root or target.suffix != ".m3u" or filename.startswith("#"):
        raise ValueError("A playlist deve ser um basename .m3u na pasta da plataforma")
    return target


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(manifest, api):
    api["atomic_json"](api["operation_dir"](manifest["operation_id"]) / "manifest.json", manifest)


def _load(operation_id, api):
    if not re.fullmatch(r"disc-set-[A-Za-z0-9_-]+", operation_id):
        raise ValueError("ID de operação de discos inválido")
    opdir = api["operation_dir"](operation_id)
    api["reject_symlink_components"](opdir)
    api["reject_symlink_components"](opdir / "manifest.json")
    manifest = json.loads((opdir / "manifest.json").read_text())
    if manifest.get("kind") != "disc_set_playlist" or manifest.get("operation_id") != operation_id:
        raise ValueError("Manifesto de conjunto incompatível")
    return manifest


def _root(system, api):
    if not isinstance(system, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", system):
        raise ValueError("Plataforma inválida")
    root = api["PROJECT_ROOT"] / "roms" / system
    api["reject_symlink_components"](root)
    if not root.is_dir():
        raise ValueError("Plataforma canônica ausente")
    return root


def _inputs(manifest, api):
    root = _root(manifest["system_id"], api)
    for item in manifest["inputs"]:
        path = _path(root, item["path"])
        if (not path.is_file() or path.stat().st_size != item["size_bytes"]
                or api["sha256_file"](path) != item["sha256"]):
            raise ValueError(f"Disco ou dependência mudou desde o plano: {item['path']}")
    return root


def _check_duplicate_playlist(root, target, paths, api):
    if any(p != target and p.name.casefold() == target.name.casefold() for p in root.iterdir()):
        raise FileExistsError("Colisão de nome Windows com outra entrada")
    selected = {_path(root, value).resolve() for value in paths}
    for existing in root.rglob("*"):
        if existing == target or existing.is_symlink() or not existing.is_file() or existing.suffix.lower() != ".m3u":
            continue
        if existing.stat().st_size > 1024 * 1024:
            continue
        check = api["verify_m3u"](existing)
        members = {(existing.parent / ref["name"]).resolve() for ref in check["references"]}
        if check["status"] == "valid" and members == selected:
            raise ValueError("O conjunto já tem uma playlist local: " + existing.relative_to(root).as_posix())


def plan(request, api):
    root = _root(request.get("system_id"), api)
    filename = request.get("playlist", "")
    target = _playlist(root, filename)
    if os.path.lexists(target) or any(p.name.casefold() == filename.casefold() for p in root.iterdir()):
        raise FileExistsError("Playlist existente ou colisão de nome Windows preservada")
    discs = request.get("discs", [])
    if (len(discs) < 2 or type(request.get("disc_count")) is not int
            or request["disc_count"] != len(discs)):
        raise ValueError("Informe o total confirmado e todos os discos do conjunto")
    if not request.get("identity_evidence") or not request.get("runtime_evidence"):
        raise ValueError("O plano exige evidência da identidade e do suporte M3U do runtime")
    catalog = json.loads((api["PROJECT_ROOT"] / "frontend/RetroFE/Config/platform-catalog.json").read_text())
    system = next((s for s in catalog["systems"] if s["id"] == request["system_id"]), {})
    extensions = {str(e).lower().lstrip(".") for e in system.get("extensions", [])}
    if "m3u" not in extensions:
        raise ValueError("O catálogo da plataforma não admite M3U")
    tracks = api["media_session"]._track_paths(root)
    paths, inputs = [], {}
    for number, item in enumerate(discs, 1):
        if type(item.get("disc_number")) is not int or item["disc_number"] != number or not item.get("identity_evidence"):
            raise ValueError("A ordem deve ser contínua e cada disco exige identidade comprovada")
        path = _path(root, item.get("path"))
        if (path in tracks or not path.is_file() or path.suffix.lower().lstrip(".") not in extensions
                or path.suffix.lower() in {".m3u", ".zip", ".7z", ".rar"}):
            raise ValueError("O membro deve ser uma entrada direta suportada pelo catálogo")
        if item["path"].casefold() in {value.casefold() for value in paths} or item["path"].startswith("#"):
            raise ValueError("Disco duplicado ou nome interpretável como comentário M3U")
        paths.append(item["path"])
        members = [path]
        if path.suffix.lower() in {".cue", ".gdi"}:
            check = api["verify_cue"](path) if path.suffix.lower() == ".cue" else api["verify_gdi"](path)
            if check["status"] != "valid":
                raise ValueError("Descritor contém referências inválidas")
            for reference in check.get("references", check.get("tracks", [])):
                member = path.parent / reference["name"]
                relative = os.path.relpath(member, root)
                members.append(_path(root, relative))
        for member in members:
            relative = member.relative_to(root).as_posix()
            if relative not in inputs:
                snapshot = api["path_snapshot"](member, include_hash=True)
                if snapshot["kind"] != "file" or snapshot["size_bytes"] <= 0:
                    raise ValueError("Disco ou trilha vazia")
                inputs[relative] = {"path": relative, "size_bytes": snapshot["size_bytes"], "sha256": snapshot["sha256"]}
    _check_duplicate_playlist(root, target, paths, api)
    payload = "\n".join(paths) + "\n"
    operation_id = "disc-set-" + uuid.uuid4().hex
    api["operation_dir"](operation_id).mkdir(parents=True, exist_ok=False)
    manifest = {"schema_version": 1, "kind": "disc_set_playlist", "operation_id": operation_id,
                "status": "prepared", "created_at": api["now"](), "history_index_refresh": "deferred",
                "system_id": request["system_id"],
                "playlist": filename, "discs": discs, "inputs": list(inputs.values()), "payload": payload,
                "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "identity_evidence": request["identity_evidence"], "runtime_evidence": request["runtime_evidence"],
                "functional_validation": "native_launch_disc_swap_saves_and_return_pending",
                "policy": {"permanent_delete": False, "roms_moved": False, "frontend_configuration_changed": False}}
    _save(manifest, api)
    api["append_event"](operation_id, "disc_set_planned", action="m3u_create", status="prepared",
                        destination=str(target), details={"disc_count": len(discs), "input_files": len(inputs)})
    return manifest


def verify(manifest, api):
    root = _inputs(manifest, api)
    target = _playlist(root, manifest["playlist"])
    if not target.is_file() or api["sha256_file"](target) != manifest["sha256"]:
        raise ValueError("Playlist ausente ou divergente")
    result = api["verify_m3u"](target)
    if result["status"] != "valid" or result["reference_count"] != len(manifest["discs"]):
        raise ValueError("Referências da playlist inválidas")
    return {"status": "verified", "operation_id": manifest["operation_id"], "references": result["reference_count"],
            "functional_validation": manifest["functional_validation"]}


def apply(manifest, confirmed, api):
    if not confirmed or manifest["status"] not in {"prepared", "applying"}:
        raise ValueError("Aplicação exige plano preparado e --apply")
    if any(api["frontend_processes_running"]().values()):
        raise ValueError("Feche os frontends antes de publicar a playlist")
    root = _inputs(manifest, api)
    target = _playlist(root, manifest["playlist"])
    if os.path.lexists(target) and (manifest["status"] != "applying" or not manifest.get("create_intent")):
        raise FileExistsError("Playlist existente preservada")
    expected_payload = "\n".join(item["path"] for item in manifest["discs"]) + "\n"
    if (manifest["payload"] != expected_payload
            or hashlib.sha256(expected_payload.encode()).hexdigest() != manifest["sha256"]):
        raise ValueError("Payload da playlist diverge da seleção revisada")
    _check_duplicate_playlist(root, target, [item["path"] for item in manifest["discs"]], api)
    manifest.update(status="applying", create_intent=True)
    _save(manifest, api)
    if not os.path.lexists(target):
        temporary = root / ("." + uuid.uuid4().hex + ".m3u.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(manifest["payload"].encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
            _sync(root)
        finally:
            temporary.unlink(missing_ok=True)
    result = verify(manifest, api)
    _sync(root)
    manifest.update(status="completed_verified", completed_at=api["now"]())
    _save(manifest, api)
    api["append_event"](manifest["operation_id"], "disc_set_completed", action="m3u_create", status=manifest["status"],
                        destination=str(target), sha256_after=manifest["sha256"])
    return result


def rollback(manifest, confirmed, api):
    if not confirmed or manifest["status"] not in {"completed_verified", "applying", "rolling_back"}:
        raise ValueError("Reversão exige operação aplicada e --rollback")
    if any(api["frontend_processes_running"]().values()):
        raise ValueError("Feche os frontends antes de reverter a playlist")
    root = _root(manifest["system_id"], api)
    target = _playlist(root, manifest["playlist"])
    retained = api["operation_dir"](manifest["operation_id"]) / "rollback-playlist.m3u"
    if manifest["status"] != "rolling_back":
        verify(manifest, api)
        if os.path.lexists(retained):
            raise FileExistsError("Destino de preservação ocupado")
        manifest.update(status="rolling_back", rollback_intent=True)
        _save(manifest, api)
    if os.path.lexists(target):
        if os.path.lexists(retained) or api["sha256_file"](target) != manifest["sha256"]:
            raise ValueError("Playlist alterada ou destino de reversão ocupado")
        os.rename(target, retained)
    if not retained.is_file() or retained.is_symlink() or api["sha256_file"](retained) != manifest["sha256"]:
        raise ValueError("Playlist preservada ausente ou divergente")
    _sync(root)
    _sync(retained.parent)
    manifest.update(status="rolled_back", rolled_back_at=api["now"]())
    _save(manifest, api)
    api["append_event"](manifest["operation_id"], "disc_set_rolled_back", action="m3u_rollback", status="rolled_back",
                        source=str(target), destination=str(retained))
    return {"status": "rolled_back", "operation_id": manifest["operation_id"]}


def dispatch(args, api):
    if args.discs_command == "plan":
        return plan(json.loads(Path(args.file).read_text()), api)
    if args.discs_command == "verify":
        return verify(_load(args.operation_id, api), api)
    with api["media_session"].write_lock(api):
        manifest = _load(args.operation_id, api)
        if args.discs_command == "apply":
            return apply(manifest, args.apply, api)
        return rollback(manifest, args.rollback, api)
