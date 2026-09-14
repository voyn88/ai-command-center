"""`DR-GITHUB-TIER-ENFORCEMENT-001`: branch protection is claimed only from the API.

Three things are held here:

* `scripts/verify_branch_protection.py` reads the protection endpoint's real
  shapes correctly — including the exact payload the decision record measured
  on `main` (no required checks, 0 required reviews, `enforce_admins` false),
  which must come back as "enforces nothing";
* `scripts/enable-branch-protection.sh` cannot go back to announcing success on
  the strength of its own write — it must read the setting back;
* no Markdown in this repository describes branch protection as a working
  control without either qualifying the claim or pointing at the decision
  record, which is the second half of the task's acceptance criterion.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify_branch_protection.py"


def _load_verifier():
    """Import the tool by path rather than putting `scripts/` on `sys.path`.

    Same reasoning as `tests/test_delivery_tooling.py`: it is a script, not a
    package, and making it importable by side effect would change the thing
    under test — and would put every other file in `scripts/` on the import
    path of the whole session.
    """
    spec = importlib.util.spec_from_file_location("verify_branch_protection_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because `from __future__ import annotations`
    # makes the module's dataclass annotations strings, and `dataclasses`
    # resolves them through `sys.modules[cls.__module__]` while building the
    # class — an unregistered module fails there, at import time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()
ENABLE_SCRIPT = ROOT / "scripts" / "enable-branch-protection.sh"
DECISION_RECORD = ROOT / "docs" / "GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md"

CHECK = "Quality gates (whitespace · Ruff · compile · pytest)"

#: The state DR-GITHUB-TIER-ENFORCEMENT-001 measured on `main` (2026-08-26),
#: in the endpoint's own nested shape.
MEASURED_ON_MAIN = {
    "url": "https://api.github.com/repos/voyn88/ai-command-center/branches/main/protection",
    "enforce_admins": {"enabled": False},
    "required_pull_request_reviews": {"required_approving_review_count": 0},
    "allow_force_pushes": {"enabled": True},
    "allow_deletions": {"enabled": False},
}

ENFORCING = {
    "url": "https://api.github.com/repos/voyn88/ai-command-center/branches/main/protection",
    "required_status_checks": {"strict": False, "checks": [{"context": CHECK}]},
    "enforce_admins": {"enabled": True},
    "required_pull_request_reviews": {"required_approving_review_count": 1},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
}


def _run(payload: object, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "-", *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------
# Reading the API's answer
# --------------------------------------------------------------------------


def test_the_state_measured_on_main_enforces_nothing():
    """The finding the decision record is built on, re-derived from the payload."""
    protection = verifier.parse_protection(MEASURED_ON_MAIN)

    assert protection.protected is True
    assert protection.required_checks == ()
    assert protection.required_reviews == 0
    assert protection.enforce_admins is False
    assert protection.enforces_nothing is True


def test_an_enforcing_branch_is_recognized_as_enforcing():
    protection = verifier.parse_protection(ENFORCING)

    assert protection.required_checks == (CHECK,)
    assert protection.required_reviews == 1
    assert protection.enforce_admins is True
    assert protection.enforces_nothing is False


def test_flags_are_read_from_both_the_nested_and_flattened_shapes():
    """`{"enabled": true}` (REST) and a bare `true` (`gh api -q`, fixtures) agree.

    Reading only the nested form would report a real `enforce_admins` gate as
    missing whenever the payload had been flattened on its way here.
    """
    nested = verifier.parse_protection(
        {"enforce_admins": {"enabled": True}, "allow_deletions": {"enabled": False}}
    )
    flat = verifier.parse_protection({"enforce_admins": True, "allow_deletions": False})

    assert nested.enforce_admins is flat.enforce_admins is True
    assert nested.allow_deletions is flat.allow_deletions is False


def test_required_checks_are_read_from_the_legacy_contexts_field_too():
    protection = verifier.parse_protection(
        {"required_status_checks": {"strict": True, "contexts": [CHECK]}}
    )

    assert protection.required_checks == (CHECK,)
    assert protection.strict_checks is True


def test_a_check_named_in_both_fields_is_not_double_counted():
    protection = verifier.parse_protection(
        {
            "required_status_checks": {
                "contexts": [CHECK],
                "checks": [{"context": CHECK}, {"context": "other"}],
            }
        }
    )

    assert protection.required_checks == (CHECK, "other")


def test_absent_force_push_and_deletion_flags_are_read_as_permitted():
    """Silence about force-pushes is not a denial of them."""
    protection = verifier.parse_protection({"enforce_admins": {"enabled": True}})

    assert protection.allow_force_pushes is True
    assert protection.allow_deletions is True


def test_an_unreadable_review_count_counts_as_no_reviews():
    protection = verifier.parse_protection(
        {"required_pull_request_reviews": {"required_approving_review_count": "1"}}
    )

    assert protection.required_reviews == 0


@pytest.mark.parametrize(
    "message",
    [
        "Branch not protected",
        "Upgrade to GitHub Pro or make this repository public to enable this feature.",
    ],
)
def test_an_api_error_body_is_unprotected_and_not_a_protection_object(message):
    """`{"message": …}` is JSON and would otherwise parse as "every gate off".

    It must instead be reported as *unprotected*, carrying GitHub's own words —
    a plan that cannot protect and a branch nobody protected are the same fact
    for a merge, and which one it is belongs in the record, not in a guess.
    """
    protection = verifier.parse_protection({"message": message, "documentation_url": "…"})

    assert protection.protected is False
    assert protection.reason == message
    assert protection.enforces_nothing is True


def test_an_unrecognized_payload_is_unreadable_rather_than_unprotected():
    with pytest.raises(verifier.ProtectionUnreadable):
        verifier.parse_protection({"unrelated": "payload"})
    with pytest.raises(verifier.ProtectionUnreadable):
        verifier.parse_protection([])


# --------------------------------------------------------------------------
# The verdict, and the three exit codes
# --------------------------------------------------------------------------


def test_cli_refuses_the_state_measured_on_main_and_names_the_record():
    result = _run(MEASURED_ON_MAIN)

    assert result.returncode == 1
    assert "NOT ENFORCED" in result.stderr
    assert "enforces no merge gate" in result.stderr
    assert "GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md" in result.stderr


def test_cli_accepts_a_branch_that_meets_every_stated_requirement():
    result = _run(
        ENFORCING,
        "--require-check",
        CHECK,
        "--require-admins",
        "--require-no-force-push",
        "--require-no-deletions",
    )

    assert result.returncode == 0, result.stderr
    assert "ENFORCED" in result.stdout


def test_cli_reports_each_unmet_requirement_separately():
    """A reader must see *which* claim failed, not just that something did."""
    result = _run(
        MEASURED_ON_MAIN,
        "--require-check",
        CHECK,
        "--require-reviews",
        "1",
        "--require-admins",
        "--require-no-force-push",
    )

    assert result.returncode == 1
    assert f"required status check {CHECK!r} is not required" in result.stderr
    assert "required_approving_review_count is 0" in result.stderr
    assert "enforce_admins is false" in result.stderr
    assert "force pushes are allowed" in result.stderr


def test_a_branch_that_only_denies_deletion_still_gates_no_merge():
    """History rules are not a merge gate; the record's gap is about merging."""
    result = _run({"url": "…", "allow_deletions": {"enabled": False}})

    assert result.returncode == 1
    assert "enforces no merge gate" in result.stderr


