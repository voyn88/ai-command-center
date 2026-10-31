"""AML Rule Engine — rule management and event evaluation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from command_center import storage

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1

# Supported condition operators
CONDITION_OPERATORS: frozenset[str] = frozenset({
    "amount_gt",        # event["amount"] > threshold
    "amount_gte",       # event["amount"] >= threshold
    "frequency_gt",     # event["frequency"] > threshold (count in period)
    "country_in",       # event["country"] in value_list
    "country_not_in",   # event["country"] not in value_list
    "risk_tier_in",     # event["risk_tier"] in value_list
    "industry_in",      # event["industry"] in value_list
    "product_in",       # event["product"] in value_list
    "pep_flag",         # event["pep_flag"] is truthy
    "adverse_media",    # event["adverse_media_flag"] is truthy
})

ALERT_TYPES: tuple[str, ...] = (
    "ctr",          # Cash Transaction Report threshold
    "str",          # Suspicious Transaction Report
    "pep_related",  # PEP involvement
    "sanctions",    # Sanctions/OFAC hit
    "risk_escalation",
    "structuring",
    "high_frequency",
    "high_risk_country",
    "generic",
)

JURISDICTIONS: tuple[str, ...] = ("RU", "global")


class RuleEngineError(Exception):
    pass


class RuleNotFound(RuleEngineError):
    pass


class InvalidCondition(RuleEngineError):
    pass


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "aml_rules.db"


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

            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                condition_op TEXT NOT NULL,
                threshold REAL,
                value_list TEXT,
                alert_type TEXT NOT NULL,
                priority_weight TEXT NOT NULL
                    CHECK(priority_weight IN ('low','medium','high','critical')),
                enabled INTEGER NOT NULL DEFAULT 1,
                jurisdiction TEXT NOT NULL DEFAULT 'global',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS rule_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT NOT NULL REFERENCES rules(id),
                event_ref TEXT,
                triggered INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL
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


def _validate_condition(op: str, threshold: float | None, value_list: list | None) -> None:
    if op not in CONDITION_OPERATORS:
        raise InvalidCondition(f"unknown operator {op!r}; valid: {sorted(CONDITION_OPERATORS)}")
    scalar_ops = {"amount_gt", "amount_gte", "frequency_gt"}
    list_ops = {"country_in", "country_not_in", "risk_tier_in", "industry_in", "product_in"}
    flag_ops = {"pep_flag", "adverse_media"}
    if op in scalar_ops and threshold is None:
        raise InvalidCondition(f"operator {op!r} requires a numeric threshold")
    if op in list_ops and not value_list:
        raise InvalidCondition(f"operator {op!r} requires a non-empty value_list")
    if op in flag_ops and threshold is not None:
        raise InvalidCondition(f"operator {op!r} does not use threshold")


# ---------------------------------------------------------------------------
# Rule CRUD
# ---------------------------------------------------------------------------


def create_rule(
    db_path: Path,
    *,
    name: str,
    description: str | None = None,
    condition_op: str,
    threshold: float | None = None,
    value_list: list[str] | None = None,
    alert_type: str,
    priority_weight: str,
    jurisdiction: str = "global",
) -> dict:
    _validate_condition(condition_op, threshold, value_list)
    if alert_type not in ALERT_TYPES:
        raise RuleEngineError(f"unknown alert_type {alert_type!r}")
    if priority_weight not in ("low", "medium", "high", "critical"):
        raise RuleEngineError(f"invalid priority_weight {priority_weight!r}")

    rule_id = str(uuid.uuid4())
    now = _utcnow()
    vl_json = json.dumps(value_list) if value_list else None
    with _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO rules(
                id, name, description, condition_op, threshold, value_list,
                alert_type, priority_weight, enabled, jurisdiction,
                created_at, updated_at, schema_version
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)
            """,
            (rule_id, name, description, condition_op, threshold, vl_json,
             alert_type, priority_weight, jurisdiction, now, now, SCHEMA_VERSION),
        )
        r = dict(conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone())
    if r["value_list"]:
        r["value_list"] = json.loads(r["value_list"])
    return r


def get_rule(db_path: Path, rule_id: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        raise RuleNotFound(rule_id)
    r = dict(row)
    if r["value_list"]:
        r["value_list"] = json.loads(r["value_list"])
    return r


def list_rules(db_path: Path, *, enabled_only: bool = False, jurisdiction: str | None = None) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if enabled_only:
        clauses.append("enabled = 1")
    if jurisdiction is not None:
        clauses.append("jurisdiction = ?")
        params.append(jurisdiction)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM rules {where} ORDER BY created_at DESC", params).fetchall()  # nosec B608 - `where` is built only from hardcoded clause literals ("enabled = 1", "jurisdiction = ?"); the only actual value (jurisdiction) is bound via `params` and a `?` placeholder
    results = []
    for r in rows:
        d = dict(r)
        if d["value_list"]:
            d["value_list"] = json.loads(d["value_list"])
        results.append(d)
    return results


def toggle_rule(db_path: Path, rule_id: str, *, enabled: bool) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT id FROM rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise RuleNotFound(rule_id)
        conn.execute(
            "UPDATE rules SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, _utcnow(), rule_id),
        )
        return dict(conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone())


# ---------------------------------------------------------------------------
# Evaluation engine
# ---------------------------------------------------------------------------


def _eval_condition(rule: dict, event: dict[str, Any]) -> bool:
    op = rule["condition_op"]
    threshold = rule.get("threshold")
    value_list = rule.get("value_list") or []
    if isinstance(value_list, str):
        value_list = json.loads(value_list)

    if op == "amount_gt":
        return float(event.get("amount", 0)) > float(threshold)
    if op == "amount_gte":
        return float(event.get("amount", 0)) >= float(threshold)
    if op == "frequency_gt":
        return int(event.get("frequency", 0)) > int(threshold)
    if op == "country_in":
        return str(event.get("country", "")).upper() in [v.upper() for v in value_list]
    if op == "country_not_in":
        return str(event.get("country", "")).upper() not in [v.upper() for v in value_list]
    if op == "risk_tier_in":
        return str(event.get("risk_tier", "")).lower() in [v.lower() for v in value_list]
    if op == "industry_in":
        return str(event.get("industry", "")).lower() in [v.lower() for v in value_list]
    if op == "product_in":
        return str(event.get("product", "")).lower() in [v.lower() for v in value_list]
    if op == "pep_flag":
        return bool(event.get("pep_flag"))
    if op == "adverse_media":
        return bool(event.get("adverse_media_flag"))
    return False


def evaluate(
    db_path: Path,
    event: dict[str, Any],
    *,
    event_ref: str | None = None,
    jurisdiction: str | None = None,
) -> list[dict]:
    """Evaluate all enabled rules against an event dict.

    Returns list of triggered rule dicts with added key 'triggered_by_event'.
    Event fields used by conditions:
      amount, frequency, country, risk_tier, industry, product, pep_flag, adverse_media_flag
    """
    rules = list_rules(db_path, enabled_only=True, jurisdiction=jurisdiction)
    triggered: list[dict] = []
    now = _utcnow()

    with _db(db_path) as conn:
        for rule in rules:
            hit = _eval_condition(rule, event)
            conn.execute(
                "INSERT INTO rule_evaluations(rule_id, event_ref, triggered, evaluated_at)"
                " VALUES(?,?,?,?)",
                (rule["id"], event_ref, 1 if hit else 0, now),
            )
            if hit:
                triggered.append({**rule, "triggered_by_event": event_ref})

    return triggered
