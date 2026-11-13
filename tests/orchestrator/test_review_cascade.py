"""Review dispatch routing and exact-target controls without a live database."""

from __future__ import annotations

import pytest

from command_center import agent_runner
from command_center.orchestrator import review_merge


def test_review_payload_carries_the_two_step_cascade(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "_rows",
        _rows_with_implementer(
            "VOYN-W0-X", "https://github.com/o/ai-command-center/pull/7"
        ),
    )
    monkeypatch.setattr(
        review_merge, "_pr_diff_and_head",
        lambda *args: review_merge._PRSnapshot.create(
            "diff", "b" * 40, "a" * 40
        )
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: ("AICC", "/srv/aicc"),
    )
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )

    assert report.reviewed == [
        ("VOYN-W0-X", "https://github.com/o/ai-command-center/pull/7")
    ]
    _queue, key, payload, task_id, max_attempts = calls[0]
    assert task_id == "VOYN-W0-X"
    assert f":{review_merge._REVIEW_POLICY_VERSION}:base:" in key
    assert ":diff:" in key
    assert [link["executor"] for link in payload["cascade"]] == ["codex", "claude"]
    assert payload["untrusted"] is True
    assert payload["task_type"] == "independent_review"
    assert payload["review_head"] == {
        "pr_number": "7",
        "head_sha": "a" * 40,
    }
    assert all(
        link["task_type"] == "independent_review"
        and link["capability"] == "model_only"
        for link in payload["cascade"]
    )
    for link in payload["cascade"]:
        argv = agent_runner._command_builder(link["executor"])(
            payload["prompt"], task_type=link["task_type"]
        )
        assert "--allow-all-tools" not in argv
        assert "--allow-tool" not in argv
        if link["executor"] == "copilot":
            assert "--available-tools=" in argv
        elif link["executor"] == "codex":
            assert argv[argv.index("--sandbox") + 1] == "read-only"
        else:
            assert argv[argv.index("--tools") + 1] == ""
    assert max_attempts == len(payload["cascade"]) == 2


def test_same_head_review_ticks_reuse_one_bounded_cycle(monkeypatch):
    """Transient retries stay inside one queue item: an unchanged head must
    not mint fresh work and thereby evade the item's retry bounds."""
    task = ("VOYN-W0-SAME-HEAD", "https://github.com/o/ai-command-center/pull/7")
    head = "a" * 40
    monkeypatch.setattr(review_merge, "_rows", _rows_with_implementer(*task))
    monkeypatch.setattr(
        review_merge,
        "_pr_diff_and_head",
        lambda *args: review_merge._PRSnapshot.create("diff", "b" * 40, head),
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: ("AICC", "/srv/aicc"),
    )
    enqueues = []

    for _ in range(3):
        review_merge.review_once(
            object(), lambda *args: enqueues.append(args), "/srv/aicc"
        )

    keys = [call[1] for call in enqueues]
    assert len(set(keys)) == 1
    assert f":7:{head}:{review_merge._REVIEW_POLICY_VERSION}:" in keys[0]
    assert [call[4] for call in enqueues] == [2, 2, 2]


def test_exact_task_target_is_parameterized_for_enqueue_and_marker(monkeypatch):
    captured = []
    cfg = review_merge.ReviewConfig(max_per_tick=3, scan_cap=40)

    def rows(_factory, sql, params=()):
        captured.append((sql, params))
        return []

    monkeypatch.setattr(review_merge, "_rows", rows)
    review_merge.review_once(
        object(), lambda *args: None, "/srv/aicc", cfg, task_id="VOYN-W0-EXACT"
    )
    review_merge.publish_review_verdicts(
        object(), "/srv/aicc", cfg, task_id="VOYN-W0-EXACT"
    )

    assert len(captured) == 2
    for sql, params in captured:
        assert "t.task_id = %s" in sql
        assert params[0] == "VOYN-W0-EXACT"
        assert params[-1] == cfg.max_per_tick


def test_empty_review_route_fails_closed_without_enqueuing(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "_rows",
        lambda *args: [
            ("VOYN-W0-X", "https://github.com/o/ai-command-center/pull/7")
        ],
    )
    monkeypatch.setattr(review_merge, "cascade_for", lambda _task_class: [])
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/tmp"
    )
    assert calls == []
    assert report.skipped == [("VOYN-W0-X", "no_review_executor_route")]


def _rows_with_implementer(task_id, pr_url, implementer=None):
    """A `_rows` fake that answers the review scan's task/PR query with a
    single row, and the independence check's implementer-history query
    (`_implementation_executor`, keyed off its distinctive join) with the
    given implementer's recorded executor -- or no row at all, meaning no
    implementation history was found. Cursor and priority queries have no rows."""

    def rows(_factory, sql, _params=()):
        if "work_result wr JOIN work_item" in sql:
            return [({"executor": implementer},)] if implementer else []
        if "FROM backlog_task" in sql:
            return [(task_id, pr_url)]
        return []

    return rows


