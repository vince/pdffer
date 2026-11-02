"""Page layout geometry (no GTK needed). Run: python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pdffer.layout import GAP, MARGIN, PageLayout, fit_width_zoom

LETTER = (612, 792)
LANDSCAPE = (792, 612)


class Layout(unittest.TestCase):
    def test_pages_stack_and_centre(self):
        lay = PageLayout([LETTER, LANDSCAPE, LETTER], zoom=1.0, view_width=1000)
        (x0, y0, w0, h0), (x1, y1, w1, _h1), _ = lay.rects
        self.assertEqual(y0, MARGIN)
        self.assertEqual(y1, MARGIN + 792 + GAP)
        self.assertEqual(x0, round((1000 - 612) / 2))  # centred in a wider view
        self.assertEqual(x1, round((1000 - 792) / 2))
        self.assertEqual(lay.height, MARGIN + 792 + GAP + 612 + GAP + 792 + MARGIN)
        self.assertEqual(lay.width, 792 + 2 * MARGIN)  # widest page + margins

    def test_fit_width(self):
        z = fit_width_zoom([LETTER, LANDSCAPE], 800 + 2 * MARGIN, 0.1, 6)
        self.assertAlmostEqual(z, 800 / 792)  # the widest page fills the view
        self.assertEqual(fit_width_zoom([LETTER], 10, 0.1, 6), 100 / 612)  # min width
        self.assertEqual(fit_width_zoom([(10, 10)], 5000, 0.1, 6), 6)  # clamped

    def test_hit_and_mapping(self):
        lay = PageLayout([LETTER, LETTER], zoom=2.0, view_width=0)
        x, y, w, h = lay.rects[1]
        self.assertEqual(lay.page_at(x + 1, y + 1), 1)
        self.assertIsNone(lay.page_at(x - 1, y + 1))  # left of the page
        self.assertIsNone(lay.page_at(x + 1, y - GAP / 2))  # in the gap
        self.assertEqual(lay.to_page(1, x + 20, y + 40), (10, 20))
        self.assertEqual(lay.to_content(1, 10, 20), (x + 20, y + 40))
        self.assertIsNone(PageLayout().page_at(0, 0))

    def test_visible_and_current(self):
        lay = PageLayout([LETTER] * 10, zoom=1.0, view_width=0)
        top = lay.rects[4][1] + 700  # bottom of page 5 at the top of the view
        self.assertEqual(list(lay.visible(top, 600)), [4, 5])
        # only 92 pt of page 5 is in the upper third, so page 6 is "current"
        self.assertEqual(lay.current(top, 600), 5)
        self.assertEqual(lay.current(lay.rects[4][1], 600), 4)
        self.assertEqual(lay.index_at(-50), 0)  # clamped
        self.assertEqual(lay.index_at(10 ** 9), 9)

    def test_anchor_survives_zoom(self):
        a = PageLayout([LETTER] * 10, zoom=1.0, view_width=0)
        anchor = a.anchor(a.rects[6][1] + 100)  # 100 pt into page 7
        self.assertEqual(anchor, (6, 100))
        b = PageLayout([LETTER] * 10, zoom=0.5, view_width=0)
        self.assertEqual(b.resolve(anchor), b.rects[6][1] + 50)


if __name__ == "__main__":
    unittest.main()
