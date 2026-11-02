"""Incremental saves: the original bytes are kept and existing digital
signatures stay valid. Run: python3 -m unittest discover tests
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import cairo
import pikepdf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pdffer.document import Document, ImageData, ImageMark, Stroke, TextMark, write_atomic
from test_roundtrip import make_pdf, pixel, render_page

RED = (1.0, 0.0, 0.0)


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class Incremental(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def _source(self, xref_style):
        """A test PDF using a classic xref 'table' or an xref 'stream'."""
        tmp = os.path.join(self.dir, "tmp.pdf")
        make_pdf(tmp)
        path = os.path.join(self.dir, f"{xref_style}.pdf")
        mode = (pikepdf.ObjectStreamMode.disable if xref_style == "table"
                else pikepdf.ObjectStreamMode.generate)
        with pikepdf.open(tmp) as pdf:
            pdf.save(path, object_stream_mode=mode)
        data = read(path)
        tail = data[data.rfind(b"startxref"):].split()[1]
        is_table = data[int(tail):].startswith(b"xref")
        self.assertEqual(is_table, xref_style == "table")
        return path, data

    def _annotate(self, path):
        doc = Document.load(path)
        for i in range(3):
            doc.add_mark(Stroke(i, [(100, 100, 0.5), (200, 100, 0.5)], RED, 4))
            doc.add_mark(TextMark(i, 100, 200, "HP 23", RED, 24))
        return doc

    def test_both_xref_styles(self):
        for style in ("table", "stream"):
            with self.subTest(style=style):
                path, original = self._source(style)
                out = self._annotate(path).snapshot()[1](False)
                self.assertEqual(out[:len(original)], original, "original bytes changed")
                with pikepdf.open(io.BytesIO(out)) as pdf:
                    self.assertEqual(pdf.get_warnings(), [], "xref not readable as written")
                    self.assertEqual(len(pdf.pages), 3)
                for i in range(3):
                    r, g, b = pixel(render_page(out, i), 150, 100)
                    self.assertTrue(r > 200 and g < 80, f"{style} page {i} mark missing")
                saved = os.path.join(self.dir, f"{style}-out.pdf")
                write_atomic(saved, out)
                self.assertEqual(len(Document.load(saved).marks), 6)

    def test_resave_reuses_unchanged_marks(self):
        path, _ = self._source("stream")
        saved = os.path.join(self.dir, "saved.pdf")
        write_atomic(saved, self._annotate(path).snapshot()[1](False))
        first = read(saved)

        doc = Document.load(saved)
        self.assertEqual(doc.snapshot()[1](False), first, "no-op save changed the file")

        doc.edit(doc.marks[-1], text="HP 12")
        second = doc.snapshot()[1](False)
        self.assertEqual(second[:len(first)], first)
        self.assertLess(len(second) - len(first), 8000, "unchanged marks were rewritten")
        write_atomic(saved, second)
        texts = sorted(m.text for m in Document.load(saved).marks if m.kind == "text")
        self.assertEqual(texts, ["HP 12", "HP 23", "HP 23"])

        doc = Document.load(saved)
        doc.remove_marks(list(doc.marks))
        write_atomic(saved, doc.snapshot()[1](False))
        self.assertEqual(Document.load(saved).marks, [])

    def test_encryption_and_permissions_kept(self):
        path, _ = self._source("table")
        perms = pikepdf.Permissions(modify_other=False, extract=False)
        kinds = {
            "rc4-40": pikepdf.Encryption(owner="o", user="", R=2, aes=False,
                                         metadata=False, allow=perms),
            "rc4-128": pikepdf.Encryption(owner="o", user="", R=3, aes=False,
                                          metadata=False, allow=perms),
            "aes-128": pikepdf.Encryption(owner="o", user="", R=4, aes=True, allow=perms),
            "aes-256": pikepdf.Encryption(owner="o", user="", R=6, allow=perms),
        }
        png = io.BytesIO()
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 10, 10)
        cairo.Context(surf).paint()
        surf.write_to_png(png)
        for name, enc in kinds.items():
            for style in (pikepdf.ObjectStreamMode.disable, pikepdf.ObjectStreamMode.generate):
                with self.subTest(encryption=name, objstreams=style.name):
                    src = os.path.join(self.dir, f"{name}.pdf")
                    with pikepdf.open(path) as pdf:
                        pdf.save(src, encryption=enc, object_stream_mode=style)
                    original = read(src)
                    doc = Document.load(src)
                    doc.add_mark(Stroke(1, [(100, 100, 0.5), (200, 100, 0.5)], RED, 4))
                    doc.add_mark(TextMark(1, 100, 200, "HP 23 — ünïcødé", RED, 24))
                    doc.add_mark(ImageMark(1, 300, 300, 20, 20, ImageData(png.getvalue())))
                    out = doc.snapshot()[1](False)

                    self.assertEqual(out[:len(original)], original, "fell back to rewrite")
                    with pikepdf.open(io.BytesIO(out)) as pdf:
                        self.assertTrue(pdf.is_encrypted)
                        self.assertEqual(pdf.encryption.R, enc.R)
                        self.assertFalse(pdf.allow.modify_other)
                        self.assertFalse(pdf.allow.extract)
                        self.assertEqual(pdf.get_warnings(), [])
                    # Poppler decrypts independently: the marks must render.
                    r, g, b = pixel(render_page(out, 1), 150, 100)
                    self.assertTrue(r > 200 and g < 80, "encrypted appearance unreadable")
                    saved = os.path.join(self.dir, "out.pdf")
                    write_atomic(saved, out)
                    marks = Document.load(saved).marks
                    self.assertEqual([m.text for m in marks if m.kind == "text"],
                                     ["HP 23 — ünïcødé"])
                    self.assertEqual([m.image.data for m in marks if m.kind == "image"],
                                     [png.getvalue()])

                    flat = doc.snapshot()[1](True)
                    with pikepdf.open(io.BytesIO(flat)) as pdf:
                        self.assertTrue(pdf.is_encrypted, "flattened copy lost encryption")
                        self.assertFalse(pdf.allow.modify_other)

    @unittest.skipUnless(all(shutil.which(t) for t in
                             ("pdfsig", "certutil", "pk12util", "openssl")),
                         "needs pdfsig, certutil, pk12util and openssl")
    def test_digital_signature_stays_valid(self):
        d = self.dir
        nss = os.path.join(d, "nss")
        os.mkdir(nss)

        def run(*cmd):
            subprocess.run(cmd, check=True, capture_output=True)
        run("certutil", "-N", "-d", f"sql:{nss}", "--empty-password")
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", f"{d}/k.pem", "-out", f"{d}/c.pem", "-subj", "/CN=Test Signer")
        run("openssl", "pkcs12", "-export", "-inkey", f"{d}/k.pem", "-in", f"{d}/c.pem",
            "-out", f"{d}/c.p12", "-name", "signer", "-passout", "pass:")
        run("pk12util", "-i", f"{d}/c.p12", "-d", f"sql:{nss}", "-W", "")
        run("certutil", "-M", "-d", f"sql:{nss}", "-n", "signer", "-t", "CT,C,C")
        path, _ = self._source("stream")
        signed = os.path.join(d, "signed.pdf")
        run("pdfsig", "-add-signature", "-nssdir", f"sql:{nss}", "-nick", "signer",
            path, signed)

        def status(p):
            return subprocess.run(["pdfsig", "-nssdir", f"sql:{nss}", p],
                                  capture_output=True, text=True).stdout
        self.assertIn("Signature is Valid", status(signed))

        out = os.path.join(d, "annotated.pdf")
        doc = self._annotate(signed)
        write_atomic(out, doc.snapshot()[1](False))
        doc = Document.load(out)  # and again, editing a reloaded mark
        doc.edit(doc.marks[-1], text="HP 5")
        write_atomic(out, doc.snapshot()[1](False))
        result = status(out)
        self.assertIn("Signature is Valid", result)
        self.assertIn("Not total document signed", result)  # i.e. changed after signing


if __name__ == "__main__":
    unittest.main()
