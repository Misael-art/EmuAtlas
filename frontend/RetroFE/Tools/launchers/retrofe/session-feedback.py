#!/usr/bin/env python3
"""Session-owned controller artwork, player assignment and exit guidance."""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import io
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

TOAST_SECONDS = 5.0
KEYBOARD_EXIT_HINT = "Teclado  Ctrl + Shift + F10 · segure 2 s"
SURFACE = "#101116"
CARD = "#191b21"
GOLD = "#cfb68c"
TEXT = "#f5f0e7"
SECONDARY = "#b6b3b0"
MUTED = "#85858d"


def player_rows(names, keyboard=True, capacity=8):
    rows = [(f"P{n + 1}", names[n] if n < len(names) and names[n] else "Sem controle") for n in range(capacity)]
    if not any(names) and keyboard:
        rows[0] = ("P1", "Teclado")
    return rows


def exit_hint(game):
    if game:
        return "SELECT / BACK + START", "Segure 2 s para sair do jogo"
    return "START  /  F10", "Opções e sair do RetroFE"


def controller_model(row, models):
    override = row.get("model_override")
    if override in models:
        return override
    if row.get('mobile') and 'mobile' in models:
        return 'mobile'
    name = row.get("name", "").casefold()
    for key, model in models.items():
        if any(re.search(pattern, name) for pattern in model.get("patterns", [])):
            return key
    if row.get("kind") == "deck" and "steam-deck" in models:
        return "steam-deck"
    pair = [row.get("vendor"), row.get("product")]
    for key, model in models.items():
        if pair in model.get("usb_ids", []):
            return key
    return "default" if "default" in models else "generic"


