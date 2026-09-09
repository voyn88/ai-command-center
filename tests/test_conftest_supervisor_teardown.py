"""Regression coverage for VOYN-W0-AICC-FLAKE-BOARD-JOURNEY-TMPDIR-TEARDOWN.

`test_board_user_journey.py::test_attention_triage_fix_relaunches_a_failed_task`
returns as soon as it observes a run's report row, without ever calling
`Supervisor.wait_for_run` for that run. The `Supervisor`'s finalization tail
(the `process_exited` lifecycle event, the auto-commit, the `finalized_at`
watermark — see `run_finalizer.RunFinalizer.mark_finalized`) keeps running on
a daemon thread after the report row is visible, so `tests/conftest.py`'s
autouse `isolated_data_dir` fixture could `shutil.rmtree` the shared
`AICC_DATA_DIR` tree while that thread was still writing into it, raising
`OSError: [Errno 39] Directory not empty` — or racing the *next* test's fresh,
unmigrated recreation of the same path, surfacing as `sqlite3.OperationalError:
no such table: run`.

`conftest._join_live_supervisor_threads` closes that window by blocking
`isolated_data_dir`'s setup and teardown on every live Supervisor background
thread before any removal. These tests exercise that helper directly, rather
than the original timing-dependent flake, so the regression is deterministic.
"""

from __future__ import annotations

import threading

import pytest

from tests.conftest import _join_live_supervisor_threads


def test_join_live_supervisor_threads_waits_for_a_slow_writer():
    finished = threading.Event()

    def _still_writing():
        # Stand in for the finalization tail (process_exited event,
        # auto-commit, finalized_at watermark) that keeps running after a
        # test has already observed the report row it cared about.
        threading.Event().wait(timeout=0.2)
        finished.set()

    thread = threading.Thread(target=_still_writing, name="run-supervisor-regression", daemon=True)
    thread.start()

    _join_live_supervisor_threads(timeout=2.0)

    assert finished.is_set()
    assert not thread.is_alive()


def test_join_live_supervisor_threads_ignores_unrelated_threads():
    release = threading.Event()

    def _unrelated():
        release.wait(timeout=5.0)

    thread = threading.Thread(target=_unrelated, name="some-other-background-thread", daemon=True)
    thread.start()
    try:
        # Must return immediately: it has nothing to do with the Supervisor
        # thread names the shared data dir teardown needs to wait on.
        _join_live_supervisor_threads(timeout=0.1)
    finally:
        release.set()
        thread.join(timeout=5.0)


def test_join_live_supervisor_threads_fails_loudly_if_a_thread_outlives_the_timeout():
    release = threading.Event()

    def _stuck_writer():
        release.wait(timeout=5.0)

    thread = threading.Thread(target=_stuck_writer, name="run-supervisor-regression-stuck", daemon=True)
    thread.start()
    try:
        with pytest.raises(AssertionError, match="run-supervisor-regression-stuck"):
            _join_live_supervisor_threads(timeout=0.1)
    finally:
        release.set()
        thread.join(timeout=5.0)
