import os
import subprocess
import sys
import time

import pytest

from command_center.runtime import identity


def _sleep_process(seconds: int = 10) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        text=True,
    )


def _completed_process() -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-c", ""], text=True)


def _stable_identity(pid: int) -> identity.ProcessIdentity:
    """Wait until an exec'ing child exposes the same identity twice."""
    deadline = time.monotonic() + 5.0
    previous: identity.ProcessIdentity | None = None
    while time.monotonic() < deadline:
        current = identity.capture_identity(pid)
        if current is not None and current == previous:
            return current
        previous = current
        time.sleep(0.01)
    raise AssertionError("process identity did not stabilize")


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
    proc = _sleep_process()
    try:
        ident = _stable_identity(proc.pid)
        assert ident.pid == proc.pid
        assert ident.command
        assert ident.start_time
    finally:
        proc.terminate()
        proc.wait()


def test_capture_identity_returns_none_for_dead_process():
    proc = _completed_process()
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
    proc = _sleep_process(1)
    try:
        assert identity.process_exists(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait()
    time.sleep(0.2)
    assert identity.process_exists(proc.pid) is False


def test_identity_matches_true_for_same_running_process():
    proc = _sleep_process()
    try:
        recorded = _stable_identity(proc.pid).as_string()
        assert identity.identity_matches(proc.pid, recorded) is True
    finally:
        proc.terminate()
        proc.wait()


def test_identity_matches_false_when_recorded_identity_is_stale_text():
    proc = _sleep_process()
    try:
        assert identity.identity_matches(proc.pid, "bogus start time|bogus command") is False
    finally:
        proc.terminate()
        proc.wait()


def test_identity_matches_false_when_process_gone():
    proc = _completed_process()
    proc.wait()
    time.sleep(0.2)
    assert identity.identity_matches(proc.pid, "anything") is False


def test_identity_matches_false_when_recorded_identity_missing():
    proc = _sleep_process()
    try:
        assert identity.identity_matches(proc.pid, None) is False
        assert identity.identity_matches(proc.pid, "") is False
    finally:
        proc.terminate()
        proc.wait()


def test_versioned_birth_identity_ignores_mutable_command_text():
    current = identity.ProcessIdentity(
        42, "posix-ps-utc-v1:Sun Aug 30 00:23:45 2026", "new argv"
    )
    recorded = "posix-ps-utc-v1:Sun Aug 30 00:23:45 2026|old argv"

    assert identity.compare_recorded_identity(current, recorded) is True


def test_versioned_birth_identity_proves_pid_reuse():
    current = identity.ProcessIdentity(
        42, "posix-ps-utc-v1:Sun Aug 30 00:23:46 2026", "same argv"
    )
    recorded = "posix-ps-utc-v1:Sun Aug 30 00:23:45 2026|same argv"

    assert identity.compare_recorded_identity(current, recorded) is False


def test_legacy_live_identity_mismatch_is_unknown_during_upgrade():
    current = identity.ProcessIdentity(
        42, "posix-ps-utc-v1:Sun Aug 30 00:23:45 2026", "python worker.py"
    )

    assert (
        identity.compare_recorded_identity(
            current, "Sat Aug 29 17:23:45 2026|python worker.py"
        )
        is None
    )


def test_different_known_identity_schemes_are_not_comparable():
    current = identity.ProcessIdentity(
        42, "windows-filetime-v1:133700000000000000", r"C:\Python\python.exe"
    )

    assert (
        identity.compare_recorded_identity(
            current, "posix-ps-utc-v1:Sun Aug 30 00:23:45 2026|python"
        )
        is None
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux procfs")
def test_linux_procfs_identity_uses_high_resolution_start_ticks():
    query = identity._query_identity_linux_procfs(os.getpid())

    assert query is not None
    assert query.status is identity.ProcessQueryStatus.LIVE
    assert query.identity is not None
    assert query.identity.start_time.startswith(
        "linux-procfs-bootid-startticks-v2:"
    )


def _procfs_stat(*, start_ticks: int, mutable_counter: int, state: str = "S") -> str:
    # Values after ``comm`` begin at procfs field 3. Index 19 is starttime
    # (field 22); surrounding counters are intentionally synthetic.
    fields = [state, *[str(mutable_counter)] * 18, str(start_ticks), "0", "0"]
    return f"4242 (python worker) {' '.join(fields)}\n"


def test_linux_procfs_identity_ignores_mutable_fields_between_reads(monkeypatch):
    snapshots = iter(
        [
            _procfs_stat(start_ticks=987654, mutable_counter=1),
            _procfs_stat(start_ticks=987654, mutable_counter=2),
        ]
    )
    monkeypatch.setattr(identity.os.path, "isdir", lambda _path: True)
    def read_text(path, *_args, **_kwargs):
        if path.name == "boot_id":
            return "11111111-2222-3333-4444-555555555555\n"
        return next(snapshots)

    monkeypatch.setattr(identity.Path, "read_text", read_text)

    query = identity._query_identity_linux_procfs(4242)

    assert query is not None
    assert query.status is identity.ProcessQueryStatus.LIVE
    assert query.identity == identity.ProcessIdentity(
        pid=4242,
        start_time=(
            "linux-procfs-bootid-startticks-v2:"
            "11111111-2222-3333-4444-555555555555:987654"
        ),
        command="python worker",
    )


def test_linux_procfs_identity_rejects_pid_reuse_between_reads(monkeypatch):
    snapshots = iter(
        [
            _procfs_stat(start_ticks=111, mutable_counter=1),
            _procfs_stat(start_ticks=222, mutable_counter=1),
        ]
    )
    monkeypatch.setattr(identity.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        identity.Path, "read_text", lambda *_args, **_kwargs: next(snapshots)
    )

    query = identity._query_identity_linux_procfs(4242)

    assert query is not None
    assert query.status is identity.ProcessQueryStatus.UNKNOWN
    assert query.identity is None


def test_linux_procfs_identity_requires_boot_id(monkeypatch):
    snapshots = iter(
        [
            _procfs_stat(start_ticks=987654, mutable_counter=1),
            _procfs_stat(start_ticks=987654, mutable_counter=2),
        ]
    )
    monkeypatch.setattr(identity.os.path, "isdir", lambda _path: True)

    def read_text(path, *_args, **_kwargs):
        if path.name == "boot_id":
            raise PermissionError("boot identity unavailable")
        return next(snapshots)

    monkeypatch.setattr(identity.Path, "read_text", read_text)

    query = identity._query_identity_linux_procfs(4242)

    assert query is not None
    assert query.status is identity.ProcessQueryStatus.UNKNOWN
    assert query.identity is None


def test_linux_procfs_identity_detects_same_ticks_from_another_boot():
    current = identity.ProcessIdentity(
        42,
        (
            "linux-procfs-bootid-startticks-v2:"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:987654"
        ),
        "python",
    )
    recorded = (
        "linux-procfs-bootid-startticks-v2:"
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:987654|python"
    )

    assert identity.compare_recorded_identity(current, recorded) is False


def test_linux_procfs_v1_and_v2_identities_are_incomparable():
    current = identity.ProcessIdentity(
        42,
        (
            "linux-procfs-bootid-startticks-v2:"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:987654"
        ),
        "python",
    )

    assert (
        identity.compare_recorded_identity(
            current, "linux-procfs-startticks-v1:987654|python"
        )
        is None
    )


def test_capture_identity_handles_invalid_pid_gracefully():
    assert identity.capture_identity(-1) is None


def test_windows_identity_query_uses_native_helper_without_signals(monkeypatch) -> None:
    expected = identity.ProcessQuery(
        identity.ProcessQueryStatus.LIVE,
        identity.ProcessIdentity(
            pid=4242,
            start_time="133700000000000000",
            command=r"C:\\Python\\python.exe",
        ),
    )
    monkeypatch.setattr(identity.os, "name", "nt")
    monkeypatch.setattr(identity, "_query_identity_windows", lambda pid: expected)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Windows identity query must not use ps or os.kill")

    monkeypatch.setattr(identity.subprocess, "run", forbidden)
    monkeypatch.setattr(identity.os, "kill", forbidden)

    assert identity.query_identity(4242) == expected


def test_failed_ps_fallback_never_signals_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(identity.os, "name", "nt")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Windows identity fallback must not call os.kill")

    monkeypatch.setattr(identity.os, "kill", forbidden)

    assert identity._status_after_failed_ps(4242) is identity.ProcessQueryStatus.UNKNOWN


def test_windows_native_api_failure_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        identity,
        "_query_identity_windows_native",
        lambda _pid: (_ for _ in ()).throw(OSError("native API unavailable")),
    )

    assert identity._query_identity_windows(4242).status is identity.ProcessQueryStatus.UNKNOWN


def test_windows_pid_outside_dword_is_unknown_without_native_call(monkeypatch) -> None:
    def forbidden(_pid: int):
        raise AssertionError("out-of-range pid must not cross the native boundary")

    monkeypatch.setattr(identity, "_query_identity_windows_native", forbidden)

    assert (
        identity._query_identity_windows(0x1_0000_0000).status
        is identity.ProcessQueryStatus.UNKNOWN
    )


@pytest.mark.skipif(os.name == "nt", reason="tests the POSIX ps fallback")
def test_failed_ps_for_existing_pid_is_unknown_and_conservatively_exists(
    monkeypatch,
) -> None:
    monkeypatch.setattr(identity, "_query_identity_linux_procfs", lambda _pid: None)
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="unsupported state keyword"
        ),
    )
    monkeypatch.setattr(identity.os, "kill", lambda _pid, _signal: None)

    query = identity.query_identity(4242)
    assert query.status is identity.ProcessQueryStatus.UNKNOWN
    assert query.identity is None
    assert identity.capture_identity(4242) is None
    assert identity.process_exists(4242) is True
    assert identity.identity_matches(4242, "recorded|identity") is False


