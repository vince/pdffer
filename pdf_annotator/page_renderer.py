"""Background page rendering with Poppler."""

import threading

import cairo
from gi.repository import GLib

from .errors import log


class PageRenderer:
    """Renders pages with Poppler on a background thread.

    Only this thread touches the Poppler document after load.
    """

    _running = set()  # renderers whose thread hasn't exited yet

    def __init__(self, poppler_doc, on_done):
        self._doc = poppler_doc
        self._on_done = on_done
        self._cond = threading.Condition()
        self._wanted = []
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="page-renderer")
        PageRenderer._running.add(self)
        self._thread.start()

    @classmethod
    def shutdown_all(cls, timeout=10):
        """Stop every renderer and wait for in-progress renders to finish.

        Must run before the process exits: Poppler's global state is torn
        down by exit handlers, and a render still running then crashes.
        """
        renderers = list(cls._running)
        for r in renderers:
            r.stop()
        for r in renderers:
            r._thread.join(timeout)

    def want(self, items):
        with self._cond:
            if items != self._wanted:
                self._wanted = list(items)
                self._cond.notify()

    def stop(self):
        with self._cond:
            self._alive = False
            self._wanted = []
            self._cond.notify()

    def _run(self):
        try:
            self._loop()
        finally:
            PageRenderer._running.discard(self)

    def _loop(self):
        while True:
            with self._cond:
                while self._alive and not self._wanted:
                    self._cond.wait()
                if not self._alive:
                    return
                item = self._wanted.pop(0)
            index, pw, ph, px_w, px_h = item
            try:
                surf = cairo.ImageSurface(cairo.FORMAT_RGB24, px_w, px_h)
                cr = cairo.Context(surf)
                cr.set_source_rgb(1, 1, 1)
                cr.paint()
                cr.scale(px_w / pw, px_h / ph)
                self._doc.get_page(index).render(cr)
                del cr
                surf.flush()
            except Exception as e:  # keep the thread alive on odd pages
                log.warning("couldn't render page %d at %dx%d px", index + 1, px_w, px_h,
                            exc_info=e)
                surf = None  # reported to on_done as a failure
            GLib.idle_add(self._on_done, self, item, surf)
