#!/usr/bin/env python3
"""Short-interval runtime trigger for the Wave-2 Audit engine.

``command_center.api.audit_service.run_audit`` is otherwise reachable only by
an explicit ``POST /audit/run`` or the once-daily self-audit campaign
(``command_center.daily_audit``, a much heavier product/engineering pipeline).
This script is the lightweight "real-time" seam: run it on a cron/systemd/
launchd timer every few minutes to get early, unattended tracking of the five
finding categories (security, lint, code-quality, deps, coverage) instead of
waiting for a human to remember or a day to pass.

``auto_trigger`` is itself a no-op for any project audited within the
configured interval, so invoking this script often (even every minute) is
safe — it never runs the checks twice within the window, it just reports the
skip.

Usage::

    python scripts/findings_auto_trigger.py
    python scripts/findings_auto_trigger.py --project AICC --project AIOS
    python scripts/findings_auto_trigger.py --min-interval-seconds 300
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from command_center.api import audit_schemas as a  # noqa: E402
from command_center.api import audit_service  # noqa: E402
from command_center.models import PROJECT_IDS, SENSITIVE_PROJECT_IDS  # noqa: E402

#: Every project the daemon considers by default when ``--project`` is not
#: given. Sensitive projects are excluded up front — ``audit_service`` would
#: skip them anyway, but not asking avoids a wasted round trip every tick.
DEFAULT_PROJECTS: tuple[str, ...] = tuple(
    p for p in PROJECT_IDS if p not in SENSITIVE_PROJECT_IDS
)


def run_once(projects: list[str], *, min_interval_seconds: int | None) -> list[dict]:
    """Auto-trigger every project in ``projects`` and return one summary dict
    per project, in order. Never raises for a project skip (not due, or
    sensitive) — only an unknown check name propagates, exactly like the
    ``/audit/auto-trigger`` endpoint."""
    summaries: list[dict] = []
    for project in projects:
        payload = a.AutoTriggerRequest(
            project=project, min_interval_seconds=min_interval_seconds
        )
        result = audit_service.auto_trigger(payload)
        summaries.append(
            {
                "project": result.project,
                "ran": result.ran,
                "reason": result.reason,
                "finding_count": len(result.findings),
                "deduped": result.deduped,
            }
        )
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        action="append",
        dest="projects",
        help="Project id to trigger (repeatable). Default: every non-sensitive project.",
    )
    parser.add_argument(
        "--min-interval-seconds",
        type=int,
        default=None,
        help="Override AICC_AUDIT_AUTO_TRIGGER_INTERVAL_SECONDS / the built-in default.",
    )
    args = parser.parse_args(argv)
    projects = args.projects or list(DEFAULT_PROJECTS)

    try:
        summaries = run_once(projects, min_interval_seconds=args.min_interval_seconds)
    except KeyError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1

    for summary in summaries:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
