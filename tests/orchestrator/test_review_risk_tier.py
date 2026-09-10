"""VOYN-W0-AICC-REVIEW-RISK-TIER: the deterministic risk class `_review_pr_risk`
computes from a diff's own changed paths, and the single-chunk budget
`_review_chunks` grants a LOW-risk diff without changing HIGH-risk behaviour
at all.
"""

from __future__ import annotations

import json

import pytest

from command_center.orchestrator import review_merge

TASK = "VOYN-W0-RISK-TIER"
PR = "https://github.com/voyn88/ai-command-center/pull/381"
BASE, HEAD = "a" * 40, "b" * 40


def snap(text: str) -> review_merge._PRSnapshot:
    return review_merge._PRSnapshot.create(text, BASE, HEAD)


def diff_for(*paths: str, body: str = "-old\n+new\n") -> str:
    return "".join(f"diff --git a/{path} b/{path}\n{body}" for path in paths)


# -- _diff_changed_paths ------------------------------------------------------


def test_changed_paths_covers_both_sides_of_a_rename():
    diff = "diff --git a/old/name.py b/new/name.py\n-x\n+y\n"
    assert review_merge._diff_changed_paths(diff) == ("old/name.py", "new/name.py")


def test_changed_paths_dedupes_and_preserves_order():
    diff = diff_for("docs/a.md", "docs/b.md", "docs/a.md")
    assert review_merge._diff_changed_paths(diff) == ("docs/a.md", "docs/b.md")


def test_changed_paths_handles_quoted_headers():
    diff = 'diff --git "a/docs/has space.md" "b/docs/has space.md"\n-x\n+y\n'
    assert review_merge._diff_changed_paths(diff) == ("docs/has space.md",)


def test_changed_paths_empty_for_unrecognisable_diff():
    assert review_merge._diff_changed_paths("") == ()
    assert review_merge._diff_changed_paths("not a real diff\n") == ()


# -- _review_pr_risk -----------------------------------------------------------


def test_docs_only_diff_is_low_risk():
    diff = diff_for("docs/guide.md", "README.md", "CHANGELOG.md")
    assert review_merge._review_pr_risk(diff) == review_merge._REVIEW_RISK_LOW


def test_test_only_diff_is_low_risk():
    diff = diff_for("tests/test_thing.py", "tests/orchestrator/test_other.py")
    assert review_merge._review_pr_risk(diff) == review_merge._REVIEW_RISK_LOW


def test_mixed_docs_and_tests_is_low_risk():
    diff = diff_for("docs/guide.md", "tests/test_thing.py")
    assert review_merge._review_pr_risk(diff) == review_merge._REVIEW_RISK_LOW


def test_ordinary_source_change_is_high_risk():
    diff = diff_for("command_center/agent_runner.py")
    assert review_merge._review_pr_risk(diff) == review_merge._REVIEW_RISK_HIGH


def test_fail_closed_on_empty_or_unrecognisable_diff():
    assert review_merge._review_pr_risk("") == review_merge._REVIEW_RISK_HIGH
    assert review_merge._review_pr_risk("not a real diff\n") == review_merge._REVIEW_RISK_HIGH


def test_a_rename_out_of_docs_into_source_is_high_risk():
    diff = "diff --git a/docs/notes.md b/command_center/notes.py\n-x\n+y\n"
    assert review_merge._review_pr_risk(diff) == review_merge._REVIEW_RISK_HIGH


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "command_center/db/sql/0020_new_table.up.sql",
        "native_gateway/auth.py",
        "command_center/api/schemas.py",
        "command_center/db/migrations.py",
        "scripts/build_release_manifest.py",
        "RELEASE_NOTES_v2.0.md",
        "deploy/systemd/voyn.service",
        "packaging/macos/entitlements.plist",
        "requirements-security.lock",
        ".github/workflows/security-scans.yml",
    ],
)
def test_high_risk_category_overrides_an_otherwise_docs_only_change(path):
    diff = diff_for("docs/guide.md", path)
    assert review_merge._review_pr_risk(diff) == review_merge._REVIEW_RISK_HIGH


# -- _review_chunks: the budget the risk class actually buys ------------------


def test_low_risk_diff_gets_a_single_chunk_past_the_standard_budget():
    filler = "x\n" * 15_000  # ~30,000 bytes: over the standard budget, under 3x it
    diff = diff_for("docs/guide.md", body=filler)
    assert (
        len(review_merge._render_review_prompt(TASK, PR, snap(diff), review_merge._make_diff_chunks([diff])[0]).encode())
        > review_merge._MAX_REVIEW_PROMPT_BYTES
    )
    chunks = review_merge._review_chunks(snap(diff), TASK, PR)
    assert len(chunks) == 1
    assert chunks[0].text == diff


def test_equivalent_size_high_risk_diff_still_chunks_as_before():
    filler = "x\n" * 15_000
    diff = diff_for("command_center/agent_runner.py", body=filler)
    chunks = review_merge._review_chunks(snap(diff), TASK, PR)
    assert len(chunks) > 1
    assert "".join(chunk.text for chunk in chunks) == diff


def test_low_risk_diff_too_large_even_for_the_widened_budget_falls_back_unchanged():
    filler = "x\n" * 40_000  # far past even the 3x low-risk budget
    diff = diff_for("docs/guide.md", body=filler)
    low_risk_chunks = review_merge._review_chunks(snap(diff), TASK, PR)
    high_risk_diff = diff_for("command_center/agent_runner.py", body=filler)
    high_risk_chunks = review_merge._review_chunks(snap(high_risk_diff), TASK, PR)
    assert len(low_risk_chunks) > 1
    # Same per-chunk contract as HIGH risk: never a bigger per-chunk prompt.
    for chunk in low_risk_chunks:
        prompt = review_merge._render_review_prompt(TASK, PR, snap(diff), chunk)
        assert len(prompt.encode()) <= review_merge._MAX_REVIEW_PROMPT_BYTES
    assert len(low_risk_chunks) == len(high_risk_chunks)


def test_small_diff_is_a_single_chunk_regardless_of_risk():
    diff = diff_for("docs/guide.md")
    assert len(review_merge._review_chunks(snap(diff), TASK, PR)) == 1
    diff = diff_for("command_center/agent_runner.py")
    assert len(review_merge._review_chunks(snap(diff), TASK, PR)) == 1


# -- the envelope carries the class, for audit only ---------------------------


def test_envelope_carries_the_risk_class():
    low_diff = diff_for("docs/guide.md")
    chunk = review_merge._review_chunks(snap(low_diff), TASK, PR)[0]
    envelope = review_merge._review_input_envelope(TASK, PR, snap(low_diff), chunk)
    assert json.loads(envelope)["risk_class"] == "low"

    high_diff = diff_for("command_center/agent_runner.py")
    chunk = review_merge._review_chunks(snap(high_diff), TASK, PR)[0]
    envelope = review_merge._review_input_envelope(TASK, PR, snap(high_diff), chunk)
    assert json.loads(envelope)["risk_class"] == "high"
