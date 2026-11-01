"""AML SAR Store — Suspicious Activity Report filing workflow.

Covers 115-ФЗ STR (ст. 7) and CTR (ст. 6) submission to Росфинмониторинг.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from command_center import storage

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1

# SAR types
SAR_TYPES: tuple[str, ...] = (
    "str",          # Suspicious Transaction Report (ст. 7 115-ФЗ)
    "ctr",          # Currency Transaction Report (ст. 6 115-ФЗ)
    "pep",          # PEP-related report
    "sanctions",    # Sanctions-related report
    "other",
)

# Filing deadlines in working days (115-ФЗ)
_FILING_DEADLINES_DAYS: dict[str, int] = {
    "str": 3,       # ст. 7 — 3 working days
    "ctr": 1,       # ст. 6 — next working day
    "pep": 3,
    "sanctions": 1,
    "other": 3,
}

SAR_STATES: tuple[str, ...] = (
    "draft",
    "under_review",
    "approved",
    "submitted",
    "acknowledged",
    "rejected",
)

# Transitions map
_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"under_review"}),
    "under_review": frozenset({"approved", "rejected", "draft"}),
    "approved": frozenset({"submitted", "draft"}),
    "submitted": frozenset({"acknowledged", "rejected"}),
    "acknowledged": frozenset(),
    "rejected": frozenset({"draft"}),
}

# Subject types and roles
SUBJECT_TYPES: tuple[str, ...] = ("individual", "legal", "beneficiary", "correspondent")
SUBJECT_ROLES: tuple[str, ...] = ("primary_subject", "related_party", "beneficiary", "witness")
ID_TYPES: tuple[str, ...] = ("passport", "inn", "ogrn", "driver_license", "other")

# Roles that can approve and submit
_APPROVER_ROLES: frozenset[str] = frozenset({"ComplianceOfficer", "MLRO"})


class SarStoreError(Exception):
    pass


class SarNotFound(SarStoreError):
    pass


class InvalidTransition(SarStoreError):
    pass


class PermissionDenied(SarStoreError):
    pass


class MissingRequiredField(SarStoreError):
    pass


class NarrativeLocked(SarStoreError):
    pass


class LostUpdate(SarStoreError):
    pass


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "aml_sars.db"


@contextmanager
def _db(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS sars (
                id TEXT PRIMARY KEY,
                sar_number TEXT NOT NULL UNIQUE,
                sar_type TEXT NOT NULL CHECK(sar_type IN ('str','ctr','pep','sanctions','other')),
                state TEXT NOT NULL DEFAULT 'draft'
                    CHECK(state IN ('draft','under_review','approved',
                                    'submitted','acknowledged','rejected')),
                case_id TEXT,
                narrative TEXT,
                filing_deadline TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                submitted_at TEXT,
                submission_ref TEXT,
                acknowledged_at TEXT,
                acknowledgement_ref TEXT,
                rejected_at TEXT,
                rejection_reason TEXT,
                approved_by TEXT,
                submitted_by TEXT,
                schema_version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS sar_subjects (
                id TEXT PRIMARY KEY,
                sar_id TEXT NOT NULL REFERENCES sars(id),
                subject_type TEXT NOT NULL,
                subject_role TEXT NOT NULL,
                name TEXT NOT NULL,
                id_type TEXT,
                id_number TEXT,
                nationality TEXT,
                address TEXT,
                account_number TEXT,
                added_by TEXT NOT NULL,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sar_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sar_id TEXT NOT NULL REFERENCES sars(id),
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                detail TEXT,
                occurred_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sars_state ON sars(state);
            CREATE INDEX IF NOT EXISTS idx_sars_case ON sars(case_id);
            CREATE INDEX IF NOT EXISTS idx_sar_subjects_sar ON sar_subjects(sar_id);

            PRAGMA user_version = 1;
            COMMIT;
            """
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _filing_deadline(sar_type: str) -> str:
    days = _FILING_DEADLINES_DAYS.get(sar_type, 3)
    # Simple calendar days (working-day calculation requires holiday calendar)
    deadline = datetime.now(UTC) + timedelta(days=days)
    return deadline.isoformat()


