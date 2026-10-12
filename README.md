# Spotify Lyrics Widget (Ubuntu)

A small borderless window that shows the lyrics of the song Spotify is currently playing, highlighting the current line
in time with the music. It shows nothing else: when nothing is playing (or the song has no lyrics) the window hides
itself.

Start songs from your phone or any other Spotify app. The widget only watches what your account is playing; it never
controls playback.

## How it works

- **Now playing:** the Spotify Web API (`currently-playing`), polled every 3 s. Between polls the position is
  extrapolated locally, so line changes are smooth.
- **Lyrics:** [LRCLIB](https://lrclib.net), a free community database of time-synced lyrics. Spotify's own lyrics
  (Musixmatch) are not available through any public API, so coverage depends on LRCLIB. Obscure songs may have none.
- **No pip packages.** Only the Python standard library plus GTK 3, provided by the conda environment described in
  `environment.yml` (`lyrics-widget`).

## Setup (about 3 minutes)

1. **Install conda** if you don't have it (Miniconda or Miniforge). `install.sh`
   stops with instructions if it can't find conda.
2. **Create a Spotify developer app** at <https://developer.spotify.com/dashboard>:
    - Redirect URI: `http://127.0.0.1:8888/callback` (exactly this)
    - Tick **Web API**, save, and copy the **Client ID**.
    - (Apps in development mode need the owner to have Premium.)
3. **Install** (creates the conda environment `lyrics-widget` from `environment.yml`
   and copies the widget to `~/.local/share/spotify-lyrics-widget`):
   ```bash
   ./install.sh
   ```
4. **Log in once** (asks for the Client ID and opens your browser; the login is stored in
   `~/.config/spotify-lyrics-widget/token.json`):
   ```bash
   conda activate lyrics-widget
   python ~/.local/share/spotify-lyrics-widget/lyrics_widget.py
   ```
5. Run `./install.sh` again to enable autostart at login.
6. Play a song on any device. The widget appears at the bottom centre of the screen.

On another Ubuntu PC: copy this folder there and repeat steps 1-5.

## Using it

- **Drag** with the left mouse button to move it (position is remembered).
- **Right-click** → Settings or Quit.
- `python lyrics_widget.py --settings` opens the settings window (in the running widget; also available as "Lyrics
  Widget Settings" in the app menu, which works even while nothing is playing).
- `python lyrics_widget.py --login` redoes the Spotify login.
- `python lyrics_widget.py --debug` prints every Spotify poll to the terminal (useful if the widget ever seems stuck).
  (Run these inside the environment: `conda activate lyrics-widget`.)

## Start automatically on login

`./install.sh` adds the autostart entry once the Spotify login exists (step 5 above). It starts the widget through the
conda environment (`conda run -n lyrics-widget ...`). Remove autostart with
`rm ~/.config/autostart/spotify-lyrics-widget.desktop`. To apply later code changes to the installed copy, use
`./update.sh`.

## Settings

Right-click the widget → **Settings…** (or run `--settings`). Changes apply live, sample lyrics are shown while the
window is open so you can see the result even when nothing is playing, and everything is saved automatically.

| setting                     | meaning                                                                                                          |
|-----------------------------|------------------------------------------------------------------------------------------------------------------|
| Background color / opacity  | window background; opacity 0-100 %                                                                               |
| Text color, Font, Font size | font size is the base size, the current line is drawn larger                                                     |
| Other lines opacity         | how dim the lines before/after the current one are                                                               |
| Corner radius               | rounded corners, px                                                                                              |
| Width / Height              | window size in px (the height is fixed so the window doesn't jump)                                               |
| Lines before / after        | how many context lines are shown around the current one                                                          |
| Always on top               | keep the widget above other windows                                                                              |
| Ask Spotify every (s)       | polling interval while a song plays (default 3, min 1). Lower = snappier pause/skip detection, more API requests |
| Lyrics timing offset (ms)   | positive = lines appear earlier (e.g. 300 if they feel late)                                                     |
| Client ID / Log in again    | change the Spotify Client ID or redo the login in the browser                                                    |
| Reset to defaults           | restores all of the above except the Client ID                                                                   |

The values live in `~/.config/spotify-lyrics-widget/config.json` (`bg_color`,
`bg_opacity`, `text_color`, `font_family`, `font_size`, `dim_opacity`, `radius`,
`width`, `height`, `lines_before`, `lines_after`, `keep_above`, `poll_seconds`,
`offset_ms`, plus `client_id` and the window position `x`, `y`). Out-of-range or invalid values are replaced by safe
ones when the file is read.

## Notes and limitations

- **Wayland:** GNOME on Wayland ignores "always on top" and window positioning for normal apps, so the widget runs
  through XWayland (`GDK_BACKEND=x11`, set automatically). It needs a compositing desktop for the transparent
  background.
- **Unsynced lyrics:** if LRCLIB only has plain (untimed) lyrics, the lines are spread evenly over the song. This is
  only approximate.
- **Latency:** pausing, seeking or skipping is noticed within about `poll_seconds`
  (3 s by default). When a song ends on its own, the next poll is timed for just after the end, so auto-advance is
  picked up almost immediately.
- **Rate limits:** Spotify limits requests (rolling 30 s window) and development-mode apps also have a quota that
  Spotify doesn't publish numbers for. If Spotify answers
  "429 too many requests", the widget hides, logs a message with the wait time and stays quiet for exactly that long
  (the wait is remembered in `state.json`, so restarting doesn't make it worse). The widget also hides on its own if it
  hasn't had a fresh answer from Spotify for about 11 seconds, instead of guessing. There is no billing: Spotify's Web
  API has no pay-per-request pricing.
- Podcasts and ads are ignored (the widget hides).
- Lyrics are fetched by title/artist/album/duration; remasters and live versions with a very different length are
  rejected so you don't get wrong lyrics.
