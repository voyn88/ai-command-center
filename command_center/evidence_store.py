"""AML Evidence Store — immutable evidence attachments for alerts, cases and customers."""

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

ENTITY_TYPES: frozenset[str] = frozenset({"alert", "case", "customer", "sar"})

EVIDENCE_TYPES: frozenset[str] = frozenset({
    "document",         # scanned doc / PDF
    "transaction_log",  # raw tx extract
    "screenshot",       # UI evidence
    "report",           # internal/external report
    "kyc_document",     # passport, certificate, etc.
    "adverse_media",    # news article reference
    "communication",    # email / letter
    "other",
})


class EvidenceStoreError(Exception):
    pass


class EvidenceNotFound(EvidenceStoreError):
    pass


class InvalidValue(EvidenceStoreError):
    pass


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "aml_evidence.db"


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

            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                file_ref TEXT,
                url TEXT,
                content_hash TEXT,
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_evidence_entity
                ON evidence(entity_type, entity_id);

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


# ---------------------------------------------------------------------------
# Public API — immutable (no update / delete)
# ---------------------------------------------------------------------------


def attach_evidence(
    db_path: Path,
    *,
    entity_type: str,
    entity_id: str,
    evidence_type: str,
    title: str,
    description: str | None = None,
    file_ref: str | None = None,
    url: str | None = None,
    content_hash: str | None = None,
    submitted_by: str,
) -> dict:
    """Attach an evidence record to any entity. Immutable once created."""
    if entity_type not in ENTITY_TYPES:
        raise InvalidValue(f"unknown entity_type {entity_type!r}; valid: {sorted(ENTITY_TYPES)}")
    if evidence_type not in EVIDENCE_TYPES:
        raise InvalidValue(f"unknown evidence_type {evidence_type!r}; valid: {sorted(EVIDENCE_TYPES)}")
    if not title.strip():
        raise InvalidValue("title must not be blank")
    if file_ref is None and url is None:
        raise InvalidValue("at least one of file_ref or url must be provided")

    ev_id = str(uuid.uuid4())
    now = _utcnow()
    with _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO evidence(
                id, entity_type, entity_id, evidence_type, title,
                description, file_ref, url, content_hash,
                submitted_by, submitted_at, schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (ev_id, entity_type, entity_id, evidence_type, title.strip(),
             description, file_ref, url, content_hash,
             submitted_by, now, SCHEMA_VERSION),
        )
        return dict(conn.execute("SELECT * FROM evidence WHERE id=?", (ev_id,)).fetchone())


def get_evidence(db_path: Path, evidence_id: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if row is None:
        raise EvidenceNotFound(evidence_id)
    return dict(row)


def list_evidence(
    db_path: Path,
    *,
    entity_type: str,
    entity_id: str,
    evidence_type: str | None = None,
) -> list[dict]:
    """Return all evidence for an entity, newest first."""
    clauses = ["entity_type = ?", "entity_id = ?"]
    params: list[object] = [entity_type, entity_id]
    if evidence_type is not None:
        clauses.append("evidence_type = ?")
        params.append(evidence_type)
    where = " AND ".join(clauses)
    with _db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM evidence WHERE {where} ORDER BY submitted_at DESC", params  # nosec B608 - `where` is joined only from hardcoded clause literals ("entity_type = ?", "entity_id = ?", "evidence_type = ?"); all actual values are bound via `params` and `?` placeholders
        ).fetchall()
    return [dict(r) for r in rows]


def count_evidence(db_path: Path, *, entity_type: str, entity_id: str) -> int:
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        ).fetchone()
    return row[0]
