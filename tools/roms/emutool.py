#!/usr/bin/env python3
"""Audit host emulator profiles and apply declared first-run defaults.

The platform catalog and host-linux.json remain the only platform/profile
sources. The default audit is read-only. The explicit onboarding action writes
only a missing, profile-declared configuration default, preserves an existing
choice, and records a rollback snapshot in the official operation history. It
does not install software, inspect archive members, or launch games.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import romtool
import runtime_install


VERSION = "1.5.0"


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (
            (candidate / "frontend/RetroFE/Config/platform-catalog.json").is_file()
            and (candidate / "frontend/RetroFE/Config/profiles/host-linux.json").is_file()
            and (candidate / "tools/roms/romtool.py").is_file()
        ):
            return candidate
    raise RuntimeError("Não encontrei a raiz portátil do projeto acima da ferramenta.")


ROOT = find_project_root()
RETROFE_ROOT = ROOT / "frontend/RetroFE"
CATALOG_PATH = RETROFE_ROOT / "Config/platform-catalog.json"
PROFILE_PATH = RETROFE_ROOT / "Config/profiles/host-linux.json"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Não consegui ler JSON válido em {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Esperava um objeto JSON em {path}.")
    return value


def resolve_path(value: str, env_name: str | None = None) -> Path:
    configured = os.environ.get(env_name, value) if env_name else value
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = RETROFE_ROOT / path
    return path.resolve()


def resolve_executable(value: str, root: Path = RETROFE_ROOT) -> str | None:
    if not value:
        return None
    configured = os.path.expandvars(os.path.expanduser(value))
    path = Path(configured)
    if path.is_absolute():
        candidate = path.resolve()
    elif "/" in configured:
        candidate = (root / path).resolve()
    else:
        return shutil.which(configured)
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _safe_userdata_file(relative: str) -> Path:
    path = Path(relative)
    userdata = ROOT / 'emulator/data'
    if not path.parts or path.is_absolute():
        raise ValueError('O caminho de estado deve ser relativo')
    # Old API callers may still provide the role prefix UserData; its physical
    # root is always emulator/data. Historical manifests are never rewritten.
    if path.parts[0].casefold() == 'userdata':
        if '..' in path.parts:
            raise ValueError('Traversal no caminho de estado')
        target = userdata.joinpath(*path.parts[1:])
    elif path.parts[:2] == ('emulator', 'data'):
        target = ROOT / path
    else:
        target = Path(os.path.abspath(RETROFE_ROOT / path))
    if not target.resolve(strict=False).is_relative_to(userdata.resolve()):
        raise ValueError('O caminho saiu de emulator/data')
    cursor = ROOT
    for part in target.relative_to(ROOT).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink em UserData: {cursor}")
    return target


def _profile_system_by_id(profile: dict, system_id: str) -> tuple[str, dict]:
    requested = system_id.strip().casefold()
    matches = [
        (name, entry) for name, entry in profile.get("collections", {}).items()
        if str(entry.get("rom_subdir", "")).casefold() == requested
    ]
    if len(matches) != 1:
        raise RuntimeError(f"system_id {system_id!r} precisa apontar a exatamente um perfil host; achei {len(matches)}.")
    return matches[0]


def _runtime_choices(system: dict) -> dict[str, dict]:
    default_id = str(system.get("runtime_id", "")).strip()
    options = system.get("runtime_options", {})
    if not default_id or not isinstance(options, dict):
        raise ValueError("O perfil não declara runtime_id e runtime_options válidos.")
    choices = {default_id: system}
    for runtime_id, entry in options.items():
        if not isinstance(runtime_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", runtime_id):
            raise ValueError(f"Identificador de runtime inválido: {runtime_id!r}")
        if runtime_id in choices or not isinstance(entry, dict):
            raise ValueError(f"Runtime duplicado ou perfil inválido: {runtime_id!r}")
        choices[runtime_id] = entry
    return choices


def _runtime_selection_path(system: dict) -> Path:
    relative = str(system.get("runtime_selection_path", ""))
    if not relative:
        raise ValueError("O perfil não declara runtime_selection_path.")
    return _safe_userdata_file(relative)


def _load_runtime_selections(system: dict) -> tuple[Path, dict, bytes | None]:
    target = _runtime_selection_path(system)
    before = target.read_bytes() if target.exists() else None
    if before is None:
        return target, {"schema_version": 1, "system_id": str(system["rom_subdir"]), "selections": {}}, None
    if len(before) > 1024 * 1024:
        raise ValueError("O arquivo de seleção de runtime excede 1 MiB.")
    try:
        payload = json.loads(before.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Arquivo de seleção de runtime inválido: {error}") from error
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1 or payload.get("system_id") != str(system["rom_subdir"])
            or not isinstance(payload.get("selections"), dict)):
        raise ValueError("Arquivo de seleção tem schema ou system_id inválido.")
    choices = _runtime_choices(system)
    preferred = payload.get('preferred_runtime_id',system['runtime_id'])
    if not isinstance(preferred,str) or preferred not in choices:
        raise ValueError('Runtime preferido desconhecido; configuração preservada.')
    identity = system.get("runtime_identity", {})
    pattern = str(identity.get("title_id_pattern", "")) if isinstance(identity, dict) else ""
    for title_id, runtime_id in payload["selections"].items():
        if (not isinstance(title_id, str) or not pattern or not re.fullmatch(pattern, title_id)
                or not isinstance(runtime_id, str) or runtime_id not in choices):
            raise ValueError("Arquivo de seleção contém Title ID ou runtime desconhecido.")
    return target, payload, before


def _ps5_game_identity(system: dict, item_value: str) -> tuple[Path, str, Path]:
    if system.get('rom_subdir') == 'switch2':
        import eden_runtime
        item = Path(item_value).expanduser()
        if not item.is_absolute():item=ROOT/item
        directory,reason,tid=eden_runtime.resolve_dump(item,ROOT/'roms/switch2')
        for name in ('main','main.npdm'):
            if (directory/name).is_symlink():raise ValueError('Dependência ExeFS por symlink recusada')
        return item.resolve(),f'{tid:016x}',directory/'main'
    raw = Path(item_value).expanduser()
    if ".." in raw.parts:
        raise ValueError("O caminho do jogo não pode conter '..'.")
    if not raw.is_absolute():
        raw = ROOT / raw
    raw = Path(os.path.abspath(raw))
    rom_root = ROOT / "roms" / str(system.get("rom_subdir", ""))
    if rom_root.is_symlink() or not rom_root.is_dir():
        raise ValueError(f"A raiz canônica da plataforma está ausente ou é symlink: {rom_root}")
    try:
        relative_item = raw.relative_to(rom_root)
    except ValueError as error:
        raise ValueError(f"O jogo precisa estar dentro de {rom_root}.") from error
    if not relative_item.parts:
        raise ValueError("Informe uma pasta de jogo, não a raiz da plataforma.")
    cursor = rom_root
    for part in relative_item.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink no caminho do jogo: {cursor}")
    item = raw.resolve(strict=True)
    if not item.is_dir() or not item.is_relative_to(rom_root.resolve()):
        raise ValueError(f"O item precisa ser uma pasta de jogo canônica abaixo de {rom_root}.")
    identity = system.get("runtime_identity")
    if not isinstance(identity, dict):
        raise ValueError("O perfil não declara a leitura de identidade interna do PS5.")

    def checked_relative(key: str) -> Path:
        relative = Path(str(identity.get(key, "")))
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"Caminho runtime_identity inválido: {key}.")
        current = item
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Componente symlink em {key}: {current}")
        return current

    metadata = checked_relative("metadata_path")
    eboot = checked_relative("executable_path")
    if not metadata.is_file() or metadata.stat().st_size > 1024 * 1024:
        raise ValueError("sce_sys/param.json ausente ou maior que 1 MiB.")
    if not eboot.is_file() or not eboot.resolve().is_relative_to(item):
        raise ValueError("eboot.bin ausente ou fora da pasta do jogo.")
    try:
        document = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Não consegui ler a identidade PS5: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("sce_sys/param.json não contém um objeto JSON.")
    title_id = str(document.get(str(identity.get("metadata_key", "TITLE_ID")), "")).strip()
    pattern = str(identity.get("title_id_pattern", ""))
    if not title_id or not pattern or not re.fullmatch(pattern, title_id):
        raise ValueError(f"TITLE_ID ausente ou inválido: {title_id!r}")
    return item, title_id, eboot.resolve()


def _probe_runtime_entry(runtime_id: str, system: dict, entry: dict) -> dict[str, object]:
    command_name = str(entry.get("probe_command", entry.get("command", "")))
    args = entry.get("probe_args", system.get("probe_args", []))
    executable = resolve_executable(command_name, RETROFE_ROOT) if command_name else None
    if not executable:
        return {"runtime_id": runtime_id, "available": False, "status": "executable_missing", "command": command_name}
    if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
        return {"runtime_id": runtime_id, "available": False, "status": "probe_not_configured", "command": executable}
    try:
        environment = romtool.host_runtime_probe_environment(entry, program_dir=Path(executable).parent)
        result = subprocess.run([executable, *args], capture_output=True, text=True, errors="replace",
                                 check=False, timeout=30, env=environment,
                                 cwd=romtool.host_runtime_probe_directory(entry))
    except subprocess.TimeoutExpired as error:
        return {"runtime_id": runtime_id, "available": False, "status": "probe_timed_out",
                "command": executable, "probe_args": args,
                "output": romtool.subprocess_output_text(error.stdout)[-1000:]}
    except (OSError, ValueError) as error:
        return {"runtime_id": runtime_id, "available": False, "status": "probe_failed",
                "command": executable, "error": f"{type(error).__name__}: {error}"}
    expected = int(entry.get("probe_expected_exit_code", system.get("probe_expected_exit_code", 0)))
    output = (result.stdout + result.stderr).strip()
    return {"runtime_id": runtime_id, "available": result.returncode == expected,
            "status": "runtime_ready_unverified" if result.returncode == expected else "probe_failed",
            "command": executable, "probe_args": args, "returncode": result.returncode,
            "expected_returncode": expected, "version_or_output": output[-1000:],
            "supported_inputs": entry.get("supported_inputs", system.get("launch_inputs", [])),
            "limitations": entry.get("limitations", [])}


def _record_file_change(change_type: str, reason: str, path: Path, before: bytes | None,
                        after: bytes, details: dict[str, object]) -> str:
    relative = str(path.relative_to(ROOT))
    before_hash = hashlib.sha256(before).hexdigest() if before is not None else ""
    after_hash = hashlib.sha256(after).hexdigest()
    record_details = {**details, "path": relative, "sha256_before": before_hash,
                      "sha256_after": after_hash, "size_before": len(before or b""),
                      "size_after": len(after), "file_was_absent": before is None,
                      "rollback_strategy": "restore the exact prior bytes; if absent, move/remove only while the after hash still matches"}
    operation = romtool.command_record_change(SimpleNamespace(
        change_type=change_type, reason=reason,
        details_json=json.dumps(record_details, ensure_ascii=False)))
    operation_id = str(operation["operation_id"])
    operation_path = romtool.operation_dir(operation_id)
    snapshot_relative = ""
    if before is not None:
        snapshot = operation_path / "rollback/files" / relative
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(before)
        snapshot_relative = str(snapshot.relative_to(romtool.PROJECT_ROOT))
    manifest_path = operation_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["details"]["rollback_snapshot"] = {
        "path": snapshot_relative, "sha256": before_hash, "size_bytes": len(before or b""),
        "file_was_absent": before is None, "after_sha256": after_hash,
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, manifest_path)
    romtool.append_event(operation_id, "emulator_project_file_changed", action=change_type,
                         status="completed", source=relative, destination=relative,
                         sha256_before=before_hash, sha256_after=after_hash,
                         size_before=len(before or b""), size_after=len(after),
                         details={"rollback_snapshot": manifest["details"]["rollback_snapshot"]})
    romtool.rebuild_history_index()
    return operation_id


def runtime_list(system_id: str, item_value: str | None = None) -> dict[str, object]:
    profile = read_json(PROFILE_PATH)
    collection, system = _profile_system_by_id(profile, system_id)
    choices = _runtime_choices(system)
    if system.get("runtime_selection_path"):
        target, document, _before = _load_runtime_selections(system)
        selection_file: str | None = str(target.relative_to(ROOT))
    else:
        if item_value:
            raise ValueError("A plataforma não declara seleção por jogo; remova --item.")
        document = {"selections": {}}
        selection_file = None
    selected_item: dict[str, object] | None = None
    if item_value:
        item, title_id, eboot = _ps5_game_identity(system, item_value)
        selected = document["selections"].get(title_id, document.get('preferred_runtime_id',str(system["runtime_id"])))
        selected_item = {"path": str(item.relative_to(ROOT)), "title_id": title_id,
                         "eboot": str(eboot.relative_to(ROOT)), "runtime_id": selected,
                         "runtime_name": choices[selected].get("display_name", selected)}
    probed = [_probe_runtime_entry(runtime_id, system, entry) for runtime_id, entry in choices.items()]
    return {"system_id": str(system["rom_subdir"]), "collection": collection,
            "default_runtime_id": str(system["runtime_id"]),
            "preferred_runtime_id":document.get('preferred_runtime_id',system['runtime_id']),
            "selection_file": selection_file,
            "selections": document["selections"], "item": selected_item,
            "runtimes": probed, "game_launch_tested": False}


def runtime_set(system_id: str, item_value: str, runtime_id: str) -> dict[str, object]:
    profile = read_json(PROFILE_PATH)
    collection, system = _profile_system_by_id(profile, system_id)
    choices = _runtime_choices(system)
    if runtime_id not in choices:
        raise ValueError(f"Runtime {runtime_id!r} não está declarado em host-linux.json; escolhas: {', '.join(choices)}")
    item, title_id, eboot = _ps5_game_identity(system, item_value)
    entry = choices[runtime_id]
    inputs = entry.get("supported_inputs", system.get("launch_inputs", []))
    if runtime_id == str(system.get("runtime_id")):
        inputs = ["directory" if value == "game_directory" else str(value).casefold().lstrip(".")
                  for value in system.get("launch_inputs", [])]
    wanted = 'exefs' if system_id=='switch2' else 'directory'
    if not isinstance(inputs, list) or wanted not in {str(value).casefold().lstrip(".") for value in inputs}:
        raise ValueError(f"{runtime_id} não declara suporte direto à entrada {wanted}.")
    probe = _probe_runtime_entry(runtime_id, system, entry)
    if not probe.get("available"):
        raise RuntimeError(f"Runtime não passou o probe antes da seleção: {json.dumps(probe, ensure_ascii=False)}")

    target, document, before = _load_runtime_selections(system)
    selections = dict(document["selections"])
    preferred=document.get('preferred_runtime_id',str(system['runtime_id']))
    previous = selections.get(title_id, preferred)
    if runtime_id == preferred:
        selections.pop(title_id, None)
    else:
        selections[title_id] = runtime_id
    if previous == runtime_id and ((runtime_id == preferred and title_id not in document["selections"])
                                   or selections.get(title_id) == runtime_id):
        return {"status": "unchanged", "system_id": str(system["rom_subdir"]),
                "title_id": title_id, "runtime_id": runtime_id,
                "runtime_name": entry.get("display_name", runtime_id), "path": str(item.relative_to(ROOT))}
    payload = {"schema_version": 1, "system_id": str(system["rom_subdir"]),
               **({'preferred_runtime_id':document['preferred_runtime_id']} if 'preferred_runtime_id' in document else {}),
               "selections": dict(sorted(selections.items()))}
    after = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    _atomic_write(target, after, mode)
    try:
        operation_id = _record_file_change(
            "emulator_runtime_selection",
            f"Selecionar runtime {system_id}/{runtime_id} para o Title ID {title_id}; a escolha é local e reversível.",
            target, before, after,
            {"system_id": str(system["rom_subdir"]), "collection": collection,
             "title_id": title_id, "previous_runtime_id": previous, "runtime_id": runtime_id,
             "runtime_probe": probe, "game_path": str(item.relative_to(ROOT)),
             "eboot_sha256": romtool.sha256_file(eboot),
             "rollback_scope": "restore selection JSON bytes; game payload is not modified"})
    except Exception:
        current = target.read_bytes() if target.exists() and not target.is_symlink() else b""
        if hashlib.sha256(current).hexdigest() == hashlib.sha256(after).hexdigest():
            if before is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, before, mode)
        raise
    return {"status": "updated", "operation_id": operation_id,
            "system_id": str(system["rom_subdir"]), "title_id": title_id,
            "previous_runtime_id": previous, "runtime_id": runtime_id,
            "runtime_name": entry.get("display_name", runtime_id),
            "path": str(item.relative_to(ROOT)), "selection_file": str(target.relative_to(ROOT)),
            "game_launch_tested": False}


def runtime_prefer(system_id, runtime_id, apply=False):
    collection,system=_profile_system_by_id(read_json(PROFILE_PATH),system_id)
    choices=_runtime_choices(system)
    if runtime_id not in choices:raise ValueError('Runtime não declarado no perfil da plataforma')
    probe=_probe_runtime_entry(runtime_id,system,choices[runtime_id])
    if not probe.get('available'):raise RuntimeError('Sonda do runtime falhou: '+json.dumps(probe,ensure_ascii=False))
    target,document,before=_load_runtime_selections(system)
    old=document.get('preferred_runtime_id',system['runtime_id'])
    if old==runtime_id:return {'status':'unchanged','runtime_id':runtime_id}
    if not apply:return {'status':'planned','system_id':system_id,'previous_runtime_id':old,'runtime_id':runtime_id,'probe':probe}
    document['preferred_runtime_id']=runtime_id
    after=(json.dumps(document,ensure_ascii=False,indent=2)+'\n').encode()
    _atomic_write(target,after,0o600)
    try:
        op=_record_file_change('emulator_runtime_preference','Selecionar runtime preferido sem alterar jogos e mantendo escolhas por Title ID.',target,before,after,
            {'system_id':system_id,'previous_runtime_id':old,'runtime_id':runtime_id,'probe':probe,'game_launch_tested':False})
    except Exception:
        if target.read_bytes()==after:
            if before is not None:_atomic_write(target,before,0o600)
            else:target.unlink(missing_ok=True)
        raise
    return {'status':'updated','operation_id':op,'runtime_id':runtime_id,'game_launch_tested':False}


def runtime_clear(system_id: str, item_value: str) -> dict[str, object]:
    profile = read_json(PROFILE_PATH)
    collection, system = _profile_system_by_id(profile, system_id)
    item, title_id, _eboot = _ps5_game_identity(system, item_value)
    target, document, before = _load_runtime_selections(system)
    selections = dict(document["selections"])
    previous = selections.pop(title_id, None)
    if previous is None:
        return {"status": "already_default", "system_id": str(system["rom_subdir"]),
                "title_id": title_id, "runtime_id": str(document.get("preferred_runtime_id",system["runtime_id"])),
                "path": str(item.relative_to(ROOT))}
    payload = {"schema_version": 1, "system_id": str(system["rom_subdir"]),
               **({'preferred_runtime_id':document['preferred_runtime_id']} if 'preferred_runtime_id' in document else {}),
               "selections": dict(sorted(selections.items()))}
    after = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    _atomic_write(target, after, mode)
    try:
        operation_id = _record_file_change(
            "emulator_runtime_selection",
            f"Restaurar o runtime preferido de {system_id} para o Title ID {title_id}.",
            target, before, after,
            {"system_id": str(system["rom_subdir"]), "collection": collection,
             "title_id": title_id, "previous_runtime_id": previous,
             "runtime_id": str(document.get("preferred_runtime_id",system["runtime_id"])), "game_path": str(item.relative_to(ROOT)),
             "rollback_scope": "restore selection JSON bytes; game payload is not modified"})
    except Exception:
        current = target.read_bytes() if target.exists() and not target.is_symlink() else b""
        if hashlib.sha256(current).hexdigest() == hashlib.sha256(after).hexdigest():
            if before is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, before, mode)
        raise
    return {"status": "updated", "operation_id": operation_id,
            "system_id": str(system["rom_subdir"]), "title_id": title_id,
            "previous_runtime_id": previous, "runtime_id": str(document.get("preferred_runtime_id",system["runtime_id"])),
            "path": str(item.relative_to(ROOT)), "selection_file": str(target.relative_to(ROOT)),
            "game_launch_tested": False}


def _ps3_firmware_context() -> tuple[dict, dict, Path, Path, Path]:
    profile = read_json(PROFILE_PATH)
    collection, system = _profile_system_by_id(profile, "ps3")
    firmware = system.get("firmware")
    if not isinstance(firmware, dict) or firmware.get("type") != "ps3_system_software_pup":
        raise ValueError("O perfil PS3 não declara um firmware PUP compatível.")
    configured_root = Path(str(profile.get("bios_root", "")))
    bios_root = configured_root if configured_root.is_absolute() else RETROFE_ROOT / configured_root
    bios_root = bios_root.resolve()
    if bios_root != (ROOT / "bios").resolve():
        raise ValueError(f"A raiz BIOS precisa ser a pasta bios do projeto; perfil atual aponta para {bios_root}.")
    canonical_relative = Path(str(firmware.get("canonical_path", "")))
    legacy_relative = Path(str(firmware.get("legacy_source_path", "")))
    if (canonical_relative.is_absolute() or not canonical_relative.parts
            or any(part in {"", ".", ".."} for part in canonical_relative.parts)
            or legacy_relative.is_absolute() or not legacy_relative.parts
            or any(part in {"", ".", ".."} for part in legacy_relative.parts)):
        raise ValueError("Os caminhos PUP canônico/legado do perfil são inválidos.")
    canonical = bios_root / canonical_relative
    legacy = ROOT / legacy_relative
    for base, path in ((bios_root, canonical), (ROOT, legacy)):
        if not path.resolve(strict=False).is_relative_to(base.resolve()):
            raise ValueError(f"Caminho PUP saiu da raiz permitida: {path}")
        cursor = base
        try:
            relative = path.relative_to(base)
        except ValueError as error:
            raise ValueError(f"Caminho PUP fora da raiz permitida: {path}") from error
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"Componente symlink no caminho PUP: {cursor}")
    version_file = _safe_userdata_file(str(firmware.get("installed_version_file", "")))
    return profile, system, canonical, legacy, version_file


def _pup_file_info(path: Path, firmware: dict) -> dict[str, object]:
    if not os.path.lexists(path):
        return {"status": "missing", "path": str(path.relative_to(ROOT))}
    if path.is_symlink():
        return {"status": "blocked_symlink", "path": str(path.relative_to(ROOT))}
    if not path.is_file():
        return {"status": "invalid_type", "path": str(path.relative_to(ROOT))}
    size = path.stat().st_size
    sha256 = romtool.sha256_file(path)
    with path.open("rb") as stream:
        signature = stream.read(5)
    expected_size = int(firmware.get("expected_size_bytes", 0))
    expected_sha256 = str(firmware.get("expected_sha256", "")).casefold()
    ok = signature == b"SCEUF" and size == expected_size and sha256 == expected_sha256
    return {"status": "verified" if ok else "signature_size_or_hash_mismatch",
            "path": str(path.relative_to(ROOT)), "size_bytes": size, "sha256": sha256,
            "signature_hex": signature.hex(), "expected_size_bytes": expected_size,
            "expected_sha256": expected_sha256}


def ps3_firmware_status() -> dict[str, object]:
    _profile, system, canonical, legacy, version_file = _ps3_firmware_context()
    firmware = system["firmware"]
    canonical_info = _pup_file_info(canonical, firmware)
    legacy_info = _pup_file_info(legacy, firmware)
    installed_release = ""
    if version_file.is_symlink():
        installed_status = "blocked_symlink"
    elif version_file.is_file():
        version_text = version_file.read_text(encoding="utf-8", errors="replace")[:4096]
        match = re.search(r"^\s*release\s*:\s*([^\s:]+)", version_text, re.IGNORECASE | re.MULTILINE)
        installed_release = match.group(1) if match else ""
        installed_status = "installed" if installed_release else "version_file_unrecognized"
    else:
        installed_status = "not_installed"
    expected_release = str(firmware.get("expected_installed_release", ""))
    if installed_release and installed_release != expected_release:
        installed_status = "installed_different_release"
    if canonical_info["status"] == "verified" and legacy_info["status"] == "verified":
        source_status = "duplicate_verified_sources"
    elif canonical_info["status"] == "verified":
        source_status = "canonical_verified"
    elif legacy_info["status"] == "verified":
        source_status = "legacy_source_verified_needs_relocation"
    elif canonical_info["status"] == "missing" and legacy_info["status"] == "missing":
        source_status = "not_present_download_available"
    else:
        source_status = "source_invalid_or_ambiguous"
    return {"system_id": "ps3", "firmware_type": "PS3 system software PUP (não BIOS)",
            "source_status": source_status, "canonical": canonical_info, "legacy_source": legacy_info,
            "installed_status": installed_status, "installed_release": installed_release,
            "expected_release": expected_release,
            "relocation_operation_id": str(firmware.get("relocation_operation_id", "")),
            "download_source": str(firmware.get("download_page", "")),
            "automatic_download": "somente página oficial PlayStation, com tamanho/SHA-256 fixados e assinatura SCEUF",
            "game_launch_tested": False}


class _SonyPupLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        for key, value in attrs:
            if key.casefold() == "href" and value and "ps3updat.pup" in value.casefold():
                self.links.append(value.strip())


def _official_sony_download_url(page_url: str) -> str:
    parsed_page = urlparse(page_url)
    host = (parsed_page.hostname or "").casefold()
    if (parsed_page.scheme != "https" or parsed_page.username or parsed_page.password
            or parsed_page.port not in {None, 443}
            or not (host == "playstation.com" or host.endswith(".playstation.com"))):
        raise ValueError("A página de firmware precisa pertencer ao domínio oficial PlayStation via HTTPS.")
    request = Request(page_url, headers={"User-Agent": "emutool/1.5.0 (project-local firmware setup)"})
    with urlopen(request, timeout=30) as response:
        final_page = response.geturl()
        final_host = (urlparse(final_page).hostname or "").casefold()
        if not (urlparse(final_page).scheme == "https" and
                (final_host == "playstation.com" or final_host.endswith(".playstation.com"))):
            raise ValueError("A página oficial redirecionou para um domínio não aprovado.")
        html = response.read(5 * 1024 * 1024 + 1)
        if len(html) > 5 * 1024 * 1024:
            raise ValueError("A página oficial excede 5 MiB; não processei o conteúdo.")
    parser = _SonyPupLinkParser()
    parser.feed(html.decode("utf-8", errors="replace"))
    candidates = sorted({urljoin(final_page, link) for link in parser.links})
    valid = []
    for candidate in candidates:
        parsed = urlparse(candidate)
        candidate_host = (parsed.hostname or "").casefold()
        if (parsed.scheme == "https" and
                (candidate_host == "ps3.update.playstation.net" or candidate_host.endswith(".ps3.update.playstation.net"))
                and Path(parsed.path).name.casefold() == "ps3updat.pup"
                and not parsed.username and not parsed.password and parsed.port in {None, 443}):
            valid.append(candidate)
    if len(valid) != 1:
        raise ValueError(f"A página oficial não forneceu um único link PUP aprovado; candidatos válidos: {len(valid)}.")
    return valid[0]


def _record_created_file_change(change_type: str, reason: str, path: Path,
                                details: dict[str, object]) -> str:
    size = path.stat().st_size
    digest = romtool.sha256_file(path)
    record_details = {**details, "path": str(path.relative_to(ROOT)), "size_bytes": size,
                      "sha256": digest, "rollback_strategy": "mover o arquivo criado para a pasta rollback da operação somente enquanto tamanho/SHA-256 coincidirem"}
    operation = romtool.command_record_change(SimpleNamespace(
        change_type=change_type, reason=reason,
        details_json=json.dumps(record_details, ensure_ascii=False)))
    operation_id = str(operation["operation_id"])
    manifest_path = romtool.operation_dir(operation_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["details"]["rollback_snapshot"] = {"path": "", "file_was_absent": True,
                                                "after_sha256": digest, "size_bytes": size}
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, manifest_path)
    romtool.append_event(operation_id, "ps3_firmware_file_downloaded", action=change_type,
                         status="completed", destination=str(path.relative_to(ROOT)),
                         sha256_after=digest, size_after=size, details=record_details)
    romtool.rebuild_history_index()
    return operation_id


def download_ps3_firmware() -> dict[str, object]:
    _profile, system, canonical, legacy, _version_file = _ps3_firmware_context()
    firmware = system["firmware"]
    canonical_info = _pup_file_info(canonical, firmware)
    legacy_info = _pup_file_info(legacy, firmware)
    if canonical_info["status"] != "missing":
        raise RuntimeError(f"Não baixei nem sobrescrevi o destino existente: {canonical_info}")
    if legacy_info["status"] != "missing":
        raise RuntimeError("Existe uma fonte PS3 PUP em caminho legado; vou preservar e não criar uma segunda cópia. Use a operação de relocação revisável registrada no perfil.")
    url = _official_sony_download_url(str(firmware.get("download_page", "")))
    expected_size = int(firmware.get("expected_size_bytes", 0))
    expected_hash = str(firmware.get("expected_sha256", "")).casefold()
    if expected_size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("O perfil não fixa tamanho e SHA-256 do PUP; download bloqueado.")
    bios_root = ROOT / "bios"
    if bios_root.is_symlink():
        raise ValueError("A raiz bios/ é symlink; download bloqueado.")
    cursor = bios_root
    for part in canonical.relative_to(bios_root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink no destino BIOS: {cursor}")
    canonical.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "emutool/1.5.0 (project-local firmware setup)"})
    temp_name = ""
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".PS3UPDAT.PUP.", suffix=".part", dir=canonical.parent)
        with os.fdopen(descriptor, "wb") as output, urlopen(request, timeout=45) as response:
            final = urlparse(response.geturl())
            final_host = (final.hostname or "").casefold()
            if (final.scheme != "https" or
                    not (final_host == "ps3.update.playstation.net" or final_host.endswith(".ps3.update.playstation.net"))
                    or Path(final.path).name.casefold() != "ps3updat.pup"
                    or final.username or final.password or final.port not in {None, 443}):
                raise ValueError("O servidor redirecionou para um host/caminho não aprovado.")
            length = response.headers.get("Content-Length")
            if length and int(length) != expected_size:
                raise ValueError(f"Content-Length {length} diverge do tamanho fixado {expected_size}.")
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > expected_size:
                    raise ValueError("O PUP recebido excedeu o tamanho fixado; download descartado.")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError(f"PUP baixado não corresponde ao tamanho/SHA-256 oficial fixado: {size} bytes, {digest.hexdigest()}.")
        with open(temp_name, "rb") as staged:
            if staged.read(5) != b"SCEUF":
                raise ValueError("Assinatura SCEUF ausente; download descartado.")
        os.chmod(temp_name, 0o644)
        try:
            os.link(temp_name, canonical)
        except FileExistsError as error:
            raise RuntimeError(f"O destino foi criado durante o download; não sobrescrevi {canonical}.") from error
        os.unlink(temp_name)
        temp_name = ""
        try:
            operation_id = _record_created_file_change(
                "ps3_firmware_download",
                "Baixar somente o PUP PS3 fixado a partir da página oficial PlayStation, após ausência de fonte local.",
                canonical,
                {"operation_scope": "ps3_firmware_download", "source_page": firmware["download_page"],
                 "download_url": url, "signature": "SCEUF", "expected_size_bytes": expected_size,
                 "expected_sha256": expected_hash, "no_overwrite": True})
        except Exception:
            if canonical.is_file() and not canonical.is_symlink() and romtool.sha256_file(canonical) == expected_hash:
                canonical.unlink()
            raise
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
    return {"status": "downloaded_verified", "operation_id": operation_id,
            "path": str(canonical.relative_to(ROOT)), "size_bytes": size,
            "sha256": digest.hexdigest(), "source_page": firmware["download_page"],
            "download_url": url, "game_launch_tested": False}


def _tree_fingerprint(root: Path) -> dict[str, object]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Árvore de firmware ausente ou symlink: {root}")
    rows = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda entry: entry.as_posix().casefold()):
        if path.is_symlink():
            raise ValueError(f"Symlink inesperado na árvore de firmware: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Tipo inesperado na árvore de firmware: {path}")
        size = path.stat().st_size
        digest = romtool.sha256_file(path)
        relative = path.relative_to(root).as_posix()
        rows.append((relative, size, digest))
        total += size
    tree_digest = hashlib.sha256()
    for relative, size, digest in rows:
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0" + str(size).encode("ascii") + b"\0" + digest.encode("ascii") + b"\n")
    return {"file_count": len(rows), "size_bytes": total, "tree_sha256": tree_digest.hexdigest(),
            "files": [{"path": path, "size_bytes": size, "sha256": digest}
                      for path, size, digest in rows]}


def _rpcs3_processes_running() -> list[dict[str, object]]:
    found = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return found
    for process in proc_root.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            executable = os.readlink(process / "exe")
        except OSError:
            continue
        if "rpcs3" in Path(executable).name.casefold():
            found.append({"pid": int(process.name), "executable": executable})
    return found


def _record_firmware_tree_install(system: dict, firmware: dict, pup: Path,
                                  destination: Path, fingerprint: dict) -> str:
    details = {"operation_scope": "ps3_firmware_install", "system_id": "ps3",
               "source_pup": str(pup.relative_to(ROOT)), "source_pup_sha256": romtool.sha256_file(pup),
               "destination_relative": str(destination.relative_to(ROOT)),
               "installed_release": str(firmware.get("expected_installed_release", "")),
               "tree_fingerprint": fingerprint,
               "rollback_strategy": "mover dev_flash para rollback somente se caminho, contagem, bytes e SHA-256 da árvore ainda coincidirem"}
    operation = romtool.command_record_change(SimpleNamespace(
        change_type="ps3_firmware_install",
        reason="Importar firmware PS3 oficial em XDG temporário, verificar release e promover sem sobrescrever dados.",
        details_json=json.dumps(details, ensure_ascii=False)))
    operation_id = str(operation["operation_id"])
    romtool.append_event(operation_id, "ps3_firmware_installed_verified", action="ps3_firmware_install",
                         status="completed", source=str(pup.relative_to(ROOT)),
                         destination=str(destination.relative_to(ROOT)),
                         sha256_before=details["source_pup_sha256"],
                         size_after=fingerprint["size_bytes"],
                         details={"installed_release": details["installed_release"],
                                  "tree_sha256": fingerprint["tree_sha256"],
                                  "file_count": fingerprint["file_count"]})
    romtool.rebuild_history_index()
    return operation_id


def _install_ps3_firmware(pup: Path, system: dict, firmware: dict) -> dict[str, object]:
    if romtool.retrofe_processes_running():
        raise RuntimeError("Feche o RetroFE normalmente antes de importar firmware PS3; nenhum processo foi encerrado.")
    running = _rpcs3_processes_running()
    if running:
        raise RuntimeError(f"RPCS3 está aberto (PIDs {[entry['pid'] for entry in running]}); feche-o normalmente antes da importação.")
    runtime = resolve_executable(str(system.get("command", "")), RETROFE_ROOT)
    if not runtime:
        raise RuntimeError("RPCS3 AppImage não está disponível no caminho portátil do perfil.")
    firmware_info = _pup_file_info(pup, firmware)
    if firmware_info["status"] != "verified":
        raise RuntimeError(f"PUP não passou o hash/tamanho/assinatura fixados: {firmware_info}")
    version_file = _safe_userdata_file(str(firmware["installed_version_file"]))
    destination = version_file.parent.parent.parent
    if os.path.lexists(destination):
        raise RuntimeError(f"A árvore dev_flash já existe em {destination}; preserve o estado atual e não sobrescreva.")
    userdata_subdir = str(system.get("userdata_subdir", ""))
    userdata_root = _safe_userdata_file("UserData/" + userdata_subdir)
    userdata_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".ps3-firmware-stage-", dir=userdata_root))
    stage_xdg = {name: stage / part for name, part in (
        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"))}
    env = os.environ.copy()
    for name, value in stage_xdg.items():
        value.mkdir(parents=True, exist_ok=True)
        env[name] = str(value)
    for name, value in system.get("environment", {}).items():
        if name not in stage_xdg and isinstance(name, str) and isinstance(value, str):
            env[name] = value.format_map({"userdata": str(userdata_root)})
    args = firmware.get("headless_install_args")
    if not isinstance(args, list) or not args or not all(isinstance(value, str) for value in args):
        shutil.rmtree(stage)
        raise RuntimeError("O perfil PS3 não declara headless_install_args válidos.")
    argv = [runtime, *(value.format_map({"firmware": str(pup.resolve())}) for value in args)]
    try:
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True,
                                errors="replace", check=False, timeout=600)
        staged_flash = stage_xdg["XDG_CONFIG_HOME"] / "rpcs3" / "dev_flash"
        staged_version = staged_flash / "vsh/etc/version.txt"
        if result.returncode != 0 or not staged_version.is_file():
            output = (result.stdout + result.stderr)[-3000:]
            shutil.rmtree(stage)
            raise RuntimeError(f"RPCS3 não concluiu importação headless do PUP (código {result.returncode}); stage descartado. Saída: {output}")
        content = staged_version.read_text(encoding="utf-8", errors="replace")[:4096]
        match = re.search(r"^\s*release\s*:\s*([^\s:]+)", content, re.IGNORECASE | re.MULTILINE)
        expected_release = str(firmware.get("expected_installed_release", ""))
        if not match or match.group(1) != expected_release:
            shutil.rmtree(stage)
            raise RuntimeError(f"Firmware importado não confirmou release {expected_release}; stage descartado.")
        fingerprint = _tree_fingerprint(staged_flash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(destination):
            shutil.rmtree(stage)
            raise RuntimeError(f"dev_flash apareceu durante a importação; não sobrescrevi {destination}.")
        os.rename(staged_flash, destination)
        promoted = _tree_fingerprint(destination)
        if promoted["tree_sha256"] != fingerprint["tree_sha256"]:
            os.rename(destination, staged_flash)
            shutil.rmtree(stage)
            raise RuntimeError("A árvore dev_flash mudou durante a promoção; retorno ao stage aplicado.")
        try:
            operation_id = _record_firmware_tree_install(system, firmware, pup, destination, promoted)
        except Exception:
            if destination.is_dir() and _tree_fingerprint(destination)["tree_sha256"] == fingerprint["tree_sha256"]:
                os.rename(destination, staged_flash)
                shutil.rmtree(stage)
            raise
        shutil.rmtree(stage)
    except subprocess.TimeoutExpired as error:
        if stage.exists():
            shutil.rmtree(stage)
        raise RuntimeError("RPCS3 excedeu 600 segundos na importação do firmware; o estado portátil original não foi tocado.") from error
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return {"status": "installed_verified", "operation_id": operation_id,
            "release": expected_release, "destination": str(destination.relative_to(ROOT)),
            "file_count": promoted["file_count"], "size_bytes": promoted["size_bytes"],
            "tree_sha256": promoted["tree_sha256"], "game_launch_tested": False}


def ps3_firmware_setup(apply: bool) -> dict[str, object]:
    if not apply:
        return {"status": "plan_only", "current": ps3_firmware_status(),
                "next_step": "rerun firmware setup --apply after reviewing; relocation/import require RetroFE and RPCS3 closed"}
    current = ps3_firmware_status()
    _profile, system, canonical, _legacy, _version_file = _ps3_firmware_context()
    firmware = system["firmware"]
    if current["source_status"] == "duplicate_verified_sources":
        raise RuntimeError("O mesmo PUP verificado existe em bios/ e roms/; preserve ambos e reconcilie primeiro o plano de relocação.")
    if current["source_status"] == "legacy_source_verified_needs_relocation":
        pids = romtool.retrofe_processes_running()
        if pids:
            return {"status": "blocked_retrofe_active", "current": current, "retrofe_pids": pids,
                    "next_step": "feche RetroFE normalmente e execute emutool.py firmware setup --apply novamente; nenhum processo será encerrado"}
        if _rpcs3_processes_running():
            return {"status": "blocked_rpcs3_active", "current": current,
                    "next_step": "feche RPCS3 normalmente e execute emutool.py firmware setup --apply novamente"}
        operation_id = str(firmware.get("relocation_operation_id", ""))
        if not operation_id:
            raise RuntimeError("A fonte está em caminho legado, mas o perfil não aponta para um plano romtool revisável.")
        manifest_path = romtool.operation_dir(operation_id) / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"Plano de relocação não encontrado: {operation_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") == "prepared":
            result = subprocess.run([sys.executable, str(ROOT / "tools/roms/romtool.py"),
                                     "apply", "--operation-id", operation_id, "--apply"],
                                    cwd=ROOT, capture_output=True, text=True, errors="replace",
                                    check=False, timeout=300)
            if result.returncode:
                return {"status": "relocation_blocked", "operation_id": operation_id,
                        "returncode": result.returncode, "output": (result.stdout + result.stderr)[-3000:],
                        "current": ps3_firmware_status()}
            current = ps3_firmware_status()
        elif manifest.get("status") not in {"completed", "completed_verified"}:
            raise RuntimeError(f"Plano de relocação PS3 está em estado {manifest.get('status')!r}; não apliquei.")
        current = ps3_firmware_status()
        if current["source_status"] not in {"canonical_verified", "not_present_download_available"}:
            raise RuntimeError(f"O plano não deixou o PUP em estado canônico verificável: {current}")
    if current["installed_status"] == "installed" and current["installed_release"] == str(firmware.get("expected_installed_release", "")):
        return {"status": "already_ready", "current": current,
                "note": "Firmware PS3 já está importado no ../../emulator/data/RPCS3; não iniciei RPCS3 nem relancei jogo."}
    if current["source_status"] == "not_present_download_available":
        downloaded = download_ps3_firmware()
        current = ps3_firmware_status()
    elif current["source_status"] == "canonical_verified":
        downloaded = None
    elif current["source_status"] == "legacy_source_verified_needs_relocation":
        raise RuntimeError("A fonte legada não foi movida; preserve-a e resolva o estado do plano.")
    else:
        raise RuntimeError(f"Fonte de firmware ausente, inválida ou ambígua: {current}")
    if current["canonical"]["status"] != "verified":
        raise RuntimeError("O PUP canônico não passou a verificação integral depois da preparação.")
    installed = _install_ps3_firmware(canonical, system, firmware)
    return {"status": "setup_complete", "download": downloaded, "installation": installed,
            "firmware": ps3_firmware_status()}


def ps3_firmware_rollback(operation_id: str, apply: bool) -> dict[str, object]:
    if not apply:
        return {"status": "plan_only", "operation_id": operation_id,
                "next_step": "use --apply only after reviewing the operation manifest and current firmware hashes"}
    if not re.fullmatch(r"[A-Za-z0-9_-]+", operation_id):
        raise ValueError("operation_id inválido.")
    operation_path = romtool.operation_dir(operation_id)
    manifest_path = operation_path / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Operação não encontrada: {operation_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    details = manifest.get("details", {})
    scope = details.get("operation_scope")
    if manifest.get("status") != "completed" or scope not in {"ps3_firmware_download", "ps3_firmware_install"}:
        raise RuntimeError("Rollback aceita somente operação concluída de download/importação PUP registrada pelo emutool.")
    if romtool.retrofe_processes_running() or _rpcs3_processes_running():
        raise RuntimeError("Feche RetroFE e RPCS3 antes de reverter firmware.")
    rollback_root = operation_path / "rollback" / "emutool-ps3-firmware"
    rollback_root.mkdir(parents=True, exist_ok=True)
    if scope == "ps3_firmware_download":
        _profile, _system, target, _legacy, _version_file = _ps3_firmware_context()
        if str(target.relative_to(ROOT)) != str(details["path"]) or target.is_symlink() or not target.is_file():
            raise RuntimeError("PUP baixado não está no destino BIOS canônico registrado; rollback bloqueado.")
        if target.stat().st_size != int(details["size_bytes"]) or romtool.sha256_file(target) != details["sha256"]:
            raise RuntimeError("PUP baixado mudou desde o registro; rollback bloqueado.")
        backup = rollback_root / "PS3UPDAT.PUP"
        if os.path.lexists(backup):
            raise RuntimeError(f"Destino de rollback já existe: {backup}")
        os.rename(target, backup)
        removed = {"path": str(target.relative_to(ROOT)), "rollback_path": str(backup.relative_to(ROOT)),
                   "sha256": details["sha256"], "size_bytes": details["size_bytes"]}
    else:
        destination_relative = str(details["destination_relative"])
        prefix = str(RETROFE_ROOT.relative_to(ROOT)) + "/"
        if destination_relative.startswith('emulator/data/'):
            target = _safe_userdata_file(destination_relative)
        elif destination_relative.startswith(prefix):
            target = _safe_userdata_file(destination_relative[len(prefix):])
        else:
            raise RuntimeError("Destino da instalação não está nos dados canônicos do emulador; rollback bloqueado.")
        current_tree = _tree_fingerprint(target)
        expected_tree = details.get("tree_fingerprint", {})
        if (current_tree["tree_sha256"] != expected_tree.get("tree_sha256")
                or current_tree["file_count"] != expected_tree.get("file_count")
                or current_tree["size_bytes"] != expected_tree.get("size_bytes")):
            raise RuntimeError("dev_flash foi alterado após a instalação; rollback bloqueado para preservar dados novos.")
        backup = rollback_root / "dev_flash"
        if os.path.lexists(backup):
            raise RuntimeError(f"Destino de rollback já existe: {backup}")
        os.rename(target, backup)
        removed = {"path": str(target.relative_to(ROOT)), "rollback_path": str(backup.relative_to(ROOT)),
                   "tree_sha256": current_tree["tree_sha256"], "file_count": current_tree["file_count"],
                   "size_bytes": current_tree["size_bytes"]}
    manifest["status"] = "rolled_back"
    manifest["rolled_back_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    manifest.setdefault("details", {})["rollback_result"] = removed
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, manifest_path)
    romtool.append_event(operation_id, "ps3_firmware_change_rolled_back", action="ps3_firmware_rollback",
                         status="rolled_back", source=removed["path"], destination=removed["rollback_path"],
                         details=removed)
    romtool.rebuild_history_index()
    return {"status": "rolled_back", "operation_id": operation_id, "preserved": removed}


def _onboarding_target(system: dict, entry: dict) -> tuple[Path, str]:
    """Resolve a declared onboarding config without following symlinks."""
    userdata_subdir = system.get("userdata_subdir")
    relative = Path(str(entry.get("path", "")))
    if not userdata_subdir:
        raise ValueError("onboarding_defaults exige userdata_subdir no perfil.")
    if not relative.parts or relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("O caminho de onboarding precisa ser relativo e sem traversal.")
    userdata_relative = Path(str(userdata_subdir))
    if userdata_relative.is_absolute() or any(part in {".", ".."} for part in userdata_relative.parts):
        raise ValueError("userdata_subdir precisa permanecer relativo a UserData/.")

    userdata_root = ROOT / 'emulator/data' / userdata_relative
    user_data_base = ROOT / 'emulator/data'
    if user_data_base.is_symlink() or userdata_root.is_symlink():
        raise ValueError("A raiz de UserData ou a pasta da coleção é symlink; gravação bloqueada.")
    userdata_resolved = userdata_root.resolve()
    if not userdata_resolved.is_relative_to(user_data_base.resolve()):
        raise ValueError("userdata_subdir saiu de UserData/.")

    target = userdata_root / relative
    cursor = userdata_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Componente symlink no caminho de onboarding: {cursor}")
    resolved = target.resolve()
    if not resolved.is_relative_to(userdata_resolved):
        raise ValueError("O arquivo de onboarding saiu da pasta portátil do emulador.")
    return target, str(target.relative_to(ROOT))


def _ini_setting_values(text: str, section: str, key: str) -> tuple[list[str], int]:
    current = ""
    section_count = 0
    values: list[str] = []
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            current = header.group(1)
            if current.casefold() == section.casefold():
                section_count += 1
            continue
        if current.casefold() != section.casefold():
            continue
        match = re.match(r"^\s*([^=]+?)\s*=\s*(.*?)\s*$", line)
        if match and match.group(1).strip().casefold() == key.casefold():
            values.append(match.group(2))
    return values, section_count


def _set_ini_value(text: str, section: str, key: str, value: str) -> str:
    """Replace the single existing key value, preserving its spelling and indentation."""
    lines = text.splitlines()
    current = ""
    for index, line in enumerate(lines):
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            current = header.group(1)
            continue
        if current.casefold() != section.casefold():
            continue
        match = re.match(r"^(\s*)([^=]+?)(\s*=\s*)(.*?)\s*$", line)
        if match and match.group(2).strip().casefold() == key.casefold():
            lines[index] = f"{match.group(1)}{match.group(2)}{match.group(3)}{value}"
            return "\n".join(lines) + "\n"
    raise ValueError(f"Chave {section}/{key} ausente para substituição.")


def _set_ini_default(text: str, section: str, key: str, value: str) -> str:
    lines = text.splitlines()
    current = ""
    section_seen = False
    insertion = len(lines)
    for index, line in enumerate(lines):
        header = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if header:
            if current.casefold() == section.casefold():
                insertion = index
                break
            current = header.group(1)
            if current.casefold() == section.casefold():
                section_seen = True
            continue
    if section_seen:
        lines.insert(insertion, f"{key} = {value}")
        return "\n".join(lines) + "\n"
    suffix = "\n" if text and not text.endswith("\n") else ""
    if text and not text.endswith("\n\n"):
        suffix = "\n"
    return f"{text}{suffix}[{section}]\n{key} = {value}\n"


def _onboarding_guide(system: dict) -> dict[str, object] | None:
    """Validate and return the ordered, profile-owned first-run guide."""
    raw = system.get("onboarding_guide")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("onboarding_guide precisa ser um objeto.")
    title = str(raw.get("title", "")).strip()
    intro = str(raw.get("intro", "")).strip()
    raw_steps = raw.get("steps")
    if not title or not intro or not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("onboarding_guide exige title, intro e uma lista não vazia de steps.")
    steps: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw_steps, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"onboarding_guide.steps[{index - 1}] precisa ser um objeto.")
        step_id = str(entry.get("id", "")).strip()
        step_title = str(entry.get("title", "")).strip()
        instruction = str(entry.get("instruction", "")).strip()
        if not step_id or not step_title or not instruction or step_id in seen_ids:
            raise ValueError(f"Etapa de onboarding {index} inválida ou com id duplicado.")
        seen_ids.add(step_id)
        steps.append({"id": step_id, "title": step_title, "instruction": instruction})
    completion = str(raw.get("completion", "")).strip()
    if not completion:
        raise ValueError("onboarding_guide exige uma instrução de completion.")
    return {"title": title, "intro": intro, "steps": steps, "completion": completion}


def onboarding_probe(system: dict, profile: dict) -> dict[str, object]:
    defaults = system.get("onboarding_defaults", [])
    manual_entries = system.get("onboarding_manual_requirements", [])
    if not defaults and not manual_entries:
        return {"status": "not_declared", "defaults": []}
    if not isinstance(defaults, list):
        return {"status": "invalid_profile", "reason": "onboarding_defaults precisa ser uma lista."}

    reports: list[dict[str, object]] = []
    for entry in defaults:
        if not isinstance(entry, dict):
            reports.append({"status": "invalid_profile", "reason": "Entrada de onboarding não é objeto."})
            continue
        try:
            target, relative_path = _onboarding_target(system, entry)
            if str(entry.get("format", "ini")).casefold() != "ini":
                raise ValueError("Formato de onboarding não suportado; somente INI está habilitado.")
            section, key = str(entry.get("section", "")), str(entry.get("key", ""))
            if not section or not key or "\n" in section or "\n" in key:
                raise ValueError("Section e key INI precisam ser textos não vazios de uma linha.")
            policy = str(entry.get("policy", "insert_if_missing")).casefold()
            if policy not in {"insert_if_missing", "set"}:
                raise ValueError("policy de onboarding inválida; use insert_if_missing ou set.")
            if not target.exists():
                state, current = "pending_default", None
            else:
                text = target.read_text(encoding="utf-8")
                values, section_count = _ini_setting_values(text, section, key)
                if section_count > 1 or len(values) > 1:
                    state, current = "ambiguous_config", None
                elif values:
                    current = values[0]
                    if current.casefold() == str(entry.get("value", "")).casefold():
                        state = "already_prepared"
                    elif policy == "set":
                        state = "pending_override"
                    else:
                        state = "explicit_choice_preserved"
                else:
                    state, current = "pending_default", None
            reports.append({
                "status": state,
                "format": "ini",
                "path": relative_path,
                "section": section,
                "key": key,
                "desired_value": str(entry.get("value", "")),
                "current_value": current,
                "policy": "apply only when the key is absent; preserve explicit values"
                if policy == "insert_if_missing" else "replace the single declared key when it differs",
                "reason": str(entry.get("reason", "")),
            })
        except (OSError, ValueError, RuntimeError) as error:
            reports.append({"status": "blocked", "reason": str(error), "path": str(entry.get("path", ""))})

    if not isinstance(manual_entries, list):
        return {"status": "invalid_profile", "defaults": reports,
                "reason": "onboarding_manual_requirements precisa ser uma lista."}
    manual_reports: list[dict[str, object]] = []
    for entry in manual_entries:
        if not isinstance(entry, dict) or str(entry.get("format", "ini")).casefold() != "ini":
            manual_reports.append({"status": "invalid_profile", "reason": "Etapa manual inválida ou formato não suportado."})
            continue
        try:
            target, relative_path = _onboarding_target(system, entry)
            section, key = str(entry.get("section", "")), str(entry.get("key", ""))
            complete_value = str(entry.get("complete_value", ""))
            if not section or not key or not complete_value:
                raise ValueError("A etapa manual exige section, key e complete_value.")
            if not target.exists():
                values, section_count = [], 0
            else:
                values, section_count = _ini_setting_values(target.read_text(encoding="utf-8"), section, key)
            if section_count > 1 or len(values) > 1:
                state, current = "ambiguous_config", None
            elif not values:
                state, current = "manual_required", None
            else:
                current = values[0]
                state = "completed" if current.casefold() == complete_value.casefold() else "manual_required"
            manual_reports.append({
                "status": state,
                "format": "ini",
                "path": relative_path,
                "section": section,
                "key": key,
                "complete_value": complete_value,
                "current_value": current,
                "reason": str(entry.get("reason", "")),
            })
        except (OSError, ValueError, RuntimeError) as error:
            manual_reports.append({"status": "blocked", "reason": str(error), "path": str(entry.get("path", ""))})

    statuses = {str(item.get("status")) for item in reports}
    manual_statuses = {str(item.get("status")) for item in manual_reports}
    if statuses & {"blocked", "invalid_profile", "ambiguous_config"} or manual_statuses & {"blocked", "invalid_profile", "ambiguous_config"}:
        overall = "blocked"
    elif "manual_required" in manual_statuses:
        overall = "manual_required"
    elif "pending_default" in statuses or "pending_override" in statuses:
        overall = "pending_default"
    elif manual_reports:
        overall = "completed"
    elif reports:
        overall = "prepared"
    else:
        overall = "not_declared"
    result: dict[str, object] = {
        "status": overall,
        "defaults": reports,
        "manual_requirements": manual_reports,
    }
    try:
        guide = _onboarding_guide(system)
    except ValueError as error:
        result["status"] = "invalid_profile"
        result["guide_error"] = str(error)
    else:
        if guide is not None:
            result["guide"] = guide
    return result


def _atomic_write(path: Path, data: bytes, existing_mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, existing_mode if existing_mode is not None else 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _record_onboarding_change(
    target: Path,
    relative_path: str,
    before: bytes | None,
    after: bytes,
    entry: dict,
) -> str:
    before_hash = hashlib.sha256(before).hexdigest() if before is not None else ""
    after_hash = hashlib.sha256(after).hexdigest()
    reason = str(entry.get("reason", "Aplicar padrão seguro de primeiro lançamento declarado pelo perfil host."))
    details = {
        "change_type": "emulator_first_run_onboarding_default",
        "config_path": relative_path,
        "setting": {"section": entry["section"], "key": entry["key"], "value": str(entry["value"])},
        "policy": str(entry.get("policy", "insert_if_missing")),
        "sha256_before": before_hash,
        "sha256_after": after_hash,
        "size_before": len(before) if before is not None else 0,
        "size_after": len(after),
        "rollback_strategy": "restore the exact snapshot when a prior file existed; otherwise remove this generated file only when its SHA-256 still matches the recorded after hash",
        "file_was_absent": before is None,
    }
    operation = romtool.command_record_change(SimpleNamespace(
        change_type="emulator_first_run_onboarding_default",
        reason=reason,
        details_json=json.dumps(details, ensure_ascii=False),
    ))
    operation_id = str(operation["operation_id"])
    operation_path = romtool.operation_dir(operation_id)
    rollback_relative = ""
    if before is not None:
        rollback_path = operation_path / "rollback/files" / relative_path
        rollback_path.parent.mkdir(parents=True, exist_ok=True)
        rollback_path.write_bytes(before)
        rollback_relative = str(rollback_path.relative_to(romtool.PROJECT_ROOT))
    manifest_path = operation_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["details"]["rollback_snapshot"] = {
        "path": rollback_relative,
        "sha256": before_hash,
        "size_bytes": len(before) if before is not None else 0,
        "file_was_absent": before is None,
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, manifest_path)
    romtool.append_event(
        operation_id,
        "emulator_first_run_default_applied",
        action="emulator_first_run_onboarding_default",
        status="completed",
        source=relative_path,
        destination=relative_path,
        sha256_before=before_hash,
        sha256_after=after_hash,
        size_before=len(before) if before is not None else 0,
        size_after=len(after),
        details={"setting": details["setting"], "rollback_snapshot": manifest["details"]["rollback_snapshot"]},
    )
    romtool.rebuild_history_index()
    return operation_id


def apply_onboarding(collection: str) -> dict[str, object]:
    profile = read_json(PROFILE_PATH)
    system = profile.get("collections", {}).get(collection)
    if not isinstance(system, dict):
        raise RuntimeError(f"A coleção {collection!r} não existe em host-linux.json.")
    defaults = system.get("onboarding_defaults", [])
    if not isinstance(defaults, list):
        raise RuntimeError("onboarding_defaults precisa ser uma lista.")

    changes: list[dict[str, object]] = []
    for entry in defaults:
        if not isinstance(entry, dict) or str(entry.get("format", "ini")).casefold() != "ini":
            raise RuntimeError("Entrada de onboarding inválida ou formato não suportado.")
        target, relative_path = _onboarding_target(system, entry)
        section, key = str(entry.get("section", "")), str(entry.get("key", ""))
        value = str(entry.get("value", ""))
        if not section or not key or not value or any("\n" in part or "\r" in part for part in (section, key, value)):
            raise RuntimeError("Section, key e value precisam ser textos não vazios de uma linha.")
        policy = str(entry.get("policy", "insert_if_missing")).casefold()
        if policy not in {"insert_if_missing", "set"}:
            raise RuntimeError("policy de onboarding inválida; use insert_if_missing ou set.")
        original = target.read_bytes() if target.exists() else None
        text = original.decode("utf-8") if original is not None else ""
        values, section_count = _ini_setting_values(text, section, key)
        if section_count > 1 or len(values) > 1:
            raise RuntimeError(f"Configuração ambígua; não alterei {relative_path}.")
        if values:
            current = values[0]
            if current.casefold() == value.casefold():
                changes.append({"path": relative_path, "status": "already_prepared", "current_value": current})
                continue
            if policy != "set":
                changes.append({"path": relative_path, "status": "explicit_choice_preserved",
                                "current_value": current})
                continue
            updated_text = _set_ini_value(text, section, key, value)
        else:
            updated_text = _set_ini_default(text, section, key, value)
        updated = updated_text.encode("utf-8")
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        _atomic_write(target, updated, mode)
        try:
            operation_id = _record_onboarding_change(target, relative_path, original, updated, entry)
        except Exception:
            current = target.read_bytes() if target.exists() and not target.is_symlink() else b""
            if hashlib.sha256(current).hexdigest() == hashlib.sha256(updated).hexdigest():
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target, original, mode)
            raise
        changes.append({"path": relative_path, "status": "default_applied", "operation_id": operation_id,
                        "sha256_before": hashlib.sha256(original).hexdigest() if original is not None else "",
                        "sha256_after": hashlib.sha256(updated).hexdigest()})
    onboarding = onboarding_probe(system, profile)
    overall = "default_applied" if any(row["status"] == "default_applied" for row in changes) else (
        "manual_required" if onboarding.get("status") == "manual_required" else "preserved_or_prepared"
    )
    return {"collection": collection, "status": overall, "changes": changes, "onboarding": onboarding}


def installed_flatpaks() -> tuple[dict[str, str], bool]:
    return runtime_install.host_flatpaks()


def mame_path(profile: dict) -> str | None:
    configured = os.environ.get("RETROFE_MAME", str(profile.get("mame_executable", "mame")))
    return resolve_executable(configured, ROOT)


def portability_summary(collection: str, system: dict, profile: dict, core_root: Path) -> dict[str, object]:
    adapter = str(system.get("adapter", ""))
    userdata = str(system.get("userdata_subdir", ""))
    if adapter == "retroarch":
        core_path = core_root.resolve()
        local = core_path.is_relative_to(ROOT.resolve())
        return {"status": "project_local" if local else "external_core_root",
                "userdata_subdir": userdata, "core_root": str(core_path),
                "external_dependency": "" if local else str(core_path),
                "reason": "RetroArch cores must travel with the project to make this profile portable."}
    if adapter == "flatpak":
        return {"status": "project_userdata_host_runtime_dependency" if userdata else "host_default_userdata",
                "userdata_subdir": userdata,
                "external_dependency": str(system.get("app_id", "Flatpak application")),
                "reason": "Flatpak is supplied by the host; emulator config/saves are project-local only when userdata_subdir is declared."}
    if adapter == "command":
        command = str(system.get("command", ""))
        executable = resolve_executable(command, RETROFE_ROOT) if command else None
        local = bool(executable) and Path(executable).resolve().is_relative_to(ROOT.resolve())
        return {"status": "project_local" if local and userdata else (
                    "project_runtime_external_userdata" if local else "host_runtime_dependency"),
                "userdata_subdir": userdata, "runtime_path": executable or command,
                "external_dependency": "" if local else (executable or command),
                "reason": "Runtime path and user state are checked separately; a host-installed command is not portable."}
    if adapter in {"mame", "mame_named"}:
        return {"status": "host_runtime_dependency", "userdata_subdir": userdata,
                "external_dependency": str(profile.get("mame_executable", "mame")),
                "reason": "MAME executable is supplied by the host package manager."}
    return {"status": "not_assessed", "userdata_subdir": userdata,
            "reason": f"No portability rule is declared for adapter {adapter or '(missing)'!r}."}


def runtime_probe(
    collection: str,
    system: dict,
    profile: dict,
    core_root: Path,
    flatpaks: dict[str, str],
    flatpak_query_ok: bool,
    mame_executable: str | None,
) -> dict[str, object]:
    adapter = str(system.get("adapter", ""))
    result: dict[str, object] = {
        "adapter": adapter or "missing",
        "available": False,
        "status": "adapter_missing" if not adapter else "adapter_unknown",
        "runtime": "",
        "version": "",
        "missing_bios": [],
        "launch_tested": False,
    }
    result["onboarding"] = onboarding_probe(system, profile)
    alternatives = system.get("runtime_options", {})
    if isinstance(alternatives, dict):
        result["runtime_options"] = [
            _probe_runtime_entry(runtime_id, system, entry)
            for runtime_id, entry in alternatives.items() if isinstance(entry, dict)
        ]
    if system.get("runtime_selection_path"):
        try:
            selection_path, selection_doc, _selection_bytes = _load_runtime_selections(system)
            result["runtime_selection"] = {
                "status": "valid",
                "path": str(selection_path.relative_to(ROOT)),
                "title_count": len(selection_doc["selections"]),
                "selections": selection_doc["selections"],
            }
        except (OSError, ValueError, RuntimeError) as error:
            result["runtime_selection"] = {"status": "invalid_or_blocked", "reason": str(error)}
    bios_identity = system.get("bios_identity", {})
    if isinstance(bios_identity, dict) and bios_identity.get("status") == "unverified":
        result["bios_identity_status"] = "unverified"
        result["bios_identity_candidates"] = bios_identity.get("candidate_files", [])
        result["warnings"] = [str(bios_identity.get("reason", "A identidade da BIOS não foi validada."))]

    bios_root = resolve_path(profile.get("bios_root", ""), "RETROFE_BIOS_ROOT")
    required_bios = [str(name) for name in system.get("bios_files", [])]
    missing_bios = [name for name in required_bios if not (bios_root / name).is_file()]
    result["missing_bios"] = missing_bios

    if adapter == "unavailable":
        category = str(system.get("profile_kind", "")).casefold()
        result["status"] = "not_applicable_host" if category == "not_applicable" else "runtime_unavailable"
        result["reason"] = str(system.get("reason", "Perfil marcado indisponível."))
        candidate = str(system.get("runtime_candidate_command", ""))
        candidate_path = resolve_executable(candidate, ROOT) if candidate else None
        if candidate_path:
            result["candidate_detected"] = candidate_path
        elif candidate:
            result["candidate_missing"] = candidate
        candidate_app = str(system.get("runtime_candidate_app_id", ""))
        if candidate_app:
            result["candidate_detected"] = candidate_app in flatpaks
            result["candidate_missing"] = "" if candidate_app in flatpaks else candidate_app
        return result

    if adapter == "retroarch":
        app_id = str(profile.get("retroarch", {}).get("app_id", ""))
        core = str(system.get("core", ""))
        core_path = core_root / core if core else None
        if not app_id or app_id not in flatpaks:
            result["status"] = "retroarch_app_query_unavailable" if not flatpak_query_ok else "retroarch_app_missing"
            result["runtime"] = app_id
            return result
        if not core_path or not core_path.is_file():
            result["status"] = "retroarch_core_missing"
            result["runtime"] = app_id
            result["version"] = flatpaks.get(app_id, "")
            result["core"] = core
            return result
        result.update(
            available=not missing_bios,
            status="bios_missing" if missing_bios else "runtime_ready_unverified",
            runtime=f"{app_id} + {core}",
            version=flatpaks.get(app_id, ""),
            core_path=str(core_path),
        )
        return result

    if adapter == "flatpak":
        app_id = str(system.get("app_id", ""))
        if not app_id:
            result["status"] = "flatpak_app_id_missing"
            return result
        if app_id not in flatpaks:
            result["status"] = "flatpak_app_query_unavailable" if not flatpak_query_ok else "flatpak_app_missing"
            result["runtime"] = app_id
            return result
        result.update(
            available=not missing_bios,
            status="bios_missing" if missing_bios else "runtime_ready_unverified",
            runtime=app_id,
            version=flatpaks.get(app_id, ""),
        )
        return result

    if adapter in {"mame", "mame_named"}:
        if not mame_executable:
            result["status"] = "mame_missing"
            return result
        result.update(
            available=not missing_bios,
            status="bios_missing" if missing_bios else "runtime_ready_unverified",
            runtime=mame_executable,
        )
        return result

    if adapter == "command":
        command = str(system.get("command", ""))
        executable = resolve_executable(command, RETROFE_ROOT) if command else None
        if not executable:
            result["status"] = "command_missing"
            result["runtime"] = command
            return result
        probe_args = system.get("probe_args")
        if (not isinstance(probe_args, list) or not probe_args
                or not all(isinstance(value, str) for value in probe_args)):
            result.update(status="runtime_probe_not_configured", runtime=executable)
            return result
        try:
            probe_env = romtool.host_runtime_probe_environment(system, program_dir=Path(executable).parent)
            probe = subprocess.run(
                [executable, *probe_args],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=30,
                env=probe_env,
                cwd=romtool.host_runtime_probe_directory(system),
            )
        except subprocess.TimeoutExpired as error:
            result.update(status="runtime_probe_timed_out", runtime=executable,
                          runtime_probe=probe_args, probe_timeout_seconds=30,
                          runtime_probe_output=romtool.subprocess_output_text(error.stdout)[-1000:])
            return result
        except (OSError, ValueError) as error:
            result.update(status="runtime_probe_failed", runtime=executable,
                          runtime_probe=probe_args, runtime_probe_error=f"{type(error).__name__}: {error}")
            return result
        expected_exit = int(system.get("probe_expected_exit_code", 0))
        probe_output = (probe.stdout + probe.stderr).strip()
        if probe.returncode != expected_exit:
            result.update(status="runtime_probe_failed", runtime=executable,
                          runtime_probe=probe_args, runtime_probe_returncode=probe.returncode,
                          runtime_probe_expected_exit_code=expected_exit,
                          runtime_probe_output=probe_output[-1000:])
            return result
        result.update(
            available=not missing_bios,
            status="bios_missing" if missing_bios else "runtime_ready_unverified",
            runtime=executable,
            version=str(system.get("runtime_version", "")),
            runtime_probe=probe_args,
            runtime_probe_returncode=probe.returncode,
            runtime_probe_output=probe_output[-1000:],
        )
        return result

    return result


# F06: distinct platform states; the best profile wins, in this order. A located runtime
# never certifies gameplay; "installed_not_probed" means the executable exists but no
# non-interactive probe is declared, which is not the same as a missing runtime.
PLATFORM_STATUS_PRECEDENCE = (
    ("runtime_ready_unverified", lambda r: bool(r.get("available")) and r.get("onboarding", {}).get("status") != "manual_required"),
    ("onboarding_pending", lambda r: bool(r.get("available"))),
    ("dependency_missing", lambda r: r.get("status") == "bios_missing"),
    ("installed_not_probed", lambda r: r.get("status") == "runtime_probe_not_configured"),
    ("runtime_probe_failed", lambda r: r.get("status") in {"runtime_probe_failed", "runtime_probe_timed_out"}),
    ("dependency_missing", lambda r: r.get("status") == "retroarch_core_missing"),
    ("not_applicable_host", lambda r: r.get("status") == "not_applicable_host"),
)


def platform_has_content(subdir: str) -> bool:
    """True when roms/<subdir> holds anything besides ES-DE *_guide.txt placeholders (no hashing)."""
    root = ROOT / "roms" / subdir
    if not subdir or root.is_symlink() or not root.is_dir():
        return False
    for _current, _dirs, files in os.walk(root, followlinks=False):
        if any(not name.startswith(".") and not name.endswith("_guide.txt") for name in files):
            return True
    return False


def platform_runtime_status(runtimes: list[dict]) -> str:
    for status, matches in PLATFORM_STATUS_PRECEDENCE:
        if any(matches(runtime) for runtime in runtimes):
            return status
    return "runtime_unavailable"


def audit(catalog: dict, profile: dict, requested_ids: list[str] | None) -> dict[str, object]:
    systems = catalog.get("systems", [])
    if not isinstance(systems, list):
        raise RuntimeError("O catálogo de plataformas não contém uma lista systems.")
    profile_collections = profile.get("collections", {})
    if not isinstance(profile_collections, dict):
        raise RuntimeError("host-linux.json não contém collections como objeto.")

    flatpaks, flatpak_query_ok = installed_flatpaks()
    ra = profile.get("retroarch", {})
    core_root = resolve_path(ra.get("core_root", ""), "RETROFE_RETROARCH_CORE_ROOT")
    mame_executable = mame_path(profile)
    profiles_by_root: dict[str, list[tuple[str, dict]]] = {}
    for collection, entry in profile_collections.items():
        subdir = str(entry.get("rom_subdir", "")).casefold()
        if subdir:
            profiles_by_root.setdefault(subdir, []).append((collection, entry))

    wanted = {value.casefold() for value in requested_ids or []}
    rows: list[dict[str, object]] = []
    for platform in systems:
        system_id = str(platform.get("id", ""))
        if wanted and system_id.casefold() not in wanted:
            continue
        subdir = str(platform.get("rom_subdir", system_id))
        matches = profiles_by_root.get(subdir.casefold(), [])
        profile_summaries: list[dict[str, object]] = []
        for collection, entry in matches:
            # Same rule as the launcher: an explicit collection core_root is relative to frontend/RetroFE.
            entry_core_root = (RETROFE_ROOT / str(entry["core_root"])).resolve() if entry.get("core_root") else core_root
            runtime = runtime_probe(collection, entry, profile, entry_core_root, flatpaks, flatpak_query_ok, mame_executable)
            profile_summaries.append({
                "collection": collection,
                "adapter": entry.get("adapter", ""),
                "launcher": entry.get("launcher", ""),
                "profile_status": entry.get("status", ""),
                "reason": entry.get("reason", ""),
                "runtime_candidate": entry.get("runtime_candidate", ""),
                "runtime_candidate_source": entry.get("runtime_candidate_source", ""),
                "runtime_options": runtime.get("runtime_options", []),
                "runtime_selection": runtime.get("runtime_selection", {}),
                "onboarding": runtime.get("onboarding", {"status": "not_declared", "defaults": []}),
                "portability": portability_summary(collection, entry, profile, entry_core_root),
                "extensions": [part.strip().lstrip(".").casefold() for part in str(entry.get("extensions", "")).split(",") if part.strip()],
                "runtime": runtime,
            })

        kind = str(platform.get("kind", "platform"))
        if kind == "support_tools":
            status = "not_game_platform"
        elif kind == "unclassified":
            status = "catalog_unclassified"
        elif not profile_summaries:
            # F06: separate "content waiting for a profile" from an empty catalog slot.
            status = "profile_missing" if platform_has_content(subdir or system_id) else "no_profile_no_content"
        else:
            status = platform_runtime_status([item["runtime"] for item in profile_summaries])

        rows.append({
            "system_id": system_id,
            "display_name": platform.get("display_name", system_id),
            "kind": kind,
            "rom_subdir": subdir,
            "emulator_hints": platform.get("emulator_hints", []),
            "preferred_retroarch_cores": platform.get("preferred_retroarch_cores", []),
            "status": status,
            "profiles": profile_summaries,
        })

    return {
        "tool": "emutool",
        "tool_version": VERSION,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "project_root": ".",
        "sources": {
            "platform_catalog": str(CATALOG_PATH.relative_to(ROOT)),
            "host_profile": str(PROFILE_PATH.relative_to(ROOT)),
        },
        "runtime_checks": {
            "flatpak_query_ok": flatpak_query_ok,
            "installed_flatpaks": flatpaks,
            "mame_executable": mame_executable or "",
            "launch_tested": False,
            "portability_definition": "runtime binaries, emulator state and required assets resolve inside the project; Flatpak remains a host runtime dependency",
        },
        "systems": rows,
    }


def render_table(report: dict[str, object]) -> str:
    rows = report["systems"]
    headers = ("SYSTEM_ID", "COLLECTION", "ADAPTER", "RUNTIME", "STATUS", "ALTERNATIVES", "PORTABILITY", "ONBOARDING")
    data: list[tuple[str, ...]] = []
    for system in rows:
        profiles = system["profiles"]
        if profiles:
            for entry in profiles:
                runtime = entry["runtime"]
                alternatives = ",".join(
                    f"{candidate.get('runtime_id')}:{candidate.get('status')}"
                    for candidate in entry.get("runtime_options", [])
                )
                data.append((
                    str(system["system_id"]),
                    str(entry["collection"]),
                    str(entry["adapter"]),
                    str(runtime.get("runtime") or runtime.get("candidate_missing") or ""),
                    str(runtime.get("status", system["status"])),
                    alternatives,
                    str(entry.get("portability", {}).get("status", "not_assessed")),
                    str(runtime.get("onboarding", {}).get("status", "not_declared")),
                ))
        else:
            data.append((str(system["system_id"]), "", "", "", str(system["status"]), "", "not_assessed", "not_declared"))
    widths = [max(len(headers[index]), *(len(row[index]) for row in data)) for index in range(len(headers))]
    lines = ["  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    lines.extend("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in data)
    lines.append("")
    lines.append("runtime_ready_unverified significa que o adaptador existe; não comprova abertura do jogo.")
    return "\n".join(lines)


def render_csv(report: dict[str, object]) -> str:
    stream = StringIO(newline="")
    fields = ["system_id", "display_name", "kind", "rom_subdir", "status", "collection", "adapter",
              "runtime_status", "runtime", "runtime_alternatives", "runtime_selection_count",
              "portability_status", "portability_external_dependency", "onboarding_status", "reason"]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for system in report["systems"]:
        profiles = system["profiles"] or [{}]
        for entry in profiles:
            runtime = entry.get("runtime", {})
            writer.writerow({
                "system_id": system["system_id"],
                "display_name": system["display_name"],
                "kind": system["kind"],
                "rom_subdir": system["rom_subdir"],
                "status": system["status"],
                "collection": entry.get("collection", ""),
                "adapter": entry.get("adapter", ""),
                "runtime_status": runtime.get("status", ""),
                "runtime": runtime.get("runtime", ""),
                "runtime_alternatives": ";".join(
                    f"{candidate.get('runtime_id')}:{candidate.get('status')}"
                    for candidate in entry.get("runtime_options", [])
                ),
                "runtime_selection_count": entry.get("runtime_selection", {}).get("title_count", ""),
                "portability_status": entry.get("portability", {}).get("status", ""),
                "portability_external_dependency": entry.get("portability", {}).get("external_dependency", ""),
                "onboarding_status": runtime.get("onboarding", {}).get("status", "not_declared"),
                "reason": entry.get("reason", ""),
            })
    return stream.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audita perfis e aplica defaults de onboarding declarados.")
    parser.add_argument("--system-id", nargs="+", help="filtra por IDs do catálogo canônico")
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table")
    parser.add_argument("--strict", action="store_true", help="retorna código 1 se houver perfil/runtime bloqueado")
    parser.add_argument("--strict-portability", action="store_true", help="retorna código 1 se um perfil depender de dados/runtime fora do projeto")
    parser.add_argument("--apply-onboarding", metavar="COLLECTION", help="aplica somente defaults de onboarding declarados e ausentes")
    parser.add_argument("--onboarding-guide", metavar="COLLECTION", help="exibe o guia de primeiro uso sem alterar arquivos")
    subparsers = parser.add_subparsers(dest="command")
    library_parser = subparsers.add_parser('library', help='centraliza runtimes em emulator/ com plano e rollback')
    library_actions = library_parser.add_subparsers(dest='library_action', required=True)
    library_actions.add_parser('plan')
    library_actions.add_parser('windows-config-plan')
    library_actions.add_parser('model2-config-plan')
    library_actions.add_parser('supermodel-data-plan')
    vita_data = library_actions.add_parser('vita3k-data-plan')
    vita_data.add_argument('--operation-id', help='retoma preparação portátil interrompida')
    vita_game = library_actions.add_parser('vita3k-game-plan')
    vita_game.add_argument('--app-directory', required=True, type=Path)
    vita_game.add_argument('--patch-directory', type=Path)
    vita_game.add_argument('--descriptor', required=True, type=Path)
    vita_game.add_argument('--operation-id', help='retoma a mesma seleção interrompida')
    cbios_plan = library_actions.add_parser('bluemsx-cbios-plan')
    cbios_plan.add_argument('--operation-id', help='retoma aquisição interrompida da mesma receita')
    cbios_plan.add_argument('--from-directory', type=Path, help='usa os arquivos oficiais locais fixados por SHA-256')
    library_actions.add_parser('dolphin-data-plan')
    library_actions.add_parser('dolphin-config-plan')
    for action in ('apply', 'verify', 'rollback'):
        entry = library_actions.add_parser(action)
        entry.add_argument('--operation-id', required=True)
        if action != 'verify':
            entry.add_argument('--apply', action='store_true', required=True)
    controls_parser = subparsers.add_parser("controls", help="planeja e aplica controles compartilhados entre frontends")
    controls_actions = controls_parser.add_subparsers(dest="controls_action", required=True)
    controls_actions.add_parser("plan", help="grava plano com hashes, preimagens e escopo de configuração")
    controls_actions.add_parser("diagnose", help="diagnóstico de controles, dock, hotplug e saída, sem alterar configurações")
    for action in ("apply", "verify", "rollback"):
        controls_action = controls_actions.add_parser(action)
        controls_action.add_argument("--operation-id", required=True)
        if action != "verify":
            controls_action.add_argument("--apply", action="store_true", required=True)
    runtime_parser = subparsers.add_parser("runtime", help="lista e seleciona runtimes por Title ID")
    runtime_actions = runtime_parser.add_subparsers(dest="runtime_action", required=True)
    runtime_prefer_parser=runtime_actions.add_parser('prefer',help='planeja/aplica preferência da plataforma, preservando escolhas por Title ID')
    runtime_prefer_parser.add_argument('--system-id',required=True)
    runtime_prefer_parser.add_argument('--runtime',required=True)
    runtime_prefer_parser.add_argument('--apply',action='store_true')
    runtime_list_parser = runtime_actions.add_parser("list", help="sonda runtimes e mostra seleções por jogo")
    runtime_list_parser.add_argument("--system-id", default="ps5")
    runtime_list_parser.add_argument("--item", help="pasta de jogo sob roms/<system_id>/ com identidade interna")
    runtime_set_parser = runtime_actions.add_parser("set", help="seleciona runtime para um jogo por identidade interna")
    runtime_set_parser.add_argument("--system-id", default="ps5")
    runtime_set_parser.add_argument("--item", required=True)
    runtime_set_parser.add_argument("--runtime", required=True)
    runtime_clear_parser = runtime_actions.add_parser("clear", help="restaura o runtime padrão para um Title ID")
    runtime_clear_parser.add_argument("--system-id", default="ps5")
    runtime_clear_parser.add_argument("--item", required=True)

    firmware_parser = subparsers.add_parser("firmware", help="diagnostica e prepara firmware PS3 oficial")
    firmware_actions = firmware_parser.add_subparsers(dest="firmware_action", required=True)
    firmware_actions.add_parser("status", help="verifica PUP canônico/legado e firmware RPCS3 importado")
    firmware_download_parser = firmware_actions.add_parser("download", help="baixa o PUP apenas da página oficial e valida hash")
    firmware_download_parser.add_argument("--apply", action="store_true", required=True)
    firmware_setup_parser = firmware_actions.add_parser("setup", help="reloca o PUP planejado e importa firmware se necessário")
    firmware_setup_parser.add_argument("--apply", action="store_true")
    firmware_rollback_parser = firmware_actions.add_parser("rollback", help="move uma instalação/download criado pelo emutool para rollback")
    firmware_rollback_parser.add_argument("--operation-id", required=True)
    firmware_rollback_parser.add_argument("--apply", action="store_true")
    install_parser = subparsers.add_parser("install", help="instala builds portáteis oficiais fixados em emulator/<SO>")
    install_actions = install_parser.add_subparsers(dest="install_action", required=True)
    platform_parser=install_actions.add_parser('platform',help='instala e integra Xbox 360/alternativas PS5/Switch2 aos frontends; plano sem --apply')
    platform_parser.add_argument('--system-id',nargs='+',required=True,choices=['xbox360','ps5','switch2'])
    platform_parser.add_argument('--apply',action='store_true')
    install_actions.add_parser("status", help="mostra receitas, estado instalado e download manual")
    install_actions.add_parser("scan", help="localiza emuladores do host (Flatpak/PATH) e o vínculo com o perfil; só leitura")
    package_parser = install_actions.add_parser('package', help='pacote nativo completo, com todos os membros verificados')
    package_parser.add_argument('--platform', choices=['linux', 'windows'], required=True)
    package_parser.add_argument('--runtime-id', required=True)
    package_parser.add_argument('--from-file', type=Path)
    package_parser.add_argument('--apply', action='store_true')
    package_parser.add_argument('--replace-existing', action='store_true')
    install_apply_parser = install_actions.add_parser("apply", help="baixa/verifica e instala um runtime; exige --apply")
    install_apply_parser.add_argument("--runtime-id", required=True)
    install_apply_parser.add_argument('--platform', choices=['linux', 'windows'], default='linux')
    install_apply_parser.add_argument("--from-file", type=Path, help="arquivo oficial já baixado (verificado pelo SHA-256 da receita)")
    install_apply_parser.add_argument("--apply", action="store_true")
    install_apply_parser.add_argument("--allow-host-duplicate", action="store_true", help="instala mesmo com o emulador presente no host")
    install_apply_parser.add_argument('--defer-history-index', action='store_true',
                                      help='registra a instalação agora e adia o índice integral para romtool history')
    install_asset_parser = install_actions.add_parser("asset", help="instala arquivo de suporte fixado (não sobrescreve); exige --apply")
    install_asset_parser.add_argument("--asset-id", required=True)
    install_asset_parser.add_argument("--apply", action="store_true")
    install_rollback_parser = install_actions.add_parser("rollback", help="desfaz uma instalação portátil; exige --rollback")
    install_rollback_parser.add_argument("--operation-id", required=True)
    install_rollback_parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.command == 'library':
        try:
            result = (runtime_install.vita3k_game_plan(ROOT, args.app_directory.absolute(),
                                                     args.descriptor.absolute(),
                                                     args.patch_directory.absolute() if args.patch_directory else None,
                                                     args.operation_id)
                      if args.library_action == 'vita3k-game-plan' else
                      runtime_install.vita3k_data_plan(ROOT, args.operation_id)
                      if args.library_action == 'vita3k-data-plan' else
                      runtime_install.bluemsx_cbios_plan(ROOT, args.operation_id, args.from_directory)
                      if args.library_action == 'bluemsx-cbios-plan' else
                      runtime_install.supermodel_data_plan(ROOT) if args.library_action == 'supermodel-data-plan' else
                      runtime_install.model2_configuration_plan(ROOT) if args.library_action == 'model2-config-plan' else
                      runtime_install.dolphin_data_plan(ROOT, config_only=args.library_action == 'dolphin-config-plan')
                      if args.library_action in ('dolphin-data-plan', 'dolphin-config-plan') else
                      runtime_install.native_configuration_plan(ROOT) if args.library_action == 'windows-config-plan' else
                      runtime_install.library_plan(ROOT) if args.library_action == 'plan' else
                      runtime_install.library_execute(ROOT, args.operation_id, args.library_action,
                                                      getattr(args, 'apply', False)))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "controls":
        import emulator_controls
        if args.controls_action == 'diagnose':
            import controller_session
            print(json.dumps(controller_session.diagnose(ROOT), ensure_ascii=False, indent=2))
            return 0
        try:
            result = (emulator_controls.plan(ROOT) if args.controls_action == "plan" else
                      emulator_controls.execute(ROOT, args.operation_id, args.controls_action))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result.get("status") == "diverged" else 0
        except (RuntimeError, OSError, ValueError) as error:
            print(f"emutool: configuração de controles bloqueada: {error}", file=sys.stderr)
            return 2
    if args.command == "install":
        try:
            if args.install_action == 'package':
                result = runtime_install.install_package(ROOT, args.runtime_id, args.platform, args.apply,
                                                         args.from_file, args.replace_existing)
            elif args.install_action == 'platform':
                import runtime_setup
                result=runtime_setup.configure(ROOT,args.system_id,apply=args.apply,install=args.apply)
            elif args.install_action in {"status", "scan"}:
                result = runtime_install.runtime_status(ROOT)
                if args.install_action == "scan":
                    result = {"host_flatpak_query_ok": result["host_flatpak_query_ok"], "emulators": [
                        {k: row[k] for k in ("runtime_id", "display_name", "decision", "status", "host_sources", "used_by")}
                        for row in result["runtimes"]]}
            elif args.install_action == "apply":
                result = runtime_install.install_runtime(ROOT, args.runtime_id, args.apply, args.from_file,
                                                         args.allow_host_duplicate, target_platform=args.platform,
                                                         defer_history_index=args.defer_history_index)
            elif args.install_action == "asset":
                result = runtime_install.install_asset(ROOT, args.asset_id, args.apply)
            else:
                result = runtime_install.rollback_install(ROOT, args.operation_id, args.rollback)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except runtime_install.HostDuplicate as error:
            print(f"emutool: emulador já presente no host: {error}", file=sys.stderr)
            return 4
        except runtime_install.ManualDownloadRequired as error:
            print(f"emutool: download manual necessário: {error}", file=sys.stderr)
            return 3
        except (RuntimeError, OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
            print(f"emutool: instalação bloqueada: {error}", file=sys.stderr)
            return 2
        return 0
    if args.command == "runtime":
        try:
            if args.runtime_action == "list":
                result = runtime_list(args.system_id, args.item)
            elif args.runtime_action == "set":
                result = runtime_set(args.system_id, args.item, args.runtime)
            elif args.runtime_action == 'prefer':
                result=runtime_prefer(args.system_id,args.runtime,args.apply)
            else:
                result = runtime_clear(args.system_id, args.item)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (RuntimeError, OSError, ValueError, UnicodeError) as error:
            print(f"emutool: seleção de runtime bloqueada: {error}", file=sys.stderr)
            return 2
        return 0
    if args.command == "firmware":
        try:
            if args.firmware_action == "status":
                result = ps3_firmware_status()
            elif args.firmware_action == "download":
                result = download_ps3_firmware()
            elif args.firmware_action == "setup":
                result = ps3_firmware_setup(args.apply)
            else:
                result = ps3_firmware_rollback(args.operation_id, args.apply)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("status") in {"blocked_retrofe_active", "blocked_rpcs3_active", "relocation_blocked"}:
                return 2
        except (RuntimeError, OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
            print(f"emutool: fluxo de firmware bloqueado: {error}", file=sys.stderr)
            return 2
        return 0
    if args.onboarding_guide:
        try:
            profile = read_json(PROFILE_PATH)
            system = profile.get("collections", {}).get(args.onboarding_guide)
            if not isinstance(system, dict):
                raise RuntimeError(f"A coleção {args.onboarding_guide!r} não existe em host-linux.json.")
            guide = _onboarding_guide(system)
            if guide is None:
                print(json.dumps({"collection": args.onboarding_guide, "status": "not_declared", "guide": None}, ensure_ascii=False, indent=2))
            else:
                probe = onboarding_probe(system, profile)
                print(json.dumps({"collection": args.onboarding_guide, "status": probe["status"], "guide": guide}, ensure_ascii=False, indent=2))
        except (RuntimeError, OSError, ValueError, UnicodeError) as error:
            print(f"emutool: guia de onboarding indisponível: {error}", file=sys.stderr)
            return 2
        return 0
    if args.apply_onboarding:
        try:
            print(json.dumps(apply_onboarding(args.apply_onboarding), ensure_ascii=False, indent=2))
        except (RuntimeError, OSError, ValueError, UnicodeError) as error:
            print(f"emutool: onboarding bloqueado: {error}", file=sys.stderr)
            return 2
        return 0
    try:
        report = audit(read_json(CATALOG_PATH), read_json(PROFILE_PATH), args.system_id)
    except (RuntimeError, OSError) as error:
        print(f"emutool: {error}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        print(render_csv(report), end="")
    else:
        print(render_table(report))

    if args.strict and any(
        system["status"] not in {"runtime_ready_unverified", "not_applicable_host", "not_game_platform"}
        for system in report["systems"]
    ):
        return 1
    if args.strict_portability and any(
        entry.get("portability", {}).get("status") != "project_local"
        for system in report["systems"] for entry in system["profiles"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
