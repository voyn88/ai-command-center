"""F1 regression coverage for `scripts/execution_center_debug.py`, exercised
as a genuinely separate OS process (not monkeypatched in-process) — this is
the only way to actually prove a CLI's process-lifetime behavior, since the
whole defect being fixed here was specific to what happens when *this*
Python process exits.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from command_center import project_config
from command_center.runtime import db, identity, supervisor

ROOT = Path(__file__).resolve().parent.parent
CLI_SCRIPT = ROOT / "scripts" / "execution_center_debug.py"
FAKE_CLAUDE_SCRIPT = Path(__file__).parent / "fixtures" / "fake_claude.py"
FAKE_CLAUDE_TREE_SCRIPT = Path(__file__).parent / "fixtures" / "fake_claude_tree.py"


def _extract_last_json_object(stdout: str) -> dict:
    """`_print()` in the CLI always renders a top-level dict starting with a
    lone `{` line (via `json.dumps(..., indent=2)`) — find the last one."""
    lines = stdout.splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == "{":
            start = i
            break
    assert start is not None, f"no JSON object found in CLI output:\n{stdout}"
    return json.loads("\n".join(lines[start:]))


def _cli_env(**overrides) -> dict:
    """Inherits `AICC_DATA_DIR` from this test process's own env (already isolated by
    `tests/conftest.py`), and additionally sets `AICC_REPORTS_ROOT` — `AICC_DATA_DIR`
    alone is not enough, because `command_center.runtime.reports.REPORTS_ROOT` is not
    derived from it (reports live at `<repo>/reports/`, not under `data/`; see that
    module's comment). Without this, every subprocess launched by this file's tests
    wrote a real report into the developer's actual `reports/AIOS/` directory on every
    `pytest` run — the CLI subprocess re-imports `reports.py` fresh, so the in-process
    `isolated_reports_dir` monkeypatch in conftest.py can never reach it."""
    env = dict(os.environ)
    env["AICC_CLAUDE_BINARY"] = str(FAKE_CLAUDE_SCRIPT)
    if "AICC_DATA_DIR" in env:
        env["AICC_REPORTS_ROOT"] = str(Path(env["AICC_DATA_DIR"]) / "reports")
    # Same default as `tests/conftest.py`'s `fake_claude` fixture: a plain
    # "implementation"-type fake run genuinely touches the working tree, so
    # `runtime.outcome.classify_process_result` classifies it `COMPLETED`
    # rather than `INCOMPLETE` (`REQUIRES_CHANGES_TASK_TYPES` + an unchanged
    # tree). A test exercising the unchanged-tree path overrides this back
    # to `""` via `overrides`.
    env["FAKE_CLAUDE_TOUCH_FILE"] = "fake_claude_default_touch.txt"
    env.update(overrides)
    return env


@pytest.fixture
def configured_repo(git_repo):
    """Points the real (isolated, per-conftest AICC_DATA_DIR) project_config
    at `git_repo` for project AIOS — the CLI subprocess inherits the same
    AICC_DATA_DIR from this test process's environment."""
    project_config.save_repository_path("AIOS", str(git_repo))
    yield git_repo
    project_config.save_repository_path("AIOS", None)


def test_offline_finalization_cutover_recovers_v24_terminal_crash_row(
    configured_repo, monkeypatch
):
    path = db.resolve_db_path()
    current_migrations = list(db.MIGRATIONS)
    with monkeypatch.context() as pre_claim:
        pre_claim.setattr(db, "MIGRATIONS", current_migrations[:-1])
        pre_claim.setattr(db, "SCHEMA_VERSION", 24)
        db.migrate(path)
        task = db.create_task(
            path, project="AIOS", title="legacy crash", task_type="implementation"
        )
        session = db.create_session(
            path,
            task_id=task["id"],
            project="AIOS",
            repository_path=str(configured_repo),
        )
        run = db.create_run(
            path,
            session_id=session["id"],
            task_id=task["id"],
            project="AIOS",
            repository_path=str(configured_repo),
            task_type="implementation",
            prompt="legacy terminal crash",
            is_resume=False,
            command=["claude", "--print"],
        )
        with db.connect(path) as conn:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE run SET state = 'COMPLETED', completed_at = ?, "
                    "exit_code = 0 WHERE id = ?",
                    (db.iso_now(), run["id"]),
                )

    refused = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "offline-finalization-cutover"],
        capture_output=True,
        text=True,
        timeout=30,
        env=_cli_env(),
    )
    assert refused.returncode == 7
    assert "CUTOVER REFUSED" in refused.stderr
    assert db.current_schema_version(path) == 24
    assert not supervisor.offline_cutover_fence_path(path).exists()

    result = subprocess.run(
        [
            sys.executable,
            str(CLI_SCRIPT),
            "offline-finalization-cutover",
            "--confirm-offline",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=_cli_env(AICC_COMPLETION_AUTOPILOT="1"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    evidence = _extract_last_json_object(result.stdout)
    assert evidence["schema_version"] == 25
    assert evidence["claims_seeded"] == 1
    assert evidence["unfinalized_remaining"] == 0
    recovered = db.get_run(path, run["id"])
    assert recovered["finalized_at"] is not None
    claim = db.get_run_finalization_claim(path, run["id"])
    assert claim["completed_at"] is not None
    assert not supervisor.offline_cutover_fence_path(path).exists()


def test_launch_blocks_until_terminal_state_and_persists_final_events(configured_repo):
    """1: blocks until terminal state. 2: final stream/result events are
    persisted before the CLI process exits."""
    env = _cli_env(FAKE_CLAUDE_DELAY="0.3")  # ~4 lines * 0.3s so a premature return would be detectable
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    elapsed = time.monotonic() - started

    assert elapsed >= 1.0, "the CLI must block for the real duration of the run, not return right after Popen"
    assert result.returncode == 0, result.stdout + result.stderr

    final = _extract_last_json_object(result.stdout)
    assert final["state"] == "COMPLETED"

    # The final `result` stream-json event must have been persisted — not
    # just the initial `process_started` lifecycle event (the exact bug: an
    # earlier version of this CLI left only that one event behind).
    events = db.list_run_events(db.resolve_db_path(), final["id"])
    event_types = {e["event_type"] for e in events}
    assert "result" in event_types
    assert "process_exited" in {e["payload"].get("lifecycle") for e in events if e["event_type"] == "lifecycle"}



def _wait_until_running(timeout: float = 20.0) -> dict:
    """Block until the CLI subprocess has a run with a live pid, or fail loudly.

    Replaces `time.sleep(1.0)  # let it launch and enter the polling loop`,
    which is a guess about scheduling rather than a wait for anything. Under
    `-n auto` the guess is sometimes wrong: the signal arrives before the CLI
    is in its loop, the run never reaches a terminal state through the
    cancellation path, and the assertion fails on absent output rather than on
    behaviour. That cost two CI reruns in one day, both of them ten minutes.

    The condition is the one the test actually depends on — a run row exists
    and carries a pid — read from the same `AICC_DATA_DIR` the subprocess
    writes to. The timeout is a diagnostic, not a schedule: it says what was
    never observed instead of continuing into a confusing failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            runs = db.list_runs(db.resolve_db_path())
        except sqlite3.OperationalError:
            # The schema is created by the CLI subprocess, not by this process,
            # so "no such table: run" simply means it has not got there yet —
            # a state of the wait, not a failure of it.
            runs = []
        for run in runs:
            # `first_output_at` rather than merely RUNNING: the row appears the
            # moment the supervisor inserts it, which is before the CLI is
            # reading output and before it can act on a signal. Waiting for the
            # first streamed line is waiting for the loop this test signals
            # into — CI proved the weaker condition insufficient, failing with
            # "no JSON object found" while the local machine passed.
            if run.get("pid") and run.get("state") == "RUNNING" and run.get("first_output_at"):
                return run
        time.sleep(0.05)
    raise AssertionError(
        f"no RUNNING run with a pid appeared within {timeout}s — the CLI never "
        "reached its foreground polling loop, so signalling it proves nothing"
    )


def test_no_orphan_process_survives_normal_cli_exit(configured_repo):
    """3: no child (or grandchild) process survives CLI exit — normal
    completion path."""
    env = _cli_env()
    result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    final = _extract_last_json_object(result.stdout)
    pid = final["pid"]
    assert pid is not None
    time.sleep(0.3)
    assert identity.process_exists(pid) is False


def test_ctrl_c_triggers_confirmed_cancellation_and_reaches_terminal_state(configured_repo):
    """4: Ctrl+C/explicit foreground cancellation reaches a terminal Run
    state, and does not merely kill the CLI process while abandoning the
    child."""
    env = _cli_env(FAKE_CLAUDE_EXTRA_SLEEP="30")
    proc = subprocess.Popen(
        [sys.executable, str(CLI_SCRIPT), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        _wait_until_running()
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    assert proc.returncode != 0, f"Ctrl+C must not report success\nstdout={stdout}\nstderr={stderr}"

    final = _extract_last_json_object(stdout)
    assert final["state"] == "CANCELLED"

    pid = final["pid"]
    assert pid is not None
    time.sleep(0.3)
    assert identity.process_exists(pid) is False, "the underlying claude process must not survive Ctrl+C either"


@pytest.mark.serial  # spawns a real 3-level process tree on a deadline; times out under xdist CPU load
def test_no_orphan_at_any_level_when_cancelled_via_ctrl_c(configured_repo):
    """3+4 combined, using the parent->child->grandchild fixture: Ctrl+C
    during `launch` must clean up the entire process tree, not just the
    top-level process the Supervisor directly `Popen`'d."""
    pidfile_base = str(configured_repo / "tree_pids")
    env = _cli_env()
    env["AICC_CLAUDE_BINARY"] = str(FAKE_CLAUDE_TREE_SCRIPT)
    env["FAKE_CLAUDE_TREE_PIDFILE"] = pidfile_base

    proc = subprocess.Popen(
        [sys.executable, str(CLI_SCRIPT), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        deadline = time.monotonic() + 10
        pids = {}
        identities = {}
        while time.monotonic() < deadline:
            if all(os.path.exists(f"{pidfile_base}.{role}") for role in ("parent", "child", "grandchild")):
                for role in ("parent", "child", "grandchild"):
                    pid = int(Path(f"{pidfile_base}.{role}").read_text().strip())
                    process_identity = identity.capture_identity(pid)
                    if process_identity is None:
                        break
                    pids[role] = pid
                    identities[role] = process_identity.as_string()
                if len(identities) != 3:
                    pids.clear()
                    identities.clear()
                    time.sleep(0.05)
                    continue
                break
            time.sleep(0.05)
        assert len(pids) == 3, "process tree never came up"

        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    assert proc.returncode != 0

    # The process tree exits asynchronously after SIGINT — the grandchild in
    # particular may still be unwinding when `communicate()` returns. Poll
    # (bounded) for each pid to disappear rather than asserting on a single fixed
    # sleep, which raced under CI load and flaked. A genuinely orphaned process
    # still fails: it simply never disappears before the deadline.
    deadline = time.monotonic() + 10
    def original_process_is_gone(pid: int, recorded_identity: str) -> bool:
        query = identity.query_identity(pid)
        if query.status in {
            identity.ProcessQueryStatus.ABSENT,
            identity.ProcessQueryStatus.ZOMBIE,
        }:
            return True
        if query.status is identity.ProcessQueryStatus.LIVE and query.identity is not None:
            return query.identity.as_string() != recorded_identity
        return False

    for role, pid in pids.items():
        recorded_identity = identities[role]
        while not original_process_is_gone(pid, recorded_identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert original_process_is_gone(pid, recorded_identity), (
            f"{role} (pid {pid}) must not survive Ctrl+C cancellation"
        )


def test_cli_does_not_advertise_a_cross_invocation_cancel_command():
    """5: the CLI never advertises a cross-invocation cancel operation it
    cannot perform — there is no `cancel` subcommand at all."""
    result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "cancel", "some-run-id", "--confirm"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "invalid choice: 'cancel'" in result.stderr

    help_result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--help"], capture_output=True, text=True, timeout=10
    )
    # The prose explaining *why* there's no cancel subcommand legitimately
    # mentions the word "cancel" — what must not exist is an actual `{...}`
    # subcommand choice named "cancel" in the usage/subparser listing.
    usage_block = help_result.stdout.split("positional arguments:")[1] if "positional arguments:" in help_result.stdout else help_result.stdout
    subcommand_line = next((line for line in usage_block.splitlines() if "{" in line and "}" in line), "")
    assert "cancel" not in subcommand_line, f"'cancel' must not be a listed subcommand: {subcommand_line!r}"


def test_cli_read_only_subcommands_are_safe_across_separate_invocations(configured_repo):
    """The read-only inspection subcommands, unlike a hypothetical
    cross-process cancel, are genuinely safe to call from a fresh invocation
    — they only read shared SQLite state."""
    env = _cli_env()
    launch_result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    final = _extract_last_json_object(launch_result.stdout)

    status_result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "run-status", final["id"]], capture_output=True, text=True, timeout=10,
    )
    assert status_result.returncode == 0
    status = _extract_last_json_object(status_result.stdout)
    assert status["state"] == "COMPLETED"

    events_result = subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "events", final["id"]], capture_output=True, text=True, timeout=10,
    )
    assert events_result.returncode == 0
    events = json.loads(events_result.stdout)
    assert any(e["event_type"] == "result" for e in events)


WIDEN_FINALIZATION_WRAPPER = Path(__file__).parent / "fixtures" / "widen_finalization.py"


@pytest.mark.serial  # deadline-sensitive subprocess run; the widened window is seconds, not minutes
def test_the_cli_waits_for_finalization_and_not_merely_for_a_terminal_row(configured_repo):
    """The guard the fix shipped without, and review said so.

    `test_launch_blocks_until_terminal_state_and_persists_final_events` above
    passes with `_await_finalization` deleted: the losing schedule is roughly
    one run in a hundred, so that test asserts the right things and almost
    never observes the case they are about.

    Here the window is widened deterministically — see
    `tests/fixtures/widen_finalization.py` — so the supervisor's
    daemon thread is still finalizing when the poll tick sees a terminal row.
    Without the wait, this CLI returns there, the interpreter exits without
    joining that daemon thread, and the run is left COMPLETED with no
    `process_exited` lifecycle event, no auto-commit and no report.

    Asserting on the *events* rather than on elapsed time: a timing assertion
    would pass for a CLI that merely slept, and the guarantee is durability,
    not duration.
    """
    env = _cli_env()
    env["AICC_TEST_WIDEN_FINALIZATION_SECONDS"] = "2.0"

    result = subprocess.run(
        [sys.executable, str(WIDEN_FINALIZATION_WRAPPER), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    final = _extract_last_json_object(result.stdout)
    events = db.list_run_events(db.resolve_db_path(), final["id"])
    lifecycle = {e["payload"].get("lifecycle") for e in events if e["event_type"] == "lifecycle"}
    assert "process_exited" in lifecycle, (
        "the CLI exited on a terminal run row while its supervisor was still "
        "finalizing; the daemon thread was never joined and the finalization "
        f"was lost. Lifecycle events present: {sorted(x for x in lifecycle if x)}"
    )

    # The event is appended *after* the terminal row, so it can only have been
    # printed by the drain that follows the wait. This is what distinguishes
    # "waited" from "happened to be fast".
    assert "process_exited" in result.stdout, (
        "the finalization completed but this CLI never printed it, so the "
        "second drain after the wait is missing"
    )


@pytest.mark.serial  # deadline-sensitive subprocess run; the widened window is seconds, not minutes
def test_ctrl_c_during_the_finalization_wait_reports_the_run_instead_of_crashing(configured_repo):
    """The window where the run is finished and the CLI is still running.

    `Supervisor.cancel` raises `SupervisorError` for a run that is not actively
    supervised — correct on its own terms — so Ctrl+C arriving while the CLI
    waited for finalization killed it with a traceback instead of reporting the
    run it had just waited for. The wait made that window two seconds wide
    where it used to be milliseconds, which is how the fix for one defect
    exposed another.
    """
    env = _cli_env()
    env["AICC_TEST_WIDEN_FINALIZATION_SECONDS"] = "6.0"

    proc = subprocess.Popen(
        [sys.executable, str(WIDEN_FINALIZATION_WRAPPER), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        # Wait for the run to be terminal — that is when the CLI is inside the
        # finalization wait and the signal lands in the window under test.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            runs = []
            try:
                runs = db.list_runs(db.resolve_db_path())
            except sqlite3.OperationalError:
                pass
            if any(r["state"] in db.TERMINAL_STATES for r in runs):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("no run reached a terminal state; the window was never entered")
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    assert "Traceback" not in stderr, stderr
    assert "SupervisorError" not in stderr, stderr
    assert "nothing to cancel" in stderr, stderr
    # And it still reports the run rather than exiting on a signal alone.
    assert _extract_last_json_object(stdout)["state"] in db.TERMINAL_STATES


@pytest.mark.serial  # deadline-sensitive subprocess run; the widened window is seconds, not minutes
def test_a_wedged_finalization_does_not_exit_zero(configured_repo, monkeypatch):
    """A warning printed while exiting 0 is the same defect one layer up.

    A caller that reads the status — a script, a queue, CI — was told the run
    finished cleanly, which is exactly the guarantee the warning says was lost.
    The timeout is squeezed rather than the run slowed, so this costs no wall
    clock: the widener holds finalization past a ceiling set below it.
    """
    env = _cli_env()
    env["AICC_TEST_WIDEN_FINALIZATION_SECONDS"] = "5.0"
    env["AICC_TEST_FINALIZATION_TIMEOUT_SECONDS"] = "1.0"

    result = subprocess.run(
        [sys.executable, str(WIDEN_FINALIZATION_WRAPPER), "launch", "AIOS", str(configured_repo), "implementation", "say ok", "--confirm"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert "did not finish finalizing" in result.stderr, result.stderr
    assert result.returncode == 5, (
        f"a wedged finalization exited {result.returncode}; a caller reading the "
        "status was told the run finished cleanly"
    )


def _load_cli():
    """Import the CLI as a module so `_run_foreground` can be driven directly.

    The subprocess tests above are the right shape for process-lifetime
    behaviour; this one is about a branch that needs a `SupervisorError` raised
    at a precise moment, which a real run cannot be made to do reliably.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("cli_under_test", CLI_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CancelFailsAPI:
    """A run that stays RUNNING and whose cancellation genuinely fails.

    This is `Supervisor.cancel`'s CAS-exhaustion path: it raises the same
    `SupervisorError` as the terminal-transition race, on a live run, meaning
    the opposite thing.
    """

    def __init__(self, message: str, *, state: str = "RUNNING") -> None:
        self._message = message
        self._state = state
        self.cancel_calls = 0
        self._interrupted = False
        #: A pid that does not exist means the process is gone. `os.getpid()`
        #: is the live case: this very process is unquestionably running.
        self.pid = os.getpid()

    def get_run(self, run_id):
        return {"id": run_id, "state": self._state, "pid": self.pid}

    def get_events(self, run_id, after_seq=0):
        # Once only. The handler drains events again, and a second interrupt
        # there would escape `_run_foreground` entirely — which is how this
        # fake first aborted the whole test session rather than the run.
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return []

    def request_cancel(self, run_id, *, confirmed, grace_seconds=None):
        from command_center.runtime.supervisor import SupervisorError

        self.cancel_calls += 1
        raise SupervisorError(self._message)


def test_a_failed_cancellation_is_not_reported_as_nothing_to_cancel(capsys):
    """`SupervisorError` means four different things and two of them are opposite.

    Two of `Supervisor.cancel`'s four raise sites are the terminal-transition
    race — the run finished while the signal was in flight, and there is
    genuinely nothing to cancel. One is CAS exhaustion against concurrent
    writes, which fires while the run is still RUNNING and means cancellation
    *failed*.

    The first version of this handler printed "Nothing to cancel" for all four,
    directly above the exception saying otherwise, and then exited — while this
    function's docstring promises it never abandons the child.
    """
    module = _load_cli()
    api = _CancelFailsAPI(
        "Run 'r1' could not be marked for cancellation after 5 attempts "
        "against concurrent writes."
    )

    code = module._run_foreground(api, {"id": "r1"})

    assert api.cancel_calls == 1, "cancellation was never even attempted"
    assert code == module._EXIT_CODE_CANCEL_FAILED, (
        f"a live run whose cancellation failed exited {code}; the caller was "
        "told the CLI finished its job"
    )
    err = capsys.readouterr().err
    assert "CANCELLATION FAILED" in err, err
    assert "Nothing to cancel" not in err, err
    assert "may still be running" in err, err


class _FinishesMidSignalAPI(_CancelFailsAPI):
    """The terminal-transition race, faithfully.

    A run that is COMPLETED before the signal never reaches `request_cancel` at
    all — the pre-check catches it. To reach the `except` with a finished run,
    it must be RUNNING when the handler looks and terminal by the time
    cancellation is refused. That is the window the handler exists for.
    """

    #: `_run_foreground` reads the run three times on this path: the polling
    #: loop, the handler's already-terminal pre-check, and the `except`'s
    #: re-read. The first two must see RUNNING or the pre-check answers and
    #: `request_cancel` is never called — which is what the first version of
    #: this fake did, and it reported the race as exercised when it was not.
    _TERMINAL_FROM_READ = 3

    def get_run(self, run_id):
        self._reads += 1
        state = self._state if self._reads >= self._TERMINAL_FROM_READ else "RUNNING"
        return {"id": run_id, "state": state, "pid": 4242}

    def __init__(self, message: str, *, state: str = "COMPLETED") -> None:
        super().__init__(message, state=state)
        self._reads = 0


def test_a_run_that_finished_mid_signal_is_still_reported_as_nothing_to_cancel(capsys):
    # The other direction, so the fix is not merely "call everything a failure".
    module = _load_cli()
    api = _FinishesMidSignalAPI(
        "Run 'r1' is not an actively supervised run in this process instance."
    )

    code = module._run_foreground(api, {"id": "r1"})

    assert api.cancel_calls == 1, "the pre-check swallowed it; the race was not exercised"
    assert code == 0, code
    err = capsys.readouterr().err
    assert "Nothing to cancel" in err, err
    assert "CANCELLATION FAILED" not in err, err


class _DeadProcessAPI(_CancelFailsAPI):
    """Cancellation took effect; only the row has not caught up.

    Two of `Supervisor.cancel`'s nine raise sites say exactly this — "has
    already exited and is finalizing" and "exited after cancellation, but its
    terminal state was not persisted in time". The run reads live at that
    instant *because the raise site guarantees the write is in flight*.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        # A pid that has certainly never existed in this session's namespace.
        self.pid = 2**22 - 1


def test_a_cancellation_that_took_effect_is_not_reported_as_a_failure(capsys):
    """Exit 6 said "the agent may still be running" about a dead process.

    The handler counted four raise sites and classified from one instantaneous
    read of a row those very sites guarantee is mid-write. Review drove both
    post-signal cases and got exit 6 with an instruction to go hunt a process
    that no longer exists — round two's defect reflected: a message asserting
    the opposite of what happened, printed above the exception that says so.
    """
    module = _load_cli()
    api = _DeadProcessAPI(
        "Run 'r1' has already exited and is finalizing; cancellation was not recorded."
    )

    code = module._run_foreground(api, {"id": "r1"})

    err = capsys.readouterr().err
    assert "CANCELLATION FAILED" not in err, err
    assert "may still be running" not in err, err
    assert "its process" in err and "is gone" in err, err
    assert code != module._EXIT_CODE_CANCEL_FAILED, (
        "a cancellation that took effect was reported as one that failed"
    )


def test_a_live_process_is_still_reported_as_a_failed_cancellation(capsys):
    # The other direction: only 'did not exit after cancellation escalation'
    # actually means the child is still there, and that must keep exit 6.
    module = _load_cli()
    api = _CancelFailsAPI(
        "Run 'r1' did not exit after cancellation escalation; ownership is retained."
    )

    code = module._run_foreground(api, {"id": "r1"})

    assert code == module._EXIT_CODE_CANCEL_FAILED, code
    err = capsys.readouterr().err
    assert "CANCELLATION FAILED" in err, err


class _SettlesLateAPI(_CancelFailsAPI):
    """The row catches up a moment after the raise, with the process alive.

    This is the shape a single instantaneous read cannot classify: the raise
    site fires while the terminal write is in flight, so the first read says
    RUNNING and the truth arrives shortly after. The pid stays alive, so the
    process check cannot rescue it either — only waiting for the row can.
    """

    _TERMINAL_FROM_READ = 4

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self._reads = 0

    def get_run(self, run_id):
        self._reads += 1
        state = "CANCELLED" if self._reads >= self._TERMINAL_FROM_READ else "RUNNING"
        return {"id": run_id, "state": state, "pid": self.pid}


def test_a_row_that_settles_a_moment_later_is_not_called_a_failed_cancellation(capsys):
    """Several raise sites fire *while* the terminal state is being written.

    Classifying on one read there is classifying on a value the raise site
    guarantees is in flight. Nothing else covers this case: the process is
    alive, so the pid check says "still running", and only giving the row a
    bounded moment gets the right answer.
    """
    module = _load_cli()
    api = _SettlesLateAPI(
        "Run 'r1' reached a terminal state after cancellation, but local "
        "finalization did not complete in time."
    )

    code = module._run_foreground(api, {"id": "r1"})

    err = capsys.readouterr().err
    assert "CANCELLATION FAILED" not in err, err
    assert code != module._EXIT_CODE_CANCEL_FAILED, (
        "the row settled to a terminal state and was still reported as a "
        "failed cancellation"
    )
    assert "Nothing to cancel" in err, err
