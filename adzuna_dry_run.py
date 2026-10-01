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


def build_report(records, coverage):
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
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "adzuna",
        "status": coverage.get("status")
        or ("complete" if coverage.get("complete") else "incomplete"),
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
        ):
            summary.write(f"| {label} | {report[key]} |\n")
        summary.write("\n**Per-query allocation**\n\n")
        for query in report["queries"]:
            summary.write(
                f"- `{query['name']}`: total={query['total']}, "
                f"returned={query['returned']}, kept={query['kept']}, "
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
    report = build_report(records, coverage)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    write_summary(report)
    print(payload)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
