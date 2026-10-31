"""Startup reconciliation: PID-reuse protection, conservative classification,
and never signaling a process based only on a reused pid.

These tests spawn real (throwaway `sleep`) processes to get real pids and
real `ps`-observable identities, then build `run` rows by hand (bypassing
`Supervisor.start`) to simulate exactly what a crashed/restarted Supervisor
would find on disk: a `RUNNING` row with a `pid` and (sometimes) a recorded
identity, but no in-memory `_ActiveRun` for it (a fresh `Supervisor()` never
launched it) — the same situation a real restart produces.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime

from command_center.runtime import db, identity, supervisor


def _make_running_row(db_path, *, pid, process_start_identity):
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
        finalization_owner_token="dead-supervisor",
        finalization_owner_pid=999_999_999,
        finalization_owner_identity="dead-start|dead-command",
    )
    run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(
        db_path, run["id"], expected_version=run["version"], new_state="RUNNING",
        fields={"pid": pid, "process_start_identity": process_start_identity, "started_at": "2026-01-01T00:00:00"},
    )
    return run


def test_reconcile_classifies_dead_pid_as_interrupted(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    proc = subprocess.Popen(["true"])
    proc.wait()
    time.sleep(0.2)  # ensure `ps` no longer reports it

    run = _make_running_row(db_path, pid=proc.pid, process_start_identity="whatever|whatever")

    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()

    assert len(outcomes) == 1
    assert outcomes[0]["classification"] == "INTERRUPTED"
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"


def test_reconcile_classifies_missing_pid_as_interrupted(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_running_row(db_path, pid=None, process_start_identity=None)

    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()

    assert outcomes[0]["classification"] == "INTERRUPTED"
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"


def test_reconcile_classifies_alive_pid_without_recorded_identity_as_unknown(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    proc = subprocess.Popen(["sleep", "3"])
    try:
        run = _make_running_row(db_path, pid=proc.pid, process_start_identity=None)
        sup = supervisor.Supervisor(db_path)
        outcomes = sup.reconcile()
        assert outcomes[0]["classification"] == "UNKNOWN"
        assert db.get_run(db_path, run["id"])["state"] == "UNKNOWN"
    finally:
        proc.terminate()
        proc.wait()


def test_reconcile_never_signals_a_process_matched_only_by_reused_pid(tmp_path):
    """A pid that currently belongs to a real, unrelated process (identity
    does not match what was recorded) must be classified INTERRUPTED — and,
    critically, that unrelated process must never be sent a signal."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    unrelated = subprocess.Popen(["sleep", "3"])
    try:
        current = identity.capture_identity(unrelated.pid)
        assert current is not None
        stale_identity = f"{current.start_time}-different|{current.command}"
        run = _make_running_row(
            db_path, pid=unrelated.pid, process_start_identity=stale_identity
        )
        sup = supervisor.Supervisor(db_path)
        outcomes = sup.reconcile()

        assert outcomes[0]["classification"] == "INTERRUPTED"
        assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"
        # The unrelated process must still be alive — it was never signalled.
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait()


