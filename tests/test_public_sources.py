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


def smartrecruiters_job(identifier, **updates):
    return {
        "id": str(identifier),
        "name": "Finance Intern",
        "company": {"identifier": "GDMSI", "name": "GDMS Canada"},
        "releasedDate": "2026-10-01T12:00:00Z",
        "location": {
            "city": "Ottawa", "region": "ON", "country": "ca",
            "fullLocation": "Ottawa, ON, Canada", "remote": False,
        },
        "typeOfEmployment": {"id": "intern", "label": "Intern"},
        "visibility": "PUBLIC",
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

    def test_smartrecruiters_paginates_and_validates_company_country(self):
        responses = [
            FakeResponse({
                "totalFound": 3,
                "content": [smartrecruiters_job(1), smartrecruiters_job(2)],
            }),
            FakeResponse({
                "totalFound": 3,
                "content": [smartrecruiters_job(3)],
            }),
        ]
        with (
            patch.object(scrape, "SMARTRECRUITERS_PAGE_SIZE", 2),
            patch.object(scrape, "g", side_effect=responses) as get,
        ):
            rows = scrape.from_smartrecruiters("gdmsi")

        self.assertEqual([row["external_id"] for row in rows], ["1", "2", "3"])
        self.assertEqual(
            [call.kwargs["params"]["offset"] for call in get.call_args_list],
            [0, 2],
        )
        self.assertEqual(
            rows[0]["url"],
            "https://jobs.smartrecruiters.com/GDMSI/1",
        )
        self.assertEqual(rows[0]["loc"], "Ottawa, ON, Canada")
        self.assertTrue(scrape.COVERAGE["smartrecruiters:gdmsi"]["complete"])

    def test_smartrecruiters_rejects_non_canada_and_identity_mismatch(self):
        non_canada = smartrecruiters_job(
            1,
            location={"city": "Boston", "region": "MA", "country": "us",
                      "fullLocation": "Boston, MA, United States"},
        )
        with patch.object(scrape, "g", return_value=FakeResponse({
            "totalFound": 1, "content": [non_canada],
        })):
            self.assertEqual(scrape.from_smartrecruiters("gdmsi"), [])
        self.assertTrue(scrape.COVERAGE["smartrecruiters:gdmsi"]["complete"])

        mismatch = smartrecruiters_job(
            2, company={"identifier": "OTHER", "name": "Other"},
        )
        with patch.object(scrape, "g", return_value=FakeResponse({
            "totalFound": 1, "content": [mismatch],
        })):
            scrape.from_smartrecruiters("gdmsi")
        self.assertFalse(scrape.COVERAGE["smartrecruiters:gdmsi"]["complete"])

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
            (scrape.from_greenhouse, "coveoen", {}),
            (scrape.from_greenhouse, "coveoen",
             {"jobs": [], "meta": {"total": 1}}),
            (scrape.from_greenhouse, "coveoen",
             {"jobs": [{"title": "Finance Intern"}]}),
            (scrape.from_ashby, "wealthsimple", {"jobs": None}),
            (scrape.from_ashby, "wealthsimple", {"jobs": [{"id": "1"}]}),
            (scrape.from_lever, "waabi", [{"id": "1"}]),
            (scrape.from_smartrecruiters, "gdmsi", {"content": []}),
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
            "absolute_url": "https://job-boards.greenhouse.io/coveoen/jobs/1",
            "updated_at": "2026-10-09T12:00:00Z",
        }
        data = {"jobs": [job, {**job, "id": 2, "internal_job_id": None}],
                "meta": {"total": 2}}
        with patch.object(scrape, "g", return_value=FakeResponse(data)):
            rows = scrape.from_greenhouse("coveoen")
        self.assertEqual([row["external_id"] for row in rows], ["1"])
        self.assertEqual(rows[0]["posted"], "")
        self.assertTrue(scrape.COVERAGE["greenhouse:coveoen"]["complete"])

    def test_greenhouse_missing_list_date_uses_detail_first_published_only(self):
        list_job = {
            "id": 1, "internal_job_id": 100, "title": "Finance Intern",
            "location": {"name": "Toronto, Ontario"},
            "absolute_url": "https://job-boards.greenhouse.io/coveoen/jobs/1",
            "updated_at": "2026-10-09T12:00:00Z",
        }
        detail_job = {
            "id": 1,
            "first_published": "2026-09-01T08:00:00-04:00",
            "updated_at": "2026-10-09T12:00:00Z",
        }
        with patch.object(
            scrape,
            "g",
            side_effect=[
                FakeResponse({"jobs": [list_job], "meta": {"total": 1}}),
                FakeResponse(detail_job),
            ],
        ):
            rows = scrape.from_greenhouse("coveoen")
            stats = scrape.hydrate_greenhouse_posted_dates(rows)

        self.assertEqual(rows[0]["posted"], "2026-09-01T08:00:00-04:00")
        self.assertEqual(
            stats,
            {"requested": 1, "resolved": 1, "unavailable": 0, "capped": 0},
        )

    def test_greenhouse_missing_first_published_remains_unavailable(self):
        row = {
            "src": "greenhouse",
            "_greenhouse_board": "coveoen",
            "external_id": "1",
            "posted": "",
        }
        with patch.object(
            scrape,
            "g",
            return_value=FakeResponse(
                {"id": 1, "updated_at": "2026-10-09T12:00:00Z"}
            ),
        ):
            stats = scrape.hydrate_greenhouse_posted_dates([row])

        self.assertEqual(row["posted"], "")
        self.assertEqual(stats["unavailable"], 1)

    def test_software_engineering_track_uses_title_evidence(self):
        positives = [
            "Software Engineer Intern",
            "Software/Application Developer Co-op",
            "Backend Developer Intern",
            "SWE Intern",
            "SDE Intern",
            "Full Stack Developer (Student Co-op)",
            "Systems Development Engineer Intern",
            "AI Software Intern",
            "Quantitative Developer - Summer Internship",
            "Développeur Logiciels (Stagiaire)",
            "Stagiaire en développement de logiciels",
        ]
        negatives = [
            "Mobile Domain System Engineering Co-op",
            "Machine Learning Intern",
            "DevOps Intern",
            "Firmware Engineering Intern",
            "Business Development Intern",
            "Product Development Intern",
            "Crop Protection Development Assistant Intern",
            "Developer Relations Intern",
            "Software Sales Intern",
            "Engineering Intern",
        ]

        for title in positives:
            with self.subTest(title=title):
                self.assertIn(
                    scrape.SOFTWARE_ENGINEERING_TRACK,
                    scrape.categorize(title),
                )
        for title in negatives:
            with self.subTest(title=title):
                self.assertNotIn(
                    scrape.SOFTWARE_ENGINEERING_TRACK,
                    scrape.categorize(title),
                )

    def test_software_word_does_not_hide_non_engineering_track(self):
        self.assertEqual(
            scrape.categorize("Software Product Marketing Intern"),
            ["Marketing"],
        )
        self.assertEqual(scrape.categorize("Finance Intern"), ["Finance"])

    def test_technical_category_vocabulary_and_overlapping_fixtures(self):
        expected_vocabulary = (
            "Software Engineering",
            "Data Science & AI",
            "Cybersecurity",
            "IT & Infrastructure",
            "Quality & Test Engineering",
            "Hardware & Embedded Systems",
            "Electrical & Electronics Engineering",
            "Mechanical, Mechatronics & Robotics",
            "Civil & Structural Engineering",
            "Environmental Engineering",
            "Chemical, Process & Materials Engineering",
            "Industrial & Manufacturing Engineering",
            "Aerospace Engineering",
            "Biomedical Engineering",
            "Mining & Geological Engineering",
            "General Engineering",
        )
        self.assertEqual(scrape.TECHNICAL_CATEGORY_NAMES, expected_vocabulary)

        fixtures = {
            "Software Engineer Intern, Test Automation": [
                "Software Engineering",
                "Quality & Test Engineering",
            ],
            "Data Engineer Intern": ["Data Science & AI"],
            "Browser Security Engineer Intern": ["Cybersecurity"],
            "DevOps & Cloud Platform Intern": ["IT & Infrastructure"],
            "Embedded Software Engineering Intern": [
                "Software Engineering",
                "Hardware & Embedded Systems",
            ],
            "Firmware Engineering Co-op": ["Hardware & Embedded Systems"],
            "Intern - Mechatronics Engineer": [
                "Electrical & Electronics Engineering",
                "Mechanical, Mechatronics & Robotics",
            ],
            "Civil/Transportation Engineering Intern/Co-op": [
                "Civil & Structural Engineering",
            ],
            "Environmental Compliance Intern": [
                "Environmental Engineering",
            ],
            "Environment, Health & Safety Intern": [
                "Environmental Engineering",
            ],
            "Process Engineer Co-op": [
                "Chemical, Process & Materials Engineering",
            ],
            "Manufacturing Engineering Co-op": [
                "Industrial & Manufacturing Engineering",
            ],
            "Applied Research Intern (NLP/ML/GenAI)": [
                "Data Science & AI",
            ],
            "Systems Integration, Verification and Validation Co-op": [
                "Quality & Test Engineering",
                "General Engineering",
            ],
            "Student, Engineering": ["General Engineering"],
            "Nuclear Canadian Licensing Intern": ["General Engineering"],
            "Data Quality Intern": ["Data Science & AI"],
            "Intern, Quality Assurance": ["Quality & Test Engineering"],
            "AMS Layout Engineer Intern": [
                "Hardware & Embedded Systems",
                "Electrical & Electronics Engineering",
            ],
            "IC Validation Engineer Intern": [
                "Quality & Test Engineering",
                "Hardware & Embedded Systems",
            ],
            "Sales & Distribution Operations Intern": [
                "Business Operations",
            ],
            "CFO Process Re-Engineering Intern": [],
            "Business Analyst Student": ["Business Operations"],
        }
        for title, expected in fixtures.items():
            with self.subTest(title=title):
                self.assertEqual(scrape.categorize(title), expected)

    def test_employer_industry_does_not_infer_technical_track(self):
        self.assertEqual(
            scrape.categorize("Mechanical Engineering Intern"),
            ["Mechanical, Mechatronics & Robotics"],
        )
        self.assertNotIn(
            "Aerospace Engineering",
            scrape.categorize("Mechanical Engineering Intern"),
        )
        self.assertEqual(
            scrape.categorize("Intern, Technical Sales & Consulting"),
            ["Consulting"],
        )

    def test_public_eligibility_keeps_scope_without_pool_or_remote_padding(self):
        cases = [
            ("Finance Intern", "Toronto, ON", True),
            ("Marketing Co-op", "Vancouver, British Columbia", True),
            ("Legal Summer Student", "Halifax, Nova Scotia", True),
            ("Senior Finance Manager", "Toronto, ON", False),
            ("International Accountant", "Toronto, ON", False),
            ("New Graduate Analyst", "Calgary, Alberta", False),
            ("Product Manager (New Grad)", "Toronto, Ontario", False),
            ("Senior Product Manager - New Grad", "Toronto, Ontario", False),
            ("Internship Talent Community", "Toronto, ON", False),
            ("2027 Intern Program - Future Opportunities", "Toronto, ON", False),
            ("Finance Intern", "Remote US", False),
            ("Finance Intern", "Remote Canada", True),
            ("Finance Intern", "Remote (US + Canada Only)", True),
            ("Finance Intern", "Remote", False),
            ("Finance Intern", "Ontario, California", False),
        ]
        for title, location, expected in cases:
            with self.subTest(title=title, location=location):
                row = {"title": title, "loc": scrape.public_location([location]),
                       "source_target": "wealthsimple", "url": "https://example.com/1"}
                candidates, _ = scrape.filter_candidates([row])
                self.assertEqual(bool(candidates), expected)

        baseline_new_grad = {
            "title": "New Graduate Analyst",
            "loc": "Calgary, Alberta",
            "url": "https://example.com/baseline-new-grad",
            "_baseline_existing": True,
        }
        candidates, _ = scrape.filter_candidates([baseline_new_grad])
        self.assertEqual(len(candidates), 1)
        new_new_grad = {
            "title": "New Graduate Analyst",
            "loc": "Calgary, Alberta",
            "url": "https://example.com/new-new-grad",
        }
        self.assertEqual(scrape.filter_candidates([new_new_grad])[0], [])

    def test_baseline_marker_preserves_existing_ids_only(self):
        rows = [
            {
                "src": "workday", "external_id": "cibc:1",
                "title": "New Graduate Analyst", "loc": "Toronto, Ontario",
                "url": "https://example.com/1",
            },
            {
                "src": "workday", "external_id": "cibc:2",
                "title": "New Graduate Analyst", "loc": "Toronto, Ontario",
                "url": "https://example.com/2",
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            baseline = Path(temp) / "baseline.json"
            baseline.write_text(json.dumps({"jobs": [rows[0]]}), encoding="utf-8")
            with patch.dict(os.environ, {"SOURCE_BASELINE_PATH": str(baseline)}):
                scrape.mark_baseline_records(rows)

        candidates, _ = scrape.filter_candidates(rows)
        self.assertEqual([row["external_id"] for row in candidates], ["cibc:1"])

    def test_canada_geography_covers_regions_and_rejects_us_namesakes(self):
        cases = {
            "Edmonton, AB": "Alberta",
            "Vancouver, British Columbia": "British Columbia",
            "Winnipeg, Manitoba": "Manitoba",
            "Fredericton, New Brunswick": "New Brunswick",
            "St. John's, Newfoundland and Labrador": "Newfoundland and Labrador",
            "Halifax, Nova Scotia": "Nova Scotia",
            "Toronto, Ontario": "Ontario",
            "Charlottetown, Prince Edward Island": "Prince Edward Island",
            "Montreal, Quebec": "Quebec",
            "Saskatoon, SK": "Saskatchewan",
            "Yellowknife, Northwest Territories": "Northwest Territories",
            "Iqaluit, Nunavut": "Nunavut",
            "Whitehorse, Yukon": "Yukon",
            "Remote (US + Canada Only)": "Remote - Canada",
            "Victoria, British Columbia, Canada": "British Columbia",
            "Ontario, California": None,
            "Port Melbourne, Victoria, Australia": None,
            "Paris, France": None,
            "Remote, United States": None,
            "Remote": None,
        }
        for location, expected in cases.items():
            with self.subTest(location=location):
                self.assertEqual(scrape.geo(location), expected)

    def test_ashby_nested_secondary_canada_location_is_eligible(self):
        job = {
            "id": "1", "isListed": True, "title": "Software Intern",
            "location": "San Francisco, California",
            "address": {"postalAddress": {"addressCountry": "United States"}},
            "secondaryLocations": [{
                "location": "Toronto, Ontario",
                "address": {"postalAddress": {
                    "addressCountry": "Canada", "addressRegion": "Ontario",
                    "addressLocality": "Toronto",
                }},
            }],
            "employmentType": "Intern",
            "jobUrl": "https://jobs.ashbyhq.com/bree/1",
        }
        with patch.object(scrape, "g", return_value=FakeResponse({"jobs": [job]})):
            rows = scrape.from_ashby("bree")
        candidates, _ = scrape.filter_candidates(rows)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["geo"], "Ontario")

    def test_ashby_multi_location_keeps_explicit_canadian_city(self):
        job = {
            "id": "1", "isListed": True, "title": "Product Management Intern",
            "location": "New York, London, Toronto, San Francisco",
            "address": {"postalAddress": {"addressCountry": "United States"}},
            "secondaryLocations": [],
            "employmentType": "FullTime",
            "jobUrl": "https://jobs.ashbyhq.com/uncountable/1",
        }
        with patch.object(scrape, "g", return_value=FakeResponse({"jobs": [job]})):
            rows = scrape.from_ashby("uncountable")
        candidates, _ = scrape.filter_candidates(rows)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["loc"], "Toronto, Ontario, Canada")

    def test_public_registry_has_verified_native_and_campus_boards(self):
        boards = {(source, board) for source, board, _ in scrape.PUBLIC_BOARDS}
        for expected in {
            ("greenhouse", "internshiplist2000"),
            ("lever", "eqbank"), ("lever", "arcteryx.com"),
            ("ashby", "bree"), ("ashby", "trulioo-coopandinterns"),
            ("greenhouse", "coveoen"), ("greenhouse", "momentenergy"),
            ("smartrecruiters", "gdmsi"),
        }:
            self.assertIn(expected, boards)

    def test_filter_funnel_reports_role_and_canada_counts(self):
        rows = [
            {"title": "Finance Intern", "loc": "Toronto, Ontario",
             "url": "https://example.com/1"},
            {"title": "Finance Intern", "loc": "New York, NY",
             "url": "https://example.com/2"},
            {"title": "Senior Finance Manager", "loc": "Calgary, Alberta",
             "url": "https://example.com/3"},
        ]

        candidates, _ = scrape.filter_candidates(rows)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(scrape.FILTER_FUNNEL, {
            "raw_active_listed": 3,
            "role_eligible": 2,
            "canada_eligible": 1,
        })

    def test_public_board_url_identity_validation(self):
        self.assertTrue(scrape.public_board_url_matches(
            "lever", "eqbank", "https://jobs.lever.co/eqbank/abc", "abc"))
        self.assertTrue(scrape.public_board_url_matches(
            "ashby", "Superhuman Platform Inc",
            "https://jobs.ashbyhq.com/Superhuman%20Platform%20Inc/abc", "abc"))
        self.assertTrue(scrape.public_board_url_matches(
            "greenhouse", "coveoen",
            "https://www.coveo.com/careers?gh_jid=123", "123"))
        self.assertTrue(scrape.public_board_url_matches(
            "smartrecruiters", "gdmsi",
            "https://jobs.smartrecruiters.com/GDMSI/123", "123"))
        self.assertFalse(scrape.public_board_url_matches(
            "lever", "eqbank", "https://jobs.lever.co/other/abc", "abc"))


