"""The Ollama PRESCREEN tier (VOYN-W0-AICC-OLLAMA-REVIEW-EXECUTOR).

BENCHMARK 2026-09-03: qwen2.5-coder:14b and deepseek-r1:8b both scored 0%
recall against a three-PR known-truth holdout (#578 P1, #586 P1, #594 P2),
twice failing the 100%-recall bar a verdict-authority promotion would need.
This tier is therefore advisory-only by design: it drafts findings and a
priority signal for the real reviewer, under its own task_type
(``review_prescreen``), its own cascade (``routing.ROUTING_MATRIX
["review_prescreen"]``), its own key namespace (``prescreen:``, never
``review:``) and its own output trailer (``PRESCREEN_PRIORITY:``, never
``VERDICT:``). Every test below is really checking one thing from a
different angle: that an Ollama result can never reach `_parse_verdict`,
`_post_marker_as_bot`, or `_remediate_rejection`.
"""

from __future__ import annotations

import pytest

from command_center.orchestrator import planner, review_merge

TASK = "VOYN-W0-PRESCREEN"
PR = "https://github.com/voyn88/ai-command-center/pull/380"
BASE, HEAD = "c" * 40, "e" * 40


def snap(text):
    return review_merge._PRSnapshot.create(text, BASE, HEAD)


# -- cascade shape -------------------------------------------------------


def test_prescreen_cascade_is_ollama_only_and_typed_model_only():
    cascade = review_merge._prescreen_cascade()
    assert [link["executor"] for link in cascade] == ["ollama"]
    assert all(
        link["task_type"] == "review_prescreen" and link["capability"] == "model_only"
        for link in cascade
    )


def test_prescreen_cascade_filters_out_non_ollama_links(monkeypatch):
    monkeypatch.setattr(
        review_merge,
        "cascade_for",
        lambda _task_class: [{"executor": "claude"}, {"executor": "ollama"}],
    )
    cascade = review_merge._prescreen_cascade()
    assert [link["executor"] for link in cascade] == ["ollama"]


# -- key namespace never collides with the real review's ----------------


def test_prescreen_key_namespace_is_disjoint_from_review_key():
    snapshot = snap("diff --git a/a b/a\n-old\n+new\n")
    review_key = review_merge._review_key(TASK, PR, snapshot)
    prescreen_key = review_merge._prescreen_key(TASK, PR, snapshot)
    assert prescreen_key.startswith("prescreen:")
    assert not review_key.startswith("prescreen:")
    assert not prescreen_key.startswith("review:")
    assert prescreen_key != review_key

    review_prefix = review_merge._chunk_key_prefix(TASK, PR, snapshot)
    prescreen_prefix = review_merge._prescreen_key_prefix(TASK, PR, snapshot)
    assert prescreen_prefix != review_prefix
    assert not prescreen_prefix.startswith("review:")


def test_prescreen_key_is_none_for_malformed_pr_url():
    snapshot = snap("diff")
    assert review_merge._prescreen_key(TASK, "not-a-pr-url", snapshot) is None


# -- prompt: distinct trailer contract -----------------------------------


def test_prescreen_prompt_uses_a_distinct_trailer_never_verdict():
    snapshot = snap("diff --git a/a b/a\n-old\n+new\n")
    chunk = review_merge._review_chunks(snapshot, TASK, PR)[0]
    prompt = review_merge._render_prescreen_prompt(TASK, PR, snapshot, chunk)
    assert "PRESCREEN_PRIORITY: LOW or PRESCREEN_PRIORITY: MEDIUM" in prompt
    assert "VERDICT: ACCEPT or VERDICT: REJECT" not in prompt
    assert "No tools are available or needed" in prompt
    assert review_merge._REVIEW_INPUT_MARKER in prompt


def test_prescreen_and_review_prompts_share_the_envelope_bytes():
    """Same chunking, same envelope content -- only the wrapper prose and
    trailer differ, so the prescreen sees byte-identical diff content to the
    real review."""
    snapshot = snap("diff --git a/a b/a\n" + "x\n" * 5_000)
    chunk = review_merge._review_chunks(snapshot, TASK, PR)[0]
    review_prompt = review_merge._render_review_prompt(TASK, PR, snapshot, chunk)
    prescreen_prompt = review_merge._render_prescreen_prompt(TASK, PR, snapshot, chunk)
    review_envelope = review_prompt.split(review_merge._REVIEW_INPUT_MARKER)[1]
    prescreen_envelope = prescreen_prompt.split(review_merge._REVIEW_INPUT_MARKER)[1]
    assert review_envelope == prescreen_envelope


# -- parser: mutually blind to each other's trailer ----------------------


def test_parse_prescreen_never_parses_a_real_verdict_and_vice_versa():
    verdict_text = f"VERDICT: ACCEPT\nHEAD_SHA: {HEAD}"
    prescreen_text = f"PRESCREEN_PRIORITY: HIGH\nHEAD_SHA: {HEAD}"
    assert review_merge._parse_verdict(verdict_text) == ("ACCEPT", HEAD)
    assert review_merge._parse_prescreen(verdict_text) is None
    assert review_merge._parse_prescreen(prescreen_text) == ("HIGH", HEAD)
    assert review_merge._parse_verdict(prescreen_text) is None


