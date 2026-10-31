#!/usr/bin/env python3
"""Cost-per-meaningful-task report CLI (VOYN-MIN-AGT-COST-METRIC).

Prints one row per agent (`run.provider_id`): how much it actually cost
(summed from each run's own reported `total_cost_usd`) to produce one
*meaningful* completed task (`completion_state == COMPLETED`) — a FinOps
metric against finished, verified work, never raw speed or run count alone.

See `command_center.runtime.cost_report` for the full computation and its
definitions of "cost" and "meaningful task".

Usage:
    python scripts/cost_per_meaningful_task_report.py [--db-path PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from command_center.runtime import db as runtime_db  # noqa: E402
from command_center.runtime.cost_report import (  # noqa: E402
    cost_per_meaningful_task,
    render_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to runtime.db (defaults to the standard resolved data dir).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object per agent instead of a markdown table.",
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or runtime_db.resolve_db_path()
    reports = cost_per_meaningful_task(db_path)

    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2))
    else:
        print(render_markdown(reports), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
