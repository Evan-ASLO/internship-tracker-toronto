"""Run the credentialed Adzuna adapter without publishing or mutating snapshots."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import scrape


def build_report(records, coverage, baseline_jobs=None):
    filtered = []
    for source in records:
        record = dict(source)
        record["title"] = scrape.clean_text(record.get("title"))
        record["loc"] = scrape.clean_text(record.get("loc"))
        if not record["title"] or not record.get("url"):
            continue
        if not scrape.is_internship(record["title"]):
            continue
        region = scrape.geo(record["loc"])
        if not region:
            continue
        record["geo"] = region
        record["cats"] = scrape.categorize(record["title"])
        filtered.append(record)

    unique = []
    seen = set()
    for record in filtered:
        identity = (
            record.get("external_id")
            or record.get("url")
            or (
                record.get("company"),
                record.get("title"),
                record.get("loc"),
            )
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(record)

    categories = Counter()
    for record in unique:
        categories.update(record.get("cats") or ["Uncategorized"])
    baseline_jobs = baseline_jobs or []
    baseline_non_adzuna = [
        job for job in baseline_jobs
        if isinstance(job, dict) and job.get("src") != "adzuna"
    ]
    overlap = []
    for record in unique:
        if any(scrape.same_listing(record, job) for job in baseline_non_adzuna):
            overlap.append(record)
    title_lengths = [len(record["title"]) for record in unique]
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "adzuna",
        "status": coverage.get("status")
        or ("complete" if coverage.get("complete") else "incomplete"),
        "sourceRefreshedAt": coverage.get("source_refreshed_at"),
        "sourceAgeHours": coverage.get("source_age_hours"),
        "reused": coverage.get("reused", 0),
        "queryTotal": coverage.get("query_total"),
        "requests": coverage.get("request_attempts", coverage.get("pages", 0)),
        "returned": coverage.get("returned", 0),
        "adapterUnique": coverage.get("records", len(records)),
        "productFiltered": len(filtered),
        "productUnique": len(unique),
        "cappedQueries": coverage.get("capped_terms", 0),
        "errors": coverage.get("errors", []),
        "queries": coverage.get("queries", []),
        "companies": dict(Counter(record["company"] for record in unique)),
        "categories": dict(categories),
        "baselineNonAdzuna": len(baseline_non_adzuna),
        "overlapWithBaseline": len(overlap),
        "projectedSnapshotTotal": len(baseline_non_adzuna) + len(unique),
        "projectedDistinctTotal": (
            len(baseline_non_adzuna) + len(unique) - len(overlap)
        ),
        "titleStats": {
            "count": len(title_lengths),
            "averageLength": (
                round(sum(title_lengths) / len(title_lengths), 1)
                if title_lengths else 0
            ),
            "maxLength": max(title_lengths, default=0),
        },
        "sampleJobs": [
            {
                "company": record["company"],
                "title": record["title"],
                "location": record["loc"],
            }
            for record in sorted(
                unique,
                key=lambda item: (item["company"].casefold(), item["title"].casefold()),
            )[:12]
        ],
    }


def write_summary(report):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("\n### Adzuna dry run\n\n")
        summary.write("| Metric | Value |\n|---|---:|\n")
        for label, key in (
            ("API query total", "queryTotal"),
            ("Requests", "requests"),
            ("Rows returned", "returned"),
            ("Adapter unique", "adapterUnique"),
            ("Product filtered", "productFiltered"),
            ("Product unique", "productUnique"),
            ("Capped queries", "cappedQueries"),
            ("Reused prior rows", "reused"),
            ("Baseline non-Adzuna", "baselineNonAdzuna"),
            ("Baseline overlap", "overlapWithBaseline"),
            ("Projected snapshot total", "projectedSnapshotTotal"),
        ):
            summary.write(f"| {label} | {report[key]} |\n")
        summary.write("\n**Per-query allocation**\n\n")
        for query in report["queries"]:
            summary.write(
                f"- `{query['name']}`: total={query['total']}, "
                f"page={query.get('page', '-')}, "
                f"returned={query['returned']}, kept={query['kept']}, "
                f"rejected={query.get('rejected', 0)}, "
                f"capped={str(query['capped']).lower()}, "
                f"status={query['status']}\n"
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if not os.environ.get("ADZUNA_APP_ID") or not os.environ.get("ADZUNA_APP_KEY"):
        print("::error::ADZUNA_APP_ID and ADZUNA_APP_KEY are required", file=sys.stderr)
        return 1

    scrape.reset_coverage()
    records = scrape.from_adzuna(None)
    coverage = scrape.COVERAGE[scrape.coverage_key("adzuna", "ca/Toronto")]
    baseline_jobs = []
    baseline_path = os.environ.get("ADZUNA_BASELINE_PATH", "live_jobs.json")
    try:
        baseline_jobs = json.loads(
            Path(baseline_path).read_text(encoding="utf-8")
        ).get("jobs", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    report = build_report(records, coverage, baseline_jobs)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    write_summary(report)
    print(payload)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