@pytest.mark.parametrize("priority", ["LOW", "MEDIUM", "HIGH"])
def test_parse_prescreen_accepts_every_priority_level(priority):
    text = f"some findings\nPRESCREEN_PRIORITY: {priority}\nHEAD_SHA: {HEAD}"
    assert review_merge._parse_prescreen(text) == (priority, HEAD)


def test_parse_prescreen_rejects_malformed_priority():
    assert review_merge._parse_prescreen(f"PRESCREEN_PRIORITY: URGENT\nHEAD_SHA: {HEAD}") is None
    assert review_merge._parse_prescreen("PRESCREEN_PRIORITY: HIGH") is None


# -- prescreen_once: enqueue behavior -------------------------------------


def test_prescreen_once_enqueues_under_its_own_task_type_and_cascade(monkeypatch):
    monkeypatch.setattr(
        review_merge, "_rows", lambda *args: [(TASK, PR)]
    )
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", lambda *args: snap("diff"))
    monkeypatch.setattr(planner, "repo_route", lambda repo: ("AICC", "/srv/aicc"))
    calls = []
    report = review_merge.prescreen_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )
    assert report.reviewed == [(TASK, PR)]
    _queue, key, payload, task_id, max_attempts = calls[0]
    assert task_id == TASK
    assert key.startswith("prescreen:")
    assert payload["task_type"] == "review_prescreen"
    assert [link["executor"] for link in payload["cascade"]] == ["ollama"]
    assert payload["untrusted"] is True
    assert max_attempts == 1


def test_prescreen_once_is_a_silent_noop_without_an_ollama_route(monkeypatch):
    monkeypatch.setattr(review_merge, "_prescreen_cascade", lambda: [])

    def boom(*_args, **_kwargs):
        raise AssertionError("must not scan/query without an ollama route")

    monkeypatch.setattr(review_merge, "_rows", boom)
    monkeypatch.setattr(review_merge, "_scan_tasks", boom)
    calls = []
    report = review_merge.prescreen_once(
        object(), lambda *args: calls.append(args), "/srv/aicc"
    )
    assert report.reviewed == [] and report.skipped == []
    assert calls == []


def test_prescreen_once_respects_exact_task_target(monkeypatch):
    captured = []

    def rows(_factory, sql, params=()):
        captured.append((sql, params))
        return []

    monkeypatch.setattr(review_merge, "_rows", rows)
    cfg = review_merge.ReviewConfig(max_per_tick=3)
    review_merge.prescreen_once(
        object(), lambda *args: None, "/srv/aicc", cfg, task_id="VOYN-W0-EXACT"
    )
    assert len(captured) == 1
    sql, params = captured[0]
    assert "t.task_id = %s" in sql
    assert params == ("VOYN-W0-EXACT", cfg.max_per_tick)


def test_prescreen_once_never_touches_the_review_scan_cursor(monkeypatch):
    """The two ticks must not share a cursor name -- interfering with
    `review_once`'s fairness window would be a regression in the real
    (verdict-bearing) pipeline caused by an advisory feature."""
    seen_cursor_names = []
    real_scan_tasks = review_merge._scan_tasks

    def spy(factory, cursor_name, *args, **kwargs):
        seen_cursor_names.append(cursor_name)
        return [], None

    monkeypatch.setattr(review_merge, "_scan_tasks", spy)
    review_merge.prescreen_once(object(), lambda *args: None, "/srv/aicc")
    assert seen_cursor_names == ["scan:prescreen_once"]
    assert real_scan_tasks is not spy  # sanity: we didn't monkeypatch review_once's


# -- publish_prescreen_findings: advisory comment only --------------------


