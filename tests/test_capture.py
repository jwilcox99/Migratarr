"""Verify isolated path rewriting and refusal after source changes."""

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from migratarr_validation import capture


class CaptureTests(unittest.TestCase):
    def test_committed_placement_scripts_pass_integrity_check(self):
        for kind in capture.SCRIPTS:
            with self.subTest(kind=kind):
                capture.checked_tree(kind)

    def test_redirects_only_known_bindings(self):
        tree = ast.parse('from pathlib import Path\nCACHE_DIR = Path("/live/cache")\nOUTPUT = Path("/live/result.csv")\nOTHER = Path("/live/other")\n')
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            rewritten = capture.redirect_destinations(
                tree, directory / "result.csv", directory / "cache")
            values = {}
            exec(compile(rewritten, "<test>", "exec"), values)
            self.assertEqual(values["OUTPUT"], directory / "result.csv")
            self.assertEqual(values["CACHE_DIR"], directory / "cache")
            self.assertEqual(values["OTHER"], Path("/live/other"))

    def test_rejects_missing_binding(self):
        with self.assertRaisesRegex(ValueError, "Missing placement path"):
            capture.redirect_destinations(ast.parse('OUTPUT = Path("/live")'),
                                          Path("/new"), Path("/cache"))

    def test_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dry_run_movies.py"
            path.write_text('OUTPUT = Path("/live")')
            with patch.object(capture, "ROOT", Path(temp)):
                with self.assertRaisesRegex(ValueError, "changed"):
                    capture.checked_tree("movie")

    def test_refuses_existing_capture_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict("os.environ", {"TMDB_TOKEN": "test"}):
                with self.assertRaises(FileExistsError):
                    capture.capture(Path(temp))


if __name__ == "__main__":
    unittest.main()
