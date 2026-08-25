"""Service-layer tests: input collection, dry-run planning, and applying an
assignment through `tasks_repository` (the board's single writer).

Kept hermetic by redirecting `AICC_DATA_DIR` (session conftest) and by
monkeypatching the two inputs that would otherwise reach out to real CLIs / a
migrated runtime.db — the executor pool and the trailing-24h spend — so each
test drives a deterministic context.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import pipeline_settings, project_config, tasks_repository
from command_center import task_pipeline
from command_center.dispatch import models, policy_config, service
from command_center.dispatch.models import DispatchPolicy, ExecutorProfile
from command_center.http_auth.identity import Principal

ROOT = Path("/unused-AICC_DATA_DIR-overrides")

# `assign()` has no `actor` parameter to pass (VOYN-W0-AICC-AUTH-HTTP-01): the
# caller is a verified `Principal`, so these tests supply one rather than a
# string an HTTP client could have chosen.
CALLER = Principal(principal_id="operator:test", tenant_id="tenant-1")


@pytest.fixture
def pool(monkeypatch):
    """A deterministic two-executor pool: a free local one and a paid cloud one.

    Also widens the project's permitted providers to both, so the engine's
    local-preference is what decides selection rather than project policy."""
    profiles = [
        ExecutorProfile(
            id="ollama", label="Ollama", kind="cli", is_local=True,
            available=True, cost_per_task_usd=0.0,
        ),
        ExecutorProfile(
            id="claude_code", label="Claude Code", kind="cli", is_local=False,
            available=True, cost_per_task_usd=0.5,
        ),
    ]
    monkeypatch.setattr(service, "collect_executor_pool", lambda policy: profiles)
    monkeypatch.setattr(service, "active_by_executor", lambda db_path: {})
    monkeypatch.setattr(
        project_config,
        "allowed_execution_providers",
        lambda project_id: ("ollama", "claude_code"),
    )
    return profiles


def _spend(monkeypatch, value: float):
    monkeypatch.setattr(task_pipeline, "daily_spend_usd", lambda *_a, **_k: value)


def _enable_master_switch():
    settings = pipeline_settings.load_settings(ROOT)
    import dataclasses

    pipeline_settings.save_settings(
        ROOT, dataclasses.replace(settings, enabled=True)
    )


def _queued_task(**kwargs):
    kwargs.setdefault("project", "AICC")
    kwargs.setdefault("task_type", "implementation")
    kwargs.setdefault("status", "Backlog")
    # `executor_pinned` is not a `new_task_record` field — apply it post-create
    # through the repository so the write still goes through the single writer.
    pinned = kwargs.pop("executor_pinned", False)
    task = tasks_repository.create_task(ROOT, title=kwargs.pop("title", "T"), **kwargs)
    if pinned:
        def _mutator(tasks):
            for t in tasks:
                if t["id"] == task["id"]:
                    t["executor_pinned"] = True
            return None

        tasks_repository.mutate_tasks(ROOT, _mutator)
        task = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    return task


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def test_collect_queued_tasks_only_returns_backlog_and_next(monkeypatch):
    _queued_task(title="a", status="Backlog")
    _queued_task(title="b", status="Next")
    _queued_task(title="c", status="In Progress")
    _queued_task(title="d", status="Done")

    queued = service.collect_queued_tasks(ROOT)

    assert {t.priority for t in queued}  # smoke: shaped objects
    assert len(queued) == 2
    # Default project policy permits claude_code.
    assert all("claude_code" in (t.allowed_executors or set()) for t in queued)


def test_collect_queued_tasks_redacts_sensitive_projects(monkeypatch):
    # BANK/LEGAL work is never dispatched by this operator-facing plane: a
    # sensitive-project task is dropped at collection so its id/project never
    # reaches the engine (mirrors the conflicts/audit per-read exclusion).
    _queued_task(title="bank", project="BANK", status="Backlog")
    _queued_task(title="legal", project="LEGAL", status="Next")
    ok = _queued_task(title="ok", project="AICC", status="Backlog")

    queued = service.collect_queued_tasks(ROOT)

    assert [t.id for t in queued] == [ok["id"]]
    assert all(t.project not in {"BANK", "LEGAL"} for t in queued)


def test_collect_queued_tasks_reads_pin_and_priority(monkeypatch):
    _queued_task(title="pinned", priority="Critical", executor="codex",
                 executor_pinned=True)

    queued = service.collect_queued_tasks(ROOT)

    assert queued[0].priority == "Critical"
    assert queued[0].pinned_executor == "codex"


# --------------------------------------------------------------------------
# plan() wires the real primitives
# --------------------------------------------------------------------------


def test_plan_prefers_free_local_executor(monkeypatch, pool):
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _queued_task(title="t1")

    plan = service.plan(ROOT)

    assert plan.assignments[0].assigned_executor == "ollama"
    assert plan.kill_switch_engaged is False


def test_plan_reports_kill_switch_when_master_switch_off(monkeypatch, pool):
    # Master switch left OFF (the default / the state kill_switch persists).
    _spend(monkeypatch, 0.0)
    _queued_task(title="t1")

    plan = service.plan(ROOT)

    assert plan.kill_switch_engaged is True
    assert plan.assignments == ()
    assert plan.decisions[0].reason == models.DEFER_KILL_SWITCH


def test_plan_enforces_daily_budget_from_pipeline_settings(monkeypatch, pool):
    import dataclasses

    _enable_master_switch()
    settings = pipeline_settings.load_settings(ROOT)
    pipeline_settings.save_settings(
        ROOT, dataclasses.replace(settings, max_daily_spend_usd=0.4)
    )
    _spend(monkeypatch, 0.3)  # 0.3 spent, ceiling 0.4
    # Force the cloud (paid) executor via a hard pin so the free local one
    # can't sidestep the budget — 0.3 + 0.5 = 0.8 > 0.4.
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1", executor="claude_code", executor_pinned=True)

    plan = service.plan(ROOT)

    assert plan.assignments == ()
    assert plan.decisions[0].reason == models.DEFER_DAILY_BUDGET


# --------------------------------------------------------------------------
# assign() applies through tasks_repository
# --------------------------------------------------------------------------


def test_assign_records_executor_on_the_task(monkeypatch, pool):
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    task = _queued_task(title="t1")

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is True
    assert result["assigned_task_ids"] == [task["id"]]
    # Persisted through the single writer, not just returned.
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert stored["executor"] == "ollama"
    assert stored["agent"] == "ollama"


def test_assign_requires_confirmation(monkeypatch, pool):
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    task = _queued_task(title="t1")

    result = service.assign(ROOT, CALLER, confirmed=False)

    assert result["applied"] is False
    assert result["reason"] == "confirmation_required"
    # Nothing was written.
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert stored.get("executor") in (None, "")


def test_assign_is_a_noop_while_kill_switch_engaged(monkeypatch, pool):
    # Master switch OFF -> kill switch engaged. Even confirmed, apply nothing.
    _spend(monkeypatch, 0.0)
    task = _queued_task(title="t1")

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is False
    assert result["reason"] == "kill_switch_engaged"
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert stored.get("executor") in (None, "")


# --------------------------------------------------------------------------
# VOYN-W0-AICC-SPEND-CAP / -DISPATCH-FAILCLOSED — the caller no longer
# swallows, and no longer substitutes a number for an unknown spend.
# --------------------------------------------------------------------------


def _set_ceiling(value: float):
    import dataclasses

    settings = pipeline_settings.load_settings(ROOT)
    pipeline_settings.save_settings(
        ROOT, dataclasses.replace(settings, max_daily_spend_usd=value)
    )


def _raise_spend_unknown(monkeypatch, kind: str):
    def _boom(*_a, **_k):
        raise task_pipeline.SpendUnknownError(kind, "seeded by test")

    monkeypatch.setattr(task_pipeline, "daily_spend_usd", _boom)


def test_plan_holds_closed_when_the_spend_cannot_be_read(monkeypatch, pool):
    """The regression: the old handler substituted `max_daily_spend_usd or
    0.0`, and with a free executor in the pool that assigned *every* task —
    byte-identical to "nothing was spent". Nothing may be assigned here."""
    _enable_master_switch()
    _set_ceiling(5.0)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _raise_spend_unknown(
        monkeypatch, task_pipeline.SpendUnknownError.STORAGE_UNAVAILABLE
    )
    _queued_task(title="t1")
    _queued_task(title="t2")

    plan = service.plan(ROOT)

    assert plan.assignments == ()
    assert {d.reason for d in plan.deferred} == {models.DEFER_SPEND_UNKNOWN}


def test_plan_reports_an_unknown_spend_as_unknown_not_as_budget_exhausted(
    monkeypatch, pool
):
    _enable_master_switch()
    _set_ceiling(5.0)
    _raise_spend_unknown(
        monkeypatch, task_pipeline.SpendUnknownError.CORRUPT_COST_EVENT
    )
    _queued_task(title="t1")

    body = service.plan(ROOT).as_dict()

    assert body["spend_status"] == models.SPEND_UNKNOWN
    assert body["daily_spend_usd"] is None
    assert body["decisions"][0]["reason"] == models.DEFER_SPEND_UNKNOWN
    assert body["decisions"][0]["reason"] != models.DEFER_DAILY_BUDGET


def test_unknown_spend_is_recorded_in_the_existing_activity_log(monkeypatch, pool):
    from command_center import activity_log

    _enable_master_switch()
    _set_ceiling(5.0)
    _raise_spend_unknown(
        monkeypatch, task_pipeline.SpendUnknownError.CORRUPT_COST_EVENT
    )
    _queued_task(title="t1")

    service.plan(ROOT)

    events = activity_log.load_activity(limit=50)
    logged = [e for e in events if e.get("type") == service.EV_DISPATCH_SPEND_UNKNOWN]
    assert logged, [e.get("type") for e in events]
    # The kind is in the message so the operator can tell a corrupt event from
    # an unreachable store without reading code.
    assert task_pipeline.SpendUnknownError.CORRUPT_COST_EVENT in logged[0]["message"]


def test_assign_applies_nothing_when_the_spend_is_unknown(monkeypatch, pool):
    _enable_master_switch()
    _set_ceiling(5.0)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _raise_spend_unknown(
        monkeypatch, task_pipeline.SpendUnknownError.STORAGE_UNAVAILABLE
    )
    task = _queued_task(title="t1")

    result = service.assign(ROOT, CALLER, confirmed=True)

    # `assign` is allowed to run (the kill switch is off) but the plan it
    # applies contains no assignment, so nothing reaches the board.
    assert result["assigned_task_ids"] == []
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert stored.get("executor") in (None, "")


def test_plan_does_not_measure_spend_when_no_ceiling_is_configured(monkeypatch, pool):
    """`max_daily_spend_usd <= 0` is "no ceiling". Not measuring is what
    removes this whole failure class from the default configuration — so the
    primitive must not even be called, and a plan must still be produced."""
    _enable_master_switch()
    _set_ceiling(0.0)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    calls: list[object] = []

    def _record_call(*a, **k):
        calls.append(a)
        raise AssertionError("daily_spend_usd must not be called without a ceiling")

    monkeypatch.setattr(task_pipeline, "daily_spend_usd", _record_call)
    _queued_task(title="t1")

    plan = service.plan(ROOT)

    assert calls == []
    assert len(plan.assignments) == 1
    assert plan.spend_status == models.SPEND_NOT_MEASURED
    assert plan.as_dict()["daily_spend_usd"] is None


def test_plan_does_not_swallow_a_programming_error_from_the_spend_primitive(
    monkeypatch, pool
):
    """The old `except Exception` laundered `AttributeError`, `KeyError` and a
    misspelled call into a budget verdict. Only `SpendUnknownError` is caught
    now, so a bug travels as a bug."""
    _enable_master_switch()
    _set_ceiling(5.0)

    def _bug(*_a, **_k):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(task_pipeline, "daily_spend_usd", _bug)
    _queued_task(title="t1")

    with pytest.raises(AttributeError):
        service.plan(ROOT)