def test_claude_implemented_excludes_claude_so_codex_reviews(monkeypatch):
    """VOYN-W0-AICC-REVIEW-EXECUTOR-INDEPENDENCE (a): the executor that
    actually implemented the task -- read from attempt history, not the
    static plan -- must not reappear in its own review cascade."""
    task_id = "VOYN-W0-IMPL-CLAUDE"
    pr_url = "https://github.com/o/ai-command-center/pull/21"
    monkeypatch.setattr(review_merge, "_rows", _rows_with_implementer(task_id, pr_url, "claude"))
    monkeypatch.setattr(
        review_merge, "_pr_diff_and_head",
        lambda *args: review_merge._PRSnapshot.create("diff", "b" * 40, "a" * 40),
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: ("AICC", "/srv/aicc"),
    )
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )
    assert report.reviewed == [(task_id, pr_url)]
    _queue, _key, payload, enqueued_task_id, max_attempts = calls[0]
    assert enqueued_task_id == task_id
    assert [link["executor"] for link in payload["cascade"]] == ["codex"]
    assert max_attempts == 1


def test_codex_implemented_excludes_codex_so_claude_reviews_when_mac_reachable(monkeypatch):
    """VOYN-W0-AICC-REVIEW-EXECUTOR-INDEPENDENCE (b): when the escalation
    link (codex) is the one that actually landed the implementation --
    not merely the plan's first entry -- the review cascade must exclude
    codex and fall to claude, provided claude is actually reachable (the
    opt-in Mac bridge, `AICC_CLAUDE_MAC_ENABLED=1`)."""
    monkeypatch.setenv("AICC_CLAUDE_MAC_ENABLED", "1")
    task_id = "VOYN-W0-IMPL-CODEX"
    pr_url = "https://github.com/o/ai-command-center/pull/22"
    monkeypatch.setattr(review_merge, "_rows", _rows_with_implementer(task_id, pr_url, "codex"))
    monkeypatch.setattr(
        review_merge, "_pr_diff_and_head",
        lambda *args: review_merge._PRSnapshot.create("diff", "b" * 40, "a" * 40),
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: ("AICC", "/srv/aicc"),
    )
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )
    assert report.reviewed == [(task_id, pr_url)]
    _queue, _key, payload, enqueued_task_id, max_attempts = calls[0]
    assert enqueued_task_id == task_id
    assert [link["executor"] for link in payload["cascade"]] == ["claude"]
    assert max_attempts == 1


def test_codex_implemented_and_claude_mac_off_waits_instead_of_self_review(monkeypatch):
    """VOYN-W0-AICC-REVIEW-EXECUTOR-INDEPENDENCE (c): with codex the
    implementer and claude's only model-only transport (the opt-in Mac
    bridge) unreachable, there is no independent reviewer left. The task
    must wait with a reason rather than ever falling back to codex
    reviewing its own change."""
    monkeypatch.delenv("AICC_CLAUDE_MAC_ENABLED", raising=False)
    task_id = "VOYN-W0-IMPL-CODEX-NO-MAC"
    pr_url = "https://github.com/o/ai-command-center/pull/23"
    monkeypatch.setattr(review_merge, "_rows", _rows_with_implementer(task_id, pr_url, "codex"))
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )
    assert calls == []
    assert report.skipped == [
        (task_id, "self_review_excluded:claude_mac_unreachable")
    ]


def test_unknown_implementer_leaves_the_review_cascade_unfiltered(monkeypatch):
    """No implementation history (e.g. a legacy or pre-independence task)
    must not be treated as a conflict -- the cascade stays exactly the
    static two-step route it is today."""
    task_id = "VOYN-W0-IMPL-UNKNOWN"
    pr_url = "https://github.com/o/ai-command-center/pull/24"
    monkeypatch.setattr(review_merge, "_rows", _rows_with_implementer(task_id, pr_url, None))
    monkeypatch.setattr(
        review_merge, "_pr_diff_and_head",
        lambda *args: review_merge._PRSnapshot.create("diff", "b" * 40, "a" * 40),
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: ("AICC", "/srv/aicc"),
    )
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )
    assert report.reviewed == [(task_id, pr_url)]
    _queue, _key, payload, _task_id, max_attempts = calls[0]
    assert [link["executor"] for link in payload["cascade"]] == ["codex", "claude"]
    assert max_attempts == 2


