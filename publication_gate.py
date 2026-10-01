"""Validate required publication outcomes for the refresh workflow."""

from __future__ import annotations

import argparse
import sys


def publication_errors(
    *,
    catalog_guard_outcome: str,
    cloudflare_enabled: bool,
    cloudflare_outcome: str,
    needs_vercel: bool,
    vercel_outcome: str,
) -> list[str]:
    errors = []
    if catalog_guard_outcome != "success":
        errors.append("Catalog drop protection blocked publication")
    if cloudflare_enabled and cloudflare_outcome != "success":
        errors.append("Cloudflare snapshot publication failed")
    if (
        catalog_guard_outcome == "success"
        and needs_vercel
        and vercel_outcome != "success"
    ):
        errors.append("Vercel production deployment failed")
    return errors


def _bool(value: str) -> bool:
    return value.lower() == "true"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-guard-outcome", required=True)
    parser.add_argument("--cloudflare-enabled", required=True)
    parser.add_argument("--cloudflare-outcome", default="")
    parser.add_argument("--needs-vercel", required=True)
    parser.add_argument("--vercel-outcome", default="")
    args = parser.parse_args(argv)

    errors = publication_errors(
        catalog_guard_outcome=args.catalog_guard_outcome,
        cloudflare_enabled=_bool(args.cloudflare_enabled),
        cloudflare_outcome=args.cloudflare_outcome,
        needs_vercel=_bool(args.needs_vercel),
        vercel_outcome=args.vercel_outcome,
    )
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
