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
must cross-reference the authority map's merge-enforcement-authority section
via a real Markdown link *href* (not merely link text), the same way
README.md, ARCHITECTURE.md and CURRENT_STATE.md do today.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_MAP = REPO_ROOT / "docs" / "AUTHORITY_MAP.md"
ANCHOR = "AUTHORITY_MAP.md#merge-enforcement-authority-voyn-w0-aicc-branch-protection-limit"

# Match a Markdown inline link whose href (inside the parentheses) contains
# the authority-map anchor. Link text is ignored: putting the anchor only in
# the brackets is a broken cross-reference and must not satisfy this gate.
_HREF_WITH_ANCHOR = re.compile(
    r"\[[^\]]*\]\([^)\s]*" + re.escape(ANCHOR) + r"[^)]*\)"
)

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


def _has_authority_map_href(text: str) -> bool:
    """True only when the anchor appears inside a Markdown link href."""
    return _HREF_WITH_ANCHOR.search(text) is not None


def test_authority_map_documents_merge_enforcement():
    text = AUTHORITY_MAP.read_text(encoding="utf-8")
    assert "Merge enforcement authority" in text
    assert "required_approving_review_count=0" in text
    assert "no documentation or dashboard" in text


def test_every_branch_protection_mention_cross_references_the_authority_map():
    offenders = [
        path
        for path in _docs_mentioning_branch_protection()
        if not _has_authority_map_href(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these docs mention 'branch protection' without a Markdown link "
        f"whose href targets docs/{ANCHOR} to disclose it is not actually "
        f"enforced: {[str(p.relative_to(REPO_ROOT)) for p in offenders]}"
    )


def test_href_check_rejects_anchor_only_in_link_text():
    """Regression: the rejected PR #660 form passed a raw substring check.

    Putting the fragment in the link text and omitting it from the href
    lands the reader at the top of AUTHORITY_MAP.md — the opposite of a
    working cross-reference — and must fail this gate.
    """
    broken = (
        "See "
        "[`docs/AUTHORITY_MAP.md#merge-enforcement-authority-voyn-w0-aicc-branch-protection-limit`]"
        "(docs/AUTHORITY_MAP.md) for the evidence."
    )
    correct = (
        "See "
        "[`docs/AUTHORITY_MAP.md`]"
        "(docs/AUTHORITY_MAP.md#merge-enforcement-authority-voyn-w0-aicc-branch-protection-limit) "
        "for the evidence."
    )
    assert ANCHOR in broken  # substring present, but not in the href
    assert not _has_authority_map_href(broken)
    assert _has_authority_map_href(correct)
