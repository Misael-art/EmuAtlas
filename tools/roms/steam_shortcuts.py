"""Steam non-Steam shortcut registration for the canonical frontends."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import stat
import struct
import sys
import tempfile
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MANAGED_TAG = "emulation-project"

FRONTENDS = {
    "retrofe": {
        "name": "RetroFE",
        "launcher_rel": "frontend/RetroFE/Tools/launchers/retrofe/launch-retrofe.sh",
    },
    "esde": {
        "name": "ES-DE",
        "launcher_rel": "frontend/ES-DE/launch.sh",
    },
}

_TYPE_OBJECT = 0x00
_TYPE_STRING = 0x01
_TYPE_INT32 = 0x02
_TYPE_END = 0x08


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise ValueError("shortcuts.vdf truncado: payload VDF binário incompleto")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def cstring(self) -> str:
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise ValueError("shortcuts.vdf truncado: string sem terminador nulo")
        value = self.data[self.pos:end].decode("utf-8")
        self.pos = end + 1
        return value

    def object(self) -> dict:
        result: dict = {}
        while True:
            type_byte = self.take(1)[0]
            if type_byte == _TYPE_END:
                return result
            key = self.cstring()
            if key in result:
                raise ValueError(f"shortcuts.vdf inválido: chave VDF duplicada: {key!r}")
            if type_byte == _TYPE_OBJECT:
                result[key] = self.object()
            elif type_byte == _TYPE_STRING:
                result[key] = self.cstring()
            elif type_byte == _TYPE_INT32:
                result[key] = struct.unpack("<I", self.take(4))[0]
            else:
                raise ValueError(f"shortcuts.vdf inválido: tipo VDF binário desconhecido 0x{type_byte:02x}")


def parse_shortcuts_vdf(data: bytes) -> dict:
    reader = _Reader(data)
    doc = reader.object()
    if reader.pos != len(data):
        raise ValueError("shortcuts.vdf inválido: bytes restantes após o objeto raiz")
    _validate_shortcuts_document(doc)
    return doc


def _validate_shortcuts_document(doc: dict) -> None:
    shortcuts = doc.get("shortcuts")
    if not isinstance(shortcuts, dict):
        raise ValueError("shortcuts.vdf inválido: objeto shortcuts ausente ou incorreto")
    indexes = set()
    for key, entry in shortcuts.items():
        if not key.isascii() or not key.isdecimal() or not isinstance(entry, dict):
            raise ValueError(f"shortcuts.vdf inválido: índice ou entrada inválida: {key!r}")
        number = int(key)
        if number in indexes:
            raise ValueError(f"shortcuts.vdf inválido: índice duplicado: {key!r}")
        indexes.add(number)


def _write_object(out: bytearray, value: dict) -> None:
    for key, item in value.items():
        encoded_key = key.encode("utf-8") + b"\x00"
        if isinstance(item, dict):
            out += bytes((_TYPE_OBJECT,)) + encoded_key
            _write_object(out, item)
        elif isinstance(item, int):
            out += bytes((_TYPE_INT32,)) + encoded_key + struct.pack(
                "<i" if item < 0 else "<I", item)
        elif isinstance(item, str):
            out += bytes((_TYPE_STRING,)) + encoded_key + item.encode("utf-8") + b"\x00"
        else:
            raise ValueError(f"Valor VDF não suportado: {type(item).__name__}")
    out += bytes((_TYPE_END,))


def serialize_shortcuts_vdf(doc: dict) -> bytes:
    _validate_shortcuts_document(doc)
    out = bytearray()
    _write_object(out, doc)
    return bytes(out)


def _crc32(exe: str, name: str) -> int:
    return zlib.crc32((exe + name).encode("utf-8")) & 0xFFFFFFFF


def vdf_appid(exe: str, name: str) -> int:
    crc = _crc32(exe, name)
    return crc - 0x100000000 if crc >= 0x80000000 else crc


def shortcut_id64(exe: str, name: str) -> int:
    return ((_crc32(exe, name) | 0x80000000) << 32) | 0x02000000


def build_shortcut_entry(name: str, exe: str, start_dir: str) -> dict:
    quoted_exe = f"\"{exe}\""
    return {
        "appid": vdf_appid(quoted_exe, name),
        "AppName": name,
        "Exe": quoted_exe,
        "StartDir": f"\"{start_dir}\"",
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": "",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 0,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "LastPlayTime": 0,
        "tags": {"0": MANAGED_TAG},
    }


def is_managed_entry(entry: dict) -> bool:
    tags = entry.get("tags")
    return isinstance(tags, dict) and MANAGED_TAG in tags.values()


def merge_entries(doc: dict, new_entries: dict) -> dict:
    merged = copy.deepcopy(doc)
    shortcuts = merged.setdefault("shortcuts", {})
    _validate_shortcuts_document(merged)
    for name, entry in new_entries.items():
        matches = [index for index, current in shortcuts.items()
                   if current.get("AppName") == name]
        if len(matches) > 1:
            raise ValueError(f"Entradas duplicadas para {name!r}; reconciliação manual necessária.")
        for index in matches:
            current = shortcuts[index]
            if not is_managed_entry(current):
                raise ValueError(
                    f"Entrada não gerenciada com o nome '{name}' já existe no Steam; "
                    "preservada sem sobrescrita. Renomeie-a no cliente antes de aplicar.")
            if current.get("Exe") != entry["Exe"] or (
                not isinstance(current.get("appid"), int) or
                current["appid"] & 0xFFFFFFFF != entry["appid"] & 0xFFFFFFFF
            ):
                raise ValueError(
                    f"Identidade Steam de {name!r} diverge (Exe/AppID). "
                    "Preservada para revisão explícita do atalho e dos controles.")
            # Keep Steam's index, identity, input configuration and user customizations.
            current["StartDir"] = entry["StartDir"]
        if not matches:
            next_index = 0
            while str(next_index) in shortcuts:
                next_index += 1
            shortcuts[str(next_index)] = copy.deepcopy(entry)
    return merged


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


@contextmanager
def _operation_lock(api):
    """Serialize writes made by this tool without touching Steam processes."""
    lock = Path(api["OPS_ROOT"]) / ".steam-shortcuts.lock"
    if lock.is_symlink():
        raise ValueError(f"Lock inseguro (symlink): {lock}")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+b") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"Lock inseguro (não regular): {lock}")
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _atomic_binary(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Durable same-directory replace, preserving original file permissions."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), stat.S_IMODE(mode))
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save_manifest(path: Path, value: dict) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_binary(path, payload, mode=0o600)


def _steam_input_module():
    helper_dir = Path(__file__).resolve().parents[2] / "frontend/RetroFE/Tools/launchers/retrofe"
    if str(helper_dir) not in sys.path:
        sys.path.insert(0, str(helper_dir))
    return importlib.import_module("steam-input")


def detect_steam_default():
    return _steam_input_module().find_steam()


def frontends_running_default() -> bool:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip().casefold()
        except OSError:
            continue
        if comm == "retrofe":
            return True
        if comm.startswith("es-de"):
            return True
    return False


def _shortcuts_path(install) -> Path:
    userdata = Path(install.root) / "userdata"
    accounts = sorted(path for path in userdata.iterdir()
                      if path.is_dir() and path.name.isdigit()) if userdata.is_dir() else []
    if len(accounts) != 1:
        raise ValueError(
            f"Era esperada exatamente uma conta Steam em {userdata}; encontradas {len(accounts)}. "
            "Resolva a conta alvo antes de cadastrar atalhos.")
    return accounts[0] / "config" / "shortcuts.vdf"


def _read_shortcuts(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"shortcuts.vdf precisa ser arquivo regular, sem symlink: {path}")
    if not path.exists():
        return {"shortcuts": {}}
    if not path.is_file():
        raise ValueError(f"shortcuts.vdf precisa ser arquivo regular, sem symlink: {path}")
    return parse_shortcuts_vdf(path.read_bytes())


def _file_state(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"shortcuts.vdf é symlink: {path}")
    if not path.exists():
        return {"exists": False, "sha256": "", "size_bytes": 0}
    if not path.is_file():
        raise ValueError(f"shortcuts.vdf não é arquivo regular: {path}")
    info = path.stat()
    return {"exists": True, "sha256": sha256_file(path), "size_bytes": info.st_size,
            "mode": stat.S_IMODE(info.st_mode)}


def _same_file_state(a: dict, b: dict) -> bool:
    return (a["exists"] == b["exists"] and a["sha256"] == b["sha256"]
            and ("mode" not in a or "mode" not in b or a["mode"] == b["mode"]))


def _selected_frontends(args) -> dict:
    selected = getattr(args, "frontend", None)
    if selected is None:
        return FRONTENDS
    if selected not in FRONTENDS:
        raise ValueError(f"Frontend desconhecido: {selected}")
    return {selected: FRONTENDS[selected]}


def _launcher_evidence(api, frontends: dict) -> dict:
    evidence = {}
    for key, spec in frontends.items():
        launcher = Path(api["PROJECT_ROOT"]) / spec["launcher_rel"]
        if not launcher.is_file() or launcher.is_symlink():
            raise FileNotFoundError(f"launcher canônico ausente para {key}: {launcher}")
        if not os.access(launcher, os.X_OK):
            raise PermissionError(f"launcher não executável para {key}: {launcher}")
        evidence[key] = {
            "name": spec["name"],
            "launcher": str(launcher),
            "launcher_sha256": sha256_file(launcher),
            "exe": f"\"{launcher}\"",
            "start_dir": f"\"{launcher.parent}\"",
            "appid": vdf_appid(f"\"{launcher}\"", spec["name"]),
            "shortcut_id64": shortcut_id64(f"\"{launcher}\"", spec["name"]),
        }
    return evidence


def _load_manifest(api, operation_id: str) -> tuple[Path, dict]:
    if not operation_id or not re.fullmatch(r"[A-Za-z0-9._-]+", operation_id):
        raise ValueError(f"ID de operação inválido: {operation_id!r}")
    manifest_path = Path(api["OPS_ROOT"]) / operation_id / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Operação não encontrada: {operation_id}")
    return manifest_path, json.loads(manifest_path.read_text(encoding="utf-8"))


def command_plan(args, api) -> dict:
    detect = api.get("detect_steam") or detect_steam_default
    install = detect()
    if install is None:
        raise RuntimeError("Steam não encontrado no host; instale pelo fluxo oficial antes de cadastrar atalhos.")
    shortcuts = _shortcuts_path(install)
    frontends = _selected_frontends(args)
    evidence = _launcher_evidence(api, frontends)
    doc = _read_shortcuts(shortcuts)
    new_entries = {item["name"]: build_shortcut_entry(item["name"], item["launcher"],
                                                      str(Path(item["launcher"]).parent))
                   for item in evidence.values()}
    desired = merge_entries(doc, new_entries)
    operation_id = f"steam-shortcuts-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = Path(api["OPS_ROOT"]) / operation_id
    opdir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "1.0",
        "operation_id": operation_id,
        "kind": "steam_shortcuts",
        "created_at": api["now"](),
        "status": "planned",
        "read_only": True,
        "steam_kind": install.kind,
        "steam_root": str(install.root),
        "shortcuts_path": str(shortcuts),
        "shortcuts_preimage": _file_state(shortcuts),
        "frontends": evidence,
        "desired_document": desired,
    }
    manifest_path = opdir / "manifest.json"
    _save_manifest(manifest_path, manifest)
    api["append_event"](operation_id, "steam_shortcuts_planned", action="steam_shortcuts_plan",
                        status="planned", destination=str(shortcuts),
                        details={"frontends": list(frontends)})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": "planned", "manifest": str(manifest_path),
            "shortcuts_path": str(shortcuts), "frontends": list(frontends)}


def command_apply(args, api) -> dict:
    if not getattr(args, "apply", False):
        raise ValueError("Apply exige --apply após revisão do manifesto.")
    with _operation_lock(api):
        return _command_apply_locked(args, api)


def _command_apply_locked(args, api) -> dict:
    manifest_path, manifest = _load_manifest(api, args.operation_id)
    if manifest["status"] not in {"planned", "applying"}:
        raise ValueError(f"Operação não está planejada: {manifest['status']}")
    steam_running = api.get("steam_process_running")
    if steam_running is None:
        steam_running = _steam_input_module().process_running
    if steam_running():
        raise RuntimeError("Steam está aberto; feche o cliente normalmente antes de aplicar (sem reinício forçado).")
    frontends_running = api.get("frontends_running") or frontends_running_default
    if frontends_running():
        raise RuntimeError("Um frontend está aberto; feche RetroFE/ES-DE antes de aplicar.")
    for item in manifest["frontends"].values():
        launcher = Path(item["launcher"])
        if (launcher.is_symlink() or not launcher.is_file() or
                not os.access(launcher, os.X_OK) or sha256_file(launcher) != item["launcher_sha256"]):
            raise RuntimeError(f"launcher mudou desde o plano; reanalise: {launcher}")
    shortcuts = Path(manifest["shortcuts_path"])
    current = _file_state(shortcuts)
    preimage = manifest["shortcuts_preimage"]
    if manifest["status"] == "applying" and _same_file_state(current, manifest["expected_postimage"]):
        manifest.update(status="applied", completed_at=api["now"](), postimage=current)
        _save_manifest(manifest_path, manifest)
        api["append_event"](args.operation_id, "steam_shortcuts_applied", action="steam_shortcuts_apply",
                            status="applied", destination=str(shortcuts),
                            sha256_before=preimage["sha256"], sha256_after=current["sha256"])
        api["rebuild_history_index"]()
        return {"operation_id": args.operation_id, "status": "applied",
                "manifest": str(manifest_path), "changed": True, "recovered": True}
    if not _same_file_state(current, preimage):
        raise RuntimeError("shortcuts.vdf divergiu do plano; nenhum arquivo foi alterado. Gere novo plano.")
    opdir = manifest_path.parent
    doc = _read_shortcuts(shortcuts)
    new_entries = {item["name"]: build_shortcut_entry(item["name"], item["launcher"],
                                                      str(Path(item["launcher"]).parent))
                   for item in manifest["frontends"].values()}
    merged = merge_entries(doc, new_entries)
    if merged == doc:
        manifest.update(status="verified", completed_at=api["now"](),
                        postimage=current)
        _save_manifest(manifest_path, manifest)
        api["append_event"](args.operation_id, "steam_shortcuts_already_current",
                            action="steam_shortcuts_apply", status="verified",
                            destination=str(shortcuts))
        api["rebuild_history_index"]()
        return {"operation_id": args.operation_id, "status": "verified",
                "manifest": str(manifest_path), "changed": False}
    preimage_path = ""
    if preimage["exists"]:
        backup_dir = opdir / "rollback"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / "shortcuts.vdf.preimage"
        if backup.is_symlink():
            raise ValueError(f"Backup inseguro (symlink): {backup}")
        if not backup.exists():
            _atomic_binary(backup, shortcuts.read_bytes(), mode=0o600)
        if sha256_file(backup) != preimage["sha256"]:
            raise RuntimeError("Backup do shortcuts.vdf não corresponde ao hash do plano.")
        preimage_path = str(backup)
    payload = serialize_shortcuts_vdf(merged)
    expected_postimage = {"exists": True, "sha256": hashlib.sha256(payload).hexdigest(),
                          "size_bytes": len(payload), "mode": preimage.get("mode", 0o600)}
    manifest.update(status="applying", preimage_path=preimage_path,
                    expected_postimage=expected_postimage)
    _save_manifest(manifest_path, manifest)
    _atomic_binary(shortcuts, payload, mode=expected_postimage["mode"])
    postimage = _file_state(shortcuts)
    if not _same_file_state(postimage, expected_postimage):
        raise RuntimeError("Gravação do shortcuts.vdf divergiu do hash esperado; recuperar pelo manifesto.")
    manifest.update(status="applied", completed_at=api["now"](), preimage_path=preimage_path,
                    postimage=postimage)
    _save_manifest(manifest_path, manifest)
    api["append_event"](args.operation_id, "steam_shortcuts_applied", action="steam_shortcuts_apply",
                        status="applied", destination=str(shortcuts),
                        sha256_before=preimage["sha256"], sha256_after=postimage["sha256"],
                        size_before=preimage["size_bytes"], size_after=postimage["size_bytes"],
                        details={"frontends": list(manifest["frontends"]),
                                 "preimage_path": preimage_path})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": "applied",
            "manifest": str(manifest_path), "changed": True}


def command_verify(args, api) -> dict:
    manifest_path, manifest = _load_manifest(api, args.operation_id)
    if manifest["status"] not in {"applied", "verified"}:
        raise ValueError(f"Operação não foi aplicada: {manifest['status']}")
    shortcuts = Path(manifest["shortcuts_path"])
    current = _file_state(shortcuts)
    expected = manifest.get("postimage") or manifest["shortcuts_preimage"]
    issues = []
    if not _same_file_state(current, expected):
        issues.append("shortcuts.vdf atual difere do estado pós-apply")
    else:
        doc = _read_shortcuts(shortcuts)
        for item in manifest["frontends"].values():
            matches = [entry for entry in doc.get("shortcuts", {}).values()
                       if entry.get("AppName") == item["name"] and is_managed_entry(entry)
                       and entry.get("Exe") == item["exe"]]
            if not matches:
                issues.append(f"entrada gerenciada ausente/divergente: {item['name']}")
    status = "diverged" if issues else "verified"
    return {"operation_id": args.operation_id, "status": status, "issues": issues,
            "manifest": str(manifest_path)}


def command_rollback(args, api) -> dict:
    if not getattr(args, "rollback", False):
        raise ValueError("Rollback exige --rollback após revisão do manifesto.")
    with _operation_lock(api):
        return _command_rollback_locked(args, api)


def _command_rollback_locked(args, api) -> dict:
    manifest_path, manifest = _load_manifest(api, args.operation_id)
    if manifest["status"] not in {"applied", "verified", "applying"}:
        raise ValueError(f"Operação não foi aplicada: {manifest['status']}")
    steam_running = api.get("steam_process_running")
    if steam_running is None:
        steam_running = _steam_input_module().process_running
    if steam_running():
        raise RuntimeError("Steam está aberto; feche o cliente normalmente antes de reverter.")
    frontends_running = api.get("frontends_running") or frontends_running_default
    if frontends_running():
        raise RuntimeError("Um frontend está aberto; feche RetroFE/ES-DE antes de reverter.")
    shortcuts = Path(manifest["shortcuts_path"])
    current = _file_state(shortcuts)
    expected = manifest.get("postimage") or manifest.get("expected_postimage") or manifest["shortcuts_preimage"]
    if not (_same_file_state(current, expected) or
            (manifest["status"] == "applying" and _same_file_state(current, manifest["shortcuts_preimage"]))):
        raise RuntimeError("Mudanças posteriores detectadas no shortcuts.vdf; rollback recusado sem sobrescrita.")
    opdir = manifest_path.parent
    removed_path = ""
    if manifest["shortcuts_preimage"]["exists"]:
        backup = Path(manifest["preimage_path"])
        if backup.is_symlink() or not backup.is_file():
            raise RuntimeError("Backup de rollback ausente ou inseguro; arquivo atual preservado.")
        payload = backup.read_bytes()
        if hashlib.sha256(payload).hexdigest() != manifest["shortcuts_preimage"]["sha256"]:
            raise RuntimeError("Preimage de rollback corrompida; arquivo atual preservado.")
        _atomic_binary(shortcuts, payload, mode=manifest["shortcuts_preimage"].get("mode", 0o600))
    elif current["exists"]:
        rollback_dir = opdir / "rollback"
        rollback_dir.mkdir(exist_ok=True)
        removed = rollback_dir / "shortcuts.vdf.removed"
        if removed.exists() or removed.is_symlink():
            raise RuntimeError("Destino do rollback já ocupado; arquivo atual preservado.")
        os.replace(shortcuts, removed)
        dirfd = os.open(shortcuts.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        removed_path = str(removed)
    manifest.update(status="rolled_back", rolled_back_at=api["now"](), removed_path=removed_path)
    _save_manifest(manifest_path, manifest)
    api["append_event"](args.operation_id, "steam_shortcuts_rolled_back",
                        action="steam_shortcuts_rollback", status="rolled_back",
                        destination=str(shortcuts),
                        details={"removed_path": removed_path})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": "rolled_back",
            "manifest": str(manifest_path)}


def command_status(args, api) -> dict:
    detect = api.get("detect_steam") or detect_steam_default
    install = detect()
    if install is None:
        raise RuntimeError("Steam não encontrado no host.")
    shortcuts = _shortcuts_path(install)
    doc = _read_shortcuts(shortcuts)
    frontends = {}
    any_collision = False
    for key, spec in FRONTENDS.items():
        launcher = Path(api["PROJECT_ROOT"]) / spec["launcher_rel"]
        exe = f"\"{launcher}\""
        matches = [entry for entry in doc.get("shortcuts", {}).values()
                   if entry.get("AppName") == spec["name"]]
        # Steam stores appid as unsigned VDF int32, but tests or imported VDFs
        # may contain the same signed value; compare by its 32-bit representation.
        registered = (len(matches) == 1 and is_managed_entry(matches[0]) and
                      matches[0].get("Exe") == exe and
                      isinstance(matches[0].get("appid"), int) and
                      (matches[0]["appid"] & 0xFFFFFFFF) == (vdf_appid(exe, spec["name"]) & 0xFFFFFFFF))
        launcher_ready = (launcher.is_file() and not launcher.is_symlink() and
                          os.access(launcher, os.X_OK))
        divergent = bool(matches) and not registered
        any_collision |= divergent
        frontends[key] = {"name": spec["name"], "registered": registered,
                          "divergent": divergent, "launcher_ready": launcher_ready,
                          "launchable": registered and launcher_ready,
                          "appid": vdf_appid(exe, spec["name"]),
                          "shortcut_id64": shortcut_id64(exe, spec["name"])}
    state = ("diverged" if any_collision else
             "registered" if all(item["launchable"] for item in frontends.values()) else
             "not_configured" if not shortcuts.exists() else "incomplete")
    return {"status": state, "steam_root": str(install.root),
            "shortcuts_path": str(shortcuts), "shortcuts_exists": shortcuts.exists(),
            "frontends": frontends,
            "graphical_cycle_verified": False,
            "note": "Cadastro e executabilidade são verificáveis; foco, gamepad, jogo e retorno exigem aceite físico."}


def command_launch(args, api) -> dict:
    frontend = getattr(args, "frontend", None)
    if frontend not in FRONTENDS:
        raise ValueError("launch exige --frontend retrofe|esde.")
    detect = api.get("detect_steam") or detect_steam_default
    install = detect()
    if install is None:
        raise RuntimeError("Steam não encontrado no host.")
    spec = FRONTENDS[frontend]
    launcher = Path(api["PROJECT_ROOT"]) / spec["launcher_rel"]
    if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError(f"Launcher indisponível ou inseguro: {launcher}")
    exe = f"\"{launcher}\""
    shortcuts = _shortcuts_path(install)
    doc = _read_shortcuts(shortcuts)
    matches = [entry for entry in doc.get("shortcuts", {}).values()
               if entry.get("AppName") == spec["name"]]
    if not matches:
        raise RuntimeError(
            f"{spec['name']} não está cadastrado no Steam; execute steam-shortcuts plan e apply primeiro.")
    if (len(matches) != 1 or not is_managed_entry(matches[0]) or
            matches[0].get("Exe") != exe or
            not isinstance(matches[0].get("appid"), int) or
            (matches[0]["appid"] & 0xFFFFFFFF) != (vdf_appid(exe, spec["name"]) & 0xFFFFFFFF)):
        raise RuntimeError(
            f"Entrada gerenciada de {spec['name']} diverge do launcher canônico; "
            "novo plan/apply necessário antes de lançar.")
    steam_running = api.get("steam_process_running")
    if steam_running is None:
        steam_running = _steam_input_module().process_running
    if not steam_running():
        start = api.get("start_steam")
        if start is None:
            start = _steam_input_module().start_steam
        start(install)
    bp_wait = float(getattr(args, "bp_wait", 5.0))
    if not math.isfinite(bp_wait) or not 0 <= bp_wait <= 30:
        raise ValueError("--bp-wait deve estar entre 0 e 30 segundos.")
    launch_uri = api.get("launch_uri")
    if launch_uri is None:
        launch_uri = _steam_input_module().launch_uri
    sleep = api.get("sleep")
    if sleep is None:
        import time
        sleep = time.sleep
    game_id = shortcut_id64(exe, spec["name"])
    uris = ["steam://open/bigpicture", f"steam://rungameid/{game_id}"]
    launch_uri(install, uris[0])
    if bp_wait > 0:
        sleep(bp_wait)
    launch_uri(install, uris[1])
    return {"status": "launch_requested", "frontend": frontend, "shortcut_id64": game_id,
            "uris": uris,
            "big_picture_ready_confirmed": False,
            "note": "As URIs foram solicitadas após espera configurável; a UI do Big Picture, foco, controles e jogo ainda exigem confirmação física."}


def dispatch(args, api) -> dict:
    command = args.steam_shortcuts_command
    if command == "plan":
        return command_plan(args, api)
    if command == "apply":
        return command_apply(args, api)
    if command == "verify":
        return command_verify(args, api)
    if command == "rollback":
        return command_rollback(args, api)
    if command == "status":
        return command_status(args, api)
    if command == "launch":
        return command_launch(args, api)
    raise ValueError(f"Comando steam-shortcuts desconhecido: {command}")
