"""Temporary Steam Desktop mapping with durable recovery, using Python's stdlib.

The loopback Steam CEF API is internal. Fail closed when its contract changes.
Only AppID 413080 is selected; the user's original configuration URL is retained.
"""

from __future__ import annotations

import desktop_environment
import base64
import fcntl
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import sys
import time
import uuid
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path


APP_ID = 413080
STATE_NAME = "steam-desktop-session.json"
GAMEPAD_URL = "template://retrofe_steamdeck_gamepad.vdf"
SESSION_TOKEN_ENV = "RETROFE_STEAM_SESSION_TOKEN"


class SteamCdp:
    """Small, bounded WebSocket/CDP client restricted to Steam on loopback."""

    def __init__(self):
        port = int(os.environ.get("RETROFE_STEAM_CDP_PORT", "8080"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json", timeout=3) as response:
            pages = json.load(response)
        page = next((p for p in pages if p.get("title") == "SharedJSContext"), None)
        if not page:
            raise RuntimeError("Steam SharedJSContext não está disponível na API local.")
        url = urllib.parse.urlsplit(page["webSocketDebuggerUrl"])
        if url.scheme != "ws" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("Steam retornou WebSocket fora de loopback; conexão recusada.")
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=8)
        self.buffer = b""
        self.sequence = 0
        key = base64.b64encode(os.urandom(16)).decode()
        path = url.path + ("?" + url.query if url.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        header = b""
        while b"\r\n\r\n" not in header:
            part = self.sock.recv(4096)
            if not part or len(header) > 32768:
                self.close()
                raise RuntimeError("Handshake WebSocket Steam inválido.")
            header += part
        header, self.buffer = header.split(b"\r\n\r\n", 1)
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if b" 101 " not in header.split(b"\r\n", 1)[0] or expected not in header:
            self.close()
            raise RuntimeError("Steam recusou o handshake WebSocket.")

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read(self, count):
        while len(self.buffer) < count:
            part = self.sock.recv(max(4096, count - len(self.buffer)))
            if not part:
                raise RuntimeError("Steam encerrou a API local.")
            self.buffer += part
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result

    def _send(self, payload, opcode=1):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += b"\xfe" + struct.pack("!H", size)
        else:
            header += b"\xff" + struct.pack("!Q", size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def evaluate(self, expression):
        self.sequence += 1
        self._send(json.dumps({"id": self.sequence, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
            "userGesture": True, "timeout": 6000,
        }}).encode())
        deadline = time.monotonic() + 10
        fragments = b""
        while time.monotonic() < deadline:
            self.sock.settimeout(max(.1, deadline - time.monotonic()))
            first, second = self._read(2)
            opcode, size = first & 15, second & 127
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            if size > 4 * 1024 * 1024:
                raise RuntimeError("Resposta Steam excedeu o limite de tamanho.")
            mask = self._read(4) if second & 128 else None
            payload = self._read(size)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise RuntimeError("Steam fechou a conexão local.")
            if opcode == 9:
                self._send(payload, 10)
                continue
            if opcode not in {0, 1}:
                continue
            fragments += payload
            if not first & 128:
                continue
            message = json.loads(fragments)
            fragments = b""
            if message.get("id") != self.sequence:
                continue
            if "error" in message:
                raise RuntimeError(f"API Steam: {message['error']}")
            result = message.get("result", {})
            if result.get("exceptionDetails"):
                raise RuntimeError(f"API Steam: {result['exceptionDetails']}")
            return result.get("result", {}).get("value")
        raise RuntimeError("Steam não respondeu à solicitação local.")

    def controllers(self):
        configs = self.evaluate("""Promise.all(Array.from({length:16}, async (_,index) => {
            try {
                const c=await SteamClient.Input.GetConfigForAppAndController(413080,index);
                if (!c || !c.bConfigurationEnabled) return null;
                return {index:c.unControllerIndex,type:c.nControllerType,url:c.URL || '',
                    title:c.Title || '',gamepad:!!c.bUsesGamepad,keyboard:!!c.bUsesKeyboard,
                    mouse:!!c.bUsesMouse};
            } catch (_) {return null;}
        }))""")
        return [c for c in configs or [] if c is not None]

    def select(self, controller, url):
        # Client 1792618285 requires a numeric selection type as argument 5.
        # A boolean silently returns undefined and only logs a CEF error.
        self.evaluate("Promise.resolve(SteamClient.Input.SetSelectedConfigForApp(413080,"
                      f"{int(controller['index'])},{json.dumps(url)},false,1)).then(()=>true)")


def state_directory():
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "retrofe/input-session"


def proc_identity(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        if fields[0] in {"Z", "X", "x"}:
            return None
        return fields[19]
    except FileNotFoundError:
        return None
    except (OSError, IndexError) as error:
        # Missing process and unreadable process are different states. Never
        # restore Steam Input when a known PID cannot be identified safely.
        raise RuntimeError(f"Não foi possível verificar /proc/{pid}/stat; sessão preservada.") from error


def alive(owner):
    return owner.get("boot_id", boot_id()) == boot_id() and proc_identity(int(owner["pid"])) == owner["start_ticks"]


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def write_state(path, data):
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked_state():
    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / STATE_NAME
    lock = directory / (STATE_NAME + ".lock")
    if path.is_symlink() or lock.is_symlink():
        raise RuntimeError("Estado Steam é symlink; restauração automática recusada.")
    with lock.open("a+") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else None
        if state and state.get("schema_version") != 1:
            raise RuntimeError("Estado Steam desconhecido; arquivo de recuperação preservado.")
        yield path, state


def inspect():
    with SteamCdp() as client:
        return client.controllers()


def snapshot():
    with locked_state() as (_, state):
        return state


def steam_game_processes():
    result = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            env = dict(item.split(b"=", 1) for item in (proc / "environ").read_bytes().split(b"\0") if b"=" in item)
            app = env.get(b"SteamGameId") or env.get(b"SteamAppId")
            if app and app not in {b"0", b"413080"}:
                result.append(proc.name)
        except OSError:
            continue
    return result


def prepare(install, start_steam, process_running, *, allow_restart=True):
    """Bootstrap the local API once, never restart Steam during a game."""
    try:
        with SteamCdp() as client:
            client.controllers()
        return []
    except (OSError, ValueError, RuntimeError):
        pass
    if has_live_owners():
        raise RuntimeError("API Steam indisponível durante uma sessão ativa; encerre o frontend antes de reiniciar Steam.")
    if steam_game_processes():
        raise RuntimeError("Steam precisa habilitar sua API local. Encerre os jogos Steam antes desta primeira preparação.")
    if process_running() and not allow_restart:
        raise RuntimeError("A API local do Steam está indisponível. Prepare-a pelo launcher após encerrar os jogos; a sessão Steam em uso foi preservada.")
    marker = install.root / ".cef-enable-remote-debugging"
    if marker.is_symlink():
        raise RuntimeError("Marcador CEF é symlink; arquivo preservado.")
    created = not marker.exists()
    if created:
        marker.touch(mode=0o600, exist_ok=False)
    changes = ["steam-cef-loopback-marker-created"] if created else []
    if process_running():
        print("[STEAM INPUT] Preparação inicial: reiniciando Steam pelo encerramento normal.", flush=True)
        subprocess.run([*install.command, "-shutdown"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        deadline = time.monotonic() + 25
        while process_running() and time.monotonic() < deadline:
            time.sleep(.25)
        if process_running():
            raise RuntimeError("Steam não aceitou encerramento normal; feche-o manualmente para preparar o perfil.")
        changes.append("steam-restarted-for-local-controller-api")
    start_steam(install)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with SteamCdp() as client:
                client.controllers()
            return changes
        except (OSError, ValueError, RuntimeError):
            time.sleep(.5)
    raise RuntimeError("API local Steam indisponível; RetroFE não iniciará com o Desktop conflitante.")


def confirm(client, expected):
    deadline = time.monotonic() + 12
    stable_since = None
    next_selection = time.monotonic() + 2
    retries = 0
    observed = []
    while time.monotonic() < deadline:
        current = {(c["index"], c["type"]): c for c in client.controllers()}
        matches = all(all(current.get((c["index"], c["type"]), {}).get(key) == c[key]
                          for key in ("url", "gamepad", "keyboard", "mouse")) for c in expected)
        now = time.monotonic()
        if matches:
            stable_since = now if stable_since is None else stable_since
            if now - stable_since >= 1:
                return
        else:
            stable_since = None
            # Steam may finish loading its cache after accepting the first
            # selection. Reapply only to the exact still-connected target.
            if now >= next_selection and retries < 3:
                for controller in expected:
                    target = current.get((controller["index"], controller["type"]))
                    if target and target["url"] != controller["url"]:
                        client.select(target, controller["url"])
                retries += 1
                next_selection = now + 2
        observed = [{"index": c["index"], "type": c["type"],
                     "connected": (c["index"], c["type"]) in current,
                     "url_matches": current.get((c["index"], c["type"]), {}).get("url") == c["url"],
                     **{key: current.get((c["index"], c["type"]), {}).get(key)
                        for key in ("gamepad", "keyboard", "mouse")}} for c in expected]
        time.sleep(.15)
    raise RuntimeError("Steam não confirmou o perfil e suas saídas; restauração será tentada. "
                       + json.dumps({"observed": observed, "bounded_reselections": retries}))


def restore(client, path, state):
    # Steam can overwrite a successful selection while loading its controller
    # cache after login. Re-read/reapply and require a stable confirmation;
    # never discard the original state on a transient failure.
    deadline, stable_since = time.monotonic() + 30, None
    while time.monotonic() < deadline:
        connected = client.controllers()
        matches = True
        for saved in state["controllers"]:
            candidates = [c for c in connected if c["type"] == saved["type"]]
            exact = [c for c in candidates if c["index"] == saved["index"]]
            target = exact[0] if exact else (candidates[0] if len(candidates) == 1 else None)
            if target is None:
                raise RuntimeError("Controle ausente ou ambíguo: perfil Desktop salvo permanece pendente.")
            if any(target[k] != saved[k] for k in ("url", "gamepad", "keyboard", "mouse")):
                client.select(target, saved["url"])
                matches = False
        stable_since = (stable_since or time.monotonic()) if matches else None
        if stable_since and time.monotonic() - stable_since >= 3:
            path.unlink(missing_ok=True)
            print("[STEAM INPUT] Perfil Desktop anterior restaurado e confirmado de forma estável pelo Steam.", flush=True)
            return
        time.sleep(.5)
    raise RuntimeError("Steam ainda não estabilizou o perfil Desktop; recuperação preservada para nova tentativa.")


def owner_active(owner):
    return alive(owner) or any(alive(child) for child in owner.get("children", []))


def refresh_owner(owner):
    """Persist live session members, including children detached before attach."""
    children = descendants(owner.get("children", []), token=owner.get("token"), parent=owner)
    changed = children != owner.get("children", [])
    if changed:
        owner["children"] = children
    return changed


def has_live_owners():
    """Recheck detached session members before recovering Desktop resources."""
    with locked_state() as (path, state):
        if not state:
            return False
        changed = False
        for owner in state["owners"].values():
            changed = refresh_owner(owner) or changed
        if changed:
            write_state(path, state)
        return any(owner_active(owner) for owner in state["owners"].values())


def live_children(pid):
    """Return the refreshed, persisted children of an active session owner."""
    with locked_state() as (path, state):
        owner = (state or {}).get("owners", {}).get(str(pid))
        if not owner:
            return []
        if refresh_owner(owner):
            write_state(path, state)
        return [child for child in owner.get("children", []) if alive(child)]


def release(pid=None, recover=False):
    with locked_state() as (path, state):
        if not state:
            return
        owners = state["owners"]
        refreshed = False
        for owner in owners.values():
            refreshed = refresh_owner(owner) or refreshed
        if pid is not None:
            owner = owners.get(str(pid))
            if owner and any(alive(c) for c in owner.get("children", [])):
                if refreshed:
                    write_state(path, state)
                raise RuntimeError("Frontend/emulador ainda ativo; Desktop continuará suspenso até encerrar.")
            owners.pop(str(pid), None)
        state["owners"] = {key: owner for key, owner in owners.items() if owner_active(owner)}
        if state["owners"]:
            write_state(path, state)
            if not recover:
                print("[STEAM INPUT] Outra sessão ainda usa o perfil Gamepad.", flush=True)
            return
        write_state(path, state)
        with SteamCdp() as client:
            restore(client, path, state)
    restore_desktop_environment()


def restore_desktop_environment():
    """Steam is back on Desktop; now the host input pieces around it. A
    failure keeps the snapshot for the independent recovery loop."""
    try:
        repaired = desktop_environment.restore(state_directory())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"[DESKTOP] {error}", file=sys.stderr, flush=True)
        return
    if repaired:
        print("[DESKTOP] Restaurado após o jogo: " + ", ".join(repaired), flush=True)


def acquire(root, pid):
    ticks = proc_identity(pid)
    if ticks is None:
        raise RuntimeError("Launcher já encerrou; perfil Desktop preservado.")
    with locked_state() as (path, state):
        if state:
            for owner in state["owners"].values():
                refresh_owner(owner)
            if not any(owner_active(o) for o in state["owners"].values()):
                with SteamCdp() as client:
                    restore(client, path, state)
                state = None
        if state is None:
            with SteamCdp() as client:
                controllers = client.controllers()
            # Internal Steam controller type 4 is Neptune (confirmed on the host).
            targets = [c for c in controllers if c["type"] == 4]
            unsupported = [c for c in controllers if c["type"] != 4 and (c["keyboard"] or c["mouse"])]
            if unsupported:
                raise RuntimeError("Há outro tipo de controle emitindo Desktop; falta perfil Gamepad compatível.")
            if not targets:
                print("[STEAM INPUT] Nenhum Steam Deck conectado; sem perfil Desktop do Deck para suspender.", flush=True)
                return False
            if any(not c["url"] or c["url"] == GAMEPAD_URL for c in targets):
                raise RuntimeError("Perfil Desktop original indisponível; restauração exata não pode ser garantida.")
            if len({c["type"] for c in targets}) != len(targets):
                raise RuntimeError("Mais de um Deck conectado: identificação individual é necessária para restauração segura.")
            state = {"schema_version": 1, "created_at": time.time(), "root": str(root),
                     "controllers": targets, "owners": {}}
        owners = state["owners"]
        if str(pid) in owners and alive(owners[str(pid)]):
            return True
        owners[str(pid)] = {"pid": pid, "start_ticks": ticks, "boot_id": boot_id(),
                            "token": uuid.uuid4().hex, "children": []}
        write_state(path, state)  # Durable original state precedes every Steam mutation.
        desktop_environment.save_once(state_directory())
        log = state_directory().parent / "steam-desktop-session.log"
        try:
            with log.open("ab") as errors:
                guardian = subprocess.Popen([
                    sys.executable, str(Path(__file__).with_name("steam-input.py")),
                    "--root", str(root), "session-watch", "--pid", str(pid), "--pid-start", ticks,
                ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                    start_new_session=True, close_fds=True)
                ready, _, _ = select.select([guardian.stdout], [], [], 4)
                if not ready or guardian.stdout.readline().strip() != b"READY":
                    raise RuntimeError("Guardião Steam não confirmou inicialização; Desktop preservado.")
                guardian.stdout.close()
            owners[str(pid)]["watchdog_pid"] = guardian.pid
            write_state(path, state)
            with SteamCdp() as client:
                for controller in state["controllers"]:
                    client.select(controller, GAMEPAD_URL)
                confirm(client, [{**c, "url": GAMEPAD_URL, "gamepad": True, "keyboard": False,
                                  "mouse": False} for c in state["controllers"]])
        except Exception:
            owners.pop(str(pid), None)
            write_state(path, state)
            if not owners:
                with SteamCdp() as client:
                    restore(client, path, state)
            raise
    print("[STEAM INPUT] Desktop suspenso; Gamepad ativo sem teclado/mouse do Steam.", flush=True)
    return True


def attach(pid, child_pid):
    with locked_state() as (path, state):
        if not state or str(pid) not in state["owners"]:
            return
        ticks = proc_identity(child_pid)
        if ticks:
            state["owners"][str(pid)]["children"].append({"pid": child_pid, "start_ticks": ticks, "boot_id": boot_id()})
            write_state(path, state)


def descendants(identities, token=None, parent=None):
    """Retain live descendants and session-token members after reparenting."""
    known = {int(p["pid"]): p for p in identities if alive(p)}
    anchor = int(parent["pid"]) if parent and alive(parent) else None
    if anchor is not None:
        known[anchor] = parent
    processes = {}
    needle = (SESSION_TOKEN_ENV + "=" + token).encode() if token else None
    current_boot = boot_id()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            text = (proc / "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            if fields[0] not in {"Z", "X", "x"}:
                member_pid = int(proc.name)
                processes[member_pid] = (int(fields[1]), fields[19])
                if needle and member_pid not in known and proc.stat().st_uid == os.getuid():
                    try:
                        if needle in (proc / "environ").read_bytes().split(b"\0"):
                            known[member_pid] = {"pid": member_pid, "start_ticks": fields[19],
                                                 "boot_id": current_boot}
                    except OSError:
                        pass  # Parent/known identities can still identify this process.
        except (OSError, IndexError) as error:
            if int(proc.name) in known:
                raise RuntimeError(f"Não foi possível verificar processo conhecido {proc.name}; sessão preservada.") from error
            continue
    changed = True
    while changed:
        changed = False
        for pid, (parent, ticks) in processes.items():
            if parent in known and pid not in known:
                known[pid] = {"pid": pid, "start_ticks": ticks, "boot_id": current_boot}
                changed = True
    return [entry for member_pid, entry in known.items() if member_pid != anchor]


def watch(pid, ticks):
    # Handshake occurs before the parent changes Steam; detached guardian survives it.
    print("READY", flush=True)
    sys.stdout = sys.stderr
    owner_id = str(pid)
    while True:
        try:
            done = False
            with locked_state() as (path, state):
                if not state:
                    return
                if owner_id not in state["owners"]:
                    if state["owners"]:
                        return
                    done = True
                else:
                    owner = state["owners"][owner_id]
                    if owner["start_ticks"] != ticks:
                        return
                    if refresh_owner(owner):
                        write_state(path, state)
                    done = not owner_active(owner)
            if done:
                release(pid)
                return
        except (OSError, ValueError, RuntimeError) as error:
            # Steam may be restarting or the controller temporarily disconnected.
            # Keep the saved state and retry instead of discarding the only backup.
            print(f"[STEAM INPUT] Recuperação aguardando: {error}", file=sys.stderr, flush=True)
            time.sleep(2)
        time.sleep(.25)
