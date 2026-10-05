"""Block suspicious verified-job catalog drops before publication."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from publish_snapshot import SnapshotError, build_envelope


DEFAULT_MAX_DROP_PERCENT = 25.0


class CatalogDropError(RuntimeError):
    """The new verified catalog is suspiciously smaller than its baseline."""


@dataclass(frozen=True)
class CatalogAssessment:
    current_count: int
    baseline_count: int
    minimum_allowed: int
    drop_percent: float
    suspicious: bool
    overridden: bool


def normalized_count(snapshot: Mapping[str, Any]) -> int:
    envelope, _ = build_envelope(snapshot)
    return len(envelope["jobs"])


def assess_catalog_drop(
    current_count: int,
    baseline_counts: Sequence[int],
    *,
    max_drop_percent: float = DEFAULT_MAX_DROP_PERCENT,
    allow_drop: bool = False,
) -> CatalogAssessment:
    if current_count < 0 or any(count < 0 for count in baseline_counts):
        raise ValueError("Catalog counts must be non-negative")
    if not 0 <= max_drop_percent < 100:
        raise ValueError("max_drop_percent must be between 0 and 100")

    baseline_count = max(baseline_counts, default=0)
    minimum_allowed = math.ceil(baseline_count * (1 - max_drop_percent / 100))
    drop_percent = (
        ((baseline_count - current_count) / baseline_count) * 100
        if baseline_count
        else 0.0
    )
    suspicious = bool(baseline_count and current_count < minimum_allowed)
    assessment = CatalogAssessment(
        current_count=current_count,
        baseline_count=baseline_count,
        minimum_allowed=minimum_allowed,
        drop_percent=drop_percent,
        suspicious=suspicious,
        overridden=suspicious and allow_drop,
    )

    if suspicious and not allow_drop:
        raise CatalogDropError(
            f"Normalized catalog dropped from {baseline_count} to {current_count} "
            f"({drop_percent:.1f}%); minimum allowed is {minimum_allowed}. "
            "Re-run manually with allow_catalog_drop only after verifying the drop."
        )
    return assessment


def load_snapshot(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Could not read catalog baseline {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotError(f"Catalog baseline root must be an object: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, action="append", default=[])
    parser.add_argument(
        "--max-drop-percent",
        type=float,
        default=DEFAULT_MAX_DROP_PERCENT,
    )
    parser.add_argument("--allow-drop", action="store_true")
    args = parser.parse_args(argv)

    try:
        current_snapshot = load_snapshot(args.current)
        public_states = (current_snapshot.get("source_state") or {}).get("public_boards") or {}
        blocked_sources = [
            key for key, state in public_states.items()
            if state.get("status") == "blocked"
        ]
        if blocked_sources:
            raise CatalogDropError(
                "Public source coverage blocks publication: " + ", ".join(blocked_sources)
            )
        adzuna_state = (
            (current_snapshot.get("source_state") or {}).get("adzuna") or {}
        )
        adzuna_expired = adzuna_state.get("status") == "expired"
        if adzuna_expired and not args.allow_drop:
            raise CatalogDropError(
                "Adzuna source grace expired; manual review and "
                "allow_catalog_drop are required"
            )
        current_count = normalized_count(current_snapshot)
        baseline_counts = [
            normalized_count(load_snapshot(path))
            for path in args.baseline
            if path.exists()
        ]
        assessment = assess_catalog_drop(
            current_count,
            baseline_counts,
            max_drop_percent=args.max_drop_percent,
            allow_drop=args.allow_drop,
        )
    except (CatalogDropError, SnapshotError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    message = (
        f"Catalog guard: current={assessment.current_count}, "
        f"baseline={assessment.baseline_count}, "
        f"minimum={assessment.minimum_allowed}, "
        f"drop={assessment.drop_percent:.1f}%"
    )
    if assessment.overridden:
        print(f"::warning::{message}; manual override accepted")
    elif adzuna_expired:
        print(f"::warning::{message}; expired Adzuna source override accepted")
    else:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
