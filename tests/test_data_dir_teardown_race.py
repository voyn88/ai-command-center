"""The isolated data dir is removed only once nothing is still writing to it.

VOYN-W0-AICC-FLAKY-TEST-DATA-DIR-TEARDOWN-RACE.

`tests/conftest.py`'s `isolated_data_dir` used to end with a bare
`shutil.rmtree(_TEST_DATA_DIR)`, on the assumption that a test body returning
means the data dir is idle. It does not. A v2 run is supervised by
`run-supervisor-<run_id>`, a daemon thread nobody joins, and that thread is
still writing to `<data dir>/runtime.db` after every marker a test can
practically wait on — `db.create_report` is followed by the `finalized_at`
stamp, which is deliberately the *last* write of finalization rather than part
of the terminal-state update. Across that gap SQLite recreates
`runtime.db-wal`/`runtime.db-shm` by name whenever the writing connection
touches the database again.

On `main` (CI run 34400558008, commit 574150a1) that landed as:

    ERROR at teardown of test_attention_triage_fix_relaunches_a_failed_task
    OSError: [Errno 39] Directory not empty: /tmp/aicc_test_data__76sgqu0

Reproduced here at 2 runs in 40 of that test; instrumenting the failing
`rmtree` showed exactly `['runtime.db-shm', 'runtime.db-wal']` left behind and
exactly one non-main thread alive, `run-supervisor-<run_id>`. A teardown ERROR
fails the whole shard: it publishes no collection receipt, so the Linux
manifest gate and the final merge gate fail with it and `main` goes red for a
flake.

The tests below are written so they fail with the fix removed rather than once
in twenty runs: each one either holds a writer open past the point the
unguarded teardown would have deleted the directory, or widens the real
supervisor's finalization window until the race is a certainty. The first test
is the control — it runs the *old* behaviour by passing `quiesce_timeout=0.0`,
so it is a live demonstration that waiting, not retrying, is what fixes this.
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from command_center.runtime import db, supervisor
from tests import conftest

READ_PROMPT = "Review the module and summarize the findings."

#: How long the real supervisor thread is held between writing the report row
#: and stamping `finalized_at`. Wide enough that the wait below cannot be
#: mistaken for scheduling noise, short enough to stay inside a test budget.
WIDEN_SECONDS = 1.5


class _DataDirWriter:
    """A stand-in with the exact shape of a supervising run.

    It announces itself the same two ways a real one does — a run id in
    `supervisor._PROCESS_OWNED_RUNS` and a `run-supervisor-<id>` thread — and
    keeps writing the same file names SQLite recreates, so a teardown that
    removes the directory underneath it is observable as a write failure
    instead of having to be inferred from a leftover file.
    """

    def __init__(self, directory: Path, run_id: str) -> None:
        self.directory = directory
        self.run_id = run_id
        self.errors: list[BaseException] = []
        self.writes = 0
        self._release = threading.Event()
        self._writing = threading.Event()
        with supervisor._PROCESS_OWNED_RUNS_GUARD:
            supervisor._PROCESS_OWNED_RUNS.add(run_id)
        self._thread = threading.Thread(
            target=self._write_until_released,
            name=f"run-supervisor-{run_id}",
            daemon=True,
        )
        self._thread.start()
        assert self._writing.wait(5.0), "the stand-in writer never started"

    def _write_until_released(self) -> None:
        try:
            while not self._release.is_set():
                for name in ("runtime.db-wal", "runtime.db-shm"):
                    (self.directory / name).write_bytes(b"x" * 64)
                self.writes += 1
                self._writing.set()
                time.sleep(0.001)
        except BaseException as error:  # noqa: BLE001 — reported, not raised on a daemon thread
            self.errors.append(error)
        finally:
            # Ownership is dropped exactly where `_release_active` drops it:
            # after the last write, never before.
            with supervisor._PROCESS_OWNED_RUNS_GUARD:
                supervisor._PROCESS_OWNED_RUNS.discard(self.run_id)

    def write_now(self) -> None:
        """One write, synchronously, from the caller's thread."""
        for name in ("runtime.db-wal", "runtime.db-shm"):
            (self.directory / name).write_bytes(b"x" * 64)

    def release_after(self, seconds: float) -> None:
        threading.Timer(seconds, self._release.set).start()

    def stop(self) -> None:
        self._release.set()
        self._thread.join(timeout=5.0)
        with supervisor._PROCESS_OWNED_RUNS_GUARD:
            supervisor._PROCESS_OWNED_RUNS.discard(self.run_id)


