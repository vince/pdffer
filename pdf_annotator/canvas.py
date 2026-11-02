"""Scrollable page view with drawing, text, eraser and select input."""

import bisect
import math
import threading
from dataclasses import dataclass

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gtk  # noqa: E402

from . import render  # noqa: E402
from .errors import log  # noqa: E402
from .document import ImageData, ImageMark, Stroke, TextMark  # noqa: E402

MARGIN = 16
GAP = 14
MIN_ZOOM, MAX_ZOOM = 0.1, 6.0
MAX_RENDER_PIXELS = 16_000_000
ERASER_RADIUS_PX = 10
MOVE_THRESHOLD_PX = 4
HANDLE_PX = 9  # resize handle size
MIN_IMAGE_PT = 8


@dataclass
class Style:
    color: tuple = (0.07, 0.07, 0.07)
    width: float = 1.5  # pen width in points
    text_size: float = 14.0


# ----------------------------------------------------------- page renderer

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


# ---------------------------------------------------------------- canvas

class PageCanvas(Gtk.DrawingArea, Gtk.Scrollable):
    __gtype_name__ = "PdfAnnotPageCanvas"

    hadjustment = GObject.Property(type=Gtk.Adjustment)
    vadjustment = GObject.Property(type=Gtk.Adjustment)
    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy,
                                      default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy,
                                      default=Gtk.ScrollablePolicy.MINIMUM)

    __gsignals__ = {
        "view-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "editor-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, overlay):
        super().__init__()
        self.overlay = overlay  # Gtk.Overlay hosting the text editor
        self.set_focusable(True)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)
        self.connect("resize", lambda *a: self._relayout())
        self.connect("notify::hadjustment", self._on_adj_set)
        self.connect("notify::vadjustment", self._on_adj_set)
        self.connect("notify::scale-factor", lambda *a: self.queue_draw())

        self.doc = None
        self.zoom = 1.0
        self.fit_width = True
        self.tool = "pen"
        self.style = Style()

        self._rects = []  # page rects in content coordinates (x, y, w, h)
        self._tops = []
        self._content_w = self._content_h = 0
        self._cache = {}  # page -> (key, surface)
        self._overlays = {}  # page -> (key, surface): finished marks, pre-rendered
        self._failed = set()  # render keys that failed; not retried
        self._renderer = None

        self._live = None  # Stroke being drawn
        self._hidden = set()  # ids of marks hidden (being erased/edited)
        self._moving = None  # [mark, dx, dy]
        self._resizing = None  # (mark, (x, y, w, h)) preview
        self.selected = None  # mark selected with the select tool
        self._press = None  # state for the current drag
        self._hover = None  # pointer position for eraser cursor
        self.editor = None

        self._setup_input()

    # ---------------------------------------------------------- document

    def set_document(self, doc):
        self.commit_editor()
        if self._renderer:
            self._renderer.stop()
            self._renderer = None
        self.doc = doc
        self._cache.clear()
        self._overlays.clear()
        self._failed.clear()
        self._hidden.clear()
        self._live = self._moving = self._resizing = self._press = None
        self.selected = None
        if doc is not None:
            self._renderer = PageRenderer(doc.poppler, self._on_rendered)
            self.fit_width = True
        self._relayout()
        if self.vadjustment:
            self.vadjustment.set_value(0)
        self.queue_draw()

    def refresh(self):
        if self.selected is not None and self.doc and self.selected not in self.doc.marks:
            self.select(None)
        self.queue_draw()

    def select(self, mark):
        if mark is not self.selected:
            self.selected = mark
            self.emit("selection-changed")
            self.queue_draw()

    def delete_selected(self):
        if self.selected is not None:
            mark = self.selected
            self.select(None)
            self.doc.remove_marks([mark])

    def insert_image(self, data, at=None):
        """Place an image (raw file bytes) on the page under widget point
        `at`, or centred on the visible part of the current page.

        Raises document.ImageError for unreadable images.
        """
        if not self.doc:
            return None
        img = ImageData(data)
        hit = self.widget_to_page(*at) if at else None
        if hit:
            index, cx, cy = hit
        else:
            index = self.current_page()
            # centre of the visible part of the page
            p = self.doc.pages[index]
            _, top, _ = self.widget_to_page(0, 0, index)
            _, _, bottom = self.widget_to_page(0, self.get_height(), index)
            top, bottom = max(0, top), min(p.height, bottom)
            cx, cy = p.width / 2, (top + bottom) / 2 if bottom > top else p.height / 2
        page = self.doc.pages[index]
        # natural size at 96 dpi, limited to 40% of the page
        w, h = img.width * 0.75, img.height * 0.75
        f = min(1.0, page.width * 0.4 / w, page.height * 0.4 / h)
        w, h = max(MIN_IMAGE_PT, w * f), max(MIN_IMAGE_PT, h * f)
        mark = ImageMark(index, cx - w / 2, cy - h / 2, w, h, img)
        self.doc.add_mark(mark)
        self.select(mark)
        return mark

    # ---------------------------------------------------------- layout

    def _on_adj_set(self, *_):
        for adj in (self.hadjustment, self.vadjustment):
            if adj is not None and not getattr(adj, "_pdfannot_hooked", False):
                adj.connect("value-changed", self._on_scrolled)
                adj._pdfannot_hooked = True
        self._relayout()

    def _on_scrolled(self, *_):
        self.queue_draw()
        if self.editor:
            self.editor.update_geometry()
        self.emit("view-changed")

    def _fit_zoom(self):
        if not self.doc:
            return 1.0
        avail = max(100, self.get_width() - 2 * MARGIN)
        maxw = max(p.width for p in self.doc.pages)
        return max(MIN_ZOOM, min(MAX_ZOOM, avail / maxw))

    def _relayout(self):
        if self.doc and self.fit_width:
            self.zoom = self._fit_zoom()
        self._rects, self._tops = [], []
        view_w = self.get_width()
        if self.doc:
            z = self.zoom
            content_w = max(p.width for p in self.doc.pages) * z + 2 * MARGIN
            total_w = max(content_w, view_w)
            y = MARGIN
            for p in self.doc.pages:
                w, h = p.width * z, p.height * z
                x = round((total_w - w) / 2)
                self._rects.append((x, round(y), w, h))
                self._tops.append(round(y))
                y += h + GAP
            self._content_w = content_w
            self._content_h = y - GAP + MARGIN
        else:
            self._content_w = self._content_h = 0
        self._configure_adjustments()
        if self.editor:
            self.editor.update_geometry()
        self.queue_draw()
        self.emit("view-changed")

    def _configure_adjustments(self):
        w, h = self.get_width(), self.get_height()
        for adj, total, size in ((self.hadjustment, self._content_w, w),
                                 (self.vadjustment, self._content_h, h)):
            if adj is None:
                continue
            upper = max(total, size)
            adj.configure(
                value=max(0, min(adj.get_value(), upper - size)),
                lower=0, upper=upper,
                step_increment=size * 0.1, page_increment=size * 0.9,
                page_size=size,
            )

    def _offset(self):
        hx = self.hadjustment.get_value() if self.hadjustment else 0
        vy = self.vadjustment.get_value() if self.vadjustment else 0
        return hx, vy

    def visible_pages(self):
        if not self._rects:
            return range(0)
        _, oy = self._offset()
        h = self.get_height()
        first = max(0, bisect.bisect_right(self._tops, oy) - 1)
        last = first
        while last + 1 < len(self._rects) and self._rects[last + 1][1] < oy + h:
            last += 1
        return range(first, last + 1)

    def current_page(self):
        """Page index most prominently on screen."""
        vis = self.visible_pages()
        if not vis:
            return 0
        _, oy = self._offset()
        mid = oy + self.get_height() / 3
        for i in vis:
            x, y, w, h = self._rects[i]
            if y + h + GAP > mid:
                return i
        return vis[-1]

    def widget_to_page(self, wx, wy, index=None):
        """Map widget coordinates to (page, x_pt, y_pt).

        If index is None, returns the page under the point (or None).
        """
        ox, oy = self._offset()
        cx, cy = wx + ox, wy + oy
        if index is None:
            i = bisect.bisect_right(self._tops, cy) - 1
            if i < 0:
                return None
            x, y, w, h = self._rects[i]
            if not (x <= cx <= x + w and y <= cy <= y + h):
                return None
            index = i
        x, y, _w, _h = self._rects[index]
        return index, (cx - x) / self.zoom, (cy - y) / self.zoom

    def page_to_widget(self, index, px, py):
        ox, oy = self._offset()
        x, y, _w, _h = self._rects[index]
        return x + px * self.zoom - ox, y + py * self.zoom - oy

    # ---------------------------------------------------------- zoom

    def set_zoom(self, zoom, anchor=None):
        if not self.doc:
            return
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if anchor is None:
            anchor = (self.get_width() / 2, self.get_height() / 2)
        ax, ay = anchor
        # remember the page point under the anchor
        ox, oy = self._offset()
        cy = ay + oy
        i = max(0, min(len(self._rects) - 1, bisect.bisect_right(self._tops, cy) - 1))
        _, px, py = self.widget_to_page(ax, ay, i)
        self.fit_width = False
        self.zoom = zoom
        self._relayout()
        x, y, _w, _h = self._rects[i]
        if self.hadjustment:
            self.hadjustment.set_value(x + px * zoom - ax)
        if self.vadjustment:
            self.vadjustment.set_value(y + py * zoom - ay)

    def zoom_by(self, factor, anchor=None):
        self.set_zoom(self.zoom * factor, anchor)

    def zoom_fit_width(self):
        if not self.doc:
            return
        page = self.current_page()
        self.fit_width = True
        self._relayout()
        self.scroll_to_page(page)

    def scroll_to_page(self, index):
        if self.vadjustment and 0 <= index < len(self._rects):
            self.vadjustment.set_value(self._rects[index][1] - MARGIN)

    # ---------------------------------------------------------- rendering

    def _scale(self):
        native = self.get_native()
        if native is not None:
            surface = native.get_surface()
            if surface is not None and hasattr(surface, "get_scale"):
                return surface.get_scale()
        return self.get_scale_factor()

    def _render_key(self, index):
        p = self.doc.pages[index]
        s = self._scale()
        pw = max(1, round(p.width * self.zoom * s))
        ph = max(1, round(p.height * self.zoom * s))
        if pw * ph > MAX_RENDER_PIXELS:
            f = math.sqrt(MAX_RENDER_PIXELS / (pw * ph))
            pw, ph = max(1, int(pw * f)), max(1, int(ph * f))
        return (index, p.width, p.height, pw, ph)

    def _on_rendered(self, renderer, key, surf):
        if renderer is not self._renderer:
            return False
        index = key[0]
        if surf is None:
            self._failed.add(key)
            self.queue_draw()
            return False
        current = self._render_key(index) if self.doc else None
        if key == current or index not in self._cache:
            self._cache[index] = (key, surf)
            self.queue_draw()
        return False

    def _draw(self, _area, cr, width, height):
        dark = Adw.StyleManager.get_default().get_dark()
        if dark:
            cr.set_source_rgb(0.14, 0.14, 0.15)
        else:
            cr.set_source_rgb(0.86, 0.86, 0.87)
        cr.paint()
        if not self.doc:
            return

        ox, oy = self._offset()
        vis = self.visible_pages()
        by_page = {}
        for m in self.doc.marks:
            if id(m) not in self._hidden:
                by_page.setdefault(m.page, []).append(m)

        wanted = []
        for i in vis:
            x, y, w, h = self._rects[i]
            sx, sy = x - ox, y - oy
            # drop shadow
            cr.set_source_rgba(0, 0, 0, 0.18)
            cr.rectangle(sx + 1, sy + 2, w, h)
            cr.fill()
            key = self._render_key(i)
            cached = self._cache.get(i)
            if cached is None and key in self._failed:
                self._draw_failed_page(cr, sx, sy, w, h)
            elif cached is None:
                cr.set_source_rgb(1, 1, 1)
                cr.rectangle(sx, sy, w, h)
                cr.fill()
            else:
                ckey, surf = cached
                cr.save()
                cr.rectangle(sx, sy, w, h)
                cr.clip()
                cr.translate(sx, sy)
                cr.scale(w / surf.get_width(), h / surf.get_height())
                cr.set_source_surface(surf, 0, 0)
                # A render at the current zoom maps ~1:1 to screen pixels, so
                # the (much slower) smoothing filter is only needed for stale
                # renders being scaled during zoom.
                cr.get_source().set_filter(
                    cairo.FILTER_FAST if ckey == key else cairo.FILTER_GOOD)
                cr.paint()
                cr.restore()
            if (cached is None or cached[0] != key) and key not in self._failed:
                wanted.append(key)

            # Finished marks come from a cached layer; only what is changing
            # right now (stroke being drawn, mark being moved) is drawn live.
            moving = self._moving[0] if self._moving else None
            resizing = self._resizing[0] if self._resizing else None
            marks = by_page.get(i, ())
            static = [m for m in marks if m is not moving and m is not resizing]
            dynamic = [m for m in marks if m is moving or m is resizing]
            live = self._live if self._live and self._live.page == i else None
            if static or dynamic or live:
                cr.save()
                cr.rectangle(sx, sy, w, h)
                cr.clip()
                if static:
                    self._paint_overlay(cr, i, static, sx, sy, w, h)
                cr.translate(sx, sy)
                cr.scale(self.zoom, self.zoom)
                for m in dynamic:
                    if m is moving:
                        render.draw_mark(cr, m, self._moving[1], self._moving[2])
                    else:
                        render.draw_image(cr, m, self._resizing[1])
                if live:
                    render.draw_mark(cr, live)
                cr.restore()

        self._draw_selection(cr)

        # eraser cursor
        if self._hover and self.tool == "eraser":
            hx, hy = self._hover
            cr.set_line_width(1.5)
            cr.arc(hx, hy, ERASER_RADIUS_PX, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.stroke_preserve()
            cr.set_dash([3, 3])
            cr.set_source_rgba(0, 0, 0, 0.8)
            cr.stroke()

        # prefetch neighbours, drop far-away pages
        if vis:
            for j in (vis[-1] + 1, vis[0] - 1):
                if 0 <= j < len(self._rects):
                    k = self._render_key(j)
                    c = self._cache.get(j)
                    if (c is None or c[0] != k) and k not in self._failed:
                        wanted.append(k)
            lo, hi = vis[0] - 3, vis[-1] + 3
            for j in [j for j in self._cache if j < lo or j > hi]:
                del self._cache[j]
        for j in [j for j in self._overlays if j not in vis]:
            del self._overlays[j]
        if self._renderer:
            self._renderer.want(wanted)

    def _paint_overlay(self, cr, index, marks, sx, sy, w, h):
        """Paint the page's finished marks from a cached transparent layer,
        re-rendering it only when something it depends on changed."""
        s = self._scale()
        pw, ph = math.ceil(w * s), math.ceil(h * s)
        if pw * ph > MAX_RENDER_PIXELS:  # extreme zoom: draw directly
            cr.save()
            cr.translate(sx, sy)
            cr.scale(self.zoom, self.zoom)
            for m in marks:
                render.draw_mark(cr, m)
            cr.restore()
            return
        key = (self.zoom, s, pw, ph, self.doc.version, frozenset(self._hidden),
               tuple(id(m) for m in marks))
        cached = self._overlays.get(index)
        if cached is None or cached[0] != key:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
            surf.set_device_scale(s, s)
            ocr = cairo.Context(surf)
            ocr.scale(self.zoom, self.zoom)
            for m in marks:
                render.draw_mark(ocr, m)
            del ocr
            self._overlays[index] = cached = (key, surf)
        cr.set_source_surface(cached[1], sx, sy)
        cr.paint()

    @staticmethod
    def _draw_failed_page(cr, x, y, w, h):
        cr.save()
        cr.set_source_rgb(0.94, 0.94, 0.94)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.rectangle(x, y, w, h)
        cr.clip()
        layout = render.make_layout(cr, "This page couldn't be displayed.", 15)
        _ink, logical = layout.get_pixel_extents()
        cr.move_to(x + (w - logical.width) / 2, y + (h - logical.height) / 2)
        cr.set_source_rgb(0.4, 0.4, 0.4)
        render.PangoCairo.show_layout(cr, layout)
        cr.restore()

    def _selection_rect(self):
        """Selected mark's rect in widget coordinates (incl. live preview)."""
        m = self.selected
        if m is None or self.tool != "select" or id(m) in self._hidden:
            return None
        if self._resizing and self._resizing[0] is m:
            x, y, w, h = self._resizing[1]
            x0, y0, x1, y1 = x, y, x + w, y + h
        else:
            x0, y0, x1, y1 = m.bbox()
            if self._moving and self._moving[0] is m:
                x0 += self._moving[1]
                x1 += self._moving[1]
                y0 += self._moving[2]
                y1 += self._moving[2]
        ax, ay = self.page_to_widget(m.page, x0, y0)
        bx, by = self.page_to_widget(m.page, x1, y1)
        return ax, ay, bx, by

    def _handles(self):
        """Resize handles of the selected image: [(corner, wx, wy)]."""
        r = self._selection_rect()
        if r is None or self.selected.kind != "image":
            return []
        ax, ay, bx, by = r
        return [("nw", ax, ay), ("ne", bx, ay), ("sw", ax, by), ("se", bx, by)]

    def _draw_selection(self, cr):
        r = self._selection_rect()
        if r is None:
            return
        ax, ay, bx, by = r
        cr.save()
        cr.set_line_width(1)
        cr.rectangle(round(ax) - 2.5, round(ay) - 2.5, round(bx - ax) + 5, round(by - ay) + 5)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.stroke_preserve()
        cr.set_dash([4, 3])
        cr.set_source_rgb(0.21, 0.52, 0.89)
        cr.stroke()
        cr.set_dash([])
        hs = HANDLE_PX
        for _c, hx, hy in self._handles():
            cr.rectangle(round(hx - hs / 2) + 0.5, round(hy - hs / 2) + 0.5, hs, hs)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(0.21, 0.52, 0.89)
            cr.stroke()
        cr.restore()

    def _handle_at(self, wx, wy):
        for corner, hx, hy in self._handles():
            if abs(wx - hx) <= HANDLE_PX and abs(wy - hy) <= HANDLE_PX:
                return corner
        return None

    def _mark_at(self, hit, kinds=None):
        """Topmost mark under a page point."""
        i, px, py = hit
        for m in reversed(self.doc.marks):
            if m.page == i and (kinds is None or m.kind in kinds) and \
                    m.hit(px, py, 4 / self.zoom):
                return m
        return None

    # ---------------------------------------------------------- input

    def _setup_input(self):
        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        pan = Gtk.GestureDrag()
        pan.set_button(Gdk.BUTTON_MIDDLE)
        pan.connect("drag-begin", self._on_pan_begin)
        pan.connect("drag-update", self._on_pan_update)
        self.add_controller(pan)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._on_pinch_begin)
        zoom.connect("scale-changed", self._on_pinch)
        self.add_controller(zoom)

        self._update_cursor()

    def set_tool(self, tool):
        if tool != self.tool:
            self.commit_editor()
            if tool != "select":
                self.select(None)
            self.tool = tool
            self._update_cursor()
            self.queue_draw()

    def _update_cursor(self):
        name = {"pen": "crosshair", "text": "text", "eraser": "none"}.get(self.tool, "default")
        self._cursor_name = name
        self.set_cursor(Gdk.Cursor.new_from_name(name, None))

    @staticmethod
    def _event_info(gesture):
        """(pressure, is_eraser_tip, is_touch) for the gesture's current event."""
        ev = gesture.get_current_event()
        pressure, eraser, touch = render.DEFAULT_PRESSURE, False, False
        if ev is None:
            return pressure, eraser, touch
        dev = ev.get_device()
        if dev is not None and dev.get_source() == Gdk.InputSource.TOUCHSCREEN:
            touch = True
        tool = ev.get_device_tool()
        if tool is not None:
            eraser = tool.get_tool_type() == Gdk.DeviceToolType.ERASER
            ok, p = ev.get_axis(Gdk.AxisUse.PRESSURE)
            if ok:
                pressure = max(0.05, min(1.0, p))
        return pressure, eraser, touch

    def _history_points(self, gesture, wx, wy, last):
        """Intermediate stylus samples (widget coords) for smoother strokes.

        Samples GTK coalesced since the previous motion event. They must lie
        between the previous point `last` and the current point (wx, wy);
        if any sample doesn't (some drivers/bindings report zeroed axes),
        the whole history is discarded rather than drawing a stray line.
        """
        ev = gesture.get_current_event()
        if ev is None or ev.get_event_type() != Gdk.EventType.MOTION_NOTIFY:
            return []
        try:
            hist = ev.get_history()
            if not hist:
                return []
            ok, ex, ey = ev.get_position()
            if not ok:
                return []
            offx, offy = wx - ex, wy - ey
            lx, ly = last
            pad = 24
            x0, x1 = min(lx, wx) - pad, max(lx, wx) + pad
            y0, y1 = min(ly, wy) - pad, max(ly, wy) + pad
            need = Gdk.AxisFlags.X | Gdk.AxisFlags.Y
            out = []
            for tc in hist:
                if (tc.flags & need) != need:
                    return []
                axes = tc.axes
                hx = axes[Gdk.AxisUse.X] + offx
                hy = axes[Gdk.AxisUse.Y] + offy
                if not (x0 <= hx <= x1 and y0 <= hy <= y1):
                    log.debug("discarding stylus history: (%.1f,%.1f) outside "
                              "last=%s cur=(%.1f,%.1f)", hx, hy, last, wx, wy)
                    return []
                p = None
                if tc.flags & Gdk.AxisFlags.PRESSURE:
                    p = axes[Gdk.AxisUse.PRESSURE]
                    if p <= 0:
                        p = None
                out.append((hx, hy, p))
            return out
        except Exception:
            return []

    def _on_drag_begin(self, gesture, wx, wy):
        if not self.doc:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        pressure, eraser_tip, touch = self._event_info(gesture)
        if touch:
            # fingers scroll; the ScrolledWindow handles panning
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        if self.editor:
            self.commit_editor()
        self.grab_focus()
        tool = "eraser" if eraser_tip else self.tool
        hit = self.widget_to_page(wx, wy)
        self._press = {"tool": tool, "start": (wx, wy), "hit": hit, "moved": False}

        if tool == "pen":
            if hit is None:
                gesture.set_state(Gtk.EventSequenceState.DENIED)
                self._press = None
                return
            i, px, py = hit
            self._live = Stroke(i, [(px, py, pressure)], self.style.color, self.style.width)
        elif tool == "eraser":
            self._press["erased"] = []
            self._press["last"] = (wx, wy)
            self._erase_at(wx, wy)
        elif tool == "text":
            mark = self._mark_at(hit, ("text",)) if hit else None
            self._press["mark"] = mark
        elif tool == "select":
            corner = self._handle_at(wx, wy)
            if corner:
                m = self.selected
                self._press["resize"] = (m, corner, (m.x, m.y, m.w, m.h))
            else:
                mark = self._mark_at(hit) if hit else None
                self.select(mark)
                self._press["mark"] = mark
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.queue_draw()

    def _on_drag_update(self, gesture, dx, dy):
        st = self._press
        if st is None:
            return
        sx, sy = st["start"]
        wx, wy = sx + dx, sy + dy
        if math.hypot(dx, dy) > MOVE_THRESHOLD_PX:
            st["moved"] = True
        tool = st["tool"]
        if tool == "pen" and self._live:
            pressure, _e, _t = self._event_info(gesture)
            last = self._live.points[-1]
            last_w = self.page_to_widget(self._live.page, last[0], last[1])
            samples = self._history_points(gesture, wx, wy, last_w)
            samples.append((wx, wy, pressure))
            pts = self._live.points
            for hx, hy, p in samples:
                _i, px, py = self.widget_to_page(hx, hy, self._live.page)
                if p is None:
                    p = pressure
                last = pts[-1]
                if abs(px - last[0]) + abs(py - last[1]) < 0.35 / self.zoom:
                    continue
                self._live.add_point(px, py, max(0.05, min(1.0, p)))
            self.queue_draw()
        elif tool == "eraser":
            lx, ly = st["last"]
            dist = math.hypot(wx - lx, wy - ly)
            steps = max(1, int(dist / (ERASER_RADIUS_PX / 2)))
            for k in range(1, steps + 1):
                self._erase_at(lx + (wx - lx) * k / steps, ly + (wy - ly) * k / steps)
            st["last"] = (wx, wy)
            self._hover = (wx, wy)
            self.queue_draw()
        elif tool == "select" and st.get("resize"):
            mark, corner, (x, y, w, h) = st["resize"]
            _i, px, py = self.widget_to_page(wx, wy, mark.page)
            # keep aspect ratio, anchored at the opposite corner
            ax = x + w if "w" in corner else x
            ay = y + h if "n" in corner else y
            f = max(abs(px - ax) / w, abs(py - ay) / h, MIN_IMAGE_PT / min(w, h))
            nw, nh = w * f, h * f
            nx = ax - nw if "w" in corner else ax
            ny = ay - nh if "n" in corner else ay
            self._resizing = (mark, (nx, ny, nw, nh))
            self.queue_draw()
        elif tool in ("text", "select") and st.get("mark") is not None and st["moved"]:
            z = self.zoom
            self._moving = [st["mark"], dx / z, dy / z]
            self.queue_draw()

    def _on_drag_end(self, gesture, dx, dy):
        st, self._press = self._press, None
        if st is None:
            return
        tool = st["tool"]
        if tool == "pen" and self._live:
            stroke, self._live = self._live, None
            self.doc.add_mark(stroke)
        elif tool == "eraser":
            erased = st["erased"]
            self._hidden.difference_update(id(m) for m in erased)
            self.doc.remove_marks(erased)
        elif tool == "select":
            if st.get("resize") and self._resizing:
                mark, (x, y, w, h) = self._resizing
                self._resizing = None
                self.doc.edit(mark, x=x, y=y, w=w, h=h)
            elif self._moving is not None:
                mark, mdx, mdy = self._moving
                self._moving = None
                self.doc.move_mark(mark, mdx, mdy)
        elif tool == "text":
            mark = st.get("mark")
            if mark is not None and self._moving is not None:
                _m, mdx, mdy = self._moving
                self._moving = None
                self.doc.move_mark(mark, mdx, mdy)
            elif mark is not None:
                self.open_editor(mark.page, mark.x, mark.y, mark)
            elif st["hit"] is not None and not st["moved"]:
                i, px, py = st["hit"]
                line_h = render.text_extent("Ag", self.style.text_size)[1]
                self.open_editor(i, px, py - line_h / 2)
        self.queue_draw()

    def _erase_at(self, wx, wy):
        hit = self.widget_to_page(wx, wy)
        if hit is None:
            return
        i, px, py = hit
        r = ERASER_RADIUS_PX / self.zoom
        erased = self._press["erased"]
        for m in self.doc.marks:
            if m.page == i and id(m) not in self._hidden and m.hit(px, py, r):
                self._hidden.add(id(m))
                erased.append(m)

    def _on_pan_begin(self, gesture, x, y):
        self._pan_start = self._offset()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_pan_update(self, gesture, dx, dy):
        hx, vy = self._pan_start
        if self.hadjustment:
            self.hadjustment.set_value(hx - dx)
        if self.vadjustment:
            self.vadjustment.set_value(vy - dy)

    def _on_motion(self, _ctl, x, y):
        self._hover = (x, y)
        if self.tool == "eraser":
            self.queue_draw()
        elif self.tool == "select" and self.doc and self._press is None:
            corner = self._handle_at(x, y)
            if corner:
                name = "nwse-resize" if corner in ("nw", "se") else "nesw-resize"
            else:
                hit = self.widget_to_page(x, y)
                name = "move" if hit and self._mark_at(hit) else "default"
            if name != self._cursor_name:
                self._cursor_name = name
                self.set_cursor(Gdk.Cursor.new_from_name(name, None))

    def _on_leave(self, _ctl):
        self._hover = None
        self.queue_draw()

    def _on_scroll(self, ctl, dx, dy):
        state = ctl.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK) or not self.doc:
            return False
        if ctl.get_unit() == Gdk.ScrollUnit.SURFACE:
            factor = math.exp(-dy * 0.01)
        else:
            factor = 1.15 ** (-dy)
        anchor = self._hover or None
        self.zoom_by(factor, anchor)
        return True

    def _on_pinch_begin(self, gesture, _seq):
        self._pinch_zoom = self.zoom

    def _on_pinch(self, gesture, scale):
        ok, cx, cy = gesture.get_bounding_box_center()
        self.set_zoom(self._pinch_zoom * scale, (cx, cy) if ok else None)

    # ---------------------------------------------------------- text editor

    def open_editor(self, page, x, y, mark=None):
        self.commit_editor()
        if mark is not None:
            self._hidden.add(id(mark))
        self.editor = TextEditor(self, page, x, y, mark)
        self.queue_draw()
        self.emit("editor-changed")

    def commit_editor(self, cancel=False):
        ed, self.editor = self.editor, None
        if ed is None:
            return
        text = ed.text().rstrip()
        ed.destroy()
        if ed.mark is not None:
            self._hidden.discard(id(ed.mark))
        if not cancel:
            if ed.mark is None:
                if text.strip():
                    self.doc.add_mark(TextMark(ed.page, ed.x, ed.y, text, ed.color, ed.size))
            elif not text.strip():
                self.doc.remove_marks([ed.mark])
            else:
                self.doc.edit_text(ed.mark, text=text, color=ed.color, size=ed.size)
        self.queue_draw()
        self.grab_focus()
        self.emit("editor-changed")

    def style_changed(self, what):
        """The user changed 'color' or 'text_size'; apply it to open text."""
        if self.editor:
            if what == "color":
                self.editor.color = self.style.color
            elif what == "text_size":
                self.editor.size = self.style.text_size
            self.editor.update_geometry()


