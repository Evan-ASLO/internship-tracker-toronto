import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

import requests
import scrape
from adzuna_dry_run import build_report
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

    @staticmethod
    def fixed_queries():
        return [
            {"name": "internship", "params": {"title_only": "internship"}},
            {"name": "coop", "params": {"what_or": "co-op coop"}},
            {"name": "new-grad", "params": {"what_phrase": "new graduate"}},
        ]

    def test_targeted_queries_dedupe_and_preserve_fields(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs["params"]))
            params = kwargs["params"]
            if params.get("title_only") == "internship":
                name = "internship"
            elif params.get("what_or") == "co-op coop":
                name = "coop"
            else:
                name = "new-grad"
            fixture = FIXTURE["queries"][name]
            return FakeResponse(
                json_value={
                    "count": fixture["count"],
                    "results": fixture["results"],
                }
            )

        with (
            patch.dict(
                os.environ,
                {"ADZUNA_APP_ID": "test-id", "ADZUNA_APP_KEY": "test-key"},
                clear=False,
            ),
            patch.object(scrape, "ADZUNA_RESULTS_PER_PAGE", 3),
            patch.object(
                scrape, "adzuna_queries_for_time", return_value=self.fixed_queries()
            ),
            patch.object(scrape, "g", side_effect=fake_get),
        ):
            jobs = scrape.from_adzuna(None)

        self.assertEqual([job["external_id"] for job in jobs], [
            "adz-1", "adz-2", "adz-3", "adz-4", "adz-5"
        ])
        self.assertEqual(jobs[0]["src"], "adzuna")
        self.assertTrue(all(job["src"] == "adzuna" for job in jobs))
        self.assertEqual(jobs[0]["title"], "Finance Internship")
        self.assertEqual(jobs[0]["company"], "Alpha Bank")
        self.assertEqual(jobs[0]["loc"], "Toronto, Ontario")
        self.assertEqual(jobs[0]["posted"], "2026-10-01T12:00:00Z")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("/jobs/ca/search/" in url for url, _ in calls))
        self.assertTrue(all(url.endswith("/1") for url, _ in calls))
        self.assertTrue(all(params["where"] == "Toronto" for _, params in calls))
        self.assertTrue(all(params["distance"] == 100 for _, params in calls))
        self.assertTrue(all(params["app_id"] == "test-id" for _, params in calls))
        self.assertTrue(all(params["app_key"] == "test-key" for _, params in calls))

        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["query_total"], 80)
        self.assertEqual(coverage["pages"], 3)
        self.assertEqual(coverage["returned"], 6)
        self.assertEqual(coverage["records"], 5)
        self.assertEqual(coverage["capped_terms"], 1)
        self.assertEqual(
            [query["name"] for query in coverage["queries"]],
            ["internship", "coop", "new-grad"],
        )
        self.assertFalse(coverage["complete"])

    def test_gta_and_internship_filters_keep_only_target_roles(self):
        with (
            patch.dict(
                os.environ,
                {"ADZUNA_APP_ID": "test-id", "ADZUNA_APP_KEY": "test-key"},
                clear=False,
            ),
            patch.object(scrape, "ADZUNA_RESULTS_PER_PAGE", 3),
            patch.object(
                scrape, "adzuna_queries_for_time", return_value=self.fixed_queries()
            ),
            patch.object(
                scrape,
                "g",
                side_effect=[
                    FakeResponse(json_value=FIXTURE["queries"]["internship"]),
                    FakeResponse(json_value=FIXTURE["queries"]["coop"]),
                    FakeResponse(json_value=FIXTURE["queries"]["new-grad"]),
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
            patch.object(
                scrape, "adzuna_queries_for_time", return_value=self.fixed_queries()
            ),
        ):
            jobs = scrape.from_adzuna(None)

        self.assertEqual(jobs, [])
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(len(coverage["errors"]), 3)
        self.assertTrue(all("HTTP 429" in error for error in coverage["errors"]))
        self.assertFalse(coverage["complete"])

    def test_rotation_is_deterministic_and_covers_every_query(self):
        start = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
        first = scrape.adzuna_queries_for_time(start)
        self.assertEqual(first, scrape.adzuna_queries_for_time(start))
        self.assertEqual(len(first), scrape.ADZUNA_REQUESTS_PER_RUN)

        names = set()
        new_grad_phrases = set()
        for offset in range(10):
            queries = scrape.adzuna_queries_for_time(start + timedelta(hours=offset))
            self.assertEqual(len(queries), 3)
            names.update(query["name"] for query in queries)
            for query in queries:
                if query["name"] == "new-grad":
                    new_grad_phrases.add(query["params"]["what_phrase"])

        self.assertEqual(names, set(scrape.ADZUNA_QUERY_NAMES))
        self.assertEqual(new_grad_phrases, {"new grad", "new graduate"})

    def test_normal_request_budget_stays_inside_official_limits(self):
        hourly = scrape.ADZUNA_REQUESTS_PER_RUN
        self.assertEqual(hourly, 3)
        self.assertLessEqual(hourly * 24, 250)
        self.assertLessEqual(hourly * 24 * 7, 1000)
        self.assertLessEqual(hourly * 24 * 30, 2500)

    def test_request_budget_caps_transient_retries(self):
        budget = {"remaining": scrape.ADZUNA_REQUESTS_PER_RUN}
        with (
            patch.object(
                scrape.requests,
                "get",
                side_effect=requests.exceptions.SSLError("tls"),
            ) as get,
            patch.object(scrape.time, "sleep"),
        ):
            with self.assertRaises(requests.exceptions.SSLError):
                scrape.g(
                    "https://api.adzuna.com/v1/api/jobs/ca/search/1",
                    request_budget=budget,
                )

        self.assertEqual(get.call_count, 3)
        self.assertEqual(budget["remaining"], 0)

    def test_dry_run_report_contains_per_query_relevance(self):
        coverage = {
            "complete": False,
            "query_total": 80,
            "pages": 3,
            "returned": 6,
            "records": 5,
            "capped_terms": 1,
            "errors": [],
            "queries": [
                {
                    "name": "internship", "total": 75, "returned": 3,
                    "kept": 3, "capped": True, "status": "success",
                }
            ],
        }
        records = []
        for fixture in FIXTURE["queries"].values():
            for result in fixture["results"]:
                records.append({
                    "external_id": result["id"],
                    "src": "adzuna",
                    "company": result["company"]["display_name"],
                    "title": result["title"].replace("<strong>", "").replace("</strong>", ""),
                    "loc": result["location"]["display_name"],
                    "url": result["redirect_url"],
                    "posted": result["created"],
                    "content": result["description"],
                })

        report = build_report(records, coverage)

        self.assertEqual(report["requests"], 3)
        self.assertEqual(report["productUnique"], 4)
        self.assertEqual(report["queries"][0]["name"], "internship")

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
