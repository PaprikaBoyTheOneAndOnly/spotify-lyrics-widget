#!/usr/bin/env python3
"""Spotify lyrics widget, Tkinter UI for Windows (Python standard library only).

A small borderless window that shows the lyrics of whatever Spotify is currently
playing (on any device) and hides itself when nothing is playing.
Right-click it -> Settings for colours, font, size, polling, re-login ...
All the logic lives in lyrics_core.py; this file only draws it.

Tk on Windows cannot draw a semi-transparent background behind fully opaque text, so
"Background opacity" fades the whole window (text included) and the corners are rounded by
making a key colour see-through. Everything else behaves like the Linux version.
"""

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, font as tkfont, ttk

import lyrics_core as core
from lyrics_core import SETTINGS_LAYOUT, build_view, clean_settings, load_json, load_settings, update_config

CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "spotify-lyrics-widget")

KEY_COLOR = "#010203"          # painted pixels of this colour are see-through (the rounded corners)
DEFAULT_FONT = "Segoe UI"
SYSTEM_FONT_LABEL = "(system default)"
MIN_ALPHA = 0.3                # never fade the window below this, or the text becomes unreadable
CURRENT_SCALE = 1.7            # the current line is drawn this much larger than the base font size

log = logging.getLogger("lyrics")


def mix(fg, bg, amount):
    """Colour `amount` (0..1) of the way from bg to fg: Tk has no text opacity, so dim by blending."""
    a = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(y + (x - y) * amount) for x, y in zip(a, b))


def window_alpha(settings):
    return max(MIN_ALPHA, settings["bg_opacity"])


