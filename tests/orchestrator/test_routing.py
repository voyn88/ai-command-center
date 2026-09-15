"""The routing matrix (BO-S2a): static, honest, hermetic."""

from __future__ import annotations

from command_center import agent_runner
from command_center.orchestrator.planner import PlanLimits, _payload_for
from command_center.orchestrator.routing import ROUTING_MATRIX, cascade_for

#: The executors the worker can actually run, read from the SAME table the
#: worker itself gates on (`handlers._run_agent` refuses any executor absent
#: from it) rather than restated here. Restating it was the earlier shape and
#: it had a latent flaw: two lists that must agree but nothing forcing them
#: to, so a matrix entry could be "proven" by editing a test constant while
#: no argv builder existed. Deriving it means an executor can only enter the
#: matrix by actually becoming runnable -- a phantom link would not fail
#: loudly (the unavailability path advances the cascade, silently burning one
#: attempt of every task's budget), so the check has to be structural.
PROVEN_EXECUTORS = set(agent_runner.COMMAND_BUILDERS)


def test_no_phantom_executors_in_the_matrix():
    for task_class, cascade in ROUTING_MATRIX.items():
        for link in cascade:
            assert link["executor"] in PROVEN_EXECUTORS, (task_class, link)


def test_the_implementation_escalation_link_is_a_different_account():
    """VOYN-W0-AICC-EXECUTOR-CODEX: the escalation link used to be a second
    `claude` entry. Live-measured 2026-08-23, that could not work as an
    escalation: the Claude credential is a Max subscription whose 5-hour cap
    caused 142 of 167 parked failures, and a second attempt lands in the same
    exhausted pool. An escalation must reach capacity the first link's limit
    cannot consume."""
    cascade = ROUTING_MATRIX["implementation"]
    assert len(cascade) >= 2, "implementation must keep an escalation link"
    assert cascade[0]["executor"] != cascade[1]["executor"], (
        "the escalation link must not re-use the first link's account/quota"
    )


def test_every_cascade_is_non_empty_and_typed():
    for task_class, cascade in ROUTING_MATRIX.items():
        assert cascade, task_class
        for link in cascade:
            assert isinstance(link.get("executor"), str) and link["executor"]
            assert isinstance(link.get("task_type"), str) and link["task_type"]


def test_cascade_for_returns_copies_not_the_matrix():
    first = cascade_for("review")
    first[0]["executor"] = "mutated"
    assert ROUTING_MATRIX["review"][0]["executor"] == "codex"


def test_review_uses_copilot_then_claude_once_each():
    cascade = cascade_for("review")
    assert [link["executor"] for link in cascade] == ["codex", "copilot", "claude"]
    assert all(link["task_type"] == "review" for link in cascade)


def test_unknown_task_class_falls_back_to_implementation():
    assert cascade_for("martian") == cascade_for("implementation")


def test_dispatch_prompt_asks_for_the_commit_and_not_for_a_pull_request() -> None:
    """VOYN-W0-AICC-AGENT-COMMIT-CONTRACT-GAP (found live 2026-08-30).

    The dispatch prompt used to say "When you open or update a pull request..."
    -- the one action an agent cannot perform, since push capability is
    withheld and `orchestrator.publish.publish_run` under the writer lease is
    the only publisher -- while never asking for the one action that publisher
    requires: the commit. Completed work therefore sat uncommitted in the task
    clone, `workspace_provisioning`'s `agent_worktree_clean` refused it, and
    the cascade spent every remaining attempt (and the model call behind each)
    reproducing the same refusal before reporting `cascade_exhausted`.

    This lives beside the routing matrix rather than in `tests/db` because the
    planner's prompt is a hermetic property of the payload: the database-backed
    planner suite is skipped wholesale without `AICC_TEST_PG_ADMIN_DSN`, and a
    gate that only runs when a database happens to be configured is not a gate
    on the contract it is meant to hold.
    """
    task = {
        "task_id": "VOYN-W0-PROMPT-CONTRACT",
        "wave": "0",
        "priority": "P0",
        "title": "t",
        "body": "b",
    }
    payload, _budget = _payload_for(task, PlanLimits(), ("AICC", "/srv/repo"))
    prompt = payload["prompt"]

    assert "git commit" in prompt
    assert "Do NOT push" in prompt
    assert "do NOT open a pull request" in prompt
    # The evidence trailer the orchestrator parses must survive any rewrite.
    assert "HEAD_SHA: <the branch head commit sha>" in prompt
    # The instruction that asked the agent to publish its own work is gone.
    assert "When you open or update a pull request" not in prompt


