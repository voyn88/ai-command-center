"""`run.finalized_at` — the marker that finalization finished, not that it began.

VOYN-W0-AICC-SRV-09-FINALIZED-AT.

`_supervise` commits the terminal run row, and only afterwards appends the
`process_exited` event, auto-commits whatever the agent left uncommitted, and
saves the report. It does that on a daemon thread, which interpreter shutdown
does not join. So between those two points every observer sees a finished run
whose report does not exist and whose work is not committed.

Measured over 20 runs on this branch, the window is a 6.1 ms median when the
working tree is clean and a **139 ms median, 152 ms maximum** when it is not —
because a run that changed the tree pays a real `git commit` inside the window,
and that commit is 133 ms of it. The work started from the small figure, and the
large one is the one that matters: the window is twenty times wider on exactly
the runs that produced something to lose, and against the 200 ms poll the CLI
uses that is not a rare race. The control test below is written around this
measurement rather than around the assumption.

The absence that mattered was not the flake, though, but the *predicate*. Before
this column there was no durable way to ask "is anything still finalizing?".
`Supervisor.wait_for_run` answers a version of it from an in-memory registry
private to the process that launched the run, which is no use to the operator
draining a cutover, to a backward mirror deciding the seam is quiet, or to a
readiness probe. Anything built on the terminal state alone was built on a fact
that becomes true too early.

The four properties below are the ones that make the marker worth having, and
each is written so that it fails if the marker were moved into the
`update_run_state` call that publishes the terminal state — which is the change
that would keep the column and destroy the guarantee.
"""

from __future__ import annotations

import os
import multiprocessing
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from command_center.runtime import db, identity, reports, supervisor

PROBE = Path(__file__).parent / "fixtures" / "finalization_kill_probe.py"

#: Wide enough that a 10 ms watcher cannot miss it, short enough that three
#: sequential runs stay well inside a normal test budget.
WIDEN_SECONDS = 2.0


