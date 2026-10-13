from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import prelaw_sources
import scrape


class FakeResponse:
    def __init__(self, *, text="", data=None, status=200, url="https://example.test"):
        self.text = text
        self._data = data
        self.status_code = status
        self.url = url

    def json(self):
        return self._data

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


def gc_block(
    poster,
    title,
    *,
    program="Student programs",
    deadline="2026-11-30",
    company="Government Department",
    location="Ottawa (Ontario)",
):
    href = (
        f'/psrs-srfp/applicant/page1800?poster={poster}'
        if poster else "/psrs-srfp/applicant/page1710?careerChoiceId=123"
    )
    return f"""
    <li class="searchResult">
      <div><strong><a href="{href}">{title}</a></strong></div>
      <div><strong>{program}</strong></div>
      <div>Closing date: {deadline}<br />{company}<br />{location}<br />
      English essential<br />$20 to $30 per hour</div>
    </li>
    """


def gc_page(total, pages, *blocks):
    return (
        f'<a>Jobs open to the public ({total})</a>'
        f'<a>1</a> of {pages} [Next]'
        + "".join(blocks)
    )


class PrelawEligibilityTests(unittest.TestCase):
    def test_categories_use_legal_policy_evidence_not_interest_words(self):
        cases = {
            "Legal & Contract Management Co-op": [
                prelaw_sources.LEGAL_CATEGORY,
            ],
            "Corporate Compliance Intern": [
                prelaw_sources.LEGAL_CATEGORY,
            ],
            "Government Relations Student": [
                prelaw_sources.POLICY_CATEGORY,
            ],
            "Policy Research Student": [
                prelaw_sources.POLICY_CATEGORY,
            ],
            "Product Coordinator Intern, Driver Compliance": [],
            "Sales Systems & Data Governance Intern": [],
            "Environmental Compliance Intern": [],
            "AI Policy Engineer Intern": [],
            "Credit Risk Intern": [],
            "Risk Analyst, Enterprise Risk Governance Intern": [],
            "Information Archiving and Governance Intern": [],
            "Proposal Standards & Governance Intern": [],
            "Software Engineering Intern": [],
            "Supervision and Governance Co-op": [
                prelaw_sources.LEGAL_CATEGORY,
            ],
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    prelaw_sources.classify_prelaw(title),
                    expected,
                )

    def test_mandatory_law_school_excludes_but_preferred_does_not(self):
        mandatory = (
            "Currently enrolled in a Canadian or US accredited law school."
        )
        preferred = (
            "Currently pursuing a bachelor's degree. Legal studies or law "
            "coursework would be considered an asset."
        )
        self.assertIsNotNone(
            prelaw_sources.mandatory_legal_education_reason(mandatory)
        )
        self.assertEqual(
            prelaw_sources.classify_prelaw(
                "Legal Summer Student",
                mandatory,
            ),
            [],
        )
        self.assertIsNone(
            prelaw_sources.mandatory_legal_education_reason(preferred)
        )
        self.assertEqual(
            prelaw_sources.classify_prelaw(
                "Compliance Intern",
                preferred,
            ),
            [prelaw_sources.LEGAL_CATEGORY],
        )

    def test_law_clerk_restriction_is_explicit(self):
        content = (
            "Applicants must be currently enrolled in a Law Clerk diploma "
            "program at an accredited college."
        )
        note = prelaw_sources.restricted_program_note(
            "Law Clerk Co-op",
            content,
        )
        self.assertIn("Restricted qualification", note)
        self.assertIn("Law Clerk", note)


