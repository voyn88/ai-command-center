"""Tests for :class:`command_center.advisor.service.AdvisorService`.

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox, so
the real Wave-1 persist/promote seams write a throwaway runtime db and tasks.json.
A fresh :class:`EventBus` is installed per test so published events are observable.
A local fake collector supplies deterministic candidates — the service, not the
collectors, is under test here.
"""

from __future__ import annotations

import pytest

from command_center.advisor.collectors.base import Collector
from command_center.advisor.config import AdvisorConfig, AutoRule
from command_center.advisor.registry import CollectorRegistry
from command_center.advisor.service import AdvisorService
from command_center.advisor.types import Candidate, CollectorContext
from command_center.events import (
    Event,
    ProposalCreated,
    ProposalPromotedToTask,
    default_bus,
)


@pytest.fixture
def events() -> list[Event]:
    captured: list[Event] = []
    bus = default_bus()
    bus.clear()
    unsubscribe = bus.subscribe(Event, captured.append)
    try:
        yield captured
    finally:
        unsubscribe()
        bus.clear()


class _FakeCollector(Collector):
    name = "fake"
    kind = "optimization"

    def __init__(self, candidates: list[Candidate]) -> None:
        self._candidates = candidates

    def collect(self, ctx: CollectorContext) -> list[Candidate]:
        return list(self._candidates)


def _registry(candidates: list[Candidate]) -> CollectorRegistry:
    reg = CollectorRegistry()
    reg.register("fake", lambda: _FakeCollector(candidates))
    return reg


def _cand(title: str, *, kind: str = "optimization", project: str = "AICC", **signals) -> Candidate:
    return Candidate(kind=kind, title=title, project_ref=project, signals=signals)


# --- persist + event ------------------------------------------------------


def test_run_persists_proposal_and_emits_created_event(events) -> None:
    service = AdvisorService(registry=_registry([_cand("Speed up CI")]))
    summary = service.run()
    assert summary.created == 1
    assert summary.by_kind == {"optimization": 1}
    created = [e for e in events if isinstance(e, ProposalCreated)]
    assert len(created) == 1
    assert created[0].kind == "optimization"
    # The persisted proposal is a real, readable row via the Wave-1 read path.
    from command_center.api import wave1_service

    listed = wave1_service.list_proposals()
    assert [p.title for p in listed.proposals] == ["Speed up CI"]
    assert listed.proposals[0].status == "new"  # conservative draft default


# --- dedup ----------------------------------------------------------------


def test_dedup_within_a_single_pass() -> None:
    service = AdvisorService(registry=_registry([_cand("Same"), _cand("  same  ")]))
    summary = service.run()
    assert summary.created == 1
    assert summary.deduped == 1


def test_dedup_against_existing_open_proposals() -> None:
    reg = _registry([_cand("Recurring signal")])
    first = AdvisorService(registry=reg).run()
    assert first.created == 1
    # A second pass over the same unchanged signal must not raise a duplicate.
    second = AdvisorService(registry=reg).run()
    assert second.created == 0
    assert second.deduped == 1


# --- auto-rules -----------------------------------------------------------


def test_default_config_drafts_never_promotes(events) -> None:
    service = AdvisorService(
        registry=_registry([_cand("Big win", impact=1.0, frequency=1.0, effort=0.0, risk=0.0)])
    )
    summary = service.run()
    assert summary.created == 1
    assert summary.promoted == 0
    assert summary.proposals[0].action == "draft"
    assert not [e for e in events if isinstance(e, ProposalPromotedToTask)]


def test_auto_promote_rule_creates_task_and_emits_event(events) -> None:
    config = AdvisorConfig(auto_rules=(AutoRule(min_priority=0.5),))
    service = AdvisorService(
        registry=_registry([_cand("Promote me", impact=1.0, frequency=1.0, effort=0.0, risk=0.0)]),
        config=config,
    )
    summary = service.run()
    assert summary.created == 1
    assert summary.promoted == 1
    outcome = summary.proposals[0]
    assert outcome.action == "promote"
    assert outcome.promoted_task_id
    promoted = [e for e in events if isinstance(e, ProposalPromotedToTask)]
    assert len(promoted) == 1
    assert promoted[0].task_id == outcome.promoted_task_id


def test_low_priority_candidate_stays_draft_under_threshold_rule() -> None:
    config = AdvisorConfig(auto_rules=(AutoRule(min_priority=0.9),))
    service = AdvisorService(
        registry=_registry([_cand("Meh", impact=0.2, frequency=0.2, effort=1.0, risk=0.8)]),
        config=config,
    )
    summary = service.run()
    assert summary.created == 1
    assert summary.promoted == 0


# --- privacy --------------------------------------------------------------


def test_sensitive_project_candidates_are_dropped_before_write(events) -> None:
    service = AdvisorService(
        registry=_registry([_cand("Leak", project="BANK"), _cand("Keep", project="AICC")])
    )
    summary = service.run()
    assert summary.created == 1
    assert summary.skipped_sensitive == 1
    created = [e for e in events if isinstance(e, ProposalCreated)]
    assert [e.project_ref for e in created] == ["AICC"]


# --- collector subset -----------------------------------------------------


def test_project_filter_restricts_persistence() -> None:
    reg = _registry([_cand("A", project="AICC"), _cand("B", project="AIOS")])
    summary = AdvisorService(registry=reg).run(project="AIOS")
    assert summary.created == 1
    assert summary.proposals[0].project_ref == "AIOS"
