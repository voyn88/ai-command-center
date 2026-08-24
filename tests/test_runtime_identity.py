import os
import subprocess
import time

import pytest

from command_center.runtime import identity


def _process_state(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    state = result.stdout.strip()
    return state or None


def test_capture_identity_for_running_process():
    proc = subprocess.Popen(["sleep", "2"])
    try:
        ident = identity.capture_identity(proc.pid)
        assert ident is not None
        assert ident.pid == proc.pid
        assert "sleep" in ident.command
        assert ident.start_time
    finally:
        proc.terminate()
        proc.wait()


def test_capture_identity_returns_none_for_dead_process():
    proc = subprocess.Popen(["true"])
    proc.wait()
    time.sleep(0.2)
    assert identity.capture_identity(proc.pid) is None


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_zombie_is_not_treated_as_a_running_process():
    pid = os.fork()
    if pid == 0:
        os._exit(0)

    try:
        deadline = time.monotonic() + 2.0
        state = _process_state(pid)
        while state is not None and not state.startswith("Z") and time.monotonic() < deadline:
            time.sleep(0.01)
            state = _process_state(pid)

        assert state is not None and state.startswith("Z")
        assert identity.capture_identity(pid) is None
        assert identity.process_exists(pid) is False
        assert identity.identity_matches(pid, "stale|identity") is False
    finally:
        os.waitpid(pid, 0)


def test_process_exists_true_while_running_false_after_exit():
    proc = subprocess.Popen(["sleep", "1"])
    try:
        assert identity.process_exists(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait()
    time.sleep(0.2)
    assert identity.process_exists(proc.pid) is False


def test_identity_matches_true_for_same_running_process():
    proc = subprocess.Popen(["sleep", "2"])
    try:
        recorded = identity.capture_identity(proc.pid).as_string()
        assert identity.identity_matches(proc.pid, recorded) is True
    finally:
        proc.terminate()
        proc.wait()


def test_identity_matches_false_when_recorded_identity_is_stale_text():
    proc = subprocess.Popen(["sleep", "2"])
    try:
        assert identity.identity_matches(proc.pid, "bogus start time|bogus command") is False
    finally:
        proc.terminate()
        proc.wait()


def test_identity_matches_false_when_process_gone():
    proc = subprocess.Popen(["true"])
    proc.wait()
    time.sleep(0.2)
    assert identity.identity_matches(proc.pid, "anything") is False


def test_identity_matches_false_when_recorded_identity_missing():
    proc = subprocess.Popen(["sleep", "2"])
    try:
        assert identity.identity_matches(proc.pid, None) is False
        assert identity.identity_matches(proc.pid, "") is False
    finally:
        proc.terminate()
        proc.wait()


def test_capture_identity_handles_invalid_pid_gracefully():
    assert identity.capture_identity(-1) is None
