"""Document model: loading, editable marks, undo/redo and saving.

Marks are saved as standard PDF annotations (/Ink and /FreeText) with
cairo-generated appearance streams, so they display in any PDF viewer.
Each annotation also carries a private /PdfAnnotatorData entry holding
the editable mark as JSON. On load those annotations are lifted back out
of the PDF and become editable marks again.
"""

import copy
import datetime
import decimal
import hashlib
import io
import itertools
import json
import math
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass, field

import cairo
import gi
import pikepdf

gi.require_version("Poppler", "0.18")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Poppler

from . import render
from .errors import UserError, name, read_error, technical

DATA_KEY = "/PdfAnnotatorData"
IMAGE_KEY = "/PdfAnnotatorImage"
FORMAT_VERSION = 1


# ---------------------------------------------------------------- marks

class _BBoxCache:
    """Caches bbox(); any attribute assignment (move, edit, undo) clears it."""

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)
        if key != "_bbox":
            object.__setattr__(self, "_bbox", None)

    def bbox(self):
        b = getattr(self, "_bbox", None)
        if b is None:
            b = self._compute_bbox()
            object.__setattr__(self, "_bbox", b)
        return b


@dataclass(eq=False)
class Stroke(_BBoxCache):
    page: int
    points: list  # [(x, y, pressure)] in page display coordinates (pt)
    color: tuple
    width: float
    kind: str = field(default="ink", init=False)

    def add_point(self, x, y, p):
        """Extend a stroke being drawn (keeps the bbox cache valid)."""
        self.points.append((x, y, p))
        self._bbox = None

    def _compute_bbox(self):
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
        for a, b in itertools.pairwise(pts):
            tol = r + self.width * render.pressure_factor(max(a[2], b[2])) / 2
            if _seg_dist(x, y, a[0], a[1], b[0], b[1]) <= tol:
                return True
        return False

    def translate(self, dx, dy):
        self.points = [(x + dx, y + dy, p) for x, y, p in self.points]


@dataclass(eq=False)
class TextMark(_BBoxCache):
    page: int
    x: float  # top-left, page display coordinates (pt)
    y: float
    text: str
    color: tuple
    size: float
    kind: str = field(default="text", init=False)

    def _compute_bbox(self):
        w, h = render.text_extent(self.text, self.size)
        return self.x, self.y, self.x + w, self.y + h

    def hit(self, x, y, r):
        x0, y0, x1, y1 = self.bbox()
        return x0 - r <= x <= x1 + r and y0 - r <= y <= y1 + r

    def translate(self, dx, dy):
        self.x += dx
        self.y += dy


class ImageError(UserError):
    pass


class ImageData:
    """An immutable decoded image, shared between a mark and its copies."""

    def __init__(self, data):
        self.data = bytes(data)
        try:
            stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(self.data))
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        except GLib.Error as e:
            if "Loader process" in e.message:  # glycin's sandboxed decoder died
                raise ImageError("The system's image decoder couldn't start, so images "
                                 "can't be inserted right now.", technical(e)) from e
            raise ImageError("This file isn't an image PDF Annotator can read, or it's "
                             "damaged. PNG and JPEG images work best.", technical(e)) from e
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
class ImageMark(_BBoxCache):
    page: int
    x: float  # top-left, page display coordinates (pt)
    y: float
    w: float
    h: float
    image: ImageData
    kind: str = field(default="image", init=False)

    def _compute_bbox(self):
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

class LoadError(UserError):
    pass


