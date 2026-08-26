"""Coverage check: read existing coverage data and flag a low line-rate.

Reads a Cobertura ``coverage.xml`` report if one is present under the target and
raises a ``coverage`` finding when the overall line-rate is below a threshold.
When no coverage data exists it raises a single ``info`` finding recording the
gap (rather than silently passing — "no data" is itself worth surfacing). Real
and in-repo: it consumes coverage data the project already produces, never runs
the test-suite itself, and makes no network call.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

from command_center.audit.checks.base import Check
from command_center.audit.types import CheckContext, Finding, default_owner_for

#: Report file names looked for under the target, in order.
_REPORT_NAMES = ("coverage.xml",)

#: Default minimum acceptable line coverage (fraction 0..1).
_DEFAULT_MIN_COVERAGE = 0.80


def _parse_line_rate(report: Path) -> float | None:
    """The overall ``line-rate`` (0..1) from a Cobertura XML report, or ``None``
    if the file is absent/unparseable/has no rate."""
    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError):
        return None
    raw = root.get("line-rate")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class CoverageCheck(Check):
    """Raise a ``coverage`` finding when line coverage is below the threshold, or
    an ``info`` finding when no coverage data is available."""

    name: ClassVar[str] = "coverage"
    category: ClassVar[str] = "coverage"

    def run(self, ctx: CheckContext) -> list[Finding]:
        min_coverage = float(ctx.options.get("min_coverage", _DEFAULT_MIN_COVERAGE))
        owner = default_owner_for(self.category)
        report = self._find_report(ctx.target)
        if report is None:
            return [
                Finding(
                    category=self.category,
                    summary="No coverage report (coverage.xml) found; coverage is unknown",
                    owner=owner,
                    severity="info",
                    source=self.name,
                )
            ]
        rate = _parse_line_rate(report)
        try:
            rel = str(report.relative_to(ctx.target))
        except ValueError:
            rel = report.name
        if rate is None:
            return [
                Finding(
                    category=self.category,
                    summary="Coverage report present but its line-rate could not be read",
                    owner=owner,
                    severity="info",
                    file_path=rel,
                    source=self.name,
                )
            ]
        if rate >= min_coverage:
            return []
        pct = round(rate * 100, 1)
        threshold_pct = round(min_coverage * 100, 1)
        return [
            Finding(
                category=self.category,
                summary=(
                    f"Line coverage {pct}% is below the {threshold_pct}% threshold"
                ),
                owner=owner,
                severity="medium" if rate >= min_coverage / 2 else "high",
                file_path=rel,
                source=self.name,
            )
        ]

    def _find_report(self, target: Path) -> Path | None:
        for name in _REPORT_NAMES:
            candidate = target / name
            if candidate.is_file():
                return candidate
        return None
