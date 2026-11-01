"""Compliance Store — read-only aggregation layer for the compliance dashboard.

Queries each module's SQLite DB directly for reporting metrics.
No writes, no state changes — pure aggregation.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from command_center import (
    customer_store,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _fetchall(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db_path.exists():
        return []
    conn = _connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _fetchone(db_path: Path, sql: str, params: tuple = ()) -> dict | None:
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _scalar(db_path: Path, sql: str, params: tuple = (), default=0):
    if not db_path.exists():
        return default
    conn = _connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Alert stats
# ---------------------------------------------------------------------------


def alert_stats(alert_db: Path) -> dict:
    """Return alert counts by state, outcome breakdown, overdue count."""
    by_state = _fetchall(
        alert_db,
        "SELECT state, COUNT(*) AS cnt FROM alerts GROUP BY state",
    )
    by_outcome = _fetchall(
        alert_db,
        "SELECT outcome, COUNT(*) AS cnt FROM alerts WHERE outcome IS NOT NULL GROUP BY outcome",
    )
    overdue = _scalar(
        alert_db,
        "SELECT COUNT(*) FROM alerts WHERE overdue=1 AND state NOT IN ('closed','archived')",
    )
    total = _scalar(alert_db, "SELECT COUNT(*) FROM alerts")
    open_count = _scalar(
        alert_db,
        "SELECT COUNT(*) FROM alerts WHERE state NOT IN ('closed','archived','escalated')",
    )
    recent_30d = _scalar(
        alert_db,
        "SELECT COUNT(*) FROM alerts WHERE created_at >= ?",
        (_days_ago(30),),
    )
    closed_30d = _scalar(
        alert_db,
        "SELECT COUNT(*) FROM alerts WHERE state='closed' AND updated_at >= ?",
        (_days_ago(30),),
    )
    true_positive = _scalar(
        alert_db,
        "SELECT COUNT(*) FROM alerts WHERE outcome='true_positive'",
    )
    false_positive = _scalar(
        alert_db,
        "SELECT COUNT(*) FROM alerts WHERE outcome='false_positive'",
    )
    return {
        "total": total,
        "open": open_count,
        "overdue": overdue,
        "recent_30d": recent_30d,
        "closed_30d": closed_30d,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "by_state": {r["state"]: r["cnt"] for r in by_state},
        "by_outcome": {(r["outcome"] or "none"): r["cnt"] for r in by_outcome},
    }


def alert_trend(alert_db: Path, days: int = 30) -> list[dict]:
    """Return daily alert creation counts for the last N days."""
    rows = _fetchall(
        alert_db,
        """
        SELECT DATE(created_at) AS day, COUNT(*) AS cnt
        FROM alerts
        WHERE created_at >= ?
        GROUP BY DATE(created_at)
        ORDER BY day
        """,
        (_days_ago(days),),
    )
    return rows


# ---------------------------------------------------------------------------
# Case stats
# ---------------------------------------------------------------------------


def case_stats(case_db: Path) -> dict:
    """Return case counts by state and priority."""
    by_state = _fetchall(
        case_db,
        "SELECT state, COUNT(*) AS cnt FROM cases GROUP BY state",
    )
    by_priority = _fetchall(
        case_db,
        "SELECT priority, COUNT(*) AS cnt FROM cases GROUP BY priority",
    )
    total = _scalar(case_db, "SELECT COUNT(*) FROM cases")
    active = _scalar(
        case_db,
        "SELECT COUNT(*) FROM cases WHERE state NOT IN ('closed','escalated_to_sar')",
    )
    escalated_to_sar = _scalar(
        case_db,
        "SELECT COUNT(*) FROM cases WHERE state='escalated_to_sar'",
    )
    closed_30d = _scalar(
        case_db,
        "SELECT COUNT(*) FROM cases WHERE state='closed' AND updated_at >= ?",
        (_days_ago(30),),
    )
    unassigned = _scalar(
        case_db,
        "SELECT COUNT(*) FROM cases WHERE assigned_to IS NULL AND state NOT IN ('closed','escalated_to_sar')",
    )
    by_investigator = _fetchall(
        case_db,
        """
        SELECT assigned_to, COUNT(*) AS cnt
        FROM cases
        WHERE assigned_to IS NOT NULL AND state NOT IN ('closed','escalated_to_sar')
        GROUP BY assigned_to
        ORDER BY cnt DESC
        LIMIT 10
        """,
    )
    return {
        "total": total,
        "active": active,
        "escalated_to_sar": escalated_to_sar,
        "closed_30d": closed_30d,
        "unassigned": unassigned,
        "by_state": {r["state"]: r["cnt"] for r in by_state},
        "by_priority": {r["priority"]: r["cnt"] for r in by_priority},
        "by_investigator": by_investigator,
    }


# ---------------------------------------------------------------------------
# SAR stats
# ---------------------------------------------------------------------------


def sar_stats(sar_db: Path) -> dict:
    """Return SAR counts by state and type, overdue count."""
    by_state = _fetchall(
        sar_db,
        "SELECT state, COUNT(*) AS cnt FROM sars GROUP BY state",
    )
    by_type = _fetchall(
        sar_db,
        "SELECT sar_type, COUNT(*) AS cnt FROM sars GROUP BY sar_type",
    )
    total = _scalar(sar_db, "SELECT COUNT(*) FROM sars")
    acknowledged = _scalar(
        sar_db,
        "SELECT COUNT(*) FROM sars WHERE state='acknowledged'",
    )
    submitted_30d = _scalar(
        sar_db,
        "SELECT COUNT(*) FROM sars WHERE state IN ('submitted','acknowledged') AND submitted_at >= ?",
        (_days_ago(30),),
    )
    pending_filing = _scalar(
        sar_db,
        "SELECT COUNT(*) FROM sars WHERE state NOT IN ('submitted','acknowledged','rejected')",
    )

    # Overdue: deadline passed and not yet submitted
    all_sars = _fetchall(sar_db, "SELECT * FROM sars")
    now = _utcnow()
    overdue_list = [
        s for s in all_sars
        if s["state"] not in ("submitted", "acknowledged")
        and s.get("filing_deadline", now) < now
    ]
    return {
        "total": total,
        "acknowledged": acknowledged,
        "pending_filing": pending_filing,
        "submitted_30d": submitted_30d,
        "overdue": len(overdue_list),
        "overdue_items": overdue_list,
        "by_state": {r["state"]: r["cnt"] for r in by_state},
        "by_type": {r["sar_type"]: r["cnt"] for r in by_type},
    }


# ---------------------------------------------------------------------------
# Customer / risk stats
# ---------------------------------------------------------------------------


def customer_stats(customer_db: Path) -> dict:
    """Return customer risk tier and KYC status distributions."""
    by_tier = _fetchall(
        customer_db,
        "SELECT risk_tier, COUNT(*) AS cnt FROM customers GROUP BY risk_tier",
    )
    by_kyc = _fetchall(
        customer_db,
        "SELECT kyc_status, COUNT(*) AS cnt FROM customers GROUP BY kyc_status",
    )
    total = _scalar(customer_db, "SELECT COUNT(*) FROM customers")
    pep_count = _scalar(customer_db, "SELECT COUNT(*) FROM customers WHERE pep_flag=1")
    adverse_media = _scalar(
        customer_db,
        "SELECT COUNT(*) FROM customers WHERE adverse_media_flag=1",
    )
    high_risk = _scalar(
        customer_db,
        "SELECT COUNT(*) FROM customers WHERE risk_tier IN ('high','pep')",
    )
    kyc_verified = _scalar(
        customer_db,
        "SELECT COUNT(*) FROM customers WHERE kyc_status='verified'",
    )
    kyc_completion_rate = round(kyc_verified / total * 100, 1) if total > 0 else 0.0

    # Due for review
    try:
        due = customer_store.get_customers_due_for_review(customer_db)
        due_count = len(due)
    except Exception:
        due_count = 0

    return {
        "total": total,
        "pep": pep_count,
        "adverse_media": adverse_media,
        "high_risk": high_risk,
        "kyc_verified": kyc_verified,
        "kyc_completion_rate": kyc_completion_rate,
        "due_for_review": due_count,
        "by_tier": {r["risk_tier"]: r["cnt"] for r in by_tier},
        "by_kyc_status": {r["kyc_status"]: r["cnt"] for r in by_kyc},
    }


# ---------------------------------------------------------------------------
# Rule engine stats
# ---------------------------------------------------------------------------


def rule_stats(rules_db: Path) -> dict:
    """Return rule counts and top-triggered rules."""
    if not rules_db.exists():
        return {"total": 0, "enabled": 0, "by_type": {}, "top_triggered": []}

    total = _scalar(rules_db, "SELECT COUNT(*) FROM rules")
    enabled = _scalar(rules_db, "SELECT COUNT(*) FROM rules WHERE enabled=1")
    by_type = _fetchall(
        rules_db,
        "SELECT alert_type, COUNT(*) AS cnt FROM rules GROUP BY alert_type",
    )
    top_triggered = _fetchall(
        rules_db,
        """
        SELECT r.name, r.alert_type, r.priority_weight,
               COUNT(e.id) AS hit_count
        FROM rules r
        LEFT JOIN rule_evaluations e ON e.rule_id = r.id AND e.triggered = 1
        GROUP BY r.id
        ORDER BY hit_count DESC
        LIMIT 10
        """,
    )
    total_evaluations = _scalar(rules_db, "SELECT COUNT(*) FROM rule_evaluations")
    total_hits = _scalar(
        rules_db,
        "SELECT COUNT(*) FROM rule_evaluations WHERE triggered=1",
    )
    return {
        "total": total,
        "enabled": enabled,
        "total_evaluations": total_evaluations,
        "total_hits": total_hits,
        "hit_rate": round(total_hits / total_evaluations * 100, 1) if total_evaluations > 0 else 0.0,
        "by_type": {r["alert_type"]: r["cnt"] for r in by_type},
        "top_triggered": top_triggered,
    }


# ---------------------------------------------------------------------------
# Overdue summary (cross-module)
# ---------------------------------------------------------------------------


def overdue_summary(
    alert_db: Path,
    case_db: Path,
    sar_db: Path,
    customer_db: Path,
) -> dict:
    """Single cross-module overdue count for the top-level warning banner."""
    a_stats = alert_stats(alert_db)
    s_stats = sar_stats(sar_db)

    try:
        due_customers = customer_store.get_customers_due_for_review(customer_db)
    except Exception:
        due_customers = []

    return {
        "overdue_alerts": a_stats["overdue"],
        "overdue_sars": s_stats["overdue"],
        "customers_due_review": len(due_customers),
        "total": a_stats["overdue"] + s_stats["overdue"] + len(due_customers),
    }


# ---------------------------------------------------------------------------
# Compliance health score (0-100) — for bank acceptance view
# ---------------------------------------------------------------------------


def compliance_health(
    alert_db: Path,
    case_db: Path,
    sar_db: Path,
    customer_db: Path,
) -> dict:
    """
    Composite score used in the bank acceptance package.

    Scoring (each pillar 0-25 pts, total 0-100):
      KYC completion     — 25 pts × completion_rate
      SAR filing         — 25 pts, -5 per overdue SAR (min 0)
      Alert closure rate — 25 pts × (closed_30d / max(recent_30d, 1))
      Case resolution    — 25 pts × (closed_30d / max(total_active, 1))
    """
    a = alert_stats(alert_db)
    s = sar_stats(sar_db)
    cu = customer_stats(customer_db)
    ca = case_stats(case_db)

    kyc_score = 25 * (cu["kyc_completion_rate"] / 100)

    sar_score = max(0.0, 25.0 - 5.0 * s["overdue"])

    alert_closure = a["closed_30d"] / max(a["recent_30d"], 1)
    alert_score = 25 * min(alert_closure, 1.0)

    case_total = max(ca["active"] + ca["closed_30d"], 1)
    case_score = 25 * min(ca["closed_30d"] / case_total, 1.0)

    total = round(kyc_score + sar_score + alert_score + case_score, 1)
    return {
        "total": total,
        "kyc_score": round(kyc_score, 1),
        "sar_score": round(sar_score, 1),
        "alert_score": round(alert_score, 1),
        "case_score": round(case_score, 1),
        "rating": _health_rating(total),
    }


def _health_rating(score: float) -> str:
    if score >= 85:
        return "SATISFACTORY"
    if score >= 70:
        return "NEEDS_IMPROVEMENT"
    if score >= 50:
        return "DEFICIENT"
    return "CRITICAL"
