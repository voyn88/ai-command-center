"""Guard rails in the backup/restore scripts, checked without a server.

These paths are the ones that destroy data when they go wrong, and none of them
needs a database to exercise — so they run everywhere, not only where
`AICC_TEST_PG_ADMIN_DSN` is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP = REPO_ROOT / "scripts" / "aicc_pg_backup.sh"
RESTORE = REPO_ROOT / "scripts" / "aicc_pg_restore.sh"

_ENV = {
    "AICC_PG_HOST": "127.0.0.1",
    "AICC_PG_DB": "aicc_live",
    "AICC_PG_USER": "aicc_migrator",
    "AICC_PG_PASSWORD": "irrelevant-for-these-checks",
}


def _run(script: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args],
        env={**os.environ, **_ENV, **(extra_env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_bin_dir(tmp_path: Path, *, client_version: str, server_version_num: str) -> Path:
    """A directory of stand-in `pg_dump`/`psql` binaries for the version guard.

    Fakes rather than real binaries so the version comparison is exercised
    without needing two PostgreSQL installations (or a live server) side by
    side — the exact drill setup (Homebrew pg_dump 15 against a pg 17 server)
    is what this guard exists for, and it is not something CI can install.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    pg_dump = bin_dir / "pg_dump"
    pg_dump.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--version" ]]; then\n'
        f'    echo "pg_dump (PostgreSQL) {client_version}"\n'
        "    exit 0\n"
        "fi\n"
        "for arg in \"$@\"; do\n"
        '    case "$arg" in\n'
        '        --file=*) echo "fake archive" > "${arg#--file=}" ;;\n'
        "    esac\n"
        "done\n"
        "exit 0\n"
    )
    pg_dump.chmod(0o755)

    psql = bin_dir / "psql"
    psql.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "{server_version_num}"\n'
        "exit 0\n"
    )
    psql.chmod(0o755)

    return bin_dir


# An empty `--keep ""` is rejected earlier, by the argument parser's `${2:?...}`
# (exit 1), so it is not part of this parametrisation.
@pytest.mark.parametrize("keep", ["abc", "0", "-3", "2.5"])
def test_backup_rejects_a_non_positive_keep(tmp_path, keep: str) -> None:
    """`tail -n +$((KEEP+1))` treats a non-numeric value as 0 — and deletes everything.

    Without validation, `--keep $RETENTION` with `RETENTION` unset prunes every
    archive in the directory, including the one just written, and still exits 0.
    """
    result = _run(BACKUP, "--out-dir", str(tmp_path / "backups"), "--keep", keep)
    assert result.returncode == 2
    assert "--keep" in result.stderr


def test_backup_leaves_an_existing_directory_permissions_alone(tmp_path) -> None:
    """An operator-managed shared backup directory must not be re-permissioned."""
    existing = tmp_path / "backups"
    existing.mkdir(mode=0o755)
    before = existing.stat().st_mode
    _run(BACKUP, "--out-dir", str(existing))  # fails later, at the version check or pg_dump
    assert existing.stat().st_mode == before


