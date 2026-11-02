"""The scrollable page view.

The canvas lays out and draws pages (rendered in the background by
PageRenderer) and the marks on them, handles scrolling and zoom, and routes
pointer input to the active tool (tools.py). It also hosts the in-place
text editor (text_editor.py).
"""

import math
from dataclasses import dataclass

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GObject, Gtk  # noqa: E402

from . import render, stylus  # noqa: E402
from .document import ImageData, ImageMark, TextMark  # noqa: E402
from .layout import MARGIN, PageLayout, fit_width_zoom  # noqa: E402
from .page_renderer import PageRenderer  # noqa: E402
from .text_editor import TextEditor  # noqa: E402
from .tools import MIN_IMAGE_PT, TOOLS  # noqa: E402

MIN_ZOOM, MAX_ZOOM = 0.1, 6.0
MAX_RENDER_PIXELS = 16_000_000  # cap per page bitmap; beyond it, upscale
MOVE_THRESHOLD_PX = 4  # a press that moves less than this is a click


@dataclass
class Style:
    color: tuple = (0.07, 0.07, 0.07)
    width: float = 1.5  # pen width in points
    text_size: float = 14.0


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
        self.style = Style()
        self.layout = PageLayout()

        self._cache = {}  # page -> (render key, surface)
        self._overlays = {}  # page -> (key, surface): finished marks, pre-rendered
        self._failed = set()  # render keys that failed; not retried
        self._renderer = None

        # Work in progress, set by tools and drawn live on top of the pages:
        self.live = None  # Stroke being drawn
        self.moving = None  # [mark, dx, dy]: mark being dragged
        self.resizing = None  # (mark, (x, y, w, h)): image being resized
        self.hidden = set()  # ids of marks not to draw (being erased/edited)
        self.hover = None  # pointer position, widget coordinates
        self.selected = None  # mark selected with the select tool
        self.editor = None  # open TextEditor

        self.tools = {name: cls(self) for name, cls in TOOLS.items()}
        self.tool = "pen"
        self._drag = None  # (tool, start, moved) for the current drag
        self._cursor_name = None
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
        self.hidden.clear()
        self.live = self.moving = self.resizing = self._drag = None
        self.selected = None
        self.layout = PageLayout()  # don't anchor to the previous document
        if doc is not None:
            self._renderer = PageRenderer(doc.poppler, self._on_rendered)
            self.fit_width = True
        self._relayout()
        if self.vadjustment:
            self.vadjustment.set_value(0)
        self.queue_draw()

    def refresh(self):
        """The document changed (edit, undo...)."""
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

    def mark_at(self, hit, kinds=None):
        """Topmost mark under a page point (page, x, y)."""
        page, px, py = hit
        for m in reversed(self.doc.marks):
            if m.page == page and (kinds is None or m.kind in kinds) and \
                    m.hit(px, py, 4 / self.zoom):
                return m
        return None

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
            _, _, top = self.widget_to_page(0, 0, index)
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

    def _relayout(self):
        old = self.layout
        # Keep the same spot of the same page at the top of the view when
        # the layout changes (window resized, drawer opened, fit-width zoom
        # recomputed); a raw pixel offset would land on a different page.
        anchor = old.anchor(self.vadjustment.get_value()) \
            if self.doc and len(old) and self.vadjustment else None
        sizes = [(p.width, p.height) for p in self.doc.pages] if self.doc else []
        if sizes and self.fit_width:
            self.zoom = fit_width_zoom(sizes, self.get_width(), MIN_ZOOM, MAX_ZOOM)
        self.layout = PageLayout(sizes, self.zoom, self.get_width())
        self._configure_adjustments()
        if anchor is not None:
            self.vadjustment.set_value(self.layout.resolve(anchor))
        if self.editor:
            self.editor.update_geometry()
        self.queue_draw()
        self.emit("view-changed")

    def _configure_adjustments(self):
        w, h = self.get_width(), self.get_height()
        for adj, total, size in ((self.hadjustment, self.layout.width, w),
                                 (self.vadjustment, self.layout.height, h)):
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
        """Scroll position: content coordinates of the widget's top-left."""
        hx = self.hadjustment.get_value() if self.hadjustment else 0
        vy = self.vadjustment.get_value() if self.vadjustment else 0
        return hx, vy

    def visible_pages(self):
        return self.layout.visible(self._offset()[1], self.get_height())

    def current_page(self):
        """Page index most prominently on screen."""
        return self.layout.current(self._offset()[1], self.get_height())

    def widget_to_page(self, wx, wy, index=None):
        """Map widget coordinates to (page, x_pt, y_pt).

        If index is None, returns the page under the point (or None).
        """
        ox, oy = self._offset()
        cx, cy = wx + ox, wy + oy
        if index is None:
            index = self.layout.page_at(cx, cy)
            if index is None:
                return None
        return (index, *self.layout.to_page(index, cx, cy))

    def page_to_widget(self, index, px, py):
        ox, oy = self._offset()
        cx, cy = self.layout.to_content(index, px, py)
        return cx - ox, cy - oy

    # ---------------------------------------------------------- zoom

    def set_zoom(self, zoom, anchor=None):
        """Zoom, keeping the page point under widget point `anchor` (default:
        the centre of the view) in place."""
        if not self.doc:
            return
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if anchor is None:
            anchor = (self.get_width() / 2, self.get_height() / 2)
        ax, ay = anchor
        index = self.layout.index_at(ay + self._offset()[1])
        _, px, py = self.widget_to_page(ax, ay, index)
        self.fit_width = False
        self.zoom = zoom
        self._relayout()
        cx, cy = self.layout.to_content(index, px, py)
        if self.hadjustment:
            self.hadjustment.set_value(cx - ax)
        if self.vadjustment:
            self.vadjustment.set_value(cy - ay)

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
        if self.vadjustment and 0 <= index < len(self.layout):
            self.vadjustment.set_value(self.layout.rects[index][1] - MARGIN)

    # ---------------------------------------------------------- rendering

    def _scale(self):
        """Device pixels per widget pixel (may be fractional)."""
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
        if Adw.StyleManager.get_default().get_dark():
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
            if id(m) not in self.hidden:
                by_page.setdefault(m.page, []).append(m)

        wanted = []
        for i in vis:
            x, y, w, h = self.layout.rects[i]
            sx, sy = x - ox, y - oy
            key = self._draw_page(cr, i, sx, sy, w, h)
            cached = self._cache.get(i)
            if (cached is None or cached[0] != key) and key not in self._failed:
                wanted.append(key)
            self._draw_marks(cr, i, by_page.get(i, ()), sx, sy, w, h)

        self.tools[self.tool].draw(cr)

        # prefetch neighbours, drop far-away pages
        if vis:
            for j in (vis[-1] + 1, vis[0] - 1):
                if 0 <= j < len(self.layout):
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

    def _draw_page(self, cr, index, sx, sy, w, h):
        """Draw a page's rendered bitmap (or a stand-in); returns the render
        key it should have at the current zoom."""
        cr.set_source_rgba(0, 0, 0, 0.18)  # drop shadow
        cr.rectangle(sx + 1, sy + 2, w, h)
        cr.fill()
        key = self._render_key(index)
        cached = self._cache.get(index)
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
            # A render at the current zoom maps ~1:1 to screen pixels, so the
            # (much slower) smoothing filter is only needed for stale renders
            # being scaled during zoom.
            cr.get_source().set_filter(cairo.FILTER_FAST if ckey == key else cairo.FILTER_GOOD)
            cr.paint()
            cr.restore()
        return key

    def _draw_marks(self, cr, index, marks, sx, sy, w, h):
        # Finished marks come from a cached layer; only what is changing
        # right now (stroke being drawn, mark being moved) is drawn live.
        moving = self.moving[0] if self.moving else None
        resizing = self.resizing[0] if self.resizing else None
        static = [m for m in marks if m is not moving and m is not resizing]
        dynamic = [m for m in marks if m is moving or m is resizing]
        live = self.live if self.live and self.live.page == index else None
        if not (static or dynamic or live):
            return
        cr.save()
        cr.rectangle(sx, sy, w, h)
        cr.clip()
        if static:
            self._paint_overlay(cr, index, static, sx, sy, w, h)
        cr.translate(sx, sy)
        cr.scale(self.zoom, self.zoom)
        for m in dynamic:
            if m is moving:
                render.draw_mark(cr, m, self.moving[1], self.moving[2])
            else:
                render.draw_image(cr, m, self.resizing[1])
        if live:
            render.draw_mark(cr, live)
        cr.restore()

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
        key = (self.zoom, s, pw, ph, self.doc.version, frozenset(self.hidden),
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

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._on_pinch_begin)
        zoom.connect("scale-changed", self._on_pinch)
        self.add_controller(zoom)

        self._set_cursor(self.tools[self.tool].cursor)

    def set_tool(self, tool):
        if tool != self.tool:
            self.commit_editor()
            self.tools[self.tool].deactivate()
            self.tool = tool
            self._set_cursor(self.tools[tool].cursor)
            self.queue_draw()

    def _set_cursor(self, name):
        if name != self._cursor_name:
            self._cursor_name = name
            self.set_cursor(Gdk.Cursor.new_from_name(name, None))

    def _on_drag_begin(self, gesture, wx, wy):
        if not self.doc:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        _pressure, eraser_tip, touch = stylus.event_info(gesture)
        if touch:
            # fingers scroll; the ScrolledWindow handles panning
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        if self.editor:
            self.commit_editor()
        self.grab_focus()
        tool = self.tools["eraser" if eraser_tip else self.tool]
        if not tool.begin(wx, wy, self.widget_to_page(wx, wy), gesture):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._drag = [tool, (wx, wy), False]
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.queue_draw()

    def _on_drag_update(self, gesture, dx, dy):
        if self._drag is None:
            return
        tool, (sx, sy), moved = self._drag
        if not moved and math.hypot(dx, dy) > MOVE_THRESHOLD_PX:
            self._drag[2] = moved = True
        tool.update(sx + dx, sy + dy, dx, dy, moved, gesture)

    def _on_drag_end(self, gesture, dx, dy):
        drag, self._drag = self._drag, None
        if drag is not None:
            tool, _start, moved = drag
            tool.end(moved)
        self.queue_draw()

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
        self.hover = (x, y)
        if self.doc and self._drag is None:
            self._set_cursor(self.tools[self.tool].hover(x, y))

    def _on_leave(self, _ctl):
        self.hover = None
        self.queue_draw()

    def _on_scroll(self, ctl, dx, dy):
        state = ctl.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK) or not self.doc:
            return False
        if ctl.get_unit() == Gdk.ScrollUnit.SURFACE:
            factor = math.exp(-dy * 0.01)
        else:
            factor = 1.15 ** (-dy)
        self.zoom_by(factor, self.hover)
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
            self.hidden.add(id(mark))
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
            self.hidden.discard(id(ed.mark))
        if not cancel:
            if ed.mark is None:
                if text.strip():
                    self.doc.add_mark(TextMark(ed.page, ed.x, ed.y, text, ed.color, ed.size))
            elif not text.strip():
                self.doc.remove_marks([ed.mark])
            else:
                self.doc.edit(ed.mark, text=text, color=ed.color, size=ed.size)
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
