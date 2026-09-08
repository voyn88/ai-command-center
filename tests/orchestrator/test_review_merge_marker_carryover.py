"""Marker carry-over on branch update (VOYN-W0-AICC-MARKER-CARRYOVER-ON-
BRANCH-UPDATE-REM): `_run_git_patch_id`, `_latest_marker_sha`,
`_accept_marker_churn_count` and `_carry_over_marker_if_patch_id_stable` in
`command_center.orchestrator.review_merge`, all pure/subprocess-only (no
PostgreSQL) -- `_gh` is faked in-process the same way tests/orchestrator/
test_chunked_review.py fakes it."""

from __future__ import annotations

import json
import subprocess

from command_center.orchestrator import review_merge

OWNER, REPO, NUMBER = "voyn88", "ai-command-center", "701"
PR = f"https://github.com/{OWNER}/{REPO}/pull/{NUMBER}"
TASK = "VOYN-W0-CARRYOVER"

BASE = "b" * 40
OLD_HEAD = "1" * 40
NEW_HEAD = "2" * 40
AUTHOR = "task-agent"
BOT = "voyn88-acceptance-gate[bot]"

# Same changed content, different hunk-header line numbers -- exactly what
# `gh pr update-branch` produces when the base advances but nobody touches
# the PR's own lines: `git patch-id --stable` must treat these as identical.
DIFF_OFFSET_12 = (
    "diff --git a/x.py b/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -12,3 +12,4 @@ def foo():\n"
    "     a = 1\n"
    "     b = 2\n"
    "     c = 3\n"
    "+    d = 4\n"
)
DIFF_OFFSET_15 = (
    "diff --git a/x.py b/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -15,3 +15,4 @@ def foo():\n"
    "     a = 1\n"
    "     b = 2\n"
    "     c = 3\n"
    "+    d = 4\n"
)
# A genuinely different edit (different added line) -- must NOT match.
DIFF_REAL_EDIT = (
    "diff --git a/x.py b/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -12,3 +12,4 @@ def foo():\n"
    "     a = 1\n"
    "     b = 2\n"
    "     c = 3\n"
    "+    d = 5\n"
)


def _proc(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


def _review(sha, author=BOT, submitted="2026-08-27T05:00:00Z", state="APPROVED"):
    return {
        "body": f"ACCEPTANCE: ACCEPT {sha}\n\nlgtm",
        "author": {"login": author},
        "submittedAt": submitted,
        "state": state,
    }


def _fake_gh_for_carryover(
    diffs,
    *,
    base=BASE,
    old_head=OLD_HEAD,
    new_head=NEW_HEAD,
    author=AUTHOR,
    reviews=None,
    comments=None,
    posted=None,
):
    """`diffs` is (old_diff, new_diff) keyed by (prior sha, current head)."""
    old_diff, new_diff = diffs
    reviews = [] if reviews is None else reviews
    comments = [] if comments is None else comments

    def fake_gh(argv, _repo):
        if argv[:2] == ["api", f"repos/{OWNER}/{REPO}/pulls/{NUMBER}"]:
            body = {
                "base": {"sha": base},
                "head": {"sha": new_head},
                "user": {"login": author},
            }
            return _proc(json.dumps(body))
        if argv[:3] == ["pr", "view", PR] and argv[3:] == ["--json", "reviews,comments"]:
            return _proc(json.dumps({"reviews": reviews, "comments": comments}))
        if argv[0] == "api" and argv[1].startswith(
            f"repos/{OWNER}/{REPO}/compare/{base}..."
        ):
            sha = argv[1].rsplit("...", 1)[1]
            if sha == old_head:
                return _proc(old_diff)
            if sha == new_head:
                return _proc(new_diff)
            return _proc("", returncode=1)
        if argv[:2] == ["pr", "comment"]:
            body = argv[argv.index("--body") + 1]
            if posted is not None:
                posted.append(body)
            return _proc("")
        raise AssertionError(f"unexpected gh call: {argv}")

    return fake_gh


# -- _run_git_patch_id --------------------------------------------------


def test_run_git_patch_id_is_stable_across_hunk_header_offsets(tmp_path):
    a = review_merge._run_git_patch_id(str(tmp_path), DIFF_OFFSET_12)
    b = review_merge._run_git_patch_id(str(tmp_path), DIFF_OFFSET_15)
    assert a is not None
    assert a == b


def test_run_git_patch_id_differs_for_a_genuinely_different_edit(tmp_path):
    a = review_merge._run_git_patch_id(str(tmp_path), DIFF_OFFSET_12)
    c = review_merge._run_git_patch_id(str(tmp_path), DIFF_REAL_EDIT)
    assert a is not None and c is not None
    assert a != c


def test_run_git_patch_id_returns_none_over_the_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(review_merge, "_MAX_REVIEW_DIFF_BYTES", 10)
    assert review_merge._run_git_patch_id(str(tmp_path), DIFF_OFFSET_12) is None


# -- _latest_marker_sha ---------------------------------------------------


def test_latest_marker_sha_is_none_with_no_reviews():
    assert review_merge._latest_marker_sha([], AUTHOR) is None


def test_latest_marker_sha_is_none_when_the_latest_review_has_no_marker():
    reviews = [
        _review(OLD_HEAD, submitted="2026-08-27T05:00:00Z"),
        {
            "body": "just a comment",
            "author": {"login": BOT},
            "submittedAt": "2026-08-27T06:00:00Z",
            "state": "COMMENTED",
        },
    ]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) is None