class SettingsWindow:
    """Edits the look of the widget live; every change is applied at once and saved."""

    def __init__(self, target, spotify):
        self.target = target
        self.spotify = spotify
        self.values = dict(target.settings)
        self.vars = {}
        self.kinds = {}
        self.swatches = {}
        self.digits = {}
        self._save_id = None
        self._login_running = False

        self.win = tk.Toplevel(target.master)
        self.win.title("Lyrics widget settings")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(self.win, padding=16)
        frame.grid()
        frame.columnconfigure(1, weight=1)

        row = 0
        for item in SETTINGS_LAYOUT:
            if len(item) == 1:
                ttk.Label(frame, text=item[0], font=("Segoe UI", 10, "bold")).grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(12 if row else 0, 2))
            else:
                key, text, kind, opts = item
                ttk.Label(frame, text=text).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=3)
                self._make_control(frame, key, kind, **opts).grid(row=row, column=1, sticky="ew", pady=3)
            row += 1

        ttk.Label(frame, text="Spotify", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
        row += 1
        ttk.Label(frame, text="Client ID").grid(row=row, column=0, sticky="w", padx=(0, 14), pady=3)
        self.client_var = tk.StringVar(value=spotify.client_id or "")
        ttk.Entry(frame, textvariable=self.client_var, width=36).grid(row=row, column=1, sticky="ew", pady=3)
        row += 1
        self.login_button = ttk.Button(frame, text="Log in to Spotify again", command=self._on_login)
        self.login_button.grid(row=row, column=0, sticky="w", pady=3)
        self.status = ttk.Label(frame, text="", wraplength=260, justify="left")
        self.status.grid(row=row, column=1, sticky="w", pady=3)
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Reset to defaults", command=self._on_reset).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="Close", command=self.close).grid(row=0, column=1)

        self.win.update_idletasks()
        x = (self.win.winfo_screenwidth() - self.win.winfo_reqwidth()) // 2
        y = (self.win.winfo_screenheight() - self.win.winfo_reqheight()) // 3
        self.win.geometry(f"+{x}+{y}")

    # -- controls -----------------------------------------------------------------
    def _make_control(self, parent, key, kind, lo=0, hi=0, step=1, digits=0):
        self.kinds[key] = kind
        self.digits[key] = digits
        value = self.values[key]
        if kind == "color":
            widget = tk.Button(parent, width=10, relief="groove", command=lambda: self._pick_color(key))
            self.swatches[key] = widget
        elif kind == "percent":  # stored as 0..1, shown as 0..100
            var = tk.DoubleVar(value=value * 100)
            widget = tk.Scale(parent, from_=lo, to=hi, orient="horizontal", length=220, variable=var,
                              resolution=1, showvalue=True, command=lambda _v, k=key: self._set(k, round(var.get() / 100, 2)))
            self.vars[key] = var
        elif kind == "font":
            var = tk.StringVar(value=value or SYSTEM_FONT_LABEL)
            families = [SYSTEM_FONT_LABEL] + sorted({f for f in tkfont.families() if not f.startswith("@")})
            widget = ttk.Combobox(parent, textvariable=var, values=families, width=30)
            widget.bind("<<ComboboxSelected>>", lambda _e, k=key: self._set_font(k))
            widget.bind("<Return>", lambda _e, k=key: self._set_font(k))
            widget.bind("<FocusOut>", lambda _e, k=key: self._set_font(k))
            self.vars[key] = var
        elif kind == "spin":
            var = tk.StringVar(value=self._format(value, digits))
            widget = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=var, width=10,
                                 command=lambda k=key: self._set_spin(k, digits))
            widget.bind("<Return>", lambda _e, k=key: self._set_spin(k, digits))
            widget.bind("<FocusOut>", lambda _e, k=key: self._set_spin(k, digits))
            self.vars[key] = var
        else:  # check
            var = tk.BooleanVar(value=value)
            widget = ttk.Checkbutton(parent, variable=var, command=lambda k=key: self._set(k, bool(var.get())))
            self.vars[key] = var
        if kind == "color":
            self._paint_swatch(key)
        return widget

    @staticmethod
    def _format(value, digits):
        return f"{value:.{digits}f}" if digits else str(int(value))

    def _paint_swatch(self, key):
        color = self.values[key]
        dark = sum(int(color[i:i + 2], 16) for i in (1, 3, 5)) < 384
        self.swatches[key].configure(bg=color, activebackground=color, text=color,
                                     fg="#ffffff" if dark else "#000000")

    def _pick_color(self, key):
        _rgb, hex_color = colorchooser.askcolor(color=self.values[key], parent=self.win, title="Pick a color")
        if hex_color:
            self._set(key, hex_color.lower())
            self._paint_swatch(key)

    def _set_font(self, key):
        name = self.vars[key].get().strip()
        self._set(key, "" if name in ("", SYSTEM_FONT_LABEL) else name)

    def _set_spin(self, key, digits):
        try:
            number = float(self.vars[key].get().replace(",", "."))
        except ValueError:
            number = self.values[key]
        self._set(key, number)
        self.vars[key].set(self._format(self.values[key], digits))  # show the clamped value

    def _set(self, key, value):
        if self.values.get(key) == value:
            return
        self.values[key] = value
        self.values = clean_settings(self.values)
        self.target.apply_settings(self.values)
        self._schedule_save()

    # -- saving -------------------------------------------------------------------
    def _schedule_save(self):
        if self._save_id:
            self.win.after_cancel(self._save_id)
        self._save_id = self.win.after(400, self._save_now)

    def _save_now(self):
        self._save_id = None
        update_config(**self.values)

    def _flush(self):
        """Write anything still pending (called when the window closes)."""
        if self._save_id:
            self.win.after_cancel(self._save_id)
            self._save_now()
        client_id = self.client_var.get().strip()
        if client_id and client_id != (load_json(core.CONFIG_FILE, {}).get("client_id") or ""):
            update_config(client_id=client_id)  # used from the next start / next login

    def _on_reset(self):
        self.values = clean_settings({})
        for key, kind in self.kinds.items():
            value = self.values[key]
            if kind == "color":
                self._paint_swatch(key)
            elif kind == "percent":
                self.vars[key].set(value * 100)
            elif kind == "font":
                self.vars[key].set(value or SYSTEM_FONT_LABEL)
            elif kind == "spin":
                self.vars[key].set(self._format(value, self.digits[key]))
            else:
                self.vars[key].set(value)
        self.target.apply_settings(self.values)
        self._schedule_save()

    def present(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self):
        self._flush()
        self.win.destroy()
        self.target.settings_closed()

    # -- Spotify login ------------------------------------------------------------
    def _on_login(self):
        if self._login_running:
            return
        client_id = self.client_var.get().strip()
        if not client_id:
            self.status.configure(text="Enter your Spotify Client ID first.")
            return
        self._login_running = True
        self.login_button.state(["disabled"])
        self.status.configure(text="Finish the login in your browser (waits up to 5 minutes)...")
        threading.Thread(target=self._login_worker, args=(client_id,), daemon=True).start()

    def _login_worker(self, client_id):
        old_client_id = self.spotify.client_id
        self.spotify.client_id = client_id
        try:
            self.spotify.login()
            update_config(client_id=client_id)
            message = "Logged in."
        except Exception as e:  # keep the GUI alive whatever went wrong
            self.spotify.client_id = old_client_id  # the existing login keeps working
            message = f"Login failed: {e}"
        self.target.post(lambda: self._login_done(message))  # Tk may only be touched from its own thread

    def _login_done(self, message):
        self._login_running = False
        if self.win.winfo_exists():
            self.login_button.state(["!disabled"])
            self.status.configure(text=message)


