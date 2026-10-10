import json
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import requests
import scrape


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, *, json_value=None, text="", status_code=200):
        self._json_value = json_value
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._json_value


class WorkdayCoverageTests(unittest.TestCase):
    def setUp(self):
        scrape.reset_coverage()
        self.fixture = json.loads(
            (FIXTURES / "workday_late_toronto.json").read_text(encoding="utf-8")
        )

    def test_recovers_toronto_job_after_offset_sixty_and_deduplicates(self):
        calls = []
        fixture = self.fixture

        def fake_post(url, **kwargs):
            payload = kwargs["json"]
            calls.append(payload)
            if not payload["appliedFacets"]:
                return FakeResponse(
                    json_value={
                        "total": fixture["nationwideTotal"],
                        "jobPostings": [],
                        "facets": [fixture["facet"]],
                    }
                )

            self.assertEqual(
                payload["appliedFacets"],
                {"locations": ["toronto-id"]},
            )
            offset = payload["offset"]
            if offset < 80:
                posts = []
                for index in range(offset, offset + scrape.WORKDAY_PAGE_SIZE):
                    if offset == fixture["lateOffset"] and index == offset:
                        posts.append(fixture["lateJob"])
                    else:
                        posts.append(
                            {
                                "title": f"Finance Intern {index}",
                                "locationsText": "Toronto, Ontario",
                                "externalPath": f"/job/Toronto/Finance-Intern-{index}_R{index:06d}",
                                "postedOn": "Posted Today",
                            }
                        )
            elif offset == 80:
                posts = [fixture["lateJob"]]
            else:
                posts = []
            return FakeResponse(
                json_value={
                    "total": fixture["filteredTotal"],
                    "jobPostings": posts,
                    "facets": [],
                }
            )

        with (
            patch.object(scrape, "SEARCH_TERMS", (fixture["term"],)),
            patch.object(scrape.requests, "post", side_effect=fake_post),
        ):
            jobs = scrape.from_workday(tuple(fixture["spec"]))

        late = [job for job in jobs if job["title"] == fixture["lateJob"]["title"]]
        self.assertEqual(len(late), 1)
        self.assertIn("approved Canada location", late[0]["loc"])
        self.assertEqual(late[0]["external_id"], "fixture:R999999")
        self.assertIn(60, [call["offset"] for call in calls])
        self.assertIn(80, [call["offset"] for call in calls])

        key = scrape.coverage_key("workday", "fixture.wd3/Jobs")
        coverage = scrape.COVERAGE[key]
        self.assertEqual(coverage["query_total"], fixture["nationwideTotal"])
        self.assertEqual(coverage["selected_total"], fixture["filteredTotal"])
        self.assertEqual(coverage["pages"], 5)
        self.assertEqual(coverage["returned"], 81)
        self.assertEqual(coverage["records"], 80)
        self.assertEqual(coverage["fallback_terms"], 0)
        self.assertEqual(coverage["capped_terms"], 0)

    def test_location_filter_excludes_same_named_us_cities(self):
        selected = scrape.workday_location_filter([self.fixture["facet"]])

        self.assertEqual(selected["parameter"], "locations")
        self.assertEqual(selected["ids"], ["toronto-id"])

    def test_direct_location_facet_is_used_when_nested_locations_are_absent(self):
        selected = scrape.workday_location_filter(
            [
                {
                    "facetParameter": "Location",
                    "values": [
                        {
                            "descriptor": "Markham, ON, CAN",
                            "id": "markham-id",
                        },
                        {
                            "descriptor": "Concord, New Hampshire",
                            "id": "concord-us-id",
                        },
                    ],
                }
            ]
        )

        self.assertEqual(selected["parameter"], "Location")
        self.assertEqual(selected["ids"], ["markham-id"])

    def test_workday_path_recovers_only_canadian_locations(self):
        self.assertEqual(
            scrape.workday_location_from_path(
                "/job/Canada-Toronto-Ontario/"
                "Applied-Research-Intern_JREQ202812"
            ),
            "Toronto, Ontario, Canada",
        )
        self.assertEqual(
            scrape.workday_location_from_path(
                "/job/United-States-Rochester-New-York/"
                "Software-Engineer-Co-Op_JREQ203587"
            ),
            "",
        )
        self.assertTrue(scrape.workday_can_use_facet_location("3 Locations"))
        self.assertFalse(
            scrape.workday_can_use_facet_location(
                "Port Melbourne, Victoria, Australia"
            )
        )

    def test_workday_request_retries_transient_tls_failures(self):
        success = FakeResponse(
            json_value={"total": 0, "jobPostings": [], "facets": []}
        )
        with (
            patch.object(
                scrape.requests,
                "post",
                side_effect=[
                    requests.exceptions.SSLError("tls"),
                    requests.exceptions.SSLError("tls"),
                    success,
                ],
            ) as post,
            patch.object(scrape.time, "sleep"),
        ):
            response = scrape._workday_json(
                "https://fixture.example/jobs",
                "intern",
                0,
                {},
            )

        self.assertEqual(response["total"], 0)
        self.assertEqual(post.call_count, 3)

    def test_workday_registry_uses_verified_repaired_sites(self):
        specs = set(scrape.WORKDAY)
        for expected in {
            ("cibc", "Campus", "wd3"),
            ("rbc", "RBCEarlyTalent1", "wd3"),
            ("manulife", "MFCJH_Jobs", "wd3"),
            ("sunlife", "Campus", "wd3"),
            ("omers", "OMERS_External", "wd3"),
        }:
            self.assertIn(expected, specs)
        for stale in {
            ("rbc", "RBCJobs", "wd3"),
            ("rbc", "External", "wd3"),
            ("manulife", "MFCJH", "wd3"),
            ("omers", "External", "wd3"),
            ("cibc", "search", "wd1"),
            ("sunlife", "Experienced-Jobs", "wd1"),
        }:
            self.assertNotIn(stale, specs)

    def test_expansion_registry_uses_measured_official_sites(self):
        sources = {
            (source, scrape.retained_source_target(source, adapter_spec))
            for source, adapter_spec, _ in scrape.RETAINED_SOURCES
        }
        for expected in {
            ("successfactors", "bell"),
            ("successfactors", "hydroone"),
            ("successfactors", "fortisbc"),
            ("workday", "autodesk.wd1/uni"),
            ("workday", "cae.wd3/career"),
            (
                "workday",
                "canadiantirecorporation.wd3/"
                "enterprise_external_careers_site",
            ),
            ("workday", "bmo.wd3/Campus"),
            ("workday", "olg.wd3/careers-students"),
            (
                "workday",
                "rockwellautomation.wd1/"
                "external-rockwell-automation-early-careers",
            ),
            ("workday", "abb.wd3/external_career_page"),
            ("workday", "ciena.wd5/careers"),
            ("workday", "cisco.wd5/cisco_careers"),
            ("workday", "generalmotors.wd5/careers_gm"),
            ("workday", "gevernova.wd5/vernova_externalsite"),
            ("workday", "intel.wd1/external"),
            ("workday", "marvell.wd1/marvellcareers"),
            ("workday", "motorolasolutions.wd5/careers"),
            ("workday", "nvidia.wd5/nvidiaexternalcareersite"),
        }:
            self.assertIn(expected, sources)
            self.assertIn(
                scrape.coverage_key(*expected),
                scrape.RETAINED_SOURCE_KEYS,
            )

    def test_search_terms_and_amazon_queries_cover_approved_expansion(self):
        self.assertIn("stagiaire", scrape.SEARCH_TERMS)
        self.assertIn("\u00e9tudiant", scrape.SEARCH_TERMS)
        self.assertIn(
            "software development engineer intern",
            scrape.AMAZON_QUERIES,
        )
        self.assertIn("applied science intern", scrape.AMAZON_QUERIES)


