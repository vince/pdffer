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
import re
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
    def __init__(self, path, base_bytes, poppler_doc, pages, marks, original_bytes=None):
        self.path = path
        self.base_bytes = base_bytes  # the PDF without our annotations
        # the file exactly as loaded; saves append to it (incremental update)
        self.original_bytes = original_bytes if original_bytes is not None else base_bytes
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
                    text = str(a[DATA_KEY])
                    mark = _mark_from_json(text, index, info, a)
                    if mark is not None:
                        if a.is_indirect:
                            # lets an unchanged mark keep its annotation object
                            mark._src = (index, a.objgen, text)
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
        doc = cls(path, base, pdoc, pages, marks, original_bytes=data)
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

        Returns (marker, job) where job(flatten) builds the PDF bytes and is
        safe to run in a worker thread.

        Normal saves are incremental updates appended to the file as loaded,
        so existing digital signatures stay valid. Flattened copies, and
        files that can't take an incremental update, are fully rewritten.
        """
        items = [_ExportItem(copy.deepcopy(m), self.pages[m.page]) for m in self.marks]
        base, original = self.base_bytes, self.original_bytes
        marker = self._marker()

        def job(flatten):
            if not flatten:
                try:
                    return build_incremental(original, items)
                except NoIncremental:
                    pass
            return build_pdf(base, items, flatten)
        return marker, job


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
        src = getattr(mark, "_src", None)
        # Unchanged since load: keep the existing annotation object.
        # (Compare parsed values: a reloaded 2 comes back as 2.0.)
        self.reuse_objgen = None
        if src and src[0] == mark.page and json.loads(src[2]) == json.loads(self.json):
            self.reuse_objgen = src[1]
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


def _make_annot(pdf, page, it, form, date):
    m = it.mark
    x0, y0, x1, y1 = it.rect
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
    return annot


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
                ref = pdf.make_indirect(_make_annot(pdf, page, it, form, date))
                if "/Annots" in page.obj and isinstance(page.obj.Annots, pikepdf.Array):
                    page.obj.Annots.append(ref)
                else:
                    page.obj.Annots = pikepdf.Array([ref])
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


# ------------------------------------------------------ incremental save

class NoIncremental(Exception):
    """This file can't take an incremental update; rewrite it instead."""


_STARTXREF = re.compile(rb"startxref\s+(\d+)")
_OBJ_HEADER = re.compile(rb"\s*\d+\s+\d+\s+obj\b")
_REF = re.compile(rb"(\d+) (\d+) R\b")


def _last_xref(data):
    """('table' | 'stream', offset) of the file's last cross-reference."""
    i = data.rfind(b"startxref")
    m = _STARTXREF.match(data, i) if i >= 0 else None
    if not m:
        raise NoIncremental("no startxref")
    off = int(m.group(1))
    if data.startswith(b"xref", off):
        return "table", off
    if _OBJ_HEADER.match(data, off):
        return "stream", off
    raise NoIncremental("startxref offset is wrong")


def _is_ours(annot):
    return isinstance(annot, pikepdf.Dictionary) and DATA_KEY in annot


def _serialize(obj):
    """(bytes of an indirect object's body, references it contains)."""
    if isinstance(obj, pikepdf.Stream):
        raw = obj.read_raw_bytes()
        obj.stream_dict.Length = len(raw)
        head = obj.stream_dict.unparse()
        return head + b"\nstream\n" + raw + b"\nendstream", _REF.findall(head)
    if isinstance(obj, pikepdf.Object):
        body = obj.unparse(resolved=True)
    elif isinstance(obj, bool):  # pikepdf returns some scalars as Python values
        body = b"true" if obj else b"false"
    elif obj is None:
        body = b"null"
    else:
        body = str(obj).encode()
    return body, _REF.findall(body)


def _xref_groups(entries):
    """Split sorted [((num, gen), offset)] into runs of consecutive numbers."""
    groups = []
    for e in entries:
        if groups and e[0][0] == groups[-1][-1][0][0] + 1:
            groups[-1].append(e)
        else:
            groups.append([e])
    return groups


