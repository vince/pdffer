"""Reading stylus input: pressure, the eraser tip, and coalesced samples."""

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

from . import render  # noqa: E402
from .errors import log  # noqa: E402


def event_info(gesture):
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


def history_points(gesture, wx, wy, last):
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
