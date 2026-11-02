"""Page layout: where each page sits in the scrollable content.

Pure geometry, no GTK. "Content" coordinates are pixels from the top-left
of the scrollable area; "page" coordinates are points from the top-left of
a page as displayed (see document.PageInfo).
"""

import bisect

MARGIN = 16  # around the pages
GAP = 14  # between pages


def fit_width_zoom(page_sizes, view_width, min_zoom, max_zoom):
    """Zoom at which the widest page fills the view width."""
    widest = max(w for w, _h in page_sizes)
    avail = max(100, view_width - 2 * MARGIN)
    return max(min_zoom, min(max_zoom, avail / widest))


class PageLayout:
    """Pages stacked vertically and centred horizontally at a zoom level."""

    def __init__(self, page_sizes=(), zoom=1.0, view_width=0):
        self.zoom = zoom
        self.rects = []  # (x, y, w, h) per page, content coordinates
        self.tops = []  # y of each page, for bisecting
        if not page_sizes:
            self.width = self.height = 0
            return
        content_w = max(w for w, _h in page_sizes) * zoom + 2 * MARGIN
        total_w = max(content_w, view_width)
        y = MARGIN
        for w, h in page_sizes:
            pw, ph = w * zoom, h * zoom
            self.rects.append((round((total_w - pw) / 2), round(y), pw, ph))
            self.tops.append(round(y))
            y += ph + GAP
        self.width = content_w
        self.height = y - GAP + MARGIN

    def __len__(self):
        return len(self.rects)

    def index_at(self, cy):
        """The page at or above content y (clamped to a valid page)."""
        return max(0, min(len(self.rects) - 1, bisect.bisect_right(self.tops, cy) - 1))

    def page_at(self, cx, cy):
        """The page containing a content point, or None."""
        if not self.rects:
            return None
        i = self.index_at(cy)
        x, y, w, h = self.rects[i]
        return i if x <= cx <= x + w and y <= cy <= y + h else None

    def to_page(self, index, cx, cy):
        x, y, _w, _h = self.rects[index]
        return (cx - x) / self.zoom, (cy - y) / self.zoom

    def to_content(self, index, px, py):
        x, y, _w, _h = self.rects[index]
        return x + px * self.zoom, y + py * self.zoom

    def visible(self, top, height):
        """Range of pages overlapping content rows [top, top + height)."""
        if not self.rects:
            return range(0)
        first = self.index_at(top)
        last = first
        while last + 1 < len(self.rects) and self.rects[last + 1][1] < top + height:
            last += 1
        return range(first, last + 1)

    def current(self, top, height):
        """The page most prominently in view: the first one reaching past
        the upper third of the view."""
        vis = self.visible(top, height)
        if not vis:
            return 0
        mark = top + height / 3
        for i in vis:
            _x, y, _w, h = self.rects[i]
            if y + h + GAP > mark:
                return i
        return vis[-1]

    def anchor(self, cy):
        """A zoom-independent position: (page, points below its top)."""
        i = self.index_at(cy)
        return i, (cy - self.rects[i][1]) / self.zoom

    def resolve(self, anchor):
        """Content y of an anchor() taken from any layout of the same pages."""
        i, offset = anchor
        return self.rects[i][1] + offset * self.zoom
