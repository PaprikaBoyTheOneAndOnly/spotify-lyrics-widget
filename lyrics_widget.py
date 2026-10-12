#!/usr/bin/env python3
"""Spotify lyrics widget for Ubuntu.

A small borderless window that shows the lyrics of whatever Spotify is currently
playing (on any device) and hides itself when nothing is playing.
"""

import argparse
import logging
import os
import signal
import sys

# GNOME on Wayland ignores "always on top" and window positioning for normal apps;
# XWayland honours both. Override with GDK_BACKEND=wayland if you prefer.
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from lyrics_core import (CONFIG_FILE, POLL_PLAYING_SECONDS, Poller, Spotify,  # noqa: E402
                         load_json, locate, save_json)

WIDTH, HEIGHT = 900, 240

CSS = b"""
window {
    background-color: rgba(18, 18, 18, 0.72);
    border-radius: 18px;
}
label {
    color: #ffffff;
    font-size: 16px;
    padding: 16px 32px;
}
"""

SETUP_HELP = """
First-time setup: you need a (free) Spotify developer app.
  1. Go to https://developer.spotify.com/dashboard and create an app.
  2. Add this Redirect URI:  http://127.0.0.1:8888/callback
  3. Tick "Web API", save, then copy the Client ID from the app's settings.
"""


def esc(text):
    return GLib.markup_escape_text(text if text else "♪")  # empty line -> music note


def render(lines, idx):
    """Pango markup: previous line, current line (big), next two lines."""
    dim = lambda t: f'<span alpha="50%">{esc(t)}</span>'
    parts = []
    if idx >= 1:
        parts.append(dim(lines[idx - 1][1]))
    current = lines[idx][1] if idx >= 0 else ""
    parts.append(f'<span size="xx-large" weight="bold">{esc(current)}</span>')
    parts.extend(dim(text) for _, text in lines[idx + 1: idx + 3])
    return "\n".join(parts)


class LyricsWindow(Gtk.Window):
    def __init__(self, poller, cfg):
        super().__init__(title="Lyrics")
        self.poller = poller
        self.cfg = cfg
        self.offset_ms = int(cfg.get("offset_ms", 0))  # positive = show lines earlier
        self._shown_key = None
        self._save_id = None

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_resizable(False)
        self.set_size_request(WIDTH, HEIGHT)
        self.stick()
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.label = Gtk.Label()
        self.label.set_use_markup(True)
        self.label.set_justify(Gtk.Justification.CENTER)
        self.label.set_line_wrap(True)
        self.label.set_max_width_chars(60)
        self.add(self.label)

        self.pos = (cfg["x"], cfg["y"]) if "x" in cfg and "y" in cfg else self._default_pos()

        self.menu = Gtk.Menu()
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_item)
        self.menu.show_all()

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_press)
        self.connect("configure-event", self._on_configure)
        self.connect("destroy", lambda *_: Gtk.main_quit())
        GLib.timeout_add(100, self._tick)

    # -- placement ----------------------------------------------------------------
    def _default_pos(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        g = monitor.get_geometry()
        return (g.x + (g.width - WIDTH) // 2, g.y + g.height - HEIGHT - 80)

    def _on_press(self, _widget, event):
        if event.button == 1:      # drag the widget anywhere
            self.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
        elif event.button == 3:    # right click -> Quit
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
        self.cfg["x"], self.cfg["y"] = self.pos
        save_json(CONFIG_FILE, self.cfg)
        return False

    # -- main tick ----------------------------------------------------------------
    def _tick(self):
        if self.poller.fatal:
            print(self.poller.fatal, file=sys.stderr)
            Gtk.main_quit()
            return False

        snap = self.poller.snapshot
        idx = locate(snap, self.offset_ms)  # None = paused / stale / song over -> show nothing
        if idx is None:
            if self.get_visible():
                self.hide()
                self._shown_key = None
            return True

        key = (snap.track_id, idx)
        if key != self._shown_key:
            self.label.set_markup(render(snap.lines, idx))
            self._shown_key = key
        if not self.get_visible():
            self.move(*self.pos)
            self.show_all()
        return True


def main():
    parser = argparse.ArgumentParser(description="Spotify lyrics widget")
    parser.add_argument("--login", action="store_true", help="(re)do the Spotify login")
    parser.add_argument("--debug", action="store_true", help="log every Spotify poll to the terminal")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    cfg = load_json(CONFIG_FILE, {})
    client_id = os.environ.get("SPOTIFY_CLIENT_ID") or cfg.get("client_id")
    if not client_id:
        print(SETUP_HELP)
        client_id = input("Paste your Spotify Client ID: ").strip()
        if not client_id:
            sys.exit("No Client ID given.")
        cfg["client_id"] = client_id
        save_json(CONFIG_FILE, cfg)

    spotify = Spotify(client_id)
    if args.login or not spotify.logged_in:
        spotify.login()
        print("Login OK. Starting the widget (right-click it to quit, drag to move).")

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C work while GTK is running
    poller = Poller(spotify, cfg.get("poll_seconds", POLL_PLAYING_SECONDS))
    poller.start()
    LyricsWindow(poller, cfg)  # stays hidden until a song with lyrics is playing
    Gtk.main()


if __name__ == "__main__":
    main()
