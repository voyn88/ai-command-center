"""Marker carry-over on branch update (VOYN-W0-AICC-MARKER-CARRYOVER-ON-
BRANCH-UPDATE): pure-function coverage that needs no PostgreSQL rig, since
none of `_latest_marker_sha`, `_run_git_patch_id`, `_accept_marker_churn_
count`, or `_carry_over_marker_if_patch_id_stable` touch the database --
they read only what `gh` (faked here) and the real `git patch-id` binary
report. See tests/db/test_review_merge.py for the merge_once integration
tests that exercise this against a live store."""
# ruff: noqa: RUF100

from __future__ import annotations

import json
import subprocess

from command_center.orchestrator import review_merge

PR = "https://github.com/voyn88/ai-command-center/pull/407"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
BASE = "c" * 40
BOT = "voyn88-acceptance-gate[bot]"
AUTHOR = "writer-bot"

# Two unified diffs for the same content edit (line 15 -> CHANGED) at two
# different hunk offsets (12 vs 15) and two different blob index hashes --
# exactly what `gh pr update-branch` produces for an unchanged PR diff after
# a base merge shifts where it lands. `git patch-id --stable` must treat
# these as identical; a plain text/sha256 hash of the diff would not.
DIFF_OFFSET_12 = (
    "diff --git a/f.txt b/f.txt\n"
    "index e8823e1..bfbcefc 100644\n"
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -12,7 +12,7 @@\n"
    " 12\n"
    " 13\n"
    " 14\n"
    "-15\n"
    "+CHANGED\n"
    " 16\n"
    " 17\n"
    " 18\n"
)
DIFF_OFFSET_15 = (
    "diff --git a/f.txt b/f.txt\n"
    "index 4ad90ab..7dc7b26 100644\n"
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -15,7 +15,7 @@\n"
    " 12\n"
    " 13\n"
    " 14\n"
    "-15\n"
    "+CHANGED\n"
    " 16\n"
    " 17\n"
    " 18\n"
)
DIFF_DIFFERENT_EDIT = (
    "diff --git a/f.txt b/f.txt\n"
    "index e8823e1..1111111 100644\n"
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -12,7 +12,7 @@\n"
    " 12\n"
    " 13\n"
    " 14\n"
    "-15\n"
    "+SOMETHING ELSE ENTIRELY\n"
    " 16\n"
    " 17\n"
    " 18\n"
)


# -- _run_git_patch_id --------------------------------------------------

def test_patch_id_is_stable_across_a_hunk_offset_shift():
    """The exact case a base-merge produces: same +/- content, different
    hunk line numbers and blob hashes -- `--stable` must agree."""
    assert review_merge._run_git_patch_id(DIFF_OFFSET_12) == (
        review_merge._run_git_patch_id(DIFF_OFFSET_15)
    )


def test_patch_id_differs_for_a_real_content_change():
    assert review_merge._run_git_patch_id(DIFF_OFFSET_12) != (
        review_merge._run_git_patch_id(DIFF_DIFFERENT_EDIT)
    )


def test_patch_id_is_none_for_an_empty_diff():
    assert review_merge._run_git_patch_id("") is None
    assert review_merge._run_git_patch_id("   \n") is None


def test_patch_id_is_none_when_git_itself_fails(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert review_merge._run_git_patch_id(DIFF_OFFSET_12) is None


# -- _latest_marker_sha ---------------------------------------------------

def test_latest_marker_sha_returns_the_priors_sha_even_though_it_is_not_head():
    reviews = [{"body": f"ACCEPTANCE: ACCEPT {OLD_HEAD}",
                "author": {"login": BOT}, "submittedAt": "2026-08-27T00:00:00Z"}]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) == OLD_HEAD


def test_latest_marker_sha_none_when_no_reviews():
    assert review_merge._latest_marker_sha([], AUTHOR) is None


def test_latest_marker_sha_none_when_most_recent_review_is_not_a_marker():
    reviews = [
        {"body": f"ACCEPTANCE: ACCEPT {OLD_HEAD}", "author": {"login": BOT},
         "submittedAt": "2026-08-27T00:00:00Z"},
        {"body": "just a comment", "author": {"login": BOT},
         "submittedAt": "2026-08-27T01:00:00Z"},
    ]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) is None


def test_latest_marker_sha_none_when_reviewer_is_the_pr_author():
    reviews = [{"body": f"ACCEPTANCE: ACCEPT {OLD_HEAD}",
                "author": {"login": AUTHOR}, "submittedAt": "2026-08-27T00:00:00Z"}]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) is None


# -- _accept_marker_churn_count -------------------------------------------

