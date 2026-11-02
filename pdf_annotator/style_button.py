"""The header bar's color-dot button and its popover: color swatches, pen
width and text size."""

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk

COLORS = [
    ("black", (0.07, 0.07, 0.07), "Black"),
    ("blue", (0.11, 0.31, 0.85), "Blue"),
    ("red", (0.82, 0.11, 0.11), "Red"),
    ("green", (0.12, 0.54, 0.23), "Green"),
    ("purple", (0.48, 0.17, 0.75), "Purple"),
    ("orange", (0.91, 0.35, 0.05), "Orange"),
]


def _hex(rgb):
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)


def css():
    """Stylesheet for the swatches (installed once per display)."""
    text = """
    .swatch { min-width: 26px; min-height: 26px; padding: 0; border-radius: 999px;
              box-shadow: inset 0 0 0 1px alpha(black, 0.25); }
    .swatch:checked { outline: 2px solid @accent_color; outline-offset: 2px; }
    """
    for name, rgb, _label in COLORS:
        text += f".swatch-{name}, .swatch-{name}:hover, .swatch-{name}:checked " \
                f"{{ background: {_hex(rgb)}; }}\n"
    return text


class StyleButton(Gtk.MenuButton):
    """Edits a canvas.Style in place and emits "changed" with the field
    that changed: "color", "width" or "text_size"."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, style):
        super().__init__(tooltip_text="Color and Size")
        self.style = style
        self._syncing = False

        self._dot = Gtk.DrawingArea(content_width=18, content_height=18,
                                    valign=Gtk.Align.CENTER)
        self._dot.set_draw_func(self._draw_dot)
        self.set_child(self._dot)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        row = Gtk.Box(spacing=8)
        self._swatches = {}
        group = None
        for name, rgb, label in COLORS:
            b = Gtk.ToggleButton(tooltip_text=label, css_classes=["swatch", f"swatch-{name}"])
            if group is None:
                group = b
            else:
                b.set_group(group)
            b.connect("toggled", self._on_swatch, rgb)
            self._swatches[rgb] = b
            row.append(b)
        box.append(row)

        box.append(Gtk.Label(label="Pen Width", xalign=0, css_classes=["caption-heading"]))
        self._width = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.5, 8, 0.25)
        self._width.set_digits(2)
        self._width.set_draw_value(True)
        for v in (1, 2, 4):
            self._width.add_mark(v, Gtk.PositionType.BOTTOM, None)
        self._width.connect("value-changed", self._on_width)
        box.append(self._width)

        size_row = Gtk.Box(spacing=12)
        size_row.append(Gtk.Label(label="Text Size", xalign=0, hexpand=True,
                                  css_classes=["caption-heading"]))
        self._size = Gtk.SpinButton.new_with_range(6, 72, 1)
        self._size.connect("value-changed", self._on_text_size)
        size_row.append(self._size)
        box.append(size_row)
        self.set_popover(Gtk.Popover(child=box))
        self.sync()

    def sync(self):
        """Show the style's current values (without emitting "changed")."""
        st = self.style
        self._syncing = True
        for rgb, b in self._swatches.items():
            b.set_active(all(abs(a - c) < 0.01 for a, c in zip(rgb, st.color, strict=True)))
        self._width.set_value(st.width)
        self._size.set_value(st.text_size)
        self._syncing = False
        self._dot.queue_draw()

    def _draw_dot(self, _area, cr, w, h):
        cr.arc(w / 2, h / 2, min(w, h) / 2 - 1, 0, 2 * math.pi)
        cr.set_source_rgb(*self.style.color)
        cr.fill_preserve()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.6)
        cr.set_line_width(1)
        cr.stroke()

    def _on_swatch(self, btn, rgb):
        if btn.get_active() and not self._syncing:
            self.style.color = rgb
            self._dot.queue_draw()
            self.emit("changed", "color")

    def _on_width(self, scale):
        if not self._syncing:
            self.style.width = scale.get_value()
            self.emit("changed", "width")

    def _on_text_size(self, spin):
        if not self._syncing:
            self.style.text_size = spin.get_value()
            self.emit("changed", "text_size")
