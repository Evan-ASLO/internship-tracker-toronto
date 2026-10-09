import hashlib
import hmac
import json
import unittest

import requests

from publish_snapshot import (
    MAX_BODY_BYTES,
    MAX_CATEGORIES,
    MAX_CATEGORY_LENGTH,
    MAX_COMPANY_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_EXTERNAL_ID_LENGTH,
    MAX_LOCATION_LENGTH,
    MAX_POSTED_TEXT_LENGTH,
    MAX_REGION_LENGTH,
    MAX_SOURCE_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_URL_LENGTH,
    PublishError,
    SnapshotError,
    build_envelope,
    canonical_json_bytes,
    ingest_signature,
    normalize_job,
    publish_snapshot,
)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeSession:
    def __init__(self, status_code=202, error=None):
        self.status_code = status_code
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return FakeResponse(self.status_code)


class SnapshotNormalizationTests(unittest.TestCase):
    def test_normalizes_legacy_job_and_uses_reliable_external_id(self):
        job = normalize_job(
            {
                "src": "GreenHouse",
                "company": " D2L ",
                "title": " Finance   Intern ",
                "loc": " Toronto, ON ",
                "geo": "Toronto",
                "cats": ["Finance", "Finance"],
                "url": "https://WWW.D2L.com/careers/jobs/?gh_jid=8188363&job_id=8188363#top",
                "posted": "2026-09-16T15:20:00-04:00",
                "content": " Build   forecasts ",
            }
        )

        expected_id = hashlib.sha256(b"greenhouse\n8188363").hexdigest()
        self.assertEqual(job["id"], expected_id)
        self.assertRegex(job["id"], r"^[0-9a-f]{64}$")
        self.assertEqual(job["externalId"], "8188363")
        self.assertEqual(job["source"], "greenhouse")
        self.assertEqual(job["location"], "Toronto, ON")
        self.assertEqual(job["region"], "Toronto")
        self.assertEqual(job["categories"], ["Finance"])
        self.assertEqual(job["postedAt"], "2026-09-16T19:20:00Z")
        self.assertEqual(job["postedText"], "2026-09-16T15:20:00-04:00")
        self.assertEqual(job["description"], "Build forecasts")

    def test_uses_canonical_url_when_external_id_is_unavailable(self):
        job = normalize_job(
            {
                "src": "custom",
                "company": "BMO",
                "title": "Finance Intern",
                "loc": "Toronto",
                "geo": "Toronto",
                "cats": [],
                "url": "HTTPS://BMO.WD3.MYWORKDAYJOBS.COM/job?id=2&a=1#apply",
                "posted": "Posted Today",
                "content": "",
            }
        )

        canonical_url = "https://bmo.wd3.myworkdayjobs.com/job?a=1&id=2"
        expected_id = hashlib.sha256(f"custom\n{canonical_url}".encode()).hexdigest()
        self.assertEqual(job["id"], expected_id)
        self.assertIsNone(job["externalId"])
        self.assertEqual(job["url"], canonical_url)
        self.assertIsNone(job["postedAt"])
        self.assertEqual(job["postedText"], "Posted Today")
        self.assertIsNone(job["description"])

    def test_workday_id_uses_tenant_scoped_requisition_not_title_slug(self):
        base = {
            "src": "workday",
            "company": "BMO",
            "title": "Finance Intern",
            "loc": "Toronto",
            "geo": "Toronto",
            "cats": ["Finance"],
            "posted": "Posted Today",
            "content": "",
        }
        first = normalize_job(
            {
                **base,
                "external_id": "R260026123-1",
                "url": "https://bmo.wd3.myworkdayjobs.com/en-US/External/job/Toronto/Old-Title_R260026123-1",
            }
        )
        edited = normalize_job(
            {
                **base,
                "title": "Renamed Finance Internship",
                "url": "https://bmo.wd3.myworkdayjobs.com/en-US/External/job/Toronto/New-Title_R260026123-1",
            }
        )

        self.assertEqual(first["externalId"], "bmo:R260026123-1")
        self.assertEqual(first["id"], edited["id"])
        self.assertEqual(
            first["id"],
            hashlib.sha256(b"workday\nbmo:R260026123-1").hexdigest(),
        )

    def test_successfactors_id_uses_host_scoped_numeric_requisition(self):
        base = {
            "src": "successfactors",
            "company": "scotiabank",
            "title": "Finance Intern",
            "loc": "Toronto",
            "geo": "Toronto",
            "cats": ["Finance"],
            "posted": "",
            "content": "",
        }
        first = normalize_job(
            {
                **base,
                "url": "https://jobs.scotiabank.com/job/Toronto-Old-Title/605898317/",
            }
        )
        edited = normalize_job(
            {
                **base,
                "title": "Renamed Finance Internship",
                "url": "https://jobs.scotiabank.com/job/Toronto-New-Title/605898317/",
            }
        )

        self.assertEqual(
            first["externalId"],
            "jobs.scotiabank.com:605898317",
        )
        self.assertEqual(first["id"], edited["id"])

    def test_workday_numeric_requisition_is_tenant_scoped(self):
        job = normalize_job(
            {
                "src": "workday",
                "company": "cibc",
                "title": "Risk Analytics Co-op",
                "loc": "Toronto",
                "geo": "Toronto",
                "cats": ["Finance"],
                "url": "https://cibc.wd3.myworkdayjobs.com/en-US/search/job/Toronto/Risk-Analytics-Co-op_2618885-1",
                "posted": "Posted Today",
                "content": "",
            }
        )

        self.assertEqual(job["externalId"], "cibc:2618885-1")
        self.assertEqual(
            job["id"],
            hashlib.sha256(b"workday\ncibc:2618885-1").hexdigest(),
        )

    def test_greenhouse_api_id_must_match_url_id(self):
        with self.assertRaisesRegex(SnapshotError, "does not match URL job ID"):
            normalize_job(
                {
                    "src": "greenhouse",
                    "external_id": "123",
                    "company": "Example",
                    "title": "Finance Intern",
                    "loc": "Toronto",
                    "geo": "Toronto",
                    "cats": ["Finance"],
                    "url": "https://boards.greenhouse.io/example/jobs/456",
                    "posted": "",
                    "content": "",
                }
            )

    def test_board_scoped_greenhouse_id_matches_custom_employer_url(self):
        job = normalize_job(
            {
                "src": "greenhouse",
                "source_target": "coveoen",
                "external_id": "coveoen:123",
                "company": "Coveo",
                "title": "Finance Intern",
                "loc": "Montreal, Quebec, Canada",
                "geo": "Quebec",
                "cats": ["Finance"],
                "url": "https://www.coveo.com/careers?gh_jid=123",
                "posted": "2026-10-01T12:00:00Z",
                "content": "",
            }
        )

        self.assertEqual(job["externalId"], "coveoen:123")

    def test_current_snapshot_normalizes_every_job_with_unique_ids(self):
        with open("live_jobs.json", encoding="utf-8") as fh:
            source = json.load(fh)

        envelope, raw_body = build_envelope(source)

        self.assertEqual(len(envelope["jobs"]), len(source["jobs"]))
        self.assertEqual(len({job["id"] for job in envelope["jobs"]}), len(source["jobs"]))
        self.assertLessEqual(len(raw_body), MAX_BODY_BYTES)

    def test_display_fields_are_capped_without_changing_fallback_identity(self):
        company = "C" * (MAX_COMPANY_LENGTH + 25)
        title = "T" * (MAX_TITLE_LENGTH + 25)
        location = "L" * (MAX_LOCATION_LENGTH + 25)
        categories = [
            f"{index:02d}-" + ("X" * MAX_CATEGORY_LENGTH)
            for index in range(MAX_CATEGORIES + 6)
        ]

        job = normalize_job(
            {
                "src": "custom",
                "company": company,
                "title": title,
                "loc": location,
                "geo": "R" * (MAX_REGION_LENGTH + 5),
                "cats": categories,
                "url": "",
                "posted": "P" * (MAX_POSTED_TEXT_LENGTH + 5),
                "content": "D" * (MAX_DESCRIPTION_LENGTH + 5),
            }
        )

        identity = "\n".join(
            ("custom", company.casefold(), title.casefold(), location.casefold())
        )
        self.assertEqual(job["id"], hashlib.sha256(identity.encode()).hexdigest())
        self.assertEqual(len(job["company"]), MAX_COMPANY_LENGTH)
        self.assertEqual(len(job["title"]), MAX_TITLE_LENGTH)
        self.assertEqual(len(job["location"]), MAX_LOCATION_LENGTH)
        self.assertEqual(len(job["region"]), MAX_REGION_LENGTH)
        self.assertEqual(len(job["categories"]), MAX_CATEGORIES)
        self.assertTrue(
            all(len(category) <= MAX_CATEGORY_LENGTH for category in job["categories"])
        )
        self.assertEqual(len(job["postedText"]), MAX_POSTED_TEXT_LENGTH)
        self.assertEqual(len(job["description"]), MAX_DESCRIPTION_LENGTH)

    def test_identity_critical_fields_are_rejected_instead_of_truncated(self):
        base = {
            "src": "custom",
            "company": "Example",
            "title": "Intern",
            "loc": "Toronto",
            "geo": "Toronto",
            "cats": [],
            "url": "https://example.com/jobs/1",
            "posted": "",
            "content": "",
        }
        cases = {
            "source": {**base, "src": "s" * (MAX_SOURCE_LENGTH + 1)},
            "externalId": {
                **base,
                "external_id": "e" * (MAX_EXTERNAL_ID_LENGTH + 1),
            },
            "url": {
                **base,
                "url": "https://example.com/" + ("u" * MAX_URL_LENGTH),
            },
        }

        for field, candidate in cases.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(SnapshotError, field):
                    normalize_job(candidate)


