"""Round-trip tests: marks saved to PDF render where they were drawn,
and reload as editable marks. Run: python3 -m unittest discover tests
"""

import io
import os
import sys
import tempfile
import unittest

import cairo
import pikepdf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pdf_annotator.document import GLib, Poppler  # noqa: E402
from pdf_annotator.document import (  # noqa: E402
    Document, ImageData, ImageMark, Stroke, TextMark, write_atomic)

RED = (1.0, 0.0, 0.0)
BLUE = (0.0, 0.0, 1.0)


def make_pdf(path):
    """3 pages: plain letter, /Rotate 90, and offset CropBox + /Rotate 270."""
    buf = io.BytesIO()
    s = cairo.PDFSurface(buf, 612, 792)
    for _ in range(3):
        cr = cairo.Context(s)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.show_page()
    s.finish()
    pdf = pikepdf.open(io.BytesIO(buf.getvalue()))
    pdf.pages[1].obj.Rotate = 90
    pdf.pages[2].obj.CropBox = pikepdf.Array([50, 60, 550, 700])
    pdf.pages[2].obj.Rotate = 270
    pdf.save(path)


def render_page(data, index, scale=1.0):
    doc = Poppler.Document.new_from_bytes(GLib.Bytes.new(data), None)
    page = doc.get_page(index)
    w, h = page.get_size()
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, int(w * scale), int(h * scale))
    cr = cairo.Context(surf)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    cr.scale(scale, scale)
    page.render(cr)
    return surf