class Document:
    def __init__(self, path, poppler_doc, pages, marks, original_gbytes, base_gbytes):
        self.path = path
        # The file exactly as loaded; saves append to it (incremental update).
        self._original = original_gbytes
        # What Poppler displays: the file without our annotations. The same
        # buffer as the original unless there were annotations to strip.
        self.base_gbytes = base_gbytes
        self.poppler = poppler_doc
        self.pages = pages
        self.marks = marks
        self._undo = []
        self._redo = []
        self._saved_marker = None
        self.on_changed = None  # callback()
        self.version = 0  # bumped on every change; lets views cache renders

    @property
    def original_bytes(self):
        return self._original.get_data()

    # ---- loading

    @classmethod
    def load(cls, path):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            err = read_error(e, path)
            raise LoadError(err.message, err.details) from e
        if not data:
            raise LoadError(f"{name(path)} is empty.")
        damaged = f"{name(path)} is damaged and can't be opened."
        try:
            pdf = pikepdf.open(io.BytesIO(data))
        except pikepdf.PasswordError as e:
            raise LoadError(f"{name(path)} is protected with a password. PDF Annotator "
                            "can't open password-protected PDFs.", technical(e)) from e
        except Exception as e:
            if b"%PDF" not in data[:1024]:
                raise LoadError(f"{name(path)} isn't a PDF.", technical(e)) from e
            raise LoadError(damaged, technical(e)) from e
        try:
            return cls._load_pdf(path, data, pdf)
        except LoadError:
            raise
        except Exception as e:
            raise LoadError(damaged, technical(e)) from e

    @classmethod
    def _load_pdf(cls, path, data, pdf):
        damaged = f"{name(path)} is damaged and can't be opened."
        pages, marks, stripped = [], [], False
        with pdf:
            if len(pdf.pages) == 0:
                raise LoadError(f"{name(path)} has no pages.")
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
            # Keep one copy of the file in memory (as GLib.Bytes, which
            # Poppler shares) rather than Python bytes plus a GLib copy.
            original = GLib.Bytes.new(data)
            if stripped:
                buf = io.BytesIO()
                pdf.save(buf)
                base = GLib.Bytes.new(buf.getvalue())
                del buf
            else:
                base = original

        try:
            pdoc = Poppler.Document.new_from_bytes(base, None)
        except GLib.Error as e:
            raise LoadError(damaged, technical(e)) from e
        if pdoc.get_n_pages() != len(pages):
            raise LoadError(damaged, f"pikepdf found {len(pages)} pages, "
                                     f"Poppler {pdoc.get_n_pages()}")
        # Use Poppler's idea of the displayed size for layout.
        for i, info in enumerate(pages):
            w, h = pdoc.get_page(i).get_size()
            info.width, info.height = w, h
        return cls(path, pdoc, pages, marks, original, base)

    # ---- editing

    def _changed(self):
        self.version += 1
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
        original = self.original_bytes
        marker = self._marker()

        def job(flatten):
            if not flatten:
                try:
                    return build_incremental(original, items)
                except NoIncremental:
                    pass
            return build_pdf(original, items, flatten)
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
    def r(v):
        return round(v, 2)

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


def build_pdf(original, items, flatten=False):
    """Return a full rewrite of `original` with our annotations replaced by
    `items` (burned into the page content if flatten). Encryption and its
    permissions are kept."""
    pdf = pikepdf.open(io.BytesIO(original))
    with pdf:
        for page in pdf.pages:
            annots = page.obj.get("/Annots")
            if isinstance(annots, pikepdf.Array) and any(_is_ours(a) for a in annots):
                keep = [a for a in annots if not _is_ours(a)]
                if keep:
                    page.obj.Annots = pikepdf.Array(keep)
                else:
                    del page.obj["/Annots"]
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
        # pikepdf drops encryption unless asked to keep it
        pdf.save(out, encryption=True if pdf.is_encrypted else None)
        return out.getvalue()


# ------------------------------------------------------ incremental save

class NoIncremental(Exception):
    """This file can't take an incremental update; rewrite it instead."""


_STARTXREF = re.compile(rb"startxref\s+(\d+)")
_OBJ_HEADER = re.compile(rb"\s*\d+\s+\d+\s+obj\b")


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


def _rc4(key, data):
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