class GCJobsParserTests(unittest.TestCase):
    def test_nonposter_inventory_counts_for_completeness_but_is_not_ingested(self):
        parsed = prelaw_sources._gc_result_blocks(
            gc_page(
                2,
                1,
                gc_block("123", "Policy Research Student"),
                gc_block(
                    None,
                    "ONGOING STUDENT RECRUITMENT",
                    program="Federal Student Work Experience Program",
                    deadline="N/A",
                ),
            )
        )
        self.assertEqual(parsed["block_count"], 2)
        self.assertEqual([row["poster"] for row in parsed["rows"]], ["123"])

    def test_deadline_uses_real_pacific_closing_time(self):
        deadline = prelaw_sources._gc_deadline("2026-11-30")
        expected = datetime.combine(
            datetime(2026, 11, 30).date(),
            datetime.max.time().replace(microsecond=0),
            prelaw_sources.ZoneInfo("America/Vancouver"),
        ).astimezone(timezone.utc).isoformat()
        self.assertEqual(deadline, expected)
        self.assertIsNone(prelaw_sources._gc_deadline("N/A"))

    def test_complete_source_can_truthfully_publish_zero(self):
        page = gc_page(
            1,
            1,
            gc_block("123", "Legal Summer Student"),
        )
        detail = (
            "<h1>Legal Summer Student</h1>"
            "<h2>Duties</h2><p>Support legal research.</p>"
            "<h2>Work environment</h2>"
            "<h2>You need (essential for the job)</h2>"
            "<p>Currently enrolled in an accredited law school.</p>"
            "<h2>Language requirements (essential for the job)</h2>"
        )

        def handler(url, kwargs):
            if "page1800" in url:
                return FakeResponse(text=detail, url=url)
            if kwargs.get("params", {}).get("search"):
                return FakeResponse(text="", url="https://gc.test/search?tab=1")
            if "isSecondPartOfPage=1" in url:
                return FakeResponse(text=page, url=url)
            return FakeResponse(text="", url=url)

        result = prelaw_sources.collect_gc_jobs(
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
            session_factory=lambda: FakeSession(handler),
            sleep=lambda _: None,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["query_total"], 1)

    def test_pagination_accepts_policy_student_and_rejects_ai_policy(self):
        pages = {
            1: gc_page(
                2,
                2,
                gc_block("123", "Policy Research Student"),
            ),
            2: gc_page(
                2,
                2,
                gc_block("124", "AI Policy Engineer Intern"),
            ),
        }
        details = {
            "123": (
                "<h1>Policy Research Student</h1>"
                "<p>Research legislative issues and write public policy "
                "briefing notes.</p><p>Currently pursuing a bachelor's degree.</p>"
            ),
            "124": (
                "<h1>AI Policy Engineer Intern</h1>"
                "<p>Build software policy evaluation tooling.</p>"
            ),
        }

        def handler(url, kwargs):
            if "page1800" in url:
                poster = url.rsplit("=", 1)[-1]
                return FakeResponse(text=details[poster], url=url)
            params = kwargs.get("params", {})
            if params.get("search"):
                return FakeResponse(text="", url="https://gc.test/search?tab=1")
            if params.get("requestedPage"):
                return FakeResponse(
                    text=pages[int(params["requestedPage"])],
                    url=url,
                )
            if "isSecondPartOfPage=1" in url:
                return FakeResponse(text=pages[1], url=url)
            return FakeResponse(text="", url=url)

        result = prelaw_sources.collect_gc_jobs(
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
            session_factory=lambda: FakeSession(handler),
            sleep=lambda _: None,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(
            [row["external_id"] for row in result["rows"]],
            ["123"],
        )
        self.assertEqual(
            result["rows"][0]["_prelaw_categories"],
            [prelaw_sources.POLICY_CATEGORY],
        )

    def test_gc_standard_ethics_boilerplate_does_not_create_legal_tag(self):
        page = """
        <h2>Duties</h2>
        <p>Model atmospheric science and analyze satellite observations.</p>
        <h2>Work environment</h2>
        <h2>Important messages</h2>
        <p>Values and Ethics Code. Avoid conflicts of interest.</p>
        <h2>You need (essential for the job)</h2>
        <p>Be enrolled in a graduate Earth science program.</p>
        <h2>Language requirements (essential for the job)</h2>
        """
        relevant = prelaw_sources._gc_detail_text(page)
        self.assertNotIn("conflicts of interest", relevant)
        self.assertEqual(
            prelaw_sources.classify_prelaw(
                "Student Research Assistant",
                relevant,
            ),
            [],
        )


class OracleCollectorTests(unittest.TestCase):
    def test_oracle_uses_real_posted_date_and_excludes_science_regulatory(self):
        listing = {
            "items": [{
                "TotalJobsCount": 2,
                "requisitionList": [
                    {
                        "Id": "1",
                        "Title": "Government Relations Student",
                        "PostedDate": "2026-08-17",
                        "PrimaryLocationCountry": "CA",
                        "PrimaryLocation": "Calgary, AB, Canada",
                    },
                    {
                        "Id": "2",
                        "Title": "Environmental Regulatory Compliance Student",
                        "PostedDate": "2026-08-17",
                        "PrimaryLocationCountry": "CA",
                        "PrimaryLocation": "Calgary, AB, Canada",
                    },
                ],
            }]
        }
        details = {
            "1": {
                "items": [{
                    "Id": "1",
                    "Title": "Government Relations Student",
                    "PrimaryLocation": "Calgary, AB, Canada",
                    "ExternalPostedStartDate": "2026-08-17T11:00:00+00:00",
                    "ExternalPostedEndDate": None,
                    "ExternalDescriptionStr": (
                        "<p>Review public policy and legislative issues.</p>"
                        "<p>Currently enrolled in Public Policy or Political "
                        "Science.</p>"
                    ),
                }]
            },
            "2": {
                "items": [{
                    "Id": "2",
                    "Title": "Environmental Regulatory Compliance Student",
                    "PrimaryLocation": "Calgary, AB, Canada",
                    "ExternalPostedStartDate": "2026-08-17T11:00:00+00:00",
                    "ExternalPostedEndDate": None,
                    "ExternalDescriptionStr": (
                        "<p>Environmental regulatory compliance.</p>"
                        "<p>Currently enrolled in Environmental Science or "
                        "Engineering.</p>"
                    ),
                }]
            },
        }

        def handler(url, kwargs):
            if "requisitiondetails" in url.casefold():
                identifier = kwargs["params"]["q"].split("=", 1)[1]
                return FakeResponse(data=details[identifier], url=url)
            return FakeResponse(data=listing, url=url)

        result = prelaw_sources.collect_cnrl_oracle(
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
            session=FakeSession(handler),
        )
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["external_id"], "CNRL-Campus:1")
        self.assertEqual(row["posted"], "2026-08-17T11:00:00+00:00")
        self.assertEqual(
            row["_prelaw_categories"],
            [prelaw_sources.POLICY_CATEGORY],
        )


