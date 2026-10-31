import re
from pathlib import Path

from command_center.delivery_gate import CheckEvidence, evaluate_delivery


CANDIDATE = "a" * 40


def _green_check(*, head_sha: str = CANDIDATE) -> CheckEvidence:
    return CheckEvidence(
        name="quality",
        status="COMPLETED",
        conclusion="SUCCESS",
        head_sha=head_sha,
    )


def test_delivery_gate_accepts_only_completed_green_exact_head_checks():
    decision = evaluate_delivery(
        candidate_sha=CANDIDATE,
        pull_request_head_sha=CANDIDATE,
        checks=[_green_check()],
    )

    assert decision.allowed is True
    assert decision.reasons == ()


def test_delivery_gate_rejects_wrong_pr_or_check_sha():
    wrong_pr = evaluate_delivery(
        candidate_sha=CANDIDATE,
        pull_request_head_sha="b" * 40,
        checks=[_green_check()],
    )
    wrong_check = evaluate_delivery(
        candidate_sha=CANDIDATE,
        pull_request_head_sha=CANDIDATE,
        checks=[_green_check(head_sha="b" * 40)],
    )

    assert wrong_pr.reasons == ("pull_request_head_sha_mismatch",)
    assert wrong_check.reasons == ("quality:head_sha_mismatch",)


def test_delivery_gate_rejects_empty_pending_or_failed_ci():
    empty = evaluate_delivery(
        candidate_sha=CANDIDATE,
        pull_request_head_sha=CANDIDATE,
        checks=[],
    )
    pending = evaluate_delivery(
        candidate_sha=CANDIDATE,
        pull_request_head_sha=CANDIDATE,
        checks=[
            CheckEvidence(
                name="quality",
                status="IN_PROGRESS",
                conclusion=None,
                head_sha=CANDIDATE,
            )
        ],
    )
    failed = evaluate_delivery(
        candidate_sha=CANDIDATE,
        pull_request_head_sha=CANDIDATE,
        checks=[
            CheckEvidence(
                name="quality",
                status="COMPLETED",
                conclusion="FAILURE",
                head_sha=CANDIDATE,
            )
        ],
    )

    assert empty.reasons == ("ci_missing",)
    assert pending.reasons == ("quality:pending",)
    assert failed.reasons == ("quality:failure",)


def test_delivery_gate_rejects_auto_complete_chain_even_when_ci_is_green():
    decision = evaluate_delivery(
        candidate_sha=CANDIDATE,
        pull_request_head_sha=CANDIDATE,
        checks=[_green_check()],
        auto_complete_requested=True,
    )

    assert decision.allowed is False
    assert decision.reasons == ("auto_complete_forbidden",)


def test_reconciliation_snapshot_classifies_every_observed_open_pr_and_worktree():
    report = (
        Path(__file__).parents[1]
        / "docs"
        / "operations"
        / "WAVE2_SAFE_DELIVERY_RECONCILIATION.md"
    ).read_text(encoding="utf-8")
    pr_rows = [
        *(f"| AICC #{number} |" for number in (145, 146, *range(148, 159), 168)),
        "| ESF #24 |",
    ]
    worktree_rows = (
        "`/Users/dmitrijcernikov/Projects/ai-command-center`",
        *(
            f"daily-audit-worktrees/{suffix}"
            for suffix in (
                "0cea…",
                "4de1…",
                "5bfd…",
                "b232…",
                "c68d…",
                "cdd9…",
                "eb1a…",
            )
        ),
        "| `_worktrees/ai-command-center/p2-native-sections` |",
        "| `_worktrees/ai-command-center/wave0-baseline-20260808` |",
        "| `_worktrees/ai-command-center/wave1-provenance-20260808` |",
        "| `_worktrees/ai-command-center/wave2-safe-delivery-20260808` |",
        "| `_worktrees/ai-command-center/pr158-truncate-text-20260808` |",
        "`ai-command-center-codex-provider`",
        "`ai-command-center-mobile`",
        "`ai-command-center-production`",
        "`ai-command-center-production-4295f9b`",
        "`ai-command-center-production-canonical`",
        "`ai-command-center-win-d1-runner`",
        "| `ai-command-center/.claude/worktrees/admiring-feynman-e54554` |",
        "| `ai-command-center/.claude/worktrees/wizardly-dubinsky-2210c4` |",
    )

    assert len(pr_rows) == 15
    assert len(worktree_rows) == 21

    lines = report.splitlines()
    classification = re.compile(r"\*\*[A-Za-z][A-Za-z /-]*\*\*")
    for marker in (*pr_rows, *worktree_rows):
        matching_lines = [line for line in lines if marker in line]
        assert len(matching_lines) == 1, (
            f"{marker!r} should appear on exactly one row, found on "
            f"{len(matching_lines)}"
        )
        assert classification.search(matching_lines[0]), (
            f"row for {marker!r} names no bolded classification: "
            f"{matching_lines[0]!r}"
        )

    assert "PR #157 and #158 therefore remain separate and untouched" in report
