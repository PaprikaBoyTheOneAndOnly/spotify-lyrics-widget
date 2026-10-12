"""Core logic for the Spotify lyrics widget (standard library only):
Spotify login + now-playing polling, LRCLIB lyrics lookup, LRC parsing.
"""

import base64
import bisect
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "spotify-lyrics-widget"
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

REDIRECT_PORT = 8888
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "user-read-currently-playing user-read-playback-state"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
LRCLIB_URL = "https://lrclib.net/api"
USER_AGENT = "spotify-lyrics-widget/1.0 (personal desktop widget)"

STATE_FILE = CONFIG_DIR / "state.json"   # remembers a Spotify "back off" across restarts
PID_FILE = CONFIG_DIR / "widget.pid"     # lets `--settings` find the running widget

POLL_PLAYING_SECONDS = 3.0   # how often Spotify is asked while a song plays (config: poll_seconds)
POLL_IDLE_SECONDS = 6.0      # ...while paused / nothing playing
MAX_DURATION_DIFF = 5        # seconds; reject lyrics for a clearly different version

log = logging.getLogger("lyrics")


# --------------------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


class HttpError(Exception):
    def __init__(self, status, headers, body):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status, self.headers, self.body = status, headers, body


def http(method, url, form=None, headers=None, timeout=10):
    """Tiny JSON-over-HTTP helper. Returns the parsed body (None if empty, e.g. 204)."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.headers, e.read().decode("utf-8", "replace")) from None
    return json.loads(raw) if raw.strip() else None


# --------------------------------------------------------------------------- Spotify

class NeedLogin(Exception):
    pass


class Spotify:
    """Authorization Code flow with PKCE (no client secret needed)."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.tok = load_json(TOKEN_FILE, {})

    @property
    def logged_in(self):
        return bool(self.tok.get("refresh_token"))

    def _store(self, resp):
        self.tok = {
            "access_token": resp["access_token"],
            "refresh_token": resp.get("refresh_token") or self.tok.get("refresh_token"),
            "expires_at": time.time() + resp.get("expires_in", 3600) - 60,
        }
        save_json(TOKEN_FILE, self.tok, private=True)

    def login(self):
        """Open the browser, let the user approve, catch the redirect on 127.0.0.1."""
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        })

        result = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h3>Logged in. You can close this tab.</h3>")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
        server.timeout = 5
        print("Opening your browser to log in to Spotify...")
        print("If nothing opens, visit this URL yourself:\n" + url)
        webbrowser.open(url)
        deadline = time.time() + 300
        while not result and time.time() < deadline:
            server.handle_request()
        server.server_close()

        if result.get("state") != state or "code" not in result:
            raise RuntimeError(f"Spotify login failed or timed out ({result.get('error', 'no code received')}).")

        self._store(http("POST", TOKEN_URL, form={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }))

    def _refresh(self):
        if not self.logged_in:
            raise NeedLogin("Not logged in to Spotify. Run (in the lyrics-widget conda env): python lyrics_widget.py --login")
        try:
            resp = http("POST", TOKEN_URL, form={
                "grant_type": "refresh_token",
                "refresh_token": self.tok["refresh_token"],
                "client_id": self.client_id,
            })
        except HttpError as e:
            if e.status in (400, 401):
                raise NeedLogin("Spotify login expired. Run (in the lyrics-widget conda env): python lyrics_widget.py --login") from None
            raise
        self._store(resp)

    def _auth_header(self):
        if time.time() >= self.tok.get("expires_at", 0):
            self._refresh()
        return {"Authorization": "Bearer " + self.tok["access_token"]}

    def now_playing(self):
        """Return a dict describing the playing track, or None if nothing (track) is playing."""
        for attempt in (1, 2):
            t0 = time.monotonic()
            try:
                data = http("GET", f"{API_URL}/me/player/currently-playing", headers=self._auth_header())
            except HttpError as e:
                if e.status == 401 and attempt == 1:
                    self.tok["expires_at"] = 0  # force a token refresh and retry once
                    continue
                raise
            t1 = time.monotonic()
            break

        if not data or not data.get("item") or data.get("currently_playing_type") != "track":
            return None
        item = data["item"]
        artists = item.get("artists") or [{}]
        return {
            "id": item.get("id") or item.get("uri") or item["name"],
            "title": item["name"],
            "artist": artists[0].get("name", ""),
            "album": (item.get("album") or {}).get("name", ""),
            "duration_ms": item["duration_ms"],
            "progress_ms": data.get("progress_ms") or 0,
            "is_playing": bool(data.get("is_playing")),
            "at": (t0 + t1) / 2,  # monotonic time the progress value refers to (latency-compensated)
        }


