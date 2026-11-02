"""Helpers for GUI tests: run the real app, wait for conditions, and
simulate pointer input.

Each run uses a fresh, non-unique application instance (so it never talks
to a copy of PDF Annotator you have open) and a throwaway settings file.
"""

import io
import json
import math
import os
import sys
import tempfile
import threading
import time
import unittest

import cairo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

HAS_DISPLAY = bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
needs_display = unittest.skipUnless(HAS_DISPLAY, "needs a display (use xvfb-run in CI)")

WAIT_TIMEOUT = 15  # seconds, per wait_for step
RUN_TIMEOUT = 90  # seconds, per app run


def make_pdf(path, pages=3, size=(612, 792)):
    """A simple PDF: light pages with a big page number, so renders are
    distinguishable from blank."""
    buf = io.BytesIO()
    s = cairo.PDFSurface(buf, *size)
    for n in range(pages):
        cr = cairo.Context(s)
        cr.set_source_rgb(0.97, 0.97, 0.92)
        cr.paint()
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.set_font_size(60)
        cr.move_to(size[0] / 2 - 40, size[1] / 2)
        cr.show_text(str(n + 1))
        cr.show_page()
    s.finish()
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


def png_bytes(w=60, h=30, rgba=(0, 0.6, 0, 1)):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    cr.set_source_rgba(*rgba)
    cr.paint()
    out = io.BytesIO()
    surf.write_to_png(out)
    return out.getvalue()


class wait_for:
    """Yield this from a test step to wait until `predicate()` is true."""

    def __init__(self, predicate, what):
        self.predicate, self.what = predicate, what


def run_app(steps, prefs=None):
    """Run `steps(win)` (a generator) inside a fresh app with one window.

    The generator may yield a number (milliseconds to wait) or a wait_for.
    Any exception it raises (including failed assertions) is re-raised
    here. Returns the settings dict as saved at shutdown.
    """
    from pdf_annotator import settings
    from pdf_annotator.app import App
    from pdf_annotator.window import Window

    cfg = tempfile.mkdtemp(prefix="pdfannot-test-")
    settings._PATH = os.path.join(cfg, "settings.json")
    settings._pending = None
    if prefs:
        with open(settings._PATH, "w") as fh:
            json.dump(prefs, fh)

    app = App()
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    outcome = {}

    def finish():
        for w in app.get_windows():
            doc = getattr(w, "doc", None)
            if doc is not None:
                doc.mark_saved(doc._marker())  # no "save changes?" prompt
        app.quit()

    def advance():
        try:
            step = next(gen)
        except StopIteration:
            finish()
            return False
        except BaseException as e:
            outcome["error"] = e
            finish()
            return False
        if isinstance(step, wait_for):
            deadline = time.monotonic() + WAIT_TIMEOUT

            def poll():
                try:
                    done = step.predicate()
                except BaseException as e:
                    outcome["error"] = e
                    finish()
                    return False
                if done:
                    GLib.idle_add(advance)
                    return False
                if time.monotonic() > deadline:
                    outcome["error"] = AssertionError(f"timed out waiting for {step.what}")
                    finish()
                    return False
                return True
            GLib.timeout_add(20, poll)
        else:
            GLib.timeout_add(int(step), advance)
        return False

    def on_activate(_app):
        nonlocal gen
        win = Window(app)
        win.present()
        gen = steps(win)
        GLib.idle_add(advance)

    gen = None
    app.connect("activate", on_activate)

    def overall_timeout():
        outcome.setdefault("error", AssertionError("GUI test exceeded run timeout"))
        finish()
        return False
    GLib.timeout_add_seconds(RUN_TIMEOUT, overall_timeout)
    app.run([])

    # Regression check for the exit crash: no render thread may outlive
    # app shutdown.
    alive = [t.name for t in threading.enumerate() if t.name == "page-renderer"]
    if "error" not in outcome and alive:
        outcome["error"] = AssertionError(f"render threads alive after quit: {alive}")
    if "error" in outcome:
        raise outcome["error"]
    settings.flush()
    try:
        with open(settings._PATH) as fh:
            return json.load(fh)
    except OSError:
        return {}


# ---------------------------------------------------------------- input

class FakeGesture:
    """Stands in for Gtk.GestureDrag when calling the canvas's handlers."""

    def get_current_event(self):
        return None

    def set_state(self, _state):
        pass


def drag(canvas, page, start, end, steps=12):
    """Drag in page coordinates (points) from start to end on `page`."""
    g = FakeGesture()
    sx, sy = canvas.page_to_widget(page, *start)
    ex, ey = canvas.page_to_widget(page, *end)
    canvas._on_drag_begin(g, sx, sy)
    for k in range(1, steps + 1):
        canvas._on_drag_update(g, (ex - sx) * k / steps, (ey - sy) * k / steps)
    canvas._on_drag_end(g, ex - sx, ey - sy)


def click(canvas, page, point):
    drag(canvas, page, point, point, steps=1)


# ---------------------------------------------------------------- output

def render_canvas(canvas):
    """Draw the canvas into an image surface exactly as on screen."""
    s = canvas._scale()
    w, h = canvas.get_width(), canvas.get_height()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, math.ceil(w * s), math.ceil(h * s))
    surf.set_device_scale(s, s)
    canvas._draw(None, cairo.Context(surf), w, h)
    surf.flush()
    return surf


def canvas_pixel(canvas, surf, page, point):
    """(r, g, b) of the rendered canvas at a page point."""
    wx, wy = canvas.page_to_widget(page, *point)
    s = canvas._scale()
    x, y = int(wx * s), int(wy * s)
    data = surf.get_data()
    off = y * surf.get_stride() + x * 4
    b, g, r = data[off], data[off + 1], data[off + 2]
    return r, g, b


def page_rendered(canvas, index):
    cached = canvas._cache.get(index)
    return cached is not None and cached[0] == canvas._render_key(index)


def find_widget(widget, cls):
    if isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = find_widget(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None
