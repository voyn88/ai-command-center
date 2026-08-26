"""Guard rails in the WAL-archiving/PITR scripts, checked without a server.

Same rationale as test_backup_script_guards.py: these are the paths that
would silently corrupt or overwrite something if they went wrong, and none of
them needs a running PostgreSQL server to exercise.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_BACKUP = REPO_ROOT / "scripts" / "aicc_pg_base_backup.sh"
PITR_RESTORE = REPO_ROOT / "scripts" / "aicc_pg_pitr_restore.sh"

_BACKUP_ENV = {
    "AICC_PG_HOST": "127.0.0.1",
    "AICC_PG_DB": "aicc_live",
    "AICC_PG_USER": "postgres",
    "AICC_PG_PASSWORD": "irrelevant-for-these-checks",
}


def _run(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script), *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_base_backup(tmp_path: Path) -> Path:
    """A directory shaped enough like `pg_basebackup` output to pass the checks."""
    backup = tmp_path / "fake-basebackup"
    backup.mkdir()
    (backup / "PG_VERSION").write_text("16")
    (backup / "backup_label").write_text("START WAL LOCATION: 0/2000028\n")
    (backup / "backup_manifest").write_text("{}")
    return backup


# An empty `--keep ""` is rejected earlier, by the argument parser's `${2:?...}`
# (exit 1), so it is not part of this parametrisation.
@pytest.mark.parametrize("keep", ["abc", "0", "-3", "2.5"])
def test_base_backup_rejects_a_non_positive_keep(tmp_path, keep: str) -> None:
    result = _run(
        BASE_BACKUP, "--out-dir", str(tmp_path / "backups"), "--keep", keep, env=_BACKUP_ENV
    )
    assert result.returncode == 2
    assert "--keep" in result.stderr


def test_base_backup_leaves_an_existing_directory_permissions_alone(tmp_path) -> None:
    existing = tmp_path / "backups"
    existing.mkdir(mode=0o755)
    before = existing.stat().st_mode
    _run(BASE_BACKUP, "--out-dir", str(existing), env=_BACKUP_ENV)  # fails later, at pg_basebackup
    assert existing.stat().st_mode == before


def test_base_backup_requires_out_dir() -> None:
    result = _run(BASE_BACKUP, env=_BACKUP_ENV)
    assert result.returncode == 2
    assert "--out-dir" in result.stderr


def test_pitr_restore_requires_all_of_its_arguments(tmp_path) -> None:
    result = _run(PITR_RESTORE)
    assert result.returncode == 2
    assert "--base-backup" in result.stderr


def test_pitr_restore_rejects_a_non_numeric_port(tmp_path) -> None:
    backup = _fake_base_backup(tmp_path)
    result = _run(
        PITR_RESTORE,
        "--base-backup", str(backup),
        "--wal-archive", str(tmp_path / "archive"),
        "--target-dir", str(tmp_path / "target"),
        "--port", "not-a-port",
    )
    assert result.returncode == 2
    assert "--port" in result.stderr


def test_pitr_restore_rejects_a_directory_that_is_not_a_base_backup(tmp_path) -> None:
    not_a_backup = tmp_path / "not-a-backup"
    not_a_backup.mkdir()
    result = _run(
        PITR_RESTORE,
        "--base-backup", str(not_a_backup),
        "--wal-archive", str(tmp_path / "archive"),
        "--target-dir", str(tmp_path / "target"),
        "--port", "55432",
    )
    assert result.returncode == 2
    assert "not a base backup" in result.stderr


def test_pitr_restore_rejects_a_missing_wal_archive(tmp_path) -> None:
    backup = _fake_base_backup(tmp_path)
    result = _run(
        PITR_RESTORE,
        "--base-backup", str(backup),
        "--wal-archive", str(tmp_path / "does-not-exist"),
        "--target-dir", str(tmp_path / "target"),
        "--port", "55432",
    )
    assert result.returncode == 2
    assert "WAL archive directory not found" in result.stderr


def test_pitr_restore_rejects_an_empty_wal_archive(tmp_path) -> None:
    backup = _fake_base_backup(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    result = _run(
        PITR_RESTORE,
        "--base-backup", str(backup),
        "--wal-archive", str(archive),
        "--target-dir", str(tmp_path / "target"),
        "--port", "55432",
    )
    assert result.returncode == 2
    assert "WAL archive is empty" in result.stderr


def test_pitr_restore_refuses_a_non_empty_target_dir_without_the_flag(tmp_path) -> None:
    """The guard that stops a PITR drill from being pointed at the live PGDATA."""
    backup = _fake_base_backup(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "000000010000000000000001").write_bytes(b"not-real-wal")
    target = tmp_path / "target"
    target.mkdir()
    (target / "something").write_text("not empty")

    result = _run(
        PITR_RESTORE,
        "--base-backup", str(backup),
        "--wal-archive", str(archive),
        "--target-dir", str(target),
        "--port", "55432",
    )
    assert result.returncode == 3
    assert "refusing to restore into a non-empty directory" in result.stderr
    # The guard must fire before anything is touched.
    assert (target / "something").exists()