# --------------------------------------------------------------------------- lyrics

_LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{2}(?:[.:]\d{1,3})?)\]")


def parse_lrc(text):
    """'[01:23.45] some words' -> [(83450, 'some words'), ...] sorted by time."""
    lines = []
    for raw in text.splitlines():
        stamps = _LRC_STAMP.findall(raw)
        if not stamps:
            continue  # metadata tags like [ar:Artist] or blank lines
        content = _LRC_STAMP.sub("", raw).strip()
        for minutes, seconds in stamps:
            ms = round((int(minutes) * 60 + float(seconds.replace(":", "."))) * 1000)
            lines.append((ms, content))
    lines.sort(key=lambda x: x[0])
    return lines


def _estimate_timing(plain, duration_ms):
    """Fallback for unsynced lyrics: spread the lines evenly over the song (approximate!)."""
    texts = [t.strip() for t in plain.splitlines() if t.strip()]
    if not texts:
        return None
    start, span = duration_ms * 0.05, duration_ms * 0.85
    return [(int(start + span * i / len(texts)), t) for i, t in enumerate(texts)]


def _clean_title(title):
    title = re.sub(r"\s+-\s+.*$", "", title)                 # " - Remastered 2011", " - Live"
    title = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title)    # "(feat. X)", "[Deluxe]"
    return title.strip() or title


def _pick(candidates, duration_s):
    usable = []
    for c in candidates:
        if not isinstance(c, dict) or c.get("instrumental"):
            continue
        if not (c.get("syncedLyrics") or c.get("plainLyrics")):
            continue
        d = c.get("duration")
        if d is not None and abs(d - duration_s) > MAX_DURATION_DIFF:
            continue
        usable.append(c)
    if not usable:
        return None
    # prefer time-synced lyrics, then the closest duration
    return min(usable, key=lambda c: (not c.get("syncedLyrics"), abs((c.get("duration") or duration_s) - duration_s)))


def fetch_lyrics(track):
    """Look the track up on LRCLIB. Returns [(ms, text), ...] or None if no lyrics exist.
    Network problems raise (so the caller can retry later instead of caching 'no lyrics')."""
    duration_s = round(track["duration_ms"] / 1000)
    best = None

    try:
        rec = http("GET", f"{LRCLIB_URL}/get?" + urllib.parse.urlencode({
            "track_name": track["title"],
            "artist_name": track["artist"],
            "album_name": track["album"],
            "duration": duration_s,
        }))
        best = _pick([rec], duration_s)
    except HttpError as e:
        if e.status != 404:
            raise

    if best is None:
        for name in dict.fromkeys([track["title"], _clean_title(track["title"])]):
            try:
                results = http("GET", f"{LRCLIB_URL}/search?" + urllib.parse.urlencode({
                    "track_name": name, "artist_name": track["artist"]}))
            except HttpError as e:
                if e.status == 404:
                    continue
                raise
            best = _pick(results or [], duration_s)
            if best:
                break

    if best is None:
        return None
    if best.get("syncedLyrics"):
        lines = parse_lrc(best["syncedLyrics"])
        if lines:
            return lines
    if best.get("plainLyrics"):
        return _estimate_timing(best["plainLyrics"], track["duration_ms"])
    return None


# --------------------------------------------------------------------------- poller

Snapshot = namedtuple("Snapshot", "track_id progress_ms at lines starts duration_ms expires")


def locate(snap, offset_ms=0, now=None):
    """Which lyric line is current? Returns its index (-1 = before the first line),
    or None when nothing should be shown: no snapshot, the snapshot is stale (no fresh
    answer from Spotify in a while) or the position is already past the end of the song."""
    if snap is None:
        return None
    now = time.monotonic() if now is None else now
    if now > snap.expires:
        return None
    position_ms = snap.progress_ms + (now - snap.at) * 1000 + offset_ms
    if position_ms > snap.duration_ms + 1000:
        return None
    return bisect.bisect_right(snap.starts, position_ms) - 1


