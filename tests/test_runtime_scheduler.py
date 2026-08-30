"""Deterministic scheduling & supervision-decision layer.

Covers the increment's required behaviours end to end: registration,
capability-aware assignment, priority ordering, concurrency/capacity limits,
workspace-exclusivity duplicate prevention, recoverable-vs-terminal failure
classification, retry attempt numbering, backoff, cancellation-as-terminal,
SLA/timing signals, restart reconciliation feeding the load snapshot, and the
fail-safe on malformed state.

Every test pins an explicit `now` so decisions are fully deterministic — no
wall clock is read inside `scheduler.plan`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from command_center import executors
from command_center.runtime import db, scheduler, supervisor

NOW = "2026-07-23T12:00:00"


def _iso(base: str, *, seconds: float) -> str:
    return (datetime.fromisoformat(base) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _registry(max_concurrency: int = 2) -> scheduler.AgentRegistry:
    return scheduler.default_registry(max_concurrency=max_concurrency)


def _item(task_id: str, workspace: str, **kw) -> scheduler.WorkItem:
    return scheduler.WorkItem(task_id=task_id, workspace=workspace, **kw)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_default_registry_only_registers_available_executors():
    """The invariant is membership-by-existence, not membership-by-availability.

    `default_registry` registers every executor that has a *provider* behind it
    (`availability_check is not None`), carrying the live availability probe on
    the `AgentSpec.available` flag rather than letting it decide membership.
    That is the whole point: `executor.available` shells out to the provider CLI
    and can report False for transient reasons (a slow probe, a restarting
    daemon), so omitting such an executor would turn a self-healing
    `agent_unavailable` into a structural `no_capable_agent`. An executor with no
    provider at all is a genuine structural absence and stays omitted."""
    reg = _registry()
    registered = {a.agent_id for a in reg.all()}
    # Membership == "has a provider (availability_check is not None)", independent
    # of whether the CLI happens to be installed on this host.
    with_provider = {
        eid for eid, exe in executors.EXECUTORS.items() if exe.availability_check is not None
    }
    assert registered == with_provider
    # Live availability is a subset of membership, never the other way around.
    available = {e.id for e in executors.EXECUTORS.values() if e.available}
    assert available <= registered
    # Claude is always installed in this suite, and a permanently-unavailable
    # executor (no provider) must never be registered.
    assert reg.get("claude_code") is not None
    assert reg.get("chatgpt") is None


def test_default_registry_does_not_offer_implementation_work_to_ollama():
    ollama = _registry().get("ollama")
    assert ollama is not None
    assert ollama.can_run(scheduler.capabilities_for_task_type("architecture_review"))
    assert not ollama.can_run(scheduler.capabilities_for_task_type("implementation"))


def test_register_rejects_unknown_executor():
    reg = scheduler.AgentRegistry()
    try:
        reg.register(
            scheduler.AgentSpec(
                agent_id="ghost", executor_id="does_not_exist", capabilities=frozenset(), max_concurrency=1
            )
        )
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_register_rejects_negative_concurrency():
    reg = scheduler.AgentRegistry()
    try:
        reg.register(
            scheduler.AgentSpec(
                agent_id="c", executor_id="claude_code", capabilities=frozenset({scheduler.CAP_ANY}), max_concurrency=-1
            )
        )
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_registry_iteration_is_order_independent():
    a = scheduler.AgentSpec("a", "claude_code", frozenset({scheduler.CAP_ANY}), 1, weight=5)
    b = scheduler.AgentSpec("b", "claude_code", frozenset({scheduler.CAP_ANY}), 1, weight=5)
    assert [x.agent_id for x in scheduler.AgentRegistry([a, b]).all()] == \
        [x.agent_id for x in scheduler.AgentRegistry([b, a]).all()] == ["a", "b"]


# --------------------------------------------------------------------------
# Success / assignment
# --------------------------------------------------------------------------


def test_single_ready_item_is_assigned_as_attempt_one():
    reg = _registry()
    result = scheduler.plan([_item("t1", "/repo/a")], registry=reg, now=NOW)
    assert len(result.assignments()) == 1
    d = result.assignments()[0]
    assert d.action == scheduler.ACTION_ASSIGN
    assert d.reason_code == scheduler.REASON_ASSIGNED
    assert d.agent_id == "claude_code"
    assert d.executor_id == "claude_code"
    assert d.attempt == 1
    assert d.decided_at == NOW


def test_decision_is_fully_serializable_audit_record():
    reg = _registry()
    result = scheduler.plan([_item("t1", "/repo/a", enqueued_at=_iso(NOW, seconds=-90), sla_seconds=60)], registry=reg, now=NOW)
    record = result.audit_records()[0]
    assert record["task_id"] == "t1"
    assert record["action"] == "ASSIGN"
    assert record["queued_seconds"] == 90.0
    assert record["sla_breached"] is True
    # Round-trips through the primitive types a JSON audit log needs.
    import json

    assert json.loads(json.dumps(record))["reason_code"] == "assigned"


# --------------------------------------------------------------------------
# Capability-aware assignment
# --------------------------------------------------------------------------


def test_capability_restricted_agent_does_not_match_implementation_work():
    reg = scheduler.AgentRegistry(
        [
            scheduler.AgentSpec(
                agent_id="reviewer",
                executor_id="claude_code",
                capabilities=scheduler.capabilities_for_task_type("review"),
                max_concurrency=2,
            )
        ]
    )
    impl_caps = scheduler.capabilities_for_task_type("implementation")
    result = scheduler.plan([_item("t1", "/repo/a", required_capabilities=impl_caps)], registry=reg, now=NOW)
    d = result.decisions[0]
    assert d.action == scheduler.ACTION_BLOCKED
    assert d.reason_code == scheduler.REASON_NO_CAPABLE_AGENT


def test_capability_restricted_agent_matches_its_own_task_type():
    reg = scheduler.AgentRegistry(
        [
            scheduler.AgentSpec(
                agent_id="reviewer",
                executor_id="claude_code",
                capabilities=scheduler.capabilities_for_task_type("review"),
                max_concurrency=2,
            )
        ]
    )
    review_caps = scheduler.capabilities_for_task_type("review")
    result = scheduler.plan([_item("t1", "/repo/a", required_capabilities=review_caps)], registry=reg, now=NOW)
    assert result.assignments()[0].agent_id == "reviewer"


def test_preferred_agent_that_cannot_satisfy_capabilities_is_blocked():
    reg = scheduler.AgentRegistry(
        [
            scheduler.AgentSpec("reviewer", "claude_code", scheduler.capabilities_for_task_type("review"), 2),
            scheduler.AgentSpec("dev", "claude_code", frozenset({scheduler.CAP_ANY}), 2),
        ]
    )
    impl_caps = scheduler.capabilities_for_task_type("implementation")
    result = scheduler.plan(
        [_item("t1", "/repo/a", required_capabilities=impl_caps, preferred_agent="reviewer")], registry=reg, now=NOW
    )
    assert result.decisions[0].action == scheduler.ACTION_BLOCKED
    assert result.decisions[0].reason_code == scheduler.REASON_NO_CAPABLE_AGENT


def test_agent_unavailable_defers_not_blocks():
    reg = scheduler.AgentRegistry(
        [scheduler.AgentSpec("c", "claude_code", frozenset({scheduler.CAP_ANY}), 2, available=False)]
    )
    result = scheduler.plan([_item("t1", "/repo/a")], registry=reg, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_DEFER
    assert result.decisions[0].reason_code == scheduler.REASON_AGENT_UNAVAILABLE


# --------------------------------------------------------------------------
# Priority scheduling
# --------------------------------------------------------------------------


def test_higher_priority_is_scheduled_first_under_scarce_capacity():
    reg = _registry(max_concurrency=1)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=1)
    items = [
        _item("low", "/repo/low", priority="Low"),
        _item("crit", "/repo/crit", priority="Critical"),
        _item("med", "/repo/med", priority="Medium"),
    ]
    result = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assigned = result.assignments()
    assert len(assigned) == 1
    assert assigned[0].task_id == "crit"
    # The other two are deferred for global capacity, in priority order.
    deferred = result.deferrals()
    assert [d.task_id for d in deferred] == ["med", "low"]
    assert all(d.reason_code == scheduler.REASON_GLOBAL_AT_CAPACITY for d in deferred)


def test_sla_breach_jumps_ahead_within_same_priority():
    reg = _registry(max_concurrency=1)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=1)
    items = [
        _item("fresh", "/repo/fresh", priority="Medium", enqueued_at=_iso(NOW, seconds=-10), sla_seconds=60),
        _item("breached", "/repo/breached", priority="Medium", enqueued_at=_iso(NOW, seconds=-120), sla_seconds=60),
    ]
    result = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assert result.assignments()[0].task_id == "breached"


# --------------------------------------------------------------------------
# Concurrency & capacity limits
# --------------------------------------------------------------------------


def test_global_capacity_limit_defers_excess():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=2)
    items = [_item(f"t{i}", f"/repo/{i}") for i in range(4)]
    result = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assert len(result.assignments()) == 2
    assert len(result.deferrals()) == 2
    assert all(d.reason_code == scheduler.REASON_GLOBAL_AT_CAPACITY for d in result.deferrals())


def test_existing_running_load_counts_against_global_capacity():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=2)
    load = scheduler.LoadSnapshot(global_running=2, busy_workspaces=frozenset({"/repo/busy"}))
    result = scheduler.plan([_item("t1", "/repo/a")], registry=reg, config=cfg, load=load, now=NOW)
    assert result.deferrals()[0].reason_code == scheduler.REASON_GLOBAL_AT_CAPACITY


def test_per_agent_capacity_limit_defers():
    reg = scheduler.AgentRegistry([scheduler.AgentSpec("c", "claude_code", frozenset({scheduler.CAP_ANY}), 1)])
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    items = [_item("t1", "/repo/a"), _item("t2", "/repo/b")]
    result = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assert len(result.assignments()) == 1
    assert result.deferrals()[0].reason_code == scheduler.REASON_AGENT_AT_CAPACITY


def test_workload_distribution_prefers_agent_with_most_spare_capacity():
    reg = scheduler.AgentRegistry(
        [
            scheduler.AgentSpec("busy", "claude_code", frozenset({scheduler.CAP_ANY}), 3),
            scheduler.AgentSpec("idle", "claude_code", frozenset({scheduler.CAP_ANY}), 3),
        ]
    )
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    load = scheduler.LoadSnapshot(running_by_agent={"busy": 2})
    result = scheduler.plan([_item("t1", "/repo/a")], registry=reg, config=cfg, load=load, now=NOW)
    assert result.assignments()[0].agent_id == "idle"


def test_workload_distribution_respects_authorized_agent_set():
    reg = scheduler.AgentRegistry(
        [
            scheduler.AgentSpec("claude_code", "claude_code", frozenset({scheduler.CAP_ANY}), 2),
            scheduler.AgentSpec("codex", "codex", frozenset({scheduler.CAP_ANY}), 2),
        ]
    )
    load = scheduler.LoadSnapshot(running_by_agent={"claude_code": 1}, global_running=1)
    result = scheduler.plan(
        [
            _item(
                "t1",
                "/repo/a",
                allowed_agents=frozenset({"claude_code", "codex"}),
            )
        ],
        registry=reg,
        config=scheduler.SchedulerConfig(max_global_concurrency=10),
        load=load,
        now=NOW,
    )
    assert result.assignments()[0].agent_id == "codex"


def test_workload_distribution_never_escapes_authorized_agent_set():
    reg = scheduler.AgentRegistry(
        [
            scheduler.AgentSpec("claude_code", "claude_code", frozenset({scheduler.CAP_ANY}), 2),
            scheduler.AgentSpec("codex", "codex", frozenset({scheduler.CAP_ANY}), 2),
        ]
    )
    result = scheduler.plan(
        [_item("t1", "/repo/a", allowed_agents=frozenset({"claude_code"}))],
        registry=reg,
        now=NOW,
    )
    assert result.assignments()[0].agent_id == "claude_code"


def test_equal_capacity_and_weight_choose_lexicographically_first_agent():
    reg = scheduler.AgentRegistry(
        [
            scheduler.AgentSpec("b", "claude_code", frozenset({scheduler.CAP_ANY}), 3, weight=2),
            scheduler.AgentSpec("a", "claude_code", frozenset({scheduler.CAP_ANY}), 3, weight=2),
        ]
    )
    result = scheduler.plan(
        [_item("t1", "/repo/a")],
        registry=reg,
        config=scheduler.SchedulerConfig(max_global_concurrency=10),
        now=NOW,
    )
    assert result.assignments()[0].agent_id == "a"


# --------------------------------------------------------------------------
# Duplicate prevention (workspace exclusivity)
# --------------------------------------------------------------------------


def test_workspace_busy_in_snapshot_defers():
    reg = _registry(max_concurrency=10)
    load = scheduler.LoadSnapshot(global_running=1, busy_workspaces=frozenset({"/repo/a"}))
    result = scheduler.plan([_item("t1", "/repo/a")], registry=reg, load=load, now=NOW)
    assert result.deferrals()[0].reason_code == scheduler.REASON_WORKSPACE_BUSY


def test_two_items_same_workspace_only_one_assigned_in_one_plan():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    items = [_item("t1", "/repo/a", priority="High"), _item("t2", "/repo/a", priority="Low")]
    result = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assert len(result.assignments()) == 1
    assert result.assignments()[0].task_id == "t1"
    assert result.deferrals()[0].task_id == "t2"
    assert result.deferrals()[0].reason_code == scheduler.REASON_WORKSPACE_BUSY


def test_workspace_paths_are_normalized_before_comparison():
    reg = _registry(max_concurrency=10)
    load = scheduler.LoadSnapshot(global_running=1, busy_workspaces=frozenset({"/repo/a"}))
    result = scheduler.plan([_item("t1", "/repo/./x/../a")], registry=reg, load=load, now=NOW)
    assert result.deferrals()[0].reason_code == scheduler.REASON_WORKSPACE_BUSY


def test_plan_never_assigns_a_task_twice():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    items = [_item(f"t{i}", f"/repo/{i}") for i in range(5)]
    result = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assigned_ids = [d.task_id for d in result.assignments()]
    assert len(assigned_ids) == len(set(assigned_ids))


# --------------------------------------------------------------------------
# Failure classification: recoverable vs terminal
# --------------------------------------------------------------------------


def test_classify_timeout_is_recoverable():
    assert scheduler.classify_failure(state="FAILED", failure_reason="timeout") == scheduler.RECOVERABLE


def test_classify_blocked_is_terminal():
    # Uses a self-refusal rather than a permission denial: the latter is now a
    # deliberate exception (environment policy changes between attempts, so a
    # repeat is not an identical attempt), covered by its own test below.
    assert scheduler.classify_failure(state="FAILED", failure_reason="blocked:final_response") == scheduler.TERMINAL


def test_classify_cancelled_is_terminal():
    assert scheduler.classify_failure(state="CANCELLED", failure_reason=None) == scheduler.TERMINAL


def test_classify_interrupted_is_recoverable():
    assert scheduler.classify_failure(state="INTERRUPTED", failure_reason=None) == scheduler.RECOVERABLE


def test_classify_incomplete_is_recoverable():
    assert scheduler.classify_failure(state="FAILED", failure_reason="incomplete:no_tree_change") == scheduler.RECOVERABLE


# --------------------------------------------------------------------------
# Retry, attempt numbering, backoff, terminal-no-retry
# --------------------------------------------------------------------------


def test_terminal_failure_is_blocked_never_retried():
    reg = _registry()
    item = _item("t1", "/repo/a", attempts_made=1, last_state="FAILED", last_failure_reason="blocked:policy")
    result = scheduler.plan([item], registry=reg, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_BLOCKED
    assert result.decisions[0].reason_code == scheduler.REASON_TERMINAL_FAILURE
    assert result.decisions[0].failure_classification == scheduler.TERMINAL


def test_cancelled_prior_attempt_is_blocked_terminal():
    reg = _registry()
    item = _item("t1", "/repo/a", attempts_made=1, last_state="CANCELLED")
    result = scheduler.plan([item], registry=reg, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_BLOCKED
    assert result.decisions[0].reason_code == scheduler.REASON_TERMINAL_FAILURE


def test_recoverable_failure_within_backoff_window_defers():
    reg = _registry()
    policy = scheduler.RetryPolicy(base_backoff_seconds=30, multiplier=2)
    # One attempt made, completed 10s ago; backoff after attempt 1 is 30s.
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state="FAILED", last_failure_reason="timeout",
        last_completed_at=_iso(NOW, seconds=-10),
    )
    result = scheduler.plan([item], registry=reg, policy=policy, now=NOW)
    d = result.decisions[0]
    assert d.action == scheduler.ACTION_DEFER
    assert d.reason_code == scheduler.REASON_BACKOFF
    assert d.failure_classification == scheduler.RECOVERABLE
    assert d.next_eligible_at == _iso(NOW, seconds=20)  # -10 + 30


def test_recoverable_failure_after_backoff_assigns_new_attempt_number():
    reg = _registry()
    policy = scheduler.RetryPolicy(base_backoff_seconds=30, multiplier=2)
    # Completed 40s ago; backoff 30s already elapsed -> eligible now.
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state="FAILED", last_failure_reason="timeout",
        last_completed_at=_iso(NOW, seconds=-40),
    )
    result = scheduler.plan([item], registry=reg, policy=policy, now=NOW)
    d = result.assignments()[0]
    assert d.action == scheduler.ACTION_ASSIGN
    assert d.attempt == 2  # explicit new attempt = attempts_made + 1


def test_exponential_backoff_grows_and_caps():
    policy = scheduler.RetryPolicy(base_backoff_seconds=30, multiplier=2, max_backoff_seconds=100)
    assert policy.backoff_seconds(1) == 30
    assert policy.backoff_seconds(2) == 60
    assert policy.backoff_seconds(3) == 100  # 120 capped at 100
    assert policy.backoff_seconds(0) == 0


def test_retry_budget_exhausted_is_blocked():
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=2)
    item = _item(
        "t1", "/repo/a", attempts_made=2, last_state="FAILED", last_failure_reason="timeout",
        last_completed_at=_iso(NOW, seconds=-10000),
    )
    result = scheduler.plan([item], registry=reg, policy=policy, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_BLOCKED
    assert result.decisions[0].reason_code == scheduler.REASON_RETRY_EXHAUSTED


def test_provider_unavailable_failure_skips_retry_budget_and_assigns():
    """A provider-unavailable failure (expired OAuth, exhausted quota, …) is
    not a signal about the task — the configured executor cannot run it right
    now. `task_sync` has already moved that executor to `failed_executors` and
    the launch layer's `select_available_executor` will pick the next one, so
    the prior attempt must NOT consume the retry budget: even at
    attempts_made >= max_attempts the scheduler falls through to assignment
    instead of blocking with `retry_exhausted`. Otherwise a `max_run_attempts=1`
    config would strand the task before failover ever happens."""
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=1)
    item = _item(
        "t1", "/repo/a", attempts_made=3, last_state="FAILED",
        last_failure_reason="session_expired",
        last_completed_at=_iso(NOW, seconds=-10000),
    )
    result = scheduler.plan([item], registry=reg, policy=policy, now=NOW)
    d = result.assignments()[0]
    assert d.action == scheduler.ACTION_ASSIGN


def test_provider_unavailable_failure_skips_backoff_and_assigns_immediately():
    """Provider failover also needs no backoff — a *different* executor is
    tried, not the same one, so the prior failure carries no timing signal
    about it. The task assigns immediately even within the backoff window."""
    reg = _registry()
    policy = scheduler.RetryPolicy(base_backoff_seconds=30, multiplier=2)
    # Completed 1s ago; backoff 30s would normally defer — but a provider
    # failure bypasses backoff and assigns now.
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state="FAILED",
        last_failure_reason="quota_limit", last_completed_at=_iso(NOW, seconds=-1),
    )
    result = scheduler.plan([item], registry=reg, policy=policy, now=NOW)
    d = result.assignments()[0]
    assert d.action == scheduler.ACTION_ASSIGN


def test_non_provider_recoverable_failure_still_respects_budget():
    """Sanity: a recoverable failure that is NOT provider-unavailable (e.g.
    `timeout`) must still hit the retry budget and block — the exemption is
    narrow to provider-unavailability reasons only."""
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=1)
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state="FAILED",
        last_failure_reason="timeout", last_completed_at=_iso(NOW, seconds=-10000),
    )
    result = scheduler.plan([item], registry=reg, policy=policy, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_BLOCKED
    assert result.decisions[0].reason_code == scheduler.REASON_RETRY_EXHAUSTED


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def test_unmet_dependency_defers():
    reg = _registry()
    result = scheduler.plan([_item("t1", "/repo/a", dependencies_met=False)], registry=reg, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_DEFER
    assert result.decisions[0].reason_code == scheduler.REASON_WAITING_DEPENDENCY


# --------------------------------------------------------------------------
# SLA / timing signals
# --------------------------------------------------------------------------


def test_sla_timing_signals_are_reported():
    reg = _registry()
    item = _item("t1", "/repo/a", enqueued_at=_iso(NOW, seconds=-30), sla_seconds=100)
    d = scheduler.plan([item], registry=reg, now=NOW).decisions[0]
    assert d.queued_seconds == 30.0
    assert d.sla_breached is False
    assert d.sla_remaining_seconds == 70.0


# --------------------------------------------------------------------------
# Malformed state fails safe
# --------------------------------------------------------------------------


def test_missing_task_id_is_blocked_not_assigned():
    reg = _registry()
    result = scheduler.plan([scheduler.WorkItem(task_id="", workspace="/repo/a")], registry=reg, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_BLOCKED
    assert result.decisions[0].reason_code == scheduler.REASON_MALFORMED


def test_missing_workspace_is_blocked_not_assigned():
    reg = _registry()
    result = scheduler.plan([scheduler.WorkItem(task_id="t1", workspace="")], registry=reg, now=NOW)
    assert result.decisions[0].action == scheduler.ACTION_BLOCKED
    assert result.decisions[0].reason_code == scheduler.REASON_MALFORMED


def test_malformed_enqueued_timestamp_does_not_crash():
    reg = _registry()
    item = _item("t1", "/repo/a", enqueued_at="not-a-timestamp", sla_seconds=60)
    d = scheduler.plan([item], registry=reg, now=NOW).decisions[0]
    # Unparseable timestamp -> timing signals are simply absent, item still assigns.
    assert d.action == scheduler.ACTION_ASSIGN
    assert d.queued_seconds is None


@pytest.mark.parametrize("last_state", [None, "", "CORRUPT", "RUNNING"])
def test_completed_attempt_count_with_invalid_prior_state_is_blocked(last_state):
    reg = _registry(max_concurrency=10)
    item = _item("t1", "/repo/a", attempts_made=999, last_state=last_state)
    decision = scheduler.plan([item], registry=reg, now=NOW).decisions[0]
    assert decision.action == scheduler.ACTION_BLOCKED
    assert decision.reason_code == scheduler.REASON_INVALID_ATTEMPT_STATE


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_plan_is_deterministic_for_identical_inputs():
    reg = _registry(max_concurrency=1)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=2)
    items = [_item(f"t{i}", f"/repo/{i}", priority=p) for i, p in enumerate(["Low", "Critical", "High", "Medium"])]
    a = scheduler.plan(items, registry=reg, config=cfg, now=NOW).audit_records()
    b = scheduler.plan(list(reversed(items)), registry=reg, config=cfg, now=NOW).audit_records()
    assert a == b


# --------------------------------------------------------------------------
# Restart reconciliation feeds the load snapshot (integration with db)
# --------------------------------------------------------------------------


def _make_running_row(db_path, *, repository_path, provider_id="claude_code"):
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path=repository_path)
    run = db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path=repository_path, prompt="p", is_resume=False, provider_id=provider_id,
        finalization_owner_token="dead-owner", finalization_owner_pid=999_999_999,
        finalization_owner_identity="dead-start|dead-command",
    )
    run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(
        db_path, run["id"], expected_version=run["version"], new_state="RUNNING", fields={"started_at": NOW}
    )
    return run


def test_build_load_snapshot_reflects_active_runs(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    _make_running_row(db_path, repository_path="/repo/a")
    _make_running_row(db_path, repository_path="/repo/b")

    snap = scheduler.build_load_snapshot(db_path)
    assert snap.global_running == 2
    assert snap.busy_workspaces == frozenset({"/repo/a", "/repo/b"})
    assert len(snap.active_task_ids) == 2
    assert snap.running_by_agent == {"claude_code": 2}


def test_build_load_snapshot_attributes_each_run_to_its_provider(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    _make_running_row(db_path, repository_path="/repo/a", provider_id="claude_code")
    _make_running_row(db_path, repository_path="/repo/b", provider_id="codex")

    snap = scheduler.build_load_snapshot(db_path)

    assert snap.running_by_agent == {"claude_code": 1, "codex": 1}


def test_active_task_cannot_be_assigned_again_in_a_different_workspace(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    active = _make_running_row(db_path, repository_path="/repo/a")
    snap = scheduler.build_load_snapshot(db_path)

    plan = scheduler.plan(
        [_item(active["task_id"], "/repo/b")],
        registry=_registry(max_concurrency=10),
        config=scheduler.SchedulerConfig(max_global_concurrency=10),
        load=snap,
        now=NOW,
    )

    assert plan.assignments() == []
    assert plan.deferrals()[0].reason_code == scheduler.REASON_DUPLICATE_TASK


def test_snapshot_after_reconciliation_frees_workspace_for_scheduling(tmp_path):
    """A run left RUNNING by a crashed predecessor is reconciled to a terminal
    state (INTERRUPTED here — its pid is long gone), which drops it out of the
    active-state set the snapshot reads, so its workspace becomes schedulable
    again. This is the restart-recovery path feeding the scheduler."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_running_row(db_path, repository_path="/repo/a")
    # No pid recorded -> reconcile classifies as INTERRUPTED (see supervisor).
    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()
    assert outcomes[0]["classification"] == "INTERRUPTED"
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"

    snap = scheduler.build_load_snapshot(db_path)
    assert snap.global_running == 0
    assert snap.busy_workspaces == frozenset()
    assert snap.active_task_ids == frozenset()

    reg = _registry()
    result = scheduler.plan([_item("t-new", "/repo/a")], registry=reg, load=snap, now=NOW)
    assert result.assignments()[0].task_id == "t-new"


