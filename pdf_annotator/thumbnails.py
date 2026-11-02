"""Sidebar listing page thumbnails (with marks), like macOS Preview."""

import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Poppler", "0.18")
from gi.repository import GLib, Gtk, Poppler  # noqa: E402

from . import render  # noqa: E402
from .canvas import PageRenderer  # noqa: E402

THUMB_W = 116
MAX_CACHED = 240


class _Thumb(Gtk.DrawingArea):
    def __init__(self, panel):
        super().__init__(halign=Gtk.Align.CENTER)
        self.panel = panel
        self.index = -1
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, w, h):
        panel = self.panel
        doc = panel.doc
        if doc is None or not 0 <= self.index < len(doc.pages):
            return
        page = doc.pages[self.index]
        surf = panel.thumbs.get(self.index)
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if surf is not None:
            cr.save()
            cr.scale(w / surf.get_width(), h / surf.get_height())
            cr.set_source_surface(surf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
            cr.restore()
        marks = [m for m in doc.marks if m.page == self.index]
        if marks:
            cr.save()
            cr.rectangle(0, 0, w, h)
            cr.clip()
            cr.scale(w / page.width, h / page.height)
            for m in marks:
                render.draw_mark(cr, m)
            cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.stroke()


class ThumbnailPanel(Gtk.Box):
    def __init__(self, canvas):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.canvas = canvas
        self.doc = None
        self.thumbs = {}  # page index -> cairo surface
        self._bound = {}  # page index -> _Thumb
        self._renderer = None
        self._request_id = 0
        self._syncing = False
        self._picked_at = 0.0

        self.model = Gtk.StringList()
        self.selection = Gtk.SingleSelection(model=self.model, autoselect=False,
                                             can_unselect=True)
        self.selection.connect("notify::selected", self._on_selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        self.list = Gtk.ListView(model=self.selection, factory=factory)
        self.list.add_css_class("navigation-sidebar")
        scrolled = Gtk.ScrolledWindow(child=self.list, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(scrolled)

    # ---- document

    def set_document(self, doc):
        if self._renderer:
            self._renderer.stop()
            self._renderer = None
        self.doc = doc
        self.thumbs.clear()
        n = len(doc.pages) if doc else 0
        self._syncing = True
        self.model.splice(0, self.model.get_n_items(), [str(i + 1) for i in range(n)])
        self.selection.set_selected(0 if n else Gtk.INVALID_LIST_POSITION)
        self._syncing = False
        if doc is not None:
            # A separate Poppler document: each renderer thread owns its own.
            pdoc = Poppler.Document.new_from_bytes(doc.base_gbytes, None)
            self._renderer = PageRenderer(pdoc, self._on_rendered)
            self._request()

    def refresh(self):
        """Marks changed: redraw visible thumbnails."""
        for t in self._bound.values():
            t.queue_draw()

    def sync_current(self, index):
        """Follow the page shown in the main view."""
        if not self.doc or time.monotonic() - self._picked_at < 0.6:
            return
        if self.selection.get_selected() != index:
            self._syncing = True
            self.selection.set_selected(index)
            self._syncing = False
            if self.get_mapped():
                self.list.scroll_to(index, Gtk.ListScrollFlags.NONE, None)

    def _on_selected(self, sel, _pspec):
        if self._syncing:
            return
        i = sel.get_selected()
        if i != Gtk.INVALID_LIST_POSITION:
            self._picked_at = time.monotonic()
            self.canvas.scroll_to_page(i)

    # ---- list items

    def _setup(self, _factory, item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=6, margin_bottom=4)
        thumb = _Thumb(self)
        label = Gtk.Label(css_classes=["caption", "dim-label"])
        box.append(thumb)
        box.append(label)
        item.set_child(box)
        item.thumb, item.label = thumb, label

    def _bind(self, _factory, item):
        index = int(item.get_item().get_string()) - 1
        thumb = item.thumb
        thumb.index = index
        page = self.doc.pages[index]
        h = min(THUMB_W * 2, round(THUMB_W * page.height / page.width))
        w = round(h * page.width / page.height)
        thumb.set_content_width(w)
        thumb.set_content_height(h)
        item.label.set_text(str(index + 1))
        self._bound[index] = thumb
        thumb.queue_draw()
        self._request()

    def _unbind(self, _factory, item):
        thumb = item.thumb
        if self._bound.get(thumb.index) is thumb:
            del self._bound[thumb.index]
        thumb.index = -1

    # ---- rendering

    def _key(self, index):
        page = self.doc.pages[index]
        s = self.get_scale_factor()
        h = min(THUMB_W * 2, THUMB_W * page.height / page.width)
        w = h * page.width / page.height
        return (index, page.width, page.height, max(1, round(w * s)), max(1, round(h * s)))

    def _request(self):
        if not self._request_id:
            self._request_id = GLib.idle_add(self._do_request)

    def _do_request(self):
        self._request_id = 0
        if self._renderer and self.doc:
            self._renderer.want([self._key(i) for i in sorted(self._bound)
                                 if i not in self.thumbs])
        return False

    def _on_rendered(self, renderer, key, surf):
        if renderer is not self._renderer:
            return False
        index = key[0]
        self.thumbs[index] = surf
        if len(self.thumbs) > MAX_CACHED:
            for i in [i for i in self.thumbs if i not in self._bound][:len(self.thumbs) // 3]:
                del self.thumbs[i]
        t = self._bound.get(index)
        if t is not None:
            t.queue_draw()
        return False
