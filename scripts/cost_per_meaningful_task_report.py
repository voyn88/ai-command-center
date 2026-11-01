#!/usr/bin/env python3
"""Cost-per-meaningful-task report CLI (VOYN-MIN-AGT-COST-METRIC).

Reports recorded runtime runs only, grouped by executor. Shows the known cost
subtotal and coverage; total cost and cost per verified task are unknown when
any run lacks a valid provider-reported cost. Queue-only jobs are not included.
A task counts only after the existing completion pipeline verifies completion.

See `command_center.cost_report` for the full computation and its
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
        help="Print a JSON array of agent reports instead of a markdown table.",
    )
    args = parser.parse_args(argv)

    from command_center.cost_report import (
        cost_per_meaningful_task,
        render_markdown,
    )
    from command_center.runtime import db as runtime_db

    db_path = args.db_path or runtime_db.resolve_db_path()
    reports = cost_per_meaningful_task(db_path)

    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2))
    else:
        print(render_markdown(reports), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