# ---------------------------------------------------------------------------
# The capability contract in the payload (VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-
# TO-UNPRIVILEGED-EXECUTOR, acceptance 1 and 3). Hermetic properties of the
# payload, so they belong here rather than in the database-gated planner suite
# -- a contract checked only when a database happens to be configured is not
# a checked contract.
# ---------------------------------------------------------------------------


def _task(**overrides):
    task = {
        "task_id": "VOYN-W0-AUTHORITY",
        "wave": "0",
        "priority": "P0",
        "title": "t",
        "body": "b",
    }
    task.update(overrides)
    return task


def test_the_payload_states_the_authority_the_task_requires(monkeypatch) -> None:
    from command_center.orchestrator import authority_preflight

    monkeypatch.setitem(
        authority_preflight.EXECUTOR_AUTHORITY, "claude", frozenset({"root"})
    )
    payload, _budget = _payload_for(
        _task(body="Restart it: run `sudo systemctl restart aicc-worker`."),
        PlanLimits(),
        ("AICC", "/srv/repo"),
    )
    assert payload["required_authority"] == ["root"]


def test_no_payload_is_built_for_a_requirement_no_executor_grants() -> None:
    """The planner parks such a task instead of dispatching it, so reaching
    here is a bug — and a payload whose cascade is empty names no executor at
    all, which the worker would index past. Fail loudly, not quietly."""
    import pytest

    with pytest.raises(ValueError, match="no executor grants"):
        _payload_for(
            _task(body="Restart it: run `sudo systemctl restart aicc-worker`."),
            PlanLimits(),
            ("AICC", "/srv/repo"),
        )


def test_an_ordinary_task_states_an_empty_requirement_not_a_missing_one() -> None:
    """Absent would be indistinguishable from an older payload; explicit and
    empty is a statement, and the worker gate reads it as one."""
    payload, _budget = _payload_for(_task(), PlanLimits(), ("AICC", "/srv/repo"))
    assert payload["required_authority"] == []
    assert payload["suspected_authority"] == []


def test_quoted_evidence_travels_as_a_suspicion_not_a_requirement() -> None:
    payload, _budget = _payload_for(
        _task(body="The agent tried to run `sudo /usr/bin/true` and was refused."),
        PlanLimits(),
        ("AICC", "/srv/repo"),
    )
    assert payload["required_authority"] == []
    assert payload["suspected_authority"] == ["root"]


def test_a_requirement_narrows_the_cascade_to_the_executors_that_grant_it(
    monkeypatch,
) -> None:
    """Acceptance 3: route to an executor that HAS the authority. Today none
    does -- the planner parks such a task before this code is reached -- so a
    granted lane has to be installed to exercise the routing half at all."""
    from command_center.orchestrator import authority_preflight

    monkeypatch.setitem(
        authority_preflight.EXECUTOR_AUTHORITY, "codex", frozenset({"root"})
    )
    task = _task(body="Restart it: run `sudo systemctl restart aicc-worker`.")
    decision = authority_preflight.decide(task["title"], task["body"])
    assert decision.capable_executors == ("codex",)

    payload, budget = _payload_for(task, PlanLimits(), ("AICC", "/srv/repo"), decision)
    assert [link["executor"] for link in payload["cascade"]] == ["codex"]
    # The attempt budget IS the cascade length: a narrowed cascade must not
    # keep a budget for links that were removed.
    assert budget == 1
    assert payload["task_type"] == "implementation"


def test_an_ordinary_task_keeps_the_whole_cascade() -> None:
    payload, budget = _payload_for(_task(), PlanLimits(), ("AICC", "/srv/repo"))
    assert [link["executor"] for link in payload["cascade"]] == [
        link["executor"] for link in cascade_for("implementation")
    ]
    assert budget == len(cascade_for("implementation"))
