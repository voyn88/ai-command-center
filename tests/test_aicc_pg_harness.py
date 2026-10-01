"""scripts/aicc_pg_harness.sh, exercised against stubbed server binaries.

No real PostgreSQL install is required: the stubs implement just enough of
initdb/pg_ctl's observable contract (PG_VERSION marker, postmaster.pid,
exit codes) to prove the harness script's own logic, in particular that a
crashed instance recovers on the next `start` without a manual `stop` first
-- the defect an earlier version of this feature was rejected for.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "scripts" / "aicc_pg_harness.sh"
TIMEOUT = 15


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(0o755)


def _install_fake_pg_bin(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)

    _write_stub(
        bin_dir / "postgres",
        "exit 0\n",
    )

    _write_stub(
        bin_dir / "initdb",
        r"""
datadir=""
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
    case "${args[$i]}" in
        -D) datadir="${args[$((i+1))]}"; i=$((i+2)) ;;
        *) i=$((i+1)) ;;
    esac
done
if [ -e "${datadir}/PG_VERSION" ]; then
    echo "initdb: error: directory \"${datadir}\" exists but is not empty" >&2
    exit 1
fi
mkdir -p "${datadir}"
echo 17 > "${datadir}/PG_VERSION"
""",
    )

    _write_stub(
        bin_dir / "pg_ctl",
        r"""
cmd="$1"; shift
datadir=""
opts=""
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
    case "${args[$i]}" in
        -D) datadir="${args[$((i+1))]}"; i=$((i+2)) ;;
        -o) opts="${args[$((i+1))]}"; i=$((i+2)) ;;
        *) i=$((i+1)) ;;
    esac
done
# `|| true`: under `set -e -o pipefail`, `grep` finding no match (e.g. a
# `status`/`stop` invocation, which passes no `-o` and so has no "port=" to
# find) would otherwise abort this stub before it ever reaches the `case`
# below -- turning every `pg_ctl status` call into a silent, wrong "not
# running", regardless of whether the fake server is actually alive.
port="$(printf '%s\n' "${opts}" | grep -oE 'port=[0-9]+' | cut -d= -f2)" || true
pidfile="${datadir}/postmaster.pid"

is_alive() {
    [ -f "${pidfile}" ] && kill -0 "$(head -n1 "${pidfile}")" 2>/dev/null
}

case "${cmd}" in
    start)
        if [ ! -e "${datadir}/PG_VERSION" ]; then
            echo "pg_ctl: directory \"${datadir}\" does not exist" >&2
            exit 1
        fi
        if is_alive; then
            echo "pg_ctl: another server might be running" >&2
            exit 1
        fi
        rm -f "${pidfile}"
        # Redirected away from the inherited stdout/stderr: this listener
        # outlives pg_ctl's own exit (it runs until `stop` kills it), so if
        # it kept those fds open, whatever captured pg_ctl's/the harness's
        # output (e.g. Python's subprocess.run(capture_output=True)) would
        # block reading for EOF until this background process itself exits --
        # turning every `start` call into a hang until the test's timeout.
        python3 - "${port}" "${pidfile}" <<'PY' >/dev/null 2>&1 &
import os
import socket
import sys

port = int(sys.argv[1])
pidfile = sys.argv[2]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(5)
with open(pidfile, "w") as f:
    f.write(str(os.getpid()) + "\n")
while True:
    conn, _ = s.accept()
    conn.close()
PY
        disown 2>/dev/null || true
        for _ in $(seq 1 50); do
            [ -f "${pidfile}" ] && break
            sleep 0.1
        done
        if [ ! -f "${pidfile}" ]; then
            echo "pg_ctl: server did not start" >&2
            exit 1
        fi
        exit 0
        ;;
    stop)
        if ! is_alive; then
            exit 1
        fi
        kill "$(head -n1 "${pidfile}")" 2>/dev/null || true
        rm -f "${pidfile}"
        exit 0
        ;;
    status)
        is_alive
        exit $?
        ;;
    *)
        echo "pg_ctl: unknown command ${cmd}" >&2
        exit 1
        ;;
