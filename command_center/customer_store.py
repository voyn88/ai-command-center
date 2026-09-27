"""AML Customer store — customer profiles, KYC checks, periodic review."""

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

CUSTOMER_TYPES: tuple[str, ...] = ("individual", "legal")
RISK_TIERS: tuple[str, ...] = ("low", "medium", "high", "pep")
KYC_STATUSES: tuple[str, ...] = ("pending", "in_progress", "verified", "expired", "rejected")
CDD_STATUSES: tuple[str, ...] = ("pending", "basic", "enhanced", "expired")

# Days between mandatory CDD reviews by risk tier
REVIEW_INTERVALS: dict[str, int] = {
    "low": 365,
    "medium": 180,
    "high": 90,
    "pep": 90,
}


class CustomerStoreError(Exception):
    pass


class NotFound(CustomerStoreError):
    pass


class InvalidValue(CustomerStoreError):
    pass


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "aml_customers.db"


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
    """Create customer tables if they do not already exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        conn.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                customer_type TEXT NOT NULL
                    CHECK(customer_type IN ('individual','legal')),
                risk_tier TEXT NOT NULL DEFAULT 'medium'
                    CHECK(risk_tier IN ('low','medium','high','pep')),
                kyc_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(kyc_status IN ('pending','in_progress','verified','expired','rejected')),
                cdd_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(cdd_status IN ('pending','basic','enhanced','expired')),
                country TEXT,
                industry TEXT,
                pep_flag INTEGER NOT NULL DEFAULT 0,
                adverse_media_flag INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_review_at TEXT,
                schema_version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS kyc_checks (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customers(id),
                check_type TEXT NOT NULL,
                result TEXT NOT NULL CHECK(result IN ('pass','fail','pending','inconclusive')),
                evidence_ref TEXT,
                notes TEXT,
                verified_at TEXT,
                verified_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customer_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                field TEXT,
                old_value TEXT,
                new_value TEXT,
                ts TEXT NOT NULL
            );

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


def _log(conn: sqlite3.Connection, customer_id: str, action: str, actor: str, **kwargs) -> None:
    conn.execute(
        "INSERT INTO customer_audit_log(customer_id,action,actor,field,old_value,new_value,ts)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            customer_id,
            action,
            actor,
            kwargs.get("field"),
            kwargs.get("old_value"),
            kwargs.get("new_value"),
            _utcnow(),
        ),
    )


# ---------------------------------------------------------------------------
# Customer CRUD
# ---------------------------------------------------------------------------


def create_customer(
    db_path: Path,
    *,
    name: str,
    customer_type: str,
    country: str | None = None,
    industry: str | None = None,
    pep_flag: bool = False,
    adverse_media_flag: bool = False,
    actor: str,
) -> dict:
    if customer_type not in CUSTOMER_TYPES:
        raise InvalidValue(f"invalid customer_type {customer_type!r}")
    customer_id = str(uuid.uuid4())
    now = _utcnow()
    with _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO customers(
                id, name, customer_type, risk_tier, kyc_status, cdd_status,
                country, industry, pep_flag, adverse_media_flag,
                created_at, updated_at, schema_version
            ) VALUES (?,?,?,'medium','pending','pending',?,?,?,?,?,?,?)
            """,
            (
                customer_id, name, customer_type,
                country, industry,
                1 if pep_flag else 0,
                1 if adverse_media_flag else 0,
                now, now, SCHEMA_VERSION,
            ),
        )
        _log(conn, customer_id, "create", actor, new_value=customer_type)
        return dict(conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())


