from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from catalog_guard import CatalogDropError, assess_catalog_drop, main


class CatalogDropGuardTests(unittest.TestCase):
    def test_uses_highest_checked_in_or_live_baseline(self):
        assessment = assess_catalog_drop(130, [120, 167])

        self.assertEqual(assessment.baseline_count, 167)
        self.assertEqual(assessment.minimum_allowed, 126)
        self.assertFalse(assessment.suspicious)

    def test_blocks_drop_greater_than_twenty_five_percent(self):
        with self.assertRaisesRegex(CatalogDropError, "dropped from 167 to 125"):
            assess_catalog_drop(125, [167])

    def test_manual_override_is_explicit_and_reported(self):
        assessment = assess_catalog_drop(100, [167], allow_drop=True)

        self.assertTrue(assessment.suspicious)
        self.assertTrue(assessment.overridden)

    def test_exact_threshold_is_allowed(self):
        assessment = assess_catalog_drop(75, [100])

        self.assertFalse(assessment.suspicious)
        self.assertEqual(assessment.minimum_allowed, 75)

    def test_empty_baseline_does_not_block_first_publication(self):
        assessment = assess_catalog_drop(10, [])

        self.assertFalse(assessment.suspicious)
        self.assertEqual(assessment.baseline_count, 0)

    def test_expired_adzuna_source_requires_manual_override(self):
        snapshot = {
            "fetched_at": "2026-10-01T20:00:00Z",
            "jobs": [],
            "source_state": {"adzuna": {"status": "expired"}},
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                blocked = main(["--current", str(path)])
            with redirect_stdout(io.StringIO()):
                allowed = main([
                    "--current", str(path),
                    "--allow-drop",
                ])

        self.assertEqual(blocked, 1)
        self.assertEqual(allowed, 0)


if __name__ == "__main__":
    unittest.main()