def build_incremental(original, items):
    """Return `original` with an incremental update appended that replaces
    our annotations with `items`. The original bytes are left untouched, so
    digital signatures over them remain valid.

    Raises NoIncremental when the file needs a full rewrite instead.
    """
    style, prev = _last_xref(original)
    pdf = pikepdf.open(io.BytesIO(original))
    with pdf:
        if pdf.is_encrypted:
            raise NoIncremental("encrypted")
        orig_size = int(pdf.trailer.get("/Size", 0))
        # QPDF numbers new objects after the existing ones; find where.
        first_new = pdf.make_indirect(pikepdf.Dictionary()).objgen[0]
        if first_new < orig_size:
            raise NoIncremental("object numbering")

        fresh = [it for it in items if it.reuse_objgen is None]
        forms = iter([])
        if fresh:
            ap_pdf = pikepdf.open(io.BytesIO(_render_appearances(fresh)))
            forms = iter([pdf.copy_foreign(p.as_form_xobject()) for p in ap_pdf.pages])
        date = _pdf_date()
        ours = {}
        for it in items:
            if it.reuse_objgen is not None:
                ref = pdf.get_object(it.reuse_objgen)
            else:
                ref = pdf.make_indirect(_make_annot(pdf, pdf.pages[it.page], it, next(forms), date))
            ours.setdefault(it.page, []).append(ref)

        changed = []
        for index, page in enumerate(pdf.pages):
            annots = page.obj.get("/Annots")
            old = list(annots) if isinstance(annots, pikepdf.Array) else []
            if index not in ours and not any(_is_ours(a) for a in old):
                continue
            new = [a for a in old if not _is_ours(a)] + ours.get(index, [])
            if all(a.is_indirect for a in old) and \
                    [a.objgen for a in new] == [a.objgen for a in old]:
                continue
            if not page.obj.is_indirect:
                raise NoIncremental("direct page object")
            if new:
                page.obj.Annots = pikepdf.Array(new)
            else:
                del page.obj["/Annots"]
            changed.append(page.obj.objgen)
        if not changed:
            return original

        # Write the changed pages plus every new object they reach.
        out = bytearray(original)
        if not out.endswith((b"\n", b"\r")):
            out += b"\n"
        offsets = {}
        queue, seen = list(changed), set(changed)
        while queue:
            objgen = queue.pop(0)
            body, refs = _serialize(pdf.get_object(objgen))
            offsets[objgen] = len(out)
            out += f"{objgen[0]} {objgen[1]} obj\n".encode() + body + b"\nendobj\n"
            for num, gen in refs:
                ref = (int(num), int(gen))
                if ref[0] >= first_new and ref not in seen:
                    seen.add(ref)
                    queue.append(ref)
        if pdf.get_warnings():
            raise NoIncremental("damaged file")

        trailer = pdf.trailer
        root = trailer.Root.objgen
        info = trailer.get("/Info")
        ids = trailer.get("/ID")
        id0 = bytes(ids[0]) if isinstance(ids, pikepdf.Array) and len(ids) == 2 \
            else os.urandom(16)
        common = f"/Root {root[0]} {root[1]} R"
        if info is not None and info.is_indirect:
            common += f" /Info {info.objgen[0]} {info.objgen[1]} R"
        common += f" /ID [<{id0.hex()}><{os.urandom(16).hex()}>] /Prev {prev}"
        size = max(orig_size, max(n for n, _g in offsets) + 1)

        xref_off = len(out)
        if style == "table":
            out += b"xref\n"
            for group in _xref_groups(sorted(offsets.items())):
                out += f"{group[0][0][0]} {len(group)}\n".encode()
                for (_num, gen), off in group:
                    out += f"{off:010d} {gen:05d} n\r\n".encode()
            out += f"trailer\n<< /Size {size} {common} >>\n".encode()
        else:
            # match the original: a cross-reference stream (PDF 1.5+)
            xref_num = size
            offsets[(xref_num, 0)] = xref_off
            entries = sorted(offsets.items())
            ow = 4 if len(out) < 2 ** 32 else 8
            rows = b"".join(b"\x01" + off.to_bytes(ow, "big") + gen.to_bytes(2, "big")
                            for (_n, gen), off in entries)
            index = " ".join(f"{g[0][0][0]} {len(g)}" for g in _xref_groups(entries))
            out += (f"{xref_num} 0 obj\n<< /Type /XRef /Size {xref_num + 1} /Index [{index}]"
                    f" /W [1 {ow} 2] {common} /Length {len(rows)} >>\nstream\n").encode()
            out += rows + b"\nendstream\nendobj\n"
        out += f"startxref\n{xref_off}\n%%EOF\n".encode()
        return bytes(out)


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
