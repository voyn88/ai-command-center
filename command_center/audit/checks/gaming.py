"""Metrics-manipulation check: flags runs whose self-report looks "fast and dirty".

Reads the audited project's most recent runs through the same unified read
model the Runs/Timeline/Executive pages use
(:func:`command_center.runtime.runs_read.list_unified_runs`), scores each with
:class:`command_center.audit.gaming_score.GamingDetector` and raises one
``gaming`` finding per run whose composite risk clears the threshold. This is
the check-plumbing wiring for the detection formula and penalty coefficients
defined in :mod:`command_center.audit.gaming_score` — this module owns no
scoring logic of its own.
"""

from __future__ import annotations

import sqlite3
from typing import ClassVar

from command_center.audit.checks.base import Check
from command_center.audit.gaming_score import GamingDetector, signals_from_run
from command_center.audit.types import CheckContext, Finding, default_owner_for
from command_center.runtime import runs_read

#: How many of the project's newest runs one pass inspects. Bounded so a busy
#: install's audit pass stays cheap; tunable via `ctx.options['gaming_run_limit']`.
_DEFAULT_RUN_LIMIT = 50

#: Composite risk (`GamingScore.risk`) at/above which a run is flagged. Tunable
#: via `ctx.options['gaming_risk_threshold']`.
_DEFAULT_RISK_THRESHOLD = 0.6


def _severity_for_risk(risk: float) -> str:
    if risk >= 0.85:
        return "high"
    if risk >= _DEFAULT_RISK_THRESHOLD:
        return "medium"
    return "low"


class GamingDetectionCheck(Check):
    """Raise a ``gaming`` finding for each recent run whose self-report shows
    signs of metric manipulation (a suspiciously fast, unvalidated or hollow
    "done" claim)."""

    name: ClassVar[str] = "gaming"
    category: ClassVar[str] = "gaming"

    def run(self, ctx: CheckContext) -> list[Finding]:
        limit = int(ctx.options.get("gaming_run_limit", _DEFAULT_RUN_LIMIT))
        threshold = float(ctx.options.get("gaming_risk_threshold", _DEFAULT_RISK_THRESHOLD))
        owner = default_owner_for(self.category)

        try:
            runs = runs_read.list_unified_runs(ctx.db_path, root=ctx.root, limit=limit)
        except (sqlite3.Error, OSError):
            # No runtime db yet (or it isn't migrated) is a data gap, not a
            # manipulation signal — surface it as info rather than raising, same
            # as the coverage check's "no coverage.xml" case.
            return [
                Finding(
                    category=self.category,
                    summary="No runtime run data available; metric-manipulation risk is unknown",
                    owner=owner,
                    severity="info",
                    source=self.name,
                )
            ]

        detector = GamingDetector()
        findings: list[Finding] = []
        for run in runs:
            if run.get("project") != ctx.project:
                continue
            score = detector.detect(signals_from_run(run))
            if score.risk < threshold:
                continue
            run_id = str(run.get("id") or "")
            reasons = "; ".join(score.reasons()) or "composite risk over threshold"
            findings.append(
                Finding(
                    category=self.category,
                    summary=(
                        f"Run {run_id[:8]} looks like a fast/dirty completion "
                        f"(risk={score.risk:.2f}): {reasons}"
                    ),
                    owner=owner,
                    severity=_severity_for_risk(score.risk),
                    loc=run_id or None,
                    dedup_key=f"gaming|{run_id}" if run_id else "",
                    source=self.name,
                )
            )
        return findings