def _run_probe(repo: Path, *, widen: float, poll: float) -> subprocess.CompletedProcess:
    """One supervised run in its own process, killed the moment it goes terminal."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["AICC_TEST_WIDEN_FINALIZATION_SECONDS"] = str(widen)
    # `python <script>` puts the *script's* directory on the path, not the
    # working directory, so the repository root has to be named explicitly.
    env["PYTHONPATH"] = os.pathsep.join([str(root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(PROBE), str(repo), str(poll)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _create_claimed_terminal_run(
    sup: supervisor.Supervisor, repo: Path, *, state: str = "COMPLETED"
) -> dict:
    task = db.create_task(
        sup.db_path, project="AIOS", title="claimed", task_type="implementation"
    )
    session = db.create_session(
        sup.db_path,
        task_id=task["id"],
        project="AIOS",
        repository_path=str(repo),
    )
    run = db.create_run(
        sup.db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        repository_path=str(repo),
        task_type="implementation",
        prompt="claimed",
        is_resume=False,
        command=["claude", "--print"],
        finalization_owner_token=sup._finalization_owner_token,
        finalization_owner_pid=sup._finalization_owner_pid,
        finalization_owner_identity=sup._finalization_owner_identity,
    )
    run = db.update_run_state(
        sup.db_path, run["id"], expected_version=run["version"], new_state="QUEUED"
    )
    run = db.update_run_state(
        sup.db_path, run["id"], expected_version=run["version"], new_state="RUNNING"
    )
    return db.update_run_state(
        sup.db_path,
        run["id"],
        expected_version=run["version"],
        new_state=state,
        fields={"completed_at": db.iso_now(), "exit_code": 0},
    )


def _replace_claim_with_dead_owner(
    sup: supervisor.Supervisor, run_id: str, *, token: str = "dead-owner-token"
) -> None:
    current = db.get_run_finalization_claim(sup.db_path, run_id)
    replaced = db.claim_run_finalization(
        sup.db_path,
        run_id,
        owner_token=token,
        owner_pid=999_999_999,
        owner_identity="dead-start|dead-command",
        expected_owner_token=current["owner_token"],
    )
    assert replaced is not None


def _fork_finalization_context_probe(
    inherited,
    db_path: str,
    cold_db_path: str,
    run_id: str,
    result_queue,
    fail_first_capture: bool,
) -> None:
    """Exercise both rejected inherited state and fresh child authority."""
    from command_center.runtime import identity as child_identity
    from command_center.runtime import supervisor as child_supervisor

    rejected: list[str] = []
    inherited_calls = (
        ("reconcile", lambda: inherited.reconcile()),
        ("cancel", lambda: inherited.cancel("missing", confirmed=False)),
        ("wait", lambda: inherited.wait_for_run("missing", timeout=0)),
        ("active", inherited.active_run_ids),
        ("finalizer", lambda: inherited._finalizer.final_result_payload("missing")),
    )
    for name, call in inherited_calls:
        try:
            call()
        except (child_supervisor.SupervisorError, RuntimeError):
            rejected.append(name)

    capture_error = None
    cold_version_before = db.current_schema_version(Path(cold_db_path))
    cold_version_after_failed_capture = cold_version_before
    original_capture = child_supervisor.identity.capture_identity
    if fail_first_capture:
        child_supervisor.identity.capture_identity = lambda _pid: None
        try:
            child_supervisor.Supervisor(db_path=Path(db_path))
        except child_supervisor.SupervisorError as exc:
            capture_error = str(exc)
        finally:
            child_supervisor.identity.capture_identity = original_capture
        cold_version_after_failed_capture = db.current_schema_version(
            Path(cold_db_path)
        )

    first = child_supervisor.Supervisor(db_path=Path(db_path))
    second = child_supervisor.Supervisor(db_path=Path(db_path))
    child_supervisor.Supervisor(db_path=Path(cold_db_path))
    first.reconcile()
    observed = child_identity.query_identity(os.getpid())
    result_queue.put(
        {
            "pid": os.getpid(),
            "global_pid": child_supervisor._SYSTEM_PROCESS_PID,
            "global_token": child_supervisor._SYSTEM_FINALIZATION_OWNER_TOKEN,
            "first_token": first._finalization_owner_token,
            "second_token": second._finalization_owner_token,
            "first_owner_pid": first._finalization_owner_pid,
            "identity_status": observed.status.value,
            "identity_matches": bool(
                observed.identity
                and observed.identity.as_string() == first._finalization_owner_identity
            ),
            "rejected": rejected,
            "capture_error": capture_error,
            "cold_version_before": cold_version_before,
            "cold_version_after_failed_capture": cold_version_after_failed_capture,
            "cold_version_after_retry": db.current_schema_version(Path(cold_db_path)),
            "finalized_at": db.get_run(Path(db_path), run_id)["finalized_at"],
        }
    )


# ---------------------------------------------------------------------------
# The shared widener, whose whole value is that it can prove it ran
# ---------------------------------------------------------------------------


def test_the_widener_counts_only_terminal_writes_and_can_prove_it_fired(tmp_path):
    """`fired()` must answer "did the sleep happen", not "was the patch installed".

    Three earlier versions of this fixture answered the second question and were
    green while measuring nothing — a `sitecustomize` that could not import
    `command_center` yet, then a tautological identity check. Review made the
    widener inert by renaming the keyword it reads and the suite passed anyway.
    Extracting it into a shared module is the moment that guarantee is most
    likely to be lost, so it is pinned here.
    """
    script = textwrap.dedent(
        """
        from tests.fixtures import finalization_window
        from command_center.runtime import db

        assert finalization_window.fired() == 0, "counted a sleep before widening"
        finalization_window.widen(0.01)
        assert finalization_window.fired() == 0, "installing the patch counted as firing"
        print("INSTALLED", db.update_run_state.__name__)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # Installing must not be mistaken for firing — the tautology that let a
    # deliberately inert widener pass three times out of three.
    assert "INSTALLED update_run_state_then_hold" in result.stdout, result.stdout


def test_the_widener_fires_on_a_real_terminal_write(
    git_repo, configure_project_repo, fake_claude
):
    """And the counter moves when a real run goes terminal.

    The other half of the guarantee: the patch is reached by the code path that
    matters, so `fired()` reporting zero after a run is genuine evidence that
    nothing was widened.
    """
    from tests.fixtures import finalization_window

    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()

    before = finalization_window.fired()
    # Restored explicitly: `widen` patches the shared `db` module, and leaving
    # that in place would make every later test in this worker sleep on its own
    # terminal writes.
    unpatched = db.update_run_state
    finalization_window.widen(0.001)
    try:
        run = sup.start_raw(
            project="AIOS",
            repository_path=str(git_repo),
            task_type="implementation",
            prompt="do a thing",
            confirmed=True,
        )
        sup.wait_for_run(run["id"], timeout=30)
    finally:
        db.update_run_state = unpatched

    assert finalization_window.fired() > before, (
        "no terminal write went through the widener, so it would measure nothing"
    )


# ---------------------------------------------------------------------------
# The ordering — the whole point of the column
# ---------------------------------------------------------------------------