@pytest.fixture
def data_dir_writer(tmp_path):
    """Start stand-in writers, and guarantee they are unregistered afterwards.

    A leaked entry in `_PROCESS_OWNED_RUNS` would make *every* later test in
    this worker wait out the quiesce timeout, so the cleanup is a fixture
    rather than a `finally` inside each test.
    """
    writers: list[_DataDirWriter] = []
    counter = iter(range(1000))

    def start(directory: Path | None = None) -> _DataDirWriter:
        target = directory if directory is not None else tmp_path / "data"
        target.mkdir(parents=True, exist_ok=True)
        writer = _DataDirWriter(target, f"regression-writer-{next(counter)}")
        writers.append(writer)
        return writer

    yield start

    for writer in writers:
        writer.stop()


# --------------------------------------------------------------------------
# Control — the unguarded teardown, run deliberately
# --------------------------------------------------------------------------


def test_removing_the_data_dir_without_waiting_fails_and_names_the_writer(
    data_dir_writer, monkeypatch
):
    """`quiesce_timeout=0.0` is the pre-fix teardown: remove, retry, give up.

    The race is pinned rather than raced for, in the spirit of
    `tests/fixtures/finalization_window.py`: `os.rmdir` is the last call of an
    `rmtree` pass, so hooking it lands the live writer's write in exactly the
    gap production hits — after the directory has been walked and emptied,
    before it can be removed. Nothing about the writer is faked; only *when*
    its write arrives is made certain, so this fails every time instead of two
    runs in forty.

    It also pins the diagnostic. The point of the error is that it identifies
    *which* writer, which is what the raw `OSError` on `main` never did.
    """
    writer = data_dir_writer()
    real_rmdir = os.rmdir

    def rmdir_losing_the_race(path, *args, **kwargs):
        writer.write_now()
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir_losing_the_race)
    try:
        with pytest.raises(AssertionError) as raised:
            conftest.remove_data_dir_when_quiet(writer.directory, quiesce_timeout=0.0)
    finally:
        # `isolated_data_dir` removes the session's real data dir after this
        # test, through the same functions patched here. Undo before then.
        monkeypatch.undo()

    message = str(raised.value)
    assert f"run:{writer.run_id}" in message
    assert f"thread:run-supervisor-{writer.run_id}" in message
    assert "runtime.db-wal" in message  # the leftover that broke the rmdir
    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.errno == errno.ENOTEMPTY
    assert writer.directory.exists(), "a failed removal must not half-delete the dir away silently"


# --------------------------------------------------------------------------
# The fix — wait for the writer, then remove
# --------------------------------------------------------------------------


def test_removing_the_data_dir_waits_for_a_live_writer_to_finish(data_dir_writer):
    writer = data_dir_writer()
    writer.release_after(0.75)

    started = time.monotonic()
    conftest.remove_data_dir_when_quiet(writer.directory)
    elapsed = time.monotonic() - started

    assert not writer.directory.exists()
    assert elapsed >= 0.5, f"the removal did not wait for the writer (returned in {elapsed:.3f}s)"
    # The property that matters: the directory was never removed *while* the
    # writer was live. A writer that lost its directory mid-write records the
    # `FileNotFoundError` here instead of it being a leftover file nobody sees.
    assert writer.errors == []
    assert writer.writes > 0


def test_a_writer_that_is_already_finished_costs_no_wait(data_dir_writer):
    """The common case — the whole suite pays this path, twice per test."""
    writer = data_dir_writer()
    writer.stop()

    started = time.monotonic()
    conftest.remove_data_dir_when_quiet(writer.directory)
    elapsed = time.monotonic() - started

    assert not writer.directory.exists()
    # Comfortably under `_WRITER_QUIESCE_TIMEOUT_SECONDS`: the claim is that a
    # finished writer is not waited for at all, not a precise stopwatch reading
    # on a runner sharing its cores with three other xdist workers.
    assert elapsed < 5.0, f"a quiet data dir must be removed immediately, took {elapsed:.3f}s"


