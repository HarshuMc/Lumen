#!/usr/bin/env bash
# LUMEN installer - XDG paths + LXQt application menu registration.
#
#   ./install.sh            install for the current user
#   bash install.sh         same
#
# Nothing is installed system wide and no root access is required.

set -euo pipefail

SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
DEST="$DATA_HOME/lumen"
ICON_BASE="$DATA_HOME/icons/hicolor"
DESKTOP_DIR="$DATA_HOME/applications"
EXEC_PATH="$DEST/main.py"

say() { printf '  %s\n' "$*"; }
fail() { printf '\n  LUMEN install: %s\n\n' "$*" >&2; exit 1; }

printf '\n  LUMEN \u2014 Your Memories, Clearly.\n\n'

# ---------------------------------------------------------------- checks
command -v python3 >/dev/null 2>&1 || fail "python3 was not found in PATH"

if ! python3 - 2>/dev/null <<'PY'
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GdkPixbuf
import cairo
PY
then
  fail "GTK 3 bindings are missing. Install them with:

      sudo apt install python3-gi gir1.2-gtk-3.0 python3-gi-cairo"
fi
say "Python + GTK 3 bindings ............ OK"

[[ -f "$SRC_DIR/main.py" ]] || fail "main.py not found next to install.sh"

# ---------------------------------------------------------------- files
mkdir -p "$DEST"
say "Installing application to $DEST"
rm -rf "$DEST/ui" "$DEST/core"
cp -a "$SRC_DIR/main.py" "$SRC_DIR/style.css" "$SRC_DIR/gen_icon.py" "$DEST/"
cp -a "$SRC_DIR/ui" "$SRC_DIR/core" "$DEST/"
find "$DEST" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "$DEST/main.py"
mkdir -p "$CACHE_HOME/lumen/thumbs"

# ---------------------------------------------------------------- icon
say "Rendering the Cairo logo (no image assets needed)"
for SIZE in 512 256 128 64; do
  ICON_DIR="$ICON_BASE/${SIZE}x${SIZE}/apps"
  mkdir -p "$ICON_DIR"
  python3 "$DEST/gen_icon.py" -o "$ICON_DIR/lumen.png" -s "$SIZE" >/dev/null \
    || fail "could not render lumen.png (${SIZE}px)"
done
say "Icon installed to $ICON_BASE/*/apps/lumen.png"

# ---------------------------------------------------------------- launcher
mkdir -p "$DESKTOP_DIR"
if [[ -f "$SRC_DIR/lumen.desktop" ]]; then
  sed -e "s|^Exec=.*|Exec=python3 \"$EXEC_PATH\"|" \
      -e "s|^TryExec=.*||" \
      "$SRC_DIR/lumen.desktop" > "$DESKTOP_DIR/lumen.desktop"
else
  cat > "$DESKTOP_DIR/lumen.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=LUMEN
GenericName=Photo Gallery
Comment=Your Memories, Clearly - Glassy Gallery
Exec=python3 "$EXEC_PATH"
Icon=lumen
Terminal=false
Categories=Graphics;Photography;Viewer;
Keywords=gallery;photos;images;videos;viewer;album;lumen;
StartupNotify=true
StartupWMClass=lumen
EOF
fi
chmod +x "$DESKTOP_DIR/lumen.desktop"
say "Launcher installed to $DESKTOP_DIR/lumen.desktop"

# ---------------------------------------------------------------- caches
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  say "Desktop database refreshed (menu entry is live)"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$ICON_BASE" >/dev/null 2>&1 || true
fi
if command -v xdg-desktop-menu >/dev/null 2>&1; then
  xdg-desktop-menu forceupdate >/dev/null 2>&1 || true
fi
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$DESKTOP_DIR/lumen.desktop" >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- verify
if python3 "$EXEC_PATH" --check >/dev/null 2>&1; then
  say "Self check ......................... OK"
else
  say "Self check reported missing optional pieces (run: python3 \"$EXEC_PATH\" --check)"
fi

printf '\n'
say "LUMEN is installed."
say "Menu:      Graphics \u2192 Viewer \u2192 LUMEN (LXQt application menu)"
say "Launch:    python3 \"$EXEC_PATH\""
say "Scanning:  ~/Pictures  ~/Downloads  ~/DCIM  ~/Documents  ~/Videos"
printf '\n'
