"""Review dispatch routing and exact-target controls without a live database."""

from __future__ import annotations

import hashlib

from command_center import agent_runner
from command_center.orchestrator import review_merge


def test_review_payload_carries_the_two_step_cascade(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "_rows",
        lambda *args: [("VOYN-W0-X", "https://github.com/o/ai-command-center/pull/7")],
    )
    monkeypatch.setattr(
        review_merge,
        "_pr_diff_and_head",
        lambda *args: review_merge._PullRequestDiff.create("diff", "b" * 40, "a" * 40),
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
    assert key == (
        f"review:VOYN-W0-X:7:{'a' * 40}:v5:base:{'b' * 40}:diff:"
        f"{hashlib.sha256(b'diff').hexdigest()}"
    )
    assert [link["executor"] for link in payload["cascade"]] == [
        "copilot",
        "claude",
    ]
    assert payload["untrusted"] is True
    assert payload["task_type"] == "independent_review"
    assert all(
        link["task_type"] == "independent_review" and link["capability"] == "model_only"
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
        else:
            assert argv[argv.index("--tools") + 1] == ""
    assert max_attempts == len(payload["cascade"]) == 2


def test_exact_task_target_is_parameterized_for_enqueue_and_marker(monkeypatch):
    captured = []

    def rows(_factory, sql, params=()):
        captured.append((sql, params))
        return []

    monkeypatch.setattr(review_merge, "_rows", rows)
    review_merge.review_once(
        object(), lambda *args: None, "/srv/aicc", task_id="VOYN-W0-EXACT"
    )
    review_merge.publish_review_verdicts(object(), "/srv/aicc", task_id="VOYN-W0-EXACT")

    assert len(captured) == 2
    for sql, params in captured:
        assert "t.task_id = %s" in sql
        assert params[0] == "VOYN-W0-EXACT"
        assert params[-1] == review_merge.ReviewConfig().max_per_tick


def test_empty_review_route_fails_closed_without_enqueuing(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "_rows",
        lambda *args: [("VOYN-W0-X", "https://github.com/o/ai-command-center/pull/7")],
    )
    monkeypatch.setattr(review_merge, "cascade_for", lambda _task_class: [])
    calls = []
    report = review_merge.review_once(
        object(), lambda *args: calls.append(args), "/tmp"
    )
    assert calls == []
    assert report.skipped == [("VOYN-W0-X", "no_review_executor_route")]
