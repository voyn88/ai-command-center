"""Tests for Phase 3: rule_engine, evidence_store, seed_rules_115fz."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from command_center import evidence_store, rule_engine
from command_center.evidence_store import EvidenceNotFound, InvalidValue as EvInvalidValue
from command_center.rule_engine import InvalidCondition, RuleNotFound
from command_center.seed_rules_115fz import seed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def r_db(tmp_path: Path) -> Path:
    p = tmp_path / "rules.db"
    rule_engine.init_db(p)
    return p


@pytest.fixture()
def e_db(tmp_path: Path) -> Path:
    p = tmp_path / "evidence.db"
    evidence_store.init_db(p)
    return p


def _make_rule(r_db: Path, **overrides) -> dict:
    defaults = dict(
        name="Test CTR rule",
        condition_op="amount_gt",
        threshold=600_000.0,
        alert_type="ctr",
        priority_weight="high",
        jurisdiction="RU",
    )
    defaults.update(overrides)
    return rule_engine.create_rule(r_db, **defaults)


# ---------------------------------------------------------------------------
# rule_engine — CRUD
# ---------------------------------------------------------------------------


def test_create_rule_scalar(r_db: Path) -> None:
    r = _make_rule(r_db)
    assert r["condition_op"] == "amount_gt"
    assert r["threshold"] == 600_000.0
    assert r["enabled"] == 1


def test_create_rule_list(r_db: Path) -> None:
    r = rule_engine.create_rule(
        r_db,
        name="Country check",
        condition_op="country_in",
        value_list=["IR", "KP"],
        alert_type="sanctions",
        priority_weight="critical",
    )
    assert r["value_list"] == ["IR", "KP"]


def test_create_rule_flag(r_db: Path) -> None:
    r = rule_engine.create_rule(
        r_db,
        name="PEP rule",
        condition_op="pep_flag",
        alert_type="pep_related",
        priority_weight="critical",
    )
    assert r["condition_op"] == "pep_flag"


def test_create_rule_invalid_op(r_db: Path) -> None:
    with pytest.raises(InvalidCondition, match="unknown operator"):
        rule_engine.create_rule(
            r_db,
            name="Bad",
            condition_op="nonexistent_op",
            threshold=1.0,
            alert_type="ctr",
            priority_weight="low",
        )


def test_create_rule_scalar_missing_threshold(r_db: Path) -> None:
    with pytest.raises(InvalidCondition, match="requires a numeric threshold"):
        rule_engine.create_rule(
            r_db,
            name="Bad scalar",
            condition_op="amount_gt",
            alert_type="ctr",
            priority_weight="low",
        )


def test_create_rule_list_missing_value_list(r_db: Path) -> None:
    with pytest.raises(InvalidCondition, match="requires a non-empty value_list"):
        rule_engine.create_rule(
            r_db,
            name="Bad list",
            condition_op="country_in",
            alert_type="high_risk_country",
            priority_weight="low",
        )


def test_get_rule_not_found(r_db: Path) -> None:
    with pytest.raises(RuleNotFound):
        rule_engine.get_rule(r_db, "nonexistent")


def test_list_rules_enabled_filter(r_db: Path) -> None:
    r = _make_rule(r_db)
    rule_engine.toggle_rule(r_db, r["id"], enabled=False)
    all_rules = rule_engine.list_rules(r_db)
    enabled = rule_engine.list_rules(r_db, enabled_only=True)
    assert len(all_rules) == 1
    assert len(enabled) == 0


def test_list_rules_jurisdiction_filter(r_db: Path) -> None:
    _make_rule(r_db, name="RU rule", jurisdiction="RU")
    rule_engine.create_rule(
        r_db,
        name="Global rule",
        condition_op="pep_flag",
        alert_type="pep_related",
        priority_weight="high",
        jurisdiction="global",
    )
    ru_rules = rule_engine.list_rules(r_db, jurisdiction="RU")
    global_rules = rule_engine.list_rules(r_db, jurisdiction="global")
    assert len(ru_rules) == 1
    assert len(global_rules) == 1


def test_toggle_rule(r_db: Path) -> None:
    r = _make_rule(r_db)
    assert r["enabled"] == 1
    toggled = rule_engine.toggle_rule(r_db, r["id"], enabled=False)
    assert toggled["enabled"] == 0
    re_enabled = rule_engine.toggle_rule(r_db, r["id"], enabled=True)
    assert re_enabled["enabled"] == 1


# ---------------------------------------------------------------------------
# rule_engine — evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_amount_gt_triggers(r_db: Path) -> None:
    _make_rule(r_db, threshold=600_000.0)
    triggered = rule_engine.evaluate(r_db, {"amount": 700_000})
    assert len(triggered) == 1


def test_evaluate_amount_gt_no_trigger(r_db: Path) -> None:
    _make_rule(r_db, threshold=600_000.0)
    triggered = rule_engine.evaluate(r_db, {"amount": 500_000})
    assert triggered == []


def test_evaluate_country_in_triggers(r_db: Path) -> None:
    rule_engine.create_rule(
        r_db,
        name="Iran check",
        condition_op="country_in",
        value_list=["IR", "KP"],
        alert_type="sanctions",
        priority_weight="critical",
    )
    triggered = rule_engine.evaluate(r_db, {"country": "IR"})
    assert len(triggered) == 1


def test_evaluate_country_in_case_insensitive(r_db: Path) -> None:
    rule_engine.create_rule(
        r_db,
        name="Iran check lower",
        condition_op="country_in",
        value_list=["ir", "kp"],
        alert_type="sanctions",
        priority_weight="critical",
    )
    triggered = rule_engine.evaluate(r_db, {"country": "IR"})
    assert len(triggered) == 1


def test_evaluate_pep_flag_triggers(r_db: Path) -> None:
    rule_engine.create_rule(
        r_db,
        name="PEP",
        condition_op="pep_flag",
        alert_type="pep_related",
        priority_weight="critical",
    )
    assert rule_engine.evaluate(r_db, {"pep_flag": True}) != []
    assert rule_engine.evaluate(r_db, {"pep_flag": False}) == []


def test_evaluate_risk_tier_in(r_db: Path) -> None:
    rule_engine.create_rule(
        r_db,
        name="High tier",
        condition_op="risk_tier_in",
        value_list=["high", "pep"],
        alert_type="risk_escalation",
        priority_weight="high",
    )
    assert rule_engine.evaluate(r_db, {"risk_tier": "high"}) != []
    assert rule_engine.evaluate(r_db, {"risk_tier": "low"}) == []


def test_evaluate_disabled_rule_not_triggered(r_db: Path) -> None:
    r = _make_rule(r_db, threshold=100.0)
    rule_engine.toggle_rule(r_db, r["id"], enabled=False)
    triggered = rule_engine.evaluate(r_db, {"amount": 999_999})
    assert triggered == []


def test_evaluate_event_ref_stored(r_db: Path) -> None:
    _make_rule(r_db, threshold=100.0)
    rule_engine.evaluate(r_db, {"amount": 200}, event_ref="txn-abc-123")
    conn = sqlite3.connect(r_db)
    try:
        rows = conn.execute(
            "SELECT event_ref, triggered FROM rule_evaluations"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("txn-abc-123", 1)]


def test_evaluate_multiple_rules(r_db: Path) -> None:
    _make_rule(r_db, name="CTR", threshold=600_000.0, alert_type="ctr")
    rule_engine.create_rule(
        r_db,
        name="PEP",
        condition_op="pep_flag",
        alert_type="pep_related",
        priority_weight="critical",
    )
    # Both should trigger
    triggered = rule_engine.evaluate(r_db, {"amount": 700_000, "pep_flag": True})
    assert len(triggered) == 2


# ---------------------------------------------------------------------------
# evidence_store
# ---------------------------------------------------------------------------


def test_attach_evidence_happy(e_db: Path) -> None:
    ev = evidence_store.attach_evidence(
        e_db,
        entity_type="alert",
        entity_id="alert-123",
        evidence_type="document",
        title="Паспорт",
        file_ref="/docs/passport.pdf",
        submitted_by="Analyst",
    )
    assert ev["entity_type"] == "alert"
    assert ev["title"] == "Паспорт"


def test_attach_evidence_invalid_entity_type(e_db: Path) -> None:
    with pytest.raises(EvInvalidValue, match="unknown entity_type"):
        evidence_store.attach_evidence(
            e_db,
            entity_type="banana",
            entity_id="x",
            evidence_type="document",
            title="X",
            file_ref="/x",
            submitted_by="Analyst",
        )


def test_attach_evidence_invalid_evidence_type(e_db: Path) -> None:
    with pytest.raises(EvInvalidValue, match="unknown evidence_type"):
        evidence_store.attach_evidence(
            e_db,
            entity_type="alert",
            entity_id="x",
            evidence_type="unicorn",
            title="X",
            file_ref="/x",
            submitted_by="Analyst",
        )


def test_attach_evidence_blank_title(e_db: Path) -> None:
    with pytest.raises(EvInvalidValue, match="title"):
        evidence_store.attach_evidence(
            e_db,
            entity_type="alert",
            entity_id="x",
            evidence_type="document",
            title="   ",
            file_ref="/x",
            submitted_by="Analyst",
        )


def test_attach_evidence_requires_ref_or_url(e_db: Path) -> None:
    with pytest.raises(EvInvalidValue, match="file_ref or url"):
        evidence_store.attach_evidence(
            e_db,
            entity_type="alert",
            entity_id="x",
            evidence_type="document",
            title="Missing ref",
            submitted_by="Analyst",
        )


def test_get_evidence_not_found(e_db: Path) -> None:
    with pytest.raises(EvidenceNotFound):
        evidence_store.get_evidence(e_db, "nonexistent")


def test_list_evidence_returns_newest_first(e_db: Path) -> None:
    for i in range(3):
        evidence_store.attach_evidence(
            e_db,
            entity_type="alert",
            entity_id="alert-1",
            evidence_type="document",
            title=f"Doc {i}",
            file_ref=f"/docs/{i}.pdf",
            submitted_by="Analyst",
        )
    items = evidence_store.list_evidence(e_db, entity_type="alert", entity_id="alert-1")
    assert len(items) == 3
    # newest first — last inserted should be first
    assert items[0]["title"] == "Doc 2"


def test_list_evidence_filter_by_type(e_db: Path) -> None:
    evidence_store.attach_evidence(
        e_db, entity_type="alert", entity_id="a1", evidence_type="document",
        title="D", file_ref="/d", submitted_by="X",
    )
    evidence_store.attach_evidence(
        e_db, entity_type="alert", entity_id="a1", evidence_type="screenshot",
        title="S", url="https://example.com", submitted_by="X",
    )
    docs = evidence_store.list_evidence(e_db, entity_type="alert", entity_id="a1",
                                        evidence_type="document")
    assert len(docs) == 1
    assert docs[0]["evidence_type"] == "document"


def test_count_evidence(e_db: Path) -> None:
    for i in range(4):
        evidence_store.attach_evidence(
            e_db, entity_type="customer", entity_id="cust-42",
            evidence_type="kyc_document", title=f"KYC {i}",
            url=f"https://vault/{i}", submitted_by="Officer",
        )
    assert evidence_store.count_evidence(e_db, entity_type="customer", entity_id="cust-42") == 4
    assert evidence_store.count_evidence(e_db, entity_type="customer", entity_id="cust-99") == 0


def test_evidence_is_immutable(e_db: Path) -> None:
    """evidence_store has no update/delete — verify the API surface."""
    assert not hasattr(evidence_store, "delete_evidence")
    assert not hasattr(evidence_store, "update_evidence")


# ---------------------------------------------------------------------------
# 115-ФЗ seed
# ---------------------------------------------------------------------------


def test_seed_creates_rules(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    n = seed(db)
    assert n > 0
    rules = rule_engine.list_rules(db)
    assert len(rules) == n


def test_seed_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    n1 = seed(db)
    n2 = seed(db)
    assert n2 == 0  # second run creates nothing
    assert len(rule_engine.list_rules(db)) == n1


def test_seed_includes_ctr_rule(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    seed(db)
    rules = rule_engine.list_rules(db)
    ctr_rules = [r for r in rules if r["alert_type"] == "ctr"]
    assert len(ctr_rules) >= 1


def test_seed_includes_pep_rule(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    seed(db)
    rules = rule_engine.list_rules(db)
    pep_rules = [r for r in rules if r["alert_type"] == "pep_related"]
    assert len(pep_rules) >= 1


def test_seed_includes_sanctions_rule(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    seed(db)
    rules = rule_engine.list_rules(db)
    sanction_rules = [r for r in rules if r["alert_type"] == "sanctions"]
    assert len(sanction_rules) >= 1


def test_seed_ctr_triggers_on_600k(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    seed(db)
    triggered = rule_engine.evaluate(db, {"amount": 650_000})
    types = {r["alert_type"] for r in triggered}
    assert "ctr" in types


def test_seed_sanctions_triggers_for_iran(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    seed(db)
    triggered = rule_engine.evaluate(db, {"country": "IR"})
    types = {r["alert_type"] for r in triggered}
    # Iran is on both the seeded OFAC sanctions list and the FATF high-risk
    # list, so both rules must fire independently — an "or" here would stay
    # green even if the sanctions rule itself silently lost Iran.
    assert {"sanctions", "high_risk_country"} <= types


def test_seed_pep_triggers_for_pep_customer(tmp_path: Path) -> None:
    db = tmp_path / "rules_seed.db"
    seed(db)
    triggered = rule_engine.evaluate(db, {"pep_flag": True})
    types = {r["alert_type"] for r in triggered}
    assert "pep_related" in types
