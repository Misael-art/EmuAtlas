#!/usr/bin/env python3
"""Read-only, session-owned player notifications and exit hints on KDE/X11."""
from __future__ import annotations

import ctypes
import ctypes.util
import time
import tkinter as tk
from tkinter import font as tkfont

TOAST_SECONDS = 5.0
KEYBOARD_EXIT_HINT = "Teclado  Ctrl + Shift + F10 · segure 2 s"


def player_rows(names, keyboard=True):
    rows = [(f"P{n + 1}", names[n] if n < len(names) else "Sem controle") for n in range(4)]
    if not names and keyboard:
        rows[0] = ("P1", "Teclado")
    return rows


def exit_hint(game):
    if game:
        return "SELECT / BACK + START", "Segure 2 s para sair do jogo"
    return "START  /  F10", "Opções e sair do RetroFE"


class SessionFeedback:
    """Two small click-through windows, updated by the existing session loop.

    No input observer, background thread or persistent process is added. The
    owner supplies actual routing, geometry and focus. Mapping never activates
    either window, and leaving the owned application hides both immediately.
    """
    def __init__(self, master=None, *, game=False, accent="#94afbe"):
        self.owns_master = master is None
        self.master = master or tk.Tk(className="EmulationFeedback")
        if self.owns_master:
            self.master.withdraw()
        self.game, self.accent = game, accent
        self.names = []
        self.keyboard = True
        self.signature = None
        self.pending = True
        self.toast_started = None
        self.focused = False
        self.drawn = None
        self.toast, self.toast_canvas = self.card("Emulation Players")
        self.hint, self.hint_canvas = self.card("Emulation Exit Hint")
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
            raise RuntimeError("A camada de avisos requer DISPLAY X11/XWayland.")
        self.master.update_idletasks()
        for window in (self.toast, self.hint):
            self.click_through(window)

    def card(self, title):
        window = tk.Toplevel(self.master, takefocus=False)
        window.withdraw()
        window.title(title)
        window.overrideredirect(True)
        window.wm_attributes("-type", "notification")
        window.attributes("-topmost", True)
        window.attributes("-alpha", 0)
        canvas = tk.Canvas(window, bg="#101b24", highlightthickness=0, takefocus=False)
        canvas.pack(fill="both", expand=True)
        return window, canvas

    def click_through(self, window):
        # Tk's visible wrapper and its child must both have an empty input
        # region. Mouse/touch remains with the game under the status cards.
        child = window.winfo_id()
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children, count = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        self.xlib.XQueryTree(self.connection, child, ctypes.byref(root), ctypes.byref(parent),
                            ctypes.byref(children), ctypes.byref(count))
        if children:
            self.xlib.XFree(children)
        for xid in {child, parent.value} - {0}:
            self.shape.XShapeCombineRectangles(self.connection, xid, 2, 0, 0, None, 0, 0, 3)
        self.xlib.XFlush(self.connection)

    def set_players(self, names, *, keyboard=True, identity=None):
        signature = (tuple(names), keyboard, identity)
        if signature != self.signature:
            self.signature = signature
            self.names, self.keyboard = list(names), keyboard
            self.pending = True
            self.drawn = None

    def show_players(self):
        self.pending = True

    def hide(self):
        self.toast.withdraw()
        self.hint.withdraw()

    @staticmethod
    def fitted(text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def draw(self, geometry):
        x, y, width, height = geometry
        scale = max(.65, min(width / 1280, height / 800, 1.5))
        # Safe inset adapts to portrait and ultrawide without covering the centre.
        margin = round(min(width, height) * .035)
        cw = min(width - margin * 2, round(440 * scale))
        th, hh = round(244 * scale), round((84 if self.game else 66) * scale)
        self.toast.geometry(f"{cw}x{th}{x + width - margin - cw:+d}{y + margin:+d}")
        self.hint.geometry(f"{cw}x{hh}{x + width - margin - cw:+d}{y + height - margin - hh:+d}")
        font = tkfont.Font(self.master, family="Noto Sans", size=max(9, round(12 * scale)))
        small = ("Noto Sans", max(8, round(9 * scale)))
        bold = ("Noto Sans", max(10, round(12 * scale)), "bold")
        for c in (self.toast_canvas, self.hint_canvas):
            c.delete("all")
        c = self.toast_canvas
        c.create_rectangle(0, 0, cw - 1, th - 1, outline="#334653", width=1)
        c.create_rectangle(0, 0, 3 * scale, th, fill=self.accent, outline="")
        left = round(22 * scale)
        c.create_text(left, 22 * scale, anchor="nw", text="CONTROLES" if self.game else "CONTROLES DO PRÓXIMO JOGO",
                      fill="#b4c7d2", font=small)
        for n, (player, name) in enumerate(player_rows(self.names, self.keyboard)):
            top = (55 + 43 * n) * scale
            assigned = name != "Sem controle"
            c.create_rectangle(left, top, left + 39 * scale, top + 27 * scale,
                               fill="#253d4b" if assigned else "#1a2832", outline="")
            c.create_text(left + 20 * scale, top + 14 * scale, text=player,
                          fill="#e5f1f7" if assigned else "#879ba9", font=bold)
            c.create_oval(left + 53 * scale, top + 10 * scale, left + 59 * scale, top + 16 * scale,
                          fill="#8bd5b2" if assigned else "#536571", outline="")
            tx = left + 72 * scale
            c.create_text(tx, top + 13 * scale, anchor="w", font=font,
                          text=self.fitted(name, font, cw - tx - left), fill="#f0f5f8" if assigned else "#91a4b2")
        c = self.hint_canvas
        key, action = exit_hint(self.game)
        c.create_rectangle(0, 0, cw - 1, hh - 1, outline="#334653", width=1)
        c.create_text(left, 12 * scale, anchor="nw", text=key, fill="#d9e9f2", font=bold)
        c.create_text(left, 36 * scale, anchor="nw", text=action, fill="#b4c7d2", font=small)
        if self.game:
            c.create_text(left, 56 * scale, anchor="nw", text=KEYBOARD_EXIT_HINT, fill="#91a4b2", font=small)

    def update(self, focused, geometry, now=None):
        now = time.monotonic() if now is None else now
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
            self.hint.deiconify()
            self.hint.attributes("-alpha", .9)
            age = now - self.toast_started
            if age < TOAST_SECONDS:
                self.toast.deiconify()
                self.toast.attributes("-alpha", .97 * max(0, min(1, age / .14, (TOAST_SECONDS - age) / .25)))
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