def test_the_marker_is_written_after_the_report_not_with_the_state(
    git_repo, configure_project_repo, fake_claude
):
    """Observed at the report write, not inferred from the final row.

    Asserting only that a completed run ends up with both a report and a marker
    would pass just as well if the marker were set in the same `UPDATE` as the
    terminal state — the bug this task exists to remove. So the check is taken
    *while* the report is being persisted: at that moment the marker must still
    be empty, because it is a consequence of this write and not an announcement
    of it.
    """
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()

    seen_at_report_time: list[object] = []
    original_create_report = db.create_report

    def recording_create_report(db_path, run_id, path):
        seen_at_report_time.append(db.get_run(db_path, run_id)["finalized_at"])
        return original_create_report(db_path, run_id, path)

    db.create_report = recording_create_report
    try:
        run = sup.start_raw(
            project="AIOS",
            repository_path=str(git_repo),
            task_type="implementation",
            prompt="do a thing",
            confirmed=True,
        )
        final = sup.wait_for_run(run["id"], timeout=30)
    finally:
        db.create_report = original_create_report

    assert final["state"] == "COMPLETED"
    assert seen_at_report_time == [None], (
        "the marker was already set while the report was still being written — "
        "it is announcing finalization rather than recording it"
    )
    assert db.get_run(sup.db_path, run["id"])["finalized_at"] is not None
    assert db.get_report(sup.db_path, run["id"]) is not None


def test_the_marker_is_write_once(git_repo, configure_project_repo, fake_claude):
    """A retry or a recovery pass must not move the recorded moment.

    `finalized_at` is evidence about when a run's output became durable. A
    second write would quietly restamp it with the time of the retry, turning
    the one fact it carries into the time somebody last looked.
    """
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()
    run = sup.start_raw(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="implementation",
        prompt="do a thing",
        confirmed=True,
    )
    sup.wait_for_run(run["id"], timeout=30)

    first = db.get_run(sup.db_path, run["id"])["finalized_at"]
    assert first is not None
    assert db.mark_run_finalized(
        sup.db_path,
        run["id"],
        owner_token=sup._finalization_owner_token,
    ) is None, "a second mark was accepted"
    assert db.get_run(sup.db_path, run["id"])["finalized_at"] == first

    # And it leaves the concurrency protocol alone: a marker is bookkeeping, and
    # bumping `version` would make an unrelated compare-and-set holder lose its
    # update because of a write that changed no domain field.
    before = db.get_run(sup.db_path, run["id"])
    db.mark_run_finalized(
        sup.db_path,
        run["id"],
        owner_token=sup._finalization_owner_token,
    )
    assert db.get_run(sup.db_path, run["id"])["version"] == before["version"]


def test_same_process_supervisors_share_finalization_ownership(
    git_repo, configure_project_repo
):
    configure_project_repo("AIOS", git_repo)
    owner = supervisor.Supervisor()
    run = _create_claimed_terminal_run(owner, git_repo)
    intruder = supervisor.Supervisor(db_path=owner.db_path)

    observed = intruder._persist_reconciliation_state(
        run["id"], classification="INTERRUPTED"
    )

    assert observed["state"] == "COMPLETED"
    assert owner._finalization_owner_token == intruder._finalization_owner_token
    assert observed["finalized_at"] is not None
    assert db.get_report(owner.db_path, run["id"]) is not None
    claim = db.get_run_finalization_claim(owner.db_path, run["id"])
    assert claim["owner_token"] == owner._finalization_owner_token
    assert claim["completed_at"] is not None
    assert intruder._mark_finalized(run["id"]) is True


def test_same_process_reconcile_recovers_process_owned_terminal_failure(
    git_repo, configure_project_repo
):
    configure_project_repo("AIOS", git_repo)
    owner = supervisor.Supervisor()
    run = _create_claimed_terminal_run(owner, git_repo)
    with supervisor._PROCESS_OWNED_RUNS_GUARD:
        supervisor._PROCESS_OWNED_RUNS.add(run["id"])

    recovery = supervisor.Supervisor(db_path=owner.db_path)
    outcomes = recovery.reconcile()

    assert sum(item["run_id"] == run["id"] for item in outcomes) == 1
    assert db.get_run(owner.db_path, run["id"])["finalized_at"] is not None
    assert db.get_report(owner.db_path, run["id"]) is not None
    with supervisor._PROCESS_OWNED_RUNS_GUARD:
        assert run["id"] not in supervisor._PROCESS_OWNED_RUNS


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="fork ownership semantics are POSIX-only",
)
@pytest.mark.parametrize("fail_first_capture", [False, True])
def test_fork_drops_parent_authority_and_requires_fresh_child_supervisor(
    git_repo, configure_project_repo, fail_first_capture, tmp_path, monkeypatch
):
    configure_project_repo("AIOS", git_repo)
    parent = supervisor.Supervisor()
    run = _create_claimed_terminal_run(parent, git_repo)
    parent_token = parent._finalization_owner_token
    parent_pid = os.getpid()
    cold_db_path = tmp_path / "cold-v24.db"
    current_migrations = list(db.MIGRATIONS)
    with monkeypatch.context() as pre_claim:
        pre_claim.setattr(db, "MIGRATIONS", current_migrations[:-1])
        pre_claim.setattr(db, "SCHEMA_VERSION", 24)
        db.migrate(cold_db_path)
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    child = context.Process(
        target=_fork_finalization_context_probe,
        args=(
            parent,
            str(parent.db_path),
            str(cold_db_path),
            run["id"],
            result_queue,
            fail_first_capture,
        ),
    )

    child.start()
    child.join(timeout=30)

    assert child.exitcode == 0
    result = result_queue.get(timeout=5)
    assert set(result["rejected"]) == {
        "reconcile",
        "cancel",
        "wait",
        "active",
        "finalizer",
    }
    assert result["pid"] != parent_pid
    assert result["global_pid"] == result["pid"]
    assert result["first_owner_pid"] == result["pid"]
    assert result["first_token"] == result["second_token"] == result["global_token"]
    assert result["first_token"] != parent_token
    assert result["identity_status"] == identity.ProcessQueryStatus.LIVE.value
    assert result["identity_matches"] is True
    assert bool(result["capture_error"]) is fail_first_capture
    assert result["cold_version_before"] == 24
    assert result["cold_version_after_failed_capture"] == 24
    assert result["cold_version_after_retry"] == 25
    # A fresh child sees the exact live parent identity and cannot steal its
    # terminal-unfinalized claim merely because its own local PID namespace
    # differs.
    assert result["finalized_at"] is None
    assert db.get_run(parent.db_path, run["id"])["finalized_at"] is None