class DailySourceCacheTests(unittest.TestCase):
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    spec = ("oracle", "CNRL-Campus", None)
    key = "oracle:CNRL-Campus"

    def setUp(self):
        scrape.reset_coverage()

    def write_baseline(self, path, refreshed_at, rows=None):
        path.write_text(json.dumps({
            "fetched_at": self.now.isoformat(),
            "jobs": rows or [],
            "source_state": {"daily_sources": {
                self.key: {
                    "status": "fresh",
                    "refreshed_at": refreshed_at.isoformat(),
                    "complete": True,
                    "reused": 0,
                }
            }},
        }), encoding="utf-8")

    def row(self):
        return {
            "company": "Canadian Natural",
            "src": "oracle",
            "title": "Government Relations Student",
            "loc": "Calgary, AB, Canada",
            "url": (
                "https://ehaa.fa.ca2.oraclecloud.com/hcmUI/"
                "CandidateExperience/en/sites/CNRL-Campus/job/1"
            ),
            "external_id": "CNRL-Campus:1",
            "posted": "2026-08-17T11:00:00+00:00",
            "content": "Source: Canadian Natural official careers.",
            "cats": [prelaw_sources.POLICY_CATEGORY],
            "source_target": "CNRL-Campus",
            "last_seen": self.now.isoformat(),
        }

    def test_off_cycle_reuses_without_calling_or_renewing_source(self):
        refreshed = self.now - timedelta(hours=10)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            self.write_baseline(path, refreshed, [self.row()])
            collector = Mock()
            with patch.dict(
                os.environ,
                {"SOURCE_BASELINE_PATH": str(path)},
                clear=False,
            ):
                rows = scrape.from_daily_prelaw_source(
                    ("oracle", "CNRL-Campus", collector),
                    now=self.now,
                )

        collector.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            scrape.COVERAGE[self.key]["source_refreshed_at"],
            refreshed.isoformat(),
        )
        self.assertEqual(scrape.COVERAGE[self.key]["status"], "reused-fresh")

    def test_complete_zero_replaces_prior_slice(self):
        refreshed = self.now - timedelta(hours=25)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            self.write_baseline(path, refreshed, [self.row()])
            collector = Mock(return_value={
                "rows": [],
                "complete": True,
                "errors": [],
                "pages": 1,
                "returned": 0,
                "details": 0,
                "query_total": 0,
            })
            with patch.dict(
                os.environ,
                {"SOURCE_BASELINE_PATH": str(path)},
                clear=False,
            ):
                rows = scrape.from_daily_prelaw_source(
                    ("oracle", "CNRL-Campus", collector),
                    now=self.now,
                )

        self.assertEqual(rows, [])
        self.assertEqual(scrape.COVERAGE[self.key]["status"], "fresh")
        self.assertEqual(
            scrape.COVERAGE[self.key]["source_refreshed_at"],
            self.now.isoformat(),
        )

    def test_failure_reuses_for_seventy_two_hours_then_blocks(self):
        collector = Mock(return_value={
            "rows": [],
            "complete": False,
            "errors": ["Timeout"],
            "pages": 0,
            "returned": 0,
            "details": 0,
            "query_total": None,
        })
        for age, expected in [(48, "reused-incomplete"), (72.01, "blocked")]:
            with self.subTest(age=age), tempfile.TemporaryDirectory() as temp:
                scrape.reset_coverage()
                path = Path(temp) / "baseline.json"
                self.write_baseline(
                    path,
                    self.now - timedelta(hours=age),
                    [self.row()],
                )
                with patch.dict(
                    os.environ,
                    {"SOURCE_BASELINE_PATH": str(path)},
                    clear=False,
                ):
                    rows = scrape.from_daily_prelaw_source(
                        ("oracle", "CNRL-Campus", collector),
                        now=self.now,
                    )
                self.assertEqual(
                    len(rows),
                    int(expected == "reused-incomplete"),
                )
                self.assertEqual(
                    scrape.COVERAGE[self.key]["status"],
                    expected,
                )


