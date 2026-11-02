"""Error messages are plain language and name the file; technical
detail is kept separately. Run: python3 -m unittest discover tests
"""

import errno
import os
import shutil
import sys
import tempfile
import unittest

import pikepdf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pdf_annotator import errors
from pdf_annotator.document import Document, ImageData, ImageError, LoadError, write_atomic
from test_roundtrip import make_pdf

JARGON = ("Errno", "BytesIO", "trailer", "Poppler", "pikepdf", "0x", ".tmp", "stream")


class Messages(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pdf = os.path.join(self.dir, "sheet.pdf")
        make_pdf(self.pdf)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def assert_load_error(self, path, *expected):
        with self.assertRaises(LoadError) as cm:
            Document.load(path)
        msg = cm.exception.message
        for text in expected:
            self.assertIn(text, msg)
        for word in JARGON:
            self.assertNotIn(word, msg, f"jargon in: {msg}")
        return cm.exception

    def test_open_failures(self):
        self.assert_load_error(os.path.join(self.dir, "gone.pdf"), "“gone.pdf”", "doesn't exist")
        self.assert_load_error(self.dir, "is a folder")
        self.assert_load_error(self.write("notes.pdf", b"hello"), "“notes.pdf” isn't a PDF")
        self.assert_load_error(self.write("empty.pdf", b""), "“empty.pdf” is empty")
        with open(self.pdf, "rb") as fh:
            data = fh.read()
        err = self.assert_load_error(self.write("cut.pdf", data[:len(data) // 3]),
                                     "“cut.pdf” is damaged")
        self.assertTrue(err.details, "technical detail should be kept")
        locked = os.path.join(self.dir, "locked.pdf")
        with pikepdf.open(self.pdf) as pdf:
            pdf.save(locked, encryption=pikepdf.Encryption(owner="o", user="pw"))
        self.assert_load_error(locked, "“locked.pdf”", "password")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_permission_denied(self):
        os.chmod(self.pdf, 0)
        self.addCleanup(os.chmod, self.pdf, 0o644)
        self.assert_load_error(self.pdf, "permission", "“sheet.pdf”")

    def test_bad_image(self):
        with self.assertRaises(ImageError) as cm:
            ImageData(b"not an image")
        self.assertIn("PNG and JPEG", cm.exception.message)
        self.assertTrue(cm.exception.details)

    def test_save_errors(self):
        target = self.pdf  # exists, so the message reassures it's unchanged
        cases = {
            errno.EACCES: "permission to save in the folder",
            errno.ENOSPC: "enough free disk space",
            errno.EROFS: "read-only drive",
            errno.ENOENT: "no longer exists",
        }
        for code, text in cases.items():
            err = errors.save_error(OSError(code, os.strerror(code), "/x/.pdfannot-1.tmp"),
                                    target)
            self.assertIn(text, err.message)
            self.assertIn("hasn't been changed", err.message)
            self.assertNotIn(".tmp", err.message)
        err = errors.save_error(OSError(errno.EACCES, "denied"),
                                os.path.join(self.dir, "new.pdf"))
        self.assertNotIn("hasn't been changed", err.message)  # nothing to reassure about
        err = errors.save_error(ValueError("boom"), target)
        self.assertIn("Something went wrong", err.message)
        self.assertEqual(err.details, "ValueError: boom")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_real_save_into_read_only_folder(self):
        ro = os.path.join(self.dir, "ro")
        os.mkdir(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        target = os.path.join(ro, "out.pdf")
        with self.assertRaises(OSError) as cm:
            write_atomic(target, b"%PDF")
        msg = errors.save_error(cm.exception, target).message
        self.assertIn("permission to save in the folder", msg)
        self.assertIn("ro”", msg)


if __name__ == "__main__":
    unittest.main()
