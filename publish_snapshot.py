"""Normalize and publish the verified jobs snapshot to Cloudflare."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import requests


SCHEMA_VERSION = 1
MAX_JOBS = 1000
MAX_BODY_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
SOURCE_NAME = "internship-tracker-toronto"
MAX_EXTERNAL_ID_LENGTH = 240
MAX_SOURCE_LENGTH = 80
MAX_COMPANY_LENGTH = 200
MAX_TITLE_LENGTH = 240
MAX_LOCATION_LENGTH = 240
MAX_REGION_LENGTH = 120
MAX_CATEGORIES = 24
MAX_CATEGORY_LENGTH = 80
MAX_URL_LENGTH = 2048
MAX_POSTED_TEXT_LENGTH = 240
MAX_DESCRIPTION_LENGTH = 12000


class SnapshotError(ValueError):
    """The local snapshot cannot be normalized safely."""


class PublishError(RuntimeError):
    """The normalized snapshot could not be published."""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def truncate_display(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def require_identity_length(field: str, value: str, limit: int) -> str:
    if len(value) > limit:
        raise SnapshotError(f"{field} is {len(value)} characters; limit is {limit}")
    return value


def canonicalize_url(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise SnapshotError(f"Invalid job URL: {raw!r}")
    if parsed.username or parsed.password:
        raise SnapshotError("Job URLs must not contain credentials")

    host = parsed.hostname.lower()
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, host, parsed.path or "/", query, ""))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalized_identity_text(value: Any) -> str:
    return clean_text(value).casefold()


def _explicit_external_id(job: Mapping[str, Any]) -> str | None:
    for key in ("externalId", "external_id"):
        value = clean_text(job.get(key))
        if value:
            return value
    return None


def _greenhouse_url_external_id(canonical_url: str) -> str | None:
    if not canonical_url:
        return None

    parsed = urlsplit(canonical_url)
    query = parse_qs(parsed.query)
    for key in ("gh_jid", "job_id"):
        values = query.get(key) or []
        if values and re.fullmatch(r"\d+", values[0]):
            return values[0]

    match = re.search(r"/jobs/(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def _reliable_url_external_id(source: str, canonical_url: str) -> str | None:
    if not canonical_url:
        return None

    parsed = urlsplit(canonical_url)
    if source == "greenhouse":
        return _greenhouse_url_external_id(canonical_url)

    if source == "workday" and parsed.hostname:
        host_parts = parsed.hostname.lower().split(".")
        final_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        match = re.search(
            r"_((?:JR|REQ|R)[A-Za-z0-9_-]+|\d{6,}(?:-\d+)?)$",
            final_segment,
            re.I,
        )
        if len(host_parts) > 1 and match:
            tenant = host_parts[0]
            return f"{tenant}:{match.group(1)}"

    if source == "successfactors" and parsed.hostname:
        match = re.search(r"/(\d+)/?$", parsed.path)
        if match:
            return f"{parsed.hostname.lower()}:{match.group(1)}"

    if source == "amazon":
        match = re.search(r"/jobs/(\d+)(?:/|$)", parsed.path)
        if match:
            return match.group(1)

    return None


def stable_job_id(
    *,
    source: str,
    external_id: str | None,
    canonical_url: str,
    company: str,
    title: str,
    location: str,
) -> str:
    if external_id:
        identity = f"{source}\n{external_id}"
    elif canonical_url:
        identity = f"{source}\n{canonical_url}"
    else:
        identity = "\n".join(
            (
                _normalized_identity_text(source),
                _normalized_identity_text(company),
                _normalized_identity_text(title),
                _normalized_identity_text(location),
            )
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _absolute_posted_at(value: str) -> str | None:
    if not value:
        return None

    candidate = value
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    timespec = "seconds" if parsed.microsecond == 0 else "milliseconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _generated_at(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        raise SnapshotError("Snapshot is missing fetched_at/generatedAt")
    normalized = _absolute_posted_at(raw)
    if not normalized:
        raise SnapshotError(f"Snapshot timestamp is not ISO-8601: {raw!r}")
    return normalized


def normalize_job(
    job: Mapping[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    source = _normalized_identity_text(job.get("source") or job.get("src"))
    company_identity = clean_text(job.get("company"))
    title_identity = clean_text(job.get("title"))
    location_identity = clean_text(job.get("location") or job.get("loc"))
    region = clean_text(job.get("region") or job.get("geo"))
    canonical_url = canonicalize_url(job.get("url"))

    if not source:
        raise SnapshotError("Job is missing source/src")
    if not company_identity:
        raise SnapshotError("Job is missing company")
    if not title_identity:
        raise SnapshotError("Job is missing title")
    require_identity_length("source", source, MAX_SOURCE_LENGTH)
    if canonical_url:
        require_identity_length("url", canonical_url, MAX_URL_LENGTH)

    explicit_external_id = _explicit_external_id(job)
    url_external_id = _reliable_url_external_id(source, canonical_url)
    greenhouse_ids = {url_external_id}
    parsed_url = urlsplit(canonical_url)
    if parsed_url.hostname in {"job-boards.greenhouse.io", "boards.greenhouse.io"}:
        board = parsed_url.path.strip("/").split("/")[0]
        greenhouse_ids.add(f"{board}:{url_external_id}")
    source_target = clean_text(job.get("source_target"))
    if source == "greenhouse" and source_target and url_external_id:
        greenhouse_ids.add(f"{source_target}:{url_external_id}")
    if (
        source == "greenhouse"
        and explicit_external_id
        and url_external_id
        and explicit_external_id not in greenhouse_ids
    ):
        raise SnapshotError(
            f"Greenhouse externalId {explicit_external_id!r} does not match "
            f"URL job ID {url_external_id!r}"
        )
    if source in {"workday", "successfactors"} and url_external_id:
        external_id = url_external_id
    else:
        external_id = explicit_external_id or url_external_id
    if external_id:
        require_identity_length("externalId", external_id, MAX_EXTERNAL_ID_LENGTH)

    raw_categories = job.get("categories")
    if raw_categories is None:
        raw_categories = job.get("cats", [])
    if not isinstance(raw_categories, list):
        raise SnapshotError(f"Job categories must be a list: {title_identity!r}")
    full_categories = sorted(
        {clean_text(category) for category in raw_categories if clean_text(category)},
        key=str.casefold,
    )
    categories = []
    for category in full_categories:
        normalized_category = truncate_display(category, MAX_CATEGORY_LENGTH)
        if normalized_category not in categories:
            categories.append(normalized_category)
        if len(categories) == MAX_CATEGORIES:
            break

    posted_text_identity = clean_text(job.get("postedText") or job.get("posted")) or None
    posted_at = clean_text(job.get("postedAt")) or None
    if posted_at:
        posted_at = _absolute_posted_at(posted_at)
    elif posted_text_identity:
        posted_at = _absolute_posted_at(posted_text_identity)
    if posted_at and generated_at:
        posted_datetime = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        generated_datetime = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if posted_datetime > generated_datetime:
            posted_at = None

    description_identity = clean_text(job.get("description") or job.get("content")) or None
    normalized_id = stable_job_id(
        source=source,
        external_id=external_id,
        canonical_url=canonical_url,
        company=company_identity,
        title=title_identity,
        location=location_identity,
    )

    return {
        "id": normalized_id,
        "externalId": external_id,
        "source": source,
        "company": truncate_display(company_identity, MAX_COMPANY_LENGTH),
        "title": truncate_display(title_identity, MAX_TITLE_LENGTH),
        "location": truncate_display(location_identity, MAX_LOCATION_LENGTH),
        "region": truncate_display(region, MAX_REGION_LENGTH),
        "categories": categories,
        "url": canonical_url,
        "postedAt": posted_at,
        "postedText": (
            truncate_display(posted_text_identity, MAX_POSTED_TEXT_LENGTH)
            if posted_text_identity
            else None
        ),
        "description": (
            truncate_display(description_identity, MAX_DESCRIPTION_LENGTH)
            if description_identity
            else None
        ),
    }


def build_envelope(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    source_jobs = snapshot.get("jobs")
    if not isinstance(source_jobs, list):
        raise SnapshotError("Snapshot jobs must be a list")
    if len(source_jobs) > MAX_JOBS:
        raise SnapshotError(f"Snapshot has {len(source_jobs)} jobs; limit is {MAX_JOBS}")

    generated_at = _generated_at(snapshot.get("generatedAt") or snapshot.get("fetched_at"))
    jobs = sorted(
        (normalize_job(job, generated_at=generated_at) for job in source_jobs),
        key=lambda job: job["id"],
    )
    ids = [job["id"] for job in jobs]
    if len(ids) != len(set(ids)):
        raise SnapshotError("Snapshot contains duplicate normalized job IDs")

    jobs_bytes = canonical_json_bytes(jobs)
    envelope = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "snapshotHash": hashlib.sha256(jobs_bytes).hexdigest(),
        "jobs": jobs,
    }
    raw_body = canonical_json_bytes(envelope)
    if len(raw_body) > MAX_BODY_BYTES:
        raise SnapshotError(
            f"Normalized snapshot is {len(raw_body)} bytes; limit is {MAX_BODY_BYTES}"
        )
    return envelope, raw_body


def ingest_signature(timestamp: str, raw_body: bytes, secret: str) -> str:
    if not secret:
        raise PublishError("CLOUDFLARE_JOBS_INGEST_SECRET is not set")
    message = timestamp.encode("ascii") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def publish_snapshot(
    snapshot: Mapping[str, Any],
    *,
    ingest_url: str,
    secret: str,
    session: Any = requests,
    now: Any = time.time,
) -> dict[str, Any]:
    parsed_url = urlsplit(clean_text(ingest_url))
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise PublishError("CLOUDFLARE_JOBS_INGEST_URL is not a valid HTTP(S) URL")

    envelope, raw_body = build_envelope(snapshot)
    timestamp = str(int(now()))
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Ingest-Timestamp": timestamp,
        "X-Ingest-Signature": ingest_signature(timestamp, raw_body, secret),
    }

    try:
        response = session.post(
            ingest_url,
            data=raw_body,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PublishError(f"Cloudflare ingest request failed: {type(exc).__name__}") from exc

    if not 200 <= response.status_code < 300:
        raise PublishError(f"Cloudflare ingest returned HTTP {response.status_code}")
    return envelope


def _load_snapshot(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotError("Snapshot root must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("live_jobs.json"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Normalize and validate without sending a network request",
    )
    args = parser.parse_args(argv)

    try:
        snapshot = _load_snapshot(args.input)
        if args.dry_run:
            envelope, raw_body = build_envelope(snapshot)
            print(
                f"Validated {len(envelope['jobs'])} jobs "
                f"({len(raw_body)} bytes, hash {envelope['snapshotHash']})"
            )
            return 0

        ingest_url = os.environ.get("CLOUDFLARE_JOBS_INGEST_URL", "")
        secret = os.environ.get("CLOUDFLARE_JOBS_INGEST_SECRET", "")
        envelope = publish_snapshot(
            snapshot,
            ingest_url=ingest_url,
            secret=secret,
        )
        print(
            f"Published {len(envelope['jobs'])} normalized jobs "
            f"from {SOURCE_NAME} (hash {envelope['snapshotHash']})"
        )
        return 0
    except (SnapshotError, PublishError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
