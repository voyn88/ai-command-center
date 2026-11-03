from __future__ import annotations

from types import SimpleNamespace

import pytest

from command_center.orchestrator import review_merge as m
from tests.orchestrator.test_chunked_review import HEAD, PR, TASK, rows, snap

PATH = "web/public/icons/icon-192.png"
PNG = (f"diff --git a/{PATH} b/{PATH}\nnew file mode 100644\n"
       f"index 0000000..1234567\nBinary files /dev/null and b/{PATH} differ\n")


def test_added_png_requires_isolated_exact_head_image_review(monkeypatch):
    snapshot = snap(PNG)
    monkeypatch.setattr(m, "_verification_review_cascade", lambda: [
        {"executor": "claude", "task_type": "verification_review", "capability": "read_only"}
    ])
    payload = {"task_type": "independent_review", "cascade": []}
    assert m._bind_media_review(payload, snapshot, PR)
    assert payload["task_type"] == "verification_review"
    assert payload["review_head"] == {"pr_number": "380", "head_sha": HEAD}
    prompt = m._render_review_prompt(TASK, PR, snapshot, m._review_chunks(snapshot, TASK, PR)[0])
    assert "No tools are available" not in prompt
    assert "Open and visually inspect EVERY" in prompt
    assert not m._media_review_complete(f"VERDICT: ACCEPT\nHEAD_SHA: {HEAD}", snapshot)
    assert not m._media_review_complete(f"MEDIA_REVIEW: {PATH} {'a' * 40}", snapshot)
    assert m._media_review_complete(f"MEDIA_REVIEW: {PATH} {HEAD}", snapshot)


@pytest.mark.parametrize("text", [
    PNG.replace("new file mode 100644", "old mode 100644"),
    PNG.replace("100644", "120000"),
    PNG.replace(".png", ".exe"),
    PNG.replace("icon-192.png", "../icon-192.png"),
    PNG.replace("Binary files /dev/null", "Binary files a/old.png"),
    PNG.replace(f"Binary files /dev/null and b/{PATH} differ", "GIT binary patch"),
    PNG + PNG,
])
def test_unknown_binary_symlink_modified_or_ambiguous_media_stays_closed(text):
    assert m._review_media_paths(text) is None


def test_every_chunk_needs_media_receipts_before_aggregate_accept():
    snapshot = snap(PNG + "diff --git a/a b/a\n" + "x\n" * 40_000)
    review_rows = rows(snapshot)
    prefix = m._chunk_key_prefix(TASK, PR, snapshot)
    verdict, reason = m._aggregate_chunk_verdict(review_rows, snapshot, prefix)
    assert verdict == "WAIT"
    assert "media_review_receipt_missing" in reason
    for row in review_rows:
        row[3]["result_text"] = f"MEDIA_REVIEW: {PATH} {HEAD}\n" + row[3]["result_text"]
    assert m._aggregate_chunk_verdict(review_rows, snapshot, prefix)[0] == "ACCEPT"


def test_missing_media_receipt_is_retryable_not_a_false_success(monkeypatch):
    monkeypatch.setattr(m, "_latest_attempt", lambda *_: (
        0, "succeeded", {"result_text": f"VERDICT: ACCEPT\nHEAD_SHA: {HEAD}"}
    ))
    assert m._next_retry_key(None, TASK, "key", HEAD, snap(PNG)) == "key:retry:1"


def test_no_media_executor_route_never_falls_back_to_text_only(monkeypatch):
    monkeypatch.setattr(m, "_verification_review_cascade", list)
    assert not m._bind_media_review({}, snap(PNG), PR)


def test_blocked_refresh_cursor_covers_tail_across_restarts_and_failure(tmp_path, monkeypatch):
    cfg = m.PrWindowConfig(blocked_refresh_budget=2)
    prs = [{"number": n, "createdAt": str(n), "labels": [{"name": cfg.label_blocked}]}
           for n in range(1, 6)]
    calls = []
    def fetch(_repo, pr, _cache, **kwargs):
        assert kwargs == {"refresh": True, "include_reviews": False}
        calls.append(pr["number"])
        return None if pr["number"] == 1 else pr
    monkeypatch.setattr(m, "_pr_window_details", fetch)
    for expected in ([1, 2], [3, 4], [5, 1]):
        # New object each time, as in separate systemd processes.
        cache = SimpleNamespace(directory=tmp_path)
        result = m._refresh_blocked_tail("/repo", prs, cfg, cache, 2)
        assert list(result) == expected
    assert calls == [1, 2, 3, 4, 5, 1]