def test_latest_marker_sha_excludes_a_self_issued_marker():
    """Defect 2 regression: a marker whose review author IS the PR's own
    author must never be trusted as the carry-over baseline -- the same
    self-approval bypass `_accept_marker_on_latest_review` already closes
    for the live merge path."""
    reviews = [_review(OLD_HEAD, author=AUTHOR)]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) is None


def test_latest_marker_sha_excludes_a_self_issued_marker_differing_only_by_case():
    reviews = [_review(OLD_HEAD, author=AUTHOR.upper())]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) is None


def test_latest_marker_sha_returns_the_most_recent_live_accept():
    reviews = [
        _review(OLD_HEAD, submitted="2026-08-27T05:00:00Z"),
        _review(NEW_HEAD, submitted="2026-08-27T06:00:00Z"),
    ]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) == NEW_HEAD


def test_latest_marker_sha_skips_a_dismissed_review():
    reviews = [
        _review(OLD_HEAD, submitted="2026-08-27T05:00:00Z"),
        _review(NEW_HEAD, submitted="2026-08-27T06:00:00Z", state="DISMISSED"),
    ]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) == OLD_HEAD


def test_latest_marker_sha_accepts_a_genuine_bot_marker_with_pr_author_supplied():
    """A real regression guard for the other half of defect 2: filtering by
    the correct (exclusion) identity must actually match a genuine
    bot-authored marker, not silently become a no-op."""
    reviews = [_review(OLD_HEAD, author=BOT)]
    assert review_merge._latest_marker_sha(reviews, AUTHOR) == OLD_HEAD


# -- _accept_marker_churn_count -------------------------------------------


def test_accept_marker_churn_count_is_zero_with_no_tagged_comments():
    comments = [{"body": "unrelated"}, {"body": "AUTO-ACCEPT-AUDIT deadbeef findings:1"}]
    assert review_merge._accept_marker_churn_count(comments) == 0


def test_accept_marker_churn_count_counts_only_tagged_comments():
    comments = [
        {"body": f"MARKER-CARRYOVER {OLD_HEAD}->{NEW_HEAD}\n\nmore text"},
        {"body": "unrelated"},
        {"body": f"MARKER-CARRYOVER {NEW_HEAD}->{'3' * 40}"},
    ]
    assert review_merge._accept_marker_churn_count(comments) == 2


# -- _carry_over_marker_if_patch_id_stable --------------------------------


