"""Drawing of annotation marks with cairo.

The same functions draw marks on screen and into the PDF appearance
streams, so what you see is what gets saved.
"""

import math

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

FONT_FAMILY = "Sans"
DEFAULT_PRESSURE = 0.5


def pressure_factor(p):
    """Map stylus pressure (0..1) to a width multiplier (0.5..1.5)."""
    return 0.5 + max(0.0, min(1.0, p))


def _font_options():
    fo = cairo.FontOptions()
    # Unhinted metrics keep text layout identical at every zoom level
    # and in the PDF output.
    fo.set_hint_metrics(cairo.HINT_METRICS_OFF)
    fo.set_hint_style(cairo.HINT_STYLE_NONE)
    return fo


def make_layout(cr, text, size):
    layout = PangoCairo.create_layout(cr)
    PangoCairo.context_set_font_options(layout.get_context(), _font_options())
    layout.context_changed()
    desc = Pango.FontDescription.from_string(FONT_FAMILY)
    desc.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    return layout


_measure_cr = None


def text_extent(text, size):
    """Logical (width, height) of text in points. Main thread only."""
    global _measure_cr
    if _measure_cr is None:
        _measure_cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    layout = make_layout(_measure_cr, text or " ", size)
    _ink, logical = layout.get_extents()
    return logical.width / Pango.SCALE, logical.height / Pango.SCALE


def draw_text(cr, x, y, text, color, size):
    cr.save()
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, make_layout(cr, text, size))
    cr.restore()


def _mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)


def _quad_to(cr, p0, c, p2):
    cr.curve_to(
        p0[0] + 2 / 3 * (c[0] - p0[0]), p0[1] + 2 / 3 * (c[1] - p0[1]),
        p2[0] + 2 / 3 * (c[0] - p2[0]), p2[1] + 2 / 3 * (c[1] - p2[1]),
        p2[0], p2[1],
    )


def draw_stroke(cr, points, color, width):
    """Draw a smoothed, optionally pressure-sensitive ink stroke.

    points: sequence of (x, y, pressure).
    """
    if not points:
        return
    cr.save()
    cr.set_source_rgb(*color)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)

    n = len(points)
    if n == 1:
        x, y, p = points[0]
        cr.arc(x, y, width * pressure_factor(p) / 2, 0, 2 * math.pi)
        cr.fill()
        cr.restore()
        return

    p0 = points[0][2]
    uniform = all(abs(pt[2] - p0) < 0.03 for pt in points)

    if uniform:
        cr.set_line_width(width * pressure_factor(p0))
        cr.move_to(points[0][0], points[0][1])
        if n == 2:
            cr.line_to(points[1][0], points[1][1])
        else:
            for i in range(1, n - 1):
                m = _mid(points[i], points[i + 1])
                cur = cr.get_current_point()
                _quad_to(cr, (cur[0], cur[1]), points[i], m)
            cr.line_to(points[-1][0], points[-1][1])
        cr.stroke()
    else:
        # Variable width: stroke each smoothed piece with its own width;
        # round caps hide the joins.
        start = points[0]
        for i in range(1, n):
            if i < n - 1:
                end = _mid(points[i], points[i + 1])
                cr.set_line_width(width * pressure_factor(points[i][2]))
                cr.move_to(start[0], start[1])
                _quad_to(cr, start, points[i], end)
            else:
                end = points[-1]
                cr.set_line_width(width * pressure_factor((start[2] + end[2]) / 2))
                cr.move_to(start[0], start[1])
                cr.line_to(end[0], end[1])
            cr.stroke()
            start = end
    cr.restore()


def draw_image(cr, mark, rect=None, export=False):
    """Draw an ImageMark into rect (x, y, w, h), defaulting to its own."""
    x, y, w, h = rect or (mark.x, mark.y, mark.w, mark.h)
    img = mark.image
    surf = img.new_surface() if export else img.surface()
    cr.save()
    cr.translate(x, y)
    cr.scale(w / img.width, h / img.height)
    cr.set_source_surface(surf, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_GOOD)
    cr.paint()
    cr.restore()


def draw_mark(cr, mark, dx=0.0, dy=0.0, export=False):
    """Draw a Stroke, TextMark or ImageMark (see document.py), optionally
    offset. export=True uses thread-safe resources for PDF output."""
    if mark.kind == "image":
        draw_image(cr, mark, (mark.x + dx, mark.y + dy, mark.w, mark.h), export)
    elif mark.kind == "ink":
        if dx or dy:
            pts = [(x + dx, y + dy, p) for x, y, p in mark.points]
        else:
            pts = mark.points
        draw_stroke(cr, pts, mark.color, mark.width)
    else:
        draw_text(cr, mark.x + dx, mark.y + dy, mark.text, mark.color, mark.size)
