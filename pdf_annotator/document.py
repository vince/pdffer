"""Document model: loading, editable marks, undo/redo and saving.

Marks are saved as standard PDF annotations (/Ink and /FreeText) with
cairo-generated appearance streams, so they display in any PDF viewer.
Each annotation also carries a private /PdfAnnotatorData entry holding
the editable mark as JSON. On load those annotations are lifted back out
of the PDF and become editable marks again.
"""

import copy
import datetime
import io
import json
import math
import os
import tempfile
import uuid
from dataclasses import dataclass, field

import sys

import cairo
import gi
import pikepdf

gi.require_version("Poppler", "0.18")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Poppler  # noqa: E402

from . import render  # noqa: E402

DATA_KEY = "/PdfAnnotatorData"
IMAGE_KEY = "/PdfAnnotatorImage"
FORMAT_VERSION = 1


# ---------------------------------------------------------------- marks

@dataclass(eq=False)
class Stroke:
    page: int
    points: list  # [(x, y, pressure)] in page display coordinates (pt)
    color: tuple
    width: float
    kind: str = field(default="ink", init=False)

    def bbox(self):
        pad = self.width * 1.5 / 2 + 0.5
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad

    def hit(self, x, y, r):
        x0, y0, x1, y1 = self.bbox()
        if x < x0 - r or x > x1 + r or y < y0 - r or y > y1 + r:
            return False
        pts = self.points
        if len(pts) == 1:
            return math.hypot(x - pts[0][0], y - pts[0][1]) <= r + self.width
        for a, b in zip(pts, pts[1:]):
            tol = r + self.width * render.pressure_factor(max(a[2], b[2])) / 2
            if _seg_dist(x, y, a[0], a[1], b[0], b[1]) <= tol:
                return True
        return False

    def translate(self, dx, dy):
        self.points = [(x + dx, y + dy, p) for x, y, p in self.points]


@dataclass(eq=False)
class TextMark:
    page: int
    x: float  # top-left, page display coordinates (pt)
    y: float
    text: str
    color: tuple
    size: float
    kind: str = field(default="text", init=False)

    def bbox(self):
        w, h = render.text_extent(self.text, self.size)
        return self.x, self.y, self.x + w, self.y + h

    def hit(self, x, y, r):
        x0, y0, x1, y1 = self.bbox()
        return x0 - r <= x <= x1 + r and y0 - r <= y <= y1 + r

    def translate(self, dx, dy):
        self.x += dx
        self.y += dy


class ImageError(Exception):
    pass


class ImageData:
    """An immutable decoded image, shared between a mark and its copies."""

    def __init__(self, data):
        self.data = bytes(data)
        try:
            stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(self.data))
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        except GLib.Error as e:
            raise ImageError(f"Unsupported or damaged image ({e.message})") from e
        orientation = pixbuf.get_option("orientation")
        rotated = orientation not in (None, "1")
        if rotated:
            pixbuf = pixbuf.apply_embedded_orientation()
        fmt = (Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha()
               else Gdk.MemoryFormat.R8G8B8)
        texture = Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), fmt,
                                        pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())
        dl = Gdk.TextureDownloader.new(texture)
        # cairo ARGB32 is native-endian premultiplied
        dl.set_format(Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED if sys.byteorder == "little"
                      else Gdk.MemoryFormat.A8R8G8B8_PREMULTIPLIED)
        pixels, self.stride = dl.download_bytes()
        self.pixels = pixels.get_data()
        self.width, self.height = texture.get_width(), texture.get_height()
        # Embed JPEGs as-is in the PDF (much smaller) unless we had to rotate.
        self.jpeg = self.data[:2] == b"\xff\xd8" and not rotated
        self._surface = None

    def __deepcopy__(self, memo):
        return self

    def surface(self):
        """Cached surface for on-screen drawing (main thread only)."""
        if self._surface is None:
            self._surface = self.new_surface(embed_jpeg=False)
        return self._surface

    def new_surface(self, embed_jpeg=True):
        """A fresh surface, safe to use from any thread."""
        surf = cairo.ImageSurface.create_for_data(
            bytearray(self.pixels), cairo.FORMAT_ARGB32, self.width, self.height, self.stride)
        if embed_jpeg and self.jpeg:
            surf.set_mime_data(cairo.MIME_TYPE_JPEG, self.data)
        return surf


@dataclass(eq=False)
class ImageMark:
    page: int
    x: float  # top-left, page display coordinates (pt)
    y: float
    w: float
    h: float
    image: ImageData
    kind: str = field(default="image", init=False)

    def bbox(self):
        return self.x, self.y, self.x + self.w, self.y + self.h

    def hit(self, x, y, r):
        return self.x - r <= x <= self.x + self.w + r and self.y - r <= y <= self.y + self.h + r

    def translate(self, dx, dy):
        self.x += dx
        self.y += dy


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


