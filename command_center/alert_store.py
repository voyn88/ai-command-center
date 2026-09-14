"""AML Alert store — database schema and connection helpers."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from command_center import storage

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 2

ROLES: tuple[str, ...] = ("Analyst", "SeniorAnalyst", "ComplianceOfficer", "MLRO")
OUTCOME_VALUES: tuple[str, ...] = (
    "true_positive",
    "false_positive",
    "escalated_to_case",
    "closed_no_action",
    "sar_filed",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AlertStoreError(Exception):
    pass


class PermissionDenied(AlertStoreError):
    pass


class InvalidTransition(AlertStoreError):
    pass


class OutcomeRequired(AlertStoreError):
    pass


class LostUpdate(AlertStoreError):
    pass


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "aml_alerts.db"


# ---------------------------------------------------------------------------
# Connection context manager
# ---------------------------------------------------------------------------


@contextmanager
def _db(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def init_db(db_path: Path) -> None:
    """Create alert tables if they do not already exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_ref TEXT,
                subject_id TEXT NOT NULL,
                subject_type TEXT NOT NULL
                    CHECK(subject_type IN ('customer','transaction','account')),
                trigger_desc TEXT NOT NULL,
                priority TEXT NOT NULL
                    CHECK(priority IN ('low','medium','high','critical')),
                priority_rationale TEXT,
                state TEXT NOT NULL DEFAULT 'generated'
                    CHECK(state IN (
                        'generated','assigned','in_triage',
                        'pending_review','escalated','closed','archived'
                    )),
                outcome TEXT
                    CHECK(outcome IN (
                        'true_positive','false_positive',
                        'escalated_to_case','closed_no_action','sar_filed'
                    )),
                disposition_rationale TEXT,
                owner TEXT,
                due_at TEXT,
                overdue INTEGER NOT NULL DEFAULT 0,
                case_id TEXT,
                predecessor_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS alert_evidence (
                alert_id TEXT NOT NULL REFERENCES alerts(id),
                evidence_ref TEXT NOT NULL,
                attached_at TEXT NOT NULL,
                attached_by TEXT NOT NULL,
                PRIMARY KEY (alert_id, evidence_ref)
            );

            CREATE TABLE IF NOT EXISTS alert_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                old_state TEXT,
                new_state TEXT,
                rationale TEXT,
                ts TEXT NOT NULL
            );

            PRAGMA user_version = 1;
            COMMIT;
            """
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    # Run migrations on a separate connection (executescript commits its own txn)
    conn2 = sqlite3.connect(db_path, timeout=5)
    conn2.row_factory = sqlite3.Row
    try:
        _migrate(conn2)
        conn2.commit()
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _migrate(conn: sqlite3.Connection) -> None:
    """Run forward migrations from current schema_version to SCHEMA_VERSION."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        # Check if column already exists before adding (idempotent)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        if "duplicate_of" not in cols:
            conn.execute("ALTER TABLE alerts ADD COLUMN duplicate_of TEXT")
        conn.execute("PRAGMA user_version = 2")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _transition(
    conn: sqlite3.Connection,
    alert_id: str,
    expected_state: str,
    new_state: str,
    actor: str,
    action: str,
    *,
    extra_sets: dict | None = None,
    rationale: str | None = None,
) -> dict:
    row = conn.execute("SELECT state FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if row is None:
        raise KeyError(alert_id)
    if row["state"] != expected_state:
        raise LostUpdate(f"expected {expected_state}, got {row['state']}")
    sets: dict[str, object] = {"state": new_state, "updated_at": _utcnow()}
    if extra_sets:
        sets.update(extra_sets)
    set_clause = ", ".join(f"{k}=?" for k in sets)
    conn.execute(
        f"UPDATE alerts SET {set_clause} WHERE id=?",  # nosec B608 - set_clause keys come only from "state"/"updated_at" plus hardcoded extra_sets keys passed by module-private callers; values are bound via params
        (*sets.values(), alert_id),
    )
    conn.execute(
        "INSERT INTO alert_audit_log"
        "(alert_id,action,actor,old_state,new_state,rationale,ts)"
        " VALUES(?,?,?,?,?,?,?)",
        (alert_id, action, actor, expected_state, new_state, rationale, _utcnow()),
    )
    return dict(conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_alert(
    db_path: Path,
    *,
    source: str,
    source_ref: str | None,
    subject_id: str,
    subject_type: str,
    trigger_desc: str,
    priority: str,
    priority_rationale: str | None,
    due_at: str | None,
    actor: str,
) -> dict:
    """Create a new alert in state 'generated'. Returns the alert dict."""
    if subject_type not in ("customer", "transaction", "account"):
        raise InvalidTransition(
            f"invalid subject_type {subject_type!r};"
            " must be one of 'customer', 'transaction', 'account'"
        )
    if priority not in ("low", "medium", "high", "critical"):
        raise InvalidTransition(
            f"invalid priority {priority!r};"
            " must be one of 'low', 'medium', 'high', 'critical'"
        )
    alert_id = str(uuid.uuid4())
    now = _utcnow()
    with _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO alerts(
                id, source, source_ref, subject_id, subject_type,
                trigger_desc, priority, priority_rationale, state,
                due_at, created_at, updated_at, schema_version
            ) VALUES (?,?,?,?,?,?,?,?,'generated',?,?,?,?)
            """,
            (
                alert_id, source, source_ref, subject_id, subject_type,
                trigger_desc, priority, priority_rationale,
                due_at, now, now, SCHEMA_VERSION,
            ),
        )
        conn.execute(
            "INSERT INTO alert_audit_log"
            "(alert_id,action,actor,old_state,new_state,rationale,ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (alert_id, "create", actor, None, "generated", None, _utcnow()),
        )
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return dict(row)


def assign_alert(
    db_path: Path,
    alert_id: str,
    *,
    owner: str,
    actor: str,
    expected_state: str = "generated",
) -> dict:
    """Transition generated → assigned; sets owner."""
    with _db(db_path) as conn:
        return _transition(
            conn, alert_id, expected_state, "assigned", actor, "assign",
            extra_sets={"owner": owner},
        )


def start_triage(
    db_path: Path,
    alert_id: str,
    *,
    actor: str,
    expected_state: str = "assigned",
) -> dict:
    """Transition assigned → in_triage.

    Actor must be the assigned owner, or have role SeniorAnalyst/ComplianceOfficer.
    """
    with _db(db_path) as conn:
        row = conn.execute("SELECT state, owner FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None:
            raise KeyError(alert_id)
        if row["state"] != expected_state:
            raise LostUpdate(f"expected {expected_state}, got {row['state']}")
        privileged = actor in ("SeniorAnalyst", "ComplianceOfficer")
        if not privileged and row["owner"] != actor:
            raise PermissionDenied(
                f"{actor!r} is not the assigned owner and lacks triage-start privilege"
            )
        return _transition(conn, alert_id, expected_state, "in_triage", actor, "start_triage")


def submit_for_review(
    db_path: Path,
    alert_id: str,
    *,
    actor: str,
    rationale: str,
    expected_state: str = "in_triage",
) -> dict:
    """Transition in_triage → pending_review."""
    with _db(db_path) as conn:
        return _transition(
            conn, alert_id, expected_state, "pending_review", actor,
            "submit_for_review", rationale=rationale,
        )


def dispose_alert(
    db_path: Path,
    alert_id: str,
    *,
    actor: str,
    outcome: str,
    rationale: str,
    expected_state: str = "pending_review",
) -> dict:
    """Transition pending_review → closed with a disposition outcome."""
    if not outcome:
        raise OutcomeRequired("outcome is required to dispose an alert")
    if outcome == "escalated_to_case":
        raise InvalidTransition(
            "use escalate_alert() to set outcome='escalated_to_case'"
        )
    if outcome not in OUTCOME_VALUES:
        raise InvalidTransition(
            f"invalid outcome {outcome!r}; must be one of {OUTCOME_VALUES}"
        )
    with _db(db_path) as conn:
        return _transition(
            conn, alert_id, expected_state, "closed", actor, "dispose",
            extra_sets={"outcome": outcome, "disposition_rationale": rationale},
            rationale=rationale,
        )


def escalate_alert(
    db_path: Path,
    alert_id: str,
    *,
    actor: str,
    case_id: str,
    rationale: str,
    expected_state: str = "pending_review",
) -> dict:
    """Transition pending_review → escalated; records case_id."""
    with _db(db_path) as conn:
        return _transition(
            conn, alert_id, expected_state, "escalated", actor, "escalate",
            extra_sets={"case_id": case_id, "outcome": "escalated_to_case"},
            rationale=rationale,
        )


def archive_alert(
    db_path: Path,
    alert_id: str,
    *,
    actor: str,
    rationale: str,
) -> dict:
    """Transition closed → archived. Only ComplianceOfficer or MLRO may archive."""
    if actor not in ("ComplianceOfficer", "MLRO"):
        raise PermissionDenied(
            f"{actor!r} is not authorised to archive alerts"
            " (ComplianceOfficer or MLRO required)"
        )
    with _db(db_path) as conn:
        return _transition(
            conn, alert_id, "closed", "archived", actor, "archive",
            rationale=rationale,
        )


def get_alert(db_path: Path, alert_id: str) -> dict:
    """Return the alert row as a dict. Raises KeyError if not found."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if row is None:
        raise KeyError(alert_id)
    return dict(row)


def list_alerts(
    db_path: Path,
    *,
    state: str | None = None,
    owner: str | None = None,
    overdue_only: bool = False,
    subject_id: str | None = None,
) -> list[dict]:
    """Return alerts matching the given filters, sorted by created_at DESC."""
    clauses: list[str] = []
    params: list[object] = []
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if owner is not None:
        clauses.append("owner = ?")
        params.append(owner)
    if overdue_only:
        clauses.append("overdue = 1")
    if subject_id is not None:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM alerts {where} ORDER BY created_at DESC"  # nosec B608 - clauses is built only from hardcoded literal fragments ("state = ?", "owner = ?", "overdue = 1", "subject_id = ?"); actual values passed via params list
    with _db(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def mark_overdue(db_path: Path) -> int:
    """Mark alerts past their due_at as overdue. Returns count of newly marked rows."""
    now = _utcnow()
    with _db(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE alerts
               SET overdue = 1, updated_at = ?
             WHERE due_at IS NOT NULL
               AND due_at < ?
               AND overdue = 0
               AND state NOT IN ('closed', 'archived')
            """,
            (now, now),
        )
        return cursor.rowcount


def attach_evidence(
    db_path: Path,
    alert_id: str,
    *,
    evidence_ref: str,
    actor: str,
) -> None:
    """Attach an evidence reference to an alert."""
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT state FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        if row is None:
            raise KeyError(alert_id)
        if row["state"] in ("closed", "archived"):
            raise AlertStoreError(
                "cannot attach evidence to closed/archived alert"
            )
        conn.execute(
            "INSERT OR IGNORE INTO alert_evidence(alert_id, evidence_ref, attached_at, attached_by)"
            " VALUES(?,?,?,?)",
            (alert_id, evidence_ref, _utcnow(), actor),
        )


def get_audit_log(db_path: Path, alert_id: str) -> list[dict]:
    """Return audit log entries for the given alert, ordered by ts ASC."""
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM alert_audit_log WHERE alert_id=? ORDER BY ts ASC",
            (alert_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def find_duplicates(
    db_path: Path,
    *,
    subject_id: str,
    trigger_desc: str,
    window_days: int = 30,
) -> list[dict]:
    """Return open alerts with matching subject and trigger within the window.

    Matching: same subject_id AND trigger_desc first-50-chars case-insensitive.
    Excludes closed/archived alerts.
    """
    cutoff = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - __import__("datetime").timedelta(days=window_days)
    ).isoformat()
    prefix = trigger_desc[:50].lower()
    with _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM alerts
             WHERE subject_id = ?
               AND LOWER(SUBSTR(trigger_desc, 1, 50)) = ?
               AND state NOT IN ('closed', 'archived')
               AND created_at >= ?
             ORDER BY created_at DESC
            """,
            (subject_id, prefix, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_duplicate(
    db_path: Path,
    alert_id: str,
    *,
    duplicate_of: str,
    actor: str,
) -> None:
    """Mark alert_id as a duplicate of duplicate_of. Writes audit log entry."""
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        if row is None:
            raise KeyError(alert_id)
        conn.execute(
            "UPDATE alerts SET duplicate_of=?, updated_at=? WHERE id=?",
            (duplicate_of, _utcnow(), alert_id),
        )
        conn.execute(
            "INSERT INTO alert_audit_log"
            "(alert_id,action,actor,old_state,new_state,rationale,ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (alert_id, "mark_duplicate", actor, None, None, f"duplicate_of={duplicate_of}", _utcnow()),
        )