def test_dead_supervisor_claim_is_taken_over_before_terminal_recovery(
    git_repo, configure_project_repo
):
    configure_project_repo("AIOS", git_repo)
    original = supervisor.Supervisor()
    run = _create_claimed_terminal_run(original, git_repo)
    dead_token = "dead-owner-token"
    replaced = db.claim_run_finalization(
        original.db_path,
        run["id"],
        owner_token=dead_token,
        owner_pid=999_999_999,
        owner_identity="999999999|dead|dead",
        expected_owner_token=original._finalization_owner_token,
    )
    assert replaced is not None

    recovery = supervisor.Supervisor(db_path=original.db_path)
    outcomes = recovery.reconcile()

    final = db.get_run(original.db_path, run["id"])
    assert final["finalized_at"] is not None
    assert db.get_report(original.db_path, run["id"]) is not None
    claim = db.get_run_finalization_claim(original.db_path, run["id"])
    assert claim["owner_token"] == recovery._finalization_owner_token
    assert claim["completed_at"] is not None
    assert any(item["run_id"] == run["id"] for item in outcomes)


@pytest.mark.parametrize(
    ("query", "takeover"),
    [
        (identity.ProcessQuery(identity.ProcessQueryStatus.UNKNOWN), False),
        (identity.ProcessQuery(identity.ProcessQueryStatus.LIVE), False),
        (
            identity.ProcessQuery(
                identity.ProcessQueryStatus.LIVE,
                identity.ProcessIdentity(4242, "old-start", "old-command"),
            ),
            False,
        ),
        (
            identity.ProcessQuery(
                identity.ProcessQueryStatus.LIVE,
                identity.ProcessIdentity(4242, "old-start", "changed-command"),
            ),
            False,
        ),
        (
            identity.ProcessQuery(
                identity.ProcessQueryStatus.LIVE,
                identity.ProcessIdentity(4242, "new-start", "new-command"),
            ),
            True,
        ),
        (identity.ProcessQuery(identity.ProcessQueryStatus.ABSENT), True),
        (identity.ProcessQuery(identity.ProcessQueryStatus.ZOMBIE), True),
    ],
)
def test_recovery_claim_requires_full_owner_death_proof(
    git_repo, configure_project_repo, monkeypatch, query, takeover
):
    configure_project_repo("AIOS", git_repo)
    original = supervisor.Supervisor()
    run = _create_claimed_terminal_run(original, git_repo)
    replaced = db.claim_run_finalization(
        original.db_path,
        run["id"],
        owner_token="prior-owner",
        owner_pid=4242,
        owner_identity="old-start|old-command",
        expected_owner_token=original._finalization_owner_token,
    )
    assert replaced is not None
    recovery = supervisor.Supervisor(db_path=original.db_path)
    recovery._finalization_owner_token = "recovery-owner"
    recovery._finalizer.owner_token = "recovery-owner"
    monkeypatch.setattr(supervisor.identity, "query_identity", lambda _pid: query)

    assert recovery._acquire_recovery_finalization_claim(run["id"]) is takeover
    claim = db.get_run_finalization_claim(original.db_path, run["id"])
    assert claim["owner_token"] == ("recovery-owner" if takeover else "prior-owner")


