"""Service-tier tests for Skill Acquisition
(``command_center.skills.service`` → ``runtime.db.skills``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db the service writes is
throwaway. The service migrates it lazily on first use.

The acquire path is exercised through an **injected recording executor** — no
real code execution, no network -- while the lifecycle transition and the
audit log around it are the real thing, exactly the marketplace-install
pattern this family is modelled on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from command_center.api import models
from command_center.api import skills_schemas as s
from command_center.skills import service
from command_center.skills.executor import SkillExecutionOutcome
from command_center.skills.finder import CandidateMetrics, CandidateProposal

_HASH_A = "a" * 64
_HASH_B = "b" * 64


@dataclass
class RecordingExecutor:
    name: str = "recording-executor"
    calls: list[str] = field(default_factory=list)
    outcome: SkillExecutionOutcome = field(
        default_factory=lambda: SkillExecutionOutcome(detail="recorded", metadata={"k": "v"})
    )

    def acquire(self, skill: models.SkillItem) -> SkillExecutionOutcome:
        self.calls.append(skill.id)
        return self.outcome


def _propose_and_approve_source(**overrides) -> models.SkillSource:
    payload = s.SkillSourceCreate(
        name="Registry", kind="mcp_registry", origin=f"origin:{id(overrides)}:{overrides.get('origin', '')}",
        proposed_by="alice",
    )
    for key, value in overrides.items():
        setattr(payload, key, value)
    source = service.propose_source(payload)
    return service.approve_source(source.id, actor="alice")


def _register(source_id: str, **overrides) -> models.SkillItem:
    payload = s.SkillItemCreate(
        name="Thing", kind="mcp_server", version="1.0.0", content_hash=_HASH_A,
        source_id=source_id,
    )
    for key, value in overrides.items():
        setattr(payload, key, value)
    return service.register_candidate(payload)


# --- sources: the human-gated allowlist -------------------------------------


def test_propose_source_starts_proposed() -> None:
    payload = s.SkillSourceCreate(
        name="a", kind="mcp_registry", origin="o1", proposed_by="alice",
    )
    source = service.propose_source(payload)
    assert source.status == "proposed" and source.approved_by == ""


def test_approve_source_is_the_human_gate_and_idempotent() -> None:
    payload = s.SkillSourceCreate(name="a", kind="repo_doc", origin="o2", proposed_by="alice")
    source = service.propose_source(payload)
    approved = service.approve_source(source.id, actor="bob")
    assert approved.status == "approved" and approved.approved_by == "bob"
    # Idempotent: approving again changes nothing (still bob, not overwritten).
    again = service.approve_source(source.id, actor="carol")
    assert again.approved_by == "bob"


def test_approve_missing_source_raises_not_found() -> None:
    with pytest.raises(service.SkillSourceNotFoundError):
        service.approve_source("nope", actor="alice")


def test_revoke_source_is_idempotent() -> None:
    source = _propose_and_approve_source(origin="o3")
    revoked = service.revoke_source(source.id, actor="alice", reason="deprecated")
    assert revoked.status == "revoked"
    again = service.revoke_source(source.id, actor="bob")
    assert again.status == "revoked"


# --- capability request + candidate discovery (allowlist enforced) ---------


def test_request_capability_requires_task_id_and_need() -> None:
    with pytest.raises(ValueError):
        service.request_capability(task_id="", task_class="x", need="need", requested_by="a")
    with pytest.raises(ValueError):
        service.request_capability(task_id="t1", task_class="x", need="", requested_by="a")


def test_find_candidates_only_hands_finder_approved_sources() -> None:
    approved = _propose_and_approve_source(origin="o4")
    proposed = service.propose_source(
        s.SkillSourceCreate(name="b", kind="mcp_registry", origin="o5", proposed_by="alice")
    )
    seen_sources: list[list[dict]] = []

    @dataclass
    class RecordingFinder:
        name: str = "recording-finder"

        def find(self, request, sources):
            seen_sources.append(sources)
            return []

    request = service.request_capability(
        task_id="t1", task_class="pdf", need="extract text from pdf", requested_by="alice",
    )
    service.find_candidates(request, finder=RecordingFinder())
    assert len(seen_sources) == 1
    ids = {row["id"] for row in seen_sources[0]}
    assert approved.id in ids
    assert proposed.id not in ids


def test_find_candidates_defaults_to_null_finder_with_no_network() -> None:
    request = service.request_capability(
        task_id="t1", task_class="pdf", need="extract text", requested_by="alice",
    )
    assert service.find_candidates(request) == []


# --- select_and_register: measurable selection (acceptance criterion 2) ----


def test_select_and_register_refuses_when_no_candidate_is_scored() -> None:
    request = service.request_capability(
        task_id="t1", task_class="pdf", need="extract", requested_by="alice",
    )
    source = _propose_and_approve_source(origin="o6")
    unscored = CandidateProposal(
        name="x", kind="mcp_server", version="1.0.0", content_hash=_HASH_A, source_id=source.id,
    )
    winner, rationale = service.select_and_register(request, [unscored])
    assert winner is None
    assert rationale["method"] == "measurable-history-required"


def test_select_and_register_persists_winner_with_rationale() -> None:
    request = service.request_capability(
        task_id="t1", task_class="pdf", need="extract", requested_by="alice",
    )
    source = _propose_and_approve_source(origin="o7")
    scored = CandidateProposal(
        name="best", kind="mcp_server", version="1.0.0", content_hash=_HASH_A, source_id=source.id,
        metrics=CandidateMetrics(success_rate=0.9, avg_cost=1.0, avg_latency_seconds=1.0),
    )
    winner, rationale = service.select_and_register(request, [scored])
    assert winner is not None
    assert winner.status == "candidate"
    assert winner.task_class == "pdf"
    assert winner.selection_rationale["method"] == "weighted-historical-score"
    persisted = service.get_item(winner.id)
    assert persisted.selection_rationale == rationale


# --- register_candidate: pinning + allowlist gate ---------------------------


def test_register_candidate_rejects_unapproved_source() -> None:
    proposed = service.propose_source(
        s.SkillSourceCreate(name="a", kind="mcp_registry", origin="o8", proposed_by="alice")
    )
    with pytest.raises(ValueError, match="not an approved source"):
        _register(proposed.id)


def test_register_candidate_rejects_missing_pin() -> None:
    source = _propose_and_approve_source(origin="o9")
    with pytest.raises(ValueError):
        _register(source.id, content_hash="not-a-hash")


def test_register_creates_candidate_from_approved_source() -> None:
    source = _propose_and_approve_source(origin="o10")
    item = _register(source.id, name="Widget", provenance="channel:stable")
    assert item.id and item.status == "candidate"
    assert item.provenance == "channel:stable"
    assert service.get_item(item.id).name == "Widget"


def test_list_items_filters_and_pages() -> None:
    source = _propose_and_approve_source(origin="o11")
    _register(source.id, name="a", content_hash=_HASH_A)
    _register(source.id, name="b", content_hash=_HASH_B)
    page = service.list_items()
    assert page.limit == 100 and len(page.items) == 2


# --- acquire lifecycle + audit log (acceptance criteria 3 and 4) ----------


def test_acquire_transitions_and_logs_who_when_what() -> None:
    source = _propose_and_approve_source(origin="o12")
    item = _register(source.id, version="2.3.4", provenance="url:https://example.test/x")
    executor = RecordingExecutor()

    acquired = service.acquire_skill(item.id, actor="alice", executor=executor)

    assert acquired.status == "acquired"
    assert executor.calls == [item.id]  # the isolation seam was actually used

    log = service.get_acquisition_log(item.id)
    assert len(log.entries) == 1
    entry = log.entries[0]
    assert entry.actor == "alice"
    assert entry.action == "acquire"
    assert entry.version == "2.3.4"
    assert entry.content_hash == _HASH_A
    assert entry.executor == "recording-executor"
    assert entry.detail == "recorded"
    assert entry.metadata == {"k": "v"}


def test_acquire_defaults_to_safe_null_executor() -> None:
    """With no executor injected, the default performs no network access but
    the lifecycle + log are still real."""
    source = _propose_and_approve_source(origin="o13")
    item = _register(source.id, version="0.1.0")
    acquired = service.acquire_skill(item.id, actor="bob")
    assert acquired.status == "acquired"
    entry = service.get_acquisition_log(item.id).entries[0]
    assert entry.executor == "null-skill-executor"
    assert entry.metadata["network"] == "denied"
    assert entry.metadata["secrets"] == "denied"
    assert entry.metadata["push"] == "denied"


def test_acquire_is_idempotent_no_duplicate_log() -> None:
    source = _propose_and_approve_source(origin="o14")
    item = _register(source.id)
    executor = RecordingExecutor()

    first = service.acquire_skill(item.id, actor="alice", executor=executor)
    second = service.acquire_skill(item.id, actor="alice", executor=executor)

    assert first.status == second.status == "acquired"
    assert executor.calls == [item.id]
    assert len(service.get_acquisition_log(item.id).entries) == 1


def test_acquire_missing_item_raises_not_found() -> None:
    with pytest.raises(service.SkillNotFoundError):
        service.acquire_skill("nope", actor="alice")


def test_failed_executor_leaves_item_candidate_and_unlogged() -> None:
    source = _propose_and_approve_source(origin="o15")
    item = _register(source.id)

    @dataclass
    class BoomExecutor:
        name: str = "boom"

        def acquire(self, skill: models.SkillItem) -> SkillExecutionOutcome:
            raise RuntimeError("materialisation failed")

    with pytest.raises(RuntimeError):
        service.acquire_skill(item.id, actor="alice", executor=BoomExecutor())

    assert service.get_item(item.id).status == "candidate"
    assert service.get_acquisition_log(item.id).entries == []


def test_reject_candidate_is_idempotent_and_logged() -> None:
    source = _propose_and_approve_source(origin="o16")
    item = _register(source.id)
    rejected = service.reject_candidate(item.id, actor="alice", reason="lost selection")
    assert rejected.status == "rejected"
    again = service.reject_candidate(item.id, actor="bob")
    assert again.status == "rejected"
    assert len(service.get_acquisition_log(item.id).entries) == 1


def test_revoke_skill_is_idempotent_and_logged_and_only_from_acquired() -> None:
    source = _propose_and_approve_source(origin="o17")
    item = _register(source.id)
    with pytest.raises(Exception):
        service.revoke_skill(item.id, actor="alice")  # candidate, not acquired
    service.acquire_skill(item.id, actor="alice")
    revoked = service.revoke_skill(item.id, actor="alice", reason="no improvement")
    assert revoked.status == "revoked"
    again = service.revoke_skill(item.id, actor="bob")
    assert again.status == "revoked"


def test_get_acquisition_log_missing_item_returns_none() -> None:
    assert service.get_acquisition_log("nope") is None


# --- effect measurement + retirement (acceptance criterion 5) --------------


def _acquired_item(origin: str) -> models.SkillItem:
    source = _propose_and_approve_source(origin=origin)
    item = _register(source.id)
    return service.acquire_skill(item.id, actor="alice")


def test_evaluate_effect_is_none_with_too_few_samples() -> None:
    item = _acquired_item("o18")
    service.record_outcome(item.id, task_id="t1", phase="baseline", cost=1.0, accepted=True, first_pass=True)
    service.record_outcome(item.id, task_id="t2", phase="with_skill", cost=0.5, accepted=True, first_pass=True)
    report = service.evaluate_effect(item.id, min_samples=5)
    assert report.improved is None


def test_evaluate_effect_true_when_with_skill_strictly_better_and_never_worse() -> None:
    item = _acquired_item("o19")
    for i in range(5):
        service.record_outcome(
            item.id, task_id=f"base-{i}", phase="baseline", cost=4.0, accepted=True, first_pass=False,
        )
    for i in range(5):
        service.record_outcome(
            item.id, task_id=f"with-{i}", phase="with_skill", cost=1.0, accepted=True, first_pass=True,
        )
    report = service.evaluate_effect(item.id, min_samples=5)
    assert report.baseline_cost_per_accepted == 4.0
    assert report.with_skill_cost_per_accepted == 1.0
    assert report.baseline_first_pass_rate == 0.0
    assert report.with_skill_first_pass_rate == 1.0
    assert report.improved is True


def test_evaluate_effect_false_when_no_better_than_baseline() -> None:
    item = _acquired_item("o20")
    for i in range(5):
        service.record_outcome(
            item.id, task_id=f"base-{i}", phase="baseline", cost=1.0, accepted=True, first_pass=True,
        )
    for i in range(5):
        service.record_outcome(
            item.id, task_id=f"with-{i}", phase="with_skill", cost=1.0, accepted=True, first_pass=True,
        )
    report = service.evaluate_effect(item.id, min_samples=5)
    assert report.improved is False  # identical, not strictly better -> no improvement


def test_sweep_retires_only_proven_underperformers() -> None:
    improved_item = _acquired_item("o21")
    for i in range(5):
        service.record_outcome(
            improved_item.id, task_id=f"b{i}", phase="baseline", cost=4.0, accepted=True, first_pass=False,
        )
        service.record_outcome(
            improved_item.id, task_id=f"w{i}", phase="with_skill", cost=1.0, accepted=True, first_pass=True,
        )

    underperformer = _acquired_item("o22")
    for i in range(5):
        service.record_outcome(
            underperformer.id, task_id=f"b{i}", phase="baseline", cost=1.0, accepted=True, first_pass=True,
        )
        service.record_outcome(
            underperformer.id, task_id=f"w{i}", phase="with_skill", cost=2.0, accepted=True, first_pass=False,
        )

    insufficient_data = _acquired_item("o23")
    service.record_outcome(
        insufficient_data.id, task_id="only", phase="with_skill", cost=1.0, accepted=True, first_pass=True,
    )

    retired = service.sweep_retire_underperforming(min_samples=5)

    assert retired == [underperformer.id]
    assert service.get_item(improved_item.id).status == "acquired"
    assert service.get_item(underperformer.id).status == "revoked"
    assert service.get_item(insufficient_data.id).status == "acquired"

    log = service.get_acquisition_log(underperformer.id).entries[0]
    assert log.action == "revoke"
    assert log.actor == "effect-sweep"
    assert "no measurable improvement" in log.detail
