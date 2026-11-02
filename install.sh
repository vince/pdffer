#!/usr/bin/env bash
# Install PDFfer for the current user (~/.local). Use --uninstall to remove.
set -euo pipefail

APP_ID=io.github.vince.PDFfer
PREFIX="${PREFIX:-$HOME/.local}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$PREFIX/share/pdffer"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor"
# Before 0.1.0 the app was called "PDF Annotator"; its files are removed on
# install and uninstall.
OLD_ID=io.github.vince.PdfAnnotator
OLD_FILES=("$PREFIX/share/pdf-annotator" "$PREFIX/bin/pdf-annotator"
           "$APPS/$OLD_ID.desktop" "$ICONS/scalable/apps/$OLD_ID.svg")

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -rf "$DEST" "$PREFIX/bin/pdffer" "$APPS/$APP_ID.desktop" \
        "$ICONS/scalable/apps/$APP_ID.svg" "${OLD_FILES[@]}"
    update-desktop-database "$APPS" 2>/dev/null || true
    echo "Uninstalled."
    exit 0
fi

if ! python3 - <<'PY' 2>/dev/null
import gi
for ns, v in (("Gtk", "4.0"), ("Adw", "1"), ("Poppler", "0.18")):
    gi.require_version(ns, v)
from gi.repository import Gtk, Adw, Poppler  # noqa
import cairo, pikepdf  # noqa
PY
then
    echo "Missing dependencies. On Fedora run:"
    echo "  sudo dnf install python3-gobject gtk4 libadwaita poppler-glib python3-cairo python3-pikepdf python3-cryptography"
    exit 1
fi

# Keep PDFfer as the default PDF app if the old name was.
WAS_DEFAULT=$([[ "$(xdg-mime query default application/pdf 2>/dev/null)" == "$OLD_ID.desktop" ]] \
              && echo yes || true)
rm -rf "$DEST" "${OLD_FILES[@]}"
mkdir -p "$DEST" "$PREFIX/bin" "$APPS" "$ICONS/scalable/apps"
cp -r "$SRC/pdffer" "$SRC/bin" "$DEST/"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
ln -sf "$DEST/bin/pdffer" "$PREFIX/bin/pdffer"
sed "s|^Exec=.*|Exec=$PREFIX/bin/pdffer %F|" \
    "$SRC/data/$APP_ID.desktop" > "$APPS/$APP_ID.desktop"
install -m644 "$SRC/pdffer/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$ICONS/scalable/apps/$APP_ID.svg"
update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -q -t -f "$ICONS" 2>/dev/null || true
if [[ -n "$WAS_DEFAULT" ]]; then
    xdg-mime default "$APP_ID.desktop" application/pdf
fi

echo "Installed. Launch \"PDFfer\" from Activities, or run: pdffer file.pdf"
echo "To make it your default PDF app:"
echo "  xdg-mime default $APP_ID.desktop application/pdf"
