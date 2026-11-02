"""Command-line behavior. Run: python3 -m unittest discover tests"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from pdffer import __version__


class Cli(unittest.TestCase):
    def test_version_flag(self):
        # works without a display and exits without opening a window
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        for flag in ("--version", "-v"):
            out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "pdffer"), flag],
                                 capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), f"PDFfer {__version__}")

    def test_version_is_semver(self):
        parts = __version__.split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(p.isdigit() for p in parts))


if __name__ == "__main__":
    unittest.main()
