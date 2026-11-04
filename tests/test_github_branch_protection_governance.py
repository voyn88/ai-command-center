"""Lock DR-GITHUB-BRANCH-PROTECTION-001 scripts to the live required contexts.

PR #663 was rejected for asserting unverifiable GitHub protection facts and for
pointing at a remediation script whose required-check context did not match the
autonomous merge gateway. These tests keep:

* ``scripts/verify-github-branch-protection.sh`` expected contexts
* ``scripts/enable-branch-protection.sh`` apply contexts
* ``review_merge._DEFAULT_REQUIRED_MERGE_CHECKS``

byte-equal, and keep the cited evidence roll-up parseable with the contexts the
decision claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from command_center.orchestrator.review_merge import _DEFAULT_REQUIRED_MERGE_CHECKS

ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "scripts/verify-github-branch-protection.sh"
ENABLE_SCRIPT = ROOT / "scripts/enable-branch-protection.sh"
GOVERNANCE = ROOT / "docs/GOVERNANCE_GITHUB_BRANCH_PROTECTION.md"
EVIDENCE_SUMMARY = (
    ROOT / "docs/audits/github-branch-protection-2026-11-01/SUMMARY.json"
)


def _bash_string_array(script: Path, array_name: str) -> tuple[str, ...]:
    """Extract a simple Bash string-array literal assigned to ``array_name``."""
    text = script.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(array_name)}=\(\s*(.*?)\s*\)$",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, f"{array_name} array not found in {script}"
    values = re.findall(r'"([^"]*)"', match.group(1))
    assert values, f"{array_name} in {script} has no quoted entries"
    return tuple(values)


def test_verify_and_enable_scripts_share_merge_once_required_contexts() -> None:
    expected = tuple(_DEFAULT_REQUIRED_MERGE_CHECKS)
    assert _bash_string_array(VERIFY_SCRIPT, "EXPECTED_CONTEXTS") == expected
    assert _bash_string_array(ENABLE_SCRIPT, "CONTEXTS") == expected


def test_enable_script_refuses_mutation_without_explicit_confirm() -> None:
    text = ENABLE_SCRIPT.read_text(encoding="utf-8")
    assert "--i-understand-this-mutates-github-settings" in text
    assert "Quality gates (whitespace · Ruff · compile · pytest)" not in text


def test_governance_record_cites_committed_evidence_rollup() -> None:
    raw = EVIDENCE_SUMMARY.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    summary = json.loads(raw)
    gov = GOVERNANCE.read_text(encoding="utf-8")

    assert digest in gov
    assert str(len(raw)) in gov
    assert summary["visibility"]["private"] is False
    assert summary["visibility"]["visibility"] == "public"
    contexts = summary["branch_main_public_protection_subset"]["protection"][
        "required_status_checks"
    ]["contexts"]
    assert tuple(contexts) == tuple(_DEFAULT_REQUIRED_MERGE_CHECKS)
    assert "DR-GITHUB-BRANCH-PROTECTION-001" in gov
    assert "scripts/verify-github-branch-protection.sh" in gov