# ---------------------------------------------------------------- pages

@dataclass
class PageInfo:
    """Page size as displayed and the display -> PDF user space transform.

    Display space is what Poppler renders: origin top-left, y down,
    crop box applied and /Rotate applied, units in points.
    """
    width: float
    height: float
    d2u: tuple  # (a, b, c, d, e, f): ux = a*x + c*y + e, uy = b*x + d*y + f

    def to_user(self, x, y):
        a, b, c, d, e, f = self.d2u
        return a * x + c * y + e, b * x + d * y + f

    def to_display(self, ux, uy):
        a, b, c, d, e, f = self.d2u
        det = a * d - b * c
        ux, uy = ux - e, uy - f
        return (d * ux - c * uy) / det, (-b * ux + a * uy) / det


def _inherited(obj, key):
    node, depth = obj, 0
    while node is not None and depth < 64:
        if key in node:
            return node[key]
        node = node.get("/Parent")
        depth += 1
    return None


def _box(value, fallback):
    try:
        v = [float(n) for n in value]
        return min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])
    except Exception:
        return fallback


def page_info_from_pikepdf(page):
    obj = page.obj
    mb = _box(_inherited(obj, "/MediaBox"), (0.0, 0.0, 612.0, 792.0))
    cb = _box(_inherited(obj, "/CropBox"), mb)
    x0, y0 = max(cb[0], mb[0]), max(cb[1], mb[1])
    x1, y1 = min(cb[2], mb[2]), min(cb[3], mb[3])
    if x1 <= x0 or y1 <= y0:
        x0, y0, x1, y1 = mb
    try:
        rot = int(_inherited(obj, "/Rotate") or 0)
    except Exception:
        rot = 0
    rot = (round(rot / 90) * 90) % 360
    cw, ch = x1 - x0, y1 - y0
    if rot == 90:
        return PageInfo(ch, cw, (0, 1, 1, 0, x0, y0))
    if rot == 180:
        return PageInfo(cw, ch, (-1, 0, 0, 1, x1, y0))
    if rot == 270:
        return PageInfo(ch, cw, (0, -1, -1, 0, x1, y1))
    return PageInfo(cw, ch, (1, 0, 0, -1, x0, y1))


# ------------------------------------------------------------- commands

class _AddMark:
    def __init__(self, mark):
        self.mark = mark

    def do(self, doc):
        doc.marks.append(self.mark)

    def undo(self, doc):
        doc.marks.remove(self.mark)


class _RemoveMarks:
    def __init__(self, doc, marks):
        ids = {id(m) for m in marks}
        self.entries = [(i, m) for i, m in enumerate(doc.marks) if id(m) in ids]

    def do(self, doc):
        for _i, m in reversed(self.entries):
            doc.marks.remove(m)

    def undo(self, doc):
        for i, m in self.entries:
            doc.marks.insert(i, m)


class _Edit:
    def __init__(self, mark, **new):
        self.mark = mark
        self.new = new
        self.old = {k: getattr(mark, k) for k in new}

    def do(self, doc):
        for k, v in self.new.items():
            setattr(self.mark, k, v)

    def undo(self, doc):
        for k, v in self.old.items():
            setattr(self.mark, k, v)


class _Move:
    def __init__(self, mark, dx, dy):
        self.mark, self.dx, self.dy = mark, dx, dy

    def do(self, doc):
        self.mark.translate(self.dx, self.dy)

    def undo(self, doc):
        self.mark.translate(-self.dx, -self.dy)


# ------------------------------------------------------------- document

class LoadError(Exception):
    pass


