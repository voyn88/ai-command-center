"""VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-EXECUTOR: the
cross-layer contract, checked without a database.

The gate only works if three independently-edited layers agree on the same
strings:

  * the worker's refusal reason (`authority_preflight.AuthorityDecision`),
  * the backlog's park classifier and resume refusal (migration 0025),
  * the payload the planner builds.

A drift in any one of them fails silently and expensively -- an authority
park that the classifier does not recognize is re-OPENed as an ordinary
return and walks straight back into the loop this change exists to close.
These tests read the SQL as text on purpose: they pin the contract itself,
and they run on every machine, including the ones with no PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path

from command_center import authority_preflight as ap
from command_center.orchestrator.planner import PlanLimits, _payload_for

SQL = (
    Path(__file__).resolve().parents[2]
    / "command_center"
    / "db"
    / "sql"
    / "0025_backlog_required_authority.up.sql"
).read_text(encoding="utf-8")

_ROUTE = ("proj", "/srv/proj")


def _task(**overrides):
    task = {
        "task_id": "VOYN-W0-EXAMPLE",
        "wave": "0",
        "priority": "P0",
        "title": "Example",
        "body": "Do the thing.",
        "repo": "voyn88/ai-command-center",
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# The payload carries the declaration (acceptance criterion 1).
# ---------------------------------------------------------------------------


def test_the_dispatch_payload_carries_the_declaration() -> None:
    payload, _ = _payload_for(
        _task(required_authorities=["root"]), PlanLimits(), _ROUTE
    )
    assert payload["required_authorities"] == ["root"]


def test_a_split_dispatch_carries_it_too() -> None:
    """A decomposition run executes in the same clone under the same
    principal, so it faces the same wall."""
    payload, _ = _payload_for(
        _task(required_authorities=["root"]), PlanLimits(), _ROUTE, mode="split"
    )
    assert payload["required_authorities"] == ["root"]


def test_a_task_that_declares_nothing_sends_an_empty_list() -> None:
    """Always present, always a list: absent would make the worker's parse
    branch on shape as well as content, and a pre-0025 row must still
    dispatch."""
    for task in (_task(), _task(required_authorities=None)):
        payload, _ = _payload_for(task, PlanLimits(), _ROUTE)
        assert payload["required_authorities"] == []


def test_the_payload_declaration_survives_the_worker_parse() -> None:
    """End to end across the contract seam: what the planner writes is what
    `parse_agent_run` reads back."""
    from command_center.worker.payloads import parse_agent_run

    payload, _ = _payload_for(
        _task(required_authorities=["postgres_role", "root"]), PlanLimits(), _ROUTE
    )
    request = parse_agent_run(payload)
    assert request.required_authorities == ("root", "postgres_role")


def test_the_prompt_teaches_the_discovery_trailer() -> None:
    """The discovery half only works if the executor is told the contract,
    in the exact spelling `declared_by_run` accepts."""
    payload, _ = _payload_for(_task(), PlanLimits(), _ROUTE)
    assert "REQUIRES_AUTHORITY:" in payload["prompt"]
    for name in ap.AUTHORITY_ORDER:
        assert name in payload["prompt"]


# ---------------------------------------------------------------------------
# The worker's reason and the backlog's classifier are the same contract.
# ---------------------------------------------------------------------------


def test_the_park_classifier_matches_the_workers_actual_reason() -> None:
    """`backlog_ingest_results` prefixes a dead item's reason with
    `cascade_exhausted: `, so the classifier's LIKE pattern must match the
    worker's reason with that prefix applied."""
    reason = ap.decide(["root"], []).reason
    park = f"cascade_exhausted: {reason}"
    pattern = "cascade_exhausted: authority_unavailable"
    assert park.startswith(pattern)
    assert f"'{pattern}%'" in SQL, "the classifier no longer matches the worker"


def test_the_discovered_reason_is_classified_too() -> None:
    assert "'cascade_exhausted: authority_required%'" in SQL


def test_an_authority_park_goes_straight_to_the_owner() -> None:
    """Not "one free return, then park": retrying cannot grant a privilege,
    so the first return is the decision (acceptance criterion 3)."""
    assert "WHEN v_authority THEN 'DEFER_TO_USER'" in SQL


def test_an_authority_park_is_never_classified_as_technical() -> None:
    """A technical park is re-OPENed. Authority outranking technical is what
    keeps a `task_status_failed`-shaped attempt from being re-dispatched."""
    assert "v_technical := (NOT v_authority) AND (" in SQL


def test_an_authority_park_is_never_auto_resumed() -> None:
    """`backlog_resume_deferred` reopens any `cascade_exhausted:` park within
    48 hours, on the reasoning that such parks are transient. An authority
    park is not: unguarded, this change would be a 48-hour pause rather than
    a decision."""
    assert "authority_park_needs_owner" in SQL


def test_an_authority_park_is_never_handed_to_the_decomposer() -> None:
    """Splitting a task that needs root yields subtasks that each need
    root."""
    assert "v_split_requested := (NOT v_technical) AND (NOT v_authority)" in SQL


def test_the_sql_vocabulary_matches_the_python_vocabulary() -> None:
    """The closed vocabulary is duplicated in SQL (a CHECK constraint cannot
    call Python). Drift would let a name the planner can send be rejected by
    the column, or vice versa."""
    rendered = ", ".join(f"'{name}'" for name in ap.AUTHORITY_ORDER)
    assert f"ARRAY[{rendered}]::text[]" in SQL
    # And the SQL literal is the whole vocabulary, not a subset of it.
    assert len(ap.AUTHORITY_ORDER) == len(ap.AUTHORITY_VOCABULARY)


def test_the_reason_code_constants_are_the_ones_the_sql_matches() -> None:
    assert ap.REASON_UNAVAILABLE == "authority_unavailable"
