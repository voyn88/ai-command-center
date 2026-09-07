"""The routing matrix (BO-S2a): static, honest, hermetic."""

from __future__ import annotations

import pytest

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


# -- bounded_implementation: the benchmark-gated free lane ------------------
#
# VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR. Fix for the defect independent review
# found in PR #700 (b280dfc2, chunk 1/6): the `aider` link was named in
# `ROUTING_MATRIX` unconditionally, and `cascade_for` never actually
# filtered it -- so every bounded_implementation dispatch reached `aider`
# regardless of promotion state, undermining the entire benchmark-gate
# premise. These tests prove the filtering is real.


def test_bounded_implementation_drops_the_unpromoted_aider_link(monkeypatch):
    monkeypatch.setattr(local_model_gates, "is_promoted", lambda task_class: False)
    cascade = cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert "aider" not in [link["executor"] for link in cascade]
    # Never worse off than the standard route: the same paid links, same
    # order, once the free link is dropped.
    assert [link["executor"] for link in cascade] == [
        link["executor"] for link in ROUTING_MATRIX["implementation"]
    ]


def test_bounded_implementation_leads_with_aider_once_promoted(monkeypatch):
    monkeypatch.setattr(local_model_gates, "is_promoted", lambda task_class: True)
    cascade = cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert cascade[0]["executor"] == "aider"
    assert cascade[0]["task_type"] == "implementation"


def test_bounded_implementation_gate_reads_the_real_ledger_by_default():
    """Without any patch, an unbenchmarked class in a fresh ledger must not
    dispatch to aider — the gate must fail closed, not merely be
    filterable-in-principle."""
    cascade = cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert "aider" not in [link["executor"] for link in cascade]


def test_bounded_implementation_gate_never_touches_plain_implementation(monkeypatch):
    """The gate is scoped to bounded_implementation only — promoting (or not)
    that class must never add or remove links from the ordinary
    "implementation" cascade."""
    before = cascade_for("implementation")
    monkeypatch.setattr(local_model_gates, "is_promoted", lambda task_class: True)
    after = cascade_for("implementation")
    assert before == after
    assert "aider" not in [link["executor"] for link in after]


def test_bounded_implementation_checks_promotion_for_its_own_class_name(monkeypatch):
    """`cascade_for` must ask the ledger about
    `BOUNDED_IMPLEMENTATION_TASK_CLASS` specifically, not some other key —
    a wrong lookup key would silently read an always-unpromoted (or
    always-promoted) default instead of this class's real state."""
    seen = []

    def fake_is_promoted(task_class):
        seen.append(task_class)
        return False

    monkeypatch.setattr(local_model_gates, "is_promoted", fake_is_promoted)
    cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert seen == [BOUNDED_IMPLEMENTATION_TASK_CLASS]


def test_bounded_implementation_aider_link_pins_the_documented_model():
    cascade = ROUTING_MATRIX[BOUNDED_IMPLEMENTATION_TASK_CLASS]
    aider_link = next(link for link in cascade if link["executor"] == "aider")
    assert aider_link["model"] == f"ollama_chat/{agent_runner.DEFAULT_AIDER_MODEL}"


# -- classify_task_class -----------------------------------------------------


def test_classify_task_class_routes_a_labelled_title_to_bounded_implementation():
    assert (
        classify_task_class("[docs] fix a typo in README", "body")
        == BOUNDED_IMPLEMENTATION_TASK_CLASS
    )
    assert (
        classify_task_class("[fixture] update golden.json", "body")
        == BOUNDED_IMPLEMENTATION_TASK_CLASS
    )
    assert (
        classify_task_class("[mechanical] rename OLD_LIMIT", "body")
        == BOUNDED_IMPLEMENTATION_TASK_CLASS
    )


def test_classify_task_class_defaults_to_implementation():
    assert classify_task_class("implement the new widget", "body") == "implementation"


def test_classify_task_class_does_not_infer_from_body_text():
    """The label must be an explicit title opt-in — a mutating task whose
    BODY merely mentions "docs" or "typo" must not be silently downgraded
    into the cheaper executor lane."""
    assert (
        classify_task_class(
            "implement the new widget", "this fixes several docs typos too"
        )
        == "implementation"
    )


@pytest.mark.parametrize(
    "title,body",
    [
        (None, None),
        ("", ""),
        ("   ", None),
        ("DOCS: something", "body"),  # no brackets — not the exact label
        ("docs fix typo", "body"),  # missing the required brackets
    ],
)
def test_classify_task_class_only_returns_routed_classes(title, body):
    result = classify_task_class(title, body)
    assert result in ROUTING_MATRIX
    assert result == "implementation"


def test_classify_task_class_label_match_is_case_insensitive():
    assert (
        classify_task_class("[DOCS] Fix a typo", "body")
        == BOUNDED_IMPLEMENTATION_TASK_CLASS
    )
