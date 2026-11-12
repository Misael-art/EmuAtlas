#!/usr/bin/env python3
"""Show host keyboard/SDL events over the active RetroFE window, without grabbing input."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import glob
import json
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from window_placement import active_window_id, find_window
except Exception as exc:  # pragma: no cover - reported to the launcher log
    raise SystemExit(f"Não consegui carregar o diagnóstico de janelas: {exc}")


ACTION_LABELS = {
    "up": "Navegar para cima",
    "down": "Navegar para baixo",
    "left": "Navegar para a esquerda",
    "right": "Navegar para a direita",
    "select": "Confirmar / abrir",
    "back": "Voltar",
    "quit": "Sair do RetroFE",
    "collectionDown": "Próxima coleção",
    "collectionUp": "Coleção anterior",
    "pageUp": "Página anterior",
    "pageDown": "Próxima página",
    "letterUp": "Letra anterior",
    "letterDown": "Próxima letra",
    "favPlaylist": "Abrir favoritos",
    "nextPlaylist": "Próxima lista",
    "prevPlaylist": "Lista anterior",
    "prevCyclePlaylist": "Lista anterior (ciclo)",
    "nextCyclePlaylist": "Próxima lista (ciclo)",
    "togglePlaylist": "Marcar / retirar favorito",
    "optionsPanel": "Abrir Opções (painel do launcher)",
    "saveFirstPlaylist": "Salvar primeira lista",
    "addPlaylist": "Adicionar à lista",
    "removePlaylist": "Remover da lista",
    "random": "Escolha aleatória",
}

KEY_ALIASES = {
    "ESC": "Escape",
    "ENTER": "Return",
    "KPENTER": "Return",
    "BACKSPACE": "Backspace",
    "PAGEUP": "PageUp",
    "PAGEDOWN": "PageDown",
    "LEFT": "Left",
    "RIGHT": "Right",
    "UP": "Up",
    "DOWN": "Down",
    "LEFTCTRL": "Left Ctrl",
    "RIGHTCTRL": "Right Ctrl",
    "LEFTSHIFT": "Left Shift",
    "RIGHTSHIFT": "Right Shift",
    "LEFTALT": "Left Alt",
    "RIGHTALT": "Right Alt",
    "LEFTMETA": "Left Meta",
    "RIGHTMETA": "Right Meta",
    "CAPSLOCK": "CapsLock",
    "DELETE": "Delete",
    "INSERT": "Insert",
    "HOME": "Home",
    "END": "End",
    "PRINT": "PrintScreen",
    "SYSRQ": "PrintScreen",
}

SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMECONTROLLER = 0x00002000
SDL_ENABLE = 1
SDL_JOYAXISMOTION = 0x600
SDL_JOYHATMOTION = 0x602
SDL_JOYBUTTONDOWN = 0x603
SDL_JOYDEVICEADDED = 0x605
SDL_JOYDEVICEREMOVED = 0x606
HAT_UP = 0x01
HAT_RIGHT = 0x02
HAT_DOWN = 0x04
HAT_LEFT = 0x08


class SDLJoyAxisEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint32),
        ("which", ctypes.c_int32),
        ("axis", ctypes.c_uint8),
        ("padding1", ctypes.c_uint8),
        ("padding2", ctypes.c_uint8),
        ("padding3", ctypes.c_uint8),
        ("value", ctypes.c_int16),
        ("padding4", ctypes.c_uint16),
    ]


class SDLJoyHatEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint32),
        ("which", ctypes.c_int32),
        ("hat", ctypes.c_uint8),
        ("value", ctypes.c_uint8),
        ("padding1", ctypes.c_uint8),
        ("padding2", ctypes.c_uint8),
    ]


class SDLJoyButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint32),
        ("which", ctypes.c_int32),
        ("button", ctypes.c_uint8),
        ("state", ctypes.c_uint8),
        ("padding1", ctypes.c_uint8),
        ("padding2", ctypes.c_uint8),
    ]


class SDLEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("jaxis", SDLJoyAxisEvent),
        ("jhat", SDLJoyHatEvent),
        ("jbutton", SDLJoyButtonEvent),
        ("padding", ctypes.c_uint8 * 56),
    ]


def parse_controls(path: Path, include_session: bool = False) -> dict[str, list[str]]:
    bindings: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        action, values = line.split("=", 1)
        action = action.strip()
        if not action:
            continue
        for token in values.split(","):
            token = token.strip()
            if token:
                bindings.setdefault(token.casefold(), []).append(action)
    if include_session:
        for token in ("f10", "joybutton7"):
            bindings.setdefault(token, []).append("optionsPanel")
    return bindings


def resolve_actions(bindings: dict[str, list[str]], tokens: list[str]) -> list[str]:
    actions: list[str] = []
    for token in tokens:
        for action in bindings.get(token.casefold(), []):
            if action not in actions:
                actions.append(action)
    return actions


def action_text(actions: list[str]) -> str:
    if not actions:
        return "Sem associação em controls.conf"
    return "  ·  ".join(
        f"{ACTION_LABELS.get(action, action)} ({action})" for action in actions
    )


def evdev_key_name(code: int) -> str:
    try:
        from evdev import ecodes

        raw = ecodes.KEY.get(code, f"KEY_{code}")
        if isinstance(raw, (tuple, list)):
            raw = next((item for item in raw if str(item).startswith("KEY_")), raw[0])
        raw = str(raw)
    except Exception:
        raw = f"KEY_{code}"
    stem = raw[4:] if raw.startswith("KEY_") else raw
    if stem in KEY_ALIASES:
        return KEY_ALIASES[stem]
    if len(stem) == 1:
        return stem.upper()
    if stem.startswith("F") and stem[1:].isdigit():
        return stem
    return stem[:1].upper() + stem[1:].lower()


def keyboard_tokens(code: int) -> list[str]:
    key_name = evdev_key_name(code)
    candidates = [key_name]
    aliases = {
        "Escape": ["Esc"],
        "Return": ["Enter", "KP Enter"],
        "PageUp": ["Page Up"],
        "PageDown": ["Page Down"],
    }
    candidates.extend(aliases.get(key_name, []))
    return candidates


def keysym_token(keysym: str) -> str | None:
    aliases = {
        "escape": "Escape", "esc": "Escape", "return": "Return", "kp_enter": "Return",
        "backspace": "Backspace", "space": "Space", "prior": "PageUp", "page_up": "PageUp",
        "next": "PageDown", "page_down": "PageDown", "left": "Left", "right": "Right",
        "up": "Up", "down": "Down", "control_l": "Left Ctrl", "control_r": "Right Ctrl",
        "shift_l": "Left Shift", "shift_r": "Right Shift", "alt_l": "Left Alt",
        "alt_r": "Right Alt", "super_l": "Left Meta", "super_r": "Right Meta",
        "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
        "tab": "Tab", "caps_lock": "CapsLock",
    }
    if not keysym or keysym == "NoSymbol":
        return None
    if keysym.casefold() in aliases:
        return aliases[keysym.casefold()]
    if len(keysym) == 1:
        return keysym.upper() if keysym.isalpha() else keysym
    if re.fullmatch(r"f\d+", keysym, re.IGNORECASE):
        return keysym.upper()
    return keysym[:1].upper() + keysym[1:]


class X11KeyboardInput:
    """Observe XInput2 raw key presses, including virtual/X11 keyboard sources."""

    def __init__(self) -> None:
        self.events: queue.Queue[int] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.keysyms = self._read_keymap()
        self.closing = False
        if not self.keysyms:
            raise RuntimeError("XInput2 não conseguiu ler o mapa de teclado")
        self._start_observer()

    def _start_observer(self) -> None:
        xinput = shutil.which("xinput")
        if not xinput:
            raise RuntimeError("xinput não está instalado")
        # The all-device test mask also claims touch events on the root window.
        # A second observer can then fail with XISelectEvents BadAccess. Select
        # the master keyboard only: debug and Options can coexist/reconnect.
        listed = subprocess.run([xinput, "list", "--short"], capture_output=True,
                                text=True, errors="replace", timeout=3, check=True)
        master = next((re.search(r"id=(\d+)", line).group(1)
                       for line in listed.stdout.splitlines()
                       if "master keyboard" in line and re.search(r"id=(\d+)", line)), None)
        if master is None:
            raise RuntimeError("XInput2 não encontrou o teclado principal")
        stdbuf = shutil.which("stdbuf")
        command = ([stdbuf, "-oL", xinput, "test-xi2", "--root", master] if stdbuf
                   else [xinput, "test-xi2", "--root", master])
        self.process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", bufsize=1,
        )
        if self.process.stdout is None:
            raise RuntimeError("xinput não abriu o fluxo de eventos")
        self.ready = threading.Event()
        self.reader = threading.Thread(target=self._read_events, name="retrofe-xinput-reader", daemon=True)
        self.reader.start()
        if not self.ready.wait(2) or self.process.poll() is not None:
            self.close()
            raise RuntimeError("XInput2 não confirmou assinatura dos eventos de teclado")

    @staticmethod
    def _read_keymap() -> dict[int, str]:
        if not shutil.which("xmodmap"):
            return {}
        result = subprocess.run(
            ["xmodmap", "-pke"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=3.0, check=False,
        )
        if result.returncode != 0:
            return {}
        keymap: dict[int, str] = {}
        for line in result.stdout.splitlines():
            match = re.match(r"\s*keycode\s+(\d+)\s*=\s*(.*)", line)
            if not match:
                continue
            symbols = [item for item in match.group(2).split() if item != "NoSymbol"]
            if symbols:
                keymap[int(match.group(1))] = symbols[0]
        return keymap

    def _read_events(self) -> None:
        assert self.process and self.process.stdout
        process, ready = self.process, self.ready
        event_type = ""
        detail: int | None = None

        def publish() -> None:
            if event_type == "RawKeyPress" and detail is not None:
                self.events.put(detail)

        for line in process.stdout:
            if "master keyboard" in line or "EVENT type" in line:
                ready.set()
            event_match = re.search(r"EVENT type \d+ \(([^)]+)\)", line)
            if event_match:
                publish()
                event_type = event_match.group(1)
                detail = None
                continue
            if event_type == "RawKeyPress":
                detail_match = re.match(r"\s*detail:\s*(\d+)", line)
                if detail_match:
                    detail = int(detail_match.group(1))
        publish()
        if not self.closing:
            detail = process.stderr.read()[-1600:] if process.stderr else ""
            print(f"[XInput2] Observador encerrou; será reconectado: {detail.strip()}", file=sys.stderr, flush=True)

    def status(self) -> str:
        if self.process is None or self.process.poll() is not None:
            return "XInput2: observador indisponível; recuperação necessária"
        return f"XInput2: ativo ({len(self.keysyms)} keycodes mapeados)"

    def poll(self, callback: Any, bindings: dict[str, list[str]]) -> None:
        if self.process is None or self.process.poll() is not None or not self.reader.is_alive():
            self.close()
            self.closing = False
            self.keysyms = self._read_keymap()
            self.events = queue.Queue()
            self._start_observer()
        while True:
            try:
                keycode = self.events.get_nowait()
            except queue.Empty:
                break
            if callback is None:
                continue
            token = keysym_token(self.keysyms.get(keycode, ""))
            label = f"XInput2 · keycode {keycode}"
            tokens: list[str] = []
            if token:
                label += f" ({token})"
                tokens = [token]
            callback(label, tokens, resolve_actions(bindings, tokens))

    def close(self) -> None:
        self.closing = True
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


def controller_button_name(name: str) -> str:
    return {
        "a": "A",
        "b": "B",
        "x": "X",
        "y": "Y",
        "back": "Back / Select",
        "guide": "Steam / Guide",
        "start": "Start",
        "leftstick": "L3",
        "rightstick": "R3",
        "leftshoulder": "L1 / LB",
        "rightshoulder": "R1 / RB",
        "dpup": "D-pad ↑",
        "dpdown": "D-pad ↓",
        "dpleft": "D-pad ←",
        "dpright": "D-pad →",
        "misc1": "Botão extra",
        "paddle1": "Grip 1",
        "paddle2": "Grip 2",
        "paddle3": "Grip 3",
        "paddle4": "Grip 4",
        "touchpad": "Touchpad",
    }.get(name.casefold(), name)


class SDLInput:
    def __init__(self) -> None:
        library = ctypes.util.find_library("SDL2-2.0") or ctypes.util.find_library("SDL2")
        if not library:
            raise RuntimeError("biblioteca SDL2 não encontrada")
        self.lib = ctypes.CDLL(library)
        self._configure_functions()
        self.lib.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        result = self.lib.SDL_InitSubSystem(SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER)
        if result != 0:
            message = self.lib.SDL_GetError().decode("utf-8", "replace")
            raise RuntimeError(f"SDL2 não abriu a entrada: {message}")
        self.lib.SDL_JoystickEventState(SDL_ENABLE)
        self.lib.SDL_GameControllerEventState(SDL_ENABLE)
        self.devices: dict[int, dict[str, Any]] = {}
        self.instance_to_index: dict[int, int] = {}
        self.axis_state: dict[tuple[int, int], int] = {}
        self.last_axis_event: dict[tuple[int, int], float] = {}
        self.refresh()

    def _configure_functions(self) -> None:
        lib = self.lib
        lib.SDL_InitSubSystem.argtypes = [ctypes.c_uint32]
        lib.SDL_InitSubSystem.restype = ctypes.c_int
        lib.SDL_GetError.restype = ctypes.c_char_p
        lib.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.SDL_SetHint.restype = ctypes.c_int
        lib.SDL_JoystickEventState.argtypes = [ctypes.c_int]
        lib.SDL_JoystickEventState.restype = ctypes.c_int
        lib.SDL_GameControllerEventState.argtypes = [ctypes.c_int]
        lib.SDL_GameControllerEventState.restype = ctypes.c_int
        lib.SDL_NumJoysticks.restype = ctypes.c_int
        lib.SDL_JoystickNameForIndex.argtypes = [ctypes.c_int]
        lib.SDL_JoystickNameForIndex.restype = ctypes.c_char_p
        lib.SDL_JoystickOpen.argtypes = [ctypes.c_int]
        lib.SDL_JoystickOpen.restype = ctypes.c_void_p
        lib.SDL_JoystickClose.argtypes = [ctypes.c_void_p]
        lib.SDL_JoystickInstanceID.argtypes = [ctypes.c_void_p]
        lib.SDL_JoystickInstanceID.restype = ctypes.c_int32
        lib.SDL_JoystickUpdate.argtypes = []
        lib.SDL_PollEvent.argtypes = [ctypes.POINTER(SDLEvent)]
        lib.SDL_PollEvent.restype = ctypes.c_int
        lib.SDL_QuitSubSystem.argtypes = [ctypes.c_uint32]
        lib.SDL_QuitSubSystem.restype = None
        if hasattr(lib, "SDL_GameControllerMappingForDeviceIndex"):
            lib.SDL_GameControllerMappingForDeviceIndex.argtypes = [ctypes.c_int]
            lib.SDL_GameControllerMappingForDeviceIndex.restype = ctypes.c_void_p
            lib.SDL_free.argtypes = [ctypes.c_void_p]

    def _mapping_for_index(self, index: int) -> tuple[dict[int, str], dict[int, str]]:
        buttons: dict[int, str] = {}
        axes: dict[int, str] = {}
        function = getattr(self.lib, "SDL_GameControllerMappingForDeviceIndex", None)
        if function is None:
            return buttons, axes
        pointer = function(index)
        if not pointer:
            return buttons, axes
        try:
            mapping = ctypes.string_at(pointer).decode("utf-8", "replace")
        finally:
            self.lib.SDL_free(pointer)
        parts = mapping.split(",")
        for item in parts[2:]:
            semantic, separator, raw = item.partition(":")
            if not separator:
                continue
            button_match = re.fullmatch(r"b(\d+)", raw)
            axis_match = re.fullmatch(r"a(\d+)", raw)
            if button_match:
                buttons[int(button_match.group(1))] = controller_button_name(semantic)
            if axis_match:
                axes[int(axis_match.group(1))] = semantic
        return buttons, axes

    def refresh(self) -> None:
        for data in self.devices.values():
            if data.get("handle"):
                self.lib.SDL_JoystickClose(data["handle"])
        self.devices.clear()
        self.instance_to_index.clear()
        for index in range(max(0, self.lib.SDL_NumJoysticks())):
            handle = self.lib.SDL_JoystickOpen(index)
            if not handle:
                continue
            instance_id = int(self.lib.SDL_JoystickInstanceID(handle))
            raw_name = self.lib.SDL_JoystickNameForIndex(index)
            name = raw_name.decode("utf-8", "replace") if raw_name else f"Controle {index}"
            buttons, axes = self._mapping_for_index(index)
            self.devices[index] = {
                "handle": handle,
                "instance": instance_id,
                "name": name,
                "buttons": buttons,
                "axes": axes,
            }
            self.instance_to_index[instance_id] = index

    def status(self) -> str:
        if not self.devices:
            return "SDL: nenhum controle enumerado"
        return "SDL: " + ", ".join(
            f"{data['name']} (joy{index})" for index, data in self.devices.items()
        )

    def poll(self, callback: Any, bindings: dict[str, list[str]]) -> None:
        self.lib.SDL_JoystickUpdate()
        event = SDLEvent()
        while self.lib.SDL_PollEvent(ctypes.byref(event)):
            event_type = int(event.type)
            if event_type in (SDL_JOYDEVICEADDED, SDL_JOYDEVICEREMOVED):
                self.refresh()
                continue
            which = int(event.jbutton.which if event_type == SDL_JOYBUTTONDOWN else
                        event.jhat.which if event_type == SDL_JOYHATMOTION else
                        event.jaxis.which if event_type == SDL_JOYAXISMOTION else -1)
            index = self.instance_to_index.get(which)
            if index is None:
                continue
            device = self.devices.get(index, {})
            source = f"SDL {index} · {device.get('name', 'controle')}"
            if event_type == SDL_JOYBUTTONDOWN:
                button = int(event.jbutton.button)
                semantic = device.get("buttons", {}).get(button)
                suffix = f" ({semantic})" if semantic else ""
                tokens = [f"joy{index}Button{button}", f"joyButton{button}"]
                if callback:
                    callback(f"{source} · Botão {button}{suffix}", tokens, resolve_actions(bindings, tokens))
            elif event_type == SDL_JOYHATMOTION:
                hat, value = int(event.jhat.hat), int(event.jhat.value)
                directions = []
                for bit, name, glyph in (
                    (HAT_UP, "Up", "↑"), (HAT_RIGHT, "Right", "→"),
                    (HAT_DOWN, "Down", "↓"), (HAT_LEFT, "Left", "←"),
                ):
                    if value & bit:
                        directions.append((name, glyph))
                if directions:
                    tokens = [f"joy{index}Hat{hat}{name}" for name, _ in directions]
                    label = f"{source} · Hat {hat} {' + '.join(glyph for _, glyph in directions)}"
                    if callback:
                        callback(label, tokens, resolve_actions(bindings, tokens))
            elif event_type == SDL_JOYAXISMOTION:
                axis, value = int(event.jaxis.axis), int(event.jaxis.value)
                direction = -1 if value < -12000 else 1 if value > 12000 else 0
                key = (which, axis)
                if self.axis_state.get(key, 0) == direction:
                    continue
                self.axis_state[key] = direction
                if direction == 0:
                    continue
                now = time.monotonic()
                if now - self.last_axis_event.get(key, 0.0) < 0.12:
                    continue
                self.last_axis_event[key] = now
                suffix = "-" if direction < 0 else "+"
                semantic = device.get("axes", {}).get(axis, "")
                glyph = "←" if direction < 0 else "→"
                if semantic.endswith("y"):
                    glyph = "↑" if direction < 0 else "↓"
                display_semantic = f" ({semantic})" if semantic else ""
                tokens = [f"joy{index}Axis{axis}{suffix}"]
                if callback:
                    callback(f"{source} · Eixo {axis}{display_semantic} {glyph}",
                             tokens, resolve_actions(bindings, tokens))

    def close(self) -> None:
        for data in self.devices.values():
            if data.get("handle"):
                self.lib.SDL_JoystickClose(data["handle"])
        self.devices.clear()
        self.instance_to_index.clear()
        self.lib.SDL_QuitSubSystem(SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER)


class EvdevKeyboardInput:
    def __init__(self) -> None:
        self.devices: dict[str, Any] = {}
        self.last_scan = 0.0
        self.scan()

    def scan(self) -> None:
        now = time.monotonic()
        if now - self.last_scan < 1.5:
            return
        self.last_scan = now
        try:
            from evdev import InputDevice, ecodes
        except ImportError:
            return
        for path in glob.glob("/dev/input/event*"):
            if path in self.devices:
                continue
            try:
                device = InputDevice(path)
                keys = set(device.capabilities().get(ecodes.EV_KEY, []))
                letters = {getattr(ecodes, f"KEY_{letter}") for letter in "ASDFY"}
                keyboard_like = len(keys & letters) >= 2 and (
                    ecodes.KEY_SPACE in keys or ecodes.KEY_ENTER in keys
                )
                if not keyboard_like:
                    device.close()
                    continue
                self.devices[path] = device
            except (OSError, PermissionError):
                continue

    def status(self) -> str:
        names = []
        for device in self.devices.values():
            name = (device.name or "teclado").strip()
            if name not in names:
                names.append(name)
        if names:
            return "Teclado: " + ", ".join(names[:2])
        return "Teclado: evdev indisponível"

    def poll(self, callback: Any, bindings: dict[str, list[str]]) -> None:
        self.scan()
        if not self.devices:
            return
        fd_to_device = {device.fd: device for device in self.devices.values()}
        try:
            ready, _, _ = select.select(list(fd_to_device), [], [], 0)
        except (OSError, ValueError):
            return
        for fd in ready:
            device = fd_to_device[fd]
            while True:
                try:
                    event = device.read_one()
                except (OSError, PermissionError):
                    break
                if event is None:
                    break
                try:
                    from evdev import ecodes
                except ImportError:
                    return
                if event.type != ecodes.EV_KEY or event.value != 1:
                    continue
                key_name = evdev_key_name(event.code)
                tokens = keyboard_tokens(event.code)
                device_name = (device.name or "teclado").replace("\n", " ")[:24]
                label = f"Teclado · {device_name} · {key_name}"
                if callback:
                    callback(label, tokens, resolve_actions(bindings, tokens))

    def close(self) -> None:
        for device in self.devices.values():
            try:
                device.close()
            except OSError:
                pass
        self.devices.clear()


class X11PassThrough:
    """Make the debug panel click-through and mark it as a non-input window."""

    class WMHints(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_ulong),
            ("input", ctypes.c_int),
            ("initial_state", ctypes.c_int),
            ("icon_pixmap", ctypes.c_ulong),
            ("icon_window", ctypes.c_ulong),
            ("icon_x", ctypes.c_int),
            ("icon_y", ctypes.c_int),
            ("icon_mask", ctypes.c_ulong),
            ("window_group", ctypes.c_ulong),
        ]

    def __init__(self, window_id: int) -> None:
        x11_name = ctypes.util.find_library("X11")
        xext_name = ctypes.util.find_library("Xext")
        if not x11_name or not xext_name:
            raise RuntimeError("X11 Shape não está disponível")
        self.x11 = ctypes.CDLL(x11_name)
        self.xext = ctypes.CDLL(xext_name)
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("não consegui abrir o display X11/XWayland")
        self.x11.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(self.WMHints)]
        self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.xext.XShapeCombineRectangles.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        self.apply(window_id)

    def apply(self, window_id: int) -> None:
        hints = self.WMHints()
        hints.flags = 1  # InputHint
        hints.input = 0  # False: this window must not receive keyboard focus.
        self.x11.XSetWMHints(self.display, ctypes.c_ulong(window_id), ctypes.byref(hints))
        # ShapeInput=2, ShapeSet=0, YXBanded=3, zero rectangles = no pointer input.
        self.xext.XShapeCombineRectangles(
            self.display, ctypes.c_ulong(window_id), 2, 0, 0, None, 0, 0, 3
        )
        self.x11.XSync(self.display, 0)


class InputDebugOverlay:
    def __init__(self, root: Path, app_pid: int) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            raise RuntimeError(f"Tk não está disponível: {exc}") from exc
        self.tk = tk
        self.root_path = root
        self.app_pid = app_pid
        self.bindings = parse_controls(root / "controls.conf", include_session=os.environ.get("RETROFE_NAVIGATION_PANEL") == "1")
        self.sdl: SDLInput | None
        self.sdl_error = ""
        try:
            self.sdl = SDLInput()
        except Exception as exc:
            self.sdl = None
            self.sdl_error = str(exc)
        self.keyboard = EvdevKeyboardInput()
        self.x11_keyboard: X11KeyboardInput | None
        self.x11_error = ""
        try:
            self.x11_keyboard = X11KeyboardInput()
        except Exception as exc:
            self.x11_keyboard = None
            self.x11_error = str(exc)
        self.events: deque[tuple[str, str, str]] = deque(maxlen=4)
        self.last_geometry: tuple[str, int, int, int, int] | None = None
        self.visible = False
        self.focused = False
        self.target_window: Any = None
        self.last_window_check = 0.0
        self.pass_through: X11PassThrough | None = None
        self._build_window()
        self._log(
            "session_start",
            app_pid=self.app_pid,
            evdev_keyboard=self.keyboard.status(),
            xinput2=self.x11_keyboard.status() if self.x11_keyboard else self.x11_error,
            sdl=self.sdl.status() if self.sdl else self.sdl_error,
            controls=str(self.root_path / "controls.conf"),
        )

    def _build_window(self) -> None:
        tk = self.tk
        root = tk.Tk()
        self.window = root
        root.withdraw()
        root.title("RetroFE Input Debug")
        root.overrideredirect(True)
        root.configure(bg="#071019")
        root.attributes("-topmost", True)
        try:
            root.wm_attributes("-type", "dock")
        except tk.TclError:
            pass

        border = tk.Frame(root, bg="#0b1c28", highlightbackground="#087f9f", highlightthickness=1)
        border.pack(fill="both", expand=True)
        header = tk.Frame(border, bg="#0b1c28")
        header.pack(fill="x", padx=13, pady=(9, 2))
        tk.Label(header, text="INPUT DEBUG", bg="#0b1c28", fg="#66d9f2",
                 font=("Noto Sans", 10, "bold")).pack(side="left")
        tk.Label(header, text="observação passiva · RetroFE", bg="#0b1c28", fg="#79909c",
                 font=("Noto Sans", 9)).pack(side="right")
        self.event_label = tk.Label(border, text="Aguardando tecla, botão ou direção…",
                                    bg="#0b1c28", fg="#f0f5f7", anchor="w",
                                    font=("Noto Sans", 12, "bold"))
        self.event_label.pack(fill="x", padx=13, pady=(5, 1))
        self.action_label = tk.Label(border, text="", bg="#0b1c28", fg="#42c9e8", anchor="w",
                                     font=("Noto Sans", 11))
        self.action_label.pack(fill="x", padx=13, pady=(0, 4))
        self.history_label = tk.Label(border, text="", bg="#0b1c28", fg="#a9bac2", anchor="w",
                                      justify="left", font=("Noto Sans", 9),
                                      wraplength=730)
        self.history_label.pack(fill="x", padx=13, pady=(1, 3))
        self.status_label = tk.Label(border, text="", bg="#0b1c28", fg="#708792", anchor="w",
                                     font=("Noto Sans", 8), wraplength=730, justify="left")
        self.status_label.pack(fill="x", padx=13, pady=(0, 7))

        root.update_idletasks()
        try:
            self.pass_through = X11PassThrough(root.winfo_id())
        except Exception as exc:
            print(f"[input-debug] aviso: clique-transparente indisponível: {exc}", flush=True)
        self.status_label.configure(text=self._status_text())
        self.window.after(80, self._tick)

    def _status_text(self) -> str:
        parts = [self.keyboard.status()]
        parts.append(self.x11_keyboard.status() if self.x11_keyboard else
                     f"XInput2 indisponível: {self.x11_error[:54]}")
        if self.sdl:
            parts.append(self.sdl.status())
        else:
            parts.append(f"SDL indisponível: {self.sdl_error[:72]}")
        parts.append("Ação prevista por controls.conf; o painel não confirma consumo pelo RetroFE.")
        return "  ·  ".join(parts)

    def _record(self, source: str, tokens: list[str], actions: list[str]) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.events.appendleft((timestamp, source, action_text(actions)))
        self.event_label.configure(text=f"{source}  ·  {timestamp}")
        self.action_label.configure(text=action_text(actions))
        self.history_label.configure(
            text="\n".join(f"{stamp}  {label}  →  {action}" for stamp, label, action in list(self.events)[1:])
        )
        self._log("event", input=source, tokens=tokens, action_ids=actions,
                  expected_action=action_text(actions))

    @staticmethod
    def _log(kind: str, **fields: Any) -> None:
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": kind,
            **fields,
        }
        print("[input-debug] " + json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)

    def _set_visibility(self, show: bool, geometry: Any = None) -> None:
        if not show or geometry is None:
            if self.visible:
                self.window.withdraw()
                self.visible = False
            return
        width = min(760, max(420, geometry.frame.width - 40))
        height = min(190, max(150, geometry.frame.height - 40))
        x = geometry.frame.x + 20
        y = geometry.frame.y + 20
        new_geometry = (geometry.window_id, x, y, width, height)
        if not self.visible or new_geometry != self.last_geometry:
            self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")
            self.window.deiconify()
            self.window.attributes("-topmost", True)
            self.window.update_idletasks()
            if self.pass_through is None:
                try:
                    self.pass_through = X11PassThrough(self.window.winfo_id())
                except Exception as exc:
                    print(f"[input-debug] aviso: clique-transparente indisponível: {exc}", flush=True)
            else:
                try:
                    self.pass_through.apply(self.window.winfo_id())
                except Exception as exc:
                    print(f"[input-debug] aviso: não reapliquei a transparência: {exc}", flush=True)
            self.visible = True
            self.last_geometry = new_geometry
            self.window.after(120, lambda: self._reapply_pass_through(0))

    def _reapply_pass_through(self, attempt: int) -> None:
        if not self.visible:
            return
        try:
            if self.pass_through is None:
                self.pass_through = X11PassThrough(self.window.winfo_id())
            else:
                self.pass_through.apply(self.window.winfo_id())
        except Exception as exc:
            self._log("pass_through_warning", attempt=attempt + 1, error=str(exc))
        delays = (250, 600, 1200)
        if attempt < len(delays):
            self.window.after(delays[attempt], lambda: self._reapply_pass_through(attempt + 1))

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self.last_window_check >= 0.25:
            self.last_window_check = now
            self.target_window = find_window(pid=self.app_pid, class_hint="RetroFE", title_hint="RetroFE")
        self.focused = bool(self.target_window and active_window_id() == self.target_window.window_id)
        self._set_visibility(self.focused, self.target_window)
        callback = self._record if self.focused else None
        self.keyboard.poll(callback, self.bindings)
        if self.x11_keyboard:
            self.x11_keyboard.poll(callback, self.bindings)
        if self.sdl:
            self.sdl.poll(callback, self.bindings)
        try:
            os.kill(self.app_pid, 0)
        except ProcessLookupError:
            self.window.destroy()
            return
        except PermissionError:
            pass
        self.window.after(80, self._tick)

    def run(self) -> None:
        try:
            self.window.mainloop()
        finally:
            self._log("session_end", app_pid=self.app_pid)
            if self.x11_keyboard:
                self.x11_keyboard.close()
            self.keyboard.close()
            if self.sdl:
                self.sdl.close()
            if self.pass_through:
                try:
                    self.pass_through.x11.XCloseDisplay(self.pass_through.display)
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app-pid", type=int, required=True)
    args = parser.parse_args()
    try:
        InputDebugOverlay(args.root.resolve(), args.app_pid).run()
    except Exception as exc:
        print(f"[input-debug] erro: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
