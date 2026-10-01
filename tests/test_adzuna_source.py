import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import scrape
from publish_snapshot import normalize_job


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "adzuna_pages.json").read_text(
        encoding="utf-8"
    )
)


class FakeResponse:
    def __init__(self, *, status_code=200, json_value=None):
        self.status_code = status_code
        self._json_value = json_value

    def json(self):
        return self._json_value


class AdzunaSourceTests(unittest.TestCase):
    def setUp(self):
        scrape.reset_coverage()

    def test_missing_credentials_skips_without_network_request(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(scrape, "g") as get,
        ):
            jobs = scrape.from_adzuna(None)

        self.assertEqual(jobs, [])
        get.assert_not_called()
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["status"], "skipped")
        self.assertEqual(coverage["pages"], 0)

    def test_paginated_canada_search_dedupes_and_preserves_fields(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs["params"]))
            page = url.rsplit("/", 1)[-1]
            return FakeResponse(
                json_value={
                    "count": FIXTURE["count"],
                    "results": FIXTURE["pages"][page],
                }
            )

        with (
            patch.dict(
                os.environ,
                {"ADZUNA_APP_ID": "test-id", "ADZUNA_APP_KEY": "test-key"},
                clear=False,
            ),
            patch.object(scrape, "ADZUNA_RESULTS_PER_PAGE", 3),
            patch.object(scrape, "ADZUNA_MAX_PAGES", 2),
            patch.object(scrape, "g", side_effect=fake_get),
        ):
            jobs = scrape.from_adzuna(None)

        self.assertEqual([job["external_id"] for job in jobs], [
            "adz-1", "adz-2", "adz-3", "adz-4", "adz-5"
        ])
        self.assertEqual(jobs[0]["src"], "adzuna")
        self.assertTrue(all(job["src"] == "adzuna" for job in jobs))
        self.assertEqual(jobs[0]["title"], "Finance Intern")
        self.assertEqual(jobs[0]["company"], "Alpha Bank")
        self.assertEqual(jobs[0]["loc"], "Toronto, Ontario")
        self.assertEqual(jobs[0]["posted"], "2026-10-01T12:00:00Z")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("/jobs/ca/search/" in url for url, _ in calls))
        self.assertTrue(all(params["where"] == "Toronto" for _, params in calls))
        self.assertTrue(all(params["distance"] == 100 for _, params in calls))
        self.assertTrue(all("intern" in params["what_or"] for _, params in calls))
        self.assertTrue(all("co-op" in params["what_or"] for _, params in calls))
        self.assertTrue(all("student" in params["what_or"] for _, params in calls))
        self.assertTrue(all("graduate" in params["what_or"] for _, params in calls))
        self.assertTrue(all(params["app_id"] == "test-id" for _, params in calls))
        self.assertTrue(all(params["app_key"] == "test-key" for _, params in calls))

        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["query_total"], 7)
        self.assertEqual(coverage["pages"], 2)
        self.assertEqual(coverage["returned"], 6)
        self.assertEqual(coverage["records"], 5)
        self.assertEqual(coverage["capped_terms"], 1)
        self.assertFalse(coverage["complete"])

    def test_gta_and_internship_filters_keep_only_target_roles(self):
        with (
            patch.dict(
                os.environ,
                {"ADZUNA_APP_ID": "test-id", "ADZUNA_APP_KEY": "test-key"},
                clear=False,
            ),
            patch.object(scrape, "ADZUNA_RESULTS_PER_PAGE", 3),
            patch.object(scrape, "ADZUNA_MAX_PAGES", 2),
            patch.object(
                scrape,
                "g",
                side_effect=[
                    FakeResponse(json_value={
                        "count": FIXTURE["count"],
                        "results": FIXTURE["pages"]["1"],
                    }),
                    FakeResponse(json_value={
                        "count": FIXTURE["count"],
                        "results": FIXTURE["pages"]["2"],
                    }),
                ],
            ),
        ):
            jobs = scrape.from_adzuna(None)

        kept = {
            job["external_id"]
            for job in jobs
            if scrape.is_internship(job["title"]) and scrape.geo(job["loc"])
        }
        self.assertEqual(kept, {"adz-1", "adz-2", "adz-4", "adz-5"})

    def test_http_quota_failure_is_visible_and_isolated(self):
        with (
            patch.dict(
                os.environ,
                {"ADZUNA_APP_ID": "test-id", "ADZUNA_APP_KEY": "test-key"},
                clear=False,
            ),
            patch.object(
                scrape,
                "g",
                return_value=FakeResponse(status_code=429, json_value={}),
            ),
        ):
            jobs = scrape.from_adzuna(None)

        self.assertEqual(jobs, [])
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["errors"], ["page1:HTTP 429"])
        self.assertFalse(coverage["complete"])

    def test_external_id_produces_stable_cloudflare_id(self):
        legacy = {
            "src": "adzuna",
            "external_id": "adz-1",
            "company": "Alpha Bank",
            "title": "Finance Intern",
            "loc": "Toronto, Ontario",
            "geo": "Toronto",
            "cats": ["Finance"],
            "url": "https://www.adzuna.ca/details/adz-1",
            "posted": "2026-10-01T12:00:00Z",
            "content": "Finance internship",
        }

        normalized = normalize_job(legacy)

        self.assertEqual(normalized["source"], "adzuna")
        self.assertEqual(normalized["externalId"], "adz-1")
        self.assertEqual(
            normalized["id"],
            hashlib.sha256(b"adzuna\nadz-1").hexdigest(),
        )

    def test_legacy_ui_contains_compliant_source_marker_attribution(self):
        html = Path("site/index.html").read_text(encoding="utf-8")
        logo = Path("site/assets/adzuna-logo.png")

        self.assertIn("j.src!=='adzuna'", html)
        self.assertIn('class="adzuna-jobs-link"', html)
        self.assertIn('class="adzuna-logo-link"', html)
        self.assertGreaterEqual(html.count('href="https://www.adzuna.ca/"'), 2)
        self.assertIn('aria-label="Jobs by Adzuna"', html)
        self.assertIn('src="assets/adzuna-logo.png"', html)
        self.assertIn('alt="Adzuna"', html)
        self.assertIn("min-width: 116px", html)
        self.assertIn("min-height: 23px", html)
        self.assertTrue(logo.is_file())
        self.assertGreater(logo.stat().st_size, 1_000)


if __name__ == "__main__":
    unittest.main()
