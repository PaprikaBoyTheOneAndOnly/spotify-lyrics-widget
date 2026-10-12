#!/usr/bin/env bash
# Copies the widget's source files from this folder into the installed copy
# (~/.local/share/spotify-lyrics-widget) and restarts the widget if it is running.
# Typical use: apply the changes you were given to the files in this folder, then run ./update.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/.local/share/spotify-lyrics-widget"
desktop="$HOME/.config/autostart/spotify-lyrics-widget.desktop"
files=(lyrics_core.py lyrics_widget.py)

if [ ! -d "$dest" ]; then
    echo "The widget isn't installed yet ($dest is missing). Run ./install.sh first." >&2
    exit 1
fi

changed=0
for f in "${files[@]}"; do
    if [ ! -f "$here/$f" ]; then
        echo "Missing $here/$f" >&2
        exit 1
    fi
    if ! cmp -s "$here/$f" "$dest/$f"; then
        cp "$here/$f" "$dest/$f"
        echo "updated: $f"
        changed=1
    fi
done
chmod +x "$dest/lyrics_widget.py"

if [ "$changed" -eq 0 ]; then
    echo "The installed widget is already up to date."
    exit 0
fi

# Changes only take effect after a restart.
if pgrep -f "$dest/lyrics_widget.py" >/dev/null; then
    pkill -f "$dest/lyrics_widget.py" || true
    sleep 1
    if [ -f "$desktop" ] && exec_line="$(grep -m1 '^Exec=' "$desktop" | cut -d= -f2-)" && [ -n "$exec_line" ]; then
        # same command that starts it at login (so a conda setup keeps working)
        setsid nohup bash -c "$exec_line" >/dev/null 2>&1 &
        echo "Widget restarted."
    else
        echo "Widget stopped. Start it again with: conda activate lyrics-widget && python $dest/lyrics_widget.py &"
    fi
else
    echo "The widget isn't running; the update applies the next time it starts."
fi
