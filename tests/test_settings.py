"""Preferences survive damaged or hand-edited settings files.
Run: python3 -m unittest discover tests"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pdf_annotator import settings


class Settings(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.orig = settings._PATH
        self.addCleanup(setattr, settings, "_PATH", self.orig)
        settings._PATH = os.path.join(d, "settings.json")

    def write(self, raw):
        with open(settings._PATH, "w") as fh:
            fh.write(raw)

    def test_valid_values_kept(self):
        good = {"tool": "text", "color": [1, 0, 0.5], "width": 3, "text_size": 20,
                "sidebar": True}
        self.write(json.dumps(good))
        self.assertEqual(settings.load(), {**good, "color": [1.0, 0.0, 0.5],
                                           "width": 3.0, "text_size": 20.0})

    def test_invalid_values_fall_back_to_defaults(self):
        self.write(json.dumps({"tool": "laser", "color": "red", "width": "thick",
                               "text_size": None, "sidebar": "yes", "extra": 1}))
        self.assertEqual(settings.load(), settings.DEFAULTS)
        self.write(json.dumps({"color": [2, 0, 0], "width": 100, "text_size": True}))
        self.assertEqual(settings.load(), settings.DEFAULTS)

    def test_unreadable_files(self):
        for raw in ("", "{not json", "[1, 2, 3]", "null"):
            self.write(raw)
            self.assertEqual(settings.load(), settings.DEFAULTS, raw)
        os.remove(settings._PATH)
        self.assertEqual(settings.load(), settings.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
