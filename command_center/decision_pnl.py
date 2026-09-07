"""Decision P&L (VOYN-MIN-COMPANY-MODEL): assigns a cost and a business value
to every Board decision (:mod:`command_center.council`) closed in a week of
operations, and rolls the result up into an automatic report ranking
solutions (projects) by net P&L — the BizDev/Sales comparative artifact the
acceptance criterion asks for.

A council decision carries no spend of its own: :mod:`command_center.dispatch`
is the only place actual task spend is measured, and only as a rolling
trailing-24h total, never attributed to one decision. So, like
`command_center.dispatch.models.TailRiskScenario`, the cost and value here are
priced from *declared* per-solution assumptions — a PM edits
`data/solution_valuation.json` (see the tracked `.example.json` for the shape)
to say what one Board decision on a solution costs and is worth. A solution
missing from that file reports as unpriced, never defaulted to `$0`, so the
report can never quietly claim a decision was free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from command_center import storage
from command_center.council import service as council_service
from command_center.runtime import db as runtime_db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = storage.resolve_data_dir(ROOT)
VALUATIONS_FILE = DATA_DIR / "solution_valuation.json"


@dataclass(frozen=True)
class SolutionValuation:
    """One solution's declared per-decision cost/value assumptions."""

    project: str
    label: str
    cost_per_decision_usd: float
    value_per_decision_usd: float
    assumptions: str = ""

    @property
    def pnl_per_decision_usd(self) -> float:
        return self.value_per_decision_usd - self.cost_per_decision_usd


def load_solution_valuations(path: Path | None = None) -> dict[str, SolutionValuation]:
    """Load the declared per-solution cost/value assumptions.

    A missing or unreadable file returns an empty mapping (every solution then
    reads as unpriced) rather than a fabricated default — the same fail-closed
    posture as `command_center.dispatch.models.SpendMeasurement`.
    """
    target = path or VALUATIONS_FILE
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    valuations: dict[str, SolutionValuation] = {}
    for project, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        try:
            cost = float(entry.get("cost_per_decision_usd", 0.0))
            value = float(entry.get("value_per_decision_usd", 0.0))
        except (TypeError, ValueError):
            continue
        valuations[project] = SolutionValuation(
            project=project,
            label=str(entry.get("label") or project),
            cost_per_decision_usd=cost,
            value_per_decision_usd=value,
            assumptions=str(entry.get("assumptions") or ""),
        )
    return valuations


@dataclass(frozen=True)
class DecisionPnLLine:
    """One Board decision priced against its solution's declared valuation."""

    decision_id: str
    motion_id: str
    title: str
    project: str | None
    outcome: str
    decided_at: str
    priced: bool
    cost_usd: float | None
    value_usd: float | None
    pnl_usd: float | None


def price_decision(
    *,
    decision_id: str,
    motion_id: str,
    title: str,
    project: str | None,
    outcome: str,
    decided_at: str,
    valuations: dict[str, SolutionValuation],
) -> DecisionPnLLine:
    valuation = valuations.get(project) if project else None
    if valuation is None:
        return DecisionPnLLine(
            decision_id=decision_id,
            motion_id=motion_id,
            title=title,
            project=project,
            outcome=outcome,
            decided_at=decided_at,
            priced=False,
            cost_usd=None,
            value_usd=None,
            pnl_usd=None,
        )
    # Governance cost is incurred regardless of outcome (the Board still spent
    # time deliberating); the declared value is only realized once a decision
    # is actually `approved` — a `rejected`/`deferred` decision earns $0 value,
    # not the solution's full sticker value, so a report never overstates what
    # deliberation alone delivered.
    cost = valuation.cost_per_decision_usd
    value = valuation.value_per_decision_usd if outcome == "approved" else 0.0
    return DecisionPnLLine(
        decision_id=decision_id,
        motion_id=motion_id,
        title=title,
        project=project,
        outcome=outcome,
        decided_at=decided_at,
        priced=True,
        cost_usd=cost,
        value_usd=value,
        pnl_usd=value - cost,
    )


def collect_week_decisions(
    week_start: str,
    week_end: str,
    *,
    valuations: dict[str, SolutionValuation] | None = None,
    db_path: Path | None = None,
) -> list[DecisionPnLLine]:
    """Every non-sensitive Board decision closed in `[week_start, week_end)`
    (ISO-8601 strings, compared lexicographically like every other
    `decided_at` comparison in this codebase), priced against `valuations`
    (loaded from disk when not supplied).

    Reuses `command_center.council.service.list_decisions`, so the BANK/LEGAL
    redaction policy already applied there covers this report too — a
    sensitive decision never reaches BizDev/Sales.
    """
    resolved_valuations = (
        load_solution_valuations() if valuations is None else valuations
    )
    path = db_path or resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        runtime_db.migrate(path)

    decision_list = council_service.list_decisions(limit=10_000)
    lines: list[DecisionPnLLine] = []
    for record in decision_list.decisions:
        decision = record.decision
        decided_at = decision.decided_at or ""
        if not (week_start <= decided_at < week_end):
            continue
        motion = runtime_db.get_motion(path, decision.motion_ref)
        project = motion.get("project_ref") if motion else None
        title = (motion.get("title") if motion else None) or decision.motion_ref
        lines.append(
            price_decision(
                decision_id=decision.id,
                motion_id=decision.motion_ref,
                title=title,
                project=project,
                outcome=decision.outcome,
                decided_at=decided_at,
                valuations=resolved_valuations,
            )
        )
    return lines


