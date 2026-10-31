"""AML Case Store — case management state machine."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from command_center import storage

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1

CASE_STATES: tuple[str, ...] = (
    "open",
    "under_investigation",
    "pending_review",
    "closed",
    "escalated_to_sar",
)

PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "critical")

ROLES: frozenset[str] = frozenset({
    "Analyst",
    "SeniorAnalyst",
    "Investigator",
    "ComplianceOfficer",
    "MLRO",
    "system",
})

# Allowed state transitions: {current_state: set_of_allowed_next_states}
_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"under_investigation"}),
    "under_investigation": frozenset({"pending_review"}),
    "pending_review": frozenset({"closed", "escalated_to_sar"}),
    "closed": frozenset(),
    "escalated_to_sar": frozenset(),
}

# Roles allowed to close or escalate cases
_SENIOR_ROLES: frozenset[str] = frozenset({"ComplianceOfficer", "MLRO"})


class CaseStoreError(Exception):
    pass


class CaseNotFound(CaseStoreError):
    pass


class InvalidTransition(CaseStoreError):
    pass


class PermissionDenied(CaseStoreError):
    pass


class MissingRequiredField(CaseStoreError):
    pass


class LostUpdate(CaseStoreError):
    pass


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "aml_cases.db"


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

            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY,
                case_number TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                state TEXT NOT NULL DEFAULT 'open'
                    CHECK(state IN ('open','under_investigation','pending_review',
                                    'closed','escalated_to_sar')),
                priority TEXT NOT NULL DEFAULT 'medium'
                    CHECK(priority IN ('low','medium','high','critical')),
                assigned_to TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                closure_reason TEXT,
                sar_ref TEXT,
                schema_version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS case_alerts (
                case_id TEXT NOT NULL REFERENCES cases(id),
                alert_id TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                linked_by TEXT NOT NULL,
                PRIMARY KEY (case_id, alert_id)
            );

            CREATE TABLE IF NOT EXISTS case_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(id),
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT,
                detail TEXT,
                occurred_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);
            CREATE INDEX IF NOT EXISTS idx_cases_assigned ON cases(assigned_to);
            CREATE INDEX IF NOT EXISTS idx_case_alerts_alert ON case_alerts(alert_id);

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


def _next_case_number(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COUNT(*) FROM cases").fetchone()
    n = row[0] + 1
    return f"AML-{n:05d}"


def _audit(
    conn: sqlite3.Connection,
    case_id: str,
    *,
    actor: str,
    action: str,
    from_state: str | None = None,
    to_state: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO case_audit_log(case_id, actor, action, from_state, to_state, detail, occurred_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (case_id, actor, action, from_state, to_state, detail, _utcnow()),
    )


def _transition(
    conn: sqlite3.Connection,
    case_id: str,
    *,
    actor: str,
    to_state: str,
    extra_fields: dict | None = None,
    expected_state: str | None = None,
) -> dict:
    row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if row is None:
        raise CaseNotFound(case_id)
    case = dict(row)

    if expected_state is not None and case["state"] != expected_state:
        raise LostUpdate(
            f"Expected state {expected_state!r} but found {case['state']!r}"
        )

    allowed = _TRANSITIONS.get(case["state"], frozenset())
    if to_state not in allowed:
        raise InvalidTransition(
            f"Cannot move case from {case['state']!r} to {to_state!r}"
        )

    updates: dict = {"state": to_state, "updated_at": _utcnow()}
    if extra_fields:
        updates.update(extra_fields)

    set_clause = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE cases SET {set_clause} WHERE id=?",  # nosec B608 - set_clause keys come only from "state"/"updated_at" plus hardcoded extra_fields keys passed by module-private callers; values are bound via params
        (*updates.values(), case_id),
    )
    _audit(
        conn, case_id,
        actor=actor,
        action=f"transition:{case['state']}→{to_state}",
        from_state=case["state"],
        to_state=to_state,
    )
    return dict(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_case(
    db_path: Path,
    *,
    title: str,
    description: str | None = None,
    priority: str = "medium",
    created_by: str,
    assigned_to: str | None = None,
) -> dict:
    if not title.strip():
        raise MissingRequiredField("title must not be blank")
    if priority not in PRIORITIES:
        raise CaseStoreError(f"invalid priority {priority!r}")

    case_id = str(uuid.uuid4())
    now = _utcnow()
    with _db(db_path) as conn:
        case_number = _next_case_number(conn)
        conn.execute(
            """
            INSERT INTO cases(id, case_number, title, description, state, priority,
                              assigned_to, created_by, created_at, updated_at, schema_version)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (case_id, case_number, title.strip(), description, "open", priority,
             assigned_to, created_by, now, now, SCHEMA_VERSION),
        )
        _audit(conn, case_id, actor=created_by, action="created",
               from_state=None, to_state="open",
               detail=f"priority={priority}, assigned_to={assigned_to}")
        return dict(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


def get_case(db_path: Path, case_id: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if row is None:
        raise CaseNotFound(case_id)
    return dict(row)


def get_case_by_number(db_path: Path, case_number: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM cases WHERE case_number=?", (case_number,)).fetchone()
    if row is None:
        raise CaseNotFound(case_number)
    return dict(row)


def list_cases(
    db_path: Path,
    *,
    state: str | None = None,
    priority: str | None = None,
    assigned_to: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if priority is not None:
        clauses.append("priority = ?")
        params.append(priority)
    if assigned_to is not None:
        clauses.append("assigned_to = ?")
        params.append(assigned_to)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM cases {where} ORDER BY created_at DESC", params  # nosec B608 - clauses is built only from hardcoded literal fragments ("state = ?", "priority = ?", "assigned_to = ?"); actual values passed via params list
        ).fetchall()
    return [dict(r) for r in rows]


def assign_investigator(
    db_path: Path,
    case_id: str,
    *,
    assigned_to: str,
    actor: str,
) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise CaseNotFound(case_id)
        conn.execute(
            "UPDATE cases SET assigned_to=?, updated_at=? WHERE id=?",
            (assigned_to, _utcnow(), case_id),
        )
        _audit(conn, case_id, actor=actor, action="assigned",
               detail=f"assigned_to={assigned_to}")
        return dict(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())


def start_investigation(
    db_path: Path,
    case_id: str,
    *,
    actor: str,
    expected_state: str | None = None,
) -> dict:
    with _db(db_path) as conn:
        return _transition(conn, case_id, actor=actor,
                           to_state="under_investigation",
                           expected_state=expected_state)


def submit_for_review(
    db_path: Path,
    case_id: str,
    *,
    actor: str,
    expected_state: str | None = None,
) -> dict:
    with _db(db_path) as conn:
        return _transition(conn, case_id, actor=actor,
                           to_state="pending_review",
                           expected_state=expected_state)


def close_case(
    db_path: Path,
    case_id: str,
    *,
    actor: str,
    closure_reason: str,
    expected_state: str | None = None,
) -> dict:
    if actor not in _SENIOR_ROLES:
        raise PermissionDenied(f"only {sorted(_SENIOR_ROLES)} can close cases; got {actor!r}")
    if not closure_reason.strip():
        raise MissingRequiredField("closure_reason must not be blank")
    now = _utcnow()
    with _db(db_path) as conn:
        return _transition(
            conn, case_id, actor=actor, to_state="closed",
            extra_fields={"closure_reason": closure_reason.strip(), "closed_at": now},
            expected_state=expected_state,
        )


def escalate_to_sar(
    db_path: Path,
    case_id: str,
    *,
    actor: str,
    sar_ref: str,
    expected_state: str | None = None,
) -> dict:
    if actor not in _SENIOR_ROLES:
        raise PermissionDenied(f"only {sorted(_SENIOR_ROLES)} can escalate to SAR; got {actor!r}")
    if not sar_ref.strip():
        raise MissingRequiredField("sar_ref must not be blank")
    with _db(db_path) as conn:
        return _transition(
            conn, case_id, actor=actor, to_state="escalated_to_sar",
            extra_fields={"sar_ref": sar_ref.strip()},
            expected_state=expected_state,
        )


def get_audit_log(db_path: Path, case_id: str) -> list[dict]:
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM case_audit_log WHERE case_id=? ORDER BY occurred_at DESC",
            (case_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Alert linking
# ---------------------------------------------------------------------------


def link_alert(
    db_path: Path,
    case_id: str,
    alert_id: str,
    *,
    actor: str,
) -> None:
    """Link an alert to a case. Idempotent — no error if already linked."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise CaseNotFound(case_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO case_alerts(case_id, alert_id, linked_at, linked_by)
            VALUES(?,?,?,?)
            """,
            (case_id, alert_id, _utcnow(), actor),
        )
        _audit(conn, case_id, actor=actor, action="alert_linked",
               detail=f"alert_id={alert_id}")


def unlink_alert(
    db_path: Path,
    case_id: str,
    alert_id: str,
    *,
    actor: str,
) -> None:
    with _db(db_path) as conn:
        row = conn.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise CaseNotFound(case_id)
        conn.execute(
            "DELETE FROM case_alerts WHERE case_id=? AND alert_id=?",
            (case_id, alert_id),
        )
        _audit(conn, case_id, actor=actor, action="alert_unlinked",
               detail=f"alert_id={alert_id}")


def get_case_alerts(db_path: Path, case_id: str) -> list[dict]:
    """Return case_alerts rows (alert_id, linked_at, linked_by) for a case."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT id FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise CaseNotFound(case_id)
        rows = conn.execute(
            "SELECT * FROM case_alerts WHERE case_id=? ORDER BY linked_at DESC",
            (case_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def find_cases_for_alert(db_path: Path, alert_id: str) -> list[dict]:
    """Return all cases that have this alert linked."""
    with _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM cases c
            JOIN case_alerts ca ON ca.case_id = c.id
            WHERE ca.alert_id = ?
            ORDER BY c.created_at DESC
            """,
            (alert_id,),
        ).fetchall()
    return [dict(r) for r in rows]
