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
    "AICC_BACKUP_AGE_RECIPIENT": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
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
    _run(BACKUP, "--out-dir", str(existing))  # fails later, at pg_dump
    assert existing.stat().st_mode == before


def test_restore_refuses_the_live_database_without_the_flag(tmp_path) -> None:
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"not-a-real-archive")
    result = _run(RESTORE, "--archive", str(archive), "--target-db", "aicc_live")
    assert result.returncode == 3
    assert "refusing to restore over the live database" in result.stderr


def test_restore_warns_loudly_when_no_checksum_is_present(tmp_path) -> None:
    """Integrity is not silently skipped: a missing sidecar is reported."""
    archive = tmp_path / "fake.dump"
    archive.write_bytes(b"not-a-real-archive")
    result = _run(RESTORE, "--archive", str(archive), "--target-db", "aicc_restore_check")
    assert "integrity not verified" in result.stderr


def test_restore_rejects_a_missing_archive(tmp_path) -> None:
    result = _run(RESTORE, "--archive", str(tmp_path / "absent.dump"), "--target-db", "x")
    assert result.returncode == 2
    assert "archive not found" in result.stderr


def test_restore_requires_an_identity_for_an_encrypted_archive(tmp_path) -> None:
    archive = tmp_path / "backup.dump.age"
    archive.write_bytes(b"encrypted")
    result = _run(RESTORE, "--archive", str(archive), "--target-db", "clean")
    assert result.returncode != 0
    assert "AICC_BACKUP_AGE_IDENTITY_FILE" in result.stderr


def test_backup_plaintext_partial_is_owner_only_from_creation() -> None:
    script = BACKUP.read_text()
    assert "umask 077" in script
    assert script.index("umask 077") < script.index('PGPASSWORD="$AICC_PG_PASSWORD" pg_dump')


@pytest.mark.skipif(
    not (shutil.which("pg_restore") and shutil.which("psql")),
    reason="PostgreSQL client binaries are not installed",
)
def test_restore_names_the_cause_when_the_checksum_does_not_match(tmp_path) -> None:
    """`sha256sum --check --status` is silent, so a bare `set -e` abort tells a
    recovering operator nothing. Bit rot must be named, and must stop the
    restore before the target database is created."""
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"corrupted archive contents")
    (tmp_path / "backup.dump.sha256").write_text(f"{'0' * 64}  {archive.name}\n")

    result = _run(RESTORE, "--archive", str(archive), "--target-db", "clean")

    assert result.returncode == 5
    assert "checksum mismatch" in result.stderr
    assert "refusing to restore a corrupted backup" in result.stderr