# ==========================================================================
# Founder Gate remediation — F1 (state-driven retry gating), F2 (task_id
# dedup), F3 (snapshot clamp), F4 (missing retry timestamp), F6 (priority
# normalization surfaced). Regression coverage for the exact defects the
# independent review reproduced.
# ==========================================================================

# --- F1: retry gating is STATE-driven, not failure_reason-driven ----------


@pytest.mark.parametrize("state", ["FAILED", "INTERRUPTED", "UNKNOWN"])
def test_f1_non_success_terminal_with_no_failure_reason_below_budget_defers_backoff(state):
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=3, base_backoff_seconds=30)
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state=state, last_failure_reason=None,
        last_completed_at=NOW,  # just completed -> inside the 30s backoff window
    )
    d = scheduler.plan([item], registry=reg, policy=policy, now=NOW).decisions[0]
    assert d.action == scheduler.ACTION_DEFER
    assert d.reason_code == scheduler.REASON_BACKOFF
    assert d.failure_classification == scheduler.RECOVERABLE
    assert d.next_eligible_at == _iso(NOW, seconds=30)


@pytest.mark.parametrize("state", ["FAILED", "INTERRUPTED", "UNKNOWN"])
def test_f1_non_success_terminal_with_no_failure_reason_exhausted_budget_blocks(state):
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=3)
    item = _item(
        "t1", "/repo/a", attempts_made=3, last_state=state, last_failure_reason=None,
        last_completed_at=_iso(NOW, seconds=-100000),  # long past any backoff
    )
    d = scheduler.plan([item], registry=reg, policy=policy, now=NOW).decisions[0]
    assert d.action == scheduler.ACTION_BLOCKED
    assert d.reason_code == scheduler.REASON_RETRY_EXHAUSTED
    assert d.failure_classification == scheduler.RECOVERABLE