def test_recovery_registers_report_after_file_only_crash(
    git_repo, configure_project_repo
):
    configure_project_repo("AIOS", git_repo)
    original = supervisor.Supervisor()
    run = _create_claimed_terminal_run(original, git_repo)
    events = db.list_run_events(original.db_path, run["id"], after_seq=0, limit=1_000_000)
    written = reports.save_report(run, events)
    assert written.is_file()
    assert db.get_report(original.db_path, run["id"]) is None
    _replace_claim_with_dead_owner(original, run["id"])

    recovery = supervisor.Supervisor(db_path=original.db_path)
    outcomes = recovery.reconcile()

    stored = db.get_report(original.db_path, run["id"])
    assert stored is not None
    assert reports.resolve_report_path(stored["path"]).is_file()
    assert db.get_run(original.db_path, run["id"])["finalized_at"] is not None
    assert sum(item["run_id"] == run["id"] for item in outcomes) == 1


def test_recovery_reuses_committed_report_row_without_rewriting(
    git_repo, configure_project_repo, monkeypatch
):
    configure_project_repo("AIOS", git_repo)
    original = supervisor.Supervisor()
    run = _create_claimed_terminal_run(original, git_repo)
    events = db.list_run_events(original.db_path, run["id"], after_seq=0, limit=1_000_000)
    written = reports.save_report(run, events)
    stored = db.create_report(
        original.db_path, run["id"], reports.stored_report_path(written)
    )
    _replace_claim_with_dead_owner(original, run["id"])
    monkeypatch.setattr(
        supervisor.reports,
        "save_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable report must not be rewritten")
        ),
    )

    recovery = supervisor.Supervisor(db_path=original.db_path)
    recovery.reconcile()

    assert db.get_report(original.db_path, run["id"]) == stored
    assert db.get_run(original.db_path, run["id"])["finalized_at"] is not None


def test_missing_report_file_keeps_claim_open_and_run_unfinalized(
    git_repo, configure_project_repo
):
    configure_project_repo("AIOS", git_repo)
    original = supervisor.Supervisor()
    run = _create_claimed_terminal_run(original, git_repo)
    missing = "reports/AIOS/intentionally-missing-report.md"
    resolved = reports.resolve_report_path(missing)
    if resolved is not None and resolved.exists():
        resolved.unlink()
    db.create_report(original.db_path, run["id"], missing)
    _replace_claim_with_dead_owner(original, run["id"])

    recovery = supervisor.Supervisor(db_path=original.db_path)
    recovery.reconcile()
    recovery.reconcile()

    final = db.get_run(original.db_path, run["id"])
    claim = db.get_run_finalization_claim(original.db_path, run["id"])
    assert final["finalized_at"] is None
    assert claim["completed_at"] is None
    assert db.get_report(original.db_path, run["id"])["path"] == missing


def test_marker_failure_is_recovered_without_repeating_report_or_commit(
    git_repo, configure_project_repo, monkeypatch
):
    configure_project_repo("AIOS", git_repo)
    owner = supervisor.Supervisor()
    run = _create_claimed_terminal_run(owner, git_repo)
    real_mark = db.mark_run_finalized
    monkeypatch.setattr(
        db,
        "mark_run_finalized",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("injected marker failure")
        ),
    )
    assert not owner._complete_owned_terminal_finalization(
        run["id"], exit_code=0, lifecycle="process_exited"
    )
    first_report = db.get_report(owner.db_path, run["id"])
    assert first_report is not None
    assert db.get_run(owner.db_path, run["id"])["finalized_at"] is None

    monkeypatch.setattr(db, "mark_run_finalized", real_mark)
    _replace_claim_with_dead_owner(owner, run["id"], token="failed-owner")
    recovery = supervisor.Supervisor(db_path=owner.db_path)
    monkeypatch.setattr(
        recovery,
        "_auto_commit_completed_work",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("auto-commit disposition must be reused")
        ),
    )
    recovery.reconcile()

    assert db.get_report(owner.db_path, run["id"]) == first_report
    assert db.get_run(owner.db_path, run["id"])["finalized_at"] is not None