class SuccessFactorsCoverageTests(unittest.TestCase):
    def setUp(self):
        scrape.reset_coverage()
        self.fixture = json.loads(
            (FIXTURES / "successfactors_late_rows.json").read_text(encoding="utf-8")
        )

    def test_recovers_row_after_start_seventy_five_and_stops_on_repeat(self):
        starts = []
        fixture = self.fixture

        def page_rows(start):
            rows = []
            for index in range(start, start + scrape.SUCCESSFACTORS_PAGE_SIZE):
                href = f"/job/Toronto-Finance-Intern-{index}/{100000 + index}/"
                title = f"Finance Intern {index}"
                if start == fixture["lateStart"] and index == start:
                    href = fixture["lateHref"]
                    title = fixture["lateTitle"]
                rows.append((href, title))
            return rows

        repeated_page = page_rows(fixture["lateStart"])

        def fake_get(url, **kwargs):
            start = kwargs["params"]["startrow"]
            starts.append(start)
            rows = repeated_page if start == 100 else page_rows(start)
            html = "".join(
                f'<a href="{href}">{title}<span></span></a>'
                for href, title in rows
            )
            return FakeResponse(text=html)

        with (
            patch.object(scrape, "SEARCH_TERMS", (fixture["term"],)),
            patch.object(scrape, "g", side_effect=fake_get),
        ):
            jobs = scrape.from_successfactors(tuple(fixture["spec"]))

        late = [job for job in jobs if job["title"] == fixture["lateTitle"]]
        self.assertEqual(len(late), 1)
        self.assertIn(75, starts)
        self.assertIn(100, starts)

        key = scrape.coverage_key("successfactors", "fixture")
        coverage = scrape.COVERAGE[key]
        self.assertEqual(coverage["pages"], 5)
        self.assertEqual(coverage["returned"], 125)
        self.assertEqual(coverage["records"], 100)
        self.assertEqual(coverage["repeated_terms"], 1)
        self.assertEqual(coverage["capped_terms"], 0)


