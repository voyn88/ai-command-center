#!/usr/bin/env python3
"""FinOps report: realized cost per meaningful task, by agent and by project.

Answers the ask this script exists for (VOYN-AGT-PERF-PAY) — an agent's
hourly cost judged by *quality*, not by how many tasks it touched. Ranking by
raw task count rewards an agent for opening many cheap, low-value runs; this
report ranks each `(project, agent)` pair by realized spend divided by the
number of tasks that actually reached `completion.CompletionState.COMPLETED`
("merged into the target branch and verified" — see `runtime.completion`).
A run that spent money but never landed still counts in `total_cost_usd` and
`run_count`; it just doesn't shrink the report's `cost_per_meaningful_task`
denominator the way a raw task-count metric would let it.

All figures come from `runtime.cost_report`, which reads the same
provider-reported `total_cost_usd` figure `task_pipeline.daily_spend_usd`
already trusts for budget gating — nothing here estimates a dollar amount.

Usage:
    python scripts/agent_cost_report.py [--project PROJECT] [--format markdown|json] [--out PATH]

With no `--out`, the report is printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from command_center.runtime import db  # noqa: E402
from command_center.runtime.cost_report import (  # noqa: E402
    build_agent_cost_report_from_db,
    render_agent_cost_report_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=None, help="Restrict the report to one project.")
    parser.add_argument(
        "--format", choices=("markdown", "json"), default="markdown", help="Output format (default: markdown).",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write to this path instead of stdout.")
    args = parser.parse_args(argv)

    db_path = db.resolve_db_path()
    db.migrate(db_path)
    rows = build_agent_cost_report_from_db(db_path, project=args.project)

    if args.format == "json":
        rendered = json.dumps([row.as_dict() for row in rows], ensure_ascii=False, indent=2)
    else:
        rendered = render_agent_cost_report_markdown(rows)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
