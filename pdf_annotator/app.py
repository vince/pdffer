"""Application entry point."""

import logging
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from . import settings  # noqa: E402
from .page_renderer import PageRenderer  # noqa: E402
from .window import APP_NAME, Window, install_css  # noqa: E402

APP_ID = "io.github.vince.PdfAnnotator"
VERSION = "0.1.0"
ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")

ACCELS = {
    "win.open": ["<Control>o"],
    "win.save": ["<Control>s"],
    "win.save-as": ["<Control><Shift>s"],
    "win.undo": ["<Control>z"],
    "win.redo": ["<Control><Shift>z", "<Control>y"],
    "win.zoom-in": ["<Control>plus", "<Control>equal", "<Control>KP_Add"],
    "win.zoom-out": ["<Control>minus", "<Control>KP_Subtract"],
    "win.zoom-fit": ["<Control>0"],
    "win.fullscreen": ["F11"],
    "win.insert-image": ["<Control>i"],
    "win.paste": ["<Control>v"],
    "win.toggle-sidebar": ["F9"],
    "window.close": ["<Control>w"],
    "app.quit": ["<Control>q"],
}


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(ICONS_DIR)
        install_css()
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)
        for name, cb in (("quit", self._quit), ("about", self._about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

    def do_shutdown(self):
        settings.flush()
        PageRenderer.shutdown_all()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()

    def do_open(self, files, _n, _hint):
        for f in files:
            path = f.get_path()
            if not path:
                continue
            win = self.get_active_window()
            if win is None or win.has_document():
                win = Window(self)
            win.present()
            win.open_path(path)

    def _quit(self, *_):
        # close windows one by one so unsaved-changes prompts run
        for win in list(self.get_windows()):
            win.close()

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
            comments="Sign and mark up PDFs with a stylus or keyboard.")
        about.present(self.get_active_window())


def main():
    logging.basicConfig(
        format="pdf-annotator: %(levelname)s: %(message)s",
        level=logging.DEBUG if os.environ.get("PDF_ANNOTATOR_DEBUG") else logging.WARNING)
    return App().run(sys.argv)