esac
""",
    )


def _run(state_dir: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AICC_PG_HARNESS_STATE": str(state_dir),
        "AICC_PG_HARNESS_TIMEOUT": "10",
    }
    return subprocess.run(
        [str(HARNESS), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT,
    )


def _postmaster_pid(state_dir: Path) -> int | None:
    pidfile = state_dir / "data" / "postmaster.pid"
    if not pidfile.exists():
        return None
    return int(pidfile.read_text().splitlines()[0])


@pytest.fixture()
def bin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    _install_fake_pg_bin(d)
    return d


@pytest.fixture()
def state_dir(tmp_path: Path, bin_dir: Path):
    d = tmp_path / "state"
    yield d
    # Best-effort teardown: stop via the script, then make sure no stray
    # listener survives a failed assertion mid-test.
    _run(d, bin_dir, "stop")
    pid = _postmaster_pid(d)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_start_initialises_and_prints_a_reachable_dsn(state_dir: Path, bin_dir: Path) -> None:
    result = _run(state_dir, bin_dir, "start")
    assert result.returncode == 0, result.stderr
    dsn = result.stdout.strip().splitlines()[-1]
    assert dsn.startswith("postgres://postgres:")
    assert (state_dir / "data" / "PG_VERSION").exists()
    assert _postmaster_pid(state_dir) is not None


def test_start_is_idempotent_when_already_running(state_dir: Path, bin_dir: Path) -> None:
    first = _run(state_dir, bin_dir, "start")
    assert first.returncode == 0, first.stderr
    pid_before = _postmaster_pid(state_dir)

    second = _run(state_dir, bin_dir, "start")
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip().splitlines()[-1] == first.stdout.strip().splitlines()[-1]
    assert _postmaster_pid(state_dir) == pid_before


def test_start_recovers_from_a_crash_without_a_manual_stop(state_dir: Path, bin_dir: Path) -> None:
    """The bug this feature was rejected for: a crash used to require a manual
    `stop` because `start` re-ran initdb against the surviving, non-empty
    data directory. It must now recover on its own.
    """
    first = _run(state_dir, bin_dir, "start")
    assert first.returncode == 0, first.stderr

    pid = _postmaster_pid(state_dir)
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("simulated crash victim did not die")
    # A crash leaves the stale pidfile behind; a clean `stop` would have
    # removed it. This is what `is_running` must see through.

    second = _run(state_dir, bin_dir, "start")
    assert second.returncode == 0, second.stderr
    dsn = second.stdout.strip().splitlines()[-1]
    assert dsn.startswith("postgres://postgres:")
    new_pid = _postmaster_pid(state_dir)
    assert new_pid is not None
    assert new_pid != pid


def test_stop_leaves_the_data_directory_for_a_fast_restart(state_dir: Path, bin_dir: Path) -> None:
    start = _run(state_dir, bin_dir, "start")
    assert start.returncode == 0, start.stderr

    stop = _run(state_dir, bin_dir, "stop")
    assert stop.returncode == 0, stop.stderr
    assert (state_dir / "data" / "PG_VERSION").exists()
    assert _postmaster_pid(state_dir) is None

    status_after_stop = _run(state_dir, bin_dir, "status")
    assert status_after_stop.returncode == 1

    restart = _run(state_dir, bin_dir, "start")
    assert restart.returncode == 0, restart.stderr


def test_status_reflects_running_and_stopped(state_dir: Path, bin_dir: Path) -> None:
    before = _run(state_dir, bin_dir, "status")
    assert before.returncode == 1

    start = _run(state_dir, bin_dir, "start")
    assert start.returncode == 0, start.stderr

    running = _run(state_dir, bin_dir, "status")
    assert running.returncode == 0
    assert "running" in running.stdout


def test_start_fails_clearly_without_server_binaries(tmp_path: Path) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {
        **os.environ,
        "PATH": f"{empty_bin}:/usr/bin:/bin",
        "AICC_PG_HARNESS_STATE": str(tmp_path / "state"),
        "AICC_PG_BINDIR": str(tmp_path / "does-not-exist"),
    }
    result = subprocess.run(
        [str(HARNESS), "start"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT,
    )
    assert result.returncode == 1
    assert "no PostgreSQL server binaries" in result.stderr
