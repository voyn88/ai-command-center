"""Repository tier for the Silent Audit Simulator's one table
(``silent_audit_result``): the persisted record that a silent, sandboxed audit
pass (:func:`command_center.audit.silent.run_silent_audit`) was attempted for a
given candidate sha.

This is the evidence store :func:`command_center.audit.silent.evaluate_silent_audit_coverage`
needs to turn from a pure function taking two sha collections into something
that can actually answer "does the last N changes' silent-audit coverage meet
the 90% bar" against real data. Recording is first-write-wins per
``candidate_sha`` (see :func:`record_silent_audit_result`): the acceptance bar
only asks whether a pass was ever attempted for a change, so a retried pass for
the same sha is a no-op rather than a second row or an overwrite.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``), matching every other table-family
module in this package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import command_center.runtime.db as db  # facade (late-bound; see docstring)


_SILENT_AUDIT_COLUMNS: tuple[str, ...] = (
    "id",
    "candidate_sha",
    "project",
    "ok",
    "checks_json",
    "finding_count",
    "deduped",
    "error",
    "started_at",
    "completed_at",
    "created_at",
)


def record_silent_audit_result(
    db_path: Path,
    *,
    candidate_sha: str,
    project: str,
    ok: bool,
    checks: Iterable[str] | None = None,
    finding_count: int = 0,
    deduped: int = 0,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> dict:
    """Record one silent-audit pass for ``candidate_sha`` and return the stored
    row (the first one ever recorded for this sha, per the unique constraint —
    a later call for the same sha returns that first row unchanged, since a
    change has *a* silent audit result the moment one exists, not the most
    recent one)."""
    if not candidate_sha or not candidate_sha.strip():
        raise ValueError("silent_audit_result.candidate_sha must be non-empty")
    if not project or not project.strip():
        raise ValueError("silent_audit_result.project must be non-empty")
    record = {
        "id": db.new_id(),
        "candidate_sha": candidate_sha,
        "project": project,
        "ok": 1 if ok else 0,
        "checks_json": json.dumps(list(checks or []), ensure_ascii=False),
        "finding_count": finding_count,
        "deduped": deduped,
        "error": error,
        "started_at": started_at,
        "completed_at": completed_at,
        "created_at": db.iso_now(),
    }
    columns = ", ".join(_SILENT_AUDIT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _SILENT_AUDIT_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO silent_audit_result "
                f"({columns}) VALUES ({placeholders})",
                record,
            )
    stored = get_silent_audit_result(db_path, candidate_sha)
    assert stored is not None  # the row above (or an earlier one) always exists now
    return stored


def get_silent_audit_result(db_path: Path, candidate_sha: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM silent_audit_result WHERE candidate_sha = ?",
            (candidate_sha,),
        ).fetchone()
        return _decode_silent_audit_row(dict(row)) if row is not None else None


def audited_shas_among(db_path: Path, shas: Iterable[str]) -> set[str]:
    """Which of ``shas`` already carry a recorded silent-audit result — the
    evidence :func:`command_center.audit.silent.evaluate_silent_audit_coverage`
    needs as its ``audited_shas`` argument to score real coverage."""
    wanted = list(dict.fromkeys(sha for sha in shas if sha))
    if not wanted:
        return set()
    placeholders = ", ".join("?" for _ in wanted)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT candidate_sha FROM silent_audit_result "
            f"WHERE candidate_sha IN ({placeholders})",
            wanted,
        ).fetchall()
        return {row["candidate_sha"] for row in rows}


def _decode_silent_audit_row(row: dict) -> dict:
    out = dict(row)
    raw = out.pop("checks_json", "[]")
    out["checks"] = json.loads(raw) if raw else []
    out["ok"] = bool(out["ok"])
    return out