def test_a_transient_not_empty_is_retried_when_no_writer_is_known(tmp_path, monkeypatch):
    """Defence in depth, not the mechanism.

    Waiting covers the writers this module can see. A single transient failure
    from one it cannot — a stray thread in a test, a filesystem that reports a
    just-unlinked entry — must not be a red shard either, so the removal is
    retried. It is deliberately *not* the fix: the control above shows a live
    writer defeats the retry budget outright.
    """
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / "runtime.db").write_bytes(b"")
    real_rmtree = shutil.rmtree
    attempts = {"n": 0}

    def rmtree_failing_once(path, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(conftest.shutil, "rmtree", rmtree_failing_once)
    try:
        conftest.remove_data_dir_when_quiet(directory)
    finally:
        monkeypatch.undo()  # see the control test: the data-dir fixture is next

    assert attempts["n"] == 2
    assert not directory.exists()


def test_a_permanent_error_is_raised_rather_than_retried(tmp_path, monkeypatch):
    """A permission problem is not a race; retrying it hides a real fault."""
    directory = tmp_path / "data"
    directory.mkdir()

    def rmtree_denied(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(conftest.shutil, "rmtree", rmtree_denied)
    try:
        with pytest.raises(PermissionError):
            conftest.remove_data_dir_when_quiet(directory)
    finally:
        monkeypatch.undo()  # see the control test: the data-dir fixture is next


# --------------------------------------------------------------------------
# The real supervisor — the two signals the stand-in above imitates
# --------------------------------------------------------------------------


def test_a_real_run_is_still_a_live_writer_after_its_report_row_exists(
    git_repo, configure_project_repo, fake_claude, tmp_path, monkeypatch
):
    """Everything above assumes a finalizing run is visible as a live writer.

    This is the test that checks the assumption against the real thing, with
    the window widened to a certainty rather than raced for: the supervising
    thread is held for `WIDEN_SECONDS` immediately after `db.create_report`
    returns — the exact marker `test_board_user_journey.py` used to wait on —
    and before the `finalized_at` stamp that follows it.

    The three assertions are the three links in the chain. The run is a live
    writer at the moment the report row appears (so the old teardown was
    removing the directory out from under it); waiting for the writers costs
    the width of that window (so the wait is real); and by the time the wait
    returns the run is finalized (so the wait covers the *last* write, not
    just the one that was already visible).
    """
    configure_project_repo("AIOS", git_repo)
    data_dir = tmp_path / "isolated_data"
    data_dir.mkdir()

    widened = {"calls": 0}
    real_create_report = db.create_report

    def create_report_then_hold(*args, **kwargs):
        result = real_create_report(*args, **kwargs)
        widened["calls"] += 1
        time.sleep(WIDEN_SECONDS)
        return result

    monkeypatch.setattr(db, "create_report", create_report_then_hold)

    sup = supervisor.Supervisor(data_dir / "runtime.db")
    run = sup.start_raw(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="review",
        prompt=READ_PROMPT,
        confirmed=True,
    )

    deadline = time.monotonic() + 30.0
    while db.get_report(sup.db_path, run["id"]) is None:
        assert time.monotonic() < deadline, "the run never wrote a report row"
        time.sleep(0.01)

    assert f"run:{run['id']}" in conftest.live_background_writers(), (
        "a run whose report row exists is still a live writer — this is the "
        "state the old teardown removed the data dir in"
    )

    started = time.monotonic()
    stragglers = conftest.quiesce_background_writers()
    elapsed = time.monotonic() - started

    assert stragglers == []
    assert widened["calls"] >= 1, "the finalization window was never widened; this test proved nothing"
    assert elapsed >= WIDEN_SECONDS * 0.5, (
        f"the wait returned after {elapsed:.3f}s, before the widened "
        f"{WIDEN_SECONDS}s finalization window could have closed"
    )
    assert db.get_run(sup.db_path, run["id"])["finalized_at"], (
        "waiting for the writers must cover the finalization write that follows "
        "the report row, not stop at the report row"
    )

    conftest.remove_data_dir_when_quiet(data_dir)
    assert not data_dir.exists()


def test_teardown_survives_a_test_that_patched_the_global_clock(data_dir_writer, monkeypatch):
    """The teardown must not run on a clock a test is allowed to replace.

    Fixture teardown runs *before* `monkeypatch`'s undo, so a test that patched
    `time.monotonic` on the shared `time` module is still patched while the data
    dir is removed. `tests/ops/test_agent_principal_isolation.py` does exactly
    that — a finite iterator of five readings, because it is driving a SIGTERM
    escalation deterministically — and `launcher.time` *is* the `time` module,
    so the patch is global rather than scoped to the launcher.

    A teardown that called `time.monotonic()` exhausted that iterator and raised
    `StopIteration`, which pytest-qt's teardown hook re-raised as `RuntimeError:
    generator raised StopIteration`. That is a teardown ERROR — the same thing
    that fails a whole shard and the manifest gate, just reached by a different
    route than the `rmtree` race itself.

    The replacement clock here is *empty*, so the assertion is the strict one:
    the teardown reads the patched clock zero times. A budget of "enough
    readings" would let a regression pass by happening to be fast enough, and
    the real iterator's length is an unrelated test's implementation detail.
    """
    writer = data_dir_writer()
    writer.stop()

    readings = iter(())
    monkeypatch.setattr(time, "monotonic", lambda: next(readings))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    try:
        conftest.remove_data_dir_when_quiet(writer.directory)
    finally:
        monkeypatch.undo()

    assert not writer.directory.exists()