def test_refresh_never_touches_queue_selected_or_active_and_is_repo_scoped(tmp_path, monkeypatch):
    cfg = m.PrWindowConfig()
    prs = [{"number": n, "labels": [{"name": label} for label in labels]}
           for n, labels in [(1, [cfg.label_active]), (2, [cfg.label_blocked]),
                             (3, [cfg.label_blocked, "queue-waiting-review"])]]
    monkeypatch.setattr(m, "_pr_window_details", lambda _r, pr, _c, **kw: pr)
    cache = SimpleNamespace(directory=tmp_path)
    assert list(m._refresh_blocked_tail("/repo", prs, cfg, cache, 1)) == [2]
    assert list(m._refresh_blocked_tail("/other", prs, cfg, cache, 1)) == [2]
    assert len(list(tmp_path.glob("*.cursor"))) == 2


def test_valid_media_retry_replaces_earlier_incomplete_accept(monkeypatch):
    snapshot = snap(PNG)
    first = rows(snapshot)[0]
    retry = (first[0] + ":retry:1", first[1], first[2], {
        "result_text": f"MEDIA_REVIEW: {PATH} {HEAD}\nVERDICT: ACCEPT\nHEAD_SHA: {HEAD}"
    })
    monkeypatch.setattr(m, "_rows", lambda *args: [first, retry])
    prefix, selected = m._chunk_review_rows(None, TASK, PR, snapshot)
    assert prefix is not None
    assert selected[0][3] == retry[3]
    assert m._media_review_complete(selected[0][3]["result_text"], snapshot)


def test_verification_prompt_requires_same_image_evidence():
    prompt = m._render_verification_prompt(TASK, PR, snap(PNG), "textual finding")
    assert f"MEDIA_REVIEW: {PATH} {HEAD}" in prompt
    assert "Open and visually inspect EVERY" in prompt


def test_cursor_rotation_survives_disabled_detail_cache(tmp_path, monkeypatch):
    env = {"AICC_GH_CACHE_TTL_SECONDS": "0", "AICC_GH_CACHE_DIR": str(tmp_path)}
    cfg = m.PrWindowConfig()
    prs = [{"number": n, "labels": [{"name": cfg.label_blocked}]} for n in [1, 2]]
    monkeypatch.setattr(m, "_pr_window_details", lambda _r, pr, _c, **kw: pr)
    for expected in [1, 2, 1]:
        cache = m.gh_access.PrDetailCache.from_env(env)
        assert not cache.enabled
        assert list(m._refresh_blocked_tail("/repo", prs, cfg, cache, 1)) == [expected]


def test_missing_verifier_media_receipt_uses_existing_malformed_result_recovery(monkeypatch):
    snapshot = snap(PNG)
    text = f"VERDICT: ACCEPT\nHEAD_SHA: {HEAD}"
    monkeypatch.setattr(m, "_latest_review_result", lambda *args: {"result_text": text})
    monkeypatch.setattr(m, "_verification_accept_is_well_formed", lambda *args: True)
    outcome, detail = m._verified_rejection_outcome(
        None, None, TASK, PR, snapshot, "code.py:1: concrete finding", m.ReviewConfig()
    )
    assert outcome == "REMEDIATE"
    assert detail == "code.py:1: concrete finding"


def test_disabled_cache_keeps_stable_cursor_directory_on_windows(tmp_path, monkeypatch):
    monkeypatch.delattr(m.gh_access.os, "getuid", raising=False)
    env = {"AICC_GH_CACHE_TTL_SECONDS": "0", "USERPROFILE": str(tmp_path)}
    first = m.gh_access.PrDetailCache.from_env(env)
    second = m.gh_access.PrDetailCache.from_env(env)
    assert not first.enabled
    assert first.directory == second.directory == tmp_path / ".cache/aicc/gh-details"


def test_media_binding_preserves_failover_and_filters_text_only_adapters():
    payload = {"cascade": [
        {"executor": "claude"}, {"executor": "grok"}, {"executor": "codex"}
    ]}
    assert m._bind_media_review(payload, snap(PNG), PR)
    assert [x["executor"] for x in payload["cascade"]] == ["claude", "codex"]
    assert all(x["capability"] == "read_only" for x in payload["cascade"])
    assert payload["media_review"] is True


def test_media_owner_alternatives_has_no_image_route(monkeypatch):
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-alternatives")
    assert not m._bind_media_review({}, snap(PNG), PR)


def test_worker_profile_cannot_rewrite_media_into_text_only(monkeypatch):
    import threading

    from command_center.worker import handlers
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-alternatives")
    monkeypatch.setattr(handlers, "parse_agent_run", lambda _: SimpleNamespace(
        task_type="verification_review"))
    # Preserve the real profile rewrite while keeping request construction local.
    monkeypatch.setattr(handlers, "replace", lambda request, **kw: SimpleNamespace(
        task_type=request.task_type, model=None, **kw))
    result = handlers._run_agent({"media_review": True}, threading.Event())
    assert not result.ok and result.infra_wait
    assert result.reason == "media_review_image_executor_unavailable"