class TextEditor:
    """An in-place text box floating over the page.

    The Gtk.TextView is created once per canvas and only shown and hidden,
    never destroyed: destroying a just-focused TextView lets GTK's Wayland
    input-method code use the freed widget when the compositor's reply
    arrives (SIGSEGV in gtk_im_context_wayland_get_global).
    """

    _css = None

    @staticmethod
    def _view(canvas):
        tv = getattr(canvas, "_text_view", None)
        if tv is None:
            tv = Gtk.TextView(visible=False)
            tv.add_css_class("annot-editor")
            tv.set_halign(Gtk.Align.START)
            tv.set_valign(Gtk.Align.START)
            tv.set_accepts_tab(False)
            tv.set_left_margin(0)
            tv.set_right_margin(0)
            tv.set_top_margin(0)
            tv.set_bottom_margin(0)
            tv.get_buffer().set_enable_undo(True)
            tv.get_buffer().connect(
                "changed", lambda *_: canvas.editor and canvas.editor.update_geometry())
            key = Gtk.EventControllerKey()
            key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            key.connect("key-pressed",
                        lambda _c, kv, code, st: bool(canvas.editor) and
                        canvas.editor._on_key(_c, kv, code, st))
            tv.add_controller(key)
            canvas.overlay.add_overlay(tv)
            canvas._text_view = tv
        return tv

    def __init__(self, canvas, page, x, y, mark):
        self.canvas = canvas
        self.page, self.x, self.y, self.mark = page, x, y, mark
        self.color = mark.color if mark else canvas.style.color
        self.size = mark.size if mark else canvas.style.text_size
        self.tv = tv = self._view(canvas)
        buf = tv.get_buffer()
        buf.begin_irreversible_action()  # undo shouldn't go past the start
        buf.set_text(mark.text if mark else "")
        buf.end_irreversible_action()
        self.update_geometry()
        tv.set_visible(True)
        tv.grab_focus()
        buf.place_cursor(buf.get_end_iter())

    @classmethod
    def _provider(cls):
        if cls._css is None:
            cls._css = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), cls._css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        return cls._css

    def update_geometry(self):
        c = self.canvas
        z = c.zoom
        r, g, b = (int(v * 255) for v in self.color)
        css_key = (r, g, b, round(self.size * z, 2))
        if css_key != getattr(self, "_css_key", None):  # restyling is costly
            self._css_key = css_key
            self._provider().load_from_string(
                "textview.annot-editor, textview.annot-editor text {"
                " background: transparent; padding: 0; margin: 0;"
                f" font-family: {render.FONT_FAMILY}; font-size: {self.size * z:.2f}px;"
                f" color: rgb({r},{g},{b}); caret-color: rgb({r},{g},{b}); }}"
                "textview.annot-editor { outline: 1px dashed alpha(currentColor, 0.6);"
                " outline-offset: 2px; }")
        wx, wy = c.page_to_widget(self.page, self.x, self.y)
        ok, pt = c.compute_point(c.overlay, Graphene.Point().init(wx, wy))
        if ok:
            wx, wy = pt.x, pt.y
        self.tv.set_margin_start(max(0, round(wx)))
        self.tv.set_margin_top(max(0, round(wy)))
        # Size from the same Pango measurement used for the final render
        # (TextView's own natural size isn't reliable inside an overlay).
        w, h = render.text_extent(self.text() + " ", self.size)
        self.tv.set_size_request(round((w + self.size * 0.5) * z), round(h * z) + 1)

    def _on_key(self, _ctl, keyval, _code, state):
        shift = state & Gdk.ModifierType.SHIFT_MASK
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not shift:
            self.canvas.commit_editor()
            return True
        if keyval == Gdk.KEY_Escape:
            self.canvas.commit_editor(cancel=True)
            return True
        return False

    def text(self):
        buf = self.tv.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def undo(self):
        buf = self.tv.get_buffer()
        if buf.get_can_undo():
            buf.undo()

    def redo(self):
        buf = self.tv.get_buffer()
        if buf.get_can_redo():
            buf.redo()

    def destroy(self):
        self.canvas.grab_focus()  # focus out first so the input method lets go
        self.tv.set_visible(False)
