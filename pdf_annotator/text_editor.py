"""The in-place text box used by the text tool."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gtk

from . import render


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
