"""Tests for the Decision P&L report (VOYN-MIN-COMPANY-MODEL).

Pure pricing/aggregation/rendering logic is tested directly on
`DecisionPnLLine` fixtures. `collect_week_decisions` is tested end to end
against the per-test `AICC_DATA_DIR` sandbox, driving real Board decisions
through `command_center.council.service` (the same path production code
uses), so the project attribution and BANK/LEGAL redaction are exercised for
real rather than assumed.

Fixtures use only generic project codes and invented ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.api import council_schemas as s
from command_center.council import service as council_service
from command_center.decision_pnl import (
    DecisionPnLLine,
    SolutionValuation,
    build_weekly_report,
    collect_week_decisions,
    load_solution_valuations,
    price_decision,
    render_markdown,
)
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(council_service.ROOT))


VALUATIONS = {
    "AML": SolutionValuation(
        project="AML",
        label="AML platform",
        cost_per_decision_usd=100.0,
        value_per_decision_usd=500.0,
    )
}


def _decide(project: str, choice: str = "yes") -> str:
    """Open a one-vote motion for `project`, cast `choice`, close it, and
    return the decision's outcome ("approved"/"rejected"/"deferred")."""
    motion = council_service.create_motion(
        s.MotionCreate(title=f"Adopt {project}", proposed_by="chair", project_ref=project)
    )
    council_service.cast_vote(
        motion.id, s.VoteCreate(voter_id="chair", choice=choice)
    )
    record = council_service.close_motion(motion.id)
    return record.decision.outcome


# --- load_solution_valuations ---------------------------------------------


def test_missing_valuations_file_is_empty(tmp_path: Path) -> None:
    assert load_solution_valuations(tmp_path / "missing.json") == {}


def test_load_valuations_from_file(tmp_path: Path) -> None:
    path = tmp_path / "solution_valuation.json"
    path.write_text(
        '{"AML": {"label": "AML", "cost_per_decision_usd": 10, '
        '"value_per_decision_usd": 40}}',
        encoding="utf-8",
    )
    valuations = load_solution_valuations(path)
    assert valuations["AML"].pnl_per_decision_usd == 30


def test_malformed_valuations_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "solution_valuation.json"
    path.write_text("not json", encoding="utf-8")
    assert load_solution_valuations(path) == {}


# --- price_decision ---------------------------------------------------------


def test_priced_decision_computes_pnl() -> None:
    line = price_decision(
        decision_id="d1",
        motion_id="m1",
        title="Adopt X",
        project="AML",
        outcome="approved",
        decided_at="2026-09-01T00:00:00Z",
        valuations=VALUATIONS,
    )
    assert line.priced is True
    assert line.cost_usd == 100.0
    assert line.value_usd == 500.0
    assert line.pnl_usd == 400.0


def test_unvalued_solution_is_unpriced_not_zero() -> None:
    line = price_decision(
        decision_id="d2",
        motion_id="m2",
        title="Adopt Y",
        project="UNKNOWN",
        outcome="approved",
        decided_at="2026-09-01T00:00:00Z",
        valuations=VALUATIONS,
    )
    assert line.priced is False
    assert line.cost_usd is None
    assert line.value_usd is None
    assert line.pnl_usd is None


def test_rejected_decision_still_costs_but_earns_no_value() -> None:
    line = price_decision(
        decision_id="d4",
        motion_id="m4",
        title="Adopt X",
        project="AML",
        outcome="rejected",
        decided_at="2026-09-01T00:00:00Z",
        valuations=VALUATIONS,
    )
    assert line.priced is True
    assert line.cost_usd == 100.0
    assert line.value_usd == 0.0
    assert line.pnl_usd == -100.0


def test_unattributed_decision_is_unpriced() -> None:
    line = price_decision(
        decision_id="d3",
        motion_id="m3",
        title="Adopt Z",
        project=None,
        outcome="deferred",
        decided_at="2026-09-01T00:00:00Z",
        valuations=VALUATIONS,
    )
    assert line.priced is False