class PublicCacheTests(unittest.TestCase):
    now = datetime(2026, 10, 4, 21, tzinfo=timezone.utc)
    spec = ("greenhouse", "coveoen", "Coveo")
    key = "greenhouse:coveoen"

    def setUp(self):
        scrape.reset_coverage()

    def prior_row(self, refreshed):
        return {
            "company": "Coveo", "title": "Finance Intern", "loc": "Montreal, Quebec",
            "src": "greenhouse", "external_id": "coveoen:123",
            "url": "https://job-boards.greenhouse.io/coveoen/jobs/123",
            "source_target": "coveoen", "last_seen": refreshed.isoformat(),
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
                    self.assertEqual(rows[0]["external_id"], "coveoen:123")

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
            return [{
                **self.prior_row(self.now), "external_id": "123",
                "url": "https://www.coveo.com/careers?gh_jid=123",
            }]
        with patch.object(scrape, "from_greenhouse", side_effect=success):
            rows = scrape.from_public_board(self.spec, now=self.now)
        self.assertEqual(rows[0]["external_id"], "coveoen:123")
        self.assertEqual(rows[0]["last_seen"], self.now.isoformat())
        self.assertEqual(
            rows[0]["url"],
            "https://job-boards.greenhouse.io/coveoen/jobs/123",
        )
        envelope, _ = build_envelope({"fetched_at": self.now.isoformat(), "jobs": rows})
        normalized = envelope["jobs"][0]
        self.assertEqual(normalized["externalId"], "coveoen:123")
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
        self.assertEqual([row["external_id"] for row in rows], ["coveoen:124"])

    def test_pipeline_retains_metadata_without_changing_consumer_contract(self):
        record = {**self.prior_row(self.now), "content": "Provider description"}
        candidates, _ = scrape.filter_candidates([record])
        serialized = scrape.snapshot_job(candidates[0])
        self.assertEqual(serialized["last_seen"], self.now.isoformat())
        self.assertEqual(serialized["source_target"], "coveoen")
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

    def test_verified_public_greenhouse_board_accepts_runner_http_202(self):
        response = type("Response", (), {
            "status_code": 202,
            "text": "",
        })()
        record = {
            "url": "https://job-boards.greenhouse.io/coveoen/jobs/123",
            "src": "greenhouse",
            "source_target": "coveoen",
        }
        with patch.object(scrape.requests, "get", return_value=response):
            _, ok, reason = scrape.verify(record)

        self.assertTrue(ok)
        self.assertEqual(reason, "202")


class RetainedSourceCacheTests(unittest.TestCase):
    now = datetime(2026, 10, 4, 21, tzinfo=timezone.utc)
    spec = ("workday", ("autodesk", "uni", "wd1"), "Autodesk")
    key = "workday:autodesk.wd1/uni"
    target = "autodesk.wd1/uni"

    def setUp(self):
        scrape.reset_coverage()

    def prior_row(self, refreshed):
        return {
            "company": "Autodesk",
            "title": "Software Development Intern",
            "loc": "Toronto, Ontario, Canada",
            "src": "workday",
            "external_id": "autodesk:R123",
            "url": (
                "https://autodesk.wd1.myworkdayjobs.com/en-US/uni/job/"
                "Toronto-Ontario-Canada/Software-Development-Intern_R123"
            ),
            "source_target": self.target,
            "last_seen": refreshed.isoformat(),
            "posted": "Posted 2 Days Ago",
            "cats": ["Software Engineering"],
        }

    def write_baseline(self, path, refreshed):
        path.write_text(json.dumps({
            "fetched_at": self.now.isoformat(),
            "jobs": [self.prior_row(refreshed)],
            "source_state": {"public_boards": {
                self.key: {
                    "status": "fresh",
                    "refreshed_at": refreshed.isoformat(),
                }
            }},
        }), encoding="utf-8")

    def test_success_adds_display_company_and_retention_metadata(self):
        def success(_):
            scrape.set_coverage(self.key, complete=True, errors=[])
            row = self.prior_row(self.now)
            row.pop("source_target")
            row.pop("last_seen")
            row["company"] = "autodesk"
            return [row]

        with patch.object(scrape, "from_workday", side_effect=success):
            rows = scrape.from_retained_source(self.spec, now=self.now)

        self.assertEqual(rows[0]["company"], "Autodesk")
        self.assertEqual(rows[0]["source_target"], self.target)
        self.assertEqual(rows[0]["last_seen"], self.now.isoformat())
        self.assertEqual(scrape.COVERAGE[self.key]["status"], "fresh")

    def test_incomplete_fetch_reuses_only_bounded_last_good_slice(self):
        def failed(_):
            scrape.set_coverage(
                self.key,
                complete=False,
                errors=["HTTP 503"],
            )
            return []

        for age, expected in [(2, 1), (6.01, 0)]:
            with self.subTest(age=age), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "baseline.json"
                self.write_baseline(path, self.now - timedelta(hours=age))
                with (
                    patch.dict(
                        os.environ,
                        {"SOURCE_BASELINE_PATH": str(path)},
                    ),
                    patch.object(scrape, "from_workday", side_effect=failed),
                    redirect_stdout(io.StringIO()),
                ):
                    rows = scrape.from_retained_source(
                        self.spec,
                        now=self.now,
                    )
                self.assertEqual(len(rows), expected)
                self.assertEqual(
                    scrape.COVERAGE[self.key]["status"],
                    "reused-incomplete" if expected else "blocked",
                )


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

    def test_smartrecruiters_id_is_stable_and_board_scoped(self):
        normalized = normalize_job({
            "src": "smartrecruiters",
            "company": "GDMS Canada",
            "title": "Finance Intern",
            "loc": "Ottawa, ON, Canada",
            "cats": ["Finance"],
            "external_id": "gdmsi:7440001",
            "url": "https://jobs.smartrecruiters.com/GDMSI/7440001",
        })

        self.assertEqual(normalized["externalId"], "gdmsi:7440001")

    def test_dedupe_prefers_baseline_identity_over_new_duplicate_source(self):
        baseline = {
            "src": "workday",
            "company": "bmo",
            "title": "Finance Co-op",
            "loc": "Toronto, Ontario",
            "url": (
                "https://bmo.wd3.myworkdayjobs.com/en-US/External/job/"
                "Toronto-Ontario/Finance-Co-op_R123-3"
            ),
            "external_id": "bmo:R123-3",
            "_baseline_existing": True,
        }
        campus = {
            **baseline,
            "company": "BMO",
            "url": (
                "https://bmo.wd3.myworkdayjobs.com/en-US/Campus/job/"
                "Toronto-Ontario/Finance-Co-op_R123"
            ),
            "external_id": "bmo:R123",
            "source_target": "bmo.wd3/Campus",
            "_baseline_existing": False,
        }

        unique, _, _ = scrape.dedupe_candidates([campus, baseline])

        self.assertEqual([row["external_id"] for row in unique], ["bmo:R123-3"])

        distinct = {
            **campus,
            "external_id": "bmo:R124",
            "url": campus["url"].replace("R123", "R124"),
        }
        unique, _, _ = scrape.dedupe_candidates([campus, baseline, distinct])
        self.assertEqual(
            [row["external_id"] for row in unique],
            ["bmo:R123-3", "bmo:R124"],
        )

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
