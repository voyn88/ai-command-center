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
from command_center.runtime import db as runtime_db

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


def _spend_unavailable(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(task_pipeline, "daily_spend_usd", _raise)


def _capacity_unavailable(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(service, "active_by_executor", _raise)


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


def test_plan_fails_closed_when_cost_data_is_unavailable_with_default_settings(
    monkeypatch, pool
):
    # The exact repro from the bug report: master switch on, default
    # `pipeline_settings` (max_daily_spend_usd=0.0, i.e. no configured cap),
    # default policy (ollama cost 0.0, prefer_local=True) — a DB outage on the
    # spend read must still refuse dispatch, not assign 2-for-2.
    _enable_master_switch()
    _spend_unavailable(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _queued_task(title="t1")
    _queued_task(title="t2")

    plan = service.plan(ROOT)

    assert plan.budget_unknown is True
    assert plan.assignments == ()
    assert all(d.reason == models.DEFER_COST_DATA_UNAVAILABLE for d in plan.decisions)


def test_assign_is_a_noop_when_cost_data_is_unavailable(monkeypatch, pool):
    _enable_master_switch()
    _spend_unavailable(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    task = _queued_task(title="t1")

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is False
    assert result["reason"] == "cost_data_unavailable"
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert stored.get("executor") in (None, "")


def test_plan_fails_closed_when_in_flight_run_counts_are_unavailable(
    monkeypatch, pool
):
    # Spend reads fine, but the run table does not: the concurrency guard would
    # otherwise plan against "nothing is running" and assign 2-for-2.
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    _capacity_unavailable(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _queued_task(title="t1")
    _queued_task(title="t2")

    plan = service.plan(ROOT)

    assert plan.capacity_unknown is True
    assert plan.budget_unknown is False
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_CAPACITY_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_assign_is_a_noop_when_in_flight_run_counts_are_unavailable(monkeypatch, pool):
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    _capacity_unavailable(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    task = _queued_task(title="t1")

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is False
    assert result["reason"] == "capacity_data_unavailable"
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert stored.get("executor") in (None, "")


def test_active_by_executor_propagates_an_unreadable_store(tmp_path):
    # The read must NOT degrade to an empty map: `plan()` needs the failure to
    # reach it so the `capacity_unknown` gate engages.
    corrupt = tmp_path / "runtime.db"
    corrupt.write_bytes(b"not a sqlite database")

    with pytest.raises(Exception):
        service.active_by_executor(corrupt)


def test_active_by_executor_still_reads_a_healthy_store(tmp_path):
    # The propagation above must not have turned every read into a failure:
    # a migrated, empty store still reads cleanly as "nothing in flight".
    healthy = tmp_path / "runtime.db"
    runtime_db.migrate(healthy)

    assert service.active_by_executor(healthy) == {}


def _insert_run(db_path: Path, *, state: str, provider_id: str) -> None:
    """Write one `run` row directly, bypassing `create_run`.

    Deliberately raw sqlite3 rather than `runtime_db.connect`: the row being
    built is one `create_run` would refuse to build in this shape, and
    `active_by_executor` only ever reads the `run` table, so the session/task
    foreign keys are irrelevant to what is under test. Columns are derived from
    `PRAGMA table_info` so a later migration adding a NOT NULL column does not
    silently turn these tests into an INSERT error.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        cols = conn.execute("PRAGMA table_info(run)").fetchall()
        values: dict[str, object] = {}
        for _cid, name, col_type, notnull, default, _pk in cols:
            if not notnull or default is not None:
                continue
            if name == "state":
                values[name] = state
            elif name == "provider_id":
                values[name] = provider_id
            else:
                values[name] = 0 if col_type.upper() == "INTEGER" else f"x-{name}"
        values["provider_id"] = provider_id  # may carry a DEFAULT; set it anyway
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO run ({names}) VALUES ({marks})", tuple(values.values())
        )
        conn.commit()
    finally:
        conn.close()


def test_active_by_executor_counts_an_attributable_active_run(tmp_path):
    # Negative control for the two tests below: a well-formed active run is
    # counted against its executor, so refusing an unattributable one cannot be
    # mistaken for refusing everything.
    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)
    _insert_run(db, state="RUNNING", provider_id="ollama")

    assert service.active_by_executor(db) == {"ollama": 1}


def test_active_by_executor_refuses_an_active_run_it_cannot_attribute(tmp_path):
    # The row-level form of the fail-open this task exists to close. A run in an
    # active state with no `provider_id` is occupying a concurrency slot that
    # cannot be charged to anyone; skipping it (the old `and executor` guard)
    # reports that slot as free, which *raises* the effective per-agent ceiling
    # by exactly the work it failed to attribute. `NOT NULL DEFAULT
    # 'claude_code'` does not prevent this: `create_run` only rejects empty
    # provider ids when given a `provider_route`, and '' satisfies NOT NULL.
    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)
    _insert_run(db, state="RUNNING", provider_id="")

    with pytest.raises(service.UnattributableActiveRun):
        service.active_by_executor(db)


def test_active_by_executor_ignores_an_idle_run_with_no_provider(tmp_path):
    # The refusal is scoped to rows that actually hold a slot. A *terminal* run
    # with no `provider_id` accounts for no in-flight work, so it must not gate
    # dispatch — otherwise one historical row would wedge the planner forever.
    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)
    _insert_run(db, state="SUCCEEDED", provider_id="")

    assert service.active_by_executor(db) == {}


def test_plan_fails_closed_on_an_unattributable_active_run(monkeypatch, tmp_path):
    # End to end, with neither runtime.db read stubbed: the row-level refusal
    # has to reach `plan()` as the `capacity_unknown` gate, not escape as an
    # unhandled error and not get quietly counted as zero.
    _enable_master_switch()
    _free_local_pool(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _queued_task(title="t1")
    _queued_task(title="t2")

    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)
    _insert_run(db, state="RUNNING", provider_id="")

    plan = service.plan(ROOT, db_path=db)

    assert plan.budget_unknown is False  # the spend read is fine; capacity is not
    assert plan.capacity_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_CAPACITY_DATA_UNAVAILABLE for d in plan.decisions
    )


def _free_local_pool(monkeypatch):
    """The default-shaped pool from the bug report: one free, available, local
    executor — the configuration in which a simulated spend figure can never
    exceed any ceiling."""
    monkeypatch.setattr(
        service,
        "collect_executor_pool",
        lambda policy: [
            ExecutorProfile(
                id="ollama", label="Ollama", kind="cli", is_local=True,
                available=True, cost_per_task_usd=0.0,
            )
        ],
    )
    monkeypatch.setattr(
        project_config, "allowed_execution_providers", lambda project_id: ("ollama",)
    )


def test_plan_assigns_end_to_end_against_a_readable_database(monkeypatch, tmp_path):
    # Control arm for the test below: same defaults, nothing stubbed out over
    # the two runtime.db reads, only the store is actually readable. This is
    # the "2 of 2 assigned" the bug report measured — correct here, because the
    # guardrail inputs really were consulted.
    _enable_master_switch()
    _free_local_pool(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _queued_task(title="t1")
    _queued_task(title="t2")
    readable = tmp_path / "runtime.db"
    runtime_db.migrate(readable)

    plan = service.plan(ROOT, db_path=readable)

    assert pipeline_settings.load_settings(ROOT).max_daily_spend_usd == 0.0
    assert len(plan.assignments) == 2


def test_plan_fails_closed_end_to_end_against_an_unreadable_database(
    monkeypatch, tmp_path
):
    # The measured repro, with neither runtime.db read stubbed: default
    # `pipeline_settings` (max_daily_spend_usd=0.0, so the ceiling check is
    # skipped entirely) and a free local executor (whose $0 cost can never push
    # a simulated total past a ceiling either) — exactly where the old "assume
    # the ceiling is hit" fallback assigned 2 of 2. It must now assign none.
    _enable_master_switch()
    _free_local_pool(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _queued_task(title="t1")
    _queued_task(title="t2")
    unreadable = tmp_path / "runtime.db"
    unreadable.write_bytes(b"not a sqlite database")

    plan = service.plan(ROOT, db_path=unreadable)

    assert pipeline_settings.load_settings(ROOT).max_daily_spend_usd == 0.0
    assert plan.budget_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_COST_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_plan_fails_closed_when_the_store_breaks_between_the_two_reads(
    monkeypatch, tmp_path
):
    # Why the capacity gate has to exist in its own right rather than riding on
    # `budget_unknown`: the trailing-24h spend and the in-flight run counts are
    # two separate queries over two separate connections, so a store that dies
    # *between* them leaves the spend read looking perfectly healthy while the
    # capacity read fails. Nothing is stubbed over `active_by_executor` here —
    # the real read hits the real (now corrupt) file. Without its own gate this
    # planned against "nothing is running" and assigned.
    _enable_master_switch()
    _free_local_pool(monkeypatch)
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _queued_task(title="t1")
    _queued_task(title="t2")

    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)

    def _spend_then_break(*_a, **_k):
        db.write_bytes(b"not a sqlite database")  # the store dies mid-plan
        return 0.0

    monkeypatch.setattr(task_pipeline, "daily_spend_usd", _spend_then_break)

    plan = service.plan(ROOT, db_path=db)

    assert plan.budget_unknown is False  # the spend read genuinely succeeded
    assert plan.capacity_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_CAPACITY_DATA_UNAVAILABLE for d in plan.decisions
    )


def _insert_costed_run(db_path: Path, *, cost_literal: str) -> None:
    """A completed run inside the trailing-24h window plus one result event
    whose `total_cost_usd` is written as the raw JSON literal `cost_literal`.

    The literal is inserted as text rather than via `json.dumps` so a bare
    `NaN` — which `json.loads` accepts by default, and which a provider or a
    damaged row can therefore produce — is exercised exactly as it would be
    read back.
    """
    import sqlite3

    from command_center import models as _models

    now = _models.iso_now()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO run (id, session_id, task_id, sequence, state, project, "
            "task_type, repository_path, prompt, created_at, updated_at, "
            "completed_at, provider_id) VALUES "
            "('r1','s1','t1',1,'SUCCEEDED','AICC','implementation','/tmp/x','p',"
            "?,?,?,'claude_code')",
            (now, now, now),
        )
        conn.execute(
            "INSERT INTO run_event (run_id, seq, event_type, payload_json, created_at) "
            "VALUES ('r1', 1, 'stream_event', ?, ?)",
            (f'{{"type":"result","total_cost_usd":{cost_literal}}}', now),
        )
        conn.commit()
    finally:
        conn.close()


def _paid_pool(monkeypatch):
    """One available cloud executor at $50/task — expensive enough that a real
    ceiling must stop it, so an assignment can only mean the ceiling failed."""
    monkeypatch.setattr(
        service,
        "collect_executor_pool",
        lambda policy: [
            ExecutorProfile(
                id="claude_code", label="Claude Code", kind="cli", is_local=False,
                available=True, cost_per_task_usd=50.0,
            )
        ],
    )
    monkeypatch.setattr(
        project_config,
        "allowed_execution_providers",
        lambda project_id: ("claude_code",),
    )


def test_plan_fails_closed_on_a_corrupt_cost_row(monkeypatch, tmp_path):
    """The other half of the reported defect: the store is perfectly readable,
    the data in it is not.

    A `total_cost_usd` of NaN does not raise on read, so no existing gate
    engaged — and because every ceiling test is a `>` comparison, and every
    such comparison against NaN is False, one row silently disabled the daily
    ceiling for the whole system. Measured before the fix: 5 of 5 assigned,
    $250 committed against a $5 ceiling.
    """
    _enable_master_switch()
    _paid_pool(monkeypatch)
    policy_config.save_policy(
        ROOT, DispatchPolicy(prefer_local=False, cost_matrix={"claude_code": 50.0})
    )
    import dataclasses

    pipeline_settings.save_settings(
        ROOT,
        dataclasses.replace(
            pipeline_settings.load_settings(ROOT), max_daily_spend_usd=5.0
        ),
    )
    for i in range(5):
        _queued_task(title=f"t{i}")

    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)
    _insert_costed_run(db, cost_literal="NaN")

    plan = service.plan(ROOT, db_path=db)

    assert plan.budget_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_COST_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_plan_still_assigns_against_an_ordinary_cost_row(monkeypatch, tmp_path):
    """Control for the refusal above: a well-formed prior cost is summed and
    compared normally, so failing closed on corrupt data has not simply
    stopped dispatch everywhere."""
    _enable_master_switch()
    _paid_pool(monkeypatch)
    policy_config.save_policy(
        ROOT, DispatchPolicy(prefer_local=False, cost_matrix={"claude_code": 50.0})
    )
    import dataclasses

    pipeline_settings.save_settings(
        ROOT,
        dataclasses.replace(
            pipeline_settings.load_settings(ROOT), max_daily_spend_usd=500.0
        ),
    )
    _queued_task(title="t0")

    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)
    _insert_costed_run(db, cost_literal="1.25")

    plan = service.plan(ROOT, db_path=db)

    assert plan.budget_unknown is False
    assert plan.daily_spend_usd == pytest.approx(1.25)
    assert len(plan.assignments) == 1


def test_assign_is_a_noop_on_a_corrupt_cost_row(monkeypatch, tmp_path):
    """The refusal must hold for the *write* path too, not just the dry run."""
    _enable_master_switch()
    _paid_pool(monkeypatch)
    policy_config.save_policy(
        ROOT, DispatchPolicy(prefer_local=False, cost_matrix={"claude_code": 50.0})
    )
    task = _queued_task(title="t0")

    db = tmp_path / "runtime.db"
    runtime_db.migrate(db)
    _insert_costed_run(db, cost_literal="NaN")

    result = service.assign(ROOT, CALLER, confirmed=True, db_path=db)

    assert result["applied"] is False
    assert result["reason"] == "cost_data_unavailable"
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert stored.get("executor") in (None, "")


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
# An unreadable dispatch policy fails closed
# (VOYN-W0-AICC-DISPATCH-FAILCLOSED-FALSE)
# --------------------------------------------------------------------------


def _corrupt_policy_file():
    path = policy_config.policy_file_path(ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"per_agent_limits": {"claude_c', encoding="utf-8")
    return path


def test_plan_fails_closed_when_the_policy_cannot_be_read(monkeypatch, pool):
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    _queued_task(title="a")
    _queued_task(title="b")
    _corrupt_policy_file()

    plan = service.plan(ROOT)

    assert plan.policy_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_POLICY_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_a_corrupt_policy_cannot_dispatch_more_than_the_policy_it_replaced(
    monkeypatch, pool
):
    """The measured fail-open, end to end through the real `plan()`.

    An operator pins `claude_code` to one concurrent run and forbids `ollama`
    for this project. The healthy policy assigns 1 of 3. Corrupting the same
    file used to assign **3 of 3** — every flag on the plan still reporting
    healthy — because the defaults it fell back to carry empty limit maps.
    """
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    monkeypatch.setattr(
        project_config, "allowed_execution_providers", lambda _p: ("claude_code",)
    )
    for title in ("a", "b", "c"):
        _queued_task(title=title)

    policy_config.save_policy(
        ROOT,
        DispatchPolicy(
            prefer_local=False,
            cost_matrix={"claude_code": 0.5},
            per_agent_limits={
                "claude_code": models.AgentLimit(max_concurrent=1, max_spend_usd=0.0)
            },
        ),
        actor="operator",
    )

    healthy = service.plan(ROOT)
    assert len(healthy.assignments) == 1
    assert healthy.policy_unknown is False

    _corrupt_policy_file()
    corrupt = service.plan(ROOT)

    # The property: a failed read can never widen what a plan is allowed to do.
    assert len(corrupt.assignments) <= len(healthy.assignments)
    assert corrupt.assignments == ()
    assert corrupt.policy_unknown is True


def test_the_plan_says_which_read_failed(monkeypatch, pool):
    # An unreadable policy and an unreadable runtime store must stay tellable
    # apart in the response, not collapse into one generic refusal.
    _enable_master_switch()
    _spend_unavailable(monkeypatch)
    _queued_task(title="a")
    assert service.plan(ROOT).decisions[0].reason == models.DEFER_COST_DATA_UNAVAILABLE

    _spend(monkeypatch, 0.0)
    _corrupt_policy_file()
    assert (
        service.plan(ROOT).decisions[0].reason
        == models.DEFER_POLICY_DATA_UNAVAILABLE
    )


def test_assign_is_a_noop_when_the_policy_cannot_be_read(monkeypatch, pool):
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    task = _queued_task(title="a")
    _corrupt_policy_file()

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is False
    assert result["reason"] == "policy_data_unavailable"
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert not stored.get("executor")


def test_a_missing_policy_file_still_dispatches(monkeypatch, pool):
    # The fallback's legitimate case must keep working: no policy saved yet is
    # a fresh install, not a failure, so dispatch proceeds normally.
    _enable_master_switch()
    _spend(monkeypatch, 0.0)
    _queued_task(title="a")
    path = policy_config.policy_file_path(ROOT)
    if path.exists():
        path.unlink()

    plan = service.plan(ROOT)

    assert plan.policy_unknown is False
    assert len(plan.assignments) == 1


# --------------------------------------------------------------------------
# The configured spend ceiling — the fourth guardrail input that can be
# corrupt, and the only one whose permissive reading is spelled the same as
# its legitimate "unset" one (`0.0` = no cap).
# --------------------------------------------------------------------------


def _write_raw_ceiling(value):
    """Put `max_daily_spend_usd` into the settings file verbatim, the way a
    hand-edit, a torn write or a non-Python client would — bypassing
    `PipelineSettings`, which is the thing under test."""
    from command_center import storage

    path = pipeline_settings.settings_file_path(ROOT)
    data = storage.read_json(path, {})
    data["max_daily_spend_usd"] = value
    storage.atomic_write_json(path, data)
    return path


@pytest.mark.parametrize(
    "raw",
    ["5.0", 20_000, -5, None, "abc", float("nan"), float("inf")],
    ids=["string", "out_of_range", "negative", "null", "garbage", "nan", "inf"],
)
def test_plan_fails_closed_on_a_corrupt_configured_ceiling(monkeypatch, pool, raw):
    """A ceiling that is present but unusable must refuse, not read as unset.

    Measured against the real `plan()` before this was closed: $100.00 already
    spent against a configured $5.00 ceiling defers both tasks — but write the
    *same* ceiling in any of these corrupt forms and it assigned 2 of 2 with
    `budget_unknown=False`, because each one decayed to `0.0`, which every
    spend gate reads as "no cap configured".
    """
    _enable_master_switch()
    policy_config.save_policy(ROOT, DispatchPolicy())
    _spend(monkeypatch, 100.0)
    _write_raw_ceiling(raw)
    _queued_task(title="t1", executor="claude_code", executor_pinned=True)
    _queued_task(title="t2", executor="claude_code", executor_pinned=True)

    plan = service.plan(ROOT)

    assert plan.budget_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_COST_DATA_UNAVAILABLE for d in plan.decisions
    )
    # The refusal has to be *transmittable*: `json.dump` emits bare `NaN`,
    # which `JSON.parse` rejects, so the reason would arrive unreadable.
    assert plan.as_dict()["max_daily_spend_usd"] is None


def test_a_corrupt_ceiling_cannot_dispatch_more_than_the_ceiling_it_replaced(
    monkeypatch, pool
):
    """The differential the bug report asks for, end to end.

    Same operator intent ($5.00/day), same spend, same queue — only the
    ceiling's *representation* differs. A failed read must never widen what a
    plan is allowed to do.
    """
    _enable_master_switch()
    policy_config.save_policy(ROOT, DispatchPolicy())
    _spend(monkeypatch, 100.0)
    _queued_task(title="t1", executor="claude_code", executor_pinned=True)
    _queued_task(title="t2", executor="claude_code", executor_pinned=True)

    _write_raw_ceiling(5.0)
    healthy = service.plan(ROOT)
    assert healthy.assignments == ()
    # Refused on the *budget*, not on a data gate — the ceiling was evaluated.
    assert healthy.budget_unknown is False
    assert all(d.reason == models.DEFER_DAILY_BUDGET for d in healthy.decisions)

    _write_raw_ceiling("5.0")
    corrupt = service.plan(ROOT)

    assert len(corrupt.assignments) <= len(healthy.assignments)
    assert corrupt.budget_unknown is True


def test_a_corrupt_ceiling_still_refuses_when_nothing_has_been_spent(
    monkeypatch, pool
):
    """The sharpest form: no spend, a free local executor, and the default
    `prefer_local` — the exact shape the report measured as assigning 2 of 2.

    Nothing about the *spend* stops this plan; only the fact that the ceiling
    governing it cannot be evaluated does. A gate that needed the budget to be
    exhausted first would not fire here at all.
    """
    _enable_master_switch()
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _spend(monkeypatch, 0.0)
    _write_raw_ceiling("5.0")
    _queued_task(title="t1")
    _queued_task(title="t2")

    plan = service.plan(ROOT)

    assert plan.budget_unknown is True
    assert plan.assignments == ()


def test_assign_is_a_noop_on_a_corrupt_configured_ceiling(monkeypatch, pool):
    """`plan()` refusing is only half of it — `assign()` must not write."""
    _enable_master_switch()
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _spend(monkeypatch, 0.0)
    _write_raw_ceiling(-5)
    task = _queued_task(title="t1")

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is False
    assert result["reason"] == "cost_data_unavailable"
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert not stored.get("executor")


def test_an_unset_ceiling_still_dispatches(monkeypatch, pool):
    """The fallback's legitimate case. `0.0`/absent means "no budget
    configured" — the shipped default — and must keep dispatching, or this
    whole gate would just be a disguised outage."""
    _enable_master_switch()
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _spend(monkeypatch, 0.0)
    _queued_task(title="t1")

    _write_raw_ceiling(0.0)
    assert len(service.plan(ROOT).assignments) == 1
    assert service.plan(ROOT).budget_unknown is False

    # And with the key absent entirely, as on a fresh install.
    from command_center import storage

    path = pipeline_settings.settings_file_path(ROOT)
    data = storage.read_json(path, {})
    data.pop("max_daily_spend_usd", None)
    storage.atomic_write_json(path, data)

    assert len(service.plan(ROOT).assignments) == 1
    assert service.plan(ROOT).budget_unknown is False


def test_a_corrupt_ceiling_is_tellable_apart_in_the_log(monkeypatch, pool, caplog):
    """An unreachable database and a hand-edited ceiling both surface as
    `budget_unknown`, and they need different remedies — so the log has to
    name which store produced the refusal."""
    _enable_master_switch()
    policy_config.save_policy(ROOT, DispatchPolicy(prefer_local=True))
    _spend(monkeypatch, 0.0)
    _write_raw_ceiling("5.0")
    _queued_task(title="t1")

    with caplog.at_level("WARNING"):
        plan = service.plan(ROOT)

    assert plan.budget_unknown is True
    assert "daily spend ceiling" in caplog.text
    assert str(pipeline_settings.settings_file_path(ROOT)) in caplog.text


# --------------------------------------------------------------------------
# The pipeline settings document — the fifth guardrail input that can fail to
# load, and the last one still read through a swallow-and-default. Its
# fallback is the only one that is wrong in *both* directions at once.
# --------------------------------------------------------------------------


def _tear_settings_file(raw: str = '{"enabled": true, "max_daily_spend_us'):
    """Leave a settings file that exists but cannot be parsed — a torn write,
    a half-flushed page, a hand-edit. Written verbatim, bypassing
    `PipelineSettings`, which is the thing under test."""
    path = pipeline_settings.settings_file_path(ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "raw",
    ['{"enabled": true, "max_daily_spend_us', "", "   ", "[]", '"a string"', "null"],
    ids=["torn", "empty", "blank", "list", "string", "null"],
)
def test_plan_fails_closed_on_an_unreadable_settings_document(monkeypatch, pool, raw):
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1")
    _queued_task(title="t2")
    _tear_settings_file(raw)

    plan = service.plan(ROOT)

    assert plan.settings_unknown is True
    assert plan.assignments == ()
    assert all(
        d.reason == models.DEFER_SETTINGS_DATA_UNAVAILABLE for d in plan.decisions
    )


def test_an_unreadable_settings_document_is_not_reported_as_the_kill_switch(
    monkeypatch, pool
):
    """The misattribution is the whole defect, not a cosmetic complaint.

    Before this gate, a torn settings file read as the all-off defaults, so
    `not settings.enabled` was True and the plan reported
    `kill_switch_engaged` — an operator decision nobody had made. The remedy
    that names points at (turn the master switch back on) is precisely the one
    that destroys the configuration; see the writer test below.
    """
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1")
    _tear_settings_file()

    plan = service.plan(ROOT)

    assert plan.kill_switch_engaged is False
    assert plan.settings_unknown is True
    assert plan.as_dict()["settings_unknown"] is True


def test_an_unreadable_settings_document_never_reads_as_an_absent_spend_ceiling(
    monkeypatch, pool
):
    """The unsafe half of the old fallback, isolated.

    `max_daily_spend_usd` decays to `0.0` under the all-off defaults, and
    `0.0` does not mean a strict cap — it means *no cap*. So the placeholder
    the service substitutes must not be a number that any ceiling check would
    accept, and the plan must not transmit one either.
    """
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1")
    _tear_settings_file()

    plan = service.plan(ROOT)

    assert not plan.max_daily_spend_usd > 0  # NaN, not the permissive 0.0
    # And the refusal does not rest on the new gate alone: an unusable ceiling
    # independently engages the budget gate one level down.
    assert plan.budget_unknown is True
    # Transmittable: `json.dump` emits bare `NaN`, which `JSON.parse` rejects.
    assert plan.as_dict()["max_daily_spend_usd"] is None


def test_a_torn_settings_file_cannot_dispatch_more_than_the_config_it_replaced(
    monkeypatch, pool
):
    """The differential, end to end. Same operator intent, same queue — only
    the settings file's *readability* differs. A failed read must never widen
    what a plan is allowed to do."""
    import dataclasses

    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1")
    _queued_task(title="t2")

    pipeline_settings.save_settings(
        ROOT,
        dataclasses.replace(
            pipeline_settings.load_settings(ROOT), enabled=True, max_daily_spend_usd=5.0
        ),
    )
    healthy = service.plan(ROOT)
    assert healthy.settings_unknown is False

    _tear_settings_file()
    torn = service.plan(ROOT)

    assert len(torn.assignments) <= len(healthy.assignments)
    assert torn.assignments == ()
    assert torn.settings_unknown is True


def test_assign_is_a_noop_when_the_settings_document_is_unreadable(monkeypatch, pool):
    """`plan()` refusing is only half of it — `assign()` must not write."""
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    task = _queued_task(title="t1")
    _tear_settings_file()

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is False
    assert result["reason"] == "settings_data_unavailable"
    stored = {t["id"]: t for t in tasks_repository.load_tasks(ROOT)}[task["id"]]
    assert not stored.get("executor")


def test_a_missing_settings_file_still_dispatches(monkeypatch, pool):
    """The fallback's legitimate case, and the thing this gate must not break:
    a fresh install has no settings file at all. That is "nothing configured
    yet", not a failure — the master switch is simply off, which is the
    shipped default and reports as the kill switch, truthfully."""
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1")
    path = pipeline_settings.settings_file_path(ROOT)
    if path.exists():
        path.unlink()

    plan = service.plan(ROOT)

    assert plan.settings_unknown is False
    assert plan.kill_switch_engaged is True

    _enable_master_switch()
    assert len(service.plan(ROOT).assignments) == 1
    assert service.plan(ROOT).settings_unknown is False


def test_an_unreadable_settings_document_is_tellable_apart_in_the_log(
    monkeypatch, pool, caplog
):
    """Five stores can now produce a refusal and they need different remedies,
    so the log has to name which one failed — and must not blame the ceiling
    for a document that could not be read at all."""
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1")
    _tear_settings_file()

    with caplog.at_level("WARNING"):
        plan = service.plan(ROOT)

    assert plan.settings_unknown is True
    assert "pipeline settings unreadable" in caplog.text
    assert str(pipeline_settings.settings_file_path(ROOT)) in caplog.text
    # The corrupt-*ceiling* line names a different remedy and must not fire
    # here: the ceiling was never read, so it cannot be the thing at fault.
    assert "daily spend ceiling" not in caplog.text


def test_assign_reports_the_same_refusal_precedence_the_engine_does(monkeypatch, pool):
    """Several gates engage at once, so the *order* they are tested in decides
    which remedy the operator is sent to.

    An unreadable settings document also yields an unusable ceiling, so it
    engages `budget_unknown` too. Testing that first made `assign()` answer
    "the trailing-24h spend could not be read" — pointing at the runtime
    store — to a caller whose actual problem is a torn settings file. That is
    this ticket's misattribution reintroduced one layer up, which is why the
    precedence is asserted rather than left to read naturally.
    """
    _spend(monkeypatch, 0.0)
    policy_config.save_policy(ROOT, DispatchPolicy())
    _queued_task(title="t1")
    _tear_settings_file()

    computed = service.plan(ROOT)
    # Both gates really are engaged — the test would be vacuous otherwise.
    assert computed.settings_unknown is True
    assert computed.budget_unknown is True

    result = service.assign(ROOT, CALLER, confirmed=True)

    assert result["applied"] is False
    assert result["reason"] == "settings_data_unavailable"
    # The reason `assign` reports and the reason the plan's decisions carry
    # are the same fact, so they must not be able to drift apart.
    assert {d.reason for d in computed.decisions} == {
        models.DEFER_SETTINGS_DATA_UNAVAILABLE
    }