@pytest.mark.parametrize("state", ["FAILED", "INTERRUPTED", "UNKNOWN"])
def test_f1_recently_completed_recoverable_never_assigns_immediately(state):
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=5, base_backoff_seconds=30)
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state=state, last_failure_reason=None,
        last_completed_at=_iso(NOW, seconds=-5),  # 5s ago, backoff is 30s
    )
    d = scheduler.plan([item], registry=reg, policy=policy, now=NOW).decisions[0]
    assert d.action != scheduler.ACTION_ASSIGN
    assert d.reason_code == scheduler.REASON_BACKOFF


@pytest.mark.parametrize("state", ["FAILED", "INTERRUPTED", "UNKNOWN"])
def test_f1_recoverable_after_backoff_assigns_attempt_made_plus_one(state):
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=5, base_backoff_seconds=30)
    item = _item(
        "t1", "/repo/a", attempts_made=2, last_state=state, last_failure_reason=None,
        last_completed_at=_iso(NOW, seconds=-1000),  # backoff long elapsed
    )
    d = scheduler.plan([item], registry=reg, policy=policy, now=NOW).assignments()[0]
    assert d.action == scheduler.ACTION_ASSIGN
    assert d.attempt == 3  # attempts_made + 1


def test_f1_cancelled_prior_remains_terminal_never_retries():
    reg = _registry()
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state="CANCELLED", last_failure_reason=None,
        last_completed_at=_iso(NOW, seconds=-100000),
    )
    d = scheduler.plan([item], registry=reg, now=NOW).decisions[0]
    assert d.action == scheduler.ACTION_BLOCKED
    assert d.reason_code == scheduler.REASON_TERMINAL_FAILURE
    assert d.failure_classification == scheduler.TERMINAL


