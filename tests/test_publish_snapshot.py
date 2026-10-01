import hashlib
import hmac
import json
import unittest

import requests

from publish_snapshot import (
    MAX_BODY_BYTES,
    PublishError,
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
                "src": "workday",
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
        expected_id = hashlib.sha256(f"workday\n{canonical_url}".encode()).hexdigest()
        self.assertEqual(job["id"], expected_id)
        self.assertIsNone(job["externalId"])
        self.assertEqual(job["url"], canonical_url)
        self.assertIsNone(job["postedAt"])
        self.assertEqual(job["postedText"], "Posted Today")
        self.assertIsNone(job["description"])

    def test_current_snapshot_normalizes_every_job_with_unique_ids(self):
        with open("live_jobs.json", encoding="utf-8") as fh:
            source = json.load(fh)

        envelope, raw_body = build_envelope(source)

        self.assertEqual(len(envelope["jobs"]), len(source["jobs"]))
        self.assertEqual(len({job["id"] for job in envelope["jobs"]}), len(source["jobs"]))
        self.assertLessEqual(len(raw_body), MAX_BODY_BYTES)


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