class PipelinePrelawTests(unittest.TestCase):
    def setUp(self):
        scrape.reset_coverage()

    def test_mandatory_law_school_job_remains_but_is_not_prelaw_tagged(self):
        rows, _ = scrape.filter_candidates([{
            "src": "greenhouse",
            "title": "Legal Summer Student",
            "loc": "Toronto, Ontario",
            "url": "https://example.com/legal-student",
            "posted": "",
            "content": "Currently enrolled in an accredited law school.",
        }])
        self.assertEqual(len(rows), 1)
        self.assertNotIn(
            prelaw_sources.LEGAL_CATEGORY,
            rows[0]["cats"],
        )

    def test_content_evidence_adds_category_and_restricted_note(self):
        rows, _ = scrape.filter_candidates([
            {
                "src": "greenhouse",
                "title": "Corporate Compliance Intern",
                "loc": "Oakville, Ontario",
                "url": "https://example.com/compliance",
                "posted": "",
                "content": (
                    "Currently pursuing a bachelor's degree. Support the "
                    "corporate ethics and compliance framework."
                ),
            },
            {
                "src": "gcjobs",
                "title": "Law Clerk Co-op",
                "loc": "Ottawa, Ontario",
                "url": "https://example.com/law-clerk",
                "posted": "",
                "content": (
                    "Applicants must be currently enrolled in a Law Clerk "
                    "diploma program."
                ),
            },
        ])
        self.assertEqual(
            rows[0]["cats"],
            [prelaw_sources.LEGAL_CATEGORY],
        )
        self.assertIn(prelaw_sources.LEGAL_CATEGORY, rows[1]["cats"])
        self.assertTrue(
            rows[1]["content"].startswith("Restricted qualification:")
        )

    def test_daily_source_placement_evidence_allows_generic_policy_title(self):
        rows, _ = scrape.filter_candidates([{
            "src": "gcjobs",
            "title": "Policy Research Assistant",
            "loc": "Ottawa, Ontario",
            "url": "https://example.com/policy",
            "posted": "",
            "content": "Research public policy and legislative issues.",
            "_placement_evidence": True,
            "_prelaw_categories": [prelaw_sources.POLICY_CATEGORY],
        }])
        self.assertEqual(len(rows), 1)
        self.assertIn(prelaw_sources.POLICY_CATEGORY, rows[0]["cats"])

    def test_workday_detail_url_and_baseline_category_fallback(self):
        record = {
            "src": "workday",
            "title": "Compliance Intern",
            "loc": "Toronto, Ontario",
            "url": (
                "https://example.wd3.myworkdayjobs.com/en-US/Campus/job/"
                "Toronto-Ontario/Compliance-Intern_R123"
            ),
            "external_id": "example:R123",
            "posted": "Posted Today",
            "content": "",
            "_prelaw_detail_unavailable": True,
            "_baseline_categories": [prelaw_sources.LEGAL_CATEGORY],
        }
        self.assertEqual(
            scrape._workday_detail_url(record),
            (
                "https://example.wd3.myworkdayjobs.com/wday/cxs/"
                "example/Campus/job/Toronto-Ontario/"
                "Compliance-Intern_R123"
            ),
        )
        rows, _ = scrape.filter_candidates([record])
        self.assertIn(prelaw_sources.LEGAL_CATEGORY, rows[0]["cats"])

        unavailable = {
            **record,
            "_baseline_categories": [],
        }
        rows, _ = scrape.filter_candidates([unavailable])
        self.assertNotIn(prelaw_sources.LEGAL_CATEGORY, rows[0]["cats"])

        carried = {
            **record,
            "title": "Legal Summer Student",
            "content": "Short cached description without qualifications.",
            "_carried_source_row": True,
            "_prelaw_detail_unavailable": False,
            "_baseline_categories": [],
        }
        rows, _ = scrape.filter_candidates([carried])
        self.assertNotIn(prelaw_sources.LEGAL_CATEGORY, rows[0]["cats"])

    def test_detail_hydration_deduplicates_requests_and_caps_work(self):
        base = {
            "src": "workday",
            "title": "Compliance Intern",
            "loc": "Toronto, Ontario",
            "external_id": "example:R123",
            "posted": "Posted Today",
            "content": "",
        }
        duplicate_rows = [
            {
                **base,
                "url": (
                    "https://example.wd3.myworkdayjobs.com/en-US/Campus/"
                    "job/Toronto/Compliance-Intern_R123"
                ),
            },
            {
                **base,
                "url": (
                    "https://example.wd3.myworkdayjobs.com/en-US/Campus/"
                    "job/Toronto/Compliance-Intern_R123"
                ),
            },
        ]
        loader = Mock(return_value="Corporate ethics and compliance framework.")
        with patch.object(scrape, "_workday_detail_content", loader):
            scrape.hydrate_prelaw_details(duplicate_rows)
        loader.assert_called_once()
        self.assertTrue(all(row["content"] for row in duplicate_rows))

        rows = [
            {
                **base,
                "external_id": f"example:R{index}",
                "url": (
                    "https://example.wd3.myworkdayjobs.com/en-US/Campus/"
                    f"job/Toronto/Compliance-Intern_R{index}"
                ),
            }
            for index in range(2)
        ]
        with (
            patch.object(scrape, "PRELAW_DETAIL_MAX_REQUESTS", 1),
            patch.object(
                scrape,
                "_workday_detail_content",
                return_value="Corporate compliance.",
            ),
        ):
            scrape.hydrate_prelaw_details(rows)
        self.assertEqual(
            sum(bool(row.get("_prelaw_detail_unavailable")) for row in rows),
            1,
        )


if __name__ == "__main__":
    unittest.main()