def test_f1_blocked_reason_remains_terminal_never_retries():
    reg = _registry()
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state="FAILED", last_failure_reason="blocked:permission",
        last_completed_at=_iso(NOW, seconds=-100000),
    )
    d = scheduler.plan([item], registry=reg, now=NOW).decisions[0]
    assert d.action == scheduler.ACTION_BLOCKED
    assert d.reason_code == scheduler.REASON_TERMINAL_FAILURE


def test_f1_completed_prior_attempt_is_not_gated_and_assigns():
    """A successful prior attempt (a resume) must NOT enter retry gating."""
    reg = _registry()
    item = _item("t1", "/repo/a", attempts_made=1, last_state="COMPLETED", last_completed_at=NOW)
    d = scheduler.plan([item], registry=reg, now=NOW).assignments()[0]
    assert d.action == scheduler.ACTION_ASSIGN
    assert d.attempt == 2


def test_f1_budget_can_never_be_exceeded_for_any_gated_state():
    """Property: for every gated state, attempts_made >= max_attempts never assigns."""
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=2)
    for state in ("FAILED", "INTERRUPTED", "UNKNOWN", "CANCELLED"):
        for attempts in (2, 3, 9):
            item = _item(
                "t", "/repo/a", attempts_made=attempts, last_state=state, last_failure_reason=None,
                last_completed_at=_iso(NOW, seconds=-100000),
            )
            d = scheduler.plan([item], registry=reg, policy=policy, now=NOW).decisions[0]
            assert d.action != scheduler.ACTION_ASSIGN, (state, attempts, d.action, d.reason_code)


