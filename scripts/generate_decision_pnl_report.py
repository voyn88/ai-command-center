from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def main() -> int:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from command_center.decision_pnl import (
        build_weekly_report,
        collect_week_decisions,
        render_markdown,
    )

    parser = argparse.ArgumentParser(
        description=(
            "Generate the weekly Decision P&L comparative report for "
            "BizDev/Sales (VOYN-MIN-COMPANY-MODEL)."
        )
    )
    parser.add_argument(
        "--week-start",
        help="ISO-8601 start, inclusive. Default: 7 days before --week-end.",
    )
    parser.add_argument(
        "--week-end", help="ISO-8601 end, exclusive. Default: now (UTC)."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path. Default: reports/decision_pnl_<week-start>_to_<week-end>.md",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    week_end = args.week_end or _iso(now)
    week_start = args.week_start or _iso(now - timedelta(days=7))

    lines = collect_week_decisions(week_start, week_end)
    report = build_weekly_report(
        lines,
        week_start=week_start,
        week_end=week_end,
        generated_at=_iso(now),
    )
    markdown = render_markdown(report)

    output = args.output or (
        REPO_ROOT
        / "reports"
        / f"decision_pnl_{week_start[:10]}_to_{week_end[:10]}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
