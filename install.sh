#!/usr/bin/env bash
# Installs the widget on Ubuntu:
#   1. creates (or updates) the conda environment "lyrics-widget" from environment.yml
#   2. copies the widget to ~/.local/share/spotify-lyrics-widget
#   3. starts it automatically at login (once you have logged in to Spotify)
# Needs conda (Miniconda, Miniforge or Anaconda). Safe to run again at any time.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/.local/share/spotify-lyrics-widget"
autostart="$HOME/.config/autostart"
env_name="lyrics-widget"
env_file="$here/environment.yml"

find_conda() {
    if [ -n "${CONDA_EXE:-}" ] && [ -x "$CONDA_EXE" ]; then
        echo "$CONDA_EXE"
        return 0
    fi
    local c d
    c="$(command -v conda 2>/dev/null || true)"
    if [ -n "$c" ] && [ -x "$c" ]; then
        echo "$c"
        return 0
    fi
    for d in "$HOME/miniconda3" "$HOME/miniforge3" "$HOME/anaconda3" "$HOME/mambaforge" /opt/conda; do
        if [ -x "$d/bin/conda" ]; then
            echo "$d/bin/conda"
            return 0
        fi
    done
    return 1
}

if ! conda_bin="$(find_conda)"; then
    cat >&2 <<'EOF'
conda was not found, so the widget can't be installed.

Install Miniconda or Miniforge first, then open a NEW terminal and run ./install.sh again:
  Miniconda: https://docs.conda.io/projects/miniconda/en/latest/
  Miniforge: https://github.com/conda-forge/miniforge
EOF
    exit 1
fi

if [ ! -f "$env_file" ]; then
    echo "environment.yml not found next to install.sh ($env_file)." >&2
    exit 1
fi

# 1. conda environment
env_exists=0
while read -r name _; do
    if [ "$name" = "$env_name" ]; then
        env_exists=1
    fi
done < <("$conda_bin" env list)

if [ "$env_exists" -eq 1 ]; then
    echo "Updating conda environment '$env_name'..."
    "$conda_bin" env update -n "$env_name" -f "$env_file" --prune
else
    echo "Creating conda environment '$env_name' (this can take a few minutes)..."
    "$conda_bin" env create -n "$env_name" -f "$env_file"
fi

if ! "$conda_bin" run -n "$env_name" python -c \
    "import gi; gi.require_version('Gtk','3.0'); gi.require_version('Gdk','3.0'); from gi.repository import Gtk, Gdk" \
    >/dev/null 2>&1; then
    echo "The environment exists but GTK can't be loaded from it." >&2
    echo "Try: $conda_bin env remove -n $env_name   and then ./install.sh again." >&2
    exit 1
fi

# 2. the widget itself
mkdir -p "$dest" "$autostart"
cp "$here/lyrics_core.py" "$here/lyrics_widget.py" "$dest/"
chmod +x "$dest/lyrics_widget.py"
echo "Copied the widget to $dest"

# 3. autostart (only once the Spotify login exists: an autostarted widget has no terminal to ask in)
if [ -f "$HOME/.config/spotify-lyrics-widget/token.json" ]; then
    cat > "$autostart/spotify-lyrics-widget.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Spotify Lyrics Widget
Comment=Shows lyrics of the song playing on Spotify
Exec="$conda_bin" run --no-capture-output -n $env_name python "$dest/lyrics_widget.py"
X-GNOME-Autostart-enabled=true
EOF
    echo "Autostart enabled: the widget starts at your next login."
    printf 'Start it now in the background with:\n  nohup "%s" run --no-capture-output -n "%s" python "%s/lyrics_widget.py" >/dev/null 2>&1 &\n' \
        "$conda_bin" "$env_name" "$dest"
else
    echo
    echo "One step left: log in to Spotify once (it asks for your Client ID and opens your browser):"
    echo "  conda activate $env_name"
    echo "  python $dest/lyrics_widget.py"
    echo "Then run ./install.sh again to enable autostart."
fi
