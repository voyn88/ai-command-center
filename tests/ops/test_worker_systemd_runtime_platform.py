"""Real-systemd proof for the worker's runtime platform (VOYN-W0-AICC-SRV-05-LINUX-VERIFIED).

Every mechanism `deploy/systemd/aicc-worker.service` and `voyn-aicc-worker@.service`
lean on -- restart pacing, cgroup kill semantics, PDEATHSIG, memory/task/address-space
limits, seccomp, journald's trusted fields, StateDirectory permissions, and the
notify/watchdog protocol -- is exercised here against the *real* kernel and systemd
on the host running this suite, via disposable `systemd-run --user` transient units.
No unit here is installed into the system manager and nothing here needs root: it
proves the mechanism a `User=`-scoped worker actually gets, not a mocked one.

Two properties the production units also depend on are deliberately NOT covered
here, because they are cross-host claims a single machine cannot exercise: that a
lease abandoned by a SIGKILLed worker on one host is picked up by exactly one other
host, and that a network partition is arbitrated by the queue rather than by either
host's local judgment. Those need a second real host (paired the way
`voyn-control-01` pairs with this one) and are tracked separately -- see
`docs/operations/SRV05_LINUX_SYSTEMD_VERIFICATION.md`.

Each test spawns and tears down its own transient unit; nothing here touches a real
system unit or requires elevated privileges. Timing-sensitive (real subprocesses,
real watchdog/restart timers), like the rest of this repo's `serial` tests.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.serial]

REPO_ROOT = Path(__file__).parents[2]
_ENV = {
    **os.environ,
    "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
}


def _systemd_user_session_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        probe = subprocess.run(
            [
                "systemd-run",
                "--user",
                f"--unit=aicc-srv05-probe-{uuid.uuid4().hex[:8]}",
                "--wait",
                "--",
                "/bin/true",
            ],
            check=False,
            env=_ENV,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return probe.returncode == 0


_LIVE_SYSTEMD = _systemd_user_session_available()

skip_without_live_systemd = pytest.mark.skipif(
    not _LIVE_SYSTEMD,
    reason="requires a real, reachable systemd --user session on Linux "
    "(systemd-run --user --wait -- /bin/true must succeed); this proves the "
    "mechanism against the real kernel/systemd rather than mocking it, so "
    "there is no fallback path off a live session",
)
skip_off_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="PR_SET_PDEATHSIG is a Linux-only prctl()",
)


def _unit_name(tag: str) -> str:
    return f"aicc-srv05-verify-{tag}-{uuid.uuid4().hex[:8]}"


def _stop_and_forget(unit: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", f"{unit}.service"],
        check=False,
        env=_ENV,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["systemctl", "--user", "reset-failed", f"{unit}.service"],
        check=False,
        env=_ENV,
        capture_output=True,
        timeout=10,
    )


@pytest.fixture
def transient_unit():
    created: list[str] = []

    def _run(
        tag: str,
        properties: list[str],
        argv: list[str],
        *,
        wait: bool = True,
        timeout: float = 30,
    ):
        unit = _unit_name(tag)
        created.append(unit)
        cmd = ["systemd-run", "--user", f"--unit={unit}"]
        if wait:
            cmd.append("--wait")
        cmd += [f"--property={p}" for p in properties]
        cmd += ["--"] + argv
        result = subprocess.run(
            cmd, check=False, env=_ENV, capture_output=True, text=True, timeout=timeout
        )
        return unit, result

    yield _run

    for unit in created:
        _stop_and_forget(unit)


def _show(unit: str, *properties: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "--user", "show", f"{unit}.service"]
        + [f"-p{p}" for p in properties],
        check=False,
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=10,
    )
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _journal_json(unit: str) -> list[dict]:
    result = subprocess.run(
        ["journalctl", "--user", "-u", f"{unit}.service", "-o", "json", "--no-pager"],
        check=False,
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


# --- 1. systemd-analyze verify: the unit files parse under the real systemd on
# the host, exactly as claimed for both the base worker unit and the ported
# preprod lane template. ExecStart/ExecStartPre/ExecReload point at
# `/opt/aicc/...`, an install-time path this dev host never has -- that is a
# deployment fact, not a unit-syntax one, so it is stubbed to `/bin/true` here
# to isolate the claim actually under test.
@skip_without_live_systemd
@pytest.mark.parametrize(
    "unit_file",
    ["deploy/systemd/aicc-worker.service", "deploy/systemd/voyn-aicc-worker@.service"],
)
def test_repo_unit_files_pass_systemd_analyze_verify(tmp_path, unit_file):
    src = (REPO_ROOT / unit_file).read_text()
    stubbed = re.sub(r"(?m)^Exec(Start|StartPre|Reload)=.*$", r"Exec\1=/bin/true", src)
    dest = tmp_path / Path(unit_file).name
    dest.write_text(stubbed)

    result = subprocess.run(
        ["systemd-analyze", "verify", str(dest)],
        check=False,
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- 2. Restart=always paces restarts against StartLimitBurst, and a clean
# `exit(0)` is not exempt from that pacing -- it restarts and can still trip
# the limit, the same way the worker's own success path cannot use exit(0) to
# take itself out of the fleet.
@skip_without_live_systemd
def test_restart_always_hits_start_limit_even_on_clean_exit(transient_unit):
    unit, _ = transient_unit(
        "restart",
        [
            "Restart=always",
            "RestartSec=1s",
            "StartLimitIntervalSec=30s",
            "StartLimitBurst=3",
        ],
        ["/bin/true"],
        wait=True,
        timeout=20,
    )
    state = _show(unit, "Result", "NRestarts")
    assert state["Result"] == "start-limit-hit"
    assert int(state["NRestarts"]) >= 3


# --- 3. KillMode is the difference between "the whole cgroup dies" and "a
# detached grandchild survives its parent's stop" -- the exact 2x2 the note
# describes, collapsed to its two informative cells since the other two
# (a process with no detached descendant, under either KillMode) are
# negative controls that both modes pass trivially.
@skip_without_live_systemd
@pytest.mark.parametrize(
    ("kill_mode", "orphan_survives"),
    [("control-group", False), ("process", True)],
)
def test_killmode_controls_whether_a_detached_grandchild_survives(
    tmp_path, transient_unit, kill_mode, orphan_survives
):
    pidfile = tmp_path / "detached.pid"
    script = tmp_path / "spawn_detached.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            setsid bash -c 'exec sleep 60' </dev/null >/dev/null 2>&1 &
            echo -n "$!" > {pidfile}
            exec sleep 60
            """
        )
    )
    script.chmod(0o755)

    unit, _ = transient_unit(
        f"killmode-{kill_mode}",
        [f"KillMode={kill_mode}"],
        [str(script)],
        wait=False,
        timeout=10,
    )
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        detached_pid = int(pidfile.read_text())

        subprocess.run(
            ["systemctl", "--user", "stop", f"{unit}.service"],
            check=False,
            env=_ENV,
            capture_output=True,
            timeout=10,
        )
        time.sleep(0.5)

        alive = _pid_alive(detached_pid)
        assert alive is orphan_survives
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --- 4. PR_SET_PDEATHSIG: a worker (registered against its own parent, the
# "one main pid" systemd tracks and signals) is SIGKILLed the instant that
# parent dies, even though only the parent was ever signalled directly. Pure
# Linux prctl semantics -- no systemd involved.
@skip_off_linux
def test_pdeathsig_kills_the_registering_process_when_its_parent_dies():
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    PR_SET_PDEATHSIG = 1

    read_fd, write_fd = os.pipe()
    main_pid = os.fork()
    if main_pid == 0:
        os.close(read_fd)
        worker_pid = os.fork()
        if worker_pid == 0:
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
            if os.getppid() == 1:
                os._exit(0)  # parent raced ahead and already exited
            os.close(write_fd)
            time.sleep(10)
            os._exit(0)
        os.write(write_fd, str(worker_pid).encode())
        os.close(write_fd)
        time.sleep(10)
        os._exit(0)

    os.close(write_fd)
    worker_pid = int(os.read(read_fd, 64).decode())
    os.close(read_fd)
    try:
        os.kill(main_pid, signal.SIGKILL)
        os.waitpid(main_pid, 0)
        deadline = time.monotonic() + 2
        while _pid_alive(worker_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _pid_alive(worker_pid)
    finally:
        if _pid_alive(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)


# --- 5. MemoryMax: a run that stays under the cgroup ceiling completes; one
# that crosses it is OOM-killed by the kernel, surfaced by systemd as
# Result=oom-kill -- not a graceful refusal, a kill.
@skip_without_live_systemd
def test_memory_max_kills_a_run_that_exceeds_it(tmp_path, transient_unit):
    script = tmp_path / "overshoot.py"
    script.write_text(_ALLOC_SCRIPT)
    unit, _ = transient_unit(
        "memover",
        ["MemoryMax=64M", "MemorySwapMax=0"],
        ["python3", str(script), "200"],
        timeout=20,
    )
    state = _show(unit, "Result")
    assert state["Result"] == "oom-kill"


@skip_without_live_systemd
def test_memory_max_allows_a_run_that_stays_under_it(tmp_path, transient_unit):
    script = tmp_path / "undershoot.py"
    script.write_text(_ALLOC_SCRIPT)
    unit, _ = transient_unit(
        "memunder",
        ["MemoryMax=64M", "MemorySwapMax=0"],
        ["python3", str(script), "20"],
        timeout=20,
    )
    state = _show(unit, "Result")
    assert state["Result"] == "success"


_ALLOC_SCRIPT = textwrap.dedent(
    """\
    import sys
    target_mb = int(sys.argv[1])
    buf = []
    for i in range(target_mb):
        b = bytearray(1024 * 1024)
        for j in range(0, len(b), 4096):
            b[j] = 1  # touch every page so it counts toward RSS
        buf.append(b)
    """
)


# --- 6. TasksMax: the cgroup task cap is enforced with EAGAIN on the fork()
# that would cross it, not a soft warning.
@skip_without_live_systemd
def test_tasks_max_returns_eagain_past_the_limit(tmp_path, transient_unit):
    script = tmp_path / "forkbomb.py"
    script.write_text(
        textwrap.dedent(
            """\
            import os, sys, time
            n = int(sys.argv[1])
            pids, errno_seen = [], None
            try:
                for _ in range(n):
                    pid = os.fork()
                    if pid == 0:
                        time.sleep(5)
                        os._exit(0)
                    pids.append(pid)
            except OSError as e:
                errno_seen = e.errno
            print(f"forked={len(pids)} errno={errno_seen}")
            for p in pids:
                try:
                    os.kill(p, 9)
                except ProcessLookupError:
                    pass
            """
        )
    )
    unit, _ = transient_unit(
        "tasksmax", ["TasksMax=4"], ["python3", str(script), "20"], timeout=20
    )
    lines = [e.get("MESSAGE", "") for e in _journal_json(unit)]
    reported = next((line for line in lines if line.startswith("forked=")), None)
    assert reported is not None, lines
    import errno as errno_module

    assert f"errno={errno_module.EAGAIN}" in reported, reported


# --- 7. LimitAS (RLIMIT_AS): an mmap() that would push the process's address
# space past the limit fails with ENOMEM; the same allocation succeeds under a
# generous limit. This is the one of these guarantees that does not hold on
# macOS -- proving it here is specifically a Linux claim.
@skip_without_live_systemd
def test_limit_as_blocks_an_allocation_that_exceeds_it(tmp_path, transient_unit):
    script = tmp_path / "mmap_probe.py"
    script.write_text(_MMAP_PROBE_SCRIPT)
    unit, _ = transient_unit(
        "limitas-over", ["LimitAS=32M"], ["python3", str(script), "64"], timeout=15
    )
    lines = [e.get("MESSAGE", "") for e in _journal_json(unit)]
    assert any("FAILED" in line for line in lines), lines


@skip_without_live_systemd
def test_limit_as_allows_an_allocation_within_it(tmp_path, transient_unit):
    script = tmp_path / "mmap_probe.py"
    script.write_text(_MMAP_PROBE_SCRIPT)
    unit, _ = transient_unit(
        "limitas-under", ["LimitAS=256M"], ["python3", str(script), "64"], timeout=15
    )
    lines = [e.get("MESSAGE", "") for e in _journal_json(unit)]
    assert any(": OK" in line for line in lines), lines


_MMAP_PROBE_SCRIPT = textwrap.dedent(
    """\
    import mmap, sys
    mb = int(sys.argv[1])
    try:
        m = mmap.mmap(-1, mb * 1024 * 1024)
        m[0:1] = b"x"
        m[-1:] = b"x"
        print(f"mmap {mb}MB: OK")
    except OSError as e:
        print(f"mmap {mb}MB: FAILED errno={e.errno}")
    """
)


# --- 8. seccomp (SystemCallFilter): denial by DAC alone (no filter) is a
# graceful EPERM the process can see and handle; a seccomp filter on the same
# syscall instead kills the process with SIGSYS. The filter is a harder
# boundary than the permission check it sits on top of.
@skip_without_live_systemd
def test_seccomp_filter_kills_with_sigsys_instead_of_returning_eperm(
    tmp_path, transient_unit
):
    script = tmp_path / "try_mount.py"
    script.write_text(
        textwrap.dedent(
            """\
            import ctypes, os
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            rc = libc.mount(b"none", b"/tmp", b"tmpfs", 0, None)
            if rc == 0:
                print("mount: unexpectedly succeeded")
            else:
                print(f"mount: failed errno={ctypes.get_errno()}")
            """
        )
    )

    baseline_unit, _ = transient_unit(
        "mount-baseline", [], ["python3", str(script)], timeout=15
    )
    baseline_lines = [e.get("MESSAGE", "") for e in _journal_json(baseline_unit)]
    assert any("errno=1" in line for line in baseline_lines), (
        baseline_lines
    )  # EPERM, no filter
    assert _show(baseline_unit, "Result")["Result"] == "success"

    filtered_unit, _ = transient_unit(
        "mount-seccomp",
        ["SystemCallFilter=~mount umount umount2"],
        ["python3", str(script)],
        timeout=15,
    )
    state = _show(filtered_unit, "Result", "ExecMainStatus")
    assert state["Result"] in ("core-dump", "signal")
    assert state["ExecMainStatus"] == str(signal.SIGSYS)


# --- 9. journald attaches fields the emitting process cannot forge (_PID,
# _UID, _GID, _SYSTEMD_UNIT, _SYSTEMD_INVOCATION_ID come from the kernel/
# systemd side of the socket, not parsed out of the message text).
@skip_without_live_systemd
def test_journald_trusted_fields_cannot_be_forged_from_stdout(transient_unit):
    unit, _ = transient_unit(
        "journal-fields",
        [],
        ["bash", "-c", 'echo "hello"; echo "_UID=0"'],
        timeout=15,
    )
    entries = _journal_json(unit)
    message_entries = [e for e in entries if e.get("MESSAGE") in ("hello", "_UID=0")]
    assert len(message_entries) == 2
    for entry in message_entries:
        # The real trusted _UID (this test's own uid) is untouched by a
        # message body that merely *contains* the text "_UID=0".
        assert entry["_UID"] == str(os.getuid())
        assert entry["_SYSTEMD_UNIT"] == f"user@{os.getuid()}.service"
        assert "_SYSTEMD_INVOCATION_ID" in entry


# --- 10. StateDirectory: the mode the unit declares (StateDirectoryMode) is
# the mode that lands on disk -- proven against the exact value
# aicc-worker.service declares, not a hardcoded assumption about it.
@skip_without_live_systemd
def test_state_directory_gets_the_mode_the_unit_declares(transient_unit):
    declared = _worker_unit_directive("StateDirectoryMode")
    assert declared, "aicc-worker.service no longer declares StateDirectoryMode"

    unit, _ = transient_unit(
        "statedir",
        [
            f"StateDirectory=aicc-srv05-verify-{uuid.uuid4().hex[:8]}",
            f"StateDirectoryMode={declared}",
        ],
        ["bash", "-c", 'stat -c "%a" "$STATE_DIRECTORY"'],
        timeout=15,
    )
    lines = [e.get("MESSAGE", "") for e in _journal_json(unit)]
    observed_mode = next(
        (line.strip() for line in lines if line.strip().isdigit()), None
    )
    # `stat -c %a` prints octal without a leading zero (e.g. "700" for 0700).
    assert observed_mode == declared.lstrip("0"), lines


def _worker_unit_directive(name: str) -> str | None:
    text = (REPO_ROOT / "deploy/systemd/aicc-worker.service").read_text()
    match = re.search(rf"(?m)^{name}=(.+)$", text)
    return match.group(1).strip() if match else None


# --- 11. Type=notify + WatchdogSec: a handler that stops petting the
# watchdog is killed with SIGABRT once the interval elapses, surfaced as
# Result=watchdog -- exactly the failure mode the worker's own heartbeat
# thread exists to avoid triggering.
@skip_without_live_systemd
def test_watchdog_aborts_a_handler_that_stops_petting_it(tmp_path, transient_unit):
    script = tmp_path / "notify_no_pet.py"
    script.write_text(
        textwrap.dedent(
            """\
            import socket, os, time
            sock_path = os.environ["NOTIFY_SOCKET"]
            if sock_path.startswith("@"):
                sock_path = "\\0" + sock_path[1:]
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.connect(sock_path)
            s.sendall(b"READY=1")
            time.sleep(10)  # never sends WATCHDOG=1 again
            """
        )
    )
    unit, _ = transient_unit(
        "watchdog",
        ["Type=notify", "WatchdogSec=2s"],
        ["python3", str(script)],
        timeout=15,
    )
    state = _show(unit, "Result", "ExecMainStatus")
    assert state["Result"] == "watchdog"
    assert state["ExecMainStatus"] == str(signal.SIGABRT)
