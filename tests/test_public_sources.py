from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import scrape
from publish_snapshot import SnapshotError, build_envelope, normalize_job


class FakeResponse:
    def __init__(self, data=None, status=200):
        self.status_code = status
        self.data = data

    def json(self):
        return self.data


def lever_job(identifier, **updates):
    return {
        "id": str(identifier),
        "text": "Finance Intern",
        "categories": {"location": "Toronto, ON", "allLocations": ["Toronto, ON"]},
        "country": "CA",
        "createdAt": 1790870400000,
        "hostedUrl": f"https://jobs.lever.co/waabi/{identifier}",
        "descriptionPlain": "Provider description",
        **updates,
    }


class PublicParsingTests(unittest.TestCase):
    def setUp(self):
        scrape.reset_coverage()

    def test_lever_paginates_to_short_page_with_stable_posting_fields(self):
        responses = [FakeResponse([lever_job(1), lever_job(2)]),
                     FakeResponse([lever_job(3)])]
        with (patch.object(scrape, "LEVER_PAGE_SIZE", 2),
              patch.object(scrape, "g", side_effect=responses) as get):
            rows = scrape.from_lever("waabi")
        self.assertEqual([row["external_id"] for row in rows], ["1", "2", "3"])
        self.assertEqual([call.kwargs["params"]["skip"] for call in get.call_args_list],
                         [0, 2])
        self.assertEqual(rows[2]["url"], "https://jobs.lever.co/waabi/3")
        self.assertEqual(rows[2]["posted"], "2026-10-01T16:00:00+00:00")
        self.assertEqual(rows[2]["loc"], "Toronto, ON")
        self.assertTrue(scrape.COVERAGE["lever:waabi"]["complete"])

    def test_lever_successful_empty_is_complete(self):
        with patch.object(scrape, "g", return_value=FakeResponse([])):
            self.assertEqual(scrape.from_lever("waabi"), [])
        self.assertTrue(scrape.COVERAGE["lever:waabi"]["complete"])

    def test_lever_partial_failure_repetition_and_cap_are_incomplete(self):
        cases = [
            ([FakeResponse([lever_job(1)]), FakeResponse(status=503)], "HTTP 503"),
            ([FakeResponse([lever_job(1)]), FakeResponse([lever_job(1)])], "repeated"),
            ([FakeResponse([lever_job(1)]), FakeResponse([lever_job(2)])], "safety cap"),
            ([FakeResponse([lever_job(1)]), FakeResponse({})], "invalid jobs"),
            ([FakeResponse([lever_job(1)]), requests.Timeout("timeout")], "Timeout"),
        ]
        for responses, error in cases:
            with self.subTest(error=error):
                scrape.reset_coverage()
                with (patch.object(scrape, "LEVER_PAGE_SIZE", 1),
                      patch.object(scrape, "LEVER_MAX_PAGES", 2),
                      patch.object(scrape, "g", side_effect=responses)):
                    scrape.from_lever("waabi")
                state = scrape.COVERAGE["lever:waabi"]
                self.assertFalse(state["complete"])
                self.assertIn(error, " ".join(state["errors"]))
                self.assertEqual(state["capped_terms"], int(error == "safety cap"))

    def test_lever_secondary_canadian_location_does_not_admit_us_only_remote(self):
        data = [
            lever_job(1, country="US", categories={
                "location": "San Francisco, CA",
                "allLocations": ["San Francisco, CA", "Toronto, ON"],
            }),
            lever_job(2, country="US", categories={
                "location": "Remote, US", "allLocations": ["Remote, US"],
            }),
            lever_job(3, country="US", categories={
                "location": "Ontario, CA", "allLocations": ["Ontario, CA"],
            }),
        ]
        with patch.object(scrape, "g", return_value=FakeResponse(data)):
            rows = scrape.from_lever("waabi")
        eligible, _ = scrape.filter_candidates(rows)
        self.assertEqual([row["external_id"] for row in eligible], ["1"])

    def test_ashby_requires_listed_jobs_and_checks_secondary_locations(self):
        job = {
            "id": "1", "isListed": True, "title": "Finance Intern",
            "location": "San Francisco, California",
            "secondaryLocations": [{"location": "Toronto, Ontario",
                                    "address": {"addressCountry": "CAN"}}],
            "jobUrl": "https://jobs.ashbyhq.com/wealthsimple/1",
            "publishedAt": "2026-10-01T00:00:00Z",
            "descriptionPlain": "Actual provider description",
        }
        with patch.object(scrape, "g", return_value=FakeResponse(
            {"jobs": [job, {**job, "id": "2", "isListed": False}]}
        )):
            rows = scrape.from_ashby("wealthsimple")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["loc"], "Toronto, Ontario")
        self.assertEqual(rows[0]["content"], "Actual provider description")
        self.assertTrue(scrape.COVERAGE["ashby:wealthsimple"]["complete"])

    def test_ashby_structured_country_excludes_us_only_location(self):
        job = {
            "id": "1", "isListed": True, "title": "Finance Intern",
            "location": "Ontario", "workplaceType": "Remote",
            "address": {"postalAddress": {"addressCountry": "USA"}},
            "jobUrl": "https://jobs.ashbyhq.com/wealthsimple/1",
        }
        with patch.object(scrape, "g", return_value=FakeResponse({"jobs": [job]})):
            rows = scrape.from_ashby("wealthsimple")
        self.assertEqual(scrape.filter_candidates(rows)[0], [])

    def test_malformed_or_truncated_responses_never_report_complete_empty(self):
        cases = [
            (scrape.from_greenhouse, "internshiplist2000", {}),
            (scrape.from_greenhouse, "internshiplist2000",
             {"jobs": [], "meta": {"total": 1}}),
            (scrape.from_greenhouse, "internshiplist2000",
             {"jobs": [{"title": "Finance Intern"}]}),
            (scrape.from_ashby, "wealthsimple", {"jobs": None}),
            (scrape.from_ashby, "wealthsimple", {"jobs": [{"id": "1"}]}),
            (scrape.from_lever, "waabi", [{"id": "1"}]),
        ]
        for adapter, board, data in cases:
            with self.subTest(adapter=adapter.__name__, data=data):
                scrape.reset_coverage()
                with patch.object(scrape, "g", return_value=FakeResponse(data)):
                    adapter(board)
                self.assertFalse(next(iter(scrape.COVERAGE.values()))["complete"])

    def test_greenhouse_excludes_prospect_posts_without_guessing_from_title(self):
        job = {
            "id": 1, "internal_job_id": 100, "title": "Finance Intern",
            "location": {"name": "Toronto, Ontario"},
            "absolute_url": "https://job-boards.greenhouse.io/internshiplist2000/jobs/1",
        }
        data = {"jobs": [job, {**job, "id": 2, "internal_job_id": None}],
                "meta": {"total": 2}}
        with patch.object(scrape, "g", return_value=FakeResponse(data)):
            rows = scrape.from_greenhouse("internshiplist2000")
        self.assertEqual([row["external_id"] for row in rows], ["1"])
        self.assertTrue(scrape.COVERAGE["greenhouse:internshiplist2000"]["complete"])

    def test_public_eligibility_keeps_scope_without_new_grad_or_pool_padding(self):
        cases = [
            ("Finance Intern", "Toronto, ON", True),
            ("Marketing Co-op", "Oakville, Ontario", True),
            ("Legal Summer Student", "Toronto, Ontario", True),
            ("Senior Finance Manager", "Toronto, ON", False),
            ("International Accountant", "Toronto, ON", False),
            ("New Graduate Analyst", "Toronto, ON", False),
            ("Internship Talent Community", "Toronto, ON", False),
            ("Finance Intern", "Remote US", False),
            ("Finance Intern", "Remote Canada", False),
            ("Finance Intern", "Ontario, California", False),
        ]
        for title, location, expected in cases:
            with self.subTest(title=title, location=location):
                row = {"title": title, "loc": scrape.public_location([location]),
                       "source_target": "wealthsimple", "url": "https://example.com/1"}
                candidates, _ = scrape.filter_candidates([row])
                self.assertEqual(bool(candidates), expected)