class CoverageTelemetryTests(unittest.TestCase):
    def test_report_surfaces_counts_fallback_caps_and_errors(self):
        scrape.reset_coverage()
        key = scrape.coverage_key("workday", "fixture")
        scrape.set_coverage(
            key,
            source="workday",
            target="fixture",
            query_total=1427,
            selected_total=81,
            pages=5,
            returned=81,
            facet_parameters="-",
            fallback_terms=1,
            capped_terms=1,
            errors=["Timeout"],
            complete=False,
        )
        output = io.StringIO()

        with redirect_stdout(output):
            scrape.print_coverage_report(
                {key: 30},
                {key: 25},
                {key: 24},
            )

        report = output.getvalue()
        self.assertIn("1427/81", report)
        self.assertIn("30", report)
        self.assertIn("25", report)
        self.assertIn("24", report)
        self.assertIn("no usable target-location facet", report)
        self.assertIn("safety cap reached", report)
        self.assertIn("request errors Timeout", report)

    def test_url_verification_retries_transient_tls_failures(self):
        success = FakeResponse(text="<html>open role</html>", status_code=200)
        record = {"url": "https://example.com/job"}
        with (
            patch.object(
                scrape.requests,
                "get",
                side_effect=[
                    requests.exceptions.SSLError("tls"),
                    requests.exceptions.SSLError("tls"),
                    success,
                ],
            ) as get,
            patch.object(scrape.time, "sleep"),
        ):
            _, ok, reason = scrape.verify(record)

        self.assertTrue(ok)
        self.assertEqual(reason, "200")
        self.assertEqual(get.call_count, 3)

    def test_source_get_retries_transient_tls_failures(self):
        success = FakeResponse(text="ok", status_code=200)
        with (
            patch.object(
                scrape.requests,
                "get",
                side_effect=[
                    requests.exceptions.SSLError("tls"),
                    requests.exceptions.SSLError("tls"),
                    success,
                ],
            ) as get,
            patch.object(scrape.time, "sleep"),
        ):
            response = scrape.g("https://example.com/source")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
