#!/usr/bin/env python3
"""Render a portable Aeon Nox-style theme transition and request a clean exit."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from window_placement import (
    Rectangle,
    backend_available,
    find_window,
    find_window_id_by_title,
    place_window,
    window_by_id,
)


DEFAULT_ROOT = Path(__file__).resolve().parents[3]
PENDING_REL = Path("UserData/retrofe/theme-switch-pending.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError(f"Arquivo de transição é symlink e foi preservado: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.is_file():
        return dict(default or {})
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Estado de transição inválido: {path}")
    return value


def set_control(state_dir: Path, **updates: Any) -> None:
    control_path = state_dir / "control.json"
    control = read_json(control_path, {"visible": False, "release": False, "message": "Preparando a interface"})
    control.update(updates)
    control["updated_at_utc"] = utc_now()
    write_json(control_path, control)


def send_clean_quit(pid: int) -> None:
    """Send the same Q key the user would press; never signal the frontend process."""
    if not process_alive(pid):
        return
    try:
        from evdev import UInput, ecodes
    except ImportError as exc:
        raise RuntimeError("o módulo Python evdev não está disponível neste host") from exc

    try:
        keyboard = UInput(events={ecodes.EV_KEY: [ecodes.KEY_Q]}, name="RetroFE Theme Switch")
    except (OSError, PermissionError) as exc:
        raise RuntimeError(f"não consegui abrir /dev/uinput: {exc}") from exc
    try:
        keyboard.write(ecodes.EV_KEY, ecodes.KEY_Q, 1)
        keyboard.syn()
        time.sleep(0.08)
        keyboard.write(ecodes.EV_KEY, ecodes.KEY_Q, 0)
        keyboard.syn()
    except OSError as exc:
        raise RuntimeError(f"não consegui enviar a tecla Q: {exc}") from exc
    finally:
        keyboard.close()


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def command_begin(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    if args.state_dir.is_symlink():
        raise ValueError(f"Diretório de transição é symlink e foi preservado: {args.state_dir}")
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending_path = root / PENDING_REL
    if not pending_path.is_file():
        raise ValueError("não há uma troca de tema pendente para apresentar")
    pending = read_json(pending_path)
    theme_label = str(pending.get("theme_label", pending.get("theme_id", "Novo tema")))
    app_pid = int(args.app_pid or 0)
    if not backend_available():
        raise RuntimeError("não há wmctrl, xdotool, xprop e DISPLAY para ancorar a transição à janela do RetroFE")
    target = find_window(pid=app_pid, class_hint="RetroFE", title_hint="RetroFE")
    if not target:
        raise RuntimeError("não encontrei a janela X11/XWayland do RetroFE; não vou abrir a transição em outra tela")
    target_geometry = target.frame
    if not target_geometry.valid():
        raise RuntimeError("a janela do RetroFE tem dimensões insuficientes para a transição")
    write_json(state_dir / "target.json", {
        "target_pid": app_pid,
        "window_id": target.window_id,
        "geometry": target_geometry.as_dict(),
        "updated_at_utc": utc_now(),
    })
    write_json(state_dir / "control.json", {
        "visible": False,
        "release": False,
        "message": "Preparando a interface",
        "theme_label": theme_label,
        "created_at_utc": utc_now(),
    })
    error_log = state_dir / "overlay.log"
    with error_log.open("ab") as stream:
        overlay = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "overlay", "--state-dir", str(state_dir), "--theme", theme_label],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stream,
            start_new_session=True,
            close_fds=True,
        )
    write_json(state_dir / "overlay.json", {"pid": overlay.pid, "started_at_utc": utc_now()})

    ready_path = state_dir / "ready.json"
    deadline = time.monotonic() + max(0.5, args.ready_timeout)
    while time.monotonic() < deadline and process_alive(overlay.pid) and not ready_path.is_file():
        time.sleep(0.04)
    if not ready_path.is_file():
        error = state_dir / "overlay-error.json"
        detail = error.read_text(encoding="utf-8") if error.is_file() else "a janela de transição não ficou pronta"
        set_control(state_dir, release=True, visible=False, message="")
        raise RuntimeError(detail.strip())

    if process_alive(app_pid):
        try:
            send_clean_quit(app_pid)
        except RuntimeError as exc:
            set_control(state_dir, release=True, visible=False, message="")
            raise RuntimeError(f"saída automática indisponível: {exc}") from exc
        message = f"Encerrando o tema atual para abrir {theme_label}"
    else:
        message = f"Abrindo {theme_label}"

    set_control(state_dir, visible=True, release=False, message=message)
    shown_path = state_dir / "shown.json"
    shown_deadline = time.monotonic() + 3.0
    while time.monotonic() < shown_deadline and process_alive(overlay.pid) and not shown_path.is_file():
        time.sleep(0.04)
    if not shown_path.is_file():
        detail_path = state_dir / "overlay-error.json"
        detail = read_json(detail_path, {}).get("reason", "a camada não ocupou a janela do RetroFE")
        set_control(state_dir, release=True, visible=False, message="")
        raise RuntimeError(str(detail))
    print(f"Transição visual ancorada à janela {target_geometry.width}x{target_geometry.height}+{target_geometry.x}+{target_geometry.y}; sessão encerrando normalmente.")
    return 0


def command_retarget(args: argparse.Namespace) -> int:
    state_dir = args.state_dir.resolve()
    target_path = state_dir / "target.json"
    target_state = read_json(target_path)
    target_state["target_pid"] = int(args.app_pid or 0)
    target_state["updated_at_utc"] = utc_now()
    target = find_window(pid=int(args.app_pid or 0), class_hint="RetroFE", title_hint="RetroFE")
    if target:
        target_state["window_id"] = target.window_id
        target_state["geometry"] = target.frame.as_dict()
    write_json(target_path, target_state)
    print("Janela de transição vinculada ao novo processo RetroFE; aguardando a janela aparecer." if not target else
          f"Alvo atualizado para {target.frame.width}x{target.frame.height}+{target.frame.x}+{target.frame.y}.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    state_dir = args.state_dir.resolve()
    control_path = state_dir / "control.json"
    control = read_json(control_path)
    if control.get("release"):
        return 0
    set_control(state_dir, message=args.message)
    return 0


def command_release(args: argparse.Namespace) -> int:
    state_dir = args.state_dir.resolve()
    control_path = state_dir / "control.json"
    control = read_json(control_path)
    set_control(state_dir, visible=bool(control.get("visible")), release=True, message=args.message)
    return 0


def blend(left: tuple[int, int, int], right: tuple[int, int, int], fraction: float) -> str:
    values = [round(a + (b - a) * fraction) for a, b in zip(left, right)]
    return "#" + "".join(f"{value:02x}" for value in values)


def draw_scene(canvas: Any, tk: Any, width: int, height: int, theme: str) -> dict[str, Any]:
    canvas.delete("all")
    left = (6, 10, 19)
    right = (12, 24, 39)
    strips = 72
    strip_width = width / strips
    for index in range(strips):
        color = blend(left, right, index / max(1, strips - 1))
        x1 = int(index * strip_width)
        x2 = int((index + 1) * strip_width) + 1
        canvas.create_rectangle(x1, 0, x2, height, fill=color, outline=color)

    scale = max(0.55, min(min(width / 1920.0, height / 1080.0), 1.35))
    cx, cy = width / 2.0, height / 2.0
    panel_width = min(width * 0.86, 720 * scale)
    panel_height = min(height * 0.82, 430 * scale)
    x1, y1 = cx - panel_width / 2, cy - panel_height / 2
    x2, y2 = cx + panel_width / 2, cy + panel_height / 2
    for line in range(32):
        y = (line * height / 31) + 0.5
        canvas.create_line(0, y, width, y, fill="#0a1320", width=1)
    canvas.create_rectangle(x1, y1, x2, y2, fill="#090f1a", outline="#183149", width=max(1, int(1.4 * scale)))
    canvas.create_line(x1 + 1, y1 + 2, x2 - 1, y1 + 2, fill="#00bde8", width=max(1, int(2 * scale)))

    ring_radius = 54 * scale
    ring_y = cy - 74 * scale
    canvas.create_oval(cx - ring_radius, ring_y - ring_radius, cx + ring_radius, ring_y + ring_radius,
                       outline="#19374d", width=max(2, int(4 * scale)))
    arc = canvas.create_arc(cx - ring_radius, ring_y - ring_radius, cx + ring_radius, ring_y + ring_radius,
                            start=15, extent=105, style=tk.ARC, outline="#00c8f2", width=max(2, int(5 * scale)))
    dot = canvas.create_oval(cx - 5 * scale, ring_y - ring_radius - 5 * scale,
                             cx + 5 * scale, ring_y - ring_radius + 5 * scale,
                             fill="#7deaff", outline="")
    canvas.create_oval(cx - 7 * scale, ring_y - 7 * scale, cx + 7 * scale, ring_y + 7 * scale,
                       fill="#10283a", outline="#2d6a86", width=max(1, int(scale)))

    small_font = max(8, int(13 * scale))
    body_font = max(10, int(17 * scale))
    title_font = max(15, int(25 * scale))
    theme_font = max(13, int(22 * scale))
    canvas.create_text(cx, y1 + 39 * scale, text="AEON NOX   /   RETROFE",
                       fill="#77a5b8", font=("Noto Sans", small_font, "bold"), justify="center")
    canvas.create_text(cx, cy + 14 * scale, text="APLICANDO TEMA",
                       fill="#eef7fb", font=("Noto Sans", title_font, "bold"), justify="center")
    theme_item = canvas.create_text(cx, cy + 53 * scale, text=theme, fill="#00c8f2",
                                    font=("Noto Sans", theme_font, "bold"),
                                    width=panel_width - 80 * scale, justify="center")
    message_item = canvas.create_text(cx, cy + 96 * scale, text="Preparando a interface",
                                     fill="#a7bbc8", font=("Noto Sans", body_font), justify="center")

    bar_width = max(120 * scale, min(panel_width - 160 * scale, 420 * scale))
    bar_left, bar_right = cx - bar_width / 2, cx + bar_width / 2
    bar_y = y2 - 54 * scale
    canvas.create_rectangle(bar_left, bar_y, bar_right, bar_y + 5 * scale, fill="#182738", outline="")
    progress = canvas.create_rectangle(bar_left, bar_y, bar_left + bar_width * 0.24, bar_y + 5 * scale,
                                       fill="#00bde8", outline="")
    canvas.create_text(cx, y2 - 24 * scale, text="Aguarde um instante", fill="#688493",
                       font=("Noto Sans", max(8, int(10 * scale))), justify="center")
    return {"arc": arc, "dot": dot, "progress": progress, "message": message_item,
            "center_x": cx, "center_y": cy, "ring_radius": ring_radius, "scale": scale,
            "bar_width": bar_width, "bar_left": bar_left, "bar_y": bar_y,
            "width": width, "height": height}


def command_overlay(args: argparse.Namespace) -> int:
    state_dir = args.state_dir.resolve()
    try:
        import tkinter as tk
    except ImportError as exc:
        write_json(state_dir / "overlay-error.json", {"reason": f"Tk não está disponível: {exc}"})
        return 2

    root: Any = None
    try:
        if not backend_available():
            raise RuntimeError("wmctrl, xdotool, xprop e DISPLAY são necessários para ancorar a camada")
        target_state_path = state_dir / "target.json"
        target_state = read_json(target_state_path)
        geometry_data = target_state.get("geometry")
        if not isinstance(geometry_data, dict):
            raise RuntimeError("a posição da janela RetroFE ainda não foi informada")
        geometry = Rectangle(*(int(geometry_data[key]) for key in ("x", "y", "width", "height")))
        if not geometry.valid():
            raise RuntimeError("a posição da janela RetroFE é inválida")
        title = f"RetroFE Theme Transition {state_dir.name}"
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.title(title)
        width, height = geometry.width, geometry.height
        root.geometry(f"{width}x{height}{geometry.x:+d}{geometry.y:+d}")
        try:
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, bd=0, bg="#070b13")
        canvas.pack(fill="both", expand=True)
        scene = draw_scene(canvas, tk, width, height, args.theme)
        opened_at = time.monotonic()
        offset = 0.0
        direction = 1
        fading = False
        fade_started = 0.0
        dismissed = False
        revealed = False
        overlay_window_id: str | None = None
        last_geometry = geometry
        next_target_check = 0.0

        def show_and_place(*, reveal: bool) -> bool:
            nonlocal overlay_window_id, last_geometry, scene, offset, direction, revealed
            root.attributes("-alpha", 0.0)
            root.attributes("-topmost", True)
            root.deiconify()
            root.update()
            target_state = read_json(target_state_path, {})
            try:
                target_pid = int(target_state.get("target_pid", 0))
            except (TypeError, ValueError):
                target_pid = 0
            target = find_window(pid=target_pid, class_hint="RetroFE", title_hint="RetroFE")
            if target:
                last_geometry = target.frame
            root.geometry(f"{last_geometry.width}x{last_geometry.height}{last_geometry.x:+d}{last_geometry.y:+d}")
            if (last_geometry.width, last_geometry.height) != (scene["width"], scene["height"]):
                canvas.config(width=last_geometry.width, height=last_geometry.height)
                scene = draw_scene(canvas, tk, last_geometry.width, last_geometry.height, args.theme)
                offset, direction = 0.0, 1
            root.update()
            deadline = time.monotonic() + 1.6
            last_actual: dict[str, int] | None = None
            while time.monotonic() < deadline:
                overlay_window_id = find_window_id_by_title(title)
                if overlay_window_id and place_window(overlay_window_id, last_geometry):
                    root.update()
                    actual = window_by_id(overlay_window_id)
                    if actual is None:
                        root.update()
                        time.sleep(0.04)
                        continue
                    last_actual = actual.frame.as_dict()
                    if (abs(actual.frame.x - last_geometry.x) > 2
                            or abs(actual.frame.y - last_geometry.y) > 2
                            or abs(actual.frame.width - last_geometry.width) > 2
                            or abs(actual.frame.height - last_geometry.height) > 2):
                        root.update()
                        time.sleep(0.04)
                        continue
                    root.attributes("-topmost", True)
                    root.lift()
                    if reveal:
                        root.attributes("-alpha", 0.98)
                        revealed = True
                        write_json(state_dir / "shown.json", {
                            "shown_at_utc": utc_now(), "window_id": overlay_window_id,
                            "geometry": actual.frame.as_dict(),
                        })
                    else:
                        write_json(state_dir / "ready.json", {
                            "ready_at_utc": utc_now(), "anchored": True,
                            "window_id": overlay_window_id, "geometry": actual.frame.as_dict(),
                        })
                    return True
                if overlay_window_id:
                    actual = window_by_id(overlay_window_id)
                    last_actual = actual.frame.as_dict() if actual else None
                root.update()
                time.sleep(0.04)
            write_json(state_dir / "overlay-error.json", {
                "reason": "não consegui posicionar a transição na janela do RetroFE",
                "failed_at_utc": utc_now(),
                "window_id": overlay_window_id,
                "target_geometry": last_geometry.as_dict(),
                "actual_geometry": last_actual,
            })
            return False

        def tick() -> None:
            nonlocal offset, direction, fading, fade_started, dismissed, next_target_check, last_geometry, scene
            if dismissed:
                return
            control = read_json(state_dir / "control.json", {})
            if control.get("visible") and not revealed:
                try:
                    if not show_and_place(reveal=True):
                        dismissed = True
                        root.destroy()
                        write_json(state_dir / "closed.json", {"closed_at_utc": utc_now(), "reason": "positionamento não confirmado"})
                        return
                except tk.TclError as exc:
                    write_json(state_dir / "overlay-error.json", {"reason": str(exc), "failed_at_utc": utc_now()})
                    dismissed = True
                    root.destroy()
                    return
            if control.get("visible") and overlay_window_id and time.monotonic() >= next_target_check:
                next_target_check = time.monotonic() + 0.25
                target_state = read_json(target_state_path, {})
                try:
                    target_pid = int(target_state.get("target_pid", 0))
                except (TypeError, ValueError):
                    target_pid = 0
                target = find_window(pid=target_pid, class_hint="RetroFE", title_hint="RetroFE")
                if target and target.frame != last_geometry:
                    if (target.frame.width, target.frame.height) != (last_geometry.width, last_geometry.height):
                        root.geometry(f"{target.frame.width}x{target.frame.height}{target.frame.x:+d}{target.frame.y:+d}")
                        canvas.config(width=target.frame.width, height=target.frame.height)
                        scene = draw_scene(canvas, tk, target.frame.width, target.frame.height, args.theme)
                        offset, direction = 0.0, 1
                    if place_window(overlay_window_id, target.frame):
                        last_geometry = target.frame
                        write_json(state_dir / "shown.json", {
                            "shown_at_utc": utc_now(), "window_id": overlay_window_id,
                            "geometry": last_geometry.as_dict(),
                        })
            current_message = str(control.get("message", "Preparando a interface"))
            canvas.itemconfigure(scene["message"], text=current_message)
            if control.get("release") and not fading:
                if not control.get("visible"):
                    dismissed = True
                    root.destroy()
                    write_json(state_dir / "closed.json", {"closed_at_utc": utc_now()})
                    return
                fading = True
                fade_started = time.monotonic()
            elapsed = time.monotonic() - opened_at
            canvas.itemconfigure(scene["arc"], start=int((elapsed * 130) % 360))
            angle = (elapsed * 2.1) % 6.283185307
            cx, cy = scene["center_x"], scene["center_y"]
            ring_radius, scale = scene["ring_radius"], scene["scale"]
            ring_y = cy - 74 * scale
            dx = cx + (ring_radius - 1 * scale) * math.cos(angle)
            dy = ring_y - (ring_radius - 1 * scale) * math.sin(angle)
            dot_radius = 5 * scale
            canvas.coords(scene["dot"], dx - dot_radius, dy - dot_radius, dx + dot_radius, dy + dot_radius)
            bar_width, bar_left, bar_y = scene["bar_width"], scene["bar_left"], scene["bar_y"]
            offset += direction * max(2.2 * scale, bar_width * 0.013)
            fill_width = bar_width * 0.24
            if offset >= bar_width - fill_width:
                offset = bar_width - fill_width
                direction = -1
            elif offset <= 0:
                offset = 0
                direction = 1
            canvas.coords(scene["progress"], bar_left + offset, bar_y,
                          bar_left + offset + fill_width, bar_y + 5 * scale)
            if fading:
                fade_fraction = min(1.0, (time.monotonic() - fade_started) / 0.55)
                try:
                    root.attributes("-alpha", max(0.0, 0.98 * (1.0 - fade_fraction)))
                except tk.TclError:
                    if fade_fraction >= 0.25:
                        fade_fraction = 1.0
                if fade_fraction >= 1.0:
                    dismissed = True
                    root.destroy()
                    write_json(state_dir / "closed.json", {"closed_at_utc": utc_now()})
                    return
            elif time.monotonic() - opened_at > 120:
                dismissed = True
                root.destroy()
                write_json(state_dir / "closed.json", {"closed_at_utc": utc_now(), "reason": "timeout"})
                return
            root.after(34, tick)

        # Map and position the transparent layer before telling the supervisor it
        # is safe to send Q. A placement failure must leave RetroFE untouched.
        if not show_and_place(reveal=False):
            root.destroy()
            return 2

        root.after(34, tick)
        root.mainloop()
        return 0
    except Exception as exc:
        write_json(state_dir / "overlay-error.json", {"reason": str(exc), "failed_at_utc": utc_now()})
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    begin = commands.add_parser("begin", help="prepara a tela e pede um encerramento normal por Q")
    begin.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    begin.add_argument("--state-dir", type=Path, required=True)
    begin.add_argument("--app-pid", type=int, required=True)
    begin.add_argument("--ready-timeout", type=float, default=4.0)

    retarget = commands.add_parser("retarget", help="acompanha a nova janela depois de reiniciar o RetroFE")
    retarget.add_argument("--state-dir", type=Path, required=True)
    retarget.add_argument("--app-pid", type=int, required=True)

    status = commands.add_parser("status", help="atualiza o texto da transição")
    status.add_argument("--state-dir", type=Path, required=True)
    status.add_argument("--message", required=True)

    release = commands.add_parser("release", help="esmaece e encerra a tela de transição")
    release.add_argument("--state-dir", type=Path, required=True)
    release.add_argument("--message", default="A interface está pronta")

    overlay = commands.add_parser("overlay", help=argparse.SUPPRESS)
    overlay.add_argument("--state-dir", type=Path, required=True)
    overlay.add_argument("--theme", default="Novo tema")

    args = parser.parse_args()
    try:
        if args.command == "begin":
            return command_begin(args)
        if args.command == "retarget":
            return command_retarget(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "release":
            return command_release(args)
        if args.command == "overlay":
            return command_overlay(args)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"TRANSIÇÃO: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