class _Crypt:
    """Encrypts strings and streams of new objects with the document's own
    security handler (PDF 32000 section 7.6), so an encrypted PDF can take
    an incremental update."""

    def __init__(self, pdf):
        info = pdf.encryption
        self.key = info.encryption_key

        def method(m):
            return {"none": None, "rc4": "rc4", "aes": "aesv2", "aesv3": "aesv3"}.get(m.name)
        if info.V in (1, 2):
            self.stream = self.string = "rc4"
        elif info.V == 4:
            self.stream, self.string = method(info.stream_method), method(info.string_method)
        elif info.V == 5:
            self.stream = self.string = "aesv3"
        else:
            raise NoIncremental(f"unsupported encryption V={info.V}")
        if None in (self.stream, self.string) and \
                "unknown" in (info.stream_method.name, info.string_method.name):
            raise NoIncremental("unknown crypt filter")
        if "aesv2" in (self.stream, self.string) or "aesv3" in (self.stream, self.string):
            try:
                from cryptography.hazmat.primitives import padding
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            except ImportError as e:
                raise NoIncremental("python3-cryptography is needed for AES") from e
            self._aes = (padding, Cipher, algorithms, modes)

    def encrypt(self, data, objgen, kind):
        method = self.stream if kind == "stream" else self.string
        if method is None:
            return data
        if method == "aesv3":
            key = self.key
        else:
            num, gen = objgen
            salt = b"sAlT" if method == "aesv2" else b""
            digest = hashlib.md5(self.key + num.to_bytes(4, "little")[:3] +
                                 gen.to_bytes(4, "little")[:2] + salt).digest()
            key = digest[:min(len(self.key) + 5, 16)]
        if method == "rc4":
            return _rc4(key, data)
        padding, Cipher, algorithms, modes = self._aes
        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return iv + enc.update(padder.update(data) + padder.finalize()) + enc.finalize()


def _fmt_real(v):
    if isinstance(v, decimal.Decimal):
        text = format(v, "f")
    else:
        text = f"{v:.6f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-", "-0") else "0"


class _Writer:
    """Serializes indirect objects for an incremental update, recording the
    objects they reference and encrypting strings and streams if needed."""

    def __init__(self, crypt=None):
        self.crypt = crypt

    def object(self, obj, objgen):
        """(body bytes, [referenced objgens]) for an indirect object."""
        self._objgen = objgen
        self._refs = []
        if isinstance(obj, pikepdf.Stream):
            raw = obj.read_raw_bytes()
            if self.crypt:
                raw = self.crypt.encrypt(raw, objgen, "stream")
            d = obj.stream_dict
            items = [(k, d[k]) for k in d.keys() if k != "/Length"]
            items.append(("/Length", len(raw)))
            body = self._dict(items) + b"\nstream\n" + raw + b"\nendstream"
        else:
            body = self._value(obj, top=True)
        return body, self._refs

    def _dict(self, items):
        parts = [pikepdf.Name(k).unparse() + b" " + self._value(v) for k, v in items]
        return b"<< " + b" ".join(parts) + b" >>"

    def _value(self, v, top=False):
        if isinstance(v, pikepdf.Object) and not top and v.is_indirect:
            self._refs.append(v.objgen)
            return f"{v.objgen[0]} {v.objgen[1]} R".encode()
        if isinstance(v, pikepdf.Dictionary):
            return self._dict([(k, v[k]) for k in v.keys()])
        if isinstance(v, pikepdf.Array):
            return b"[" + b" ".join(self._value(x) for x in v) + b"]"
        if isinstance(v, pikepdf.String):
            data = bytes(v)
            if self.crypt:
                data = self.crypt.encrypt(data, self._objgen, "string")
            return b"<" + data.hex().encode() + b">"
        if isinstance(v, pikepdf.Name):
            return v.unparse()
        if isinstance(v, bool):
            return b"true" if v else b"false"
        if isinstance(v, int):
            return str(v).encode()
        if isinstance(v, (float, decimal.Decimal)):
            return _fmt_real(v).encode()
        if v is None:
            return b"null"
        if isinstance(v, pikepdf.Object):
            return v.unparse()
        raise NoIncremental(f"can't serialize {type(v).__name__}")


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
        writer = _Writer(_Crypt(pdf) if pdf.is_encrypted else None)
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
            body, refs = writer.object(pdf.get_object(objgen), objgen)
            offsets[objgen] = len(out)
            out += f"{objgen[0]} {objgen[1]} obj\n".encode() + body + b"\nendobj\n"
            for ref in refs:
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
        if pdf.is_encrypted:
            # The file key depends on the first /ID, which we keep.
            enc = trailer.get("/Encrypt")
            if enc is None or not enc.is_indirect:
                raise NoIncremental("no indirect /Encrypt in trailer")
            common += f" /Encrypt {enc.objgen[0]} {enc.objgen[1]} R"
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
