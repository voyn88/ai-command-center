#!/usr/bin/env python3
"""Median / p95 independent-review verdict latency, measured from the
queue's own timestamps -- the before/after evidence
VOYN-W0-AICC-REVIEW-RISK-TIER's acceptance criteria calls for. Run it once
before the risk-tiered chunk budget ships and again after, over a comparable
window, to see whether it moved:

    python scripts/review_verdict_latency.py

The aggregation itself (grouping every attempt at every chunk of one review
cycle, and refusing to guess at a cycle that has not fully landed) lives in
`command_center.orchestrator.review_merge.review_verdict_latencies`, tested
independently of any database. This script is only the SQL that turns the
two tables the queue already writes -- `work_item_public`, `work_result` --
into the rows that function expects.

Reads AICC's ordinary PostgreSQL connection pool configuration (the same the
rest of the control plane connects with -- see `command_center/db/pool.py`
and `config.py`); a host with no pool configured gets that error directly
rather than a guessed-at DSN.
"""

from __future__ import annotations

import argparse
import sys


def _fetch_rows(limit: int) -> list[tuple[str, str, object, object]]:
    from command_center.db import pool

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT wi.task_id, wi.idempotency_key, wi.created_at, wr.created_at "
            "FROM work_item_public wi "
            "LEFT JOIN work_result wr ON wr.result_id = wi.result_id "
            "WHERE wi.idempotency_key LIKE 'review:%%' "
            "ORDER BY wi.created_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=20_000,
        help="most recent review-class work items to scan (default: 20000). "
        "Bounded scans can undercount a cycle whose earlier chunks fall "
        "outside the window; widen this if the report below looks short.",
    )
    args = parser.parse_args(argv)

    # Imported lazily so `--help` works without a database configured.
    from command_center.orchestrator.review_merge import (
        median_and_p95,
        review_verdict_latencies,
    )

    rows = _fetch_rows(args.limit)
    latencies = review_verdict_latencies(rows)
    summary = median_and_p95(latencies)
    if summary is None:
        print("no fully-landed review cycles in the scanned window", file=sys.stderr)
        return 1
    median, p95 = summary
    print(
        f"{len(latencies)} fully-landed review cycle(s) out of {len(rows)} "
        f"scanned work item(s) -- median {median:.1f}s, p95 {p95:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
