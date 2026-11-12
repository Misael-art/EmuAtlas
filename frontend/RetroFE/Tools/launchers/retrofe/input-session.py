#!/usr/bin/env python3
"""Inspect controller/session input and guard KDE shortcuts during RetroFE."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
INPUT_DEVICES = Path("/proc/bus/input/devices")
STATE_NAME = "kde-global-shortcut-guard.json"
KGLOBALACCEL_SERVICE = "org.kde.kglobalaccel"
KGLOBALACCEL_PATH = "/kglobalaccel"
KGLOBALACCEL_INTERFACE = "org.kde.KGlobalAccel"
SDL_INIT_JOYSTICK = 0x00000200


def classify_controller(name: str, vendor: str = "", product: str = "") -> str:
    folded = name.casefold()
    if "motion sensor" in folded or "imu" in folded:
        return "sensor"
    if "steam deck" in folded or (vendor.casefold() == "28de" and product.casefold() == "1205"):
        return "steam-deck"
    if "xbox" in folded or vendor.casefold() == "045e":
        return "xbox"
    return "gamepad"


def parse_linux_input_devices(text: str) -> dict[str, Any]:
    controllers: list[dict[str, Any]] = []
    trackpads: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        name_match = re.search(r'^N: Name="([^"]+)"', block, re.MULTILINE)
        handlers_match = re.search(r"^H: Handlers=(.+)$", block, re.MULTILINE)
        vendor_match = re.search(r"^I: Bus=\S+ Vendor=([0-9a-fA-F]+) Product=([0-9a-fA-F]+)", block, re.MULTILINE)
        if not name_match or not handlers_match:
            continue
        name = name_match.group(1)
        handlers = handlers_match.group(1).split()
        vendor, product = vendor_match.groups() if vendor_match else ("", "")
        kind = classify_controller(name, vendor, product)
        if any(re.fullmatch(r"mouse\d+", item) for item in handlers) and ("steam deck" in name.casefold() or vendor.casefold() == "28de"):
            trackpads.append(name)
        js_handlers = [item for item in handlers if re.fullmatch(r"js\d+", item)]
        if js_handlers and kind != "sensor":
            controllers.append({"name": name, "kind": kind, "handlers": js_handlers, "vendor": vendor, "product": product})
    return {"controllers": controllers, "trackpads": trackpads}


def enumerate_sdl_controllers(root: Path = ROOT) -> tuple[list[dict[str, Any]], str | None]:
    library = ctypes.util.find_library("SDL2-2.0") or ctypes.util.find_library("SDL2")
    if not library:
        bundled = sorted((root / "core/linux/lib").glob("libSDL2-2.0.so*"))
        library = str(bundled[0]) if bundled else None
    if not library:
        return [], "SDL2 library was not found"
    try:
        sdl = ctypes.CDLL(library)
        init = sdl.SDL_InitSubSystem
        init.argtypes = [ctypes.c_uint32]
        init.restype = ctypes.c_int
        count_joysticks = sdl.SDL_NumJoysticks
        count_joysticks.restype = ctypes.c_int
        name_for_index = sdl.SDL_JoystickNameForIndex
        name_for_index.argtypes = [ctypes.c_int]
        name_for_index.restype = ctypes.c_char_p
        is_game_controller = sdl.SDL_IsGameController
        is_game_controller.argtypes = [ctypes.c_int]
        is_game_controller.restype = ctypes.c_int
        get_vendor = sdl.SDL_JoystickGetDeviceVendor
        get_vendor.argtypes = [ctypes.c_int]
        get_vendor.restype = ctypes.c_uint16
        get_product = sdl.SDL_JoystickGetDeviceProduct
        get_product.argtypes = [ctypes.c_int]
        get_product.restype = ctypes.c_uint16
        get_error = sdl.SDL_GetError
        get_error.restype = ctypes.c_char_p
        if init(SDL_INIT_JOYSTICK) != 0:
            error = get_error()
            return [], error.decode(errors="replace") if error else "SDL joystick initialization failed"
        devices = []
        for index in range(max(0, count_joysticks())):
            raw_name = name_for_index(index)
            name = raw_name.decode(errors="replace") if raw_name else f"Joystick {index}"
            vendor = f"{get_vendor(index):04x}"
            product = f"{get_product(index):04x}"
            devices.append({
                "index": index,
                "name": name,
                "kind": classify_controller(name, vendor, product),
                "game_controller": bool(is_game_controller(index)),
                "vendor": vendor,
                "product": product,
            })
        sdl.SDL_QuitSubSystem.argtypes = [ctypes.c_uint32]
        sdl.SDL_QuitSubSystem(SDL_INIT_JOYSTICK)
        return devices, None
    except (AttributeError, OSError) as error:
        return [], f"SDL2 enumeration failed: {error}"


def normalize_key(value: str) -> str:
    value = value.strip().strip('"').casefold()
    aliases = {"esc": "escape", "spacebar": "space", "kp enter": "keypad enter"}
    return aliases.get(value, value)


def parse_keyboard_controls(text: str) -> set[str]:
    keys: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        _, value = line.split("=", 1)
        for binding in value.split(","):
            key = binding.strip()
            if not key or key.casefold() == "none" or key.casefold().startswith(("joy", "mouse", "axis", "hat")):
                continue
            keys.add(normalize_key(key))
    return keys


def parse_shortcut_conflicts(controls: str, global_shortcuts: str) -> list[dict[str, str]]:
    controls_keys = parse_keyboard_controls(controls)
    conflicts: list[dict[str, str]] = []
    for line in global_shortcuts.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[", "_")) or "=" not in line:
            continue
        action, value = line.split("=", 1)
        active = value.split(",", 1)[0].strip()
        if not active or active.casefold() == "none":
            continue
        for sequence in re.split(r"[\t;]", active):
            tokens = [token.strip() for token in sequence.split("+") if token.strip()]
            if not tokens:
                continue
            key = normalize_key(tokens[-1])
            if key in controls_keys:
                conflicts.append({"action": action, "shortcut": sequence.strip(), "key": key})
    return conflicts


def process_ancestry(pid: int | None = None) -> list[str]:
    commands: list[str] = []
    current = pid or os.getppid()
    visited: set[int] = set()
    for _ in range(32):
        if current <= 1 or current in visited:
            break
        visited.add(current)
        proc = Path(f"/proc/{current}")
        try:
            raw = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            if raw:
                commands.append(raw)
            stat = (proc / "stat").read_text()
            tail = stat[stat.rfind(")") + 2 :].split()
            current = int(tail[1])
        except (OSError, ValueError, IndexError):
            break
    return commands


def detect_session_mode(env: dict[str, str] | None = None, ancestry: list[str] | None = None) -> str:
    env = os.environ if env is None else env
    override = env.get("RETROFE_INPUT_SESSION", "auto").strip().casefold()
    if override in {"steam", "big-picture", "gaming"}:
        return "big-picture"
    if override == "desktop":
        return "desktop-kde" if "kde" in env.get("XDG_CURRENT_DESKTOP", "").casefold() else "desktop"
    desktop = env.get("XDG_CURRENT_DESKTOP", "").casefold()
    if "gamescope" in desktop or env.get("XDG_SESSION_DESKTOP", "").casefold() == "gamescope":
        return "big-picture"
    ancestry = process_ancestry() if ancestry is None else ancestry
    steam_ancestor = any(re.search(r"(^|[/\s])steam(?:webhelper|webhelper\.exe)?(?:\s|$)", command, re.IGNORECASE) for command in ancestry)
    gamepad_ui = any(re.search(r"-gamepadui|-bigpicture|big.picture", command, re.IGNORECASE) for command in ancestry)
    steam_environment = any(env.get(name, "").strip() for name in (
        "SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID", "STEAM_COMPAT_DATA_PATH", "SteamClientLaunch"
    ))
    if gamepad_ui or (steam_ancestor and steam_environment) or (steam_environment and env.get("SteamClientLaunch", "") == "1"):
        return "big-picture"
    if "kde" in desktop or env.get("KDE_FULL_SESSION", "").casefold() == "true":
        return "desktop-kde"
    return "desktop" if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") else "headless"


def default_shortcuts_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "kglobalshortcutsrc"


def find_qdbus() -> str | None:
    configured = os.environ.get("RETROFE_QDBUS_BIN")
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    return shutil.which("qdbus6") or shutil.which("qdbus")


def make_report(
    root: Path = ROOT,
    input_file: Path = INPUT_DEVICES,
    shortcuts_file: Path | None = None,
    env: dict[str, str] | None = None,
    ancestry: list[str] | None = None,
    sdl_devices: list[dict[str, Any]] | None = None,
    sdl_error: str | None = None,
) -> dict[str, Any]:
    try:
        linux_devices = parse_linux_input_devices(input_file.read_text(errors="replace"))
    except OSError:
        linux_devices = {"controllers": [], "trackpads": []}
    if sdl_devices is None:
        sdl_devices, sdl_error = enumerate_sdl_controllers(root)
    controls_path = root / "controls.conf"
    controls = controls_path.read_text(errors="replace") if controls_path.is_file() else ""
    shortcuts_path = shortcuts_file or default_shortcuts_path()
    try:
        global_shortcuts = shortcuts_path.read_text(errors="replace")
        shortcuts_readable = True
    except OSError:
        global_shortcuts = ""
        shortcuts_readable = False
    conflicts = parse_shortcut_conflicts(controls, global_shortcuts)
    session = detect_session_mode(env, ancestry)
    can_block = bool(find_qdbus())
    return {
        "session": session,
        "controllers": sdl_devices,
        "sdl_error": sdl_error,
        "linux_controllers": linux_devices["controllers"],
        "trackpads": linux_devices["trackpads"],
        "xbox_sdl_detected": any(item["kind"] == "xbox" for item in sdl_devices),
        "xbox_kernel_detected": any(item["kind"] == "xbox" for item in linux_devices["controllers"]),
        "shortcut_conflicts": conflicts,
        "shortcut_config_readable": shortcuts_readable,
        "block_shortcuts": bool(session == "desktop-kde" and (conflicts or not shortcuts_readable) and can_block),
        "trackpad_untouched": True,
    }


def report_lines(report: dict[str, Any]) -> list[str]:
    labels = {"desktop-kde": "KDE Desktop", "big-picture": "Steam Big Picture/Gaming Mode", "desktop": "desktop gráfico", "headless": "sem display"}
    lines = [f"[INPUT] Sessão: {labels.get(report['session'], report['session'])}."]
    controllers = report["controllers"]
    if controllers:
        for device in controllers:
            flavor = "SDL GameController" if device["game_controller"] else "SDL joystick"
            lines.append(f"[INPUT] Controle SDL {device['index']}: {device['name']} ({flavor}; {device['kind']}).")
    elif report["linux_controllers"]:
        names = ", ".join(item["name"] for item in report["linux_controllers"])
        lines.append(f"[INPUT] Controles do kernel encontrados, mas SDL não os enumerou: {names}.")
    else:
        lines.append("[INPUT] Nenhum controle de jogo foi detectado; teclado e mouse continuam disponíveis.")
    if report["xbox_sdl_detected"]:
        lines.append("[INPUT] Controle Xbox detectado; o RetroFE aceita joystick 0 e 1 pelo controls.conf.")
    elif report["xbox_kernel_detected"]:
        lines.append("[INPUT] Xbox aparece no kernel, mas não na enumeração SDL; revise permissões e suporte SDL antes de esperar navegação.")
    else:
        lines.append("[INPUT] Nenhum controle Xbox separado foi enumerado nesta sessão.")
    if report["trackpads"]:
        lines.append(f"[INPUT] Trackpad preservado: {', '.join(report['trackpads'])}; nenhum dispositivo apontador será desativado.")
    conflicts = report["shortcut_conflicts"]
    if report["session"] == "big-picture":
        lines.append("[INPUT] Steam controla esta sessão; atalhos globais do KDE não serão alterados.")
    elif not report["shortcut_config_readable"] and report["block_shortcuts"]:
        lines.append("[INPUT] Não consegui auditar kglobalshortcutsrc; suspensão preventiva dos atalhos durante o RetroFE.")
    elif not report["shortcut_config_readable"]:
        lines.append("[INPUT] Não consegui auditar kglobalshortcutsrc nem acessar KGlobalAccel; atalhos permanecem como estão.")
    elif conflicts and report["block_shortcuts"]:
        keys = sorted({item["key"] for item in conflicts})
        names = ", ".join(keys[:8])
        if len(keys) > 8:
            names += f" e mais {len(keys) - 8}"
        lines.append(f"[INPUT] {len(conflicts)} colisões de atalho KDE em {len(keys)} teclas ({names}); suspensão temporária durante o RetroFE.")
    elif conflicts:
        lines.append("[INPUT] Há colisão de teclas, mas a sessão não permite bloquear atalhos KDE; ela será registrada e o launcher seguirá.")
    else:
        lines.append("[INPUT] Nenhuma colisão entre controls.conf e atalhos KDE; atalhos do desktop permanecem ativos.")
    return lines


def qdbus_command(blocked: bool) -> tuple[bool, str]:
    executable = find_qdbus()
    if not executable:
        return False, "qdbus não encontrado"
    command = [
        executable,
        KGLOBALACCEL_SERVICE,
        KGLOBALACCEL_PATH,
        f"{KGLOBALACCEL_INTERFACE}.blockGlobalShortcuts",
        "true" if blocked else "false",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or f"qdbus terminou com código {result.returncode}"
    return True, ""


def state_paths(state_dir: Path) -> tuple[Path, Path]:
    return state_dir / STATE_NAME, state_dir / f"{STATE_NAME}.lock"


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, indent=2, sort_keys=True)
            output.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def process_start_ticks(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def guard_owner_alive(pid: str, detail: dict) -> bool:
    # Owners are keyed by PID; older or partial records may lack the duplicated "pid" field.
    try:
        owner = int(detail.get("pid", pid))
    except (TypeError, ValueError):
        return False
    if detail.get("boot_id", boot_id()) != boot_id():
        return False
    return process_alive(owner, detail.get("start_ticks")) or steam_members_alive(detail)


def steam_members_alive(detail: dict) -> bool:
    """Use the existing Steam lease to retain KDE during detached emulators."""
    steam = detail.get("steam_owner")
    if not isinstance(steam, dict) or steam.get("boot_id") != boot_id():
        return False
    import steam_desktop_session

    state = steam_desktop_session.snapshot()
    owner = (state or {}).get("owners", {}).get(str(steam.get("pid")))
    if not owner or owner.get("start_ticks") != steam.get("start_ticks"):
        return False
    # live_children refreshes saved PID/start-tick identities, including
    # reparented children, and raises on uncertain /proc reads.
    return bool(steam_desktop_session.live_children(int(steam["pid"])))


def process_alive(pid: int, expected_start_ticks: str | None = None) -> bool:
    if pid <= 1:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        if fields[0] == "Z":
            return False
        if expected_start_ticks is not None and fields[19] != expected_start_ticks:
            return False
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, IndexError, ValueError) as error:
        raise RuntimeError(f"Identidade de /proc/{pid} inconclusiva; proteção KDE preservada") from error


def _locked_state(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path, lock_path = state_paths(state_dir)
    lock_handle = lock_path.open("a+")
    os.chmod(lock_path, 0o600)
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    return path, lock_handle, read_state(path)


def _unlock(lock_handle: Any) -> None:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    lock_handle.close()


def acquire_guard(state_dir: Path, target_pid: int, root: Path, start_watchdog: bool = True,
                  steam_owner_pid: int | None = None) -> tuple[bool, str]:
    target_start_ticks = process_start_ticks(target_pid)
    if target_start_ticks is None or not process_alive(target_pid, target_start_ticks):
        return False, f"processo RetroFE {target_pid} terminou antes do bloqueio"
    steam_owner = None
    if steam_owner_pid is not None:
        steam_ticks = process_start_ticks(steam_owner_pid)
        if steam_ticks is None or not process_alive(steam_owner_pid, steam_ticks):
            return False, "launcher Steam encerrou antes da proteção KDE"
        steam_owner = {"pid": steam_owner_pid, "start_ticks": steam_ticks, "boot_id": boot_id()}
    if start_watchdog:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "guard",
            "watch",
            "--state-dir",
            str(state_dir),
            "--pid",
            str(target_pid),
            "--pid-start",
            target_start_ticks,
        ]
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except OSError as error:
            return False, f"não consegui iniciar o guardião de restauração: {error}"
    path, lock_handle, state = _locked_state(state_dir)
    try:
        owners = state.get("owners", {}) if state.get("active") else {}
        owners = {
            str(pid): detail
            for pid, detail in owners.items()
            if isinstance(detail, dict) and guard_owner_alive(pid, detail)
        }
        if not owners:
            if state.get("active"):
                ok, error = qdbus_command(False)
                if not ok:
                    return False, f"não consegui recuperar bloqueio antigo: {error}"
            if not process_alive(target_pid, target_start_ticks):
                return False, f"processo RetroFE {target_pid} terminou antes do bloqueio"
        owners[str(target_pid)] = {
            "pid": target_pid,
            "root": str(root),
            "started": int(time.time()),
            "start_ticks": target_start_ticks,
            "boot_id": boot_id(),
        }
        if steam_owner:
            owners[str(target_pid)]["steam_owner"] = steam_owner
        write_state(path, {"schema": 1, "active": True, "owners": owners})
        if len(owners) == 1:
            ok, error = qdbus_command(True)
            if not ok:
                path.unlink(missing_ok=True)
                return False, error
    finally:
        _unlock(lock_handle)
    return True, "atalhos globais bloqueados temporariamente"


def release_guard(state_dir: Path, target_pid: int) -> tuple[bool, str]:
    path, _ = state_paths(state_dir)
    if not path.exists():
        return True, "nenhum bloqueio ativo"
    path, lock_handle, state = _locked_state(state_dir)
    try:
        if not state.get("active"):
            return True, "nenhum bloqueio ativo"
        owners = state.get("owners", {})
        current = owners.get(str(target_pid))
        if isinstance(current, dict) and steam_members_alive(current):
            return True, "atalhos KDE preservados enquanto o emulador da sessão Steam permanece ativo"
        owners.pop(str(target_pid), None)
        owners = {
            str(pid): detail
            for pid, detail in owners.items()
            if isinstance(detail, dict) and guard_owner_alive(pid, detail)
        }
        if owners:
            write_state(path, {"schema": 1, "active": True, "owners": owners})
            return True, "atalhos continuam bloqueados por outra sessão RetroFE ativa"
        ok, error = qdbus_command(False)
        if not ok:
            write_state(path, {"schema": 1, "active": True, "owners": {}})
            return False, f"não consegui restaurar atalhos globais: {error}"
        path.unlink(missing_ok=True)
        return True, "atalhos globais do KDE restaurados"
    finally:
        _unlock(lock_handle)


def recover_guard(state_dir: Path) -> tuple[bool, str]:
    path, _ = state_paths(state_dir)
    if not path.exists():
        return True, "nenhum bloqueio órfão"
    path, lock_handle, state = _locked_state(state_dir)
    try:
        if not state.get("active"):
            return True, "nenhum bloqueio órfão"
        owners = state.get("owners", {})
        owners = {
            str(pid): detail
            for pid, detail in owners.items()
            if isinstance(detail, dict) and guard_owner_alive(pid, detail)
        }
        if owners:
            write_state(path, {"schema": 1, "active": True, "owners": owners})
            return True, "guardião de atalhos já acompanha uma sessão RetroFE ativa"
        ok, error = qdbus_command(False)
        if not ok:
            return False, f"bloqueio órfão permanece ativo; restauração falhou: {error}"
        path.unlink(missing_ok=True)
        return True, "bloqueio órfão recuperado; atalhos globais restaurados"
    finally:
        _unlock(lock_handle)


def watch_guard(state_dir: Path, target_pid: int, expected_start_ticks: str | None) -> None:
    while process_alive(target_pid, expected_start_ticks):
        time.sleep(0.25)
    while True:
        ok, _ = release_guard(state_dir, target_pid)
        if ok:
            return
        time.sleep(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="lista controles e colisões sem alterar atalhos")
    inspect_parser.add_argument("--root", type=Path, default=ROOT)
    inspect_parser.add_argument("--input-file", type=Path, default=INPUT_DEVICES)
    inspect_parser.add_argument("--shortcuts-file", type=Path)
    decision_parser = subparsers.add_parser("decision", help="retorna block ou preserve")
    decision_parser.add_argument("--root", type=Path, default=ROOT)
    decision_parser.add_argument("--input-file", type=Path, default=INPUT_DEVICES)
    decision_parser.add_argument("--shortcuts-file", type=Path)
    guard_parser = subparsers.add_parser("guard", help="gerencia bloqueio temporário e restauração")
    guard_actions = guard_parser.add_subparsers(dest="action", required=True)
    for action in ("acquire", "release", "recover", "watch"):
        item = guard_actions.add_parser(action)
        item.add_argument("--state-dir", type=Path, required=True)
        if action in {"acquire", "release", "watch"}:
            item.add_argument("--pid", type=int, required=True)
        if action == "watch":
            item.add_argument("--pid-start", required=True)
        if action == "acquire":
            item.add_argument("--root", type=Path, default=ROOT)
            item.add_argument("--steam-owner-pid", type=int)
            item.add_argument("--no-watchdog", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"inspect", "decision"}:
        report = make_report(args.root, args.input_file, args.shortcuts_file)
        if args.command == "inspect":
            for line in report_lines(report):
                print(line)
            return 0
        print("block" if report["block_shortcuts"] else "preserve")
        return 0
    if args.action == "acquire":
        ok, message = acquire_guard(args.state_dir, args.pid, args.root, not args.no_watchdog,
                                    args.steam_owner_pid)
    elif args.action == "release":
        ok, message = release_guard(args.state_dir, args.pid)
    elif args.action == "recover":
        ok, message = recover_guard(args.state_dir)
    else:
        watch_guard(args.state_dir, args.pid, args.pid_start)
        return 0
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
