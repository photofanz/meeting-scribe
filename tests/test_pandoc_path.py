"""Converters must find Homebrew pandoc even when PATH is launchd/SSH-narrow."""
from pathlib import Path
import sys
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import make_pdf  # noqa: E402


class FindPandocTests(unittest.TestCase):
    def test_which_wins_when_path_has_pandoc(self):
        with mock.patch.object(make_pdf.shutil, "which", return_value="/usr/bin/pandoc"):
            self.assertEqual(make_pdf.find_pandoc(), "/usr/bin/pandoc")

    def test_homebrew_location_is_used_when_path_is_empty(self):
        with mock.patch.object(make_pdf.shutil, "which", return_value=None):
            with mock.patch.object(make_pdf.Path, "is_file", return_value=True):
                self.assertEqual(make_pdf.find_pandoc(), "/opt/homebrew/bin/pandoc")

    def test_missing_everywhere_returns_none(self):
        with mock.patch.object(make_pdf.shutil, "which", return_value=None):
            with mock.patch.object(make_pdf.Path, "is_file", return_value=False):
                self.assertIsNone(make_pdf.find_pandoc())


if __name__ == "__main__":
    unittest.main()