class SnapshotDeterminismTests(unittest.TestCase):
    def setUp(self):
        self.job_a = {
            "src": "greenhouse",
            "external_id": "42",
            "company": "Alpha",
            "title": "Finance Intern",
            "loc": "Toronto",
            "geo": "Toronto",
            "cats": ["Finance"],
            "url": "https://example.com/jobs/42",
            "posted": "2026-09-01T00:00:00Z",
            "content": "",
        }
        self.job_b = {
            "src": "workday",
            "company": "Beta",
            "title": "Operations Intern",
            "loc": "Toronto",
            "geo": "Toronto",
            "cats": ["Business Operations"],
            "url": "https://jobs.example.com/role?b=2&a=1",
            "posted": "Posted Today",
            "content": "Role details",
        }

    def test_hash_and_body_are_deterministic(self):
        one, one_body = build_envelope(
            {"fetched_at": "2026-09-20T10:00:00Z", "jobs": [self.job_a, self.job_b]}
        )
        two, two_body = build_envelope(
            {"fetched_at": "2026-09-20T10:00:00Z", "jobs": [self.job_b, self.job_a]}
        )

        self.assertEqual(one, two)
        self.assertEqual(one_body, two_body)
        expected_hash = hashlib.sha256(canonical_json_bytes(one["jobs"])).hexdigest()
        self.assertEqual(one["snapshotHash"], expected_hash)

    def test_signature_is_lowercase_hex_over_timestamp_dot_body(self):
        body = b'{"schemaVersion":1}'
        timestamp = "1790870400"
        secret = "test-secret"
        expected = hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()

        signature = ingest_signature(timestamp, body, secret)

        self.assertEqual(signature, expected)
        self.assertRegex(signature, r"^[0-9a-f]{64}$")


class SnapshotPublisherTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "fetched_at": "2026-09-20T10:00:00Z",
            "jobs": [
                {
                    "src": "greenhouse",
                    "external_id": "42",
                    "company": "Alpha",
                    "title": "Finance Intern",
                    "loc": "Toronto",
                    "geo": "Toronto",
                    "cats": ["Finance"],
                    "url": "https://example.com/jobs/42",
                    "posted": "2026-09-01T00:00:00Z",
                    "content": "",
                }
            ],
        }

    def test_posts_signed_raw_body(self):
        session = FakeSession(status_code=202)

        envelope = publish_snapshot(
            self.snapshot,
            ingest_url="https://worker.example/api/internal/jobs/snapshot",
            secret="secret",
            session=session,
            now=lambda: 1790870400.9,
        )

        self.assertEqual(len(session.calls), 1)
        url, request = session.calls[0]
        self.assertEqual(url, "https://worker.example/api/internal/jobs/snapshot")
        self.assertEqual(request["headers"]["X-Ingest-Timestamp"], "1790870400")
        self.assertEqual(
            request["headers"]["X-Ingest-Signature"],
            ingest_signature("1790870400", request["data"], "secret"),
        )
        self.assertEqual(json.loads(request["data"]), envelope)

    def test_non_success_response_fails_without_exposing_secret(self):
        session = FakeSession(status_code=503)

        with self.assertRaises(PublishError) as caught:
            publish_snapshot(
                self.snapshot,
                ingest_url="https://worker.example/api/internal/jobs/snapshot",
                secret="do-not-print-me",
                session=session,
                now=lambda: 1790870400,
            )

        self.assertIn("HTTP 503", str(caught.exception))
        self.assertNotIn("do-not-print-me", str(caught.exception))

    def test_network_failure_is_explicit(self):
        session = FakeSession(error=requests.Timeout("timed out"))

        with self.assertRaisesRegex(PublishError, "Timeout"):
            publish_snapshot(
                self.snapshot,
                ingest_url="https://worker.example/api/internal/jobs/snapshot",
                secret="secret",
                session=session,
                now=lambda: 1790870400,
            )


if __name__ == "__main__":
    unittest.main()