# --- F2: at most one ASSIGN per task_id per plan --------------------------


def test_f2_duplicate_task_id_different_workspaces_yields_exactly_one_assign():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    items = [_item("SAME", "/repo/a"), _item("SAME", "/repo/b")]
    plan = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assert len(plan.assignments()) == 1
    dups = [d for d in plan.deferrals() if d.reason_code == scheduler.REASON_DUPLICATE_TASK]
    assert len(dups) == 1


def test_f2_duplicate_winner_is_deterministic_under_input_reversal():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    items = [_item("SAME", "/repo/a"), _item("SAME", "/repo/b")]
    fwd = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    rev = scheduler.plan(list(reversed(items)), registry=reg, config=cfg, now=NOW)
    assert fwd.audit_records() == rev.audit_records()
    # Canonical winner is the lexicographically-smaller normalized workspace.
    assert fwd.assignments()[0].task_id == "SAME"


def test_f2_deferred_first_item_does_not_consume_task_id():
    """If the earliest same-task item is only DEFERRED (workspace busy), a later
    twin on a free workspace may still be assigned — dedup keys on ASSIGNED
    task_ids, not merely seen ones."""
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    # /repo/a is already busy in the live snapshot -> the /repo/a twin defers.
    load = scheduler.LoadSnapshot(global_running=1, busy_workspaces=frozenset({"/repo/a"}))
    items = [_item("SAME", "/repo/a"), _item("SAME", "/repo/b")]
    plan = scheduler.plan(items, registry=reg, config=cfg, load=load, now=NOW)
    assert len(plan.assignments()) == 1
    assert plan.assignments()[0].task_id == "SAME"
    reasons = {d.reason_code for d in plan.deferrals()}
    assert scheduler.REASON_WORKSPACE_BUSY in reasons
    assert scheduler.REASON_DUPLICATE_TASK not in reasons