class Document:
    def __init__(self, path, base_bytes, poppler_doc, pages, marks):
        self.path = path
        self.base_bytes = base_bytes  # the PDF without our annotations
        self.poppler = poppler_doc
        self.pages = pages
        self.marks = marks
        self._undo = []
        self._redo = []
        self._saved_marker = None
        self.on_changed = None  # callback()

    # ---- loading

    @classmethod
    def load(cls, path):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise LoadError(f"Could not read file: {e.strerror}") from e
        try:
            pdf = pikepdf.open(io.BytesIO(data))
        except pikepdf.PasswordError as e:
            raise LoadError("This PDF is password protected.") from e
        except Exception as e:
            raise LoadError(f"Not a readable PDF: {e}") from e

        pages, marks, stripped = [], [], False
        with pdf:
            for index, page in enumerate(pdf.pages):
                info = page_info_from_pikepdf(page)
                pages.append(info)
                annots = page.obj.get("/Annots")
                if annots is None or not isinstance(annots, pikepdf.Array):
                    continue
                keep, ours = [], []
                for a in annots:
                    if isinstance(a, pikepdf.Dictionary) and DATA_KEY in a:
                        ours.append(a)
                    else:
                        keep.append(a)
                if not ours:
                    continue
                stripped = True
                for a in ours:
                    mark = _mark_from_json(str(a[DATA_KEY]), index, info, a)
                    if mark is not None:
                        marks.append(mark)
                if keep:
                    page.obj.Annots = pikepdf.Array(keep)
                else:
                    del page.obj["/Annots"]
            if stripped:
                buf = io.BytesIO()
                pdf.save(buf)
                base = buf.getvalue()
            else:
                base = data

        gbytes = GLib.Bytes.new(base)
        try:
            pdoc = Poppler.Document.new_from_bytes(gbytes, None)
        except GLib.Error as e:
            raise LoadError(f"Poppler could not open this PDF: {e.message}") from e
        if pdoc.get_n_pages() != len(pages):
            raise LoadError("Page count mismatch while loading this PDF.")
        # Use Poppler's idea of the displayed size for layout.
        for i, info in enumerate(pages):
            w, h = pdoc.get_page(i).get_size()
            info.width, info.height = w, h
        doc = cls(path, base, pdoc, pages, marks)
        doc.base_gbytes = gbytes  # shared with extra Poppler documents
        return doc

    # ---- editing

    def _changed(self):
        if self.on_changed:
            self.on_changed()

    def _push(self, cmd):
        cmd.do(self)
        self._undo.append(cmd)
        self._redo.clear()
        self._changed()

    def add_mark(self, mark):
        self._push(_AddMark(mark))

    def remove_marks(self, marks):
        if marks:
            self._push(_RemoveMarks(self, marks))

    def edit(self, mark, **changes):
        """Change attributes of a mark (text, color, size, geometry...)."""
        changes = {k: v for k, v in changes.items() if getattr(mark, k) != v}
        if changes:
            self._push(_Edit(mark, **changes))

    edit_text = edit

    def move_mark(self, mark, dx, dy):
        if dx or dy:
            self._push(_Move(mark, dx, dy))

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def undo(self):
        if self._undo:
            cmd = self._undo.pop()
            cmd.undo(self)
            self._redo.append(cmd)
            self._changed()

    def redo(self):
        if self._redo:
            cmd = self._redo.pop()
            cmd.do(self)
            self._undo.append(cmd)
            self._changed()

    def _marker(self):
        return self._undo[-1] if self._undo else None

    @property
    def dirty(self):
        return self._marker() is not self._saved_marker

    def mark_saved(self, marker):
        self._saved_marker = marker
        self._changed()

    # ---- saving

    def snapshot(self):
        """Capture everything needed to save, on the main thread.

        Returns (marker, job) where job() builds the PDF bytes and is safe
        to run in a worker thread.
        """
        items = [_ExportItem(copy.deepcopy(m), self.pages[m.page]) for m in self.marks]
        base = self.base_bytes
        marker = self._marker()
        return marker, (lambda flatten: build_pdf(base, items, flatten))


def _mark_from_json(text, page_index, info, annot=None):
    try:
        d = json.loads(text)
        if d["type"] == "image":
            stream = annot.get(IMAGE_KEY) if annot is not None else None
            if not isinstance(stream, pikepdf.Stream):
                return None
            image = ImageData(stream.read_bytes())
            x, y = info.to_display(d["x"], d["y"])
            return ImageMark(page_index, x, y, float(d["w"]), float(d["h"]), image)
        color = tuple(float(c) for c in d["color"])[:3]
        if d["type"] == "ink":
            pts = []
            for ux, uy, p in d["points"]:
                x, y = info.to_display(ux, uy)
                pts.append((x, y, float(p)))
            if not pts:
                return None
            return Stroke(page_index, pts, color, float(d["width"]))
        if d["type"] == "text":
            x, y = info.to_display(d["x"], d["y"])
            return TextMark(page_index, x, y, str(d["text"]), color, float(d["size"]))
    except Exception:
        return None
    return None


def _mark_to_json(mark, info):
    r = lambda v: round(v, 2)  # noqa: E731
    if mark.kind == "image":
        ux, uy = info.to_user(mark.x, mark.y)
        return json.dumps({"v": FORMAT_VERSION, "type": "image", "x": r(ux), "y": r(uy),
                           "w": round(mark.w, 3), "h": round(mark.h, 3)},
                          separators=(",", ":"))
    base = {"v": FORMAT_VERSION, "color": [round(c, 4) for c in mark.color]}
    if mark.kind == "ink":
        pts = []
        for x, y, p in mark.points:
            ux, uy = info.to_user(x, y)
            pts.append([r(ux), r(uy), round(p, 3)])
        base.update(type="ink", width=mark.width, points=pts)
    else:
        ux, uy = info.to_user(mark.x, mark.y)
        base.update(type="text", x=r(ux), y=r(uy), text=mark.text, size=mark.size)
    return json.dumps(base, separators=(",", ":"), ensure_ascii=True)


