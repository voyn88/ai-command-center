"""Case Playbooks: organizational memory-as-code for AML case handling.

Every AML case that closed with a confirmed, regulator-accepted outcome
encodes a strategy an analyst already worked out by hand: "an incident that
looks like *this* should be handled like *that*." Historically that
knowledge lived only in the closed case's audit trail — the next analyst
who saw a similar incident had to rediscover it from scratch.

This module turns that knowledge into code: each entry in `SCENARIO_CATALOG`
is one historical case ("VOYN-MIN-MEMORY-ARCH" migration target — three to
start with) rewritten as a `CaseScenario` — a match condition plus the
action that resolved it. Two entry points mirror `rule_engine`'s condition
vocabulary so a "new incident" is just the same kind of event dict
`rule_engine.evaluate` already accepts, with the alert type(s) it triggered
folded in:

- `match_scenario(incident, catalog)` — finds the first scenario in the
  catalog (authored most-specific-first) whose conditions all hold against
  a new incident, and returns it as a `ScenarioMatch` with a human-readable
  reason tracing back to the source case.
- `apply_scenario(db_path, case_id, match, actor=...)` — the "executable"
  half: replays the matched scenario's action against a real case in
  `case_store`, so a matched historical strategy becomes an actual case
  transition rather than a suggestion someone has to act on by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from command_center import case_store

# ---------------------------------------------------------------------------
# Condition vocabulary — deliberately mirrors rule_engine.CONDITION_OPERATORS
# (amount/frequency/country/risk_tier/industry/pep_flag/adverse_media) so a
# scenario condition reads the same way a rule condition does, plus
# `alert_type_in`/`amount_lt`/`no_adverse_media` for the extra nuance a
# closed-case strategy needs that a single triggering rule doesn't.
# ---------------------------------------------------------------------------


def _op_alert_type_in(incident: dict, value: list[str]) -> bool:
    triggered = incident.get("alert_types") or ([incident["alert_type"]] if incident.get("alert_type") else [])
    return any(str(t).lower() in [v.lower() for v in value] for t in triggered)


def _op_amount_gte(incident: dict, value: float) -> bool:
    return float(incident.get("amount", 0)) >= float(value)


def _op_amount_lt(incident: dict, value: float) -> bool:
    return float(incident.get("amount", 0)) < float(value)


def _op_frequency_gt(incident: dict, value: float) -> bool:
    return int(incident.get("frequency", 0)) > int(value)


def _op_country_in(incident: dict, value: list[str]) -> bool:
    return str(incident.get("country", "")).upper() in [v.upper() for v in value]


def _op_industry_in(incident: dict, value: list[str]) -> bool:
    return str(incident.get("industry", "")).lower() in [v.lower() for v in value]


def _op_risk_tier_in(incident: dict, value: list[str]) -> bool:
    return str(incident.get("risk_tier", "")).lower() in [v.lower() for v in value]


def _op_pep_flag(incident: dict, _value: Any) -> bool:
    return bool(incident.get("pep_flag"))


def _op_adverse_media(incident: dict, _value: Any) -> bool:
    return bool(incident.get("adverse_media_flag"))


def _op_no_adverse_media(incident: dict, _value: Any) -> bool:
    return not incident.get("adverse_media_flag")


CONDITION_EVALUATORS: dict[str, Callable[[dict, Any], bool]] = {
    "alert_type_in": _op_alert_type_in,
    "amount_gte": _op_amount_gte,
    "amount_lt": _op_amount_lt,
    "frequency_gt": _op_frequency_gt,
    "country_in": _op_country_in,
    "industry_in": _op_industry_in,
    "risk_tier_in": _op_risk_tier_in,
    "pep_flag": _op_pep_flag,
    "adverse_media": _op_adverse_media,
    "no_adverse_media": _op_no_adverse_media,
}

RECOMMENDED_ACTIONS: tuple[str, ...] = ("escalate_to_sar", "close")


@dataclass(frozen=True)
class ScenarioCondition:
    op: str
    value: Any = None


@dataclass(frozen=True)
class CaseScenario:
    id: str
    source_case_number: str
    title: str
    narrative: str
    conditions: tuple[ScenarioCondition, ...]
    recommended_action: str  # "escalate_to_sar" | "close"
    recommended_priority: str
    action_reason_template: str


def _condition_holds(condition: ScenarioCondition, incident: dict) -> bool:
    evaluator = CONDITION_EVALUATORS.get(condition.op)
    if evaluator is None:
        raise ValueError(f"unknown scenario condition operator {condition.op!r}")
    return evaluator(incident, condition.value)


def scenario_matches(scenario: CaseScenario, incident: dict) -> bool:
    return all(_condition_holds(condition, incident) for condition in scenario.conditions)


# ---------------------------------------------------------------------------
# The migrated catalog — three historical, regulator-confirmed cases
# rewritten as executable scenarios. Authored most-specific-first: a
# sanctions hit should win over a generic risk-escalation reading of the
# same incident.
# ---------------------------------------------------------------------------

SCENARIO_CATALOG: tuple[CaseScenario, ...] = (
    CaseScenario(
        id="MEM-001-sanctions-immediate-escalation",
        source_case_number="AML-00058",
        title="Санкционная юрисдикция контрагента — немедленная эскалация в SAR",
        narrative=(
            "Контрагент операции находился в юрисдикции под первичными санкциями "
            "OFAC. Расследование заняло менее суток: комплаенс-офицер эскалировал "
            "дело в SAR без дополнительного EDD (санкционный hit — zero-tolerance), "
            "регулятор подтвердил приём сообщения без замечаний."
        ),
        conditions=(
            ScenarioCondition("alert_type_in", ["sanctions"]),
            ScenarioCondition(
                "country_in",
                ["CU", "IR", "KP", "RU", "SY", "BY", "VE"],
            ),
        ),
        recommended_action="escalate_to_sar",
        recommended_priority="critical",
        action_reason_template="SAR-{scenario_id}-{case_number}",
    ),
    CaseScenario(
        id="MEM-002-structuring-escalation",
        source_case_number="AML-00042",
        title="Структурирование через серию операций ниже порога CTR",
        narrative=(
            "Клиент из отрасли с интенсивным оборотом наличных провёл более пяти "
            "депозитов в течение недели, каждый чуть ниже порога обязательного "
            "контроля. Расследование подтвердило признаки дробления, дело "
            "эскалировано в SAR, регулятор принял сообщение без возврата на "
            "доработку."
        ),
        conditions=(
            ScenarioCondition("alert_type_in", ["structuring"]),
            ScenarioCondition("frequency_gt", 5),
            ScenarioCondition("industry_in", ["cash_intensive"]),
        ),
        recommended_action="escalate_to_sar",
        recommended_priority="high",
        action_reason_template="SAR-{scenario_id}-{case_number}",
    ),
    CaseScenario(
        id="MEM-003-pep-clean-edd-false-positive",
        source_case_number="AML-00051",
        title="PEP-клиент с чистым EDD и суммой ниже порога STR — false positive",
        narrative=(
            "Алерт сработал только на факте PEP-статуса клиента: усиленная "
            "проверка (EDD) не выявила негативных медиа или иных отклонений, а "
            "сумма операции была ниже порога STR. Дело закрыто как false "
            "positive; повторных срабатываний по клиенту не было."
        ),
        conditions=(
            ScenarioCondition("alert_type_in", ["pep_related"]),
            ScenarioCondition("pep_flag", None),
            ScenarioCondition("no_adverse_media", None),
            ScenarioCondition("amount_lt", 100_000.0),
        ),
        recommended_action="close",
        recommended_priority="low",
        action_reason_template=(
            "False positive (по образцу {source_case_number}): EDD пройден, "
            "негативных медиа нет, сумма ниже порога STR."
        ),
    ),
)


@dataclass(frozen=True)
class ScenarioMatch:
    scenario: CaseScenario
    incident: dict
    reason: str


def match_scenario(incident: dict, catalog: tuple[CaseScenario, ...] = SCENARIO_CATALOG) -> ScenarioMatch | None:
    """Find the first scenario in `catalog` whose conditions all hold for
    `incident` — an event dict shaped like the ones `rule_engine.evaluate`
    accepts, plus `alert_type`/`alert_types` for whichever rule(s) it
    triggered. Returns `None` when no historical strategy applies; no match
    is better than a misapplied one.
    """
    for scenario in catalog:
        if scenario_matches(scenario, incident):
            return ScenarioMatch(
                scenario=scenario,
                incident=incident,
                reason=(
                    f"совпадает со сценарием «{scenario.title}» "
                    f"(мигрирован из дела {scenario.source_case_number}): "
                    f"{scenario.narrative}"
                ),
            )
    return None


def apply_scenario(db_path: Path, case_id: str, match: ScenarioMatch, *, actor: str) -> dict:
    """Execute the matched scenario's recommended action against a real
    case in `case_store` — the executable half of memory-as-code: a matched
    historical strategy becomes a `case_store` state transition, not just a
    suggestion left for someone to act on by hand.
    """
    scenario = match.scenario
    case = case_store.get_case(db_path, case_id)
    reason = scenario.action_reason_template.format(
        scenario_id=scenario.id,
        case_number=case["case_number"],
        source_case_number=scenario.source_case_number,
    )
    if scenario.recommended_action == "escalate_to_sar":
        return case_store.escalate_to_sar(db_path, case_id, actor=actor, sar_ref=reason)
    if scenario.recommended_action == "close":
        return case_store.close_case(db_path, case_id, actor=actor, closure_reason=reason)
    raise ValueError(f"unsupported recommended_action {scenario.recommended_action!r}")
