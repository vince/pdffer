"""Pointer tools: what a press, drag and release does on the page.

The canvas owns the view and routes a primary-button drag to the active
tool (or to the eraser when the stylus is flipped to its eraser end). Tools
change the document through its undoable methods, and show work in
progress through the canvas's preview state: `live`, `moving`,
`resizing` and `hidden`.
"""

import math

from . import render, stylus
from .document import Stroke

ERASER_RADIUS_PX = 10
HANDLE_PX = 9  # resize handle size
MIN_IMAGE_PT = 8  # smallest image edge
MIN_STEP_PT = 0.35  # ignore pen movement smaller than this (at 100%)

SELECTION_BLUE = (0.21, 0.52, 0.89)


class Tool:
    cursor = "default"

    def __init__(self, canvas):
        self.canvas = canvas

    def begin(self, wx, wy, hit, gesture):
        """Pointer pressed at widget (wx, wy); hit is (page, x, y) or None.
        Return False to ignore this drag."""
        return True

    def update(self, wx, wy, dx, dy, moved, gesture):
        """Pointer dragged to (wx, wy), (dx, dy) from the start. `moved` is
        true once it has gone past the click threshold."""

    def end(self, moved):
        """Pointer released."""

    def hover(self, wx, wy):
        """Pointer moved without a button pressed; returns a cursor name."""
        return self.cursor

    def draw(self, cr):
        """Draw tool feedback over the pages, in widget coordinates."""

    def deactivate(self):
        """Another tool was chosen."""


class PenTool(Tool):
    cursor = "crosshair"

    def begin(self, wx, wy, hit, gesture):
        if hit is None:  # strokes start on a page
            return False
        c = self.canvas
        pressure, _eraser, _touch = stylus.event_info(gesture)
        page, px, py = hit
        c.live = Stroke(page, [(px, py, pressure)], c.style.color, c.style.width)
        return True

    def update(self, wx, wy, dx, dy, moved, gesture):
        c = self.canvas
        stroke = c.live
        if stroke is None:
            return
        pressure, _eraser, _touch = stylus.event_info(gesture)
        last = stroke.points[-1]
        last_w = c.page_to_widget(stroke.page, last[0], last[1])
        samples = stylus.history_points(gesture, wx, wy, last_w)
        samples.append((wx, wy, pressure))
        for sx, sy, p in samples:
            _page, px, py = c.widget_to_page(sx, sy, stroke.page)
            last = stroke.points[-1]
            if abs(px - last[0]) + abs(py - last[1]) < MIN_STEP_PT / c.zoom:
                continue
            stroke.add_point(px, py, max(0.05, min(1.0, pressure if p is None else p)))
        c.queue_draw()

    def end(self, moved):
        c = self.canvas
        stroke, c.live = c.live, None
        if stroke is not None:
            c.doc.add_mark(stroke)


