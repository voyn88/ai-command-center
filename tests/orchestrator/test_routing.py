"""The routing matrix (BO-S2a): static, honest, hermetic."""

from __future__ import annotations

from command_center import agent_runner
from command_center.orchestrator import local_model_gates
from command_center.orchestrator.planner import PlanLimits, _payload_for
from command_center.orchestrator.routing import (
    BOUNDED_IMPLEMENTATION_TASK_CLASS,
    ROUTING_MATRIX,
    cascade_for,
    classify_task_class,
)

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


def test_review_uses_codex_then_claude_once_each():
    cascade = cascade_for("review")
    assert [link["executor"] for link in cascade] == ["codex", "claude"]
    assert all(link["task_type"] == "review" for link in cascade)


def test_every_link_is_an_executor_the_isolated_worker_will_launch():
    """ADR-0010: the fleet runs under principal isolation and the broker
    launches only `PRINCIPAL_EXECUTOR_BINARIES`. A link outside that set is a
    phantom link -- the worker refuses it at preflight and the attempt is
    burned (live 2026-09-08: copilot ate the last attempt of every task whose
    first two failed). This test is the gate that keeps such a link out."""
    for task_class, cascade in ROUTING_MATRIX.items():
        for link in cascade:
            assert link["executor"] in agent_runner.PRINCIPAL_EXECUTOR_BINARIES, (
                task_class, link, "not launchable under principal isolation"
            )


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


# --------------------------------------------------------------------------
# BOUNDED_IMPLEMENTATION_TASK_CLASS (AICC Fleet decision 2026-09-03): aider
# leads the cascade, but ONLY once local_model_gates says it is promoted.


def test_bounded_implementation_drops_the_unpromoted_aider_link():
    assert local_model_gates.is_promoted(BOUNDED_IMPLEMENTATION_TASK_CLASS) is False
    cascade = cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert "aider" not in [link["executor"] for link in cascade]
    assert cascade == cascade_for("implementation")


def test_bounded_implementation_leads_with_aider_once_promoted():
    for _ in range(local_model_gates.PROMOTION_SAMPLE_FLOOR):
        local_model_gates.record_benchmark_run(BOUNDED_IMPLEMENTATION_TASK_CLASS, True)
    assert local_model_gates.is_promoted(BOUNDED_IMPLEMENTATION_TASK_CLASS) is True

    cascade = cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert cascade[0]["executor"] == "aider"
    assert cascade[0]["task_type"] == "implementation"


def test_promoting_bounded_implementation_never_touches_plain_implementation():
    for _ in range(local_model_gates.PROMOTION_SAMPLE_FLOOR):
        local_model_gates.record_benchmark_run(BOUNDED_IMPLEMENTATION_TASK_CLASS, True)
    assert "aider" not in [link["executor"] for link in cascade_for("implementation")]


def test_classify_task_class_matches_only_an_exact_bracketed_title_prefix():
    for title in ("[docs] fix typos", "[fixture] bump golden value", "[mechanical] rename"):
        assert classify_task_class(title, "irrelevant body") == BOUNDED_IMPLEMENTATION_TASK_CLASS
    # Case-insensitive, and leading whitespace is trimmed.
    assert classify_task_class("  [DOCS] Fix Typos", "x") == BOUNDED_IMPLEMENTATION_TASK_CLASS


def test_classify_task_class_requires_the_prefix_to_end_at_the_bracket():
    """`[Mechanical Refactor]` must NOT match `[mechanical]`: the space right
    after "mechanical" breaks the exact-prefix match, so a title that merely
    mentions the word does not silently downgrade a task's executor lane."""
    assert classify_task_class("[Mechanical Refactor] do a big rewrite", "x") == "implementation"


def test_classify_task_class_never_scans_the_body():
    """Opting in is a title-only, author decision -- incidental body wording
    (an attacker's or a careless author's) must not silently downgrade a
    mutating task into the weaker executor lane."""
    assert classify_task_class("Refactor the auth module", "[docs] just kidding") == (
        "implementation"
    )


def test_classify_task_class_default_is_plain_implementation():
    assert classify_task_class("Implement the new widget", "body") == "implementation"


def test_bounded_implementation_dispatch_flows_from_title_to_cascade():
    task = {
        "task_id": "VOYN-W0-BOUNDED",
        "wave": "0",
        "priority": "P2",
        "title": "[docs] fix a typo in the README",
        "body": "Fix the typo.",
    }
    payload, budget = _payload_for(task, PlanLimits(), ("AICC", "/srv/repo"))
    assert payload["cascade"] == cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert budget == len(cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS))