def get_customer(db_path: Path, customer_id: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if row is None:
        raise NotFound(customer_id)
    return dict(row)


def list_customers(
    db_path: Path,
    *,
    risk_tier: str | None = None,
    kyc_status: str | None = None,
    customer_type: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if risk_tier is not None:
        clauses.append("risk_tier = ?")
        params.append(risk_tier)
    if kyc_status is not None:
        clauses.append("kyc_status = ?")
        params.append(kyc_status)
    if customer_type is not None:
        clauses.append("customer_type = ?")
        params.append(customer_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM customers {where} ORDER BY created_at DESC", params  # nosec B608 - `where` is built only from a fixed set of hardcoded clause literals ("risk_tier = ?", "kyc_status = ?", "customer_type = ?"); all actual values are bound via `params` and `?` placeholders
        ).fetchall()
    return [dict(r) for r in rows]


def update_risk_tier(db_path: Path, customer_id: str, *, risk_tier: str, actor: str) -> dict:
    if risk_tier not in RISK_TIERS:
        raise InvalidValue(f"invalid risk_tier {risk_tier!r}")
    with _db(db_path) as conn:
        row = conn.execute("SELECT risk_tier FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise NotFound(customer_id)
        old = row["risk_tier"]
        conn.execute(
            "UPDATE customers SET risk_tier=?, updated_at=? WHERE id=?",
            (risk_tier, _utcnow(), customer_id),
        )
        _log(conn, customer_id, "update_risk_tier", actor, field="risk_tier", old_value=old, new_value=risk_tier)
        return dict(conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())


def update_kyc_status(db_path: Path, customer_id: str, *, kyc_status: str, actor: str) -> dict:
    if kyc_status not in KYC_STATUSES:
        raise InvalidValue(f"invalid kyc_status {kyc_status!r}")
    with _db(db_path) as conn:
        row = conn.execute("SELECT kyc_status FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise NotFound(customer_id)
        old = row["kyc_status"]
        conn.execute(
            "UPDATE customers SET kyc_status=?, updated_at=? WHERE id=?",
            (kyc_status, _utcnow(), customer_id),
        )
        _log(conn, customer_id, "update_kyc_status", actor, field="kyc_status", old_value=old, new_value=kyc_status)
        return dict(conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())


def record_review(db_path: Path, customer_id: str, *, actor: str) -> dict:
    """Mark last_review_at = now."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT id FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise NotFound(customer_id)
        now = _utcnow()
        conn.execute(
            "UPDATE customers SET last_review_at=?, updated_at=? WHERE id=?",
            (now, now, customer_id),
        )
        _log(conn, customer_id, "record_review", actor)
        return dict(conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())


# ---------------------------------------------------------------------------
# KYC checks
# ---------------------------------------------------------------------------


def add_kyc_check(
    db_path: Path,
    *,
    customer_id: str,
    check_type: str,
    result: str,
    evidence_ref: str | None = None,
    notes: str | None = None,
    verified_by: str,
) -> dict:
    if result not in ("pass", "fail", "pending", "inconclusive"):
        raise InvalidValue(f"invalid result {result!r}")
    check_id = str(uuid.uuid4())
    now = _utcnow()
    with _db(db_path) as conn:
        row = conn.execute("SELECT id FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise NotFound(customer_id)
        conn.execute(
            """
            INSERT INTO kyc_checks(
                id, customer_id, check_type, result, evidence_ref,
                notes, verified_at, verified_by, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (check_id, customer_id, check_type, result, evidence_ref, notes, now, verified_by, now),
        )
        return dict(conn.execute("SELECT * FROM kyc_checks WHERE id=?", (check_id,)).fetchone())


def get_kyc_checks(db_path: Path, customer_id: str) -> list[dict]:
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM kyc_checks WHERE customer_id=? ORDER BY created_at DESC",
            (customer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Risk profile
# ---------------------------------------------------------------------------


def get_customer_risk_profile(db_path: Path, customer_id: str) -> dict:
    """Return customer dict merged with latest KYC checks summary."""
    customer = get_customer(db_path, customer_id)
    checks = get_kyc_checks(db_path, customer_id)
    failed = [c for c in checks if c["result"] == "fail"]
    passed = [c for c in checks if c["result"] == "pass"]
    return {
        **customer,
        "kyc_checks_total": len(checks),
        "kyc_checks_passed": len(passed),
        "kyc_checks_failed": len(failed),
        "kyc_latest": checks[0] if checks else None,
    }


# ---------------------------------------------------------------------------
# Periodic review
# ---------------------------------------------------------------------------


def get_customers_due_for_review(db_path: Path) -> list[dict]:
    """Return customers whose CDD review is overdue based on risk tier intervals."""
    now = datetime.now(UTC)
    all_customers = list_customers(db_path)
    due: list[dict] = []
    for c in all_customers:
        interval = REVIEW_INTERVALS.get(c["risk_tier"], 365)
        last = c.get("last_review_at")
        if last is None:
            # Never reviewed — due immediately if created > interval days ago
            created = datetime.fromisoformat(c["created_at"])
            if (now - created).days >= interval:
                due.append(c)
        else:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).days >= interval:
                due.append(c)
    return due