def test_required_reviews_alone_satisfy_the_default_question():
    result = _run(
        {"url": "…", "required_pull_request_reviews": {"required_approving_review_count": 1}}
    )

    assert result.returncode == 0, result.stderr


def test_an_unreadable_answer_exits_2_and_is_not_a_verdict():
    """Unreadable is not "nothing is enforced" — and neither is a verdict."""
    not_json = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "-"],
        input="not json at all",
        capture_output=True,
        text=True,
        check=False,
    )

    assert not_json.returncode == 2
    assert "UNVERIFIED" in not_json.stderr
    assert "NOT ENFORCED" not in not_json.stderr


def test_a_missing_token_is_unverified_rather_than_unprotected(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with pytest.raises(verifier.ProtectionUnreadable, match="GITHUB_TOKEN"):
        verifier.fetch_protection("voyn88/ai-command-center", "main", {})


def test_the_observed_state_is_printed_as_quotable_facts():
    result = _run(MEASURED_ON_MAIN)

    assert "required_approving_review_count:  0" in result.stdout
    assert "enforce_admins:                   false" in result.stdout
    assert "required status checks:           none" in result.stdout


# --------------------------------------------------------------------------
# The two claim-side guards
# --------------------------------------------------------------------------


def test_enable_script_reads_the_setting_back_before_claiming_success():
    """It may not announce protection on the strength of its own write again.

    The regression this pins: the script used to `gh api -X PUT …` and then
    unconditionally echo "Branch 'main' is now protected." — which is how a
    branch enforcing nothing gets described as protected in an audit.
    """
    body = ENABLE_SCRIPT.read_text(encoding="utf-8")

    assert "verify_branch_protection.py" in body, (
        "the enable script must verify protection through the API verifier"
    )
    # Every success announcement must sit inside the verifier's success branch.
    success_lines = [
        line for line in body.splitlines() if "✓" in line and not line.lstrip().startswith("#")
    ]
    assert success_lines, "the script should still confirm success when it is real"
    verify_at = body.index("python3 \"${VERIFIER}\"")
    for line in success_lines:
        assert body.index(line) > verify_at, (
            f"success is announced before the API read-back: {line.strip()!r}"
        )


#: A mention of the control. Deliberately narrow: the noun phrase itself, not
#: every appearance of the word "protection".
_MENTION = re.compile(r"branch[ _-]?protection|protected branch", re.I)

#: Phrasing that asserts the control is in force *now*. A mention alone is not
#: a claim — the decision record, the CI comments and the limitations sections
#: all mention it truthfully — so the guard fires on the assertion, not the word.
_ASSERTION = re.compile(
    r"\b(?:is|are|was|were)\s+(?:currently\s+)?(?:enabled|configured|active|set\s+up|in\s+force|on)\b"
    r"|\benforc(?:es|ed|ing)\b"
    r"|\bgated\s+by\b|\bprotected\s+by\b|\bguarded\s+by\b|\bbacked\s+by\b"
    r"|\brequires\b|\bblocks\b|\bprevents\b|\bguarantees\b|\bensures\b",
    re.I,
)

#: What turns an assertion back into an honest sentence: a negation, a
#: conditional/future framing, or a pointer to the record or the verifier —
#: which is the re-check the acceptance criterion asks for.
_DISCLAIMED = re.compile(
    r"\bnot\b|\bno\b|\bnothing\b|\bnone\b|\bnever\b|\bcannot\b|\bcan't\b|\bwithout\b"
    r"|\bwould\b|\bcould\b|\bshould\b|\bif\b|\bonce\b|\buntil\b|\bwhen\b|\bpending\b"
    r"|\bunverified\b|\bmust\b|\bcan\b"
    r"|DR-GITHUB-TIER-ENFORCEMENT-001|GITHUB_TIER_ENFORCEMENT_GAP_DECISION"
    r"|verify_branch_protection|enable-branch-protection",
    re.I,
)

#: Sentence-ish split. Markdown bullets and line breaks end a thought as surely
#: as a full stop does, so a negation two bullets away cannot launder a claim.
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n")


def unqualified_claims(text: str) -> list[str]:
    """Sentences asserting branch protection is a control, with nothing to qualify them."""
    found = []
    for sentence in _SENTENCE.split(text):
        flat = " ".join(sentence.split())
        if not flat or not _MENTION.search(flat):
            continue
        if not _ASSERTION.search(flat):
            continue
        if _DISCLAIMED.search(flat):
            continue
        found.append(flat)
    return found


def _markdown_files() -> list[Path]:
    skip = {".git", "node_modules", ".venv", "venv", "archive", "generated"}
    return [
        path
        for path in ROOT.rglob("*.md")
        if not skip & set(path.relative_to(ROOT).parts)
        and path.resolve() != DECISION_RECORD.resolve()
    ]


def test_the_claim_detector_actually_catches_the_sentences_it_is_for():
    """Positive control: a guard that flags nothing would pass silently forever."""
    assert unqualified_claims("Merges to `main` are gated by branch protection.")
    assert unqualified_claims("Branch protection is enabled on main.")
    assert unqualified_claims("Branch protection enforces the Quality gates check.")
    assert unqualified_claims("A protected branch blocks direct pushes here.")

    # …and leaves the honest forms alone, including every shape already in the
    # repository's own documents.
    assert not unqualified_claims("The workflow does not configure GitHub branch protection.")
    assert not unqualified_claims(
        "The current private-repository plan does not expose branch protection/rulesets."
    )
    assert not unqualified_claims(
        "After this workflow has appeared green on an exact PR head, branch protection can bind "
        "its stable job name."
    )
    assert not unqualified_claims(
        "Branch protection enforces nothing here — see DR-GITHUB-TIER-ENFORCEMENT-001."
    )


def test_no_document_describes_branch_protection_as_a_working_control():
    """The task's second acceptance clause, kept true by a test rather than by care.

    On this repository's plan the API says the control does not exist, so a
    document asserting it does is wrong until somebody re-checks. Adding a
    pointer to DR-GITHUB-TIER-ENFORCEMENT-001 or to
    `scripts/verify_branch_protection.py` is the way past this guard — that is,
    the re-verification the record demands.
    """
    offenders = [
        f"{path.relative_to(ROOT)}: {claim[:160]}"
        for path in _markdown_files()
        for claim in unqualified_claims(path.read_text(encoding="utf-8", errors="replace"))
    ]

    assert not offenders, (
        "branch protection is described as a working control. Re-check it with "
        "`python3 scripts/verify_branch_protection.py --repo <owner/name>` against the live "
        "API before claiming it, and cite DR-GITHUB-TIER-ENFORCEMENT-001:\n  "
        + "\n  ".join(offenders)
    )


def test_the_decision_record_is_registered_in_the_decision_log():
    log = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")

    assert "DR-GITHUB-TIER-ENFORCEMENT-001" in log
    assert "docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md" in log