def test_carry_over_posts_marker_when_patch_id_is_stable(monkeypatch):
    posted, marker_calls = [], []
    monkeypatch.setattr(
        review_merge,
        "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_OFFSET_15),
            reviews=[_review(OLD_HEAD, author=BOT)],
            posted=posted,
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda _c, _p, verdict, sha: (marker_calls.append((_p, verdict, sha)) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, TASK)

    assert carried is True
    assert marker_calls == [(PR, "ACCEPT", NEW_HEAD)]
    assert len(posted) == 1
    assert posted[0].startswith(f"MARKER-CARRYOVER {OLD_HEAD}->{NEW_HEAD}")


def test_carry_over_is_idempotent_and_still_posts_marker_on_retry(monkeypatch):
    """A tick that already posted the audit comment (e.g. it was
    interrupted before the marker call) must not double-post the audit --
    and must still go on to post the marker."""
    posted, marker_calls = [], []
    prior_comment = {"body": f"MARKER-CARRYOVER {OLD_HEAD}->{NEW_HEAD}\n\nalready here"}
    monkeypatch.setattr(
        review_merge,
        "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_OFFSET_15),
            reviews=[_review(OLD_HEAD, author=BOT)],
            comments=[prior_comment],
            posted=posted,
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda _c, _p, verdict, sha: (marker_calls.append((_p, verdict, sha)) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, TASK)

    assert carried is True
    assert marker_calls == [(PR, "ACCEPT", NEW_HEAD)]
    assert len(posted) == 0


def test_carry_over_declines_when_patch_id_differs(monkeypatch):
    posted, marker_calls = [], []
    monkeypatch.setattr(
        review_merge,
        "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_REAL_EDIT),
            reviews=[_review(OLD_HEAD, author=BOT)],
            posted=posted,
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_: (marker_calls.append(True) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, TASK)

    assert carried is False
    assert not marker_calls
    assert not posted


def test_carry_over_declines_without_acceptance_bot_credentials(monkeypatch):
    marker_calls = []
    monkeypatch.setattr(
        review_merge,
        "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_OFFSET_15),
            reviews=[_review(OLD_HEAD, author=BOT)],
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: None)
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_: (marker_calls.append(True) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, TASK)

    assert carried is False
    assert not marker_calls


def test_carry_over_declines_when_the_prior_marker_is_self_issued(monkeypatch):
    """Defect 2 regression, end to end: a PR author who posts their own
    marker-shaped review body must not get it carried over as if it were
    the trusted acceptance identity's verdict."""
    marker_calls = []
    monkeypatch.setattr(
        review_merge,
        "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_OFFSET_15),
            reviews=[_review(OLD_HEAD, author=AUTHOR)],
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_: (marker_calls.append(True) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, TASK)

    assert carried is False
    assert not marker_calls


def test_carry_over_declines_past_the_churn_cap(monkeypatch):
    marker_calls = []
    churned_comments = [
        {"body": f"MARKER-CARRYOVER {'0' * 40}->{str(i) * 40}"}
        for i in range(1, review_merge._MAX_MARKER_CARRYOVER_CHURN + 1)
    ]
    monkeypatch.setattr(
        review_merge,
        "_gh",
        _fake_gh_for_carryover(
            (DIFF_OFFSET_12, DIFF_OFFSET_15),
            reviews=[_review(OLD_HEAD, author=BOT)],
            comments=churned_comments,
        ),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_: (marker_calls.append(True) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, TASK)

    assert carried is False
    assert not marker_calls


def test_carry_over_declines_with_no_prior_marker_at_all(monkeypatch):
    marker_calls = []
    monkeypatch.setattr(
        review_merge,
        "_gh",
        _fake_gh_for_carryover((DIFF_OFFSET_12, DIFF_OFFSET_15), reviews=[]),
    )
    monkeypatch.setattr(review_merge, "_acceptance_app_credentials", lambda: object())
    monkeypatch.setattr(
        review_merge, "_post_marker_as_bot",
        lambda *_: (marker_calls.append(True) or True, ""),
    )

    carried = review_merge._carry_over_marker_if_patch_id_stable("/tmp", PR, TASK)

    assert carried is False
    assert not marker_calls
