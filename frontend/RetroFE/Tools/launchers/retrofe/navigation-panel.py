#!/usr/bin/env python3
"""Context-preserving Options for RetroFE, anchored to its actual client window.

Start/F10 is reserved for this panel and must have no engine action. SDL events
remain in this process while the focusable panel is open; no menu navigation is
injected into RetroFE. The existing input observer and host option tools are reused.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import importlib.util
import json
import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
import window_placement as windows


SCRIPT_DIR = Path(__file__).resolve().parent
OPTIONS_KEY = "F10"
OPTIONS_BUTTON = "joyButton7"


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


inputs = module("retrofe_input_observer", "input-debug-overlay.py")
display = module("retrofe_display_preferences", "display-profile.py")
transition = module("retrofe_clean_exit", "theme-transition.py")
feedback = module("retrofe_session_feedback", "session-feedback.py")


def controller_backend(root):
    directory = root.resolve().parents[1] / "tools/roms"
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("retrofe_controller_routing", directory / "controller_session.py")
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    # Resolve the same configuration dependencies used by assignment callbacks.
    importlib.import_module("emulator_controls")
    return routing


def navigation_contract(root, theme_id=None):
    """The session owns recovery/exit; themes may style their presentation."""
    registry = json.loads((root / "Config/themes.json").read_text())
    if theme_id is None:
        saved = root / "UserData/retrofe/theme-profile.json"
        theme_id = json.loads(saved.read_text())["theme_id"] if saved.exists() else registry["default_theme_id"]
    theme = next((t for t in registry["themes"] if t["id"] == theme_id), None)
    if theme is None:
        raise RuntimeError(f"Tema sem contrato de navegação: {theme_id}")
    contract = theme.get("session_navigation", {})
    if contract.get("provider", "launcher") != "launcher":
        raise RuntimeError("O tema não oferece o provedor de Opções/saída supervisionado.")
    bindings = inputs.parse_controls(root / "controls.conf")
    # The launcher projects each observed SDL mapping before starting the engine.
    # Raw button 7 can be L3 on a mapped controller.
    tokens = [OPTIONS_KEY]
    if inputs.resolve_actions(bindings, tokens):
        raise RuntimeError("Start/F10 conflitam com bindings do RetroFE.")
    if inputs.resolve_actions(bindings, ["Q"]) != ["quit"]:
        raise RuntimeError("A saída normal por Q não está disponível de forma exclusiva.")
    if any(token.startswith("joy") and "quit" in actions for token, actions in bindings.items()):
        raise RuntimeError("Saída imediata em botão SDL não é segura; use confirmação em Opções.")
    for name in ("accent", "surface", "panel", "selection"):
        value = contract.get(name)
        if value is not None and (len(value) != 7 or value[0] != "#" or any(c not in "0123456789abcdefABCDEF" for c in value[1:])):
            raise RuntimeError(f"Cor de navegação inválida: {name}")
    return theme, contract


def request_clean_exit(app_pid):
    target = windows.find_window(pid=app_pid, class_hint="RetroFE")
    if not target or windows.active_window_id() != target.window_id:
        raise RuntimeError("RetroFE não está em foco; saída por teclado recusada.")
    try:
        transition.send_clean_quit(app_pid)
        return "uinput-Q"
    except Exception:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "q"], capture_output=True, timeout=3, check=True)
        return "focused-X11-Q"


class OptionsPanel:
    def __init__(self, root, app_pid):
        self.root_path = root
        self.app_pid = app_pid
        self.theme_info, self.contract = navigation_contract(root)
        self.target = windows.find_window(pid=app_pid, class_hint="RetroFE")
        if not self.target:
            raise RuntimeError("Janela RetroFE não encontrada; painel não será aberto em outro monitor.")
        bindings = inputs.parse_controls(root / "controls.conf")
        collisions = inputs.resolve_actions(bindings, [OPTIONS_KEY])
        if collisions:
            raise RuntimeError(f"Start/F10 ainda têm ações do engine: {collisions}")
        self.window = tk.Tk(className="RetroFEOptions")
        self.window.withdraw()
        self.sdl = inputs.SDLInput()
        # Reserve the observed logical Start as well as the unmapped fallback.
        for index, mapping in self.sdl.mappings.items():
            start = mapping.get("start", "")
            if start.startswith("b") and start[1:].isdigit():
                tokens = [f"joy{index}Button{start[1:]}", f"joyButton{start[1:]}"]
                if inputs.resolve_actions(bindings, tokens):
                    self.sdl.close()
                    self.window.destroy()
                    raise RuntimeError("O Start observado conflita com uma ação do RetroFE.")
        try:
            self.keyboard = inputs.X11KeyboardInput()
        except Exception:
            self.sdl.close()
            self.window.destroy()
            raise
        self.sdl.lib.SDL_JoystickNumButtons.argtypes = [ctypes.c_void_p]
        self.sdl.lib.SDL_JoystickNumButtons.restype = ctypes.c_int
        self.sdl.lib.SDL_JoystickGetButton.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.sdl.lib.SDL_JoystickGetButton.restype = ctypes.c_uint8
        self.sdl.lib.SDL_JoystickGetAxis.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.sdl.lib.SDL_JoystickGetAxis.restype = ctypes.c_int16
        self.sdl.lib.SDL_JoystickNumHats.argtypes = [ctypes.c_void_p]
        self.sdl.lib.SDL_JoystickNumHats.restype = ctypes.c_int
        self.sdl.lib.SDL_JoystickGetHat.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.sdl.lib.SDL_JoystickGetHat.restype = ctypes.c_uint8
        self.xlib = ctypes.CDLL(ctypes.util.find_library("X11"))
        self.xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.xlib.XOpenDisplay.restype = ctypes.c_void_p
        self.xlib.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xdisplay = self.xlib.XOpenDisplay(None)
        self.window.title("RetroFE Options")
        # A managed dialog is essential on KWin/XWayland. override_redirect can
        # appear above SDL while keyboard focus remains in the frontend.
        self.window.overrideredirect(False)
        self.window.wm_attributes("-type", "dialog")
        self.window.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.window, bg="#090e15", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.window.bind("<KeyPress>", self.key)
        self.canvas.bind("<Button-1>", self.click)
        self.opened = False
        self.opened_at = 0.0
        self.screen = "root"
        self.index = 0
        self.rows = []
        self.busy = False
        self.notice = ""
        self.changed_at = 0.0
        self.ignore_until = 0.0
        self.closing = None
        self.results = queue.Queue()
        self.width, self.height = 0, 0
        self.drawn = None
        self.last_window_check = 0.0
        self.front_active = False
        self.routing = controller_backend(self.root_path)
        from mobile_controller import Client
        self.mobile = Client(self.root_path.parents[1])
        self.feedback = feedback.SessionFeedback(self.window, root=self.root_path.parents[1],
            on_assign=self.reassign_player, on_identify=self.identify_controller, on_add=self.mobile.pairing)
        self.player_order = []
        self.player_devices = []
        self.preferences = {"schema": 1, "revision": 0, "slots": None, "models": {}}
        self.last_player_scan = 0.0
        self.window.after(30, self.tick)
        self.log("ready", options_key=OPTIONS_KEY, options_button=OPTIONS_BUTTON)

    def refresh_players(self):
        rows = self.routing.input_inventory()
        try:
            self.preferences = self.routing.load_preferences(self.root_path.parents[1])
        except (ValueError, OSError) as exc:
            self.feedback.notice = str(exc)
        mobile = {r['controller_id']: r for r in self.mobile.clients}
        self.player_devices = [{**row,
            "controller_id": self.routing.device_key(row),
            "display_name": mobile.get(self.routing.device_key(row), {}).get('name', row['name']),
            "online": mobile.get(self.routing.device_key(row), {}).get('online', True),
            "routable": bool(self.routing.source_mapping(row, self.sdl.devices.values())),
            "model_override": self.preferences["models"].get(self.routing.device_key(row))}
            for row in rows if row["kind"] in {"deck", "external"}]
        mapped = [row for row in self.player_devices if row["routable"]]
        output = windows.active_output_name()
        self.player_order = self.routing.slots(mapped, self.routing.dock_mode(output),
                                               self.player_order, self.preferences["slots"],
                                               self.routing.player_capacity(self.root_path.parents[1]))
        available = {row["path"]: row for row in rows}
        self.feedback.set_devices(self.player_devices,
            [self.routing.device_key(available[path]) if path else None for path in self.player_order],
            keyboard=not any(self.player_order))

    def reassign_player(self, source_id, player):
        self.refresh_players()
        self.preferences = self.routing.assign_player(self.root_path.parents[1], self.player_devices,
            self.player_order, source_id, player, self.preferences["revision"])
        self.refresh_players()

    def identify_controller(self, source_id, model):
        self.refresh_players()
        if source_id not in {self.routing.device_key(row) for row in self.player_devices}:
            raise ValueError("O controle foi desconectado.")
        self.preferences = self.routing.identify_controller(self.root_path.parents[1],
            source_id, model, self.preferences["revision"])
        self.refresh_players()

    def log(self, event, **values):
        print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event,
                          "app_pid": self.app_pid, **values}, ensure_ascii=False), flush=True)

    def theme(self):
        registry = json.loads((self.root_path / "Config/themes.json").read_text())
        saved = self.root_path / "UserData/retrofe/theme-profile.json"
        theme_id = json.loads(saved.read_text()).get("theme_id") if saved.exists() else registry["default_theme_id"]
        return registry, theme_id

    def make_rows(self):
        if self.screen == "root":
            return [
                ("Continuar", "Voltar à mesma seleção", "resume", True),
                ("Tema", "Escolher a aparência do frontend", "themes", True),
                ("Tela", "Proporção e resolução", "display", True),
                ("Controles", "Dispositivos P1–P4 e entrada opcional", "controllers", True),
                ("Mídia e metadados", "Faltantes, conversão ES-DE e Skraper", "media", True),
                ("Como navegar", "Botões, letras e favoritos", "guide", True),
                ("Sair do RetroFE", "Exige confirmação", "exit", True),
            ]
        if self.screen == "themes":
            registry, active = self.theme()
            saved = display.read_preference(self.root_path)
            profile = saved["profile_id"] if saved else "16x9-hd"
            return [(item["label"], "Tema atual" if item["id"] == active else
                     ("Reabre em Plataformas" if profile in item["supported_display_profiles"] else "Indisponível nesta proporção"),
                     "theme:" + item["id"], profile in item["supported_display_profiles"] and item["id"] != active)
                    for item in registry["themes"]]
        if self.screen == "display":
            registry, active = self.theme()
            theme = next(t for t in registry["themes"] if t["id"] == active)
            return [(label, "Vale na próxima abertura" if value["id"] in theme["supported_display_profiles"] else "Não suportado pelo tema",
                     "display:" + value["id"], value["id"] in theme["supported_display_profiles"])
                    for label, value in display.PROFILES.items()]
        if self.screen == "media":
            return [("Ver o que falta", "Capas, screenshots, vídeos e metadados por jogo", "media:status", True),
                    ("Converter metadados do ES-DE", "Só preenche o que falta; vale na próxima abertura", "media:metadata", True),
                    ("Baixar mídias com Skraper", "Fecha o RetroFE, abre o Skraper e volta ao terminar", "media:skraper", True),
                    ("Voltar às opções", "", "root", True)]
        if self.screen == "controllers":
            return [("Ver P1–P4", "Mostrar os controles previstos para o próximo jogo", "players", True),
                    ("Steam Input (opcional)", "Abre o Steam para configurar seu perfil Desktop", "controls", True),
                    ("Voltar às opções", "", "root", True)]
        if self.screen == "exit":
            return [("Continuar jogando", "Cancelar e voltar à biblioteca", "resume", True),
                    ("Encerrar o RetroFE", "Voltar ao Desktop", "confirm_exit", True)]
        return [("Voltar às opções", "", "root", True)]

    def show(self, screen="root"):
        self.target = windows.window_by_id(self.target.window_id) or self.target
        self.screen, self.index, self.notice = screen, 0, ""
        self.rows = self.make_rows()
        self.opened = True
        self.opened_at = time.monotonic()
        self.closing = None
        self.ignore_until = time.monotonic() + .25
        geometry = self.target.client
        self.width, self.height = geometry.width, geometry.height
        self.window.geometry(f"{geometry.width}x{geometry.height}{geometry.x:+d}{geometry.y:+d}")
        self.window.deiconify()
        self.window.attributes("-alpha", 0.0)
        self.window.update_idletasks()
        self.window.update()
        dialog_id = windows.find_window_id_by_title("RetroFE Options")
        if not dialog_id:
            raise RuntimeError("O KDE não registrou a janela Options; não é seguro capturar navegação.")
        self.dialog_id = dialog_id
        subprocess.run(["xprop", "-id", dialog_id, "-f", "_MOTIF_WM_HINTS", "32c", "-set",
                        "_MOTIF_WM_HINTS", "2, 0, 0, 0, 0"], capture_output=True, timeout=2, check=True)
        subprocess.run(["xprop", "-id", dialog_id, "-f", "WM_TRANSIENT_FOR", "32x", "-set",
                        "WM_TRANSIENT_FOR", self.target.window_id], capture_output=True, timeout=2, check=True)
        windows.place_window(dialog_id, geometry, activate=True)
        subprocess.run(["wmctrl", "-ir", dialog_id, "-b", "add,skip_taskbar,skip_pager"],
                       capture_output=True, timeout=2, check=False)
        subprocess.run(["wmctrl", "-ia", dialog_id], capture_output=True, timeout=2, check=True)
        self.window.lift()
        self.window.focus_force()
        self.opened_at = time.monotonic()
        self.changed_at = time.monotonic()
        self.drawn = None
        self.draw()
        self.log("open", screen=screen, geometry=geometry.as_dict())

    def release_buttons(self):
        self.sdl.lib.SDL_JoystickUpdate()
        keymap = (ctypes.c_ubyte * 32)()
        if self.xdisplay:
            self.xlib.XQueryKeymap(self.xdisplay, keymap)
            if any(keymap):
                return False
        for d in self.sdl.devices.values():
            handle = d["handle"]
            if any(self.sdl.lib.SDL_JoystickGetButton(handle, b)
                   for b in range(self.sdl.lib.SDL_JoystickNumButtons(handle))):
                return False
            if any(self.sdl.lib.SDL_JoystickGetHat(handle, h)
                   for h in range(self.sdl.lib.SDL_JoystickNumHats(handle))):
                return False
            if any(abs(self.sdl.lib.SDL_JoystickGetAxis(handle, axis)) > 12000
                   for axis, name in d["axes"].items() if name in {"leftx", "lefty", "rightx", "righty"}):
                return False
        return True

    def close(self, action=None):
        if self.busy:
            return
        self.closing = (time.monotonic(), action)
        self.notice = "Solte os controles para voltar"
        self.drawn = None

    def finish_close(self, action):
        self.window.withdraw()
        self.opened = False
        self.closing = None
        self.ignore_until = time.monotonic() + .4
        # Do not move or resize the preserved frontend, only restore its focus.
        subprocess.run(["wmctrl", "-ia", self.target.window_id], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=2, check=False)
        self.log("close", action=action or "resume")
        if action == "confirm_exit":
            self.window.after(120, self.request_exit)
        elif action:
            self.execute(action, after_close=True)

    def request_exit(self):
        if windows.active_window_id() != self.target.window_id:
            self.show("exit")
            self.notice = "A janela perdeu foco. Confirme novamente para encerrar."
            return
        try:
            self.log("quit-request", transport=request_clean_exit(self.app_pid))
        except Exception as exc:
            self.show("exit")
            self.notice = f"Não consegui encerrar: {exc}"
            self.drawn = None

    def key(self, event):
        if not self.opened or self.busy or self.closing or time.monotonic() < self.ignore_until:
            return "break"
        key = event.keysym
        if key in {"Up", "Left"}:
            self.move(-1)
        elif key in {"Down", "Right"}:
            self.move(1)
        elif key in {"Return", "KP_Enter"}:
            self.confirm()
        elif key in {"Escape", OPTIONS_KEY}:
            self.back()
        return "break"

    def click(self, event):
        if not self.opened or self.busy or self.closing:
            return
        for i, bounds in enumerate(getattr(self, "row_bounds", [])):
            if bounds[0] <= event.x <= bounds[2] and bounds[1] <= event.y <= bounds[3]:
                self.index = i
                self.confirm()
                return

    def event(self, source, tokens, _actions):
        now = time.monotonic()
        if now < self.ignore_until or self.busy or self.closing:
            return
        if not self.opened:
            start = "gamepad:start" in tokens or ("gamepad:mapped" not in tokens and OPTIONS_BUTTON in tokens)
            if self.front_active and (OPTIONS_KEY in tokens or start):
                self.show()
            return
        if not source.startswith("SDL"):
            return  # Focused Tk handles keyboard once; XInput2 is the global opener.
        self.ignore_until = now + .13
        mapped = "gamepad:mapped" in tokens
        if "gamepad:start" in tokens or "gamepad:b" in tokens or (not mapped and (OPTIONS_BUTTON in tokens or "joyButton1" in tokens)):
            self.back()
        elif "gamepad:a" in tokens or (not mapped and "joyButton0" in tokens):
            self.confirm()
        elif any(t in {"gamepad:dpup", "gamepad:dpleft", "gamepad:lefty-", "gamepad:leftx-"} or
                 (not mapped and t.endswith(("Hat0Up", "Hat0Left", "Axis1-", "Axis0-"))) for t in tokens):
            self.move(-1)
        elif any(t in {"gamepad:dpdown", "gamepad:dpright", "gamepad:lefty+", "gamepad:leftx+"} or
                 (not mapped and t.endswith(("Hat0Down", "Hat0Right", "Axis1+", "Axis0+"))) for t in tokens):
            self.move(1)

    def move(self, direction):
        self.index = (self.index + direction) % len(self.rows)
        self.changed_at = time.monotonic()
        self.drawn = None

    def back(self):
        if self.screen in {"root", "exit"}:
            self.close()
        else:
            self.show()

    def confirm(self):
        label, detail, action, enabled = self.rows[self.index]
        if not enabled:
            self.notice = detail
            self.drawn = None
            return
        self.log("select", screen=self.screen, label=label, action=action)
        if action == "players":
            self.feedback.show_players()
            self.close()
        elif action in {"themes", "display", "guide", "exit", "root", "media", "controllers"}:
            self.show(action)
        elif action == "resume":
            self.close()
        elif action == "confirm_exit":
            self.close(action)
        elif action.startswith("theme:") or action in {"controls", "media:skraper"}:
            self.close(action)
        else:
            self.execute(action)

    def execute(self, action, after_close=False):
        if action == "controls":
            command = [sys.executable, str(SCRIPT_DIR / "steam-input.py"), "--root", str(self.root_path), "open-desktop"]
        elif action.startswith("media:"):
            task = action.split(":", 1)[1]
            tool = [sys.executable, str(SCRIPT_DIR / "media-tasks.py")]
            command = {"status": [*tool, "status", "--summary"], "metadata": [*tool, "metadata"],
                       "skraper": [*tool, "request", "skraper"]}[task]
        elif action.startswith("theme:"):
            command = [sys.executable, str(SCRIPT_DIR / "theme-profile.py"), "--root", str(self.root_path), "request", action.split(":", 1)[1]]
        else:
            command = [sys.executable, str(SCRIPT_DIR / "display-profile.py"), "--root", str(self.root_path), "set", action.split(":", 1)[1]]
        self.busy = True
        self.notice = "Aplicando…"
        self.drawn = None

        def run():
            try:
                result = subprocess.run(command, text=True, capture_output=True, timeout=60,
                                        env={**os.environ, "RETROFE_SILENT_OPTIONS": "1"})
                self.results.put((result.returncode, (result.stdout + result.stderr).strip(), after_close, action))
            except Exception as exc:
                self.results.put((1, str(exc), after_close, action))
        threading.Thread(target=run, daemon=True).start()

    def draw(self):
        key = (self.screen, self.index, self.notice, self.width, self.height)
        if key == self.drawn:
            return
        self.drawn = key
        c = self.canvas
        c.delete("all")
        w, h = self.width, self.height
        scale = max(.55, min(w / 1280, h / 800, 1.7))
        margin = round(60 * scale)
        title = {"root": "OPÇÕES", "themes": "TEMA", "display": "TELA", "guide": "COMO NAVEGAR", "exit": "ENCERRAR SESSÃO", "media": "MÍDIA E METADADOS", "controllers": "CONTROLES"}[self.screen]
        c.configure(bg=self.contract.get("surface", "#090e15"))
        accent = self.contract.get("accent", "#75c8f7")
        c.create_rectangle(margin, margin, w-margin, h-margin, fill=self.contract.get("panel", "#0e1924"), outline="#284557", width=1)
        c.create_rectangle(margin, margin, margin+4*scale, h-margin, fill=accent, outline="")
        x, top = margin+38*scale, margin+34*scale
        c.create_text(x, top, text=f"{self.theme_info['label'].upper()}  /  RETROFE", fill=accent, anchor="nw", font=("Noto Sans", round(11*scale)))
        c.create_text(x, top+28*scale, text=title, fill="#edf4f8", anchor="nw", font=("Noto Sans", round(28*scale), "bold"))
        c.create_text(w-margin-34*scale, top+8*scale, text="START  /  F10", fill="#92acba", anchor="ne", font=("Noto Sans", round(11*scale)))
        row_height = min(67*scale, (h-2*margin-170*scale)/max(len(self.rows), 1))
        y = top + 103*scale
        list_width = (w-2*margin-80*scale) if self.screen != "guide" else (w*.34)
        self.row_bounds = []
        for i, (label, detail, action, enabled) in enumerate(self.rows):
            selected = i == self.index
            bounds = (x-12*scale, y-6*scale, x+list_width, y+row_height-9*scale)
            self.row_bounds.append(bounds)
            if selected:
                c.create_rectangle(*bounds, fill=self.contract.get("selection", "#193c51"), outline=accent, width=1)
                c.create_rectangle(bounds[0], bounds[1], bounds[0]+3*scale, bounds[3], fill="#87d9fc", outline="")
            c.create_text(x+8*scale, y, text=label, fill="#f3f8fa" if enabled else "#657885", anchor="nw", font=("Noto Sans", round(16*scale), "bold" if selected else "normal"))
            if detail:
                c.create_text(x+8*scale, y+27*scale, text=detail, fill="#9db7c5" if enabled else "#657885", anchor="nw", font=("Noto Sans", round(10*scale)))
            y += row_height
        if self.screen == "guide":
            c.create_text(w*.48, top+104*scale, text="A  Confirmar / jogar\nB  Voltar\nSTART  Opções\nLB / RB  Letra anterior / seguinte\nX  Marcar ou retirar favorito\nSELECT  Ver favoritos\nL3 / R3  Trocar página\nY  Focar próxima coleção; A abre\nSair  Dentro de Opções, com confirmação", fill="#d2e0e8", anchor="nw", font=("Noto Sans", round(14*scale)), width=w*.4, justify="left")
        footer = "A / ENTER  Confirmar     B / ESC  Voltar"
        if self.notice:
            footer = self.notice
        c.create_text(x, h-margin-30*scale, text=footer, fill="#8ed2f3", anchor="sw", font=("Noto Sans", round(11*scale)), width=w-2*margin-70*scale)

    def tick(self):
        if not transition.process_alive(self.app_pid):
            self.shutdown()
            return
        now = time.monotonic()
        if now-self.last_window_check > .15:
            self.last_window_check = now
            active = windows.active_window_id()
            self.front_active = active == self.target.window_id
            if self.opened and active not in {self.target.window_id, getattr(self, "dialog_id", None)}:
                # Alt-Tab dismisses the panel without stealing focus back.
                self.window.withdraw()
                self.opened = False
                self.closing = None
                self.log("close", action="focus-left-session")
            target = windows.window_by_id(self.target.window_id)
            if target and target.client != self.target.client:
                self.target = target
                if self.opened:
                    rect = target.client
                    self.width, self.height = rect.width, rect.height
                    self.window.geometry(f"{rect.width}x{rect.height}{rect.x:+d}{rect.y:+d}")
                    self.drawn = None
        try:
            self.keyboard.poll(self.event if (self.opened or self.front_active) else None, {})
        except Exception as exc:
            self.log("input-failed", reason=str(exc))
            self.window.quit()
            return
        self.sdl.poll(self.event if (self.opened or self.front_active) else None, {})
        players = {self.routing.device_key(row): n for n, path in enumerate(self.player_order)
                   for row in self.player_devices if row['path'] == path}
        self.mobile.sync(self.opened or self.front_active, players, self.reassign_player)
        if now - self.last_player_scan > 1:
            self.last_player_scan = now
            self.refresh_players()
        rect = self.target.client
        self.feedback.update(self.front_active and not self.opened,
                             (rect.x, rect.y, rect.width, rect.height), now)
        if self.closing and now-self.closing[0] > .2 and self.release_buttons():
            self.finish_close(self.closing[1])
        try:
            while True:
                code, message, after_close, action = self.results.get_nowait()
                self.busy = False
                self.log("result", code=code, message=message)
                if after_close and code:
                    self.show()
                self.notice = message if code else ("Perfil salvo para a próxima abertura" if self.screen == "display" else message)
                self.drawn = None
                if action == "media:skraper" and not code:
                    # Queued for the supervisor; the normal Q exit hands the media lock to Skraper.
                    self.window.after(120, self.request_exit)
        except queue.Empty:
            pass
        if self.opened:
            self.window.attributes("-alpha", min(1.0, (now-self.opened_at)/.14))
            self.draw()
        self.window.after(20, self.tick)

    def shutdown(self):
        self.window.quit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app-pid", type=int)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-contract", action="store_true")
    parser.add_argument("--prepare-controls", action="store_true")
    parser.add_argument("--theme", help="valida o contrato do tema de destino")
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--quit", action="store_true", help="saída normal por Q, somente na janela em foco")
    args = parser.parse_args()
    if args.prepare_controls:
        sys.path.insert(0, str(args.root.resolve().parents[1] / "tools/roms"))
        import emulator_controls
        print(json.dumps(emulator_controls.prepare_retrofe(args.root.resolve().parents[1])))
        return
    if args.check or args.check_contract:
        if args.check and (not windows.backend_available() or not all(shutil.which(name) for name in ("xinput", "xmodmap"))):
            raise RuntimeError("Options requer backend X11/XWayland, wmctrl, xdotool, xprop, xinput e xmodmap.")
        theme, _ = navigation_contract(args.root, args.theme)
        if args.check:
            controller_backend(args.root)
        print(f"[OPÇÕES] {theme['label']}: Start/F10 e saída confirmada disponíveis.")
        return
    if not args.app_pid:
        parser.error("--app-pid é necessário para acompanhar o frontend")
    if args.quit:
        target = windows.find_window(pid=args.app_pid, class_hint="RetroFE")
        if target:
            subprocess.run(["wmctrl", "-ia", target.window_id], capture_output=True, timeout=2, check=True)
            time.sleep(.15)
        print(request_clean_exit(args.app_pid))
        return
    panel = OptionsPanel(args.root.resolve(), args.app_pid)
    signal.signal(signal.SIGTERM, lambda *_: panel.window.quit())
    signal.signal(signal.SIGINT, lambda *_: panel.window.quit())
    try:
        if args.ready_file:
            transition.write_json(args.ready_file, {"pid": os.getpid(), "app_pid": args.app_pid,
                                                  "theme_id": panel.theme_info["id"]})
        panel.window.mainloop()
    finally:
        if args.ready_file:
            args.ready_file.unlink(missing_ok=True)
        panel.keyboard.close()
        panel.mobile.close()
        panel.sdl.close()
        panel.feedback.close()
        panel.window.destroy()
        if panel.xdisplay:
            panel.xlib.XCloseDisplay(panel.xdisplay)


if __name__ == "__main__":
    main()
