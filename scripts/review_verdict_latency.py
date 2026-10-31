#!/usr/bin/env python3
"""Measure median / p95 independent-review verdict latency.

This is the evidence half of VOYN-W0-AICC-REVIEW-RISK-TIER-REM, whose
acceptance criterion is "median/p95 verdict time measured before/after".
It reads the work queue, groups review work items into review cycles, and
prints how long each cycle took to reach a verdict. All of the grouping and
statistics live in `command_center.orchestrator.review_merge` so they are
unit-testable without a database; this file is argument parsing, one SQL
query, and printing.

Usage:
    python scripts/review_verdict_latency.py --days 7
    python scripts/review_verdict_latency.py --days 14 --split-policy
    python scripts/review_verdict_latency.py --days 14 --before v8 --after v9

`--split-policy` reports one summary per `_REVIEW_POLICY_VERSION` found in
the keys. Because risk tiering bumped that constant v8 -> v9, the v8 bucket
IS the "before" measurement and the v9 bucket IS the "after" one, taken from
the same rows under the same definition -- which is the only way the two
numbers are comparable. `--before`/`--after` names the two buckets
explicitly and adds a delta line.

Read-only: it issues one SELECT and writes nothing.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from command_center.orchestrator.review_merge import (  # noqa: E402
    VerdictRow,
    review_cycle_and_chunk,
    summarize_verdict_latencies,
)

#: One row per review work item in the window. The join is `work_item.
#: result_id = work_result.result_id` -- the item's OWN acknowledged result,
#: the one `work_item_succeeded_has_result` makes inseparable from the
#: 'succeeded' state -- and NOT `work_result.work_item_id`, which would also
#: match a result row written for some other attempt on the same item. The
#: `wi.state = 'succeeded'` predicate is then redundant with that join by
#: construction, and is kept anyway: `review_verdict_latencies` documents a succeeded
#: gate, so the query states it rather than leaving a reader to reconstruct
#: it from a CHECK constraint in another file. An item that is still
#: ready/claimed, or that went dead, comes back with a NULL result and is
#: dropped as in-flight by the caller.
_ROWS_SQL = """
SELECT wi.idempotency_key, wi.state, wi.created_at, wr.created_at
  FROM work_item wi
  LEFT JOIN work_result wr
    ON wr.result_id = wi.result_id
   AND wi.state = 'succeeded'
 WHERE wi.idempotency_key LIKE 'review:%%'
   AND wi.created_at >= %s
 ORDER BY wi.created_at
"""

_POLICY = re.compile(r":(v[0-9]+):base:")


def _policy_version(idempotency_key: str) -> str:
    match = _POLICY.search(idempotency_key)
    return match.group(1) if match else "unknown"


def _fetch_rows(conn, window_start: datetime) -> list[VerdictRow]:
    """Every review work item enqueued at or after `window_start`.

    Bounded by time, not by LIMIT, and deliberately so: a `LIMIT`-bounded
    `ORDER BY created_at DESC` scan can cut a review cycle in half at the
    far edge and make it look like it finished sooner than it did. The time
    window has the same edge, but it is an edge the caller knows the
    position of, so `summarize_verdict_latencies(window_start=...)` can drop
    the cycles that straddle it instead of mismeasuring them."""
    with conn.cursor() as cur:
        cur.execute(_ROWS_SQL, (window_start,))
        return [
            VerdictRow(
                idempotency_key=key,
                state=state,
                enqueued_at=enqueued_at,
                result_at=result_at,
            )
            for key, state, enqueued_at, result_at in cur.fetchall()
        ]


def _report(
    label: str, rows: list[VerdictRow], window_start: datetime
) -> None:
    summary = summarize_verdict_latencies(rows, window_start=window_start)
    print(f"{label:>12}  {summary.render()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=float, default=7.0,
        help="how far back to read work items (default: 7)",
    )
    parser.add_argument(
        "--split-policy", action="store_true",
        help="one summary per review policy version found in the keys",
    )
    parser.add_argument(
        "--before", default=None,
        help="policy version to label as the before measurement (e.g. v8)",
    )
    parser.add_argument(
        "--after", default=None,
        help="policy version to label as the after measurement (e.g. v9)",
    )
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be positive")
    if (args.before is None) != (args.after is None):
        parser.error("--before and --after must be given together")

    from command_center.db import pool
    from command_center.db.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    window_start = datetime.now(timezone.utc) - timedelta(days=args.days)
    pool.open_pool(config)
    try:
        with pool.connection() as conn:
            rows = _fetch_rows(conn, window_start)
    finally:
        pool.close_pool()

    print(f"window: {window_start.isoformat()} .. now  ({len(rows)} work items)")
    _report("all", rows, window_start)

    buckets: dict[str, list[VerdictRow]] = {}
    for row in rows:
        if review_cycle_and_chunk(row.idempotency_key) is None:
            continue
        buckets.setdefault(_policy_version(row.idempotency_key), []).append(row)

    if args.split_policy or args.before is not None:
        for policy in sorted(buckets):
            _report(policy, buckets[policy], window_start)

    if args.before is not None:
        before = summarize_verdict_latencies(
            buckets.get(args.before, []), window_start=window_start
        )
        after = summarize_verdict_latencies(
            buckets.get(args.after, []), window_start=window_start
        )
        if before.median_seconds is None or after.median_seconds is None:
            print(
                f"delta: not computable -- {args.before} has {before.cycles} "
                f"measured cycles, {args.after} has {after.cycles}"
            )
            return 1
        print(
            f"delta: median {after.median_seconds - before.median_seconds:+.1f}s  "
            f"p95 {after.p95_seconds - before.p95_seconds:+.1f}s  "
            f"({args.before} n={before.cycles} -> {args.after} n={after.cycles})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