def test_f2_malformed_twin_does_not_block_valid_canonical_item():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    items = [scheduler.WorkItem(task_id="SAME", workspace=""), _item("SAME", "/repo/b")]
    plan = scheduler.plan(items, registry=reg, config=cfg, now=NOW)
    assert len(plan.assignments()) == 1
    assert plan.assignments()[0].task_id == "SAME"
    assert {d.reason_code for d in plan.blocked()} == {scheduler.REASON_MALFORMED}


# --- F3: clamp corrupted snapshot counts ----------------------------------


def test_f3_negative_global_running_is_clamped():
    reg = _registry(max_concurrency=10)
    cfg = scheduler.SchedulerConfig(max_global_concurrency=1)
    load = scheduler.LoadSnapshot(global_running=-5)
    items = [_item(f"t{i}", f"/repo/{i}") for i in range(4)]
    plan = scheduler.plan(items, registry=reg, config=cfg, load=load, now=NOW)
    # Clamp to 0 -> exactly max_global_concurrency assignments, never more.
    assert len(plan.assignments()) == 1


def test_f3_negative_per_agent_running_is_clamped():
    reg = scheduler.AgentRegistry([scheduler.AgentSpec("c", "claude_code", frozenset({scheduler.CAP_ANY}), 1)])
    cfg = scheduler.SchedulerConfig(max_global_concurrency=10)
    load = scheduler.LoadSnapshot(running_by_agent={"c": -4})
    items = [_item(f"t{i}", f"/repo/{i}") for i in range(4)]
    plan = scheduler.plan(items, registry=reg, config=cfg, load=load, now=NOW)
    # Agent capacity is 1; negative running must not inflate it.
    assert len(plan.assignments()) == 1