@pytest.mark.skipif(os.name == "nt", reason="tests the POSIX ps fallback")
def test_failed_ps_for_absent_pid_is_confirmed_absent(monkeypatch) -> None:
    monkeypatch.setattr(identity, "_query_identity_linux_procfs", lambda _pid: None)
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        ),
    )

    def absent(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(identity.os, "kill", absent)

    assert identity.query_identity(4242).status is identity.ProcessQueryStatus.ABSENT
    assert identity.process_exists(4242) is False


@pytest.mark.skipif(os.name == "nt", reason="tests the POSIX ps fallback")
def test_malformed_successful_ps_output_is_unknown_not_absent(monkeypatch) -> None:
    monkeypatch.setattr(identity, "_query_identity_linux_procfs", lambda _pid: None)
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="S malformed", stderr=""
        ),
    )

    assert identity.query_identity(4242).status is identity.ProcessQueryStatus.UNKNOWN
    assert identity.process_exists(4242) is True


@pytest.mark.skipif(os.name == "nt", reason="tests the POSIX ps environment")
def test_posix_identity_query_canonicalizes_ps_locale_and_timezone(monkeypatch) -> None:
    observed: dict[str, str] = {}
    monkeypatch.setattr(identity, "_query_identity_linux_procfs", lambda _pid: None)

    def fake_ps(*_args, **kwargs):
        observed.update(kwargs["env"])
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="S Sun Aug 30 00:23:45 2026 /usr/bin/python\n",
            stderr="",
        )

    monkeypatch.setattr(identity.subprocess, "run", fake_ps)

    query = identity.query_identity(4242)
    assert query.status is identity.ProcessQueryStatus.LIVE
    assert query.identity.start_time.startswith("posix-ps-utc-v1:")
    assert observed["LANG"] == observed["LC_ALL"] == "C"
    assert observed["TZ"] == "UTC"
