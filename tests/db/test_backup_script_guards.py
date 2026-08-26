"""Guard rails in the backup/restore scripts, checked without a server.

These paths are the ones that destroy data when they go wrong, and none of them
needs a database to exercise — so they run everywhere, not only where
`AICC_TEST_PG_ADMIN_DSN` is set.
"""

from __future__ import annotations

import os
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