class EraserTool(Tool):
    """Wipe over marks to remove them; one drag is one undo step."""

    cursor = "none"  # the tool draws its own circle

    def begin(self, wx, wy, hit, gesture):
        self.erased = []
        self.last = (wx, wy)
        self._erase_at(wx, wy)
        return True

    def update(self, wx, wy, dx, dy, moved, gesture):
        c = self.canvas
        lx, ly = self.last
        steps = max(1, int(math.hypot(wx - lx, wy - ly) / (ERASER_RADIUS_PX / 2)))
        for k in range(1, steps + 1):
            self._erase_at(lx + (wx - lx) * k / steps, ly + (wy - ly) * k / steps)
        self.last = (wx, wy)
        c.hover = (wx, wy)
        c.queue_draw()

    def end(self, moved):
        c = self.canvas
        c.hidden.difference_update(id(m) for m in self.erased)
        c.doc.remove_marks(self.erased)

    def _erase_at(self, wx, wy):
        c = self.canvas
        hit = c.widget_to_page(wx, wy)
        if hit is None:
            return
        page, px, py = hit
        r = ERASER_RADIUS_PX / c.zoom
        for m in c.doc.marks:
            if m.page == page and id(m) not in c.hidden and m.hit(px, py, r):
                c.hidden.add(id(m))  # disappears now, removed on release
                self.erased.append(m)

    def hover(self, wx, wy):
        self.canvas.queue_draw()  # move the circle
        return self.cursor

    def draw(self, cr):
        if self.canvas.hover is None:
            return
        hx, hy = self.canvas.hover
        cr.save()
        cr.set_line_width(1.5)
        cr.arc(hx, hy, ERASER_RADIUS_PX, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.stroke_preserve()
        cr.set_dash([3, 3])
        cr.set_source_rgba(0, 0, 0, 0.8)
        cr.stroke()
        cr.restore()


def _drag_move(canvas, mark, dx, dy):
    """Preview moving `mark` by a widget-space drag."""
    canvas.moving = [mark, dx / canvas.zoom, dy / canvas.zoom]
    canvas.queue_draw()


def _commit_move(canvas):
    if canvas.moving is not None:
        mark, mdx, mdy = canvas.moving
        canvas.moving = None
        canvas.doc.move_mark(mark, mdx, mdy)


class TextTool(Tool):
    """Click to type; click existing text to edit it, drag it to move it."""

    cursor = "text"

    def begin(self, wx, wy, hit, gesture):
        self.hit = hit
        self.mark = self.canvas.mark_at(hit, ("text",)) if hit else None
        return True

    def update(self, wx, wy, dx, dy, moved, gesture):
        if self.mark is not None and moved:
            _drag_move(self.canvas, self.mark, dx, dy)

    def end(self, moved):
        c = self.canvas
        if self.mark is not None and c.moving is not None:
            _commit_move(c)
        elif self.mark is not None:
            c.open_editor(self.mark.page, self.mark.x, self.mark.y, self.mark)
        elif self.hit is not None and not moved:
            page, px, py = self.hit
            line_h = render.text_extent("Ag", c.style.text_size)[1]
            c.open_editor(page, px, py - line_h / 2)  # caret centred on the click


class SelectTool(Tool):
    """Select a mark to move it; drag an image's corners to resize it."""

    def begin(self, wx, wy, hit, gesture):
        c = self.canvas
        corner = self._handle_at(wx, wy)
        if corner:
            m = c.selected
            self.resize = (m, corner, (m.x, m.y, m.w, m.h))
            self.mark = None
        else:
            self.resize = None
            self.mark = c.mark_at(hit) if hit else None
            c.select(self.mark)
        return True

    def update(self, wx, wy, dx, dy, moved, gesture):
        c = self.canvas
        if self.resize:
            mark, corner, (x, y, w, h) = self.resize
            _page, px, py = c.widget_to_page(wx, wy, mark.page)
            # keep the aspect ratio, anchored at the opposite corner
            ax = x + w if "w" in corner else x
            ay = y + h if "n" in corner else y
            f = max(abs(px - ax) / w, abs(py - ay) / h, MIN_IMAGE_PT / min(w, h))
            nw, nh = w * f, h * f
            nx = ax - nw if "w" in corner else ax
            ny = ay - nh if "n" in corner else ay
            c.resizing = (mark, (nx, ny, nw, nh))
            c.queue_draw()
        elif self.mark is not None and moved:
            _drag_move(c, self.mark, dx, dy)

    def end(self, moved):
        c = self.canvas
        if self.resize and c.resizing:
            mark, (x, y, w, h) = c.resizing
            c.resizing = None
            c.doc.edit(mark, x=x, y=y, w=w, h=h)
        else:
            _commit_move(c)

    def hover(self, wx, wy):
        corner = self._handle_at(wx, wy)
        if corner:
            return "nwse-resize" if corner in ("nw", "se") else "nesw-resize"
        hit = self.canvas.widget_to_page(wx, wy)
        return "move" if hit and self.canvas.mark_at(hit) else "default"

    def deactivate(self):
        self.canvas.select(None)

    # ---- selection outline and handles

    def _selection_rect(self):
        """Selected mark's rect in widget coordinates, incl. live preview."""
        c = self.canvas
        m = c.selected
        if m is None or id(m) in c.hidden:
            return None
        if c.resizing and c.resizing[0] is m:
            x, y, w, h = c.resizing[1]
            x0, y0, x1, y1 = x, y, x + w, y + h
        else:
            x0, y0, x1, y1 = m.bbox()
            if c.moving and c.moving[0] is m:
                x0, x1 = x0 + c.moving[1], x1 + c.moving[1]
                y0, y1 = y0 + c.moving[2], y1 + c.moving[2]
        ax, ay = c.page_to_widget(m.page, x0, y0)
        bx, by = c.page_to_widget(m.page, x1, y1)
        return ax, ay, bx, by

    def _handles(self):
        """Resize handles of a selected image: [(corner, wx, wy)]."""
        r = self._selection_rect()
        if r is None or self.canvas.selected.kind != "image":
            return []
        ax, ay, bx, by = r
        return [("nw", ax, ay), ("ne", bx, ay), ("sw", ax, by), ("se", bx, by)]

    def _handle_at(self, wx, wy):
        for corner, hx, hy in self._handles():
            if abs(wx - hx) <= HANDLE_PX and abs(wy - hy) <= HANDLE_PX:
                return corner
        return None

    def draw(self, cr):
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
        cr.set_source_rgb(*SELECTION_BLUE)
        cr.stroke()
        cr.set_dash([])
        hs = HANDLE_PX
        for _corner, hx, hy in self._handles():
            cr.rectangle(round(hx - hs / 2) + 0.5, round(hy - hs / 2) + 0.5, hs, hs)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(*SELECTION_BLUE)
            cr.stroke()
        cr.restore()


TOOLS = {"pen": PenTool, "text": TextTool, "eraser": EraserTool, "select": SelectTool}