def test_backup_rejects_a_pg_dump_client_older_than_the_server(tmp_path) -> None:
    """The version check runs — and fails loudly — before pg_dump ever connects.

    This is the drill scenario the task was filed from: a Homebrew pg_dump
    15.18 against a pg 17.6 server. Left unchecked, pg_dump discovers the
    mismatch on its own, but only after it has already connected and started
    reading — on an unattended nightly run that reads as a silently failed
    backup, not a clear error. Fake `pg_dump`/`psql` stand in for a second
    PostgreSQL install so the comparison is exercised without one.
    """
    fake_bin = _fake_bin_dir(tmp_path, client_version="15.18", server_version_num="170006")
    out_dir = tmp_path / "backups"
    result = _run(
        BACKUP,
        "--out-dir",
        str(out_dir),
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode == 3
    assert "15.18" in result.stderr
    assert "17" in result.stderr
    # The guard fires before the output directory — or any archive — is created.
    assert not out_dir.exists()


def test_backup_proceeds_when_the_client_is_not_older_than_the_server(tmp_path) -> None:
    """A client at or ahead of the server's major version clears the guard."""
    fake_bin = _fake_bin_dir(tmp_path, client_version="17.2", server_version_num="170004")
    out_dir = tmp_path / "backups"
    result = _run(
        BACKUP,
        "--out-dir",
        str(out_dir),
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert list(out_dir.glob("aicc-aicc_live-*.dump"))


def test_restore_refuses_the_live_database_without_the_flag(tmp_path) -> None:
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"not-a-real-archive")
    result = _run(RESTORE, "--archive", str(archive), "--target-db", "aicc_live")
    assert result.returncode == 3
    assert "refusing to restore over the live database" in result.stderr


@pytest.mark.skipif(
    not (shutil.which("pg_restore") and shutil.which("psql")),
    reason="PostgreSQL client binaries are not installed",
)
def test_restore_warns_loudly_when_no_checksum_is_present(tmp_path) -> None:
    """Integrity is not silently skipped: a missing sidecar is reported.

    The script checks for `pg_restore`/`psql` on `PATH` before it ever reaches
    the checksum warning, so a host missing either tool exits earlier with
    `"pg_restore not found in PATH"` / `"psql not found in PATH"` instead —
    the same host-dependent-marker shape `test_delivery_tooling.py` was fixed
    for, on a host this suite's own CI lockfile does not describe. Skipped
    rather than asserted around: this test's subject is the checksum warning,
    not the tool-presence guard, which has no coverage of its own here.
    """
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"not-a-real-archive")
    result = _run(RESTORE, "--archive", str(archive), "--target-db", "aicc_restore_check")
    assert "integrity not verified" in result.stderr


def test_restore_rejects_a_missing_archive(tmp_path) -> None:
    result = _run(RESTORE, "--archive", str(tmp_path / "absent.dump"), "--target-db", "x")
    assert result.returncode == 2
    assert "archive not found" in result.stderr


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def test_backup_invokes_pg_dump_without_owner_or_privileges(tmp_path) -> None:
    """`--no-owner --no-privileges` is what makes an archive restorable onto a
    cluster with different role names (dev, DR, a drill database) instead of
    carrying the backup role's identity with it — see the portability note in
    `aicc_pg_restore.sh`. A future edit that drops either flag silently breaks
    that, and nothing else in this suite would notice.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "pg_dump.args"
    _write_stub(
        fake_bin / "pg_dump",
        f'printf "%s\\n" "$@" > "{log}"\n'
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        '        --file=*) : > "${arg#--file=}" ;;\n'
        '    esac\n'
        'done\n',
    )
    env = {**os.environ, **_ENV, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        [str(BACKUP), "--out-dir", str(tmp_path / "backups")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    args = log.read_text().splitlines()
    assert "--no-owner" in args, result.stderr
    assert "--no-privileges" in args, result.stderr


def test_restore_invokes_pg_restore_without_owner_or_privileges(tmp_path) -> None:
    """Same portability property, verified on the read side of the same trade.

    `pg_restore --no-owner --no-privileges` is what lets the operator hand
    ownership to the target cluster's own roles afterward (the NOTICE this
    script prints); without it, restoring a live-cluster archive elsewhere
    fails on roles that do not exist there.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_stub(fake_bin / "psql", 'echo 5\nexit 0\n')
    log = tmp_path / "pg_restore.args"
    _write_stub(fake_bin / "pg_restore", f'printf "%s\\n" "$@" > "{log}"\nexit 0\n')
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"not-a-real-archive")
    env = {**os.environ, **_ENV, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        [str(RESTORE), "--archive", str(archive), "--target-db", "aicc_restore_check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    args = log.read_text().splitlines()
    assert "--no-owner" in args, result.stderr
    assert "--no-privileges" in args, result.stderr