def test_two_dead_owner_recoverers_have_one_cas_winner(
    git_repo, configure_project_repo, monkeypatch
):
    configure_project_repo("AIOS", git_repo)
    original = supervisor.Supervisor()
    run = _create_claimed_terminal_run(original, git_repo)
    _replace_claim_with_dead_owner(original, run["id"])
    first = supervisor.Supervisor(db_path=original.db_path)
    second = supervisor.Supervisor(db_path=original.db_path)
    for contender, token in ((first, "recoverer-a"), (second, "recoverer-b")):
        contender._finalization_owner_token = token
        contender._finalizer.owner_token = token
    barrier = threading.Barrier(2)
    real_claim = db.claim_run_finalization

    def synchronized_claim(*args, **kwargs):
        if kwargs.get("expected_owner_token") == "dead-owner-token":
            barrier.wait(timeout=5)
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(db, "claim_run_finalization", synchronized_claim)
    results: list[list[dict]] = []
    errors: list[BaseException] = []

    def recover(contender):
        try:
            results.append(contender.reconcile())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=recover, args=(first,)),
        threading.Thread(target=recover, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sum(item["run_id"] == run["id"] for batch in results for item in batch) == 1
    claim = db.get_run_finalization_claim(original.db_path, run["id"])
    assert claim["owner_token"] in {"recoverer-a", "recoverer-b"}
    assert claim["completed_at"] is not None
    assert db.get_report(original.db_path, run["id"]) is not None


@pytest.mark.parametrize(
    ("state", "failure_reason", "outcome", "classification", "disposition", "error_code"),
    [
        ("COMPLETED", None, "succeeded", "success", "succeeded", None),
        ("CANCELLED", None, "cancelled", "cancelled", "terminal", "cancelled"),
        (
            "FAILED",
            "authentication_failed",
            "failed",
            "authentication",
            "terminal",
            "authentication_failed",
        ),
    ],
)
def test_recovery_finishes_started_provider_attempt_once(
    git_repo,
    configure_project_repo,
    state,
    failure_reason,
    outcome,
    classification,
    disposition,
    error_code,
):
    configure_project_repo("AIOS", git_repo)
    original = supervisor.Supervisor()
    run = _create_claimed_terminal_run(original, git_repo, state=state)
    if failure_reason:
        run = db.update_run_fields(
            original.db_path,
            run["id"],
            expected_version=run["version"],
            fields={"failure_reason": failure_reason},
        )
    db.start_provider_attempt(
        original.db_path,
        run_id=run["id"],
        attempt_number=1,
        provider_id="claude_code",
        started_at=db.iso_now(),
    )
    _replace_claim_with_dead_owner(original, run["id"])

    recovery = supervisor.Supervisor(db_path=original.db_path)
    recovery.reconcile()
    first = db.list_provider_attempts(original.db_path, run["id"])
    recovery.reconcile()
    second = db.list_provider_attempts(original.db_path, run["id"])

    assert first == second
    assert len(first) == 1
    assert first[0]["outcome"] == outcome
    assert first[0]["classification"] == classification
    assert first[0]["disposition"] == disposition
    assert first[0]["error_code"] == error_code
    assert first[0]["completed_at"] is not None


def test_a_failed_run_is_finalized_too(git_repo, configure_project_repo, fake_claude):
    """Not only the happy path, because the predicate has to reach zero.

    A marker written only on COMPLETED would leave every failure parked in the
    "still finalizing" set forever, and a drain gate that never drains is worse
    than no gate — it is a gate operators learn to skip.
    """
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()
    run = sup.start_raw(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="implementation",
        prompt="do a thing",
        confirmed=True,
        # `fake_claude` honours this and exits nonzero.
    )
    sup.wait_for_run(run["id"], timeout=30)

    # Whatever the classification, the run must not be left unfinalized.
    row = db.get_run(sup.db_path, run["id"])
    assert row["state"] in db.TERMINAL_STATES
    assert row["finalized_at"] is not None
    assert db.count_unfinalized_runs(sup.db_path) == 0


def test_a_reconciled_orphan_is_finalized(git_repo, configure_project_repo):
    """The restart path, which has no report and no auto-commit to wait for.

    When the supervisor that owned a run is gone, startup reconciliation makes
    the last decision about it. That decision is the finalization, and a run
    left unmarked here would be indistinguishable from one whose supervisor is
    still working — on an install that has restarted even once, permanently.
    """
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()
    task = db.create_task(sup.db_path, project="AIOS", title="orphan", task_type="implementation")
    session = db.create_session(
        sup.db_path, task_id=task["id"], project="AIOS", repository_path=str(git_repo)
    )
    run = db.create_run(
        sup.db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        repository_path=str(git_repo),
        task_type="implementation",
        prompt="orphan",
        is_resume=False,
        command=["claude", "--print"],
        finalization_owner_token="dead-owner-token",
        finalization_owner_pid=999_999_999,
        finalization_owner_identity="999999999|dead|dead",
    )
    db.update_run_state(sup.db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    current = db.get_run(sup.db_path, run["id"])
    db.update_run_state(
        sup.db_path, run["id"], expected_version=current["version"], new_state="RUNNING"
    )

    assert db.count_unfinalized_runs(sup.db_path) == 0, "a RUNNING run is not pending finalization"

    sup._persist_reconciliation_state(run["id"], classification="INTERRUPTED")

    row = db.get_run(sup.db_path, run["id"])
    assert row["state"] == "INTERRUPTED"
    assert row["finalized_at"] is not None
    assert db.count_unfinalized_runs(sup.db_path) == 0


def test_reconciliation_retries_a_transient_finalization_marker_failure(
    git_repo, configure_project_repo, monkeypatch
):
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()
    task = db.create_task(
        sup.db_path, project="AIOS", title="orphan", task_type="implementation"
    )
    session = db.create_session(
        sup.db_path, task_id=task["id"], project="AIOS", repository_path=str(git_repo)
    )
    run = db.create_run(
        sup.db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        repository_path=str(git_repo),
        task_type="implementation",
        prompt="orphan",
        is_resume=False,
        command=["claude", "--print"],
        finalization_owner_token="dead-owner-token",
        finalization_owner_pid=999_999_999,
        finalization_owner_identity="999999999|dead|dead",
    )
    db.update_run_state(
        sup.db_path, run["id"], expected_version=run["version"], new_state="QUEUED"
    )
    current = db.get_run(sup.db_path, run["id"])
    db.update_run_state(
        sup.db_path, run["id"], expected_version=current["version"], new_state="RUNNING"
    )
    original_mark = db.mark_run_finalized
    failures_remaining = 1

    def fail_first_mark(*args, **kwargs):
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise sqlite3.OperationalError("injected finalization-marker failure")
        return original_mark(*args, **kwargs)

    monkeypatch.setattr(db, "mark_run_finalized", fail_first_mark)

    reconciled = sup._persist_reconciliation_state(
        run["id"], classification="INTERRUPTED"
    )

    assert reconciled["state"] == "INTERRUPTED"
    assert reconciled["finalized_at"]
    assert db.count_unfinalized_runs(sup.db_path) == 0


# ---------------------------------------------------------------------------
# The predicate, from a process that did not launch the run
# ---------------------------------------------------------------------------


def test_another_process_can_compute_the_unfinished_finalization_predicate(
    git_repo, configure_project_repo, fake_claude, tmp_path
):
    """The cutover operator's question, asked from outside.

    Deliberately evaluated in a *separate interpreter* that never imports
    `Supervisor`: the existing answer, `wait_for_run`, reads `self._active`, an
    in-memory dict belonging to the process that launched the run. An operator
    draining the seam has no handle on that object and no way to obtain one, so
    a predicate that needs it is not a predicate at all. This one needs only the
    database file.
    """
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()
    run = sup.start_raw(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="implementation",
        prompt="do a thing",
        confirmed=True,
    )
    sup.wait_for_run(run["id"], timeout=30)

    reader = textwrap.dedent(
        """
        import sys
        from command_center.runtime import db
        assert "command_center.runtime.supervisor" not in sys.modules
        path = db.resolve_db_path()
        print(db.count_unfinalized_runs(path))
        print([r["id"] for r in db.list_unfinalized_runs(path)])
        """
    )

    def ask() -> tuple[int, list[str]]:
        result = subprocess.run(
            [sys.executable, "-c", reader],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        count, ids = result.stdout.splitlines()
        return int(count), eval(ids)  # noqa: S307 - a list of ids this test just printed

    count, ids = ask()
    assert count == 0 and ids == [], "a fully finalized run still reads as pending"

    # Now the state a killed supervisor leaves: terminal, unfinalized. Written
    # directly rather than by racing a real one, because this test is about
    # whether the predicate *sees* that state, and the next one is about whether
    # a real kill produces it.
    with db.connect(sup.db_path) as conn:
        conn.execute("UPDATE run SET finalized_at = NULL WHERE id = ?", (run["id"],))
        conn.commit()

    count, ids = ask()
    assert count == 1 and ids == [run["id"]], (
        "a terminal run with no marker is invisible to the predicate"
    )


# ---------------------------------------------------------------------------
# A kill inside the window
# ---------------------------------------------------------------------------


@pytest.mark.serial  # real subprocess killed on a DB-observed edge; flaky when xdist saturates all cores
@pytest.mark.skipif(os.name != "posix", reason="SIGKILL semantics are POSIX")
def test_a_kill_inside_the_window_leaves_the_run_visibly_unfinalized(
    git_repo, configure_project_repo
):
    """The failure the marker converts from silent to detectable.

    Before the column, a supervisor killed here left a run reading COMPLETED
    with no report and the agent's work uncommitted — indistinguishable, to
    every reader, from a run that finished cleanly. Now the same kill leaves it
    terminal *and unfinalized*, which is a recoverable statement.

    The window is widened for this test (see the probe). Nothing about the order
    of operations changes; the gap between two of them is made large enough to
    aim at, because a 2.5 ms target cannot be hit on purpose and a test that
    tried would be measuring the scheduler.
    """
    configure_project_repo("AIOS", git_repo)
    db_path = db.resolve_db_path()

    result = _run_probe(git_repo, widen=WIDEN_SECONDS, poll=0.01)

    assert result.returncode == -signal.SIGKILL, (
        f"the probe was not killed (rc={result.returncode}): {result.stdout}{result.stderr}"
    )
    assert "SURVIVED" not in result.stdout
    run_id = result.stdout.splitlines()[0].strip()

    row = db.get_run(db_path, run_id)
    assert row is not None
    assert row["state"] in db.TERMINAL_STATES, "the kill landed before the state was committed"
    assert row["finalized_at"] is None, (
        "the run reads finalized although its supervisor died before writing the report"
    )
    assert db.get_report(db_path, run_id) is None, (
        "the report exists, so the kill did not land inside the window at all "
        "and this test proves nothing"
    )

    # And the whole reason for the column: the state alone says 'finished'.
    assert db.count_unfinalized_runs(db_path) == 1


@pytest.mark.serial  # real subprocess killed on a DB-observed edge; flaky when xdist saturates all cores
@pytest.mark.skipif(os.name != "posix", reason="SIGKILL semantics are POSIX")
def test_without_the_widening_the_marker_and_the_report_never_disagree(
    git_repo, configure_project_repo
):
    """The control, without which the test above proves only that SIGKILL works.

    Same probe, same self-kill on the same trigger — "terminal and not yet
    finalized" — with the widening removed, so the window is whatever it really
    is on this machine.

    This test was first written to assert that the unwidened window is too
    narrow to hit. Measurement falsified that, and the assertion was changed
    rather than the measurement: at 139 ms median for a run that changed the
    working tree, a 200 ms watcher lands inside the window most of the time, and
    20 of 20 trials were killed there. The window is not a rare race; it only
    looked like one when measured on runs with nothing to commit.

    So what the widening buys is determinism, not reachability, and what this
    control pins is the ONE-DIRECTIONAL invariant that actually holds on every
    schedule: `finalized_at` set with no report is the one outcome that must
    never occur, because it is precisely the outcome that made the original
    loss invisible. The reverse -- a report already written but `finalized_at`
    still NULL -- is not a defect; it is the architecturally correct shape of
    an abnormal termination caught mid-way (VOYN-W0-AICC-FINALIZED-AT-REPORT-
    CONTRACT, found live 2026-08-23 breaking every PR's required gate): the
    report is durable evidence the run produced a result, `finalized_at` is a
    SEPARATE, later marker for "the whole finalization sequence completed,"
    and a kill between the two leaves a real, recoverable, non-corrupt state
    -- exactly what `db.count_unfinalized_runs` and its companion row-lister
    exist to surface to an operator, not a state this test should treat as
    indistinguishable from data loss. The bidirectional `finalized ==
    has_report` this test used to assert was strictly stronger than the
    contract the code actually implements or needs to.
    """
    configure_project_repo("AIOS", git_repo)
    db_path = db.resolve_db_path()

    result = _run_probe(git_repo, widen=0, poll=0.2)

    run_id = result.stdout.splitlines()[0].strip()
    row = db.get_run(db_path, run_id)
    assert row is not None and row["state"] in db.TERMINAL_STATES

    finalized = row["finalized_at"] is not None
    has_report = db.get_report(db_path, run_id) is not None
    if finalized:
        assert has_report, (
            "finalized_at is set but no report exists -- this is the one "
            "outcome that must never occur, the original data-loss defect "
            f"this test exists to catch (finalized_at={row['finalized_at']!r})"
        )
    # report-without-finalized is the recoverable in-flight shape (see
    # docstring); count_unfinalized_runs must still see it as needing
    # attention regardless of which side of that shape it landed on.
    assert db.count_unfinalized_runs(db_path) == (0 if finalized else 1)

    if result.returncode == 0:
        assert "SURVIVED" in result.stdout, result.stdout
        assert finalized, "the probe reported SURVIVED without a marker"
        # The window as the probe measured it from inside, which is the only
        # part of the elapsed time that is about this code rather than about
        # interpreter start-up and whatever else the runner is doing. It must be
        # nowhere near the widened value, or the harness passed the widening
        # through and this is not a control.
        (window_ms,) = [
            float(line.split()[1])
            for line in result.stdout.splitlines()
            if line.startswith("WINDOW_MS")
        ]
        assert window_ms < WIDEN_SECONDS * 1000 / 2, (
            f"the unwidened window measured {window_ms:.1f} ms, close to the "
            f"widened {WIDEN_SECONDS * 1000:.0f} ms — the control is widened too"
        )
    else:
        # The common outcome, as it turns out: the real window is wide enough to
        # hit without any help. Not a failure — it is the defect this task
        # exists to make visible, reproduced at its true width, and the
        # assertions above already proved it stayed visible.
        assert result.returncode == -signal.SIGKILL, result.stderr
        assert not finalized