def pixel(surf, x, y):
    stride = surf.get_stride()
    data = surf.get_data()
    off = int(y) * stride + int(x) * 4
    b, g, r = data[off], data[off + 1], data[off + 2]
    return r, g, b


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "in.pdf")
        make_pdf(self.src)

    def _marks(self, doc):
        for i in range(3):
            # horizontal red line at display y=100, x=100..200
            doc.add_mark(Stroke(i, [(100, 100, 0.5), (150, 100, 0.5), (200, 100, 0.5)], RED, 4))
            doc.add_mark(TextMark(i, 100, 200, "HP 23", BLUE, 24))

    def _check_render(self, data, label):
        for i in range(3):
            surf = render_page(data, i)
            r, g, b = pixel(surf, 150, 100)
            self.assertTrue(r > 200 and g < 80 and b < 80,
                            f"{label} page {i}: stroke not at expected spot ({r},{g},{b})")
            # text should put some blue ink in its box (upright text)
            blue = 0
            for x in range(100, 170):
                for y in range(200, 228):
                    pr, pg, pb = pixel(surf, x, y)
                    if pb > 150 and pr < 120:
                        blue += 1
            self.assertGreater(blue, 30, f"{label} page {i}: text missing")
            # nothing drawn left of where we drew
            self.assertEqual(pixel(surf, 50, 100), (255, 255, 255))

    def test_save_reload(self):
        doc = Document.load(self.src)
        self.assertEqual(doc.marks, [])
        self._marks(doc)
        self.assertTrue(doc.dirty)
        marker, job = doc.snapshot()
        data = job(False)
        self._check_render(data, "annotated")

        out = os.path.join(self.dir, "out.pdf")
        write_atomic(out, data)
        doc2 = Document.load(out)
        self.assertEqual(len(doc2.marks), 6)
        for m in doc2.marks:
            if m.kind == "ink":
                self.assertAlmostEqual(m.points[0][0], 100, places=1)
                self.assertAlmostEqual(m.points[0][1], 100, places=1)
            else:
                self.assertAlmostEqual(m.x, 100, places=1)
                self.assertAlmostEqual(m.y, 200, places=1)
                self.assertEqual(m.text, "HP 23")
        # base doc for editing must not contain our annotations
        self.assertEqual(len(render_annots(doc2.base_gbytes.get_data())), 0)
        # saving again doesn't duplicate annotations
        data2 = doc2.snapshot()[1](False)
        self.assertEqual(len(render_annots(data2)), 6)

    def test_flatten(self):
        doc = Document.load(self.src)
        self._marks(doc)
        data = doc.snapshot()[1](True)
        self._check_render(data, "flattened")
        self.assertEqual(len(render_annots(data)), 0)
        flat = os.path.join(self.dir, "flat.pdf")
        write_atomic(flat, data)
        self.assertEqual(Document.load(flat).marks, [])

    def test_undo_redo_dirty(self):
        doc = Document.load(self.src)
        s = Stroke(0, [(1, 1, 0.5)], RED, 2)
        doc.add_mark(s)
        doc.mark_saved(doc.snapshot()[0])
        self.assertFalse(doc.dirty)
        doc.remove_marks([s])
        self.assertEqual(doc.marks, [])
        self.assertTrue(doc.dirty)
        doc.undo()
        self.assertEqual(doc.marks, [s])
        self.assertFalse(doc.dirty)
        doc.redo()
        self.assertEqual(doc.marks, [])
        t = TextMark(0, 5, 5, "a", BLUE, 12)
        doc.add_mark(t)
        doc.edit_text(t, text="b")
        doc.move_mark(t, 3, 4)
        self.assertEqual((t.text, t.x, t.y), ("b", 8, 9))
        doc.undo()
        doc.undo()
        self.assertEqual((t.text, t.x, t.y), ("a", 5, 5))

    def test_hit_testing(self):
        s = Stroke(0, [(10, 10, 0.5), (50, 10, 0.5)], RED, 2)
        self.assertTrue(s.hit(30, 12, 1))
        self.assertFalse(s.hit(30, 20, 1))
        t = TextMark(0, 100, 100, "HP 9", BLUE, 14)
        self.assertTrue(t.hit(105, 105, 0))
        self.assertFalse(t.hit(90, 90, 0))

    def test_images(self):
        # green PNG with a transparent left half, and a red JPEG
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 20)
        cr = cairo.Context(surf)
        cr.set_source_rgb(0, 0.8, 0)
        cr.rectangle(20, 0, 20, 20)
        cr.fill()
        png = io.BytesIO()
        surf.write_to_png(png)
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 30, 30)
        pb.fill(0xff0000ff)
        ok, jpeg = pb.save_to_bufferv("jpeg", [], [])
        jpeg = bytes(jpeg)

        doc = Document.load(self.src)
        for i in range(3):
            doc.add_mark(Stroke(i, [(0, 0, 0.5)], RED, 1))  # mixed with other kinds
            doc.add_mark(ImageMark(i, 100, 300, 80, 40, ImageData(png.getvalue())))
            doc.add_mark(ImageMark(i, 300, 300, 60, 60, ImageData(jpeg)))
        data = doc.snapshot()[1](False)
        for i in range(3):
            surf = render_page(data, i)
            self.assertEqual(pixel(surf, 120, 320), (255, 255, 255), f"page {i}: alpha lost")
            r, g, b = pixel(surf, 160, 320)
            self.assertTrue(g > 150 and r < 80, f"page {i}: png missing ({r},{g},{b})")
            r, g, b = pixel(surf, 330, 330)
            self.assertTrue(r > 200 and g < 80, f"page {i}: jpeg missing ({r},{g},{b})")
        self.assertLess(len(data), 60_000)

        out = os.path.join(self.dir, "img.pdf")
        write_atomic(out, data)
        doc2 = Document.load(out)
        imgs = [m for m in doc2.marks if m.kind == "image"]
        self.assertEqual(len(imgs), 6)
        for m in imgs:
            self.assertIn(round(m.x), (100, 300))
            self.assertAlmostEqual(m.y, 300, places=1)
            self.assertIn((round(m.w), round(m.h)), ((80, 40), (60, 60)))
        self.assertEqual(imgs[0].image.data, png.getvalue())
        # flattened copy renders them too
        flat = doc2.snapshot()[1](True)
        r, g, b = pixel(render_page(flat, 1), 160, 320)
        self.assertTrue(g > 150 and r < 80)
        # resize via edit is undoable
        m = imgs[0]
        doc2.edit(m, w=160, h=80)
        doc2.undo()
        self.assertEqual(round(m.w), 80)

    def test_pressure_stroke_has_no_gaps(self):
        """Variable-width strokes are filled outlines split at sharp turns;
        corners and joins must be solid ink (no holes, no pinching)."""
        from pdf_annotator import render
        zig = [(10 + i * 4, 20 + (14 if (i // 5) % 2 else 0) +
                (i % 5) * (2.8 if (i // 5) % 2 == 0 else -2.8), 0.2 + 0.8 * i / 40)
               for i in range(41)]
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 800, 260)
        cr = cairo.Context(surf)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.scale(4, 4)
        render.draw_stroke(cr, zig, (0, 0, 1), 4)
        for i, (x, y, _p) in enumerate(zig):  # every sample point, incl. corners
            r, g, b = pixel(surf, x * 4, y * 4)
            self.assertTrue(b > 200 and r < 60, f"gap at point {i}: {(r, g, b)}")
            if i:
                mx, my = (x + zig[i - 1][0]) / 2, (y + zig[i - 1][1]) / 2
                r, g, b = pixel(surf, mx * 4, my * 4)
                self.assertTrue(b > 200 and r < 60, f"gap before point {i}")
        self.assertEqual(pixel(surf, 4, 240), (255, 255, 255))


def render_annots(data):
    pdf = pikepdf.open(io.BytesIO(data))
    out = []
    for p in pdf.pages:
        out += list(p.obj.get("/Annots", []))
    return out


if __name__ == "__main__":
    unittest.main()
