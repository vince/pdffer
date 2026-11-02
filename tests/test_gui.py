"""End-to-end tests that drive the real app window.

Needs a display: run normally on a desktop (windows flash briefly), or
headless with `xvfb-run -a python3 -m unittest discover tests`.
"""

import os
import shutil
import tempfile
import unittest

from harness import (  # noqa: E402  (harness sets up sys.path and GTK)
    FakeGesture, canvas_pixel, click, drag, drag_widget, find_widget, make_pdf,
    needs_display, page_rendered, png_bytes, render_canvas, run_app, wait_for)

from gi.repository import Gdk, Gtk  # noqa: E402

from pdf_annotator.document import Document  # noqa: E402

BLUE = (0.11, 0.31, 0.85)


def inky(rgb):
    """Clearly not the light page background."""
    return min(rgb) < 150


@needs_display
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdfannot-gui-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pdf = make_pdf(os.path.join(self.dir, "sheet.pdf"), pages=3)

    def open(self, win, path=None, page=0):
        win.open_path(path or self.pdf)
        c = win.canvas
        yield wait_for(lambda: win.doc is not None and page_rendered(c, page), "page render")

    # ------------------------------------------------------------ drawing

    def test_draw_type_undo_save_reload(self):
        out = os.path.join(self.dir, "out.pdf")

        def steps(win):
            yield from self.open(win)
            c = win.canvas
            win.tools.set_active_name("pen")
            c.style.color = BLUE
            drag(c, 0, (100, 300), (300, 320))
            self.assertEqual([m.kind for m in win.doc.marks], ["ink"])
            # the cached marks layer must show the new stroke right away
            surf = render_canvas(c)
            self.assertTrue(inky(canvas_pixel(c, surf, 0, (200, 310))), "stroke not drawn")
            self.assertIn("•", win.get_title())

            win.tools.set_active_name("text")
            click(c, 0, (100, 400))
            self.assertIsNotNone(c.editor)
            c.editor.tv.get_buffer().set_text("HP 17")
            c.editor._on_key(None, Gdk.KEY_Return, 0, 0)
            self.assertIsNone(c.editor)
            self.assertEqual([m.kind for m in win.doc.marks], ["ink", "text"])

            win.action_undo()
            self.assertEqual(len(win.doc.marks), 1)
            surf = render_canvas(c)  # undone text must disappear from the cache
            self.assertFalse(any(inky(canvas_pixel(c, surf, 0, (x, 400)))
                                 for x in range(102, 140, 4)))
            win.action_redo()
            self.assertEqual(len(win.doc.marks), 2)

            win.save(out)
            yield wait_for(lambda: not win._saving, "save")
            self.assertFalse(win.doc.dirty)
            self.assertNotIn("•", win.get_title())

        run_app(steps)
        marks = Document.load(out).marks
        self.assertEqual(sorted(m.kind for m in marks), ["ink", "text"])
        self.assertEqual([m.text for m in marks if m.kind == "text"], ["HP 17"])

    def test_eraser_is_one_undo_step(self):
        def steps(win):
            yield from self.open(win)
            c = win.canvas
            win.tools.set_active_name("pen")
            for y in (300, 330, 360):
                drag(c, 0, (200, y), (260, y))
            win.tools.set_active_name("eraser")
            drag(c, 0, (230, 280), (230, 380), steps=30)
            self.assertEqual(win.doc.marks, [])
            win.action_undo()
            self.assertEqual(len(win.doc.marks), 3)

        run_app(steps)

    def test_text_editor_fits_its_text(self):
        def steps(win):
            yield from self.open(win)
            c = win.canvas
            win.tools.set_active_name("text")
            click(c, 0, (100, 200))
            c.editor.tv.get_buffer().set_text("Hit points: 23 of 41")
            from pdf_annotator import render
            text_w, _h = render.text_extent("Hit points: 23 of 41", c.editor.size)
            width, _height = c.editor.tv.get_size_request()
            self.assertGreaterEqual(width, text_w * c.zoom, "editor would clip the text")

        run_app(steps)

    def test_style_button(self):
        from pdf_annotator.style_button import COLORS
        red = dict((n, rgb) for n, rgb, _label in COLORS)["red"]

        def steps(win):
            yield from self.open(win)
            c, btn = win.canvas, win.style_btn
            win.tools.set_active_name("text")
            click(c, 0, (100, 200))  # an open text box follows style changes
            btn._swatches[red].set_active(True)
            btn._width.set_value(3.0)
            btn._size.set_value(20)
            self.assertEqual(c.style.color, red)
            self.assertEqual((c.style.width, c.style.text_size), (3.0, 20))
            self.assertEqual((c.editor.color, c.editor.size), (red, 20))

        saved = run_app(steps)
        self.assertEqual((tuple(saved["color"]), saved["width"], saved["text_size"]),
                         (red, 3.0, 20))

        def restored(win):
            yield 0
            self.assertEqual(win.canvas.style.color, red)
            self.assertTrue(win.style_btn._swatches[red].get_active())
        run_app(restored, prefs=saved)

    # ------------------------------------------------------------ images

    def test_image_insert_move_resize_delete(self):
        out = os.path.join(self.dir, "img.pdf")

        def steps(win):
            yield from self.open(win)
            c = win.canvas
            win._insert_image_bytes(png_bytes(120, 40))
            m = c.selected
            self.assertIsNotNone(m)
            self.assertEqual(c.tool, "select")
            x0, y0, w0, h0 = m.x, m.y, m.w, m.h

            drag(c, 0, (m.x + m.w / 2, m.y + m.h / 2), (m.x + m.w / 2 + 40, m.y + m.h / 2 + 30))
            self.assertAlmostEqual(m.x, x0 + 40, delta=1)
            self.assertAlmostEqual(m.y, y0 + 30, delta=1)

            drag(c, 0, (m.x + m.w, m.y + m.h), (m.x + m.w + 60, m.y + m.h + 5))
            self.assertGreater(m.w, w0)
            self.assertAlmostEqual(m.w / m.h, w0 / h0, places=6)
            w1 = m.w
            win.action_undo()
            self.assertAlmostEqual(m.w, w0)
            win.action_redo()
            self.assertAlmostEqual(m.w, w1)

            win.save(out)
            yield wait_for(lambda: not win._saving, "save")
            c.delete_selected()
            self.assertEqual(win.doc.marks, [])
            win.action_undo()
            self.assertEqual(len(win.doc.marks), 1)

        run_app(steps)
        imgs = [m for m in Document.load(out).marks if m.kind == "image"]
        self.assertEqual(len(imgs), 1)
        self.assertAlmostEqual(imgs[0].w / imgs[0].h, 3.0, places=2)

    def test_remembered_select_tool_is_active(self):
        # regression: toolbar showed Select while the canvas drew with Pen
        def steps(win):
            yield 0
            self.assertEqual(win.tools.get_active_name(), "select")
            self.assertEqual(win.canvas.tool, "select")

        run_app(steps, prefs={"tool": "select"})

    # ------------------------------------------------------------ pages drawer

    def test_resizing_keeps_your_place(self):
        book = make_pdf(os.path.join(self.dir, "book.pdf"), pages=30)

        def steps(win):
            yield from self.open(win, book)
            c = win.canvas
            c.scroll_to_page(14)
            yield wait_for(lambda: page_rendered(c, 14), "page 15")
            zoom = c.zoom
            win.sidebar_btn.set_active(True)  # narrower view: fit-width zooms out
            yield wait_for(lambda: c.zoom < zoom * 0.95, "fit-width to rezoom")
            yield 400  # let the drawer finish opening
            self.assertEqual(c.current_page(), 14)

        run_app(steps)

    def test_open_short_pdf_while_deep_in_a_long_one(self):
        # regression: the previous document's page number was used to
        # anchor the new layout (IndexError opening a shorter PDF)
        book = make_pdf(os.path.join(self.dir, "book.pdf"), pages=30)

        def steps(win):
            yield from self.open(win, book)
            win.canvas.scroll_to_page(25)
            yield wait_for(lambda: page_rendered(win.canvas, 25), "page 26")
            yield from self.open(win, self.pdf)
            self.assertEqual(len(win.doc.pages), 3)
            self.assertEqual(win.canvas.current_page(), 0)

        run_app(steps)

    def test_image_lands_in_view(self):
        # regression: the visible area was computed from the wrong axis, so
        # images went to the middle of the page even when scrolled past it
        book = make_pdf(os.path.join(self.dir, "book.pdf"), pages=3)

        def steps(win):
            yield from self.open(win, book)
            c = win.canvas
            c.set_zoom(3.0)  # one page is several screens tall
            _page, _x, top = c.widget_to_page(0, 0, 0)
            c.vadjustment.set_value(c.vadjustment.get_value() + (560 - top) * c.zoom)
            yield wait_for(lambda: page_rendered(c, 0), "zoomed render")
            win._insert_image_bytes(png_bytes(40, 40))
            m = c.selected
            _page, _x, view_top = c.widget_to_page(0, 0, 0)
            _page, _x, view_bottom = c.widget_to_page(0, c.get_height(), 0)
            self.assertTrue(view_top <= m.y + m.h / 2 <= view_bottom,
                            f"image at y={m.y:.0f}, view shows {view_top:.0f}-{view_bottom:.0f}")

        run_app(steps)

    def test_thumbnails_jump_and_follow(self):
        book = make_pdf(os.path.join(self.dir, "book.pdf"), pages=30)

        def steps(win):
            yield from self.open(win, book)
            c, thumbs = win.canvas, win.thumbnails
            win.sidebar_btn.set_active(True)
            yield wait_for(lambda: 0 in thumbs.thumbs, "first thumbnail")
            self.assertTrue(win.split.get_show_sidebar())

            thumbs.selection.set_selected(9)  # click page 10
            yield wait_for(lambda: c.current_page() == 9, "jump to page 10")

            c.scroll_to_page(19)
            yield wait_for(lambda: thumbs.selection.get_selected() == 19,
                           "drawer to follow page 20")

            # the last page can't scroll to the top; its highlight must stay
            thumbs.selection.set_selected(29)
            yield wait_for(lambda: c.current_page() >= 27, "jump to the last page")
            yield 300
            self.assertEqual(thumbs.selection.get_selected(), 29)

        saved = run_app(steps)
        self.assertTrue(saved.get("sidebar"), "drawer state should be remembered")

    # ------------------------------------------------------------ errors

    def test_error_dialog_is_plain_language(self):
        notes = os.path.join(self.dir, "notes.pdf")
        with open(notes, "w") as fh:
            fh.write("just some notes")

        def steps(win):
            win.open_path(notes)
            yield wait_for(lambda: win.get_visible_dialog() is not None, "error dialog")
            dlg = win.get_visible_dialog()
            self.assertEqual(dlg.get_body(), "“notes.pdf” isn't a PDF.")
            self.assertIsNotNone(find_widget(dlg, Gtk.Expander), "technical details")
            dlg.force_close()

        run_app(steps)

    def test_failed_page_shows_placeholder(self):
        import pdf_annotator.page_renderer as renderer_mod

        class BrokenFirstPage:
            def __init__(self, doc):
                self.doc = doc

            def get_page(self, i):
                if i == 0:
                    raise RuntimeError("simulated render failure")
                return self.doc.get_page(i)

        original = renderer_mod.PageRenderer.__init__

        def patched(renderer, doc, on_done):
            original(renderer, BrokenFirstPage(doc), on_done)
        renderer_mod.PageRenderer.__init__ = patched
        self.addCleanup(setattr, renderer_mod.PageRenderer, "__init__", original)

        def steps(win):
            win.open_path(self.pdf)
            c = win.canvas
            yield wait_for(lambda: c._render_key(0) in c._failed, "page 1 to fail")
            surf = render_canvas(c)
            r, g, b = canvas_pixel(c, surf, 0, (20, 20))
            self.assertTrue(225 < r < 250 and r == g == b, f"no placeholder: {(r, g, b)}")
            # a failed page isn't re-requested on every redraw
            c._renderer.want([])
            render_canvas(c)
            self.assertNotIn(c._render_key(0), c._renderer._wanted)

        run_app(steps)

    def test_quit_while_rendering_leaves_no_threads(self):
        # run_app itself asserts no render thread outlives shutdown; queue
        # slow renders so some are in progress when we quit.
        def steps(win):
            yield from self.open(win)
            win.canvas._renderer.want([(i % 3, 612.0, 792.0, 2400, 3100) for i in range(9)])
            yield 50

        run_app(steps)