def _fmt(seconds):
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    if s >= 60:
        return f"{s // 60} min {s % 60} s"
    return f"{s} s"


class Poller(threading.Thread):
    """Background thread. Keeps `snapshot` up to date: None = show nothing,
    otherwise the lyrics of the playing track plus a reference playback position."""

    def __init__(self, spotify, poll_seconds=POLL_PLAYING_SECONDS):
        super().__init__(daemon=True)
        self.spotify = spotify
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.snapshot = None
        self.fatal = None      # message if the widget must quit (e.g. login revoked)
        self._cache = {}       # track id -> lines, or None if the song has no lyrics

    def run(self):
        wait = self._blocked_remaining()
        if wait > 0:
            log.warning("Spotify asked us to back off earlier; waiting another %s.", _fmt(wait))
            time.sleep(wait)
        failures = 0
        while True:
            try:
                now = self.spotify.now_playing()
                log.debug("poll: %s", {k: now[k] for k in ("title", "is_playing", "progress_ms")}
                          if now else "nothing playing")
                delay = self._update(now)
                failures = 0
            except NeedLogin as e:
                self.fatal = str(e)
                return
            except HttpError as e:
                if e.status == 429:
                    delay = self._rate_limited(e)
                else:
                    failures += 1
                    delay = min(2 ** failures, 30)
                    log.warning("Spotify error %s; retrying in %d s", e, delay)
            except Exception as e:  # offline, odd response, even a bug: log it, never die silently
                failures += 1
                delay = min(2 ** failures, 30)
                log.warning("Polling problem (%s: %s); retrying in %d s", type(e).__name__, e, delay)
            time.sleep(delay)

    def _rate_limited(self, err):
        """Spotify said 429. Honour Retry-After (also across restarts) and say so in the log."""
        try:
            wait = max(1.0, float(err.headers.get("Retry-After", 30)))
        except (TypeError, ValueError, AttributeError):
            wait = 30.0
        reason = None
        try:
            body = json.loads(err.body)
            reason = body.get("reason") or (body.get("error") or {}).get("reason")
        except (ValueError, AttributeError, TypeError):
            pass
        log.warning("Spotify rate limit/quota hit%s. Not asking again for %s.",
                    f" ({reason})" if reason else "", _fmt(wait))
        try:
            save_json(STATE_FILE, {"blocked_until": time.time() + wait})
        except OSError:
            pass
        return wait

    @staticmethod
    def _blocked_remaining():
        try:
            return max(0.0, float(load_json(STATE_FILE, {}).get("blocked_until", 0)) - time.time())
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _playing_delay(self, now):
        """Poll again after poll_seconds, or just after the song is due to end (instant auto-advance)."""
        remaining = (now["duration_ms"] - now["progress_ms"]) / 1000 - (time.monotonic() - now["at"])
        return max(1.0, min(self.poll_seconds, remaining + 0.7))

    def _update(self, now):
        """Apply a now-playing answer. Returns the number of seconds until the next poll."""
        if not now or not now["is_playing"]:
            self.snapshot = None
            return POLL_IDLE_SECONDS
        tid = now["id"]
        if self.snapshot is not None and self.snapshot.track_id != tid:
            self.snapshot = None  # never show the previous song's lyrics while the next loads
        if tid not in self._cache:
            try:
                lyrics = fetch_lyrics(now)
            except HttpError as e:  # LRCLIB trouble must not be mistaken for a Spotify rate limit
                raise OSError(f"LRCLIB error: {e}") from None
            if len(self._cache) > 100:
                self._cache.pop(next(iter(self._cache)))
            self._cache[tid] = lyrics
        lines = self._cache[tid]
        if not lines:
            self.snapshot = None
        else:
            # `expires`: if no fresh answer from Spotify arrives in time, stop showing lyrics
            self.snapshot = Snapshot(tid, now["progress_ms"], now["at"], lines,
                                     [ms for ms, _ in lines], now["duration_ms"],
                                     now["at"] + self.poll_seconds * 3 + 2)
        return self._playing_delay(now)


