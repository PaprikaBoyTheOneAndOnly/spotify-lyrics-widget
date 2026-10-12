#!/usr/bin/env python3
"""Spotify lyrics widget for Ubuntu.

A small borderless window that shows the lyrics of whatever Spotify is currently
playing (on any device) and hides itself when nothing is playing.
Right-click it -> Settings for colours, font, size, polling, re-login ...
"""

import argparse
import atexit
import logging
import os
import signal
import sys
import threading

# GNOME on Wayland ignores "always on top" and window positioning for normal apps;
# XWayland honours both. Override with GDK_BACKEND=wayland if you prefer.
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from lyrics_core import (CONFIG_FILE, Poller, Spotify, build_css, clean_settings,  # noqa: E402
                         find_running_widget, load_json, load_settings, locate,
                         remove_pid_file, render_markup, update_config, write_pid_file)

SETUP_HELP = """
First-time setup: you need a (free) Spotify developer app.
  1. Go to https://developer.spotify.com/dashboard and create an app.
  2. Add this Redirect URI:  http://127.0.0.1:8888/callback
  3. Tick "Web API", save, then copy the Client ID from the app's settings.
"""

# Shown while the settings window is open and no song with lyrics is playing.
SAMPLE_LINES = [(i * 1000, text) for i, text in enumerate([
    "Earlier line", "Earlier line", "Earlier line", "Previous line",
    "This is the current line",
    "Next line", "Line after that", "Another line", "Another line", "Another line", "Last line",
])]
SAMPLE_INDEX = 4

# Rows of the settings window: (key, label, kind, options); 1-tuples are section headings.
LAYOUT = [
    ("Appearance",),
    ("bg_color", "Background color", "color", {}),
    ("bg_opacity", "Background opacity (%)", "percent", {"lo": 0, "hi": 100}),
    ("text_color", "Text color", "color", {}),
    ("font_family", "Font", "font", {}),
    ("font_size", "Font size (px)", "spin", {"lo": 8, "hi": 72, "step": 1}),
    ("dim_opacity", "Other lines opacity (%)", "percent", {"lo": 10, "hi": 100}),
    ("radius", "Corner radius (px)", "spin", {"lo": 0, "hi": 60, "step": 1}),
    ("Layout",),
    ("width", "Width (px)", "spin", {"lo": 300, "hi": 2400, "step": 10}),
    ("height", "Height (px)", "spin", {"lo": 80, "hi": 1200, "step": 10}),
    ("lines_before", "Lines before current", "spin", {"lo": 0, "hi": 4, "step": 1}),
    ("lines_after", "Lines after current", "spin", {"lo": 0, "hi": 6, "step": 1}),
    ("keep_above", "Always on top", "check", {}),
    ("Timing",),
    ("poll_seconds", "Ask Spotify every (s)", "spin", {"lo": 1, "hi": 30, "step": 0.5, "digits": 1}),
    ("offset_ms", "Lyrics timing offset (ms)", "spin", {"lo": -5000, "hi": 5000, "step": 50}),
]


def rgba_to_hex(rgba):
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue))