class StylusHistoryTests(unittest.TestCase):
    """Stray-line regression: invalid coalesced stylus samples are dropped."""

    def test_history_validation(self):
        from pdf_annotator.stylus import history_points as hp

        class TC:
            def __init__(self, x, y, flags):
                self.axes = [0.0] * 12
                self.axes[Gdk.AxisUse.X] = x
                self.axes[Gdk.AxisUse.Y] = y
                self.axes[Gdk.AxisUse.PRESSURE] = 0.6
                self.flags = flags

        class Ev:
            def __init__(self, hist):
                self.hist = hist

            def get_event_type(self):
                return Gdk.EventType.MOTION_NOTIFY

            def get_history(self):
                return self.hist

            def get_position(self):
                return True, 110, 147  # surface coords: 47 px header above

        class G(FakeGesture):
            def __init__(self, hist):
                self.ev = Ev(hist)

            def get_current_event(self):
                return self.ev

        flags = Gdk.AxisFlags.X | Gdk.AxisFlags.Y | Gdk.AxisFlags.PRESSURE
        good = hp(G([TC(105, 145, flags)]), 110, 100, (100, 98))
        self.assertEqual(good, [(105, 98, 0.6)])
        self.assertEqual(hp(G([TC(0, 0, flags)]), 110, 100, (100, 98)), [],
                         "zeroed sample must be discarded")
        self.assertEqual(hp(G([TC(105, 145, 0)]), 110, 100, (100, 98)), [],
                         "sample without X/Y must be discarded")


if __name__ == "__main__":
    unittest.main()
