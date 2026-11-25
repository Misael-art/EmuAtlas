"""Selected raw CD sets, lossless per-track proof and reversible publication."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET

try:
    from . import disc_sets, runtime_install
except ImportError:
    import disc_sets
    import runtime_install


SECTOR_BYTES = 2352


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _cookie(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _frames(value):
    minutes, seconds, frames = map(int, value.split(":"))
    if seconds >= 60 or frames >= 75:
        raise ValueError("Índice CUE fora da base de 75 quadros por segundo")
    return (minutes * 60 + seconds) * 75 + frames


def inspect_cue(cue, api):
    """Digest all raw sectors once, retaining physical pregaps and track boundaries."""
    api["reject_symlink_components"](cue)
    if not cue.is_file() or cue.stat().st_size > 1024 * 1024:
        raise ValueError("Descritor CUE ausente ou acima do limite de revisão")
    before = _cookie(cue)
    raw = cue.read_bytes()
    files, tracks = [], []
    current_file = current_track = None
    for line in raw.decode("utf-8-sig").splitlines():
        value = line.strip()
        if not value or value.upper().startswith("REM "):
            continue
        match = re.fullmatch(r'FILE\s+"([^"\r\n]+)"\s+BINARY', value, re.I)
        if match:
            name = match[1]
            path = disc_sets._path(cue.parent, name)
            if (not path.is_file() or path == cue or path.suffix.lower() != ".bin"
                    or name.casefold() in {item["path"].casefold() for item in files}):
                raise ValueError("CUE exige arquivos BIN físicos, únicos e completos")
            current_file = {"path": name, "tracks": []}
            files.append(current_file)
            current_track = None
            continue
        match = re.fullmatch(r"TRACK\s+(\d{2})\s+(MODE1/2352|MODE2/2352|AUDIO)", value, re.I)
        if match and current_file is not None:
            number = int(match[1])
            if number != len(tracks) + 1 or number > 99:
                raise ValueError("A ordem das faixas CUE deve ser contínua")
            current_track = {"number": number, "mode": match[2].upper(), "indices": {}}
            tracks.append(current_track)
            current_file["tracks"].append(current_track)
            continue
        match = re.fullmatch(r"INDEX\s+(00|01)\s+(\d{2,3}:\d{2}:\d{2})", value, re.I)
        if match and current_track is not None:
            key = match[1]
            if key in current_track["indices"]:
                raise ValueError("Índice CUE duplicado")
            current_track["indices"][key] = _frames(match[2])
            continue
        raise ValueError("Diretiva CUE sem perfil aprovado: " + value)
    if not files or not tracks or _cookie(cue) != before:
        raise ValueError("CUE vazio ou alterado durante a leitura")
    proof = []
    inputs = [{"path": cue.name, "size_bytes": len(raw), "sha256": _digest(raw)}]
    for item in files:
        path = disc_sets._path(cue.parent, item["path"])
        initial = _cookie(path)
        size = initial[2]
        if not size or size % SECTOR_BYTES or not item["tracks"]:
            raise ValueError("BIN vazio, desalinhado ou sem faixas")
        starts = []
        for track in item["tracks"]:
            indices = track["indices"]
            if "01" not in indices or indices.get("00", indices["01"]) > indices["01"]:
                raise ValueError("INDEX 01 ausente ou pregap incoerente")
            starts.append(indices.get("00", indices["01"]))
        ends = starts[1:] + [size // SECTOR_BYTES]
        if starts[0] != 0:
            raise ValueError("O perfil exige cobertura integral dos setores do BIN")
        full = hashlib.sha256()
        with path.open("rb") as stream:
            for track, start, end in zip(item["tracks"], starts, ends):
                if not start <= track["indices"]["01"] < end:
                    raise ValueError("Extensão de faixa CUE fora do BIN")
                remaining = (end - start) * SECTOR_BYTES
                digest = hashlib.sha256()
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("BIN truncado durante a validação")
                    full.update(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                proof.append({"number": track["number"], "mode": track["mode"],
                              "frames": end - start,
                              "index_01": track["indices"]["01"] - start,
                              "sha256": digest.hexdigest()})
            if stream.read(1):
                raise ValueError("Setores BIN adicionais sem faixa")
        if _cookie(path) != initial:
            raise ValueError("BIN alterado durante a validação")
        inputs.append({"path": item["path"], "size_bytes": size, "sha256": full.hexdigest()})
    return {"tracks": proof, "inputs": inputs}


def _profile(api, profile_id):
    path = api["TOOL_ROOT"] / "config/compression-profiles.json"
    profiles = json.loads(path.read_text()).get("cd_profiles", [])
    matches = [row for row in profiles if row["id"] == profile_id]
    if len(matches) != 1:
        raise ValueError("Perfil CD ausente ou ambíguo")
    return matches[0]


def _runtime(root, system, profile):
    try:
        from . import media_stray
    except ImportError:
        import media_stray
    configured = media_stray._runtime_records(root, system)
    supported = []
    for record in configured:
        extensions = record.get("extensions", [])
        if isinstance(extensions, str):
            extensions = extensions.split(",")
        if (record.get("runtime_candidate", "").casefold() == profile["runtime"].casefold()
                and {"cue", "chd"}.issubset({e.strip().lower().lstrip(".") for e in extensions})):
            supported.append({"runtime": record["runtime_candidate"],
                              "command": record.get("command", ""), "extensions": sorted(extensions)})
    if not supported:
        raise ValueError("O perfil host deve comprovar CUE e CHD no runtime aprovado")
    return supported


def _gamelist_path(value):
    parts = [part for part in value.strip().replace("\\", "/").split("/") if part not in {"", "."}]
    if parts and parts[0] == "roms":
        parts = parts[1:]
    return "/".join(parts)


def _game_metadata(game):
    def signature(node):
        return (node.tag, tuple(sorted(node.attrib.items())), node.text or "",
                node.tail.strip() if node.tail else "", tuple(signature(child) for child in node))

    ignored = {"path", "hidden", "nomultiscrape", "nogamecount"}
    result = {"@attributes": tuple(sorted(game.attrib.items()))}
    for node in game:
        if node.tag in ignored:
            continue
        if node.tag in result:
            raise ValueError("Metadado repetido na gamelist exige revisão")
        result[node.tag] = signature(node)
    return result


def _retire_track_metadata(before, after, source, destination, tracks, reference_file):
    """Keep one game entry and retain retired raw records in the operation proof."""
    pattern = re.compile(rb"<game\b[^>]*>.*?</game\s*>", re.I | re.S)
    blocks = list(pattern.finditer(before))
    parsed = ET.fromstring(before)
    if len(blocks) != len(parsed.findall("game")):
        raise ValueError("Estrutura de gamelist exige revisão antes de consolidar faixas")
    games = [ET.fromstring(block.group()) for block in blocks]
    paths = [_gamelist_path(game.findtext("path") or "") for game in games]
    dependent = [index for index, path in enumerate(paths) if path in tracks]
    affected_paths = [node for node in parsed.iter("path") if _gamelist_path(node.text or "") in tracks]
    if len(affected_paths) != len(dependent):
        raise ValueError("Referência de faixa fora de uma entrada de jogo suportada")
    if not dependent:
        return after, []
    if len({paths[index] for index in dependent}) != len(dependent):
        raise ValueError("A gamelist repete uma faixa dependente")
    primary = [index for index, path in enumerate(paths) if path in {source, destination}]
    if len(primary) > 1:
        raise ValueError("Mais de uma entrada principal na gamelist do CD")
    promoted = not primary
    primary_index = primary[0] if primary else max(dependent, key=lambda index: len(_game_metadata(games[index])))
    metadata = _game_metadata(games[primary_index])
    for index in dependent:
        if any(len(games[index].findall(tag)) > 1 for tag in ("hidden", "nomultiscrape", "nogamecount")):
            raise ValueError("Sinalizadores repetidos na faixa exigem revisão")
        flags = [games[index].findtext(tag) for tag in ("hidden", "nomultiscrape", "nogamecount")]
        if any(value is not None for value in flags) and flags != ["true", "true", "true"]:
            raise ValueError("Sinalizadores pessoais da faixa exigem revisão")
        conflicting = [key for key, value in _game_metadata(games[index]).items() if metadata.get(key) != value]
        if conflicting:
            raise ValueError("Metadados da faixa diferem da entrada principal; preservar e revisar "
                             + reference_file + ": " + ", ".join(conflicting))
    retired = [{"reference_file": reference_file, "path": paths[index],
                "action": "promote_common_metadata_to_primary" if promoted and index == primary_index else "retire_dependent_entry",
                "raw_xml": blocks[index].group().decode("utf-8")} for index in dependent]
    remove = set(dependent) - {primary_index}
    managed_flags = promoted and all(games[primary_index].findtext(tag) == "true"
                                     for tag in ("hidden", "nomultiscrape", "nogamecount"))
    index = -1

    def replace(match):
        nonlocal index
        index += 1
        if index in remove:
            return b""
        block = match.group()
        if managed_flags and index == primary_index:
            for tag in ("hidden", "nomultiscrape", "nogamecount"):
                block = re.sub(rb"<" + tag.encode() + rb">\s*true\s*</" + tag.encode() + rb">", b"", block)
        return block

    if len(list(pattern.finditer(after))) != len(blocks):
        raise ValueError("Reescrita da gamelist mudou sua estrutura")
    result = pattern.sub(replace, after)
    if any(_gamelist_path(node.text or "") in tracks for node in ET.fromstring(result).iter("path")):
        raise ValueError("Referência de faixa fora de uma entrada de jogo suportada")
    return result, retired


def _references(cue, inputs, destination, system, api):
    root = api["PROJECT_ROOT"]
    platform = root / "roms" / system
    source_paths = {row["path"]: (cue.parent / row["path"]).relative_to(platform).as_posix() for row in inputs}
    destination_relative = destination.relative_to(platform).as_posix()
    updates, blockers, exclusions = api["variant_organization"]._path_refs_for_moves(
        {name: destination_relative for name in source_paths}, system, root,
        root / "frontend/ES-DE/UserData", source_paths)
    retired = []
    track_paths = set(source_paths.values()) - {cue.relative_to(platform).as_posix()}
    for item in updates:
        after, removed = _retire_track_metadata(item["before_bytes"], item["after_bytes"],
                                               cue.relative_to(platform).as_posix(), destination_relative,
                                               track_paths, item["path"])
        item.update(after_bytes=after, sha256_after=_digest(after), size_after=len(after))
        retired.extend(removed)
    members = {cue.parent / row["path"] for row in inputs}
    playlists = set()
    for descriptor in sorted(platform.rglob("*")):
        if descriptor == cue or descriptor.suffix.lower() not in {".cue", ".gdi", ".m3u", ".m3u8"}:
            continue
        api["reject_symlink_components"](descriptor)
        if descriptor.stat().st_size > 1024 * 1024:
            raise ValueError("Descritor excede o limite de revisão: " + str(descriptor))
        if descriptor.suffix.lower() in {".cue", ".gdi"}:
            check = api["verify_cue"](descriptor) if descriptor.suffix.lower() == ".cue" else api["verify_gdi"](descriptor)
            references = check.get("references", check.get("tracks", []))
            if any((descriptor.parent / ref["name"]).resolve() in members for ref in references):
                raise ValueError("Trilha compartilhada com outro descritor: " + str(descriptor))
            continue
        before = descriptor.read_bytes()
        after_lines = []
        changed = False
        for line in before.decode("utf-8-sig").splitlines(keepends=True):
            name = line.strip()
            resolved = (descriptor.parent / name.replace("\\", "/")).resolve()
            if name and not name.startswith("#") and resolved in members:
                if resolved != cue:
                    raise ValueError("Uma playlist referencia diretamente uma faixa BIN")
                newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                line = os.path.relpath(destination, descriptor.parent).replace(os.sep, "/") + newline
                changed = True
            after_lines.append(line)
        if changed:
            playlists.add(str(descriptor))
            after = "".join(after_lines).encode("utf-8")
            updates.append({"path": str(descriptor), "before_bytes": before, "after_bytes": after,
                            "sha256_before": _digest(before), "sha256_after": _digest(after)})
    known = {item["path"] for item in updates} | playlists | {str(cue)}
    known |= {item["path"] for item in exclusions}
    check = api["compression_reference_index"](list(members), root / "roms")
    for item in check.values():
        if item["status"] not in {"checked_none", "referenced"}:
            blockers.append({"reason": "Busca de referências incompleta", "detail": item})
        for path in item["path_matches"]:
            if path not in known:
                blockers.append({"path": path, "reason": "Referência exige regra de atualização explícita"})
    if blockers:
        raise ValueError("Referências CD pendentes: " + json.dumps(blockers, ensure_ascii=False))
    return updates, exclusions, retired


def _save(manifest, api):
    api["atomic_json"](api["operation_dir"](manifest["operation_id"]) / "manifest.json", manifest)


def _load(operation_id, api):
    if not re.fullmatch(r"compact-cd-[a-f0-9]{32}", operation_id):
        raise ValueError("ID de compactação CD inválido")
    path = api["operation_dir"](operation_id) / "manifest.json"
    api["reject_symlink_components"](path)
    manifest = json.loads(path.read_text())
    if manifest.get("kind") != "cd_lossless_compression" or manifest["operation_id"] != operation_id:
        raise ValueError("Manifesto CD incompatível")
    return manifest


def plan(system, source, profile_id, api):
    profile = _profile(api, profile_id)
    if system not in profile["systems"]:
        raise ValueError("Plataforma fora do perfil CD")
    platform = disc_sets._root(system, api)
    cue = disc_sets._path(platform, source)
    if cue.suffix.lower() != ".cue":
        raise ValueError("Selecione o descritor CUE completo")
    destination = cue.with_suffix(".chd")
    if any(p.name.casefold() == destination.name.casefold() for p in destination.parent.iterdir()):
        raise FileExistsError("Destino CHD existente preservado")
    proof = inspect_cue(cue, api)
    runtime = _runtime(api["PROJECT_ROOT"], system, profile)
    executable = shutil.which(profile["tool_executable"])
    if not executable:
        raise FileNotFoundError("Conversor CD ausente: " + profile["tool_executable"])
    tool = Path(executable).resolve()
    updates, exclusions, retired = _references(cue, proof["inputs"], destination, system, api)
    # Same-basename subchannel files remain beside the CHD, with their bytes guarded.
    sidecars = []
    for path in sorted(cue.parent.iterdir()):
        if path.stem.casefold() == cue.stem.casefold() and path.suffix.lower() in {".sbi", ".sub", ".ccd", ".toc"}:
            if path.suffix.lower() != ".sbi":
                raise ValueError("Conjunto tem suporte adicional sem perfil aprovado: " + path.name)
            api["reject_symlink_components"](path)
            sidecars.append({"path": path.relative_to(api["PROJECT_ROOT"]).as_posix(),
                             "sha256": api["sha256_file"](path), "size_bytes": path.stat().st_size})
    operation_id = "compact-cd-" + uuid.uuid4().hex
    opdir = api["operation_dir"](operation_id)
    opdir.mkdir(parents=True, exist_ok=False)
    for row in updates:
        relative = Path(row["path"]).relative_to(api["PROJECT_ROOT"])
        for folder, content in (("rollback/files", row.pop("before_bytes")), ("proposed", row.pop("after_bytes"))):
            api["atomic_write_bytes"](opdir / folder / relative, content)
        row["path"] = relative.as_posix()
        row["mode"] = (api["PROJECT_ROOT"] / relative).stat().st_mode & 0o777
    manifest = {"schema_version": 1, "kind": "cd_lossless_compression", "operation_id": operation_id,
                "status": "prepared", "created_at": api["now"](), "system_id": system,
                "source": cue.relative_to(api["PROJECT_ROOT"]).as_posix(),
                "destination": destination.relative_to(api["PROJECT_ROOT"]).as_posix(),
                "profile": profile, "runtime": runtime, "tool": str(tool),
                "tool_sha256": api["sha256_file"](tool), "source_proof": proof,
                "sidecars": sidecars, "edits": updates, "reference_exclusions": exclusions,
                "retired_track_metadata": retired,
                "source_size_bytes": sum(item["size_bytes"] for item in proof["inputs"]),
                "history_index_refresh": "deferred", "originals_preserved": True,
                "functional_validation": "native_gameplay_disc_swap_saves_and_return_pending"}
    _save(manifest, api)
    api["append_event"](operation_id, "cd_compression_planned", action="compact_cd_plan", status="prepared",
                        source=str(cue), destination=str(destination), details={"tracks": len(proof["tracks"])})
    return manifest


def _guards(manifest, api):
    root = api["PROJECT_ROOT"]
    if (_profile(api, manifest["profile"]["id"]) != manifest["profile"]
            or _runtime(root, manifest["system_id"], manifest["profile"]) != manifest["runtime"]):
        raise ValueError("Perfil CD ou runtime mudou desde o plano")
    for row in manifest["sidecars"]:
        path = runtime_install._library_target(root, row["path"])
        if not path.is_file() or path.stat().st_size != row["size_bytes"] or api["sha256_file"](path) != row["sha256"]:
            raise ValueError("Suporte SBI mudou desde o plano")


def _run(manifest, args, log, api):
    tool = Path(manifest["tool"])
    if api["sha256_file"](tool) != manifest["tool_sha256"]:
        raise ValueError("Conversor mudou desde o plano")
    with log.open("ab") as stream:
        result = subprocess.run([str(tool), *args], stdout=stream, stderr=subprocess.STDOUT,
                                timeout=manifest["profile"]["timeout_seconds"], check=False)
    if result.returncode:
        raise ValueError("Conversor CD falhou; consulte " + str(log))


def _roundtrip(manifest, candidate, api):
    opdir = api["operation_dir"](manifest["operation_id"])
    api["reject_symlink_components"](candidate)
    before = _cookie(candidate)
    _run(manifest, ["verify", "-i", str(candidate)], opdir / "verify.log", api)
    with tempfile.TemporaryDirectory(prefix="roundtrip-", dir=opdir) as temporary:
        folder = Path(temporary)
        cue = folder / "roundtrip.cue"
        _run(manifest, ["extractcd", "-i", str(candidate), "-o", str(cue),
                        "-ob", str(folder / "track-%t.bin"), "-sb"], opdir / "roundtrip.log", api)
        actual = inspect_cue(cue, api)
    if actual["tracks"] != manifest["source_proof"]["tracks"] or _cookie(candidate) != before:
        raise ValueError("CD reconstruído diverge dos setores, modos, limites ou pregaps originais")
    return {"validation": "chdman_verify_and_full_raw_track_roundtrip",
            "tracks": actual["tracks"], "size_bytes": candidate.stat().st_size,
            "sha256": api["sha256_file"](candidate)}


def _publication(manifest, candidate, api):
    root = api["PROJECT_ROOT"]
    opdir = api["operation_dir"](manifest["operation_id"])
    child_id = "emulator-library-cd-" + manifest["operation_id"].removeprefix("compact-cd-")
    child_dir = api["operation_dir"](child_id)
    child_dir.mkdir(parents=True, exist_ok=True)
    moves = []
    source = root / manifest["source"]
    for row in manifest["source_proof"]["inputs"]:
        path = source.parent / row["path"]
        retained = opdir / "rollback/originals" / row["path"]
        inventory = runtime_install._library_tree(path)
        if len(inventory) != 1 or any(inventory[0][key] != row[key] for key in ("size_bytes", "sha256")):
            raise ValueError("Original mudou antes da publicação CD")
        moves.append({"source": path.relative_to(root).as_posix(),
                      "destination": retained.relative_to(root).as_posix(),
                      "files": inventory})
    inventory = runtime_install._library_tree(candidate)
    if len(inventory) != 1 or any(inventory[0][key] != manifest["output_proof"][key] for key in ("size_bytes", "sha256")):
        raise ValueError("Candidato mudou antes da publicação CD")
    moves.append({"source": candidate.relative_to(root).as_posix(), "destination": manifest["destination"],
                  "files": inventory})
    for edit in manifest["edits"]:
        for folder in ("rollback/files", "proposed"):
            api["atomic_write_bytes"](child_dir / folder / edit["path"], (opdir / folder / edit["path"]).read_bytes())
    child = {"schema_version": 1, "kind": "emulator_library_migration", "operation_id": child_id,
             "status": "prepared", "created_at": api["now"](), "scope": "selected_lossless_cd_set",
             "parent_operation_id": manifest["operation_id"], "durable_moves": True,
             "moves": moves, "edits": manifest["edits"], "history_index_deferred": True}
    api["atomic_json"](child_dir / "manifest.json", child)
    manifest["publication_operation_id"] = child_id
    _save(manifest, api)
    return child_id


def verify(manifest, api):
    if not manifest.get("publication_operation_id"):
        raise ValueError("CD ainda não publicado")
    _guards(manifest, api)
    runtime_install.library_execute(api["PROJECT_ROOT"], manifest["publication_operation_id"], "verify")
    proof = _roundtrip(manifest, api["PROJECT_ROOT"] / manifest["destination"], api)
    if proof != manifest["output_proof"]:
        raise ValueError("CHD publicado mudou")
    return {"status": "verified", "operation_id": manifest["operation_id"], **proof,
            "originals_preserved": True, "functional_validation": manifest["functional_validation"]}


def _catalog_items(manifest, api):
    root = api["PROJECT_ROOT"]
    opdir = api["operation_dir"](manifest["operation_id"])
    source = root / manifest["source"]
    items = [{"item_id": "cd-image", "system_id": manifest["system_id"],
              "classification": "rom", "status": "completed_verified", "source": str(source),
              "destination": str(root / manifest["destination"]), "after": manifest["output_proof"]}]
    for index, row in enumerate(manifest["source_proof"]["inputs"][1:], 1):
        items.append({"item_id": "preserved-track-" + str(index), "system_id": manifest["system_id"],
                      "classification": "rom_track_preserved_rollback", "status": "completed_verified",
                      "source": str(source.parent / row["path"]),
                      "destination": str(opdir / "rollback/originals" / row["path"]), "after": row})
    return items


def apply(manifest, confirmed, api):
    if not confirmed or manifest["status"] not in {"prepared", "converting", "failed", "publishing", "completed_verified"}:
        raise ValueError("Aplicação CD exige plano atual e --apply")
    root = api["PROJECT_ROOT"]
    runtime_install._library_guard(root)
    _guards(manifest, api)
    if manifest["status"] == "completed_verified":
        result = verify(manifest, api)
        if "items" not in manifest:
            manifest["items"] = _catalog_items(manifest, api)
            _save(manifest, api)
        return result
    opdir = api["operation_dir"](manifest["operation_id"])
    candidate = opdir / "candidate.chd"
    if not manifest.get("publication_operation_id"):
        source = runtime_install._library_target(root, manifest["source"])
        destination = runtime_install._library_target(root, manifest["destination"])
        if any(p.name.casefold() == destination.name.casefold() for p in destination.parent.iterdir()):
            raise FileExistsError("Destino CHD existente preservado")
        if inspect_cue(source, api) != manifest["source_proof"]:
            raise ValueError("Conjunto CD mudou desde o plano")
        updates, exclusions, retired = _references(source, manifest["source_proof"]["inputs"],
                                                   root / manifest["destination"], manifest["system_id"], api)
        current = [{"path": Path(row["path"]).relative_to(root).as_posix(),
                    "sha256_before": row["sha256_before"], "sha256_after": row["sha256_after"]} for row in updates]
        expected = [{key: row[key] for key in ("path", "sha256_before", "sha256_after")} for row in manifest["edits"]]
        if (current != expected or exclusions != manifest["reference_exclusions"]
                or retired != manifest.get("retired_track_metadata", [])):
            raise ValueError("Referências CD mudaram desde o plano")
        if shutil.disk_usage(opdir).free < 2 * manifest["source_size_bytes"] + 10 * 1024**3:
            raise ValueError("Reserva insuficiente para candidato e roundtrip CD")
        if os.path.lexists(candidate) and not manifest.get("create_intent"):
            raise FileExistsError("Candidato sem intenção durável preservado")
        manifest.update(status="converting", create_intent=True)
        _save(manifest, api)
        try:
            if not candidate.exists():
                _run(manifest, ["createcd", "-i", str(source), "-o", str(candidate), "-np", "1",
                                "-c", manifest["profile"]["codecs"], "-hs", str(manifest["profile"]["hunk_bytes"])],
                     opdir / "convert.log", api)
            proof = _roundtrip(manifest, candidate, api)
            if inspect_cue(source, api) != manifest["source_proof"]:
                raise ValueError("Originais mudaram durante a conversão CD")
            manifest["output_proof"] = proof
            gain = manifest["source_size_bytes"] - proof["size_bytes"]
            manifest.update(active_library_reduction_bytes=gain,
                            net_disk_freed_bytes=-proof["size_bytes"])
            if gain < max(manifest["profile"]["minimum_gain_bytes"],
                          manifest["source_size_bytes"] * manifest["profile"]["minimum_gain_ratio"]):
                manifest.update(status="no_gain_original_preserved")
                _save(manifest, api)
                return {"status": manifest["status"], "operation_id": manifest["operation_id"]}
            runtime_install._sync_library_copy(candidate, runtime_install._library_tree(candidate))
            _publication(manifest, candidate, api)
        except Exception:
            manifest["status"] = "failed"
            _save(manifest, api)
            raise
    manifest["status"] = "publishing"
    _save(manifest, api)
    child_id = manifest["publication_operation_id"]
    child = json.loads((api["operation_dir"](child_id) / "manifest.json").read_text())
    if child["status"] not in {"applied", "completed_verified"}:
        runtime_install.library_execute(root, child_id, "apply", True)
    result = verify(manifest, api)
    manifest["items"] = _catalog_items(manifest, api)
    manifest.update(status="completed_verified", completed_at=api["now"]())
    _save(manifest, api)
    api["append_event"](manifest["operation_id"], "cd_compression_completed", action="compact_cd_apply",
                        status=manifest["status"], details={"publication_operation_id": child_id,
                        "active_library_reduction_bytes": manifest["active_library_reduction_bytes"],
                        "net_disk_freed_bytes": manifest["net_disk_freed_bytes"]})
    return result


def rollback(manifest, confirmed, api):
    if not confirmed or not manifest.get("publication_operation_id"):
        raise ValueError("Reversão CD exige publicação e --rollback")
    child = json.loads((api["operation_dir"](manifest["publication_operation_id"]) / "manifest.json").read_text())
    if child["status"] != "rolled_back":
        runtime_install.library_execute(api["PROJECT_ROOT"], manifest["publication_operation_id"], "rollback", True)
    source = api["PROJECT_ROOT"] / manifest["source"]
    if inspect_cue(source, api) != manifest["source_proof"]:
        raise ValueError("Conjunto CD restaurado diverge do original")
    for item in manifest.get("items", []):
        item["status"] = "rolled_back"
    manifest.update(status="rolled_back", rolled_back_at=api["now"]())
    _save(manifest, api)
    api["append_event"](manifest["operation_id"], "cd_compression_rolled_back", action="compact_cd_rollback", status="rolled_back")
    return {"status": "rolled_back", "operation_id": manifest["operation_id"]}


def dispatch(args, api):
    if args.cd_command == "plan":
        return plan(args.system_id, args.source, args.profile, api)
    manifest = _load(args.operation_id, api)
    if args.cd_command == "verify":
        return verify(manifest, api)
    with api["media_session"].write_lock(api):
        manifest = _load(args.operation_id, api)
        if args.cd_command == "apply":
            return apply(manifest, args.apply, api)
        return rollback(manifest, args.rollback, api)
