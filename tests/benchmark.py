#!/usr/bin/env python3
"""Performance benchmark (run by hand, not part of the test suite):

    python3 tests/benchmark.py

Measures, on a page with 300 pressure strokes and 40 text boxes, the
cost of a redraw while drawing and of a select-tool hover check, plus the
memory used to open a large generated PDF. Numbers depend on the machine;
compare before/after on the same one.
"""

import gc
import math
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cairo

from harness import page_rendered, render_canvas, run_app, wait_for
from pdffer.document import Stroke, TextMark


def heavy_pdf(path, pages=120):
    s = cairo.PDFSurface(path, 612, 792)
    for p in range(pages):
        cr = cairo.Context(s)
        for k in range(3000):
            cr.set_source_rgba((k * 7 % 255) / 255, (k * 13 % 255) / 255, (p * 3 % 255) / 255, 0.5)
            cr.arc((k * 37) % 612, (k * 53) % 792, 4 + k % 9, 0, 6.28)
            cr.fill()
        cr.show_page()
    s.finish()


def marks(page):
    random.seed(1)
    out = []
    for k in range(300):
        x0, y0 = random.uniform(30, 500), random.uniform(30, 700)
        pts = [(x0 + i * 1.2, y0 + 8 * math.sin(i / 6 + k), 0.3 + 0.6 * abs(math.sin(i / 11)))
               for i in range(100)]
        out.append(Stroke(page, pts, (0.1, 0.2, 0.7), 1.5))
    for k in range(40):
        out.append(TextMark(page, random.uniform(30, 450), random.uniform(30, 740),
                            f"HP {k} / 41", (0.8, 0.1, 0.1), 14))
    return out


def rss_mb():
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    return 0.0


def main():
    path = os.path.join(tempfile.mkdtemp(prefix="pdffer-bench-"), "heavy.pdf")
    print("generating test PDF…", flush=True)
    heavy_pdf(path)
    size_mb = os.path.getsize(path) / 2 ** 20

    def steps(win):
        c = win.canvas
        gc.collect()
        before = rss_mb()
        win.open_path(path)
        yield wait_for(lambda: win.doc is not None and page_rendered(c, 0), "first page")
        gc.collect()
        print(f"memory to open a {size_mb:.0f} MB PDF: +{rss_mb() - before:.0f} MB")

        win.doc.marks.extend(marks(0))
        win.doc.version += 1
        c.live = Stroke(0, [(100 + i, 100, 0.5) for i in range(50)], (0, 0, 0), 1.5)
        render_canvas(c)  # warm the caches
        n = 30
        t = time.perf_counter()
        for i in range(n):
            c.live.add_point(150 + i, 100, 0.5)
            render_canvas(c)
        print(f"redraw per pen movement (300 strokes + 40 texts): "
              f"{(time.perf_counter() - t) / n * 1000:.1f} ms")
        c.live = None

        n = 200
        t = time.perf_counter()
        for _ in range(n):
            c.mark_at((0, random.uniform(0, 600), random.uniform(0, 790)))
        print(f"select-tool hover check: {(time.perf_counter() - t) / n * 1000:.2f} ms")

    run_app(steps)


if __name__ == "__main__":
    main()
