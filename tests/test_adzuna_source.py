import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
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
        with tempfile.TemporaryDirectory() as temp:
            missing = str(Path(temp) / "missing.json")
            with (
                patch.dict(
                    os.environ,
                    {"ADZUNA_BASELINE_PATH": missing},
                    clear=True,
                ),
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
        return scrape.adzuna_queries()

    @staticmethod
    def moment_with_parity(parity):
        moment = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
        while int(moment.timestamp() // 3600) % 2 != parity:
            moment += timedelta(hours=1)
        return moment

    @staticmethod
    def baseline_row(external_id):
        return {
            "company": "Prior Co",
            "title": "Finance Intern",
            "loc": "Toronto, Ontario",
            "geo": "Toronto",
            "cats": ["Finance"],
            "url": f"https://www.adzuna.ca/details/{external_id}",
            "posted": "2026-10-01T00:00:00Z",
            "src": "adzuna",
            "external_id": external_id,
            "content": "Prior successful row",
        }

    def write_baseline(self, path, refreshed_at, rows=None):
        payload = {
            "fetched_at": refreshed_at.isoformat(),
            "categories": ["Finance"],
            "jobs": rows or [self.baseline_row("prior-1")],
            "source_state": {
                "adzuna": {
                    "status": "fresh",
                    "refreshed_at": refreshed_at.isoformat(),
                    "age_seconds": 0,
                    "requests": 5,
                    "reused": 0,
                }
            },
        }
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def test_targeted_queries_dedupe_and_preserve_fields(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs["params"]))
            params = kwargs["params"]
            if params.get("title_only") == "internship":
                name = "internship"
            elif params.get("title_only") == "intern":
                name = "intern"
            elif params.get("what_or") == "co-op coop":
                name = "coop"
            elif params.get("title_only") == "student":
                name = "student"
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
                {
                    "ADZUNA_APP_ID": "test-id",
                    "ADZUNA_APP_KEY": "test-key",
                    "ADZUNA_FORCE_REFRESH": "true",
                },
                clear=False,
            ),
            patch.object(scrape, "ADZUNA_RESULTS_PER_PAGE", 3),
            patch.object(
                scrape, "adzuna_queries", return_value=self.fixed_queries()
            ),
            patch.object(scrape, "g", side_effect=fake_get),
        ):
            jobs = scrape.from_adzuna(None)

        self.assertEqual([job["external_id"] for job in jobs], [
            "adz-1", "adz-2", "adz-3", "adz-6", "adz-4", "adz-7", "adz-5"
        ])
        self.assertEqual(jobs[0]["src"], "adzuna")
        self.assertTrue(all(job["src"] == "adzuna" for job in jobs))
        self.assertEqual(jobs[0]["title"], "Finance Internship")
        self.assertEqual(jobs[0]["company"], "Alpha Bank")
        self.assertEqual(jobs[0]["loc"], "Toronto, Ontario")
        self.assertEqual(jobs[0]["posted"], "2026-10-01T12:00:00Z")
        self.assertEqual(len(calls), 5)
        self.assertTrue(all("/jobs/ca/search/" in url for url, _ in calls))
        self.assertTrue(all(url.endswith("/1") for url, _ in calls))
        self.assertTrue(all(params["where"] == "Toronto" for _, params in calls))
        self.assertTrue(all(params["distance"] == 100 for _, params in calls))
        self.assertTrue(all(params["app_id"] == "test-id" for _, params in calls))
        self.assertTrue(all(params["app_key"] == "test-key" for _, params in calls))

        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["query_total"], 82)
        self.assertEqual(coverage["pages"], 5)
        self.assertEqual(coverage["request_attempts"], 5)
        self.assertEqual(coverage["returned"], 10)
        self.assertEqual(coverage["records"], 7)
        self.assertEqual(coverage["capped_terms"], 1)
        self.assertEqual(
            [query["name"] for query in coverage["queries"]],
            list(scrape.ADZUNA_QUERY_NAMES),
        )
        self.assertEqual(coverage["status"], "fresh")
        self.assertFalse(coverage["complete"])

    def test_gta_and_internship_filters_keep_only_target_roles(self):
        with (
            patch.dict(
                os.environ,
                {
                    "ADZUNA_APP_ID": "test-id",
                    "ADZUNA_APP_KEY": "test-key",
                    "ADZUNA_FORCE_REFRESH": "true",
                },
                clear=False,
            ),
            patch.object(scrape, "ADZUNA_RESULTS_PER_PAGE", 3),
            patch.object(scrape, "adzuna_queries", return_value=self.fixed_queries()),
            patch.object(
                scrape,
                "g",
                side_effect=[
                    FakeResponse(json_value=FIXTURE["queries"][name])
                    for name in scrape.ADZUNA_QUERY_NAMES
                ],
            ),
        ):
            jobs = scrape.from_adzuna(None)

        kept = {
            job["external_id"]
            for job in jobs
            if scrape.is_internship(job["title"]) and scrape.geo(job["loc"])
        }
        self.assertEqual(
            kept,
            {"adz-1", "adz-2", "adz-4", "adz-5", "adz-6", "adz-7"},
        )

    def test_http_quota_failure_is_visible_and_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.dict(
                    os.environ,
                    {
                        "ADZUNA_APP_ID": "test-id",
                        "ADZUNA_APP_KEY": "test-key",
                        "ADZUNA_FORCE_REFRESH": "true",
                        "ADZUNA_BASELINE_PATH": str(Path(temp) / "missing.json"),
                    },
                    clear=False,
                ),
                patch.object(
                    scrape,
                    "g",
                    return_value=FakeResponse(status_code=429, json_value={}),
                ),
                patch.object(
                    scrape, "adzuna_queries", return_value=self.fixed_queries()
                ),
            ):
                jobs = scrape.from_adzuna(None)

        self.assertEqual(jobs, [])
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(len(coverage["errors"]), 5)
        self.assertTrue(all("HTTP 429" in error for error in coverage["errors"]))
        self.assertFalse(coverage["complete"])

    def test_all_query_families_are_fixed_and_deterministic(self):
        queries = scrape.adzuna_queries()

        self.assertEqual(queries, scrape.adzuna_queries())
        self.assertEqual(
            [query["name"] for query in queries],
            list(scrape.ADZUNA_QUERY_NAMES),
        )
        new_grad = next(query for query in queries if query["name"] == "new-grad")
        self.assertEqual(new_grad["params"], {"what_phrase": "new grad"})
        self.assertNotIn("graduate", new_grad["params"].values())

    def test_normal_request_budget_stays_inside_official_limits(self):
        active_requests = scrape.ADZUNA_REQUESTS_PER_ACTIVE_RUN
        active_runs_per_day = 24 // scrape.ADZUNA_REFRESH_INTERVAL_HOURS
        self.assertEqual(active_requests, 5)
        self.assertEqual(active_requests * active_runs_per_day, 60)
        self.assertLessEqual(active_requests * active_runs_per_day, 250)
        self.assertLessEqual(active_requests * active_runs_per_day * 7, 1000)
        self.assertLessEqual(active_requests * active_runs_per_day * 30, 2500)
        self.assertLessEqual(active_requests * active_runs_per_day * 31, 2500)

    def test_request_budget_caps_transient_retries(self):
        budget = {"remaining": scrape.ADZUNA_REQUESTS_PER_ACTIVE_RUN}
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
        self.assertEqual(budget["remaining"], 2)

    def test_active_run_never_exceeds_five_attempts_including_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.dict(
                    os.environ,
                    {
                        "ADZUNA_APP_ID": "test-id",
                        "ADZUNA_APP_KEY": "test-key",
                        "ADZUNA_FORCE_REFRESH": "true",
                        "ADZUNA_BASELINE_PATH": str(Path(temp) / "missing.json"),
                    },
                    clear=False,
                ),
                patch.object(
                    scrape.requests,
                    "get",
                    side_effect=requests.exceptions.SSLError("tls"),
                ) as get,
                patch.object(scrape.time, "sleep"),
            ):
                jobs = scrape.from_adzuna(None, now=self.moment_with_parity(0))

        self.assertEqual(jobs, [])
        self.assertEqual(get.call_count, 5)
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["request_attempts"], 5)
        self.assertEqual(coverage["status"], "expired")

    def test_dry_run_report_contains_per_query_relevance(self):
        coverage = {
            "complete": False,
            "query_total": 82,
            "pages": 5,
            "request_attempts": 5,
            "returned": 10,
            "records": 7,
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

        self.assertEqual(report["requests"], 5)
        self.assertEqual(report["productUnique"], 6)
        self.assertEqual(report["queries"][0]["name"], "internship")

    def test_off_hour_reuses_prior_slice_without_network_or_duplicates(self):
        now = self.moment_with_parity(1)
        with tempfile.TemporaryDirectory() as temp:
            baseline = Path(temp) / "baseline.json"
            self.write_baseline(
                baseline,
                now - timedelta(hours=1),
                [self.baseline_row("prior-1"), self.baseline_row("prior-2")],
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "ADZUNA_APP_ID": "test-id",
                        "ADZUNA_APP_KEY": "test-key",
                        "ADZUNA_BASELINE_PATH": str(baseline),
                    },
                    clear=False,
                ),
                patch.object(scrape, "g") as get,
            ):
                jobs = scrape.from_adzuna(None, now=now)

        get.assert_not_called()
        self.assertEqual(
            [job["external_id"] for job in jobs],
            ["prior-1", "prior-2"],
        )
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["status"], "reused-off-hour")
        self.assertEqual(coverage["request_attempts"], 0)
        self.assertEqual(coverage["reused"], 2)
        source_state = scrape.source_state_payload()["adzuna"]
        self.assertEqual(source_state["status"], "reused-off-hour")
        self.assertEqual(source_state["reused"], 2)

    def test_partial_active_failure_reuses_fresh_prior_slice(self):
        now = self.moment_with_parity(0)
        with tempfile.TemporaryDirectory() as temp:
            baseline = Path(temp) / "baseline.json"
            self.write_baseline(baseline, now - timedelta(hours=2))
            with (
                patch.dict(
                    os.environ,
                    {
                        "ADZUNA_APP_ID": "test-id",
                        "ADZUNA_APP_KEY": "test-key",
                        "ADZUNA_BASELINE_PATH": str(baseline),
                        "ADZUNA_FORCE_REFRESH": "true",
                    },
                    clear=False,
                ),
                patch.object(
                    scrape,
                    "g",
                    return_value=FakeResponse(status_code=429, json_value={}),
                ),
            ):
                jobs = scrape.from_adzuna(None, now=now)

        self.assertEqual([job["external_id"] for job in jobs], ["prior-1"])
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["status"], "reused-partial")
        self.assertEqual(coverage["reused"], 1)
        self.assertEqual(len(coverage["errors"]), 5)

    def test_successful_active_refresh_atomically_replaces_prior_slice(self):
        now = self.moment_with_parity(0)
        with tempfile.TemporaryDirectory() as temp:
            baseline = Path(temp) / "baseline.json"
            self.write_baseline(baseline, now - timedelta(hours=2))
            responses = [
                FakeResponse(json_value=FIXTURE["queries"][name])
                for name in scrape.ADZUNA_QUERY_NAMES
            ]
            with (
                patch.dict(
                    os.environ,
                    {
                        "ADZUNA_APP_ID": "test-id",
                        "ADZUNA_APP_KEY": "test-key",
                        "ADZUNA_BASELINE_PATH": str(baseline),
                        "ADZUNA_FORCE_REFRESH": "true",
                    },
                    clear=False,
                ),
                patch.object(scrape, "ADZUNA_RESULTS_PER_PAGE", 3),
                patch.object(scrape, "g", side_effect=responses),
            ):
                jobs = scrape.from_adzuna(None, now=now)

        ids = {job["external_id"] for job in jobs}
        self.assertNotIn("prior-1", ids)
        self.assertEqual(ids, {
            "adz-1", "adz-2", "adz-3", "adz-4",
            "adz-5", "adz-6", "adz-7",
        })
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["status"], "fresh")
        self.assertEqual(coverage["reused"], 0)

    def test_expired_prior_slice_is_not_carried(self):
        now = self.moment_with_parity(1)
        with tempfile.TemporaryDirectory() as temp:
            baseline = Path(temp) / "baseline.json"
            self.write_baseline(
                baseline,
                now - timedelta(hours=scrape.ADZUNA_GRACE_HOURS + 1),
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "ADZUNA_APP_ID": "test-id",
                        "ADZUNA_APP_KEY": "test-key",
                        "ADZUNA_BASELINE_PATH": str(baseline),
                    },
                    clear=False,
                ),
                patch.object(scrape, "g") as get,
            ):
                jobs = scrape.from_adzuna(None, now=now)

        get.assert_not_called()
        self.assertEqual(jobs, [])
        coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
        self.assertEqual(coverage["status"], "expired")
        self.assertGreater(coverage["source_age_hours"], scrape.ADZUNA_GRACE_HOURS)

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