def _next_sar_number(conn: sqlite3.Connection) -> str:
    year = datetime.now(UTC).year
    row = conn.execute(
        "SELECT COUNT(*) FROM sars WHERE sar_number LIKE ?", (f"SAR-{year}-%",)
    ).fetchone()
    n = row[0] + 1
    return f"SAR-{year}-{n:05d}"


def _audit(
    conn: sqlite3.Connection,
    sar_id: str,
    *,
    actor: str,
    action: str,
    from_state: str | None = None,
    to_state: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO sar_audit_log(sar_id,actor,action,from_state,to_state,detail,occurred_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (sar_id, actor, action, from_state, to_state, detail, _utcnow()),
    )


def _transition(
    conn: sqlite3.Connection,
    sar_id: str,
    *,
    actor: str,
    to_state: str,
    extra_fields: dict | None = None,
    expected_state: str | None = None,
) -> dict:
    row = conn.execute("SELECT * FROM sars WHERE id=?", (sar_id,)).fetchone()
    if row is None:
        raise SarNotFound(sar_id)
    sar = dict(row)

    if expected_state is not None and sar["state"] != expected_state:
        raise LostUpdate(f"Expected state {expected_state!r}, found {sar['state']!r}")

    allowed = _TRANSITIONS.get(sar["state"], frozenset())
    if to_state not in allowed:
        raise InvalidTransition(
            f"Cannot move SAR from {sar['state']!r} to {to_state!r}"
        )

    updates: dict = {"state": to_state, "updated_at": _utcnow()}
    if extra_fields:
        updates.update(extra_fields)

    set_clause = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE sars SET {set_clause} WHERE id=?",  # nosec B608 - set_clause keys come only from "state"/"updated_at" plus hardcoded extra_fields keys passed by module-private callers; values are bound via params
        (*updates.values(), sar_id),
    )
    _audit(conn, sar_id, actor=actor,
           action=f"transition:{sar['state']}→{to_state}",
           from_state=sar["state"], to_state=to_state)
    return dict(conn.execute("SELECT * FROM sars WHERE id=?", (sar_id,)).fetchone())


# ---------------------------------------------------------------------------
# Public API — CRUD
# ---------------------------------------------------------------------------


