"""The write -> verify -> review ensemble route (VOYN-AGT-COUPON)."""

from __future__ import annotations

from command_center import agent_runner
from command_center.orchestrator.ensemble import (
    ROLE_REVIEWER,
    ROLE_VERIFIER,
    ROLE_WRITER,
    write_verify_review_route,
)
from command_center.orchestrator.routing import ROUTING_MATRIX


def test_route_is_exactly_three_stages_in_write_verify_review_order():
    route = write_verify_review_route()
    assert [stage.role for stage in route] == [ROLE_WRITER, ROLE_VERIFIER, ROLE_REVIEWER]


def test_every_stage_has_a_non_empty_cascade():
    for stage in write_verify_review_route():
        assert stage.cascade, stage.role
        for link in stage.cascade:
            assert isinstance(link.get("executor"), str) and link["executor"]
            assert isinstance(link.get("task_type"), str) and link["task_type"]


def test_only_the_writer_stage_can_mutate():
    """The control-layout invariant: each role's task_type must resolve to
    strictly the capability its role needs. A writer stage retyped into a
    read-only/model-only task_type could never actually implement anything;
    a verifier/reviewer stage retyped into a mutating task_type would let a
    "verify" or "review" run silently gain write access to the tree."""
    route = write_verify_review_route()
    by_role = {stage.role: stage for stage in route}

    for link in by_role[ROLE_WRITER].cascade:
        assert link["task_type"] in agent_runner.MUTATING_TASK_TYPES

    for link in by_role[ROLE_VERIFIER].cascade:
        assert link["task_type"] in agent_runner.READ_ONLY_TASK_TYPES
        assert link["task_type"] not in agent_runner.MUTATING_TASK_TYPES

    for link in by_role[ROLE_REVIEWER].cascade:
        assert link["task_type"] in agent_runner.MODEL_ONLY_TASK_TYPES
        assert link["task_type"] not in agent_runner.MUTATING_TASK_TYPES


def test_verifier_and_reviewer_stages_are_read_review_task_types():
    """Both verdict roles spend the metered review key
    (`agent_runner.REVIEW_TASK_TYPES`), never the plain `review` chat/audit
    task_type -- money is for verdicts, not conversation."""
    route = write_verify_review_route()
    by_role = {stage.role: stage for stage in route}
    for role in (ROLE_VERIFIER, ROLE_REVIEWER):
        for link in by_role[role].cascade:
            assert link["task_type"] in agent_runner.REVIEW_TASK_TYPES


def test_route_reuses_the_live_proven_executor_matrix_not_a_private_copy():
    """The route must be built from `ROUTING_MATRIX["implementation"]`
    (planner.py's own source), so it can never diverge from what the
    orchestrator actually dispatches for a real task."""
    route = write_verify_review_route()
    writer_executors = [link["executor"] for link in route[0].cascade]
    matrix_executors = [link["executor"] for link in ROUTING_MATRIX["implementation"]]
    assert writer_executors == matrix_executors


def test_route_returns_fresh_cascades_each_call():
    """Two calls must not share mutable state -- mutating one route's stage
    must never leak into the next caller's."""
    first = write_verify_review_route()
    first[0].cascade[0]["executor"] = "mutated"
    second = write_verify_review_route()
    assert second[0].cascade[0]["executor"] != "mutated"