class _ExportItem:
    """A mark plus its precomputed geometry in PDF user space."""

    def __init__(self, mark, info):
        self.mark = mark
        self.page = mark.page
        self.json = _mark_to_json(mark, info)
        x0, y0, x1, y1 = mark.bbox()
        corners = [info.to_user(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        bx0 = min(c[0] for c in corners)
        by0 = min(c[1] for c in corners)
        bx1 = max(c[0] for c in corners)
        by1 = max(c[1] for c in corners)
        bx1 = max(bx1, bx0 + 1)
        by1 = max(by1, by0 + 1)
        self.rect = (bx0, by0, bx1, by1)
        # display -> cairo page coordinates of an appearance page whose
        # size is the user-space bbox (cairo PDF pages are y-down).
        a, b, c, d, e, f = info.d2u
        self.matrix = cairo.Matrix(a, -b, c, -d, e - bx0, by1 - f)
        if mark.kind == "ink":
            self.inklist = []
            for x, y, _p in mark.points:
                ux, uy = info.to_user(x, y)
                self.inklist += [round(ux, 2), round(uy, 2)]


def _pdf_date():
    now = datetime.datetime.now().astimezone()
    off = now.utcoffset() or datetime.timedelta()
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return now.strftime("D:%Y%m%d%H%M%S") + f"{sign}{mins // 60:02d}'{mins % 60:02d}'"


def _render_appearances(items):
    buf = io.BytesIO()
    w0 = items[0].rect[2] - items[0].rect[0]
    h0 = items[0].rect[3] - items[0].rect[1]
    surface = cairo.PDFSurface(buf, w0, h0)
    for it in items:
        surface.set_size(it.rect[2] - it.rect[0], it.rect[3] - it.rect[1])
        cr = cairo.Context(surface)
        cr.set_matrix(it.matrix)
        render.draw_mark(cr, it.mark, export=True)
        cr.show_page()
        del cr
    surface.finish()
    return buf.getvalue()


def build_pdf(base_bytes, items, flatten=False):
    """Return PDF bytes: the base document plus the given marks."""
    pdf = pikepdf.open(io.BytesIO(base_bytes))
    with pdf:
        if items:
            ap_pdf = pikepdf.open(io.BytesIO(_render_appearances(items)))
            date = _pdf_date()
            wrapped = set()
            for i, it in enumerate(items):
                form = pdf.copy_foreign(ap_pdf.pages[i].as_form_xobject())
                page = pdf.pages[it.page]
                x0, y0, x1, y1 = it.rect
                if flatten:
                    if it.page not in wrapped:
                        page.contents_add(b"q\n", prepend=True)
                        page.contents_add(b"\nQ\n")
                        wrapped.add(it.page)
                    name = page.add_resource(form, pikepdf.Name.XObject, prefix="PdfAnnot")
                    page.contents_add(
                        f"q 1 0 0 1 {x0:.4f} {y0:.4f} cm {name} Do Q\n".encode())
                    continue
                m = it.mark
                annot = pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Rect=pikepdf.Array([x0, y0, x1, y1]),
                    F=4,  # print
                    NM=pikepdf.String("pdfannot-" + uuid.uuid4().hex),
                    M=pikepdf.String(date),
                    P=page.obj,
                    AP=pikepdf.Dictionary(N=form),
                )
                if m.kind != "image":
                    annot.C = pikepdf.Array([round(c, 4) for c in m.color])
                if m.kind == "image":
                    annot.Subtype = pikepdf.Name.Stamp
                    annot.Name = pikepdf.Name("/Image")
                    annot.Contents = pikepdf.String("Image")
                    annot[IMAGE_KEY] = pdf.make_stream(m.image.data)
                elif m.kind == "ink":
                    annot.Subtype = pikepdf.Name.Ink
                    annot.InkList = pikepdf.Array([pikepdf.Array(it.inklist)])
                    annot.BS = pikepdf.Dictionary(W=round(m.width, 3))
                else:
                    annot.Subtype = pikepdf.Name.FreeText
                    annot.Contents = pikepdf.String(m.text)
                    r, g, b = m.color
                    annot.DA = pikepdf.String(f"/Helv {m.size:g} Tf {r:.3f} {g:.3f} {b:.3f} rg")
                    annot.BS = pikepdf.Dictionary(W=0)
                annot[DATA_KEY] = pikepdf.String(it.json)
                ref = pdf.make_indirect(annot)
                if "/Annots" in page.obj and isinstance(page.obj.Annots, pikepdf.Array):
                    page.obj.Annots.append(ref)
                else:
                    page.obj.Annots = pikepdf.Array([ref])
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def write_atomic(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pdfannot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