# --- F4: missing/unparseable retry timestamp fails safe -------------------


@pytest.mark.parametrize("bad_ts", [None, "not-a-timestamp"])
def test_f4_missing_last_completed_at_defers_not_assigns(bad_ts):
    reg = _registry()
    policy = scheduler.RetryPolicy(max_attempts=5)
    item = _item(
        "t1", "/repo/a", attempts_made=1, last_state="FAILED", last_failure_reason="timeout",
        last_completed_at=bad_ts,
    )
    d = scheduler.plan([item], registry=reg, policy=policy, now=NOW).decisions[0]
    assert d.action == scheduler.ACTION_DEFER
    assert d.reason_code == scheduler.REASON_RETRY_TIMING_UNKNOWN
    assert d.next_eligible_at is None  # never fabricated


# --- F6: unknown priority normalization surfaced --------------------------


def test_f6_unknown_priority_is_surfaced_in_audit():
    reg = _registry()
    d = scheduler.plan([_item("t1", "/repo/a", priority="URGENT!!")], registry=reg, now=NOW).decisions[0]
    assert d.priority_recognized is False
    assert d.priority_rank == 0
    assert d.as_dict()["priority_recognized"] is False


def test_f6_known_priority_marked_recognized():
    reg = _registry()
    d = scheduler.plan([_item("t1", "/repo/a", priority="Critical")], registry=reg, now=NOW).decisions[0]
    assert d.priority_recognized is True


