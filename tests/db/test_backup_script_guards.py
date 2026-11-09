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
PITR_PREPARE = REPO_ROOT / "scripts" / "aicc_pg_pitr_prepare.sh"

_ENV = {
    "AICC_PG_HOST": "127.0.0.1",
    "AICC_PG_DB": "aicc_live",
    "AICC_PG_USER": "aicc_migrator",
    "AICC_PG_PASSWORD": "irrelevant-for-these-checks",
}


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args],
        env={**os.environ, **_ENV},
        capture_output=True,
        text=True,
        check=False,
    )


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
    _run(BACKUP, "--out-dir", str(existing))  # fails later, at psql/pg_dump
    assert existing.stat().st_mode == before


def test_backup_refuses_client_older_than_server(tmp_path) -> None:
    """A Homebrew pg15 host against a pg17 server must fail before pg_dump runs."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "pg_dump.invoked"
    _write_stub(
        fake_bin / "pg_dump",
        'if [[ "${1:-}" == "--version" ]]; then\n'
        '    echo "pg_dump (PostgreSQL) 15.18"\n'
        "    exit 0\n"
        "fi\n"
        f'touch "{log}"\n'
        "exit 0\n",
    )
    _write_stub(fake_bin / "psql", 'echo "170006"\n')
    env = {**os.environ, **_ENV, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        [str(BACKUP), "--out-dir", str(tmp_path / "backups")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "15" in result.stderr and "17" in result.stderr
    assert not log.exists()


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


def test_pitr_prepare_refuses_a_nonempty_target_before_verification(tmp_path) -> None:
    base = tmp_path / "base"
    wal = tmp_path / "wal"
    target = tmp_path / "live"
    base.mkdir()
    wal.mkdir()
    target.mkdir()
    (base / "backup_manifest").write_text("manifest")
    marker = target / "must-survive"
    marker.write_text("live data")

    result = subprocess.run(
        [str(PITR_PREPARE), "--base", str(base), "--wal-dir", str(wal),
         "--target-dir", str(target), "--target-time", "2026-11-04T14:30:00Z"],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 3
    assert marker.read_text() == "live data"


@pytest.mark.parametrize("value", ["yesterday", "2026-11-04 14:30:00", "x'\nshared_preload_libraries=x"])
def test_pitr_prepare_rejects_ambiguous_or_injectable_target_time(tmp_path, value: str) -> None:
    base = tmp_path / "base"
    wal = tmp_path / "wal"
    base.mkdir()
    wal.mkdir()
    (base / "backup_manifest").write_text("manifest")
    result = subprocess.run(
        [str(PITR_PREPARE), "--base", str(base), "--wal-dir", str(wal),
         "--target-dir", str(tmp_path / "target"), "--target-time", value],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert not (tmp_path / "target").exists()


def test_pitr_prepare_verifies_then_writes_recovery_configuration(tmp_path) -> None:
    base = tmp_path / "base"
    wal = tmp_path / "wal"
    target = tmp_path / "target"
    fake_bin = tmp_path / "bin"
    base.mkdir(); wal.mkdir(); fake_bin.mkdir()
    (base / "backup_manifest").write_text("manifest")
    (base / "PG_VERSION").write_text("17")
    _write_stub(fake_bin / "pg_verifybackup", "exit 0")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = subprocess.run(
        [str(PITR_PREPARE), "--base", str(base), "--wal-dir", str(wal),
         "--target-dir", str(target), "--target-time", "2026-11-04T14:30:00Z"],
        env=env, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    config = (target / "postgresql.auto.conf").read_text()
    assert f'restore_command = \'cp "{wal}/%f" "%p"\'' in config
    assert "recovery_target_time = '2026-11-04T14:30:00Z'" in config
    assert "recovery_target_action = 'pause'" in config
    assert (target / "recovery.signal").exists()


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
        'if [[ "${1:-}" == "--version" ]]; then\n'
        '    echo "pg_dump (PostgreSQL) 17.2"\n'
        "    exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        '        --file=*) : > "${arg#--file=}" ;;\n'
        '    esac\n'
        'done\n',
    )
    _write_stub(fake_bin / "psql", 'echo "170006"\n')
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