def rounded(canvas, bounds, radius, **options):
    x1, y1, x2, y2 = bounds
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    points = (x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r,
              x2, y2, x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1)
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class XRectangle(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


class SessionFeedback:
    """Pointer interaction stays inside the visible controller card/chip.

    The existing session loop supplies routing and focus. Changes are committed
    through its callbacks, and the UI displays the acknowledged assignment.
    """
    def __init__(self, master=None, *, game=False, accent=GOLD, root=None,
                 on_assign=None, on_identify=None, on_add=None):
        self.owns_master = master is None
        self.master = master or tk.Tk(className="EmulationFeedback")
        if self.owns_master:
            self.master.withdraw()
        self.frontend = Path(root) / "frontend/RetroFE" if root else Path(__file__).resolve().parents[3]
        registry = self.frontend / "Config/controller-artwork.json"
        config = json.loads(registry.read_text()) if registry.exists() else {}
        self.capacity = config.get('player_capacity', 8)
        self.models = config["models"] if registry.exists() else {
            "generic": {"label": "Controle genérico", "image": {}}}
        self.game = game
        self.accent = accent
        self.on_assign, self.on_identify = on_assign, on_identify
        self.on_add = on_add
        self.pairing = None
        self.pairing_busy = False
        self.pairing_cancelled = False
        self.pairing_results = queue.Queue()
        self.qr_image = None
        self.address_index = 0
        self.devices, self.ports, self.names = {}, [None] * self.capacity, []
        self.keyboard = True
        self.signature = None
        self.pending = True
        self.toast_started = None
        self.focused = False
        self.drawn = None
        self.notice = ""
        self.selected = self.drag = self.press = self.picker = self.animation = None
        self.hover_player = None
        self.highlight = None
        self.highlight_until = 0
        self.images = {}
        self.atlas = None
        self.toast, self.toast_canvas = self.card("Emulation Players")
        self.hint, self.hint_canvas = self.card("Emulation Exit Hint")
        self.body = tk.Canvas(self.toast, bg=SURFACE, highlightthickness=0, takefocus=False)
        self.body.configure(yscrollcommand=self.scroll_indicator)
        self.body.bind("<ButtonPress-1>", self.pointer_down)
        self.body.bind("<B1-Motion>", self.pointer_move)
        self.body.bind("<ButtonRelease-1>", self.pointer_up)
        self.body.bind("<MouseWheel>", lambda e: self.body.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.body.bind("<Button-4>", lambda e: self.body.yview_scroll(-1, "units"))
        self.body.bind("<Button-5>", lambda e: self.body.yview_scroll(1, "units"))
        self.toast_canvas.bind("<Button-1>", self.header_click)
        self.hint_canvas.bind("<Button-1>", lambda _e: self.show_players())
        self.xlib = ctypes.CDLL(ctypes.util.find_library("X11"))
        self.shape = ctypes.CDLL(ctypes.util.find_library("Xext"))
        self.xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.xlib.XOpenDisplay.restype = ctypes.c_void_p
        self.xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xlib.XFlush.argtypes = [ctypes.c_void_p]
        self.xlib.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)), ctypes.POINTER(ctypes.c_uint)]
        self.xlib.XFree.argtypes = [ctypes.c_void_p]
        self.shape.XShapeCombineRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.connection = self.xlib.XOpenDisplay(None)
        if not self.connection:
            self.close()
            raise RuntimeError("Os avisos exigem DISPLAY X11/XWayland.")
        self.master.update_idletasks()

    def card(self, title):
        window = tk.Toplevel(self.master, takefocus=False)
        window.withdraw()
        window.title(title)
        window.overrideredirect(True)
        window.wm_attributes("-type", "notification")
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0)
        canvas = tk.Canvas(window, bg=SURFACE, highlightthickness=0, takefocus=False)
        canvas.pack(fill="both", expand=True)
        return window, canvas

    def shape_window(self, window, width, height, input_rectangles, scale=None):
        child = window.winfo_id()
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children, count = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        self.xlib.XQueryTree(self.connection, child, ctypes.byref(root), ctypes.byref(parent),
                            ctypes.byref(children), ctypes.byref(count))
        if children:
            self.xlib.XFree(children)
        radius = max(8, round(16 * (scale if scale is not None else getattr(self, "scale", 1))))
        outline = [(0, radius, width, height - 2 * radius)]
        for y in range(radius):
            inset = round(radius - (radius ** 2 - (radius - y - .5) ** 2) ** .5)
            outline.extend([(inset, y, width - 2 * inset, 1),
                            (inset, height - y - 1, width - 2 * inset, 1)])
        for xid in {child, parent.value} - {0}:
            for kind, regions in ((0, outline), (2, input_rectangles)):
                rectangles = (XRectangle * len(regions))(*(XRectangle(*r) for r in regions))
                self.shape.XShapeCombineRectangles(self.connection, xid, kind, 0, 0,
                                                  rectangles, len(regions), 0, 0)
        self.xlib.XFlush(self.connection)

    def set_players(self, names, *, keyboard=True, identity=None):
        devices = [{"controller_id": f"legacy-{n}-{identity}", "path": str(n), "name": name}
                   for n, name in enumerate(names) if name]
        self.set_devices(devices, [row["controller_id"] for row in devices], keyboard=keyboard)

    def set_devices(self, rows, ports, *, keyboard=True):
        signature = (json.dumps(rows, sort_keys=True), tuple(ports), keyboard)
        if signature == self.signature:
            return
        self.signature = signature
        self.devices = {row["controller_id"]: row for row in rows}
        capacity = getattr(self, 'capacity', 8)
        self.ports = (list(ports) + [None] * capacity)[:capacity]
        self.names = [self.devices[key]["name"] if key in self.devices else None for key in self.ports]
        self.keyboard = keyboard
        if getattr(self, "drag", None) and self.drag["id"] not in self.devices:
            self.drag = self.press = None
            self.notice = "Controle desconectado"
        if getattr(self, "selected", None) not in self.devices:
            self.selected = None
        if getattr(self, "picker", None) not in self.devices:
            self.picker = None
        self.pending = True
        self.drawn = None

    def show_players(self):
        self.pairing = None
        self.pending = True
        self.drawn = None

    def hide(self):
        self.toast.withdraw()
        self.hint.withdraw()
        self.drag = self.press = self.selected = self.picker = None

    @staticmethod
    def fitted(text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def icon(self, model_id, width, height):
        cache_key = (model_id, width, height)
        if cache_key in self.images:
            return self.images[cache_key]
        from PIL import Image, ImageTk
        image = self.models[model_id].get("image", {})
        path = (self.frontend / image.get("path", "")).resolve()
        if not path.is_file() or not path.is_relative_to((self.frontend / "Assets/shared").resolve()):
            return None
        if "grid" in image:
            if self.atlas is None or self.atlas[0] != path:
                with Image.open(path) as source:
                    self.atlas = path, source.convert("RGBA")
            source = self.atlas[1]
            rows, columns = image["grid"]
            row, column = image["cell"]
            iw, ih = source.size
            rendered = source.crop((column*iw//columns, row*ih//rows,
                                    (column+1)*iw//columns, (row+1)*ih//rows))
            if "region" in image:
                rendered = rendered.crop(image["region"])
        else:
            with Image.open(path) as source:
                rendered = source.convert("RGBA")
        bounds = rendered.getchannel("A").getbbox()
        if bounds:
            rendered = rendered.crop(bounds)
        rendered.thumbnail((width, height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(rendered, master=self.master)
        self.images[cache_key] = photo
        return photo

    def draw_icon(self, canvas, model_id, x, y, width, height, **options):
        icon = self.icon(model_id, round(width), round(height))
        if icon:
            canvas.create_image(x, y, image=icon, **options)
        else:
            canvas.create_text(x, y, text="◈", fill=SECONDARY,
                               font=("Noto Sans", max(14, round(24 * self.scale))))

    def draw(self, geometry):
        x, y, width, height = geometry
        self.scale = s = max(.65, min(width / 1280, height / 800, 1.4))
        margin = round(min(width, height) * .035)
        self.card_width = cw = min(width - margin * 2, round(480 * s))
        self.row_height = round(80 * s)
        self.name_font = tkfont.Font(self.master, family="Noto Sans", size=-max(12, round(16*s)), weight="bold")
        self.small_font = tkfont.Font(self.master, family="Noto Sans", size=-max(10, round(12*s)))
        self.brand_font = tkfont.Font(self.master, family="Noto Sans", size=-max(9, round(10*s)))
        self.title_font = tkfont.Font(self.master, family="Noto Sans", size=-round(28*s), weight="bold")
        title_y = round(38*s)
        instruction_y = title_y + self.title_font.metrics("linespace") + round(8*s)
        extras = [key for key in self.devices if key not in self.ports]
        row_count = len(self.ports) + len(extras)
        pairing = getattr(self, 'pairing', None)
        self.header_height = instruction_y + self.small_font.metrics("linespace") + round(18*s)
        self.add_bounds = None
        if not self.picker and not pairing:
            top = self.header_height
            self.add_bounds = (26*s, top, cw-26*s, top+32*s)
            self.header_height += round(44*s)
        footer_height = round(38 * s)
        content_height = round((410 if pairing else 440) * s) if pairing or self.picker else row_count * self.row_height
        body_height = min(content_height, round(height * .82) - self.header_height - footer_height)
        self.toast_height = th = self.header_height + body_height + footer_height
        hh = round((88 if self.game else 70) * s)
        self.toast.geometry(f"{cw}x{th}{x + width - margin - cw:+d}{y + margin:+d}")
        self.hint.geometry(f"{cw}x{hh}{x + width - margin - cw:+d}{y + height - margin - hh:+d}")
        self.body.place(x=round(14*s), y=self.header_height, width=cw-round(28*s), height=body_height)
        self.body_width = cw - round(28*s)
        self.toast_canvas.delete("all")
        c = self.toast_canvas
        rounded(c, (1, 1, cw-1, th-1), 18*s, fill=SURFACE, outline="#504638", width=1)
        c.create_line(28*s, 1, cw-28*s, 1, fill=self.accent, width=1)
        c.create_text(26*s, 20*s, anchor="nw", text="EM JOGO" if self.game else "PRÓXIMO JOGO",
                      fill=GOLD, font=("Noto Sans", -max(9, round(10*s))))
        c.create_text(26*s, title_y, anchor="nw", text="Adicionar controle" if pairing else "Modelo do controle" if self.picker else "Controles",
                      fill=TEXT, font=self.title_font, tags="heading")
        c.create_text(26*s, instruction_y, anchor="nw", text="Escaneie com seu celular ou tablet" if pairing else "Escolha a representação do seu controle" if self.picker else "Arraste um controle para trocar de jogador",
                      fill=SECONDARY, font=self.small_font, tags="instruction")
        c.create_text(cw-29*s, 28*s, text="‹" if self.picker or pairing else "×", fill=SECONDARY,
                      font=("Noto Sans", -round(26*s)))
        if self.add_bounds:
            rounded(c, self.add_bounds, 9*s, fill="#25221e", outline="#61513c", width=1)
            c.create_text(cw/2, self.add_bounds[1]+16*s, text="Preparando conexão…" if self.pairing_busy else "＋  Adicionar celular ou tablet",
                          fill=GOLD, font=self.small_font)
        footer = "QR Code temporário · mesma rede local" if pairing else "Role para ver outros modelos" if self.picker else self.notice or f"P1–P{self.capacity} · role para ver todos os jogadores"
        c.create_text(26*s, th-19*s, anchor="w", text=self.fitted(footer, self.small_font, cw-52*s),
                      fill=GOLD if self.notice else MUTED, font=self.small_font)
        self.draw_body()
        c = self.hint_canvas
        c.delete("all")
        rounded(c, (1, 1, cw-1, hh-1), 15*s, fill=SURFACE, outline="#3b3630", width=1)
        key, action = exit_hint(self.game)
        c.create_text(22*s, 13*s, anchor="nw", text=key, fill=TEXT,
                      font=("Noto Sans", -max(11, round(14*s)), "bold"))
        c.create_text(22*s, 37*s, anchor="nw", text=action, fill=SECONDARY, font=self.small_font)
        if self.game:
            c.create_text(22*s, 59*s, anchor="nw", text=KEYBOARD_EXIT_HINT, fill=MUTED, font=self.small_font)
        chip = (cw-round(100*s), round(10*s), cw-round(14*s), round(34*s))
        rounded(c, chip, 8*s, fill="#25221e", outline="#61513c", width=1)
        c.create_text((chip[0]+chip[2])/2, (chip[1]+chip[3])/2, text="Controles", fill=GOLD,
                      font=self.small_font)
        self.master.update_idletasks()
        self.shape_window(self.toast, cw, th, [(0, 0, cw, th)])
        self.shape_window(self.hint, cw, hh, [(chip[0], chip[1], chip[2]-chip[0], chip[3]-chip[1])])

    def scroll_indicator(self, first, last):
        if not hasattr(self, "header_height"):
            return
        self.toast_canvas.delete("scroll-indicator")
        first, last = float(first), float(last)
        if last-first >= .999:
            return
        s = self.scale
        top = self.header_height + 5*s
        height = self.body.winfo_height() - 10*s
        x = self.card_width - 8*s
        rounded(self.toast_canvas, (x, top, x+2*s, top+height), s,
                fill="#353239", outline="", tags="scroll-indicator")
        rounded(self.toast_canvas, (x, top+first*height, x+2*s, top+last*height), s,
                fill=GOLD, outline="", tags="scroll-indicator")

    def draw_body(self):
        c, s, bw = self.body, self.scale, self.body_width
        c.delete("all")
        self.targets = []
        self.model_targets = []
        if getattr(self, 'pairing', None):
            self.draw_pairing()
            return
        if self.picker:
            keys = sorted(self.models, key=lambda key: self.models[key]["label"])
            tw, rh = bw / 3, 110*s
            for n, key in enumerate(keys):
                col, row = n % 3, n // 3
                bounds = (col*tw+3*s, row*rh+3*s, (col+1)*tw-3*s, (row+1)*rh-3*s)
                rounded(c, bounds, 11*s, fill=CARD, outline="#34343b", width=1)
                self.draw_icon(c, key, col*tw+tw/2, row*rh+36*s, tw-18*s, 55*s)
                model = self.models[key]
                brand, label = model.get("brand", ""), model["label"]
                if brand and label.casefold().startswith(brand.casefold()+" "):
                    label = label[len(brand):].strip()
                c.create_text(col*tw+tw/2, row*rh+74*s, text=brand, fill=MUTED, font=self.brand_font)
                c.create_text(col*tw+tw/2, row*rh+94*s, text=self.fitted(label, self.small_font, tw-14*s),
                              fill=TEXT, font=self.small_font)
                self.model_targets.append((bounds, key))
            c.configure(scrollregion=(0, 0, bw, ((len(keys)+2)//3)*rh))
            return
        extras = [key for key in self.devices if key not in self.ports]
        entries = [(n, key) for n, key in enumerate(self.ports)] + [(None, key) for key in extras]
        for n, (player, key) in enumerate(entries):
            top = n*self.row_height
            bounds = (2*s, top+3*s, bw-2*s, top+self.row_height-5*s)
            highlighted = player is not None and (player == self.hover_player or player == self.highlight)
            selected = key is not None and key == self.selected
            rounded(c, bounds, 12*s, fill="#28231d" if highlighted or selected else CARD,
                    outline=GOLD if highlighted or selected else "#303139", width=1)
            c.create_text(27*s, top+34*s, text=f"P{player+1}" if player is not None else "+",
                          fill=GOLD if key else MUTED, font=("Noto Sans", -round(18*s), "bold"))
            model_bounds = None
            if key in self.devices:
                device = self.devices[key]
                model = controller_model(device, self.models)
                moving = (self.drag and self.drag["id"] == key) or (self.animation and self.animation["id"] == key)
                c.create_oval(46*s, top+8*s, 156*s, top+65*s, fill="#25272d", outline="#303239")
                if not moving:
                    self.draw_icon(c, model, 102*s, top+36*s, 108*s, 64*s)
                label = device.get('display_name', device.get("name", "Controle")) if model in {"generic", "default", "mobile"} else self.models[model]["label"]
                c.create_text(164*s, top+24*s, anchor="w", text=self.fitted(label, self.name_font, bw-180*s),
                              fill=TEXT, font=self.name_font)
                bus = "Celular / tablet · rede local" if device.get('mobile') else {3: "USB", 5: "Bluetooth"}.get(device.get("bus"), "Conectado")
                detail = bus if device.get("routable", True) else "Mapeamento pendente"
                if device.get('mobile') and not device.get('online', True):
                    detail = 'Reconectando · comandos liberados'
                c.create_text(164*s, top+48*s, anchor="w", text=detail, fill=MUTED, font=self.small_font)
                model_bounds = (bw-75*s, top+37*s, bw-12*s, top+61*s)
                rounded(c, model_bounds, 7*s, fill="#25231f", outline="#504638", width=1)
                c.create_text(bw-43*s, top+49*s, text="Modelo", fill=GOLD, font=self.small_font)
            else:
                keyboard = player == 0 and not any(self.ports) and self.keyboard
                c.create_text(83*s, top+35*s, text="⌨" if keyboard else "+", fill=MUTED,
                              font=("Noto Sans", -round(25*s)))
                c.create_text(133*s, top+25*s, anchor="w", text="Teclado" if keyboard else "Livre",
                              fill=SECONDARY, font=self.name_font)
                c.create_text(133*s, top+48*s, anchor="w", text="Solte um controle aqui" if self.drag else "Aguardando controle",
                              fill=MUTED, font=self.small_font)
            self.targets.append({"bounds": bounds, "player": player, "id": key, "model_bounds": model_bounds})
        c.configure(scrollregion=(0, 0, bw, len(entries)*self.row_height))
        ghost = self.drag or self.animation
        if ghost and ghost["id"] in self.devices:
            model = controller_model(self.devices[ghost["id"]], self.models)
            x, y = ghost["position"]
            rounded(c, (x-63*s, y-42*s, x+63*s, y+42*s), 13*s, fill="#29231b", outline=GOLD, width=2)
            self.draw_icon(c, model, x, y, 106*s, 67*s)

    @staticmethod
    def contains(bounds, x, y):
        return bounds and bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]

    def target_at(self, x, y):
        if x < 0 or x > self.body.winfo_width() or y < 0 or y > self.body.winfo_height():
            return None
        y = self.body.canvasy(y)
        return next((row for row in self.targets if self.contains(row["bounds"], x, y)), None)

    def pointer_down(self, event):
        self.toast_started = time.monotonic()
        if self.picker or getattr(self, 'pairing', None):
            return
        row = self.target_at(event.x, event.y)
        if not row:
            return
        if self.contains(row["model_bounds"], event.x, self.body.canvasy(event.y)):
            self.picker = row["id"]
            self.press = self.selected = self.drag = self.animation = None
            self.ignore_click_until = time.monotonic() + .2
            self.body.yview_moveto(0)
            self.drawn = None
            return
        self.press = {"id": row["id"], "player": row["player"], "start": (event.x, event.y)}
        if row["id"] and not self.devices[row["id"]].get("routable", True):
            self.notice = "Este controle precisa de um mapeamento seguro"
            self.press = None
            self.drawn = None

    def pointer_move(self, event):
        if not self.press or not self.press["id"]:
            return
        key = self.press["id"]
        if key not in self.devices or not self.devices[key].get("routable", True):
            return
        dx, dy = event.x-self.press["start"][0], event.y-self.press["start"][1]
        if self.drag is None and abs(dx)+abs(dy) < 7:
            return
        self.drag = {"id": key, "position": (event.x, self.body.canvasy(event.y))}
        row = self.target_at(event.x, event.y)
        self.hover_player = row["player"] if row else None
        if event.y < 12:
            self.body.yview_scroll(-1, "units")
        elif event.y > self.body.winfo_height()-12:
            self.body.yview_scroll(1, "units")
        self.body.configure(cursor="fleur")
        self.draw_body()

    def apply_drop(self, key, player, position):
        self.selected = self.drag = self.press = None
        self.animation = None
        self.hover_player = None
        if key not in self.devices or not self.devices[key].get("routable", True):
            self.notice = "O controle foi desconectado ou está sem mapeamento"
            self.drawn = None
            return
        try:
            if not self.on_assign:
                raise ValueError("Reorganização indisponível nesta sessão")
            self.on_assign(key, player)
            if self.ports[player] != key:
                raise RuntimeError("O backend ainda não confirmou a nova porta")
            self.notice = f"{self.models[controller_model(self.devices[key], self.models)]['label']} agora é P{player+1}"
            self.highlight, self.highlight_until = player, time.monotonic()+.7
            self.animation = {"id": key, "origin": position, "position": position,
                              "target": (102*self.scale, (player+.45)*self.row_height), "start": time.monotonic()}
        except Exception as exc:
            self.notice = str(exc)
        self.pending = True
        self.drawn = None

    def pointer_up(self, event):
        if getattr(self, 'pairing', None):
            if self.contains(self.address_bounds, event.x, self.body.canvasy(event.y)):
                self.address_index = (self.address_index+1) % len(self.pairing['urls'])
                self.qr_image = None
                self.drawn = None
            return
        if self.picker:
            if time.monotonic() < getattr(self, "ignore_click_until", 0):
                return
            y = self.body.canvasy(event.y)
            key = next((key for bounds, key in self.model_targets if self.contains(bounds, event.x, y)), None)
            if key:
                try:
                    if not self.on_identify:
                        raise ValueError("Identificação indisponível nesta sessão")
                    self.on_identify(self.picker, key)
                    self.notice = "Representação do controle salva"
                    self.picker = None
                    self.body.yview_moveto(0)
                except Exception as exc:
                    self.notice = str(exc)
                self.pending = True
                self.drawn = None
            return
        row = self.target_at(event.x, event.y)
        if self.drag:
            key, position = self.drag["id"], self.drag["position"]
            if row and row["player"] is not None:
                self.apply_drop(key, row["player"], position)
            else:
                self.drag = self.press = None
                self.hover_player = None
                self.notice = "Movimento cancelado"
        elif self.press and row:
            if self.selected == row["id"]:
                self.selected = None
                self.notice = ""
            elif self.selected and row["player"] is not None:
                self.apply_drop(self.selected, row["player"], (event.x, self.body.canvasy(event.y)))
            else:
                self.selected = row["id"]
                self.notice = "Clique no jogador de destino" if self.selected else ""
        self.press = None
        self.body.configure(cursor="")
        self.drawn = None

    def header_click(self, event):
        if self.contains(getattr(self, 'add_bounds', None), event.x, event.y):
            self.add_mobile()
            return
        if event.x > self.card_width-60*self.scale and event.y < 60*self.scale:
            if self.picker or getattr(self, 'pairing', None):
                self.picker = None
                self.pairing = None
                self.qr_image = None
                self.body.yview_moveto(0)
                self.drawn = None
            else:
                if self.pairing_busy:
                    self.pairing_cancelled = True
                self.selected = self.drag = None
                self.toast_started = time.monotonic()-TOAST_SECONDS
                self.toast.withdraw()

    def add_mobile(self):
        if self.pairing_busy:
            return
        self.pairing_busy = True
        self.pairing_cancelled = False
        self.notice = ''
        self.selected = self.drag = self.press = self.picker = None
        self.drawn = None
        def prepare():
            try:
                if not self.on_add:
                    raise RuntimeError('Adicionar controle indisponível nesta sessão.')
                result = self.on_add()
            except Exception as exc:
                result = {'error': str(exc)}
            self.pairing_results.put(result)
        threading.Thread(target=prepare, daemon=True).start()

    def draw_pairing(self):
        from PIL import Image, ImageTk
        from mobile_controller import qr_png
        c, s, bw = self.body, self.scale, self.body_width
        url = self.pairing['urls'][self.address_index]
        if self.qr_image is None:
            with Image.open(io.BytesIO(qr_png(url))) as image:
                modules = image.width // 6
                side = modules * max(1, int(220*s/modules))
                rendered = image.resize((side, side), Image.Resampling.NEAREST)
                self.qr_image = ImageTk.PhotoImage(rendered, master=self.master)
        c.create_image(bw/2, 126*s, image=self.qr_image)
        c.create_text(bw/2, 255*s, text='1  Abra a câmera e escaneie o código', fill=TEXT, font=self.small_font)
        c.create_text(bw/2, 281*s, text='2  Conecte e escolha seu jogador', fill=SECONDARY, font=self.small_font)
        address = url.split('/#')[0]
        self.address_bounds = (16*s, 305*s, bw-16*s, 348*s)
        rounded(c, self.address_bounds, 9*s, fill=CARD, outline='#504638', width=1)
        c.create_text(bw/2, 326*s, text=address, fill=GOLD, font=self.small_font)
        if len(self.pairing['urls']) > 1:
            c.create_text(bw/2, 370*s, text='Clique no endereço para trocar a interface de rede', fill=MUTED, font=self.small_font)
        c.configure(scrollregion=(0, 0, bw, 398*s))

    def update(self, focused, geometry, now=None):
        now = time.monotonic() if now is None else now
        results = getattr(self, 'pairing_results', None)
        if results is not None:
            try:
                result = results.get_nowait()
                self.pairing_busy = False
                if self.pairing_cancelled:
                    self.drawn = None
                elif 'error' in result:
                    self.notice = result['error']
                else:
                    self.pairing = result
                    self.address_index = 0
                    self.qr_image = None
                    self.body.yview_moveto(0)
                if not self.pairing_cancelled:
                    self.pending = True
                self.drawn = None
            except queue.Empty:
                pass
        if getattr(self, 'pairing', None) and now >= self.pairing['expires_at']:
            self.pairing = None
            self.notice = 'QR Code expirou. Clique em Adicionar para gerar outro.'
            self.drawn = None
        if not focused or not geometry or min(geometry[2:]) < 200:
            self.hide()
            self.focused = False
        else:
            if not self.focused:
                self.pending = True
            self.focused = True
            if self.pending:
                self.toast_started, self.pending = now, False
            if geometry != self.drawn:
                self.draw(geometry)
                self.drawn = geometry
            if self.animation:
                progress = min(1, (now-self.animation["start"])/.18)
                eased = 1-(1-progress)**3
                start, end = self.animation["origin"], self.animation["target"]
                self.animation["position"] = tuple(a+(b-a)*eased for a, b in zip(start, end))
                if progress >= 1:
                    self.animation = None
                self.draw_body()
            if self.highlight is not None and now > self.highlight_until:
                self.highlight = None
                self.draw_body()
            pointer = self.toast.winfo_pointerxy()
            hovered = self.toast.winfo_viewable() and self.toast.winfo_rootx() <= pointer[0] <= self.toast.winfo_rootx()+self.toast.winfo_width() and self.toast.winfo_rooty() <= pointer[1] <= self.toast.winfo_rooty()+self.toast.winfo_height()
            if hovered or self.drag or self.selected or self.picker or getattr(self, 'pairing', None) or (getattr(self, 'pairing_busy', False) and not self.pairing_cancelled):
                self.toast_started = now - .3
            self.hint.deiconify()
            self.hint.attributes("-alpha", .95)
            age = now-self.toast_started
            if age < TOAST_SECONDS:
                self.toast.deiconify()
                self.toast.attributes("-alpha", max(0, min(1, age/.14, (TOAST_SECONDS-age)/.25)))
            else:
                self.toast.withdraw()
        if self.owns_master:
            self.master.update_idletasks()
            self.master.update()

    def close(self):
        for window in (self.toast, self.hint):
            window.destroy()
        if getattr(self, "connection", None):
            self.xlib.XCloseDisplay(self.connection)
            self.connection = None
        if self.owns_master:
            self.master.destroy()