def test_a_probe_failure_is_transient_not_structural(monkeypatch):
    """`executor.available` is a live subprocess probe, so it can report False
    for reasons unrelated to the executor existing — a loaded machine missing
    the timeout, a daemon restarting.

    Omitting such an executor made the planner answer `no_capable_agent`: a
    *structural* verdict meaning "no agent can ever run this", which tells a
    human to change configuration. The honest answer is `agent_unavailable` —
    transient, and self-healing on the next tick."""
    from command_center import executors

    class _Down:
        id = "claude_code"
        availability_check = staticmethod(lambda: None)
        available = False

    monkeypatch.setattr(executors, "EXECUTORS", {"claude_code": _Down()})
    registry = scheduler.default_registry()

    assert registry.get("claude_code") is not None, "a down executor must still be registered"
    plan = scheduler.plan(
        [scheduler.WorkItem(task_id="t", workspace="/tmp/w")],
        registry=registry,
        now="2026-07-24T10:00:00",
    )
    decision = plan.decisions[0]
    assert decision.action == scheduler.ACTION_DEFER
    assert decision.reason_code == scheduler.REASON_AGENT_UNAVAILABLE


def test_an_executor_with_no_provider_is_still_structurally_absent(monkeypatch):
    """A stub with nothing behind it can never run anything, so its absence is
    a genuine structural fact rather than a transient one."""
    from command_center import executors

    class _Stub:
        id = "chatgpt"
        availability_check = None
        available = False

    monkeypatch.setattr(executors, "EXECUTORS", {"chatgpt": _Stub()})
    assert scheduler.default_registry().all() == []


def test_a_permission_denial_is_recoverable_not_terminal():
    """`blocked:` refusals are terminal because repeating an identical attempt
    cannot change the agent's own judgement. A *permission* denial is different
    in kind: it records the environment's tool policy at the moment of the run,
    and that policy is exactly what an operator changes in response to seeing
    the failure. Treating it as terminal left tasks permanently unrunnable
    after their permissions were fixed."""
    assert scheduler.classify_failure(
        state="FAILED", failure_reason="blocked:permission_denied:Glob,Read"
    ) == scheduler.RECOVERABLE


def test_an_agent_self_refusal_stays_terminal():
    """The distinction has to cut both ways, or it is just a blanket retry."""
    assert scheduler.classify_failure(
        state="FAILED", failure_reason="blocked:final_response:blocked by policy"
    ) == scheduler.TERMINAL


def test_a_recoverable_permission_denial_is_still_bounded_by_the_retry_budget():
    """Recoverable does not mean unbounded — a genuinely mis-permissioned task
    must still stop rather than retry forever."""
    plan = scheduler.plan(
        [
            scheduler.WorkItem(
                task_id="t",
                workspace="/tmp/w",
                attempts_made=3,
                last_state="FAILED",
                last_failure_reason="blocked:permission_denied:Bash",
                last_completed_at="2026-07-01T00:00:00",
            )
        ],
        registry=scheduler.default_registry(),
        policy=scheduler.RetryPolicy(max_attempts=3),
        now="2026-07-24T10:00:00",
    )
    assert plan.decisions[0].reason_code == scheduler.REASON_RETRY_EXHAUSTED
