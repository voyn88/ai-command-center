"""`scripts/aicc_pg_harness.sh` (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).

`initdb` and `pg_ctl` are stubbed rather than run for real: the property under
test is the harness's own control flow (when it reinitializes vs. recovers an
existing data directory), not PostgreSQL's, and a host running this suite is
not guaranteed to have the server package installed at all.

A prior version of `start` unconditionally ran `initdb` against `data/`.
`initdb` refuses a nonempty directory, so once the server process died
without going through `stop` -- a crash, an OOM kill, a `pytest` run that
never reached teardown -- the data directory was left behind and every
following `start` failed, requiring a manual `stop` first even though `start`
looks idempotent (review on
https://github.com/voyn88/ai-command-center/pull/568). The stub `initdb`
below reproduces the real refusal (`exit 1` against a directory that already
has `PG_VERSION`) so a regression here fails the same way the real binary
would.

`REPO_ROOT`-relative behaviour aside, `find_bindir` searches a hardcoded
`/usr/lib/postgresql/*/bin` glob rather than PATH, so each test patches a
copy of the script the same way `tests/test_check_postgres_host.py` does.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SCRIPT = REPO_ROOT / "scripts" / "aicc_pg_harness.sh"

_FAKE_INITDB = """#!/usr/bin/env bash
set -e
data=""
prev=""
for a in "$@"; do
    if [ "$prev" = "-D" ]; then data="$a"; fi
    prev="$a"
done
if [ -f "$data/PG_VERSION" ]; then
    echo "initdb: error: directory \\"$data\\" exists but is not empty" >&2
    exit 1
fi
mkdir -p "$data"
echo 16 >"$data/PG_VERSION"
exit 0
"""

_FAKE_PG_CTL = """#!/usr/bin/env bash
set -e
data=""
mode=""
args=("$@")
i=0
n=${#args[@]}
while [ "$i" -lt "$n" ]; do
    case "${args[$i]}" in
        -D) data="${args[$((i + 1))]}"; i=$((i + 2)) ;;
        -l|-o|-m) i=$((i + 2)) ;;
        -w) i=$((i + 1)) ;;
        start|stop|status) mode="${args[$i]}"; i=$((i + 1)) ;;
        *) i=$((i + 1)) ;;
    esac
done
case "$mode" in
    start) mkdir -p "$data"; touch "$data/postmaster.pid"; exit 0 ;;
    stop) rm -f "$data/postmaster.pid"; exit 0 ;;
    status) [ -f "$data/postmaster.pid" ] && exit 0 || exit 1 ;;
    *) echo "unexpected pg_ctl invocation: $*" >&2; exit 1 ;;
esac
"""

_FAKE_POSTGRES = "#!/usr/bin/env bash\nexit 0\n"


def _write(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _fake_script(tmp_path: Path) -> Path:
    """A copy of the real script with its bindir glob redirected to a stub cluster."""
    bin_dir = tmp_path / "pg-server" / "16" / "bin"
    bin_dir.mkdir(parents=True)
    _write(bin_dir / "initdb", _FAKE_INITDB)
    _write(bin_dir / "pg_ctl", _FAKE_PG_CTL)
    _write(bin_dir / "postgres", _FAKE_POSTGRES)

    glob_root = tmp_path / "pg-server" / "*" / "bin"
    script_text = REAL_SCRIPT.read_text().replace("/usr/lib/postgresql/*/bin", str(glob_root))
    patched = tmp_path / "aicc_pg_harness.sh"
    _write(patched, script_text)
    return patched


def _run(
    script: Path, *args: str, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args], env=env or os.environ.copy(), capture_output=True, text=True
    )


def _env(tmp_path: Path) -> dict:
    return {**os.environ, "AICC_PG_HARNESS_STATE": str(tmp_path / "state")}


def test_fresh_start_initializes_and_reports_a_dsn(tmp_path) -> None:
    script = _fake_script(tmp_path)
    env = _env(tmp_path)

    result = _run(script, "start", env=env)

    assert result.returncode == 0, result.stderr
    assert "export AICC_TEST_PG_ADMIN_DSN=" in result.stdout
    assert (tmp_path / "state" / "data" / "PG_VERSION").exists()


def test_start_after_a_crash_recovers_the_existing_cluster(tmp_path) -> None:
    """The exact defect under review: `start` after a crash used to require a
    manual `stop` first because it reran `initdb` against the surviving,
    nonempty `data/` directory."""
    script = _fake_script(tmp_path)
    env = _env(tmp_path)

    first = _run(script, "start", env=env)
    assert first.returncode == 0, first.stderr
    pw_before = (tmp_path / "state" / "pw").read_text()

    # Simulate a crash: the server process is gone but nothing ran `stop`,
    # so `data/` (and its PG_VERSION) survives.
    (tmp_path / "state" / "data" / "postmaster.pid").unlink()

    second = _run(script, "start", env=env)

    assert second.returncode == 0, second.stderr
    assert "export AICC_TEST_PG_ADMIN_DSN=" in second.stdout
    # Recovered the existing cluster rather than reinitializing it.
    assert (tmp_path / "state" / "pw").read_text() == pw_before
    assert (tmp_path / "state" / "data" / "postmaster.pid").exists()


def test_start_while_already_running_is_a_no_op(tmp_path) -> None:
    script = _fake_script(tmp_path)
    env = _env(tmp_path)

    first = _run(script, "start", env=env)
    second = _run(script, "start", env=env)

    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout


def test_stop_removes_all_state(tmp_path) -> None:
    script = _fake_script(tmp_path)
    env = _env(tmp_path)

    _run(script, "start", env=env)
    result = _run(script, "stop", env=env)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state").exists()
