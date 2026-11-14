"""Left-hand save shelf, sharing the existing session's Tk and input lifecycle."""
from __future__ import annotations

import datetime as dt
import time
import tkinter as tk

try:
    from .save_journey import TAGS, safe_path
except ImportError:
    from save_journey import TAGS, safe_path

BG, SURFACE, BORDER = "#101116", "#191b21", "#504638"
GOLD, TEXT, MUTED = "#cfb68c", "#f5f0e7", "#b6b3b0"
OPEN_HINT = "Saves · P1 Select + Y · Ctrl + Shift + J"


def duration(seconds):
    if seconds is None:
        return "Tempo anterior desconhecido"
    minutes = int(seconds) // 60
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def badges(row):
    result = []
    if row["stage"]:
        result.append(f"Fase {row['stage']}")
    result.extend({"boss": "♛ Chefe", "ending": "⚑ Final", "challenge": "◆ Hiperdesafio",
                   "discovery": "✧ Descoberta", "checkpoint": "Marco", "practice": "Treino"}[tag]
                  for tag in row["tags"])
    return result


class SaveShelf:
    def __init__(self, feedback, backend, on_return=None):
        self.feedback, self.backend = feedback, backend
        self.on_return = on_return
        self.window, self.canvas = feedback.card("EmuAtlas Save Journey")
        self.window.bind("<Escape>", lambda _e: self.collapse())
        self.window.bind("<Return>", lambda _e: self.choose())
        self.window.bind("<Up>", lambda _e: self.move(-1))
        self.window.bind("<Down>", lambda _e: self.move(1))
        self.window.bind("<Key-f>", lambda _e: self.favorite())
        self.canvas.bind("<Button-1>", self.click)
        self.canvas.bind("<Motion>", self.motion)
        self.canvas.bind("<Button-4>", lambda _e: self.scroll(-1))
        self.canvas.bind("<Button-5>", lambda _e: self.scroll(1))
        self.canvas.bind("<MouseWheel>", lambda e: self.scroll(-1 if e.delta > 0 else 1))
        self.transition, self.transition_canvas = feedback.card("EmuAtlas Resume")
        self.rows, self.images, self.hits = [], {}, []
        self.offset = self.selected = 0
        self.expanded = False
        self.geometry = None
        self.last_revision, self.last_refresh = -1, 0
        self.last_inside = 0
        self.keys_down, self.pad_down = set(), set()
        self.release_gate = False
        self.editor = None
        self.loading = None
        self.notice = ""
        self.repeat_at = 0
        self.confirm_label, self.back_label, self.favorite_label = "A", "B", "X"
        self.reduce_motion = backend.config.get("emuatlas_reduce_motion", "false") == "true"
        self.scale, self.width, self.height = 1, 1, 1
        self.feedback.save_journey_hint = OPEN_HINT
        self.feedback.drawn = None

    @property
    def captures_input(self):
        return self.expanded or self.loading is not None or self.release_gate or self.editor is not None

    def expand(self, keyboard=False, source="hover"):
        self.notice = ""
        self.expanded = True
        self.expansion_mode = "keyboard" if keyboard else source
        self.backend.request("pause", True)
        self.last_inside = time.monotonic()
        self.draw()
        if keyboard:
            self.window.focus_force()

    def collapse(self):
        if self.loading:
            return
        if self.editor:
            self.editor.destroy()
            self.editor = None
        self.expanded = False
        self.release_gate = True
        self.backend.request("pause", False)
        self.window.withdraw()
        self.draw()
        if self.on_return:
            self.on_return()

    def refresh(self):
        self.rows = self.backend.store.rows(limit=5, offset=self.offset)
        self.count = self.backend.store.count()
        self.selected = min(self.selected, max(0, len(self.rows) - 1))
        self.images.clear()

    def photo(self, row, width, height):
        from PIL import Image, ImageTk
        if not row["preview"]:
            return None
        key = row["sequence"], int(width), int(height)
        if key not in self.images:
            try:
                with Image.open(safe_path(self.backend.store.directory, row["preview"])) as image:
                    image = image.convert("RGB")
                    image.thumbnail((max(1, int(width)), max(1, int(height))), Image.Resampling.LANCZOS)
                    self.images[key] = ImageTk.PhotoImage(image, master=self.feedback.master)
            except (OSError, ValueError):
                self.images[key] = None
        return self.images[key]

    def text(self, canvas, x, y, text, size=12, color=TEXT, bold=False, **options):
        return canvas.create_text(x, y, text=text, fill=color,
            font=("Noto Sans", -max(9, round(size * self.scale)), "bold" if bold else "normal"),
            anchor="nw", **options)

    def rounded(self, canvas, bounds, radius=12, **options):
        x1, y1, x2, y2 = bounds
        r = min(radius * self.scale, (x2-x1)/2, (y2-y1)/2)
        return canvas.create_polygon(x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
            x2, y2-r, x2, y2, x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1,
            smooth=True, splinesteps=24, **options)

    def draw(self):
        if not self.geometry:
            return
        x, y, width, height = self.geometry
        self.scale = s = max(.7, min(width/1280, height/800, 1.4))
        margin = max(8, round(height * .025))
        image_ratio = 4/3
        if self.rows and self.rows[0]["preview"]:
            from PIL import Image
            try:
                with Image.open(safe_path(self.backend.store.directory, self.rows[0]["preview"])) as image:
                    image_ratio = image.width/image.height
            except (OSError, ValueError):
                pass
        gutter = max(0, (width - height * image_ratio)/2)
        compact = gutter >= 80*s
        self.width = w = round(min(width-2*margin, 320*s) if self.expanded else
                               min(158*s, gutter-margin) if compact else 104*s)
        self.row_height = rh = round(114*s)
        header = round(67*s)
        if self.expanded:
            rh = self.row_height = max(round(76*s), min(rh, (height-2*margin-round(120*s))//5))
            h = min(height-2*margin, header + rh*max(1, len(self.rows)) + round(76*s))
        else:
            h = round(164*s if compact and self.rows else 48*s)
        self.height, self.header = h, header
        top = y + margin if self.expanded else y + (height-h)//2
        self.window.geometry(f"{w}x{h}{x+margin:+d}{top:+d}")
        self.canvas.delete("all")
        self.hits = []
        self.rounded(self.canvas, (1, 1, w-1, h-1), fill=BG, outline=BORDER)
        self.canvas.create_line(18*s, 1, w-18*s, 1, fill=GOLD)
        if not self.expanded:
            if self.rows and compact:
                row = self.backend.store.rows(limit=1)[0]
                self.text(self.canvas, 12*s, 10*s, "♥ FAVORITO" if row["favorite"] else "ÚLTIMO SAVE",
                          9, GOLD, True)
                photo = self.photo(row, w-16*s, 82*s)
                if photo:
                    self.canvas.create_image(w/2, 73*s, image=photo)
                else:
                    self.text(self.canvas, 12*s, 53*s, "Imagem pendente", 10, MUTED)
                self.text(self.canvas, 12*s, 120*s, f"#{row['sequence']:03d}  ·  {duration(row['metadata'].get('session_seconds'))}", 10)
                self.text(self.canvas, 12*s, 141*s, "SELECT + Y", 9, MUTED)
            else:
                self.text(self.canvas, 13*s, 14*s, f"Saves  {getattr(self, 'count', 0)}", 12, GOLD, True)
            return
        first = self.offset+1 if self.rows else 0
        self.text(self.canvas, 18*s, 12*s,
                  f"SUA JORNADA · {first}–{self.offset+len(self.rows)} / {getattr(self,'count',0)}",
                  9, GOLD, True)
        self.text(self.canvas, 18*s, 29*s, "Gravações", 22, TEXT, True)
        self.text(self.canvas, w-33*s, 16*s, "×", 22, MUTED)
        self.hits.append(((w-47*s, 0, w, 48*s), "close", None))
        for index, row in enumerate(self.rows):
            top = header + rh*index
            bounds = (9*s, top, w-9*s, top+rh-5*s)
            self.rounded(self.canvas, bounds, 9, fill="#25231f" if index == self.selected else SURFACE,
                         outline=GOLD if index == self.selected else "#2d2c30")
            self.hits.append((bounds, "load", index))
            photo = self.photo(row, 93*s, rh-16*s)
            if photo:
                self.canvas.create_image(60*s, top+(rh-5*s)/2, image=photo)
            else:
                self.text(self.canvas, 21*s, top+24*s, "SAVE" if row["kind"] == "save" else "STATE", 11, MUTED)
            tx = 118*s
            self.text(self.canvas, tx, top+8*s, f"#{row['sequence']:03d}", 15, TEXT, True)
            self.text(self.canvas, w-40*s, top+6*s, "♥" if row["favorite"] else "♡", 20,
                      "#e6b59f" if row["favorite"] else MUTED)
            self.hits.append(((w-51*s, top, w-12*s, top+34*s), "favorite", index))
            try:
                when = dt.datetime.fromisoformat(row["created_at"]).strftime("%d/%m/%y · %H:%M:%S")
            except ValueError:
                when = "Data indisponível"
            self.text(self.canvas, tx, top+31*s, when, 10, MUTED)
            self.text(self.canvas, tx, top+49*s, duration(row["metadata"].get("total_observed_seconds")), 10, MUTED)
            labels = badges(row)
            label = " · ".join(labels) if labels else "Save interno" if row["kind"] == "save" else "Save state"
            self.text(self.canvas, tx, top+67*s, label, 10, GOLD, width=w-tx-18*s)
            self.text(self.canvas, w-40*s, top+rh-27*s, "•••", 12, MUTED)
            self.hits.append(((w-53*s, top+rh-35*s, w-12*s, top+rh-6*s), "edit", index))
        if not self.rows:
            self.text(self.canvas, 22*s, header+18*s, "Sua primeira gravação aparece aqui.", 12, MUTED,
                      width=w-44*s)
        bottom = h-round(66*s)
        self.text(self.canvas, 18*s, bottom,
                  f"↑↓ Escolher · {self.confirm_label} Retomar · {self.back_label} Voltar", 9, MUTED)
        self.text(self.canvas, 18*s, bottom+18*s, f"{self.favorite_label} ♥ · Start Marcações", 9, MUTED)
        self.text(self.canvas, w-45*s, bottom+18*s, "CSV", 9, GOLD)
        self.hits.append(((w-60*s, bottom+14*s, w-12*s, bottom+33*s), "export", None))
        message = (self.notice or self.backend.message)[:110]
        item = self.text(self.canvas, 18*s, bottom+36*s, message, 9, GOLD, width=w-36*s)
        while message and self.canvas.bbox(item)[3] > h-4*s:
            message = message[:-4]
            self.canvas.itemconfigure(item, text=message+"…")

    def motion(self, event):
        self.last_inside = time.monotonic()
        if not self.expanded and not self.loading:
            self.expand()
        for bounds, action, index in self.hits:
            if action == "load" and bounds[0] <= event.x <= bounds[2] and bounds[1] <= event.y <= bounds[3]:
                if self.selected != index:
                    self.selected = index
                    self.draw()
                break

    def click(self, event):
        if not self.expanded:
            self.expand(keyboard=True)
            return
        for bounds, action, index in reversed(self.hits):
            if not bounds[0] <= event.x <= bounds[2] or not bounds[1] <= event.y <= bounds[3]:
                continue
            if index is not None:
                self.selected = index
            if action == "load":
                self.choose()
            elif action == "favorite":
                self.favorite()
            elif action == "edit":
                self.edit()
            elif action == "close":
                self.collapse()
            elif action == "export":
                self.backend.request("export")
            break

    def move(self, direction):
        if not self.rows:
            return
        target = self.selected + direction
        if target < 0 and self.offset:
            self.offset -= 1
            self.refresh()
        elif target >= len(self.rows) and self.offset + len(self.rows) < self.count:
            self.offset += 1
            self.refresh()
        else:
            self.selected = max(0, min(len(self.rows)-1, target))
        self.draw()

    def scroll(self, direction):
        if self.expanded:
            self.move(direction)

    def favorite(self):
        if self.rows:
            self.notice = ""
            self.backend.request("favorite", self.rows[self.selected]["sequence"])
            self.offset = self.selected = 0

    def edit(self):
        if not self.rows or self.editor:
            return
        self.notice = ""
        row = self.rows[self.selected]
        window = self.editor = tk.Toplevel(self.feedback.master)
        window.title(f"Gravação #{row['sequence']} · Marcações")
        window.configure(bg=BG)
        window.attributes("-topmost", True)
        window.resizable(False, False)
        def label(text):
            tk.Label(window, text=text, bg=BG, fg=TEXT, anchor="w",
                     font=("Noto Sans", 12)).pack(fill="x", padx=22, pady=(12, 4))
        label("Guarde o significado deste momento")
        details = (f"#{row['sequence']:03d} · {'Save interno' if row['kind']=='save' else 'Save state'}\n"
                   f"{row['created_at'][:19].replace('T',' ')}\n"
                   f"Sessão {duration(row['metadata'].get('session_seconds'))}\n"
                   f"Jornada {duration(row['metadata'].get('total_observed_seconds'))}")
        tk.Label(window, text=details, bg=BG, fg=MUTED, justify="left", anchor="w",
                 font=("Noto Sans", 10)).pack(fill="x", padx=22, pady=5)
        choices = {}
        self.editor_controls = []
        self.editor_focus = 0
        for key, value in TAGS.items():
            choices[key] = tk.BooleanVar(value=key in row["tags"])
            choice = tk.Checkbutton(window, text=value, variable=choices[key], bg=BG, fg=TEXT,
                           selectcolor=SURFACE, activebackground=BG, activeforeground=GOLD,
                           highlightcolor=GOLD, font=("Noto Sans", 11), anchor="w")
            choice.pack(fill="x", padx=22)
            self.editor_controls.append((choice, "choice"))
        label("Fase · zero remove a marcação")
        stage = tk.Spinbox(window, from_=0, to=9999, width=8, font=("Noto Sans", 12),
                           bg=SURFACE, fg=TEXT, buttonbackground=GOLD, relief="flat",
                           insertbackground=GOLD, highlightcolor=GOLD)
        stage.delete(0, "end")
        stage.insert(0, row["stage"] or 0)
        stage.pack(anchor="w", padx=22)
        self.editor_controls.append((stage, "stage"))
        label("Sua nota")
        note = tk.Text(window, width=35, height=2, bg=SURFACE, fg=TEXT, insertbackground=GOLD,
                       font=("Noto Sans", 11), wrap="word")
        note.insert("1.0", row["note"])
        note.pack(padx=22, pady=4)
        error = tk.Label(window, text="", bg=BG, fg="#e6b59f", wraplength=300)
        error.pack(padx=22)
        def save():
            try:
                number = int(stage.get())
                if not 0 <= number <= 9999 or len(note.get("1.0", "end").strip()) > 500:
                    raise ValueError("Confira a fase e a nota de até 500 caracteres.")
                self.backend.request("annotate", {"sequence": row["sequence"],
                    "tags": [key for key, value in choices.items() if value.get()],
                    "stage": number, "note": note.get("1.0", "end").strip()})
                close()
            except ValueError as exc:
                error.configure(text=str(exc))
        def close():
            window.destroy()
            self.editor = None
            self.window.focus_force()
        save_button = tk.Button(window, text="Salvar marcações", command=save, bg=GOLD, fg=BG,
                  font=("Noto Sans", 11, "bold"), relief="flat")
        save_button.pack(fill="x", padx=22, pady=14)
        tk.Label(window, text=f"↑↓ Escolher · {self.confirm_label} Alterar/Salvar · {self.back_label} Voltar",
                 bg=BG, fg=MUTED, font=("Noto Sans", 9)).pack(padx=22, pady=(0, 10))
        self.editor_controls.append((save_button, "save"))
        self.close_editor = close
        window.protocol("WM_DELETE_WINDOW", close)
        window.bind("<Escape>", lambda _e: close())
        window.update_idletasks()
        if self.geometry:
            x,y,w,h = self.geometry
            window.geometry(f"+{x+(w-window.winfo_width())//2}+{y+(h-window.winfo_height())//2}")
        window.focus_force()
        self.editor_controls[0][0].focus_set()

    def choose(self):
        if not self.rows or self.loading or self.editor:
            return
        row = self.backend.store.get(self.rows[self.selected]["sequence"])
        if row["kind"] == "save" and row["companion"] is None:
            self.notice = "Save interno sem state acompanhante. Use o menu de carregar do jogo."
            return
        self.notice = ""
        if self.backend.request("load", row["sequence"]) is False:
            self.notice = self.backend.message
            return
        self.loading = {"row": row, "start": time.monotonic(), "revision": self.backend.revision}
        self.transition.deiconify()
        self.transition.attributes("-alpha", 1)

    def animate(self, now):
        if not self.loading:
            return
        row, start = self.loading["row"], self.loading["start"]
        x,y,w,h = self.geometry
        elapsed = now - start
        progress = 1 if self.reduce_motion else min(1, elapsed/.26)
        eased = 1-(1-progress)**3
        tw = round(self.width + (min(w*.78, h*1.25)-self.width)*eased)
        th = round(self.row_height + (min(h*.78, tw*.8)-self.row_height)*eased)
        tx = round(x + 12 + ((w-tw)/2-12)*eased)
        ty = round(y + h*.22 + ((h-th)/2-h*.22)*eased)
        self.transition.geometry(f"{tw}x{th}{tx:+d}{ty:+d}")
        c = self.transition_canvas
        c.delete("all")
        self.rounded(c, (1,1,tw-1,th-1), fill=BG, outline=GOLD)
        photo = self.photo(row, tw-20, th-76*self.scale)
        if photo:
            c.create_image(tw/2, (th-50*self.scale)/2, image=photo)
        self.text(c, 18*self.scale, th-54*self.scale, f"Gravação #{row['sequence']:03d}", 17, TEXT, True)
        self.text(c, 18*self.scale, th-29*self.scale, self.backend.message[:85], 10, GOLD)
        if (elapsed > .5 and self.backend.load_pending is None and
                self.backend.load_requested is None and self.backend.revision != self.loading["revision"]):
            if self.backend.last_load_outcome != "loaded":
                self.notice = self.backend.message
                self.transition.withdraw()
                self.loading = None
                self.draw()
                return
            if "released" not in self.loading:
                self.loading["released"] = now
                self.loading["resume_status"] = self.backend.status_generation
                self.backend.request("resume")
            if (self.backend.status != "PLAYING" or
                    self.backend.status_generation < self.loading["resume_status"] + 2):
                return
            if "fade_start" not in self.loading:
                self.loading["fade_start"] = now
            fade = 1 if self.reduce_motion else min(1, (now-self.loading["fade_start"])/.18)
            self.transition.attributes("-alpha", 1-fade)
            if fade == 1:
                self.notice = self.backend.message
                self.transition.withdraw()
                self.loading = None
                self.collapse()

    def controller(self, held, now=None, player=0):
        now = time.monotonic() if now is None else now
        if player != 0:
            return self.captures_input
        new = held-self.pad_down
        self.pad_down = set(held)
        if not self.expanded and {"back", "y"} <= held:
            self.expand(source="controller")
            self.release_gate = True
            return True
        if self.release_gate:
            if not held:
                self.release_gate = False
            return True
        if self.editor:
            if "b" in new:
                self.close_editor()
                return True
            direction = int("dpdown" in new) - int("dpup" in new)
            if direction:
                self.editor_focus = (self.editor_focus+direction) % len(self.editor_controls)
            widget, kind = self.editor_controls[self.editor_focus]
            widget.focus_set()
            if "a" in new and kind in {"choice", "save"}:
                widget.invoke()
            if kind == "stage":
                difference = int("dpright" in new)-int("dpleft" in new)
                if difference:
                    value = max(0, min(9999, int(widget.get() or 0)+difference))
                    widget.delete(0, "end")
                    widget.insert(0, value)
            return True
        if self.expanded and not self.loading:
            if "dpup" in new:
                self.move(-1)
            if "dpdown" in new:
                self.move(1)
            if new & {"dpup", "dpdown"}:
                self.repeat_at = now+.4
            if "a" in new:
                self.choose()
            if "b" in new:
                self.collapse()
            if "x" in new:
                self.favorite()
            if "start" in new:
                self.edit()
        return self.captures_input

    def keyboard(self, held, keycodes):
        chord = (bool(held & {keycodes.KEY_LEFTCTRL,keycodes.KEY_RIGHTCTRL}) and
                 bool(held & {keycodes.KEY_LEFTSHIFT,keycodes.KEY_RIGHTSHIFT}) and keycodes.KEY_J in held)
        if chord and not self.expanded and not getattr(self, "keyboard_chord", False):
            self.expand(keyboard=True)
        self.keyboard_chord = chord
        if not held and not self.pad_down and not self.expanded:
            self.release_gate = False

    def update(self, focused, geometry, now):
        if not focused or not geometry:
            self.window.withdraw()
            self.transition.withdraw()
            if self.expanded and not self.loading:
                self.collapse()
            return
        changed = self.geometry != geometry
        self.geometry = geometry
        if now-self.last_refresh > .5 or self.last_revision != self.backend.revision:
            self.last_refresh = now
            self.last_revision = self.backend.revision
            self.refresh()
            changed = True
        if changed:
            self.draw()
        if self.expanded and not self.editor and not self.loading and now > self.repeat_at:
            direction = int("dpdown" in self.pad_down)-int("dpup" in self.pad_down)
            if direction:
                self.move(direction)
                self.repeat_at = now+.14
        if self.expanded and not self.editor and not self.loading:
            px,py = self.window.winfo_pointerxy()
            if self.window.winfo_rootx() <= px <= self.window.winfo_rootx()+self.width and self.window.winfo_rooty() <= py <= self.window.winfo_rooty()+self.height:
                self.last_inside = now
            elif self.expansion_mode == "hover" and now-self.last_inside > .8:
                self.collapse()
        self.window.deiconify()
        self.window.attributes("-alpha", .97)
        self.feedback.shape_window(self.window, self.width, self.height, [(0,0,self.width,self.height)],
                                   scale=self.scale)
        if self.loading:
            self.animate(now)
        if self.notice or self.backend.message:
            message = self.notice or self.backend.message
            self.window.title("EmuAtlas Saves · " + message[:100])

    def close(self):
        self.feedback.save_journey_hint = ""
        self.feedback.drawn = None
        for window in (self.editor, self.window, self.transition):
            if window:
                try:
                    window.destroy()
                except tk.TclError:
                    pass