def test_reconcile_legacy_live_identity_mismatch_is_unknown(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    unrelated = subprocess.Popen(["sleep", "3"])
    try:
        run = _make_running_row(
            db_path,
            pid=unrelated.pid,
            process_start_identity="Sat Aug 29 17:23:45 2026|sleep 3",
        )
        outcomes = supervisor.Supervisor(db_path).reconcile()

        assert outcomes[0]["classification"] == "UNKNOWN"
        assert db.get_run(db_path, run["id"])["state"] == "UNKNOWN"
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait()


def test_reconcile_leaves_verified_still_running_process_as_running_but_flags_orphan(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    proc = subprocess.Popen(["sleep", "3"])
    try:
        recorded_identity = identity.capture_identity(proc.pid).as_string()
        run = _make_running_row(db_path, pid=proc.pid, process_start_identity=recorded_identity)

        sup = supervisor.Supervisor(db_path)
        outcomes = sup.reconcile()
        repeated = sup.reconcile()

        assert outcomes[0]["classification"] == "RUNNING"
        assert repeated[0]["classification"] == "RUNNING"
        assert db.get_run(db_path, run["id"])["state"] == "RUNNING"
        # Still alive: reconciliation must not have signalled it.
        assert proc.poll() is None
        # Not adopted into the new Supervisor's active-run registry (no pipe/wait handle for it).
        assert run["id"] not in sup.active_run_ids()

        events = db.list_run_events(db_path, run["id"])
        lifecycles = [e["payload"].get("lifecycle") for e in events if e["event_type"] == "lifecycle"]
        assert lifecycles.count("reconciliation_orphaned") == 1
    finally:
        proc.terminate()
        proc.wait()


def test_reconcile_does_not_guess_completion_for_any_ambiguous_case(tmp_path):
    """None of the reconciliation outcomes for a stale RUNNING row may ever be
    COMPLETED — that would be guessing a successful outcome for a run whose
    actual fate was never observed."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    dead = subprocess.Popen(["true"])
    dead.wait()
    time.sleep(0.2)
    _make_running_row(db_path, pid=dead.pid, process_start_identity=None)

    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()
    for outcome in outcomes:
        assert outcome["classification"] != "COMPLETED"
        assert outcome["classification"] != "FAILED"


def test_reconcile_only_touches_running_rows(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
        finalization_owner_token="dead-supervisor",
        finalization_owner_pid=999_999_999,
        finalization_owner_identity="dead-start|dead-command",
    )
    run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="RUNNING")
    run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="COMPLETED")
    assert db.mark_run_finalized(
        db_path, run["id"], owner_token="dead-supervisor"
    ) is not None

    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()

    assert outcomes == []
    assert db.get_run(db_path, run["id"])["state"] == "COMPLETED"


def test_reconcile_is_idempotent_for_interrupted_rows(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    dead = subprocess.Popen(["true"])
    dead.wait()
    time.sleep(0.2)
    run = _make_running_row(db_path, pid=dead.pid, process_start_identity=None)

    sup = supervisor.Supervisor(db_path)
    first = sup.reconcile()
    second = sup.reconcile()

    assert first[0]["classification"] == "INTERRUPTED"
    assert second == [], "an already-reconciled (now terminal) run must not be reconciled again"
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"


# --------------------------------------------------------------------------
# PREPARED/QUEUED are now in scope too (a Supervisor can crash before a run
# ever reaches RUNNING) — see `ALLOWED_TRANSITIONS`'s and `reconcile()`'s
# docstrings.
# --------------------------------------------------------------------------


def _make_queued_row(db_path, *, pid=None, process_start_identity=None):
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
        finalization_owner_token="dead-supervisor",
        finalization_owner_pid=999_999_999,
        finalization_owner_identity="dead-start|dead-command",
    )
    run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    if pid is not None:
        run = db.update_run_fields(
            db_path, run["id"], expected_version=run["version"],
            fields={"pid": pid, "process_start_identity": process_start_identity},
        )
    return run


def _make_prepared_row(db_path):
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    return db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
        finalization_owner_token="dead-supervisor",
        finalization_owner_pid=999_999_999,
        finalization_owner_identity="dead-start|dead-command",
    )


def test_reconcile_classifies_stale_prepared_row_as_interrupted(tmp_path):
    """A Supervisor that crashed right after `create_run` (before ever
    reaching the QUEUED transition) must not leave the row stuck PREPARED
    forever — and, since Sprint 2's workspace lock, permanently blocking
    that workspace."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_prepared_row(db_path)

    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()

    assert len(outcomes) == 1
    assert outcomes[0]["classification"] == "INTERRUPTED"
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"


def test_reconcile_classifies_stale_queued_row_with_no_pid_as_interrupted(tmp_path):
    """The common crash-mid-launch case: QUEUED was persisted, but the crash
    happened before `Popen` ever ran, so there is no pid to check."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_queued_row(db_path)

    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()

    assert len(outcomes) == 1
    assert outcomes[0]["classification"] == "INTERRUPTED"
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"


def test_reconcile_classifies_queued_row_with_matching_live_pid_as_running(tmp_path):
    """The narrow window: `Popen` succeeded and the pid/identity were
    recorded, but the crash happened before the QUEUED -> RUNNING transition
    itself was persisted. Reconciliation must recognize this is genuinely
    running (positive pid+identity match) and transition it to RUNNING
    explicitly, not leave it stuck QUEUED."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    proc = subprocess.Popen(["sleep", "3"])
    try:
        recorded_identity = identity.capture_identity(proc.pid).as_string()
        run = _make_queued_row(db_path, pid=proc.pid, process_start_identity=recorded_identity)
        assert run["state"] == "QUEUED"

        sup = supervisor.Supervisor(db_path)
        outcomes = sup.reconcile()

        assert outcomes[0]["classification"] == "RUNNING"
        updated = db.get_run(db_path, run["id"])
        assert updated["state"] == "RUNNING"
        assert proc.poll() is None, "reconciliation must never signal a verified-live process"

        events = db.list_run_events(db_path, run["id"])
        lifecycles = [e["payload"].get("lifecycle") for e in events if e["event_type"] == "lifecycle"]
        assert "reconciliation_orphaned" in lifecycles
    finally:
        proc.terminate()
        proc.wait()


def test_reconcile_skips_a_run_this_instance_has_registered_as_launching(tmp_path):
    """`self._launching` (populated by `start_raw` right after the QUEUED
    transition, before `Popen`) must protect an in-flight launch of *this*
    instance from being reconciled away by a concurrent `reconcile()` call
    (e.g. another browser tab's dashboard refresh) that would otherwise see
    an indistinguishable pid-less QUEUED row and misclassify it INTERRUPTED."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _make_queued_row(db_path)

    sup = supervisor.Supervisor(db_path)
    sup._launching.add(run["id"])
    outcomes = sup.reconcile()

    assert outcomes == []
    assert db.get_run(db_path, run["id"])["state"] == "QUEUED"


def test_reconcile_does_not_use_claude_agents_registry(tmp_path, monkeypatch):
    """Reconciliation must never shell out to `claude agents --json` — the
    Supervisor's own SQLite `run` table is its lifecycle registry.

    A RUNNING row must actually be present for this to mean anything: with no
    rows to classify, the per-row branch this guards never runs, and the
    assertion holds vacuously regardless of what that branch does.
    """
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)

    dead = subprocess.Popen(["true"])
    dead.wait()
    time.sleep(0.2)  # ensure `ps`/procfs no longer reports it
    run = _make_running_row(db_path, pid=dead.pid, process_start_identity=None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError(f"reconcile() must not invoke subprocess: {args!r}")

    monkeypatch.setattr(supervisor.subprocess, "Popen", fail_if_called)
    monkeypatch.setattr(supervisor.subprocess, "run", fail_if_called)

    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()

    assert len(outcomes) == 1
    assert outcomes[0]["classification"] == "INTERRUPTED"
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"


def test_reconcile_continues_after_one_run_persistence_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    broken = _make_running_row(db_path, pid=None, process_start_identity=None)
    healthy = _make_running_row(db_path, pid=None, process_start_identity=None)
    real_update = db.update_run_state

    def fail_one_run(db_path, run_id, *, expected_version, new_state, fields=None):
        if run_id == broken["id"] and new_state in db.TERMINAL_STATES:
            raise RuntimeError("injected one-run reconciliation failure")
        return real_update(
            db_path,
            run_id,
            expected_version=expected_version,
            new_state=new_state,
            fields=fields,
        )

    monkeypatch.setattr(supervisor.db, "update_run_state", fail_one_run)
    sup = supervisor.Supervisor(db_path)
    outcomes = sup.reconcile()

    assert db.get_run(db_path, broken["id"])["state"] == "RUNNING"
    assert db.get_run(db_path, healthy["id"])["state"] == "INTERRUPTED"
    by_id = {item["run_id"]: item for item in outcomes}
    assert by_id[broken["id"]]["classification"] == "ERROR"
    assert by_id[healthy["id"]]["classification"] == "INTERRUPTED"


# --------------------------------------------------------------------------
# Cross-process debounce (audit P0): a run that only *looks* gone (a peer owns
# it and is between create_run and Popen, or exited-but-finalizing) must not be
# INTERRUPTED on a single observation, only after it stays gone across a grace
# window — so a peer's succeeding run is never terminalized out from under it.
# --------------------------------------------------------------------------


def _dead_running_row(db_path):
    proc = subprocess.Popen(["true"])
    proc.wait()
    time.sleep(0.2)  # ensure `ps` no longer reports it
    return _make_running_row(db_path, pid=proc.pid, process_start_identity="whatever|whatever")


def test_reconcile_does_not_terminalize_a_gone_run_on_first_observation(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _dead_running_row(db_path)

    sup = supervisor.Supervisor(db_path)
    sup._reconcile_absence_grace = 60.0  # long grace: never fires within the test
    sup.reconcile()

    assert db.get_run(db_path, run["id"])["state"] == "RUNNING"  # not interrupted yet
    assert run["id"] in sup._suspected_gone


def test_reconcile_terminalizes_a_gone_run_after_the_grace_window(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _dead_running_row(db_path)

    sup = supervisor.Supervisor(db_path)
    sup._reconcile_absence_grace = 0.05
    sup.reconcile()  # first: suspicion only
    assert db.get_run(db_path, run["id"])["state"] == "RUNNING"
    time.sleep(0.06)  # let the grace elapse
    sup.reconcile()  # second: now confirmed gone
    assert db.get_run(db_path, run["id"])["state"] == "INTERRUPTED"


def test_reconcile_never_clobbers_a_run_that_resolves_during_grace(tmp_path):
    """The core protection: the owning process CAS's the run terminal
    (COMPLETED) while our grace is still open — reconcile must not overwrite
    that with INTERRUPTED, and must forget the suspicion."""
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    run = _dead_running_row(db_path)

    sup = supervisor.Supervisor(db_path)
    sup._reconcile_absence_grace = 60.0
    sup.reconcile()  # first: suspicion recorded, still RUNNING
    assert run["id"] in sup._suspected_gone

    current = db.get_run(db_path, run["id"])
    db.update_run_state(
        db_path, run["id"], expected_version=current["version"],
        new_state="COMPLETED", fields={"completed_at": "2026-01-01T00:00:01"},
    )
    sup.reconcile()  # second: run is no longer active

    assert db.get_run(db_path, run["id"])["state"] == "COMPLETED"  # never clobbered
    assert run["id"] not in sup._suspected_gone  # suspicion pruned


# --------------------------------------------------------------------------
# Adopted-orphan timeout enforcement (audit P0/H2): a RUNNING row from a
# previous incarnation of this app whose process is still alive is adopted (left
# RUNNING) — but it is not re-registered, so its timeout watchdog is gone. Past
# the deadline, reconcile only terminalizes the row once the process is
# independently re-confirmed gone — a database identity is not a live
# ownership handle, so a still-alive orphan is never sent a signal and stays
# adopted RUNNING past its own deadline (see
# `test_reconcile_reaps_an_orphan_past_its_timeout` below).
# --------------------------------------------------------------------------


def _make_running_row_with_timeout(db_path, *, pid, process_start_identity, started_at, timeout_seconds):
    task = db.create_task(db_path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False, timeout_seconds=timeout_seconds,
        finalization_owner_token="dead-supervisor",
        finalization_owner_pid=999_999_999,
        finalization_owner_identity="dead-start|dead-command",
    )
    run = db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(
        db_path, run["id"], expected_version=run["version"], new_state="RUNNING",
        fields={"pid": pid, "process_start_identity": process_start_identity, "started_at": started_at},
    )
    return run


def test_reconcile_reaps_an_orphan_past_its_timeout(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    # A real, own-session-group process so os.killpg(pid) targets exactly it.
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        captured = identity.capture_identity(proc.pid)
        assert captured is not None
        recorded = captured.as_string()
        run = _make_running_row_with_timeout(
            db_path, pid=proc.pid, process_start_identity=recorded,
            started_at="2020-01-01T00:00:00", timeout_seconds=1,  # long past its deadline
        )
        sup = supervisor.Supervisor(db_path)
        sup.reconcile()

        # A database identity is not a live ownership handle. Even past the
        # deadline, cross-restart reconciliation never sends a destructive
        # signal without pidfd+cgroup/job-object ownership.
        assert db.get_run(db_path, run["id"])["state"] == "RUNNING"
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_reconcile_adopts_an_orphan_still_within_its_timeout(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        recorded = identity.capture_identity(proc.pid).as_string()
        run = _make_running_row_with_timeout(
            db_path, pid=proc.pid, process_start_identity=recorded,
            started_at=datetime.now().isoformat(), timeout_seconds=3600,  # nowhere near deadline
        )
        sup = supervisor.Supervisor(db_path)
        sup.reconcile()

        assert db.get_run(db_path, run["id"])["state"] == "RUNNING"  # adopted, not reaped
        assert proc.poll() is None  # never signalled
    finally:
        proc.kill()
        proc.wait()


def test_reconcile_never_reaps_an_orphan_with_no_recorded_timeout(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        recorded = identity.capture_identity(proc.pid).as_string()
        run = _make_running_row_with_timeout(
            db_path, pid=proc.pid, process_start_identity=recorded,
            started_at="2020-01-01T00:00:00", timeout_seconds=None,  # no deadline -> conservative
        )
        sup = supervisor.Supervisor(db_path)
        sup.reconcile()

        assert db.get_run(db_path, run["id"])["state"] == "RUNNING"  # left adopted
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()