def test_owner_alternatives_review_filters_keep_grok_4_6_exclude_cursor(monkeypatch):
    """owner-alternatives is cursor+grok; model_only/verification filters must
    keep the supported Grok 4.6 reviewer and drop Cursor before enqueue, so the
    route is not emptied into no_review_executor_route ahead of worker rewrite.
    Independent identity/acceptance gates are unchanged by this allowlist.
    """
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-alternatives")
    model_only = review_merge._model_only_review_cascade()
    verification = review_merge._verification_review_cascade()

    assert [link["executor"] for link in model_only] == ["grok"]
    assert model_only[0]["model"] == "grok-4.6"
    assert model_only[0]["task_type"] == "independent_review"
    assert model_only[0]["capability"] == "model_only"

    assert [link["executor"] for link in verification] == ["grok"]
    assert verification[0]["model"] == "grok-4.6"
    assert verification[0]["task_type"] == "verification_review"
    assert verification[0]["capability"] == "read_only"

    assert "cursor" not in review_merge._MODEL_ONLY_REVIEW_EXECUTORS
    assert "grok" in review_merge._MODEL_ONLY_REVIEW_EXECUTORS

    # Enqueue path must carry Grok 4.6 through the model_only filter (not empty).
    monkeypatch.setattr(
        review_merge,
        "_rows",
        _rows_with_implementer(
            "VOYN-W0-X", "https://github.com/o/ai-command-center/pull/7"
        ),
    )
    monkeypatch.setattr(
        review_merge,
        "_pr_diff_and_head",
        lambda *args: review_merge._PRSnapshot.create(
            "diff", "b" * 40, "a" * 40
        ),
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: ("AICC", "/srv/aicc"),
    )
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )
    assert report.skipped == []
    assert len(calls) == 1
    _queue, _key, payload, task_id, max_attempts = calls[0]
    assert task_id == "VOYN-W0-X"
    assert [link["executor"] for link in payload["cascade"]] == ["grok"]
    assert payload["cascade"][0]["model"] == "grok-4.6"
    assert payload["cascade"][0]["capability"] == "model_only"
    assert payload["task_type"] == "independent_review"
    assert max_attempts == 1
    # Cursor Ask remains excluded from model-only argv construction.
    monkeypatch.setenv(agent_runner.PRINCIPAL_ISOLATION_REQUIRED_ENV, "required")
    with pytest.raises(agent_runner.RunnerError, match="model-only"):
        agent_runner.build_cursor_command(
            payload["prompt"], task_type="independent_review"
        )


def test_review_tick_stops_when_global_review_wip_is_full(monkeypatch):
    queries = []

    def rows(_factory, sql, params=()):
        queries.append((sql, params))
        if "count(DISTINCT task_id)" in sql:
            return [(8,)]
        raise AssertionError("a full review WIP must stop before scanning tasks")

    monkeypatch.setattr(review_merge, "_rows", rows)
    calls = []
    report = review_merge.review_once(
        lambda: None,
        lambda *args: calls.append(args),
        "/srv/aicc",
        review_merge.ReviewConfig(max_active_reviews=8),
    )

    assert report.reviewed == []
    assert calls == []
    assert len(queries) == 1


def test_review_tick_only_fills_available_global_wip(monkeypatch):
    tasks = [
        ("VOYN-W0-X", "https://github.com/o/ai-command-center/pull/7"),
        ("VOYN-W0-Y", "https://github.com/o/ai-command-center/pull/8"),
    ]

    def rows(_factory, sql, params=()):
        if "count(DISTINCT task_id)" in sql:
            return [(7,)]
        return tasks if "FROM backlog_task" in sql else []

    monkeypatch.setattr(review_merge, "_rows", rows)
    monkeypatch.setattr(
        review_merge, "_scan_tasks", lambda *args, **kwargs: (tasks, None)
    )
    monkeypatch.setattr(
        review_merge,
        "_pr_diff_and_head",
        lambda *args: review_merge._PRSnapshot.create(
            "diff --git a/x b/x\n+ok\n", "b" * 40, "a" * 40
        ),
    )
    monkeypatch.setattr(
        "command_center.orchestrator.planner.repo_route",
        lambda repo: ("AICC", "/srv/aicc"),
    )
    calls = []
    report = review_merge.review_once(
        lambda: None,
        lambda *args: calls.append(args),
        "/srv/aicc",
        review_merge.ReviewConfig(max_per_tick=8, max_active_reviews=8),
    )

    assert report.reviewed == [tasks[0]]
    assert len(calls) == 1


def test_review_once_skips_closed_pr_as_pr_not_open_without_diff_fetch(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "_rows",
        _rows_with_implementer(
            "VOYN-W0-CLOSED", "https://github.com/o/ai-command-center/pull/7"
        ),
    )
    monkeypatch.setattr(
        review_merge,
        "_review_window_read_decision",
        lambda *_args, **_kwargs: review_merge._PrWindowReadDecision(
            True, "pr_not_open", {"state": "closed"}
        ),
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("diff fetch must not run for closed PRs")

    monkeypatch.setattr(review_merge, "_pr_diff_and_head_with_pull", fail_if_called)
    calls = []

    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )

    assert calls == []
    assert report.skipped == [("VOYN-W0-CLOSED", "pr_not_open")]


def test_publish_review_verdicts_skips_closed_pr_as_pr_not_open_without_diff_fetch(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "_rows",
        lambda *args: [("VOYN-W0-CLOSED", "https://github.com/o/ai-command-center/pull/7")],
    )
    monkeypatch.setattr(
        review_merge,
        "_review_window_read_decision",
        lambda *_args, **_kwargs: review_merge._PrWindowReadDecision(
            True, "pr_not_open", {"state": "closed"}
        ),
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("diff fetch must not run for closed PRs")

    monkeypatch.setattr(review_merge, "_pr_diff_and_head_with_pull", fail_if_called)

    report = review_merge.publish_review_verdicts(object(), "/srv/aicc")

    assert report.skipped == [("VOYN-W0-CLOSED", "pr_not_open")]