def create_sar(
    db_path: Path,
    *,
    sar_type: str,
    narrative: str | None = None,
    case_id: str | None = None,
    created_by: str,
) -> dict:
    if sar_type not in SAR_TYPES:
        raise SarStoreError(f"invalid sar_type {sar_type!r}; valid: {SAR_TYPES}")

    sar_id = str(uuid.uuid4())
    now = _utcnow()
    deadline = _filing_deadline(sar_type)

    with _db(db_path) as conn:
        sar_number = _next_sar_number(conn)
        conn.execute(
            """
            INSERT INTO sars(id, sar_number, sar_type, state, case_id, narrative,
                             filing_deadline, created_by, created_at, updated_at, schema_version)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (sar_id, sar_number, sar_type, "draft", case_id, narrative,
             deadline, created_by, now, now, SCHEMA_VERSION),
        )
        _audit(conn, sar_id, actor=created_by, action="created",
               from_state=None, to_state="draft",
               detail=f"type={sar_type}, case_id={case_id}")
        return dict(conn.execute("SELECT * FROM sars WHERE id=?", (sar_id,)).fetchone())


def get_sar(db_path: Path, sar_id: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM sars WHERE id=?", (sar_id,)).fetchone()
    if row is None:
        raise SarNotFound(sar_id)
    return dict(row)


def get_sar_by_number(db_path: Path, sar_number: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM sars WHERE sar_number=?", (sar_number,)).fetchone()
    if row is None:
        raise SarNotFound(sar_number)
    return dict(row)


def list_sars(
    db_path: Path,
    *,
    state: str | None = None,
    sar_type: str | None = None,
    case_id: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if sar_type is not None:
        clauses.append("sar_type = ?")
        params.append(sar_type)
    if case_id is not None:
        clauses.append("case_id = ?")
        params.append(case_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM sars {where} ORDER BY created_at DESC", params  # nosec B608 - clauses is built only from hardcoded literal fragments ("state = ?", "sar_type = ?", "case_id = ?"); actual values passed via params list
        ).fetchall()
    return [dict(r) for r in rows]


def update_narrative(
    db_path: Path,
    sar_id: str,
    *,
    narrative: str,
    actor: str,
) -> dict:
    """Update narrative — only allowed in draft or under_review states."""
    if not narrative.strip():
        raise MissingRequiredField("narrative must not be blank")
    with _db(db_path) as conn:
        row = conn.execute("SELECT state FROM sars WHERE id=?", (sar_id,)).fetchone()
        if row is None:
            raise SarNotFound(sar_id)
        if row["state"] not in ("draft", "under_review"):
            raise NarrativeLocked(
                f"Narrative cannot be edited in state {row['state']!r}"
            )
        conn.execute(
            "UPDATE sars SET narrative=?, updated_at=? WHERE id=?",
            (narrative.strip(), _utcnow(), sar_id),
        )
        _audit(conn, sar_id, actor=actor, action="narrative_updated")
        return dict(conn.execute("SELECT * FROM sars WHERE id=?", (sar_id,)).fetchone())


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def submit_for_review(
    db_path: Path,
    sar_id: str,
    *,
    actor: str,
    expected_state: str | None = None,
) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT narrative FROM sars WHERE id=?", (sar_id,)).fetchone()
        if row is None:
            raise SarNotFound(sar_id)
        if not row["narrative"] or not row["narrative"].strip():
            raise MissingRequiredField("SAR narrative must be completed before review")
        return _transition(conn, sar_id, actor=actor, to_state="under_review",
                           expected_state=expected_state)


def approve_sar(
    db_path: Path,
    sar_id: str,
    *,
    actor: str,
    expected_state: str | None = None,
) -> dict:
    if actor not in _APPROVER_ROLES:
        raise PermissionDenied(f"only {sorted(_APPROVER_ROLES)} can approve SARs; got {actor!r}")
    with _db(db_path) as conn:
        return _transition(conn, sar_id, actor=actor, to_state="approved",
                           extra_fields={"approved_by": actor},
                           expected_state=expected_state)


def submit_to_regulator(
    db_path: Path,
    sar_id: str,
    *,
    actor: str,
    submission_ref: str,
    expected_state: str | None = None,
) -> dict:
    if actor not in _APPROVER_ROLES:
        raise PermissionDenied(f"only {sorted(_APPROVER_ROLES)} can file SARs; got {actor!r}")
    if not submission_ref.strip():
        raise MissingRequiredField("submission_ref must not be blank")
    now = _utcnow()
    with _db(db_path) as conn:
        return _transition(conn, sar_id, actor=actor, to_state="submitted",
                           extra_fields={
                               "submitted_at": now,
                               "submission_ref": submission_ref.strip(),
                               "submitted_by": actor,
                           },
                           expected_state=expected_state)


def acknowledge_receipt(
    db_path: Path,
    sar_id: str,
    *,
    actor: str,
    acknowledgement_ref: str,
    expected_state: str | None = None,
) -> dict:
    if not acknowledgement_ref.strip():
        raise MissingRequiredField("acknowledgement_ref must not be blank")
    now = _utcnow()
    with _db(db_path) as conn:
        return _transition(conn, sar_id, actor=actor, to_state="acknowledged",
                           extra_fields={
                               "acknowledged_at": now,
                               "acknowledgement_ref": acknowledgement_ref.strip(),
                           },
                           expected_state=expected_state)


def reject_sar(
    db_path: Path,
    sar_id: str,
    *,
    actor: str,
    rejection_reason: str,
    expected_state: str | None = None,
) -> dict:
    if not rejection_reason.strip():
        raise MissingRequiredField("rejection_reason must not be blank")
    now = _utcnow()
    with _db(db_path) as conn:
        return _transition(conn, sar_id, actor=actor, to_state="rejected",
                           extra_fields={
                               "rejected_at": now,
                               "rejection_reason": rejection_reason.strip(),
                           },
                           expected_state=expected_state)


def reopen_to_draft(
    db_path: Path,
    sar_id: str,
    *,
    actor: str,
) -> dict:
    """Return a rejected or approved SAR back to draft for rework."""
    with _db(db_path) as conn:
        return _transition(conn, sar_id, actor=actor, to_state="draft")


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------


def add_subject(
    db_path: Path,
    sar_id: str,
    *,
    subject_type: str,
    subject_role: str,
    name: str,
    id_type: str | None = None,
    id_number: str | None = None,
    nationality: str | None = None,
    address: str | None = None,
    account_number: str | None = None,
    added_by: str,
) -> dict:
    if subject_type not in SUBJECT_TYPES:
        raise SarStoreError(f"invalid subject_type {subject_type!r}")
    if subject_role not in SUBJECT_ROLES:
        raise SarStoreError(f"invalid subject_role {subject_role!r}")
    if not name.strip():
        raise MissingRequiredField("subject name must not be blank")

    subj_id = str(uuid.uuid4())
    now = _utcnow()
    with _db(db_path) as conn:
        row = conn.execute("SELECT id, state FROM sars WHERE id=?", (sar_id,)).fetchone()
        if row is None:
            raise SarNotFound(sar_id)
        if row["state"] in ("submitted", "acknowledged"):
            raise NarrativeLocked(
                f"Cannot add subjects to SAR in state {row['state']!r}"
            )
        conn.execute(
            """
            INSERT INTO sar_subjects(id, sar_id, subject_type, subject_role, name,
                                     id_type, id_number, nationality, address,
                                     account_number, added_by, added_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (subj_id, sar_id, subject_type, subject_role, name.strip(),
             id_type, id_number, nationality, address, account_number,
             added_by, now),
        )
        _audit(conn, sar_id, actor=added_by, action="subject_added",
               detail=f"name={name.strip()}, role={subject_role}")
        return dict(conn.execute(
            "SELECT * FROM sar_subjects WHERE id=?", (subj_id,)
        ).fetchone())


def get_subjects(db_path: Path, sar_id: str) -> list[dict]:
    with _db(db_path) as conn:
        row = conn.execute("SELECT id FROM sars WHERE id=?", (sar_id,)).fetchone()
        if row is None:
            raise SarNotFound(sar_id)
        rows = conn.execute(
            "SELECT * FROM sar_subjects WHERE sar_id=? ORDER BY added_at",
            (sar_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def get_audit_log(db_path: Path, sar_id: str) -> list[dict]:
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sar_audit_log WHERE sar_id=? ORDER BY occurred_at DESC",
            (sar_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Deadline helpers
# ---------------------------------------------------------------------------


def is_overdue(sar: dict) -> bool:
    """Return True if the SAR has not been submitted and its deadline has passed."""
    if sar["state"] in ("submitted", "acknowledged"):
        return False
    try:
        deadline = datetime.fromisoformat(sar["filing_deadline"])
        return datetime.now(UTC) > deadline
    except (ValueError, TypeError):
        return False


def list_overdue(db_path: Path) -> list[dict]:
    """Return all SARs not yet submitted whose filing deadline has passed."""
    all_sars = list_sars(db_path)
    return [s for s in all_sars if is_overdue(s)]