# --------------------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "bg_color": "#121212",       # window background
    "bg_opacity": 0.72,          # 0 = fully transparent, 1 = solid
    "text_color": "#ffffff",
    "font_family": "",           # empty = system font
    "font_size": 16,             # px; the current line is drawn larger automatically
    "dim_opacity": 0.5,          # opacity of the lines before/after the current one
    "radius": 18,                # corner radius, px
    "width": 900,
    "height": 240,
    "lines_before": 1,
    "lines_after": 2,
    "poll_seconds": POLL_PLAYING_SECONDS,
    "offset_ms": 0,              # positive = lines appear earlier
    "keep_above": True,          # always on top
}

# key -> (min, max, type)
_SETTING_LIMITS = {
    "bg_opacity": (0.0, 1.0, float),
    "dim_opacity": (0.1, 1.0, float),
    "font_size": (8, 72, int),
    "radius": (0, 60, int),
    "width": (300, 2400, int),
    "height": (80, 1200, int),
    "lines_before": (0, 4, int),
    "lines_after": (0, 6, int),
    "poll_seconds": (1.0, 30.0, float),
    "offset_ms": (-5000, 5000, int),
}
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clamp_number(value, default, lo, hi, cast):
    if isinstance(value, bool):
        return default
    try:
        number = cast(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number != number:  # NaN
        return default
    return max(lo, min(hi, number))


def clean_settings(raw=None):
    """Return a complete, validated settings dict: unknown keys are dropped, bad or
    missing values fall back to the defaults, numbers are clamped to sane ranges."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = raw.get(key, default)
        if key in ("bg_color", "text_color"):
            out[key] = value if isinstance(value, str) and _HEX_COLOR.match(value) else default
        elif key == "font_family":
            out[key] = re.sub(r"[^\w .,\-()]", "", value)[:60].strip() if isinstance(value, str) else default
        elif key == "keep_above":
            out[key] = value if isinstance(value, bool) else default
        else:
            lo, hi, cast = _SETTING_LIMITS[key]
            out[key] = _clamp_number(value, default, lo, hi, cast)
    return out


def load_settings():
    return clean_settings(load_json(CONFIG_FILE, {}))


def update_config(**changes):
    """Read-modify-write config.json so keys written elsewhere (client_id, x, y) survive."""
    cfg = load_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.update(changes)
    save_json(CONFIG_FILE, cfg)
    return cfg


def hex_to_rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def build_css(s):
    """GTK CSS for the lyrics window. Scoped by style class so it never touches other windows."""
    r, g, b = hex_to_rgb(s["bg_color"])
    family = f'font-family: "{s["font_family"]}";' if s["font_family"] else ""
    return (
        f"window.lyrics-window {{ background-image: none; background-color: rgba({r}, {g}, {b}, {s['bg_opacity']:.2f}); "
        f"border-radius: {s['radius']}px; }}\n"
        f"label.lyrics-text {{ color: {s['text_color']}; font-size: {s['font_size']}px; "
        f"padding: 16px 32px; {family} }}\n"
    )


def render_markup(lines, idx, before=1, after=2, dim=0.5):
    """Pango markup: `before` dimmed lines, the current line (big, bold), `after` dimmed lines.
    idx == -1 means the song has not reached its first line yet."""
    alpha = f"{round(dim * 100)}%"

    def esc(text):
        return _xml_escape(text) if text else "♪"  # empty line -> music note

    parts = [f'<span alpha="{alpha}">{esc(lines[i][1])}</span>' for i in range(max(0, idx - before), idx)]
    current = lines[idx][1] if idx >= 0 else ""
    parts.append(f'<span size="xx-large" weight="bold">{esc(current)}</span>')
    parts.extend(f'<span alpha="{alpha}">{esc(text)}</span>' for _, text in lines[idx + 1: idx + 1 + after])
    return "\n".join(parts)


# --------------------------------------------------------------------------- running-instance helpers

def write_pid_file():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid_file():
    try:
        if PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def find_running_widget(needle=b"lyrics_widget"):
    """PID of a running widget (from the pid file, double-checked against /proc), or None."""
    try:
        pid = int(PID_FILE.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if needle in cmdline else None
