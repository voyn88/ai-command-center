"""The routing matrix (BO-S2a): static, honest, hermetic."""

from __future__ import annotations

from command_center import agent_runner, local_model_gates
from command_center.orchestrator.planner import PlanLimits, _payload_for, repo_route
from command_center.orchestrator.routing import (
    BOUNDED_IMPLEMENTATION_TASK_CLASS,
    BILLING_MODE_API_METERED,
    BILLING_MODE_SUBSCRIPTION,
    BILLING_MODE_UNKNOWN,
    ROUTING_MATRIX,
    billing_mode_for_executor,
    cascade_for,
    classify_task_class,
    metered_executors,
)


def test_bounded_implementation_is_fail_closed_until_promoted():
    assert not local_model_gates.is_promoted(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS) == cascade_for(
        "implementation"
    )


def test_promoted_bounded_implementation_leads_with_isolated_aider():
    for _ in range(local_model_gates.PROMOTION_SAMPLE_FLOOR):
        local_model_gates.record_benchmark_run(
            BOUNDED_IMPLEMENTATION_TASK_CLASS, True
        )
    cascade = cascade_for(BOUNDED_IMPLEMENTATION_TASK_CLASS)
    assert cascade[0] == {"executor": "aider", "task_type": "implementation"}
    assert "aider" in agent_runner.PRINCIPAL_EXECUTOR_BINARIES


def test_bounded_classification_is_explicit_title_only_opt_in():
    assert (
        classify_task_class("[docs] fix typo", "x")
        == BOUNDED_IMPLEMENTATION_TASK_CLASS
    )
    assert classify_task_class("Refactor auth", "[docs] misleading") == "implementation"


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


def test_owner_profile_avoids_exhausted_subscription_and_preserves_class(monkeypatch):
    monkeypatch.setenv('AICC_EXECUTOR_PROFILE', 'owner-alternatives')
    for task_class, task_type in [('implementation', 'implementation'), ('review', 'review')]:
        links = cascade_for(task_class)
        assert [link['executor'] for link in links] == ['cursor', 'grok']
        assert [link.get('model') for link in links] == ['auto', 'grok-4.6']
        assert all(link['task_type'] == task_type for link in links)
        assert all(link['executor'] in agent_runner.PRINCIPAL_EXECUTOR_BINARIES for link in links)


def test_owner_alternatives_selects_grok_4_6_and_keeps_cursor_auto(monkeypatch):
    """Permanent owner-alternatives roster pin: Grok 4.6, Cursor auto unchanged."""
    monkeypatch.setenv('AICC_EXECUTOR_PROFILE', 'owner-alternatives')
    impl = cascade_for('implementation')
    review = cascade_for('review')
    assert impl[0] == {"executor": "cursor", "task_type": "implementation", "model": "auto"}
    assert impl[1] == {"executor": "grok", "task_type": "implementation", "model": "grok-4.6"}
    assert review[0]["model"] == "auto" and review[0]["executor"] == "cursor"
    assert review[1]["model"] == "grok-4.6" and review[1]["executor"] == "grok"
    # Default matrix must stay on the subscription cascade (not globally replaced).
    monkeypatch.delenv('AICC_EXECUTOR_PROFILE', raising=False)
    assert [link["executor"] for link in cascade_for("implementation")] == [
        "claude",
        "codex",
    ]


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


def test_owner_billing_mode_table_is_pinned() -> None:
    assert billing_mode_for_executor("grok") == BILLING_MODE_API_METERED
    assert billing_mode_for_executor("cursor") == BILLING_MODE_API_METERED
    assert billing_mode_for_executor("openai_http") == BILLING_MODE_API_METERED
    assert billing_mode_for_executor("codex") == BILLING_MODE_SUBSCRIPTION
    assert billing_mode_for_executor("claude") == BILLING_MODE_SUBSCRIPTION
    assert billing_mode_for_executor("copilot") == BILLING_MODE_UNKNOWN
    assert list(metered_executors()) == ["cursor", "grok", "openai_http"]


def test_default_repo_routes_include_crm_lane(monkeypatch) -> None:
    """CRM backlog tasks must be dispatchable without a per-host override.

    This is deliberately hermetic rather than in the database planner suite:
    the route table is a static planner contract and must be checked even
    where the PostgreSQL integration fixtures are unavailable.
    """
    monkeypatch.delenv("AICC_PLANNER_REPO_ROUTES", raising=False)
    assert repo_route("voyn-logistics-crm") == (
        "CRM",
        "/home/voynadmin/Projects/voyn-logistics-crm",
    )


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


def test_owner_cascade_orders_all_isolated_providers_by_task_class(monkeypatch):
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-cascade")
    assert [x["executor"] for x in cascade_for("implementation")] == [
        "claude", "codex", "cursor", "grok",
    ]
    assert [x["executor"] for x in cascade_for("review")] == [
        "codex", "claude", "cursor", "grok",
    ]
    assert all(x["executor"] in agent_runner.PRINCIPAL_EXECUTOR_BINARIES
               for x in cascade_for("review"))
    changed = cascade_for("review")
    changed[0]["model"] = "wrong-provider-model"
    assert cascade_for("review")[0]["model"] is None


def test_owner_cascade_model_only_route_matches_broker_capability(monkeypatch):
    from command_center.orchestrator import review_merge
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-cascade")
    route = review_merge._model_only_review_cascade()
    assert [x["executor"] for x in route] == ["grok"]
    assert route[0]["task_type"] == "independent_review"
    assert route[0]["capability"] == "model_only"


def test_owner_cascade_cursor_model_override(monkeypatch):
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-cascade")
    monkeypatch.setenv("AICC_CURSOR_MODEL", "composer-2.5")
    link = next(x for x in cascade_for("review") if x["executor"] == "cursor")
    assert link["model"] == "composer-2.5"


def test_mac_subscription_is_opt_in_model_only_not_a_writer(monkeypatch):
    from command_center.orchestrator import review_merge
    monkeypatch.setenv('AICC_EXECUTOR_PROFILE','owner-cascade')
    monkeypatch.setenv('AICC_CLAUDE_MAC_ENABLED','1')
    assert [x['executor'] for x in review_merge._model_only_review_cascade()]==['claude','grok']
    assert all(x['capability']=='model_only' for x in review_merge._model_only_review_cascade())
    assert cascade_for('implementation')[0]['executor']=='claude'
    # Implementation still uses the existing server broker development profile,
    # never the Mac's zero-tool inference transport.
    manifest=agent_runner.build_principal_isolation_manifest(repository_path=__import__('pathlib').Path.cwd(),prompt='text',task_type='implementation',timeout_seconds=60,executor='claude',model=None)
    assert manifest['profile']=='trusted_development'
