"""VOYN-W0-AICC-BRANCH-PROTECTION-LIMIT: no doc may claim branch protection
without the merge-gateway caveat.

`gh api repos/<owner>/<repo>/branches/main/protection` confirms the current
plan/repository has no real branch protection
(`required_approving_review_count=0`, `required_status_checks` absent,
`enforce_admins=false`) — see `docs/AUTHORITY_MAP.md`'s "Merge enforcement
authority" section, the source of truth for this claim.

Any Markdown doc that mentions "branch protection" is one edit away from
reintroducing the stale claim that GitHub enforces it. This fitness test
keeps that structural: every such doc (other than the authority map itself)
must cross-reference the authority map's merge-enforcement-authority section,
the same way README.md, ARCHITECTURE.md and CURRENT_STATE.md do today.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_MAP = REPO_ROOT / "docs" / "AUTHORITY_MAP.md"
ANCHOR = "AUTHORITY_MAP.md#merge-enforcement-authority-voyn-w0-aicc-branch-protection-limit"

# Root- and docs-level Markdown only: narrative docs a reader treats as
# authoritative, not scratch notes, changelogs or third-party vendor files.
_CANDIDATE_DIRS = (REPO_ROOT, REPO_ROOT / "docs")


def _docs_mentioning_branch_protection() -> list[Path]:
    found = []
    for directory in _CANDIDATE_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path == AUTHORITY_MAP:
                continue
            text = path.read_text(encoding="utf-8")
            if "branch protection" in text.lower():
                found.append(path)
    return found


def test_authority_map_documents_merge_enforcement():
    text = AUTHORITY_MAP.read_text(encoding="utf-8")
    assert "Merge enforcement authority" in text
    assert "required_approving_review_count=0" in text
    assert "no documentation or dashboard" in text


def test_every_branch_protection_mention_cross_references_the_authority_map():
    offenders = [
        path
        for path in _docs_mentioning_branch_protection()
        if ANCHOR not in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "these docs mention 'branch protection' without linking "
        f"docs/{ANCHOR} to disclose it is not actually enforced: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in offenders]}"
    )