class LyricsWindow:
    def __init__(self, poller, spotify):
        self.poller = poller
        self.spotify = spotify
        self.settings = load_settings()
        self.preview = False  # True while the settings window is open: sample text if nothing plays
        self._shown_key = None
        self._save_id = None
        self._settings_window = None
        self._visible = False
        self._queue = queue.Queue()  # work handed over from other threads (run in _tick)
        self._drag = (0, 0)

        self.master = tk.Tk()
        self.master.withdraw()  # hidden owner of all windows; destroying it ends the program
        self.win = tk.Toplevel(self.master)
        self.win.withdraw()
        self.win.title("Lyrics")
        self.win.overrideredirect(True)  # no title bar, no taskbar button
        self.win.configure(bg=KEY_COLOR)
        try:
            self.win.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            pass  # not Windows: corners stay square

        self.canvas = tk.Canvas(self.win, bg=KEY_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="Settings...", command=self.open_settings)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)
        self.win.bind("<ButtonPress-1>", self._on_press)
        self.win.bind("<B1-Motion>", self._on_drag)
        self.win.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        cfg = load_json(core.CONFIG_FILE, {})
        self.pos = self._valid_pos(cfg.get("x"), cfg.get("y"))
        self.apply_settings(self.settings)
        self.master.after(100, self._tick)

    def run(self):
        self.master.mainloop()

    def quit(self):
        self.master.destroy()

    def post(self, fn):
        """Run fn on the Tk thread soon. Safe to call from any thread."""
        self._queue.put(fn)

    # -- settings -----------------------------------------------------------------
    def apply_settings(self, settings):
        """Apply a validated settings dict immediately (called on startup and by the settings window)."""
        self.settings = settings
        w, h = settings["width"], settings["height"]
        self.win.geometry(f"{w}x{h}+{self.pos[0]}+{self.pos[1]}")
        self.canvas.configure(width=w, height=h)
        self.win.attributes("-topmost", settings["keep_above"])
        self.win.attributes("-alpha", window_alpha(settings))
        if self.poller is not None:
            self.poller.poll_seconds = float(settings["poll_seconds"])
        self._shown_key = None  # force a re-render with the new look

    def open_settings(self):
        if self._settings_window is None:
            self._settings_window = SettingsWindow(self, self.spotify)
        self.preview = True
        self._settings_window.present()

    def settings_closed(self):
        self._settings_window = None
        self.preview = False
        self._shown_key = None

    # -- placement ----------------------------------------------------------------
    def _default_pos(self):
        s = self.settings
        return ((self.master.winfo_screenwidth() - s["width"]) // 2,
                self.master.winfo_screenheight() - s["height"] - 80)

    def _valid_pos(self, x, y):
        """The saved position, unless it is (almost) off screen, e.g. a monitor was unplugged."""
        s = self.settings
        sw, sh = self.master.winfo_screenwidth(), self.master.winfo_screenheight()
        if isinstance(x, int) and isinstance(y, int) and -s["width"] + 60 < x < sw - 60 and 0 <= y < sh - 40:
            return (x, y)
        return self._default_pos()

    def _on_press(self, event):
        self._drag = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _on_drag(self, event):
        self.win.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _on_release(self, _event):
        pos = (self.win.winfo_x(), self.win.winfo_y())
        if pos != tuple(self.pos):
            self.pos = pos
            if self._save_id:
                self.master.after_cancel(self._save_id)
            self._save_id = self.master.after(800, self._save_position)

    def _save_position(self):
        self._save_id = None
        update_config(x=self.pos[0], y=self.pos[1])

    # -- drawing ------------------------------------------------------------------
    def _draw(self, view):
        s = self.settings
        w, h = s["width"], s["height"]
        c = self.canvas
        c.delete("all")

        r = min(s["radius"], w // 2, h // 2)
        if r > 0:
            pts = [r, 0, w - r, 0, w, 0, w, r, w, h - r, w, h, w - r, h, r, h, 0, h, 0, h - r, 0, r, 0, 0]
            c.create_polygon(pts, smooth=True, fill=s["bg_color"], outline=s["bg_color"])
        else:
            c.create_rectangle(0, 0, w, h, fill=s["bg_color"], outline=s["bg_color"])

        family = s["font_family"] or DEFAULT_FONT
        normal = (family, -s["font_size"])
        big = (family, -round(s["font_size"] * CURRENT_SCALE), "bold")
        dim = mix(s["text_color"], s["bg_color"], s["dim_opacity"])
        wrap = max(100, w - 64)

        items, y = [], 0
        for text, current in view.rows:
            item = c.create_text(w // 2, y, text=text, anchor="n", justify="center", width=wrap,
                                 font=big if current else normal, fill=s["text_color"] if current else dim)
            x1, y1, x2, y2 = c.bbox(item)
            y = y2 + 4
            items.append(item)
        if items:  # centre the block vertically (never above the top edge)
            offset = max(0, (h - y) / 2)
            for item in items:
                c.move(item, 0, offset)

        c.create_text(14, 8, text=view.caption, anchor="nw", font=(family, -12), fill=mix(s["text_color"], s["bg_color"], 0.6))

    # -- main tick ----------------------------------------------------------------
    def _tick(self):
        while True:
            try:
                self._queue.get_nowait()()
            except queue.Empty:
                break
            except Exception:
                log.exception("Queued UI call failed")

        if self.poller.fatal:
            log.error(self.poller.fatal)
            print(self.poller.fatal, file=sys.stderr)
            self.quit()
            return

        view = build_view(self.poller.snapshot, self.settings, self.preview)
        if view is None:
            if self._visible:
                self.win.withdraw()
                self._visible = False
                self._shown_key = None
        else:
            if view.key != self._shown_key:
                self._draw(view)
                self._shown_key = view.key
            if not self._visible:
                self.win.geometry(f"+{self.pos[0]}+{self.pos[1]}")
                self.win.deiconify()
                self.win.attributes("-topmost", self.settings["keep_above"])
                self._visible = True
        self.master.after(100, self._tick)


def setup_logging(debug):
    level = logging.DEBUG if debug else logging.INFO
    fmt = dict(format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if sys.stderr is None:  # started with pythonw.exe (autostart): there is no console, so log to a file
        os.makedirs(CONFIG_DIR, exist_ok=True)
        logging.basicConfig(level=level, filename=os.path.join(CONFIG_DIR, "widget.log"), encoding="utf-8", **fmt)
    else:
        logging.basicConfig(level=level, **fmt)


def enable_dpi_awareness():
    """Sharp text and real pixel sizes on scaled displays."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main():
    args = core.parse_args()
    core.init(CONFIG_DIR)
    setup_logging(args.debug)
    enable_dpi_awareness()

    if args.settings and core.send_command("settings"):
        print("Opened the settings in the running widget.")
        return
    server = core.ControlServer()
    if not server.bind():
        sys.exit(f"Port {core.CONTROL_PORT} is in use: is the widget already running?")
    server.start()

    spotify = core.setup_spotify(args)

    poller = core.Poller(spotify, load_settings()["poll_seconds"])
    poller.start()
    window = LyricsWindow(poller, spotify)  # stays hidden until a song with lyrics is playing
    server.handler = lambda command: window.post(window.open_settings)  # runs on the Tk thread
    if args.settings:
        window.post(window.open_settings)
    window.run()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            log.error(e.code)  # visible in widget.log when there is no console
        raise