def test_publish_prescreen_findings_posts_single_chunk_result(monkeypatch):
    key = review_merge._prescreen_key(TASK, PR, snap("diff --git a/a b/a\n-old\n+new\n"))
    result_text = (
        "the change drops an error path\nPRESCREEN_PRIORITY: MEDIUM\nHEAD_SHA: " + HEAD
    )

    def fake_rows(_factory, sql, _params=()):
        if "SELECT t.task_id" in sql:
            return [(TASK, PR)]
        if sql.startswith("SELECT wr.payload"):
            return [({"result_text": result_text},)]
        return []

    monkeypatch.setattr(review_merge, "_rows", fake_rows)
    monkeypatch.setattr(
        review_merge, "_pr_diff_and_head",
        lambda *_: snap("diff --git a/a b/a\n-old\n+new\n"),
    )
    posted = []

    def fake_gh(argv, _repo):
        import json
        import subprocess

        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"comments": []}), "")
        posted.append(argv[argv.index("--body") + 1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must never post a marker")),
    )
    monkeypatch.setattr(
        review_merge, "_remediate_rejection",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must never remediate")),
    )

    report = review_merge.publish_prescreen_findings(None, "/repo")

    assert report.reviewed == [(TASK, PR)]
    assert len(posted) == 1
    assert "advisory only" in posted[0]
    assert "Signal priority: **MEDIUM**" in posted[0]
    assert "ACCEPTANCE:" not in posted[0]
    assert key is not None


def test_publish_prescreen_findings_is_idempotent_per_head_and_findings(monkeypatch):
    result_text = "findings\nPRESCREEN_PRIORITY: LOW\nHEAD_SHA: " + HEAD
    snapshot = snap("diff --git a/a b/a\n-old\n+new\n")

    def fake_rows(_factory, sql, _params=()):
        if "SELECT t.task_id" in sql:
            return [(TASK, PR)]
        if sql.startswith("SELECT wr.payload"):
            return [({"result_text": result_text},)]
        return []

    monkeypatch.setattr(review_merge, "_rows", fake_rows)
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", lambda *_: snapshot)

    findings_hash = __import__("hashlib").sha256(result_text.encode()).hexdigest()[:16]
    tag = f"OLLAMA-PRESCREEN {HEAD} findings:{findings_hash}"

    def fake_gh(argv, _repo):
        import json
        import subprocess

        if argv[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"comments": [{"body": tag}]}), ""
            )
        raise AssertionError("must not post again once the tag is already present")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    report = review_merge.publish_prescreen_findings(None, "/repo")
    assert report.reviewed == [(TASK, PR)]


def test_publish_prescreen_findings_waits_on_stale_head(monkeypatch):
    stale_text = "findings\nPRESCREEN_PRIORITY: LOW\nHEAD_SHA: " + "a" * 40
    snapshot = snap("diff --git a/a b/a\n-old\n+new\n")

    def fake_rows(_factory, sql, _params=()):
        if "SELECT t.task_id" in sql:
            return [(TASK, PR)]
        if sql.startswith("SELECT wr.payload"):
            return [({"result_text": stale_text},)]
        return []

    monkeypatch.setattr(review_merge, "_rows", fake_rows)
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", lambda *_: snapshot)
    report = review_merge.publish_prescreen_findings(None, "/repo")
    assert report.reviewed == []
    assert report.skipped == [(TASK, "prescreen_output_unparseable_or_stale")]


def test_publish_prescreen_findings_skips_without_a_result_yet(monkeypatch):
    snapshot = snap("diff --git a/a b/a\n-old\n+new\n")

    def fake_rows(_factory, sql, _params=()):
        if "SELECT t.task_id" in sql:
            return [(TASK, PR)]
        return []

    monkeypatch.setattr(review_merge, "_rows", fake_rows)
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", lambda *_: snapshot)
    report = review_merge.publish_prescreen_findings(None, "/repo")
    assert report.reviewed == []
    assert report.skipped == [(TASK, "no_prescreen_result_yet")]


# -- aggregation: multi-chunk priority + completeness ---------------------


def _chunk_row(index, count, snapshot, *, priority="LOW", state="succeeded", head=None):
    payload = {
        "review_chunk": {
            "index": index, "count": count,
            "base_sha": snapshot.base, "head_sha": snapshot.head,
            "diff_hash": snapshot.digest,
        }
    }
    result = {"result_text": f"chunk {index}\nPRESCREEN_PRIORITY: {priority}\nHEAD_SHA: {head or snapshot.head}"}
    return (f"key{index}", state, payload, result)


def test_aggregate_prescreen_picks_the_highest_priority_across_chunks():
    snapshot = snap("diff --git a/a b/a\n" + "x\n" * 20_000)
    rows = [
        _chunk_row(0, 2, snapshot, priority="LOW"),
        _chunk_row(1, 2, snapshot, priority="HIGH"),
    ]
    priority, findings = review_merge._aggregate_prescreen(rows, snapshot)
    assert priority == "HIGH"
    assert "Chunk 1/2" in findings and "Chunk 2/2" in findings


def test_aggregate_prescreen_waits_on_incomplete_chunks():
    snapshot = snap("diff --git a/a b/a\n" + "x\n" * 20_000)
    rows = [_chunk_row(0, 2, snapshot, priority="LOW")]
    status, reason = review_merge._aggregate_prescreen(rows, snapshot)
    assert status == "WAIT"
    assert "missing" in reason


def test_aggregate_prescreen_waits_on_stale_chunk():
    snapshot = snap("diff --git a/a b/a\n" + "x\n" * 20_000)
    rows = [
        _chunk_row(0, 2, snapshot, priority="LOW"),
        _chunk_row(1, 2, snapshot, priority="HIGH", head="a" * 40),
    ]
    status, reason = review_merge._aggregate_prescreen(rows, snapshot)
    assert status == "WAIT"
    assert "stale" in reason


def test_aggregate_prescreen_waits_on_unsucceeded_chunk():
    snapshot = snap("diff --git a/a b/a\n" + "x\n" * 20_000)
    rows = [
        _chunk_row(0, 2, snapshot, priority="LOW"),
        _chunk_row(1, 2, snapshot, state="failed"),
    ]
    status, reason = review_merge._aggregate_prescreen(rows, snapshot)
    assert status == "WAIT"
    assert "not_succeeded" in reason
