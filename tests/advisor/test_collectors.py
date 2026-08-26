"""Unit tests for the built-in collectors and the collector interface.

Collectors are pure producers, so these tests hand each collector a fabricated
run history (by monkeypatching the ``db.list_runs`` facade the collectors read
through) and assert on the candidates — no sqlite, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from command_center.advisor.collectors.base import Collector
from command_center.advisor.collectors.external import (
    ExternalSignalSource,
    TrendCollector,
    UxCollector,
)
from command_center.advisor.collectors.feedback import FeedbackCollector
from command_center.advisor.collectors.optimization import OptimizationCollector
from command_center.advisor.types import Candidate, CollectorContext
from command_center.runtime import db


@pytest.fixture
def ctx() -> CollectorContext:
    return CollectorContext(root=Path("/nonexistent"), db_path=Path("/nonexistent/db"))


def _run(project: str, *, state: str, seconds: float | None, reason: str | None = None) -> dict:
    started = "2026-08-12T10:00:00"
    completed = None
    if seconds is not None:
        completed = (datetime.fromisoformat(started) + timedelta(seconds=seconds)).isoformat(
            timespec="seconds"
        )
    return {
        "project": project,
        "state": state,
        "started_at": started,
        "completed_at": completed,
        "failure_reason": reason,
    }


def _patch_runs(monkeypatch, runs: list[dict]) -> None:
    monkeypatch.setattr(db, "list_runs", lambda *a, **k: list(runs))


# --- interface ------------------------------------------------------------


def test_collector_is_abstract() -> None:
    with pytest.raises(TypeError):
        Collector()  # type: ignore[abstract]


def test_builtin_collectors_expose_name_and_kind() -> None:
    for cls in (OptimizationCollector, FeedbackCollector, TrendCollector, UxCollector):
        assert isinstance(cls.name, str) and cls.name
        assert isinstance(cls.kind, str) and cls.kind


# --- optimization ---------------------------------------------------------


def test_optimization_raises_candidate_for_cluster_of_slow_runs(monkeypatch, ctx) -> None:
    _patch_runs(
        monkeypatch,
        [
            _run("AICC", state="COMPLETED", seconds=600),
            _run("AICC", state="COMPLETED", seconds=700),
            _run("AICC", state="COMPLETED", seconds=30),
        ],
    )
    out = OptimizationCollector(slow_threshold_seconds=300, min_slow_runs=2).collect(ctx)
    assert len(out) == 1
    cand = out[0]
    assert cand.kind == "optimization"
    assert cand.project_ref == "AICC"
    assert cand.source == "optimization"
    assert 0.0 < cand.signals["frequency"] <= 1.0


def test_optimization_ignores_fast_history_and_missing_timestamps(monkeypatch, ctx) -> None:
    _patch_runs(
        monkeypatch,
        [
            _run("AICC", state="COMPLETED", seconds=10),
            _run("AICC", state="COMPLETED", seconds=None),  # still-running, no completion
            {"project": "AICC", "state": "COMPLETED", "started_at": None, "completed_at": None},
        ],
    )
    assert OptimizationCollector().collect(ctx) == []


def test_optimization_yields_nothing_on_empty_history(monkeypatch, ctx) -> None:
    _patch_runs(monkeypatch, [])
    assert OptimizationCollector().collect(ctx) == []


# --- feedback -------------------------------------------------------------


def test_feedback_raises_candidate_for_recurring_failures(monkeypatch, ctx) -> None:
    _patch_runs(
        monkeypatch,
        [
            _run("AIOS", state="FAILED", seconds=5, reason="timeout"),
            _run("AIOS", state="FAILED", seconds=5, reason="timeout"),
            _run("AIOS", state="COMPLETED", seconds=5),
        ],
    )
    out = FeedbackCollector(min_failures=2).collect(ctx)
    assert len(out) == 1
    cand = out[0]
    assert cand.kind == "feedback"
    assert cand.project_ref == "AIOS"
    assert "timeout" in cand.body


def test_feedback_below_threshold_is_silent(monkeypatch, ctx) -> None:
    _patch_runs(monkeypatch, [_run("AIOS", state="FAILED", seconds=5, reason="x")])
    assert FeedbackCollector(min_failures=2).collect(ctx) == []


# --- external (trend/competitor/ux) --------------------------------------


def test_external_collector_with_no_source_yields_zero(ctx) -> None:
    assert TrendCollector().collect(ctx) == []
    assert UxCollector().collect(ctx) == []


def test_external_collector_uses_injected_source(ctx) -> None:
    class _Source:
        def fetch(self, ctx: CollectorContext) -> list[Candidate]:
            return [Candidate(kind="trend", title="Adopt X", project_ref="AICC")]

    source = _Source()
    assert isinstance(source, ExternalSignalSource)
    out = TrendCollector(source=source).collect(ctx)
    assert len(out) == 1
    assert out[0].title == "Adopt X"
    assert out[0].source == "trend"


def test_external_collector_reads_source_from_context_options(ctx) -> None:
    class _Source:
        def fetch(self, ctx: CollectorContext) -> list[Candidate]:
            return [Candidate(kind="ux", title="Fix flow", project_ref="AICC")]

    ctx_with = CollectorContext(
        root=ctx.root, db_path=ctx.db_path, options={"ux_source": _Source()}
    )
    out = UxCollector().collect(ctx_with)
    assert len(out) == 1 and out[0].title == "Fix flow"
