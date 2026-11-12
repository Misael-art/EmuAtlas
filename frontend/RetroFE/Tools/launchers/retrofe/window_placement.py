#!/usr/bin/env python3
"""Read and align X11/XWayland windows used by RetroFE and Steam Input."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Rectangle:
    x: int
    y: int
    width: int
    height: int

    def valid(self) -> bool:
        return self.width >= 160 and self.height >= 120

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class Window:
    window_id: str
    pid: int | None
    wm_class: str
    title: str
    client: Rectangle
    frame: Rectangle
    frame_extents: tuple[int, int, int, int] = (0, 0, 0, 0)
    minimized: bool = False
    fullscreen: bool = False


@dataclass(frozen=True)
class Monitor:
    name: str
    outputs: tuple[str, ...]
    rectangle: Rectangle
    primary: bool


def rectangles_match(left: Rectangle, right: Rectangle, tolerance: int = 2) -> bool:
    return (abs(left.x - right.x) <= tolerance and abs(left.y - right.y) <= tolerance
            and abs(left.width - right.width) <= tolerance
            and abs(left.height - right.height) <= tolerance)


def parse_monitor_rectangles(output: str) -> list[Rectangle]:
    geometry_pattern = re.compile(r"(?P<width>\d+)/\d+x(?P<height>\d+)/\d+(?P<x>[+-]\d+)(?P<y>[+-]\d+)")
    rectangles: list[Rectangle] = []
    for line in output.splitlines():
        match = geometry_pattern.search(line)
        if not match:
            continue
        rectangles.append(Rectangle(int(match.group("x")), int(match.group("y")),
                                    int(match.group("width")), int(match.group("height"))))
    return rectangles


def parse_xrandr_monitors(output: str) -> list[Monitor]:
    """Parse X11/XWayland monitor names and rectangles from xrandr --listmonitors."""
    header_pattern = re.compile(r"^\s*\d+:\s*(?P<header>.*?)\s*$")
    geometry_pattern = re.compile(
        r"(?P<width>\d+)/\d+x(?P<height>\d+)/\d+(?P<x>[+-]\d+)(?P<y>[+-]\d+)"
    )
    monitors: list[Monitor] = []
    for line in output.splitlines():
        header = header_pattern.match(line)
        geometry = geometry_pattern.search(line)
        if not header or not geometry:
            continue
        before = line[:geometry.start()].strip().split()
        if not before:
            continue
        outputs = tuple(line[geometry.end():].split())
        name_token = before[-1].lstrip("+*")
        if not outputs:
            outputs = (name_token,)
        name = outputs[0] if len(outputs) == 1 else name_token
        monitors.append(
            Monitor(
                name=name,
                outputs=outputs,
                rectangle=Rectangle(
                    int(geometry.group("x")),
                    int(geometry.group("y")),
                    int(geometry.group("width")),
                    int(geometry.group("height")),
                ),
                primary="*" in before[-1],
            )
        )
    return monitors


def xrandr_monitors() -> list[Monitor]:
    if not shutil.which("xrandr"):
        return []
    try:
        result = run(["xrandr", "--listmonitors"], timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return parse_xrandr_monitors(result.stdout)


def active_output_name() -> str | None:
    """Mirror the ES-DE launcher: prefer the output currently active in KWin."""
    from shutil import which

    for executable in ("qdbus6", "qdbus"):
        if not which(executable):
            continue
        try:
            result = run(
                [executable, "org.kde.KWin", "/KWin", "org.kde.KWin.activeOutputName"],
                timeout=3.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        values = result.stdout.strip().splitlines()
        if result.returncode == 0 and values and re.fullmatch(r"[A-Za-z0-9_.:-]+", values[-1]):
            return values[-1]
    return None


def detect_target_monitor(output_name: str = "") -> tuple[Monitor, str]:
    monitors = xrandr_monitors()
    if not monitors:
        raise RuntimeError("xrandr não informou monitores X11/XWayland disponíveis.")

    requested = output_name.strip()
    if requested.casefold() == "auto":
        requested = ""
    if requested:
        selected = next(
            (monitor for monitor in monitors if requested == monitor.name or requested in monitor.outputs),
            None,
        )
        if selected is None:
            available = ", ".join(monitor.name for monitor in monitors)
            raise RuntimeError(f"Saída {requested!r} não encontrada no XWayland; saídas: {available}.")
        return selected, f"override:{requested}"

    active = active_output_name()
    if active:
        selected = next(
            (monitor for monitor in monitors if active == monitor.name or active in monitor.outputs),
            None,
        )
        if selected:
            return selected, f"kwin-active:{active}"

    primary = next((monitor for monitor in monitors if monitor.primary), monitors[0])
    reason = f"primary-fallback:{primary.name}"
    if active:
        reason = f"primary-fallback:kwin-{active}-unmapped:{primary.name}"
    return primary, reason


def matches_monitor_rectangle(rectangle: Rectangle) -> bool:
    try:
        result = run(["xrandr", "--listmonitors"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return any(rectangles_match(rectangle, monitor) for monitor in parse_monitor_rectangles(result.stdout))


def run(args: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False)


def backend_available() -> bool:
    from shutil import which

    return bool(os.environ.get("DISPLAY") and all(which(name) for name in ("wmctrl", "xdotool", "xprop")))


def _wmctrl_rows() -> list[tuple[str, int | None, str, str]]:
    if not backend_available():
        return []
    try:
        result = run(["wmctrl", "-l", "-p", "-G", "-x"])
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    rows: list[tuple[str, int | None, str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=9)
        if len(fields) < 8:
            continue
        window_id = fields[0]
        try:
            pid = int(fields[2]) or None
        except (ValueError, IndexError):
            pid = None
        wm_class = fields[7] if len(fields) >= 8 else ""
        # wmctrl prints the host after WM_CLASS when both -p and -x are used.
        title = fields[9] if len(fields) >= 10 else (fields[8] if len(fields) >= 9 else "")
        rows.append((window_id, pid, wm_class, title))
    return rows


def _parse_xprop(window_id: str) -> tuple[int | None, str, tuple[int, int, int, int], bool, bool]:
    try:
        result = run(["xprop", "-id", window_id, "_NET_WM_PID", "WM_CLASS", "_NET_FRAME_EXTENTS", "_NET_WM_STATE"])
    except (OSError, subprocess.TimeoutExpired):
        return None, "", (0, 0, 0, 0), False, False
    if result.returncode != 0:
        return None, "", (0, 0, 0, 0), False, False
    text = result.stdout
    pid_match = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", text)
    class_match = re.search(r'WM_CLASS\(STRING\)\s*=\s*"([^"]*)",\s*"([^"]*)"', text)
    extents_match = re.search(r"_NET_FRAME_EXTENTS\(CARDINAL\)\s*=\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)", text)
    extents = tuple(int(item) for item in extents_match.groups()) if extents_match else (0, 0, 0, 0)
    state_match = re.search(r"_NET_WM_STATE\(ATOM\)\s*=\s*(.*)", text)
    minimized = bool(state_match and "_NET_WM_STATE_HIDDEN" in state_match.group(1))
    fullscreen = bool(state_match and "_NET_WM_STATE_FULLSCREEN" in state_match.group(1))
    wm_class = ".".join(class_match.groups()) if class_match else ""
    return int(pid_match.group(1)) if pid_match else None, wm_class, extents, minimized, fullscreen


def _client_geometry(window_id: str) -> Rectangle | None:
    try:
        result = run(["xdotool", "getwindowgeometry", "--shell", window_id])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"X", "Y", "WIDTH", "HEIGHT"}:
            try:
                values[key] = int(value)
            except ValueError:
                return None
    if not all(key in values for key in ("X", "Y", "WIDTH", "HEIGHT")):
        return None
    return Rectangle(values["X"], values["Y"], values["WIDTH"], values["HEIGHT"])


def list_windows() -> list[Window]:
    windows: list[Window] = []
    for window_id, row_pid, row_class, title in _wmctrl_rows():
        client = _client_geometry(window_id)
        if client is None:
            continue
        prop_pid, prop_class, (left, right, top, bottom), minimized, fullscreen = _parse_xprop(window_id)
        frame = Rectangle(client.x - left, client.y - top,
                          client.width + left + right, client.height + top + bottom)
        if not frame.valid():
            continue
        windows.append(Window(window_id, prop_pid or row_pid, prop_class or row_class,
                              title, client, frame, (left, right, top, bottom), minimized, fullscreen))
    return windows


def active_window_id() -> str | None:
    try:
        result = run(["xdotool", "getactivewindow"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        return None
    try:
        window_number = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)
    except ValueError:
        return None
    return f"0x{window_number:08x}"


def find_window(*, pid: int | None = None, class_hint: str = "", title_hint: str = "",
                visible_only: bool = True) -> Window | None:
    windows = list_windows()
    active = active_window_id()
    class_key = class_hint.casefold()
    title_key = title_hint.casefold()
    candidates: list[tuple[int, Window]] = []
    has_hints = bool(pid or class_key or title_key)
    for window in windows:
        if visible_only and window.minimized:
            continue
        score = 0
        matched_hint = False
        if pid and window.pid == pid:
            score += 100
            matched_hint = True
        if class_key and class_key in window.wm_class.casefold():
            score += 50
            matched_hint = True
        if title_key and title_key in window.title.casefold():
            score += 30
            matched_hint = True
        if has_hints and not matched_hint:
            continue
        if window.window_id == active:
            score += 10
        if score:
            candidates.append((score, window))
    return max(candidates, key=lambda item: (item[0], item[1].frame.width * item[1].frame.height))[1] if candidates else None


def window_by_id(window_id: str | None) -> Window | None:
    if not window_id:
        return None
    listed = next((window for window in list_windows() if window.window_id == window_id), None)
    if listed:
        return listed
    # Override-redirect windows are intentionally absent from wmctrl's EWMH
    # client list. Read their X11 properties directly so the borderless
    # transition overlay can still be measured and verified.
    client = _client_geometry(window_id)
    if client is None:
        return None
    pid, wm_class, (left, right, top, bottom), minimized, fullscreen = _parse_xprop(window_id)
    try:
        title_result = run(["xdotool", "getwindowname", window_id])
    except (OSError, subprocess.TimeoutExpired):
        title_result = subprocess.CompletedProcess([], 1, "", "")
    title = title_result.stdout.strip() if title_result.returncode == 0 else ""
    frame = Rectangle(client.x - left, client.y - top,
                      client.width + left + right, client.height + top + bottom)
    if not frame.valid():
        return None
    return Window(window_id, pid, wm_class, title, client, frame,
                  (left, right, top, bottom), minimized, fullscreen)


def place_window(window_id: str, rectangle: Rectangle, *, activate: bool = False) -> bool:
    if not backend_available() or not rectangle.valid():
        return False
    if activate:
        try:
            run(["wmctrl", "-ir", window_id, "-b", "remove,fullscreen,maximized_vert,maximized_horz"])
            run(["wmctrl", "-ia", window_id])
            time.sleep(0.12)
        except (OSError, subprocess.TimeoutExpired):
            pass
    current = window_by_id(window_id)
    if current is None:
        return False
    left, right, top, bottom = current.frame_extents
    # xdotool windowmove positions the managed window's outer frame on KWin;
    # only windowsize uses the client dimensions after subtracting extents.
    frame_x = rectangle.x
    frame_y = rectangle.y
    client_width = rectangle.width - left - right
    client_height = rectangle.height - top - bottom
    if client_width < 1 or client_height < 1:
        return False
    try:
        moved = run(["xdotool", "windowmove", "--sync", window_id, str(frame_x), str(frame_y)])
        sized = run(["xdotool", "windowsize", "--sync", window_id, str(client_width), str(client_height)])
    except (OSError, subprocess.TimeoutExpired):
        return False
    if moved.returncode != 0 or sized.returncode != 0:
        return False
    actual = window_by_id(window_id)
    if actual is None:
        return False
    return rectangles_match(actual.frame, rectangle)


def set_fullscreen(window_id: str, enabled: bool, timeout: float = 1.5) -> bool:
    if not backend_available():
        return False
    action = "add" if enabled else "remove"
    try:
        result = run(["wmctrl", "-ir", window_id, "-b", f"{action},fullscreen"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        current = window_by_id(window_id)
        if current and current.fullscreen == enabled:
            return True
        time.sleep(0.05)
    return False


def find_window_id_by_title(title: str) -> str | None:
    managed = next((window.window_id for window in list_windows() if window.title == title), None)
    if managed:
        return managed
    if not backend_available():
        return None
    pattern = "^" + re.escape(title) + "$"
    try:
        result = run(["xdotool", "search", "--onlyvisible", "--name", pattern])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for window_id in result.stdout.splitlines():
        window = window_by_id(window_id.strip())
        if window and window.title == title:
            return window.window_id
    return None


def wait_for_steam_window(before_ids: set[str], timeout: float = 10.0) -> Window | None:
    deadline = time.monotonic() + timeout
    last_candidate: Window | None = None
    while time.monotonic() < deadline:
        windows = list_windows()
        active = active_window_id()
        steam = [window for window in windows if not window.minimized and "steam" in
                 (window.wm_class + " " + window.title).casefold()]
        active_steam = next((window for window in steam if window.window_id == active), None)
        if active_steam:
            return active_steam
        new_steam = [window for window in steam if window.window_id not in before_ids]
        if new_steam:
            last_candidate = max(new_steam, key=lambda window: window.frame.width * window.frame.height)
        elif steam:
            last_candidate = max(steam, key=lambda window: window.frame.width * window.frame.height)
        if last_candidate:
            return last_candidate
        time.sleep(0.15)
    return last_candidate


def choose_controller_window(windows: list[Window], active: str | None,
                             before_ids: set[str], *, allow_generic: bool = False) -> Window | None:
    steam = [window for window in windows if not window.minimized and "steam" in
             (window.wm_class + " " + window.title).casefold()]
    controller = [window for window in steam if any(word in window.title.casefold() for word in
                   ("controller", "layout", "gamepad", "steam input", "configuration"))]
    if controller:
        return (next((window for window in controller if window.window_id == active), None)
                or next((window for window in controller if window.window_id not in before_ids), None)
                or max(controller, key=lambda window: window.frame.width * window.frame.height))
    if not allow_generic or not steam:
        return None
    return (next((window for window in steam if window.window_id == active), None)
            or next((window for window in steam if window.window_id not in before_ids), None)
            or max(steam, key=lambda window: window.frame.width * window.frame.height))


def wait_for_controller_window(before_ids: set[str], timeout: float = 12.0,
                               generic_grace: float = 2.0) -> Window | None:
    deadline = time.monotonic() + timeout
    fallback_deadline = time.monotonic() + max(0.0, min(generic_grace, timeout))
    fallback: Window | None = None
    while time.monotonic() < deadline:
        windows = list_windows()
        active = active_window_id()
        controller = choose_controller_window(windows, active, before_ids)
        if controller:
            return controller
        fallback = choose_controller_window(windows, active, before_ids, allow_generic=True) or fallback
        if fallback and time.monotonic() >= fallback_deadline:
            return fallback
        time.sleep(0.15)
    return fallback


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError(f"O estado de posicionamento é symlink e foi preservado: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def start_restore_monitor(state_file: Path, *, steam_window: Window, retrofe_window_id: str,
                          original: Rectangle | None, original_minimized: bool,
                          original_fullscreen: bool,
                          additional_windows: list[dict[str, Any]] | None = None,
                          timeout: float = 1800.0) -> int:
    if state_file.is_file() and not state_file.is_symlink():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
            previous_pid = int(previous.get("watcher_pid", 0))
            command_line = Path(f"/proc/{previous_pid}/cmdline").read_bytes() if previous_pid > 0 else b""
            if b"window_placement.py\x00watch-return" in command_line:
                return previous_pid
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    state = {
        "schema_version": 1,
        "steam_window_id": steam_window.window_id,
        "retrofe_window_id": retrofe_window_id,
        "original_geometry": original.as_dict() if original else None,
        "original_minimized": original_minimized,
        "original_fullscreen": original_fullscreen,
        "additional_windows": additional_windows or [],
        "timeout_seconds": timeout,
        "created_at_utc": utc_now(),
    }
    atomic_json(state_file, state)
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch-return",
                               "--state-file", str(state_file)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    state["watcher_pid"] = process.pid
    atomic_json(state_file, state)
    return process.pid


def _restore_window(window_id: str, original_data: Any, original_minimized: bool,
                    original_fullscreen: bool) -> None:
    window = window_by_id(window_id)
    if not window:
        return
    set_fullscreen(window_id, original_fullscreen)
    if isinstance(original_data, dict):
        try:
            rectangle = Rectangle(*(int(original_data[key]) for key in ("x", "y", "width", "height")))
        except (KeyError, TypeError, ValueError):
            rectangle = None
        if rectangle and rectangle.valid() and not original_fullscreen:
            place_window(window_id, rectangle, activate=False)
    if original_minimized or not original_data:
        try:
            run(["xdotool", "windowminimize", window_id])
        except (OSError, subprocess.TimeoutExpired):
            pass


def _restore(state: dict[str, Any]) -> None:
    _restore_window(str(state.get("steam_window_id", "")), state.get("original_geometry"),
                    bool(state.get("original_minimized", False)),
                    bool(state.get("original_fullscreen", False)))
    related = state.get("additional_windows", [])
    if not isinstance(related, list):
        return
    for item in related:
        if not isinstance(item, dict):
            continue
        _restore_window(str(item.get("window_id", "")), item.get("original_geometry"),
                        bool(item.get("original_minimized", True)),
                        bool(item.get("original_fullscreen", False)))


def watch_return(state_file: Path) -> int:
    if state_file.is_symlink() or not state_file.is_file():
        return 2
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        timeout = max(10.0, min(3600.0, float(state.get("timeout_seconds", 1800))))
    except (OSError, ValueError, json.JSONDecodeError):
        return 2
    deadline = time.monotonic() + timeout
    retrofe_id = str(state.get("retrofe_window_id", ""))
    steam_id = str(state.get("steam_window_id", ""))
    while time.monotonic() < deadline:
        windows = list_windows()
        by_id = {window.window_id: window for window in windows}
        if steam_id not in by_id:
            break
        retrofe = by_id.get(retrofe_id)
        if not retrofe:
            break
        if active_window_id() == retrofe_id:
            break
        time.sleep(0.35)
    _restore(state)
    try:
        if not state_file.is_symlink():
            state_file.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    if not backend_available():
        print(json.dumps({"available": False, "reason": "wmctrl, xdotool e xprop com DISPLAY ativo são necessários"}, ensure_ascii=False))
        return 2
    target = find_window(pid=args.pid, class_hint=args.class_hint, title_hint=args.title_hint)
    if not target:
        print(json.dumps({"available": True, "found": False}, ensure_ascii=False))
        return 1
    print(json.dumps({"available": True, "found": True, "window_id": target.window_id,
                      "pid": target.pid, "wm_class": target.wm_class, "title": target.title,
                      "geometry": target.frame.as_dict(), "minimized": target.minimized}, ensure_ascii=False))
    return 0


def command_monitor_target(args: argparse.Namespace) -> int:
    try:
        monitor, reason = detect_target_monitor(args.output_name)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rect = monitor.rectangle
    print("\t".join((monitor.name, str(rect.x), str(rect.y), str(rect.width),
                     str(rect.height), reason)))
    return 0


def wait_for_window_pid(pid: int, timeout: float) -> Window | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        target = find_window(pid=pid, visible_only=False)
        if target:
            return target
        time.sleep(0.15)
    return find_window(pid=pid, visible_only=False)


def command_place_retrofe(args: argparse.Namespace) -> int:
    if not backend_available():
        print("wmctrl, xdotool, xprop e DISPLAY são necessários para posicionar a janela.", file=sys.stderr)
        return 2

    monitor = Rectangle(args.monitor_x, args.monitor_y, args.monitor_width, args.monitor_height)
    if not monitor.valid() or args.width < 160 or args.height < 120:
        print("A geometria do monitor ou da janela é inválida.", file=sys.stderr)
        return 2

    fullscreen = args.fullscreen == "yes"
    window = wait_for_window_pid(args.pid, args.timeout)
    if window is None:
        print(f"Não encontrei a janela RetroFE do PID {args.pid} após {args.timeout:g}s.", file=sys.stderr)
        return 1

    if window.fullscreen and not set_fullscreen(window.window_id, False):
        print("Não consegui retirar o fullscreen temporariamente para mover o RetroFE.", file=sys.stderr)
        return 1

    window = window_by_id(window.window_id) or window
    if fullscreen:
        target_rect = monitor
    else:
        left, right, top, bottom = window.frame_extents
        frame_width = args.width + left + right
        frame_height = args.height + top + bottom
        x = monitor.x + max(0, (monitor.width - frame_width) // 2)
        y = monitor.y + max(0, (monitor.height - frame_height) // 2)
        target_rect = Rectangle(x, y, frame_width, frame_height)

    placed = False
    for _ in range(6):
        if place_window(window.window_id, target_rect, activate=False):
            placed = True
            break
        time.sleep(0.15)
    if not placed:
        print("O compositor não confirmou a posição/tamanho solicitado para a janela RetroFE.", file=sys.stderr)
        return 1

    if fullscreen and not set_fullscreen(window.window_id, True):
        print("A janela foi posicionada, mas o compositor não confirmou fullscreen.", file=sys.stderr)
        return 1

    actual = window_by_id(window.window_id)
    payload = {
        "window_id": window.window_id,
        "pid": args.pid,
        "fullscreen": fullscreen,
        "geometry": actual.frame.as_dict() if actual else target_rect.as_dict(),
        "target_geometry": target_rect.as_dict(),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="lê a geometria de uma janela X11/XWayland")
    inspect.add_argument("--pid", type=int)
    inspect.add_argument("--class-hint", default="")
    inspect.add_argument("--title-hint", default="")
    monitor = commands.add_parser("monitor-target", help="resolve a saída ativa do KWin para a geometria X11/XWayland")
    monitor.add_argument("--output-name", default="", help="conector explícito, ou auto para a saída ativa")
    place = commands.add_parser("place-retrofe", help="posiciona a janela RetroFE na saída escolhida antes do frontend ficar pronto")
    place.add_argument("--pid", type=int, required=True)
    place.add_argument("--monitor-x", type=int, required=True)
    place.add_argument("--monitor-y", type=int, required=True)
    place.add_argument("--monitor-width", type=int, required=True)
    place.add_argument("--monitor-height", type=int, required=True)
    place.add_argument("--width", type=int, required=True)
    place.add_argument("--height", type=int, required=True)
    place.add_argument("--fullscreen", choices=("yes", "no"), default="no")
    place.add_argument("--timeout", type=float, default=8.0)
    watcher = commands.add_parser("watch-return", help=argparse.SUPPRESS)
    watcher.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inspect":
        return command_inspect(args)
    if args.command == "monitor-target":
        return command_monitor_target(args)
    if args.command == "place-retrofe":
        return command_place_retrofe(args)
    return watch_return(args.state_file)


if __name__ == "__main__":
    raise SystemExit(main())