class SettingsWindow(Gtk.Window):
    """Edits the look of the widget live; every change is applied at once and saved."""

    def __init__(self, target, spotify):
        super().__init__(title="Lyrics widget settings")
        self.target = target
        self.spotify = spotify
        self.values = dict(target.settings)
        self.controls = {}
        self.kinds = {}
        self._loading = False
        self._save_id = None
        self._login_running = False

        self.set_border_width(16)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", self._on_delete)

        grid = Gtk.Grid(column_spacing=14, row_spacing=8)
        self.add(grid)

        row = 0
        for item in LAYOUT:
            if len(item) == 1:
                heading = Gtk.Label(xalign=0)
                heading.set_markup(f"<b>{item[0]}</b>")
                if row:
                    heading.set_margin_top(12)
                grid.attach(heading, 0, row, 2, 1)
            else:
                key, text, kind, opts = item
                grid.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
                grid.attach(self._make_control(key, kind, **opts), 1, row, 1, 1)
            row += 1

        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Spotify</b>")
        heading.set_margin_top(12)
        grid.attach(heading, 0, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label="Client ID", xalign=0), 0, row, 1, 1)
        self.client_entry = Gtk.Entry()
        self.client_entry.set_width_chars(34)
        self.client_entry.set_text(spotify.client_id or "")
        grid.attach(self.client_entry, 1, row, 1, 1)
        row += 1

        self.login_button = Gtk.Button(label="Log in to Spotify again")
        self.login_button.connect("clicked", self._on_login)
        grid.attach(self.login_button, 0, row, 1, 1)
        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(34)
        grid.attach(self.status, 1, row, 1, 1)
        row += 1

        buttons = Gtk.Box(spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(14)
        reset = Gtk.Button(label="Reset to defaults")
        reset.connect("clicked", self._on_reset)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self._close())
        buttons.pack_start(reset, False, False, 0)
        buttons.pack_start(close, False, False, 0)
        grid.attach(buttons, 0, row, 2, 1)

        self.show_all()

    # -- controls -----------------------------------------------------------------
    def _make_control(self, key, kind, lo=0, hi=0, step=1, digits=0):
        self.kinds[key] = kind
        if kind == "color":
            widget = Gtk.ColorButton()
            widget.connect("color-set", lambda b: self._set(key, rgba_to_hex(b.get_rgba())))
        elif kind == "percent":  # stored as 0..1, shown as 0..100
            widget = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
            widget.set_digits(0)
            widget.set_hexpand(True)
            widget.set_size_request(220, -1)
            widget.connect("value-changed", lambda s: self._set(key, round(s.get_value() / 100, 2)))
        elif kind == "font":
            widget = Gtk.FontButton()
            widget.set_show_size(False)
            widget.connect("font-set", lambda b: self._set(key, b.get_font_desc().get_family() or ""))
        elif kind == "spin":
            widget = Gtk.SpinButton.new_with_range(lo, hi, step)
            widget.set_digits(digits)
            widget.connect("value-changed",
                           lambda s: self._set(key, s.get_value() if digits else s.get_value_as_int()))
        else:  # check
            widget = Gtk.CheckButton()
            widget.connect("toggled", lambda b: self._set(key, b.get_active()))
        self.controls[key] = widget
        self._show_value(key)
        return widget

    def _show_value(self, key):
        """Put self.values[key] into its control without triggering the change handler."""
        widget, value, kind = self.controls[key], self.values[key], self.kinds[key]
        self._loading = True
        try:
            if kind == "color":
                rgba = Gdk.RGBA()
                rgba.parse(value)
                widget.set_rgba(rgba)
            elif kind == "percent":
                widget.set_value(value * 100)
            elif kind == "font":
                widget.set_font((value or "Sans") + " 12")
            elif kind == "spin":
                widget.set_value(value)
            else:
                widget.set_active(value)
        finally:
            self._loading = False

    def _set(self, key, value):
        if self._loading:
            return
        self.values[key] = value
        self.values = clean_settings(self.values)
        self.target.apply_settings(self.values)
        self._schedule_save()

    # -- saving -------------------------------------------------------------------
    def _schedule_save(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(400, self._save_now)

    def _save_now(self):
        self._save_id = None
        update_config(**self.values)
        return False

    def _flush(self):
        """Write anything still pending (called when the window closes)."""
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_now()
        client_id = self.client_entry.get_text().strip()
        if client_id and client_id != (load_json(CONFIG_FILE, {}).get("client_id") or ""):
            update_config(client_id=client_id)  # used from the next start / next login

    def _on_reset(self, *_):
        self.values = clean_settings({})
        for key in self.controls:
            self._show_value(key)
        self.target.apply_settings(self.values)
        self._schedule_save()

    def _on_delete(self, *_):
        self._flush()
        return False  # let GTK destroy the window

    def _close(self):
        self._flush()
        self.destroy()

    # -- Spotify login ------------------------------------------------------------
    def _on_login(self, *_):
        if self._login_running:
            return
        client_id = self.client_entry.get_text().strip()
        if not client_id:
            self.status.set_text("Enter your Spotify Client ID first.")
            return
        self._login_running = True
        self.login_button.set_sensitive(False)
        self.status.set_text("Finish the login in your browser (waits up to 5 minutes)...")
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
        GLib.idle_add(self._login_done, message)

    def _login_done(self, message):
        self._login_running = False
        self.login_button.set_sensitive(True)
        self.status.set_text(message)
        return False


class LyricsWindow(Gtk.Window):
    def __init__(self, poller, spotify):
        super().__init__(title="Lyrics")
        self.poller = poller
        self.spotify = spotify
        self.settings = load_settings()
        self.preview = False  # True while the settings window is open: sample text if nothing plays
        self._shown_key = None
        self._save_id = None
        self._settings_window = None

        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_resizable(False)
        self.stick()
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        # The CSS is scoped to these two style classes so it never restyles the settings window.
        self.get_style_context().add_class("lyrics-window")
        self.provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.label = Gtk.Label()
        self.label.get_style_context().add_class("lyrics-text")
        self.label.set_use_markup(True)
        self.label.set_justify(Gtk.Justification.CENTER)
        self.label.set_line_wrap(True)
        self.label.set_max_width_chars(60)
        self.add(self.label)

        cfg = load_json(CONFIG_FILE, {})
        self.pos = (cfg["x"], cfg["y"]) if "x" in cfg and "y" in cfg else self._default_pos()
        self.apply_settings(self.settings)

        self.menu = Gtk.Menu()
        settings_item = Gtk.MenuItem(label="Settings...")
        settings_item.connect("activate", lambda *_: self.open_settings())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(settings_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(quit_item)
        self.menu.show_all()

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_press)
        self.connect("configure-event", self._on_configure)
        self.connect("destroy", lambda *_: Gtk.main_quit())
        GLib.timeout_add(100, self._tick)

    # -- settings -----------------------------------------------------------------
    def apply_settings(self, settings):
        """Apply a validated settings dict immediately (called on startup and by the settings window)."""
        self.settings = settings
        try:
            self.provider.load_from_data(build_css(settings).encode())
        except GLib.Error as e:
            logging.getLogger("lyrics").warning("Could not apply the style: %s", e)
        self.set_size_request(settings["width"], settings["height"])
        self.resize(settings["width"], settings["height"])
        self.set_keep_above(settings["keep_above"])
        if self.poller is not None:
            self.poller.poll_seconds = float(settings["poll_seconds"])
        self._shown_key = None  # force a re-render with the new look

    def open_settings(self):
        if self._settings_window is None:
            self._settings_window = SettingsWindow(self, self.spotify)
            self._settings_window.connect("destroy", self._on_settings_closed)
        self.preview = True
        self._settings_window.present()

    def _on_settings_closed(self, *_):
        self._settings_window = None
        self.preview = False
        self._shown_key = None

    def on_signal(self):
        """SIGUSR1 (sent by `lyrics_widget.py --settings`) opens the settings window."""
        self.open_settings()
        return GLib.SOURCE_CONTINUE

    # -- placement ----------------------------------------------------------------
    def _default_pos(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        g = monitor.get_geometry()
        return (g.x + (g.width - self.settings["width"]) // 2,
                g.y + g.height - self.settings["height"] - 80)

    def _on_press(self, _widget, event):
        if event.button == 1:      # drag the widget anywhere
            self.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
        elif event.button == 3:    # right click -> Settings / Quit
            self.menu.popup_at_pointer(event)
        return True

    def _on_configure(self, *_):
        pos = tuple(self.get_position())
        if pos != tuple(self.pos):
            self.pos = pos
            if self._save_id:
                GLib.source_remove(self._save_id)
            self._save_id = GLib.timeout_add(800, self._save_position)

    def _save_position(self):
        self._save_id = None
        update_config(x=self.pos[0], y=self.pos[1])
        return False

    # -- main tick ----------------------------------------------------------------
    def _tick(self):
        if self.poller.fatal:
            print(self.poller.fatal, file=sys.stderr)
            Gtk.main_quit()
            return False

        s = self.settings
        snap = self.poller.snapshot
        idx = locate(snap, s["offset_ms"])  # None = paused / stale / song over -> show nothing
        if idx is not None:
            lines, key = snap.lines, (snap.track_id, idx)
        elif self.preview:
            lines, idx, key = SAMPLE_LINES, SAMPLE_INDEX, ("sample", SAMPLE_INDEX)
        else:
            if self.get_visible():
                self.hide()
                self._shown_key = None
            return True

        if key != self._shown_key:
            self.label.set_markup(render_markup(lines, idx, s["lines_before"], s["lines_after"], s["dim_opacity"]))
            self._shown_key = key
        if not self.get_visible():
            self.move(*self.pos)
            self.show_all()
        return True


def main():
    parser = argparse.ArgumentParser(description="Spotify lyrics widget")
    parser.add_argument("--login", action="store_true", help="(re)do the Spotify login")
    parser.add_argument("--settings", action="store_true",
                        help="open the settings window (in the running widget, or start one)")
    parser.add_argument("--debug", action="store_true", help="log every Spotify poll to the terminal")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.settings:
        running = find_running_widget()
        if running and running != os.getpid():
            os.kill(running, signal.SIGUSR1)
            print("Opened the settings in the running widget.")
            return

    interactive = sys.stdin.isatty()
    cfg = load_json(CONFIG_FILE, {})
    client_id = os.environ.get("SPOTIFY_CLIENT_ID") or cfg.get("client_id")
    if not client_id:
        if not interactive:
            sys.exit("No Spotify Client ID yet. Run the widget once from a terminal to set it up.")
        print(SETUP_HELP)
        client_id = input("Paste your Spotify Client ID: ").strip()
        if not client_id:
            sys.exit("No Client ID given.")
        update_config(client_id=client_id)

    spotify = Spotify(client_id)
    if args.login or not spotify.logged_in:
        if not interactive:
            sys.exit("Not logged in to Spotify. Run the widget once from a terminal to log in.")
        spotify.login()
        print("Login OK. Starting the widget (right-click it for settings or to quit, drag to move).")

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C work while GTK is running
    poller = Poller(spotify, load_settings()["poll_seconds"])
    poller.start()
    window = LyricsWindow(poller, spotify)  # stays hidden until a song with lyrics is playing

    try:
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, window.on_signal)
    except Exception:
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)  # never let a stray SIGUSR1 kill the widget
    write_pid_file()
    atexit.register(remove_pid_file)
    if args.settings:
        GLib.idle_add(window.open_settings)
    Gtk.main()


if __name__ == "__main__":
    main()
