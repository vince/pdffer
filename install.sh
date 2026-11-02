#!/usr/bin/env bash
# Install PDF Annotator for the current user (~/.local). Use --uninstall to remove.
set -euo pipefail

APP_ID=io.github.vince.PdfAnnotator
PREFIX="${PREFIX:-$HOME/.local}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$PREFIX/share/pdf-annotator"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -rf "$DEST" "$PREFIX/bin/pdf-annotator" "$APPS/$APP_ID.desktop" \
        "$ICONS/scalable/apps/$APP_ID.svg"
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

rm -rf "$DEST"
mkdir -p "$DEST" "$PREFIX/bin" "$APPS" "$ICONS/scalable/apps"
cp -r "$SRC/pdf_annotator" "$SRC/pdf-annotator" "$DEST/"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
ln -sf "$DEST/pdf-annotator" "$PREFIX/bin/pdf-annotator"
sed "s|^Exec=.*|Exec=$PREFIX/bin/pdf-annotator %F|" \
    "$SRC/data/$APP_ID.desktop" > "$APPS/$APP_ID.desktop"
install -m644 "$SRC/pdf_annotator/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$ICONS/scalable/apps/$APP_ID.svg"
update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -q -t -f "$ICONS" 2>/dev/null || true

echo "Installed. Launch \"PDF Annotator\" from Activities, or run: pdf-annotator file.pdf"
echo "To make it your default PDF app:"
echo "  xdg-mime default $APP_ID.desktop application/pdf"
