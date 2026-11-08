from __future__ import annotations

import base64

import pytest

from command_center.orchestrator import planner, review_merge

TASK = "VOYN-W0-PNG-REVIEW"
PR = "https://github.com/voyn88/ai-command-center/pull/1079"
BASE, HEAD = "c" * 40, "e" * 40


def snap(text):
    return review_merge._PRSnapshot.create(text, BASE, HEAD)


def png_bytes(width: int, height: int) -> bytes:
    return (
        review_merge._PNG_SIGNATURE
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


# -- _png_diff_paths ----------------------------------------------------------


def test_png_diff_paths_empty_for_a_text_only_diff():
    assert review_merge._png_diff_paths("diff --git a/x b/x\n+y\n") == ()


def test_png_diff_paths_extracts_png_only_binary_changes():
    diff = (
        "diff --git a/a.png b/a.png\nindex 1..2 100644\n"
        "Binary files a/a.png and b/a.png differ\n"
        "diff --git a/dir/b.PNG b/dir/b.PNG\nindex 3..4 100644\n"
        "Binary files a/dir/b.PNG and b/dir/b.PNG differ\n"
    )
    assert review_merge._png_diff_paths(diff) == ("a.png", "dir/b.PNG")


def test_png_diff_paths_requires_a_receipt_only_for_what_exists_at_head():
    added = "Binary files /dev/null and b/new.png differ\n"
    assert review_merge._png_diff_paths(added) == ("new.png",)

    # A pure deletion leaves nothing at HEAD to decode -- requiring a
    # receipt for the vanished path would 404 forever and block the
    # deletion from ever merging.
    deleted = "Binary files a/old.png and /dev/null differ\n"
    assert review_merge._png_diff_paths(deleted) == ()

    # A rename only needs the NEW path inspected; the old one is gone from
    # the tree that would merge.
    renamed = "Binary files a/old.png and b/new.png differ\n"
    assert review_merge._png_diff_paths(renamed) == ("new.png",)

    # Deleting (or renaming away) a non-png binary still fails the whole
    # diff closed, even though the vanished path itself needs no receipt.
    deleted_other = "Binary files a/data.bin and /dev/null differ\n"
    assert review_merge._png_diff_paths(deleted_other) is None


def test_png_diff_paths_refuses_non_png_binary():
    diff = "Binary files a/data.bin and b/data.bin differ\n"
    assert review_merge._png_diff_paths(diff) is None


def test_png_diff_paths_refuses_mixed_png_and_other_binary():
    diff = (
        "Binary files a/a.png and b/a.png differ\n"
        "Binary files a/data.bin and b/data.bin differ\n"
    )
    assert review_merge._png_diff_paths(diff) is None


def test_png_diff_paths_refuses_git_binary_patch():
    diff = "diff --git a/a.png b/a.png\nGIT binary patch\nliteral 10\n"
    assert review_merge._png_diff_paths(diff) is None


# -- _png_ground_truth ---------------------------------------------------------


def test_png_ground_truth_decodes_real_ihdr_dimensions(monkeypatch):
    raw = png_bytes(640, 480)
    body = {"encoding": "base64", "content": base64.b64encode(raw).decode()}
    monkeypatch.setattr(
        review_merge, "_rest_json", lambda _repo, _path: (review_merge._REST_OK, body)
    )
    fact = review_merge._png_ground_truth("/repo", "voyn88", "ai-command-center", "a.png", HEAD)
    assert fact == review_merge._PngFact("a.png", 640, 480)


@pytest.mark.parametrize("body", [
    {"encoding": "base64", "content": base64.b64encode(b"not a png").decode()},
    {"encoding": "base64", "content": "@@@not-base64@@@"},
    {"encoding": "none", "content": "irrelevant"},
    {"content": "missing-encoding-field"},
    {},
])
def test_png_ground_truth_fails_closed_on_bad_content(monkeypatch, body):
    monkeypatch.setattr(
        review_merge, "_rest_json", lambda _repo, _path: (review_merge._REST_OK, body)
    )
    assert review_merge._png_ground_truth("/repo", "o", "r", "a.png", HEAD) is None


def test_png_ground_truth_fails_closed_on_rest_failure(monkeypatch):
    monkeypatch.setattr(
        review_merge, "_rest_json", lambda _repo, _path: (review_merge._REST_FAILED, None)
    )
    assert review_merge._png_ground_truth("/repo", "o", "r", "a.png", HEAD) is None


# -- receipt matching -----------------------------------------------------------


def test_media_receipts_match_requires_every_path_with_exact_dimensions():
    facts = [review_merge._PngFact("a.png", 640, 480), review_merge._PngFact("b.png", 10, 10)]
    good = "MEDIA_REVIEW: a.png 640x480\nMEDIA_REVIEW: b.png 10x10\nVERDICT: ACCEPT\n"
    assert review_merge._media_receipts_match(good, facts)

    missing_one = "MEDIA_REVIEW: a.png 640x480\nVERDICT: ACCEPT\n"
    assert not review_merge._media_receipts_match(missing_one, facts)

    wrong_dims = "MEDIA_REVIEW: a.png 641x480\nMEDIA_REVIEW: b.png 10x10\n"
    assert not review_merge._media_receipts_match(wrong_dims, facts)

    transposed = "MEDIA_REVIEW: a.png 480x640\nMEDIA_REVIEW: b.png 10x10\n"
    assert not review_merge._media_receipts_match(transposed, facts)


def test_media_receipts_match_is_false_with_no_facts():
    assert not review_merge._media_receipts_match("MEDIA_REVIEW: a.png 1x1\n", [])


def test_render_media_review_prompt_never_states_the_required_dimensions():
    facts = [review_merge._PngFact("assets/logo.png", 640, 480)]
    prompt = review_merge._render_media_review_prompt(PR, snap("diff"), facts)
    assert "640" not in prompt
    assert "480" not in prompt
    assert "assets/logo.png" in prompt
    assert HEAD in prompt


# -- _media_review_outcome -------------------------------------------------------


def test_media_review_outcome_rejects_when_ground_truth_is_unreadable(monkeypatch):
    monkeypatch.setattr(review_merge, "_png_ground_truth", lambda *_: None)
    verdict, detail = review_merge._media_review_outcome(
        None, lambda *_: None, "/repo", TASK, PR, snap("diff"), ("a.png",),
        review_merge.ReviewConfig(),
    )
    assert verdict == "REJECT"
    assert detail == "media_ground_truth_unreadable:a.png"


def test_media_review_outcome_enqueues_a_single_verification_review(monkeypatch):
    fact = review_merge._PngFact("a.png", 640, 480)
    monkeypatch.setattr(review_merge, "_png_ground_truth", lambda *_: fact)
    monkeypatch.setattr(review_merge, "_latest_review_result", lambda *_: None)
    monkeypatch.setattr(review_merge, "_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(planner, "repo_route", lambda _r: ("AICC", "/repo"))
    monkeypatch.setattr(
        review_merge, "_verification_review_cascade", lambda: [{"executor": "copilot"}]
    )
    dispatched = []
    verdict, detail = review_merge._media_review_outcome(
        None, lambda *args: dispatched.append(args), "/repo", TASK, PR, snap("diff"),
        ("a.png",), review_merge.ReviewConfig(),
    )
    assert verdict == "WAIT"
    assert detail == "media_review_enqueued"
    assert len(dispatched) == 1
    _queue, _key, payload, dispatched_task, _cascade_len = dispatched[0]
    assert dispatched_task == TASK
    assert payload["task_type"] == "verification_review"
    assert payload["review_head"] == {"pr_number": "1079", "head_sha": HEAD}
    assert "640" not in payload["prompt"] and "480" not in payload["prompt"]


def test_media_review_outcome_waits_without_a_fresh_enqueue_while_pending(monkeypatch):
    fact = review_merge._PngFact("a.png", 640, 480)
    monkeypatch.setattr(review_merge, "_png_ground_truth", lambda *_: fact)
    monkeypatch.setattr(review_merge, "_latest_review_result", lambda *_: None)
    monkeypatch.setattr(review_merge, "_rows", lambda *_a, **_k: [("ready", None)])
    dispatched = []
    verdict, detail = review_merge._media_review_outcome(
        None, lambda *args: dispatched.append(args), "/repo", TASK, PR, snap("diff"),
        ("a.png",), review_merge.ReviewConfig(),
    )
    assert (verdict, detail) == ("WAIT", "media_review_pending")
    assert not dispatched


def test_media_review_outcome_accepts_only_with_matching_receipts(monkeypatch):
    fact = review_merge._PngFact("a.png", 640, 480)
    monkeypatch.setattr(review_merge, "_png_ground_truth", lambda *_: fact)

    monkeypatch.setattr(
        review_merge, "_latest_review_result",
        lambda *_: {"result_text": f"MEDIA_REVIEW: a.png 640x480\nVERDICT: ACCEPT\nHEAD_SHA: {HEAD}\n"},
    )
    verdict, detail = review_merge._media_review_outcome(
        None, None, "/repo", TASK, PR, snap("diff"), ("a.png",), review_merge.ReviewConfig(),
    )
    assert (verdict, detail) == ("ACCEPT", "media_receipts_verified")

    monkeypatch.setattr(
        review_merge, "_latest_review_result",
        lambda *_: {"result_text": f"MEDIA_REVIEW: a.png 1x1\nVERDICT: ACCEPT\nHEAD_SHA: {HEAD}\n"},
    )
    verdict, detail = review_merge._media_review_outcome(
        None, None, "/repo", TASK, PR, snap("diff"), ("a.png",), review_merge.ReviewConfig(),
    )
    assert (verdict, detail) == ("REJECT", "media_review_receipt_mismatch")

    monkeypatch.setattr(
        review_merge, "_latest_review_result",
        lambda *_: {"result_text": f"The image looks wrong.\nVERDICT: REJECT\nHEAD_SHA: {HEAD}\n"},
    )
    verdict, detail = review_merge._media_review_outcome(
        None, None, "/repo", TASK, PR, snap("diff"), ("a.png",), review_merge.ReviewConfig(),
    )
    assert verdict == "REJECT" and detail.startswith("media_review_rejected:")


# -- end-to-end gate through publish_review_verdicts -----------------------------


def test_publish_review_verdicts_withholds_marker_until_media_receipts_verify(monkeypatch):
    diff = "diff --git a/a.png b/a.png\nBinary files a/a.png and b/a.png differ\n"
    snapshot = snap(diff)
    posted = []
    monkeypatch.setattr(
        review_merge, "_rows",
        lambda _factory, sql, _params=(): [(TASK, PR)] if "SELECT t.task_id" in sql else [],
    )
    monkeypatch.setattr(review_merge, "_pr_diff_and_head", lambda *_: snapshot)
    monkeypatch.setattr(review_merge, "_has_accept_marker_with_pull", lambda *_a, **_k: (False, HEAD))
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", object)
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda _c, _p, verdict, sha: (posted.append((verdict, sha)) or True, ""),
    )
    monkeypatch.setattr(
        review_merge, "_latest_review_result",
        lambda _f, _t, key: (
            None if key.startswith("media:")
            else {"result_text": f"VERDICT: ACCEPT\nHEAD_SHA: {HEAD}"}
        ),
    )
    monkeypatch.setattr(
        review_merge, "_png_ground_truth",
        lambda *_: review_merge._PngFact("a.png", 640, 480),
    )
    monkeypatch.setattr(planner, "repo_route", lambda _r: ("AICC", "/repo"))
    monkeypatch.setattr(
        review_merge, "_verification_review_cascade", lambda: [{"executor": "copilot"}]
    )
    dispatched = []

    report = review_merge.publish_review_verdicts(
        None, "/repo", enqueue=lambda *args: dispatched.append(args)
    )

    assert not posted
    assert dispatched and dispatched[0][2]["task_type"] == "verification_review"
    assert any("media_review_enqueued" in reason for _t, reason in report.skipped)