def test_churn_count_counts_every_marker_ever_posted(monkeypatch):
    body = json.dumps({"reviews": [
        {"body": f"ACCEPTANCE: ACCEPT {'1' * 40}"},
        {"body": f"ACCEPTANCE: ACCEPT {'2' * 40}"},
        {"body": "not a marker"},
        {"body": f"ACCEPTANCE: ACCEPT {'3' * 40}"},
    ]})

    def fake_gh(argv, _repo):
        assert argv == ["pr", "view", PR, "--json", "reviews"]
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    assert review_merge._accept_marker_churn_count("/tmp", PR) == 3


def test_churn_count_is_zero_on_a_failed_lookup(monkeypatch):
    monkeypatch.setattr(
        review_merge, "_gh",
        lambda argv, _repo: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )
    assert review_merge._accept_marker_churn_count("/tmp", PR) == 0


# -- _carry_over_marker_if_patch_id_stable --------------------------------

def _fake_gh_for_carryover(compare_diffs, comments=None, posted=None):
    comments = comments if comments is not None else []
    posted = posted if posted is not None else []

    def fake_gh(argv, _repo):
        if argv[:2] == ["pr", "view"] and "reviews,headRefOid,baseRefOid,author,state" in argv:
            body = json.dumps({
                "state": "OPEN", "headRefOid": NEW_HEAD, "baseRefOid": BASE,
                "author": {"login": AUTHOR},
                "reviews": [{"body": f"ACCEPTANCE: ACCEPT {OLD_HEAD}",
                             "author": {"login": BOT},
                             "submittedAt": "2026-08-27T00:00:00Z"}],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:2] == ["pr", "view"] and "comments" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"comments": comments}), ""
            )
        if argv[0] == "api":
            target = argv[1]
            if f"{OLD_HEAD}" in target.split("...")[-1]:
                return subprocess.CompletedProcess(argv, 0, compare_diffs[0], "")
            if f"{NEW_HEAD}" in target.split("...")[-1]:
                return subprocess.CompletedProcess(argv, 0, compare_diffs[1], "")
            return subprocess.CompletedProcess(argv, 1, "", "unexpected compare")
        if argv[:2] == ["pr", "comment"]:
            posted.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", f"unexpected: {argv}")

    return fake_gh


def test_carry_over_posts_marker_when_patch_id_is_stable(monkeypatch):
    posted_comments = []
    marker_calls = []
    monkeypatch.setattr(
        review_merge, "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_OFFSET_15), posted=posted_comments
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda _creds, pr, decision, sha: (marker_calls.append((pr, decision, sha)) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, "VOYN-W0-X")

    assert carried is True
    assert marker_calls == [(PR, "ACCEPT", NEW_HEAD)]
    assert len(posted_comments) == 1
    assert "MARKER-CARRYOVER" in posted_comments[0][posted_comments[0].index("--body") + 1]
    assert OLD_HEAD in posted_comments[0][posted_comments[0].index("--body") + 1]
    assert NEW_HEAD in posted_comments[0][posted_comments[0].index("--body") + 1]


def test_carry_over_declines_when_patch_id_changed(monkeypatch):
    marker_calls = []
    monkeypatch.setattr(
        review_merge, "_gh",
        _fake_gh_for_carryover((DIFF_OFFSET_12, DIFF_DIFFERENT_EDIT)),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_args: (marker_calls.append(_args) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, "VOYN-W0-X")

    assert carried is False
    assert marker_calls == []


def test_carry_over_declines_when_there_is_no_prior_marker(monkeypatch):
    def fake_gh(argv, _repo):
        if argv[:2] == ["pr", "view"]:
            body = json.dumps({
                "state": "OPEN", "headRefOid": NEW_HEAD, "baseRefOid": BASE,
                "author": {"login": AUTHOR}, "reviews": [],
            })
            return subprocess.CompletedProcess(argv, 0, body, "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)
    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, "VOYN-W0-X")
    assert carried is False


def test_carry_over_is_idempotent_and_still_posts_marker_on_retry(monkeypatch):
    """A tick that already posted the audit comment (but never got to the
    marker, e.g. a prior crash) must not double-post the audit -- and must
    still go on to post the marker, exactly like `_post_auto_accept_audit`'s
    contract."""
    prior_comment = {"body": f"MARKER-CARRYOVER {OLD_HEAD}->{NEW_HEAD}\n\nalready here"}
    marker_calls = []
    monkeypatch.setattr(
        review_merge, "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_OFFSET_15), comments=[prior_comment]
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda _creds, pr, decision, sha: (marker_calls.append((pr, decision, sha)) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, "VOYN-W0-X")

    assert carried is True
    assert marker_calls == [(PR, "ACCEPT", NEW_HEAD)]


def test_carry_over_declines_without_acceptance_bot_credentials(monkeypatch):
    marker_calls = []
    monkeypatch.setattr(
        review_merge, "_gh",
        _fake_gh_for_carryover((DIFF_OFFSET_12, DIFF_OFFSET_15)),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: None)
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_args: (marker_calls.append(_args) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, "VOYN-W0-X")

    assert carried is False
    assert marker_calls == []