# --- build_weekly_report -----------------------------------------------------


def _line(project, outcome, cost, value, priced=True) -> DecisionPnLLine:
    return DecisionPnLLine(
        decision_id=f"d-{project}-{outcome}",
        motion_id=f"m-{project}-{outcome}",
        title="t",
        project=project,
        outcome=outcome,
        decided_at="2026-09-01T00:00:00Z",
        priced=priced,
        cost_usd=cost if priced else None,
        value_usd=value if priced else None,
        pnl_usd=(value - cost) if priced else None,
    )


def test_build_weekly_report_ranks_by_net_pnl() -> None:
    lines = [
        _line("AML", "approved", 100.0, 900.0),  # pnl 800
        _line("ESF", "approved", 100.0, 300.0),  # pnl 200
        _line("UNKNOWN", "deferred", 0.0, 0.0, priced=False),
    ]
    report = build_weekly_report(
        lines,
        week_start="2026-09-01T00:00:00Z",
        week_end="2026-09-08T00:00:00Z",
        generated_at="2026-09-08T00:00:00Z",
    )
    assert [s.project for s in report.summaries] == ["AML", "ESF", "UNKNOWN"]
    assert report.summaries[0].total_pnl_usd == 800.0
    assert report.summaries[-1].priced is False
    assert report.total_cost_usd == 200.0
    assert report.total_value_usd == 1200.0
    assert report.total_pnl_usd == 1000.0
    assert report.unpriced_decision_count == 1


def test_render_markdown_reports_unpriced_without_fabricating_zero() -> None:
    lines = [_line("UNKNOWN", "approved", 0.0, 0.0, priced=False)]
    report = build_weekly_report(
        lines,
        week_start="2026-09-01T00:00:00Z",
        week_end="2026-09-08T00:00:00Z",
        generated_at="2026-09-08T00:00:00Z",
    )
    markdown = render_markdown(report)
    assert "| UNKNOWN | 1 | 1 | 0 | 0 | unpriced | unpriced | unpriced |" in markdown
    assert "excluded from the totals" in markdown


# --- collect_week_decisions (end to end via command_center.council) --------


def test_collect_week_decisions_prices_by_project() -> None:
    _decide("AML", "yes")
    lines = collect_week_decisions(
        "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z", valuations=VALUATIONS
    )
    assert len(lines) == 1
    assert lines[0].project == "AML"
    assert lines[0].outcome == "approved"
    assert lines[0].priced is True
    assert lines[0].pnl_usd == 400.0


def test_collect_week_decisions_excludes_out_of_range() -> None:
    _decide("AML", "yes")
    lines = collect_week_decisions(
        "2000-01-01T00:00:00Z", "2000-01-02T00:00:00Z", valuations=VALUATIONS
    )
    assert lines == []


def test_collect_week_decisions_redacts_sensitive_projects() -> None:
    # `council_service.create_motion` rejects a BANK/LEGAL `project_ref`
    # outright (defense at the write boundary), so a sensitive decision can
    # only exist via the low-level repository — mirrors the fixture in
    # `tests/test_council_db.py::test_list_motions_excludes_sensitive_projects_in_sql`.
    path = resolve_db_path(council_service.ROOT)
    motion = db.create_motion(
        path, title="secret", proposed_by="chair", quorum=1, project_ref="BANK"
    )
    vote = db.cast_vote(
        path, motion_id=motion["id"], voter_id="chair", role="chair", choice="yes"
    )
    db.record_decision(
        path,
        motion_id=motion["id"],
        expected_version=motion["version"],
        outcome="approved",
        tally={"yes": 1, "no": 0, "abstain": 0},
        roles=[{"voter_id": "chair", "voter_kind": "ai", "role": "chair", "choice": "yes"}],
        rationale="unanimous",
        quorum=1,
    )
    assert vote["choice"] == "yes"  # motion has a vote, so it would price otherwise

    lines = collect_week_decisions(
        "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z", valuations=VALUATIONS
    )
    assert lines == []