class PublicCacheTests(unittest.TestCase):
    now = datetime(2026, 10, 4, 21, tzinfo=timezone.utc)
    spec = ("greenhouse", "internshiplist2000", "Geotab")
    key = "greenhouse:internshiplist2000"

    def setUp(self):
        scrape.reset_coverage()

    def prior_row(self, refreshed):
        return {
            "company": "Geotab", "title": "Finance Intern", "loc": "Toronto, ON",
            "src": "greenhouse", "external_id": "internshiplist2000:123",
            "url": "https://job-boards.greenhouse.io/internshiplist2000/jobs/123",
            "source_target": "internshiplist2000", "last_seen": refreshed.isoformat(),
            "posted": "", "cats": ["Finance"],
        }

    def write_baseline(self, path, refreshed):
        path.write_text(json.dumps({
            "fetched_at": self.now.isoformat(),
            "jobs": [self.prior_row(refreshed)],
            "source_state": {"public_boards": {
                self.key: {"status": "fresh", "refreshed_at": refreshed.isoformat()}
            }},
        }), encoding="utf-8")

    def failed_fetch(self, _):
        scrape.set_coverage(self.key, complete=False, errors=["HTTP 503"])
        return [{"title": "Untrusted partial page"}]

    def test_incomplete_fetch_reuses_only_bounded_last_good_slice(self):
        for age, expected_status in [(2, "reused-incomplete"), (6, "reused-incomplete"),
                                     (6.01, "blocked"), (-1, "blocked")]:
            with self.subTest(age=age), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "baseline.json"
                refreshed = self.now - timedelta(hours=age)
                self.write_baseline(path, refreshed)
                with (patch.dict(os.environ, {"SOURCE_BASELINE_PATH": str(path)}),
                      patch.object(scrape, "from_greenhouse", side_effect=self.failed_fetch),
                      redirect_stdout(io.StringIO())):
                    rows = scrape.from_public_board(self.spec, now=self.now)
                state = scrape.source_state_payload()["public_boards"][self.key]
                self.assertEqual(state["status"], expected_status)
                self.assertEqual(state["refreshed_at"], refreshed.isoformat())
                self.assertFalse(state["complete"])
                self.assertEqual(len(rows), int(expected_status == "reused-incomplete"))
                if rows:
                    self.assertEqual(rows[0]["last_seen"], refreshed.isoformat())
                    self.assertEqual(rows[0]["external_id"], "internshiplist2000:123")

    def test_incomplete_first_fetch_is_blocked_not_successful_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            with (patch.dict(os.environ, {
                "SOURCE_BASELINE_PATH": str(Path(temp) / "missing.json")
            }), patch.object(scrape, "from_greenhouse", side_effect=self.failed_fetch),
                  redirect_stdout(io.StringIO())):
                rows = scrape.from_public_board(self.spec, now=self.now)
        self.assertEqual(rows, [])
        self.assertEqual(scrape.COVERAGE[self.key]["status"], "blocked")

    def test_successful_empty_replaces_cached_jobs_immediately(self):
        def empty(_):
            scrape.set_coverage(self.key, complete=True, errors=[])
            return []
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            self.write_baseline(path, self.now - timedelta(hours=1))
            with (patch.dict(os.environ, {"SOURCE_BASELINE_PATH": str(path)}),
                  patch.object(scrape, "from_greenhouse", side_effect=empty)):
                self.assertEqual(scrape.from_public_board(self.spec, now=self.now), [])
        self.assertEqual(scrape.COVERAGE[self.key]["status"], "fresh")
        self.assertEqual(scrape.COVERAGE[self.key]["source_refreshed_at"],
                         self.now.isoformat())

    def test_invalid_cached_deadline_blocks_instead_of_silently_dropping_row(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            self.write_baseline(path, self.now - timedelta(hours=1))
            data = json.loads(path.read_text(encoding="utf-8"))
            data["jobs"][0]["application_deadline"] = "invalid"
            path.write_text(json.dumps(data), encoding="utf-8")
            with (patch.dict(os.environ, {"SOURCE_BASELINE_PATH": str(path)}),
                  patch.object(scrape, "from_greenhouse", side_effect=self.failed_fetch),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(scrape.from_public_board(self.spec, now=self.now), [])
        self.assertEqual(scrape.COVERAGE[self.key]["status"], "blocked")

    def test_success_adds_board_qualified_identity_and_last_seen(self):
        def success(_):
            scrape.set_coverage(self.key, complete=True, errors=[])
            return [{**self.prior_row(self.now), "external_id": "123"}]
        with patch.object(scrape, "from_greenhouse", side_effect=success):
            rows = scrape.from_public_board(self.spec, now=self.now)
        self.assertEqual(rows[0]["external_id"], "internshiplist2000:123")
        self.assertEqual(rows[0]["last_seen"], self.now.isoformat())
        envelope, _ = build_envelope({"fetched_at": self.now.isoformat(), "jobs": rows})
        normalized = envelope["jobs"][0]
        self.assertEqual(normalized["externalId"], "internshiplist2000:123")
        self.assertNotIn("last_seen", normalized)
        self.assertNotIn("source_target", normalized)

    def test_known_deadline_filters_expired_postings(self):
        def success(_):
            scrape.set_coverage(self.key, complete=True, errors=[])
            return [
                {**self.prior_row(self.now), "external_id": "123",
                 "application_deadline": self.now.isoformat()},
                {**self.prior_row(self.now), "external_id": "124",
                 "application_deadline": (self.now + timedelta(days=1)).isoformat()},
            ]
        with patch.object(scrape, "from_greenhouse", side_effect=success):
            rows = scrape.from_public_board(self.spec, now=self.now)
        self.assertEqual([row["external_id"] for row in rows], ["internshiplist2000:124"])

    def test_pipeline_retains_metadata_without_changing_consumer_contract(self):
        record = {**self.prior_row(self.now), "content": "Provider description"}
        candidates, _ = scrape.filter_candidates([record])
        serialized = scrape.snapshot_job(candidates[0])
        self.assertEqual(serialized["last_seen"], self.now.isoformat())
        self.assertEqual(serialized["source_target"], "internshiplist2000")
        envelope, body = build_envelope({
            "fetched_at": self.now.isoformat(), "jobs": [serialized],
        })
        self.assertEqual(envelope["schemaVersion"], 1)
        self.assertEqual(set(envelope["jobs"][0]), {
            "id", "externalId", "source", "company", "title", "location", "region",
            "categories", "url", "postedAt", "postedText", "description",
        })
        self.assertLess(len(body), 2 * 1024 * 1024)

    def test_inconclusive_url_check_blocks_but_explicit_closure_does_not(self):
        record = self.prior_row(self.now)
        for reason in ["HTTP 403", "HTTP 429", "HTTP 503", "error Timeout",
                       "HTTP 404", "HTTP 410", "closed text"]:
            with self.subTest(reason=reason):
                scrape.set_coverage(self.key, status="fresh", complete=True, errors=[])
                with redirect_stdout(io.StringIO()):
                    scrape.public_verification_failure(record, reason)
                blocked = reason not in {"HTTP 404", "HTTP 410", "closed text"}
                self.assertEqual(scrape.COVERAGE[self.key]["status"] == "blocked", blocked)


class PublicIdentityTests(unittest.TestCase):
    def test_new_greenhouse_ids_are_board_scoped_without_changing_legacy_ids(self):
        base = {
            "src": "greenhouse", "company": "Example", "title": "Finance Intern",
            "loc": "Toronto", "cats": [],
        }
        one = normalize_job({**base, "external_id": "one:123",
                             "url": "https://job-boards.greenhouse.io/one/jobs/123"})
        two = normalize_job({**base, "external_id": "two:123",
                             "url": "https://job-boards.greenhouse.io/two/jobs/123"})
        legacy = normalize_job({**base, "external_id": "123",
                                "url": "https://job-boards.greenhouse.io/one/jobs/123"})
        self.assertEqual(len({one["id"], two["id"], legacy["id"]}), 3)
        self.assertEqual(legacy["externalId"], "123")
        with self.assertRaises(SnapshotError):
            normalize_job({**base, "external_id": "wrong:123",
                           "url": "https://job-boards.greenhouse.io/one/jobs/123"})

    def test_duplicate_official_url_is_not_counted_again(self):
        base = {
            "company": "Example", "title": "Finance Intern", "loc": "Toronto",
            "url": "https://jobs.lever.co/example/123", "src": "lever",
            "external_id": "example:123", "source_target": "example",
        }
        mirrored = {**base, "src": "other", "external_id": "other:456",
                    "url": base["url"] + "/apply?utm_source=feed"}
        unique, _, _ = scrape.dedupe_candidates([base, mirrored])
        self.assertEqual(len(unique), 1)

    def test_direct_source_replaces_matching_aggregate_not_new_opportunity(self):
        direct = {
            "company": "Wealthsimple", "title": "Finance Intern", "loc": "Toronto, ON",
            "src": "ashby", "source_target": "wealthsimple",
            "external_id": "wealthsimple:1", "url": "https://jobs.ashbyhq.com/wealthsimple/1",
        }
        aggregate = {**direct, "src": "adzuna", "source_target": None,
                     "external_id": "2", "url": "https://www.adzuna.ca/details/2",
                     "loc": "Toronto, Ontario"}
        unique, _, _ = scrape.dedupe_candidates([aggregate, direct])
        self.assertEqual(unique, [direct])


if __name__ == "__main__":
    unittest.main()