@dataclass(frozen=True)
class SolutionPnLSummary:
    """One solution's rolled-up decision count and net P&L for the week."""

    project: str
    decision_count: int
    approved_count: int
    rejected_count: int
    deferred_count: int
    priced: bool
    total_cost_usd: float | None
    total_value_usd: float | None
    total_pnl_usd: float | None


@dataclass(frozen=True)
class WeeklyDecisionPnLReport:
    week_start: str
    week_end: str
    generated_at: str
    lines: tuple[DecisionPnLLine, ...]
    # Ranked best net P&L first; unpriced solutions sort last.
    summaries: tuple[SolutionPnLSummary, ...]
    total_cost_usd: float
    total_value_usd: float
    total_pnl_usd: float
    unpriced_decision_count: int


def build_weekly_report(
    lines: list[DecisionPnLLine],
    *,
    week_start: str,
    week_end: str,
    generated_at: str,
) -> WeeklyDecisionPnLReport:
    """Group priced decision lines by solution and rank them by net P&L —
    the automatic comparative report a sales conversation can point at."""
    by_project: dict[str, list[DecisionPnLLine]] = {}
    for line in lines:
        by_project.setdefault(line.project or "(unattributed)", []).append(line)

    summaries: list[SolutionPnLSummary] = []
    for project, group in by_project.items():
        priced_lines = [g for g in group if g.priced]
        outcome_counts = {"approved": 0, "rejected": 0, "deferred": 0}
        for g in group:
            outcome_counts[g.outcome] = outcome_counts.get(g.outcome, 0) + 1
        is_priced = len(priced_lines) == len(group)
        summaries.append(
            SolutionPnLSummary(
                project=project,
                decision_count=len(group),
                approved_count=outcome_counts.get("approved", 0),
                rejected_count=outcome_counts.get("rejected", 0),
                deferred_count=outcome_counts.get("deferred", 0),
                priced=is_priced,
                total_cost_usd=(
                    sum(g.cost_usd for g in priced_lines) if is_priced else None
                ),
                total_value_usd=(
                    sum(g.value_usd for g in priced_lines) if is_priced else None
                ),
                total_pnl_usd=(
                    sum(g.pnl_usd for g in priced_lines) if is_priced else None
                ),
            )
        )
    summaries.sort(key=lambda s: (s.total_pnl_usd is None, -(s.total_pnl_usd or 0.0)))

    priced_all = [line for line in lines if line.priced]
    return WeeklyDecisionPnLReport(
        week_start=week_start,
        week_end=week_end,
        generated_at=generated_at,
        lines=tuple(lines),
        summaries=tuple(summaries),
        total_cost_usd=sum(line.cost_usd for line in priced_all),
        total_value_usd=sum(line.value_usd for line in priced_all),
        total_pnl_usd=sum(line.pnl_usd for line in priced_all),
        unpriced_decision_count=sum(1 for line in lines if not line.priced),
    )


def render_markdown(report: WeeklyDecisionPnLReport) -> str:
    """Render the weekly report as the Markdown artifact BizDev/Sales share."""
    priced_decision_count = len(report.lines) - report.unpriced_decision_count
    out = [
        "# Decision P&L",
        "",
        f"Week: {report.week_start} to {report.week_end}",
        f"Generated: {report.generated_at}",
        "",
        "Comparative solution report for BizDev/Sales (VOYN-MIN-COMPANY-MODEL). "
        "Cost and value are priced from the declared assumptions in "
        "`data/solution_valuation.json` (see `data/solution_valuation.example.json` "
        "for the shape) — never measured — so a solution absent from that file "
        "reports as unpriced rather than a fabricated `$0`.",
        "",
        "## Solutions ranked by net P&L",
        "",
        "| Solution | Decisions | Approved | Rejected | Deferred | Cost (USD) | Value (USD) | Net P&L (USD) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in report.summaries:
        if s.priced:
            cost = f"{s.total_cost_usd:,.2f}"
            value = f"{s.total_value_usd:,.2f}"
            pnl = f"{s.total_pnl_usd:,.2f}"
        else:
            cost = value = pnl = "unpriced"
        out.append(
            f"| {s.project} | {s.decision_count} | {s.approved_count} | "
            f"{s.rejected_count} | {s.deferred_count} | {cost} | {value} | {pnl} |"
        )
    out += [
        "",
        "## Company totals (priced decisions only)",
        "",
        f"- Decisions priced: {priced_decision_count} of {len(report.lines)}",
        f"- Total cost: {report.total_cost_usd:,.2f} USD",
        f"- Total value: {report.total_value_usd:,.2f} USD",
        f"- Total net P&L: {report.total_pnl_usd:,.2f} USD",
    ]
    if report.unpriced_decision_count:
        out += [
            "",
            f"**{report.unpriced_decision_count} decision(s) excluded from the "
            "totals above** — their solution has no entry in "
            "`data/solution_valuation.json`.",
        ]
    out.append("")
    return "\n".join(out)
