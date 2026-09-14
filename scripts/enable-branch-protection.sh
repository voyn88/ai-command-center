#!/usr/bin/env bash
# Ask GitHub to protect `main` with the "Quality gates" required check (plus no
# force-push, no delete, admins included), then READ THE SETTING BACK and report
# only what the API actually confirms.
#
# The read-back is the point. `DR-GITHUB-TIER-ENFORCEMENT-001`
# (docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md) found this repository's `main`
# enforcing nothing — no required checks, 0 required reviews, enforce_admins
# false — while this script had been printing "Branch 'main' is now protected."
# on the strength of its own write. A write that the plan or the endpoint did
# not honour is exactly the case where a checkmark does the most damage, so the
# verdict here comes from `scripts/verify_branch_protection.py` reading the
# protection endpoint, never from the fact that the PUT returned.
#
# Requires: gh CLI authenticated with repo admin scope, python3.
# Usage:   bash scripts/enable-branch-protection.sh
set -euo pipefail

BRANCH="main"
CONTEXT="Quality gates (whitespace · Ruff · compile · pytest)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFIER="${HERE}/verify_branch_protection.py"

# `gh api` targets the current repo by default (from git remote).
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "→ Requesting branch protection on '$BRANCH' for ${REPO}"

# A refused write is reported, not fatal on its own: the read-back below is the
# authority either way, and on a plan without branch protection this PUT is the
# call that 403s. Its message is worth showing before the verdict.
if ! gh api -X PUT "repos/{owner}/{repo}/branches/${BRANCH}/protection" --input - <<JSON
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["${CONTEXT}"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false
}
JSON
then
  echo "  (the protection write was refused — verifying what is actually in force)" >&2
fi

echo "→ Reading the setting back from the API"
# On 403/404 `gh api` prints GitHub's error body to stdout and exits non-zero;
# `|| true` keeps that body, because "Upgrade to GitHub Pro …" / "Branch not
# protected" is the answer, not a failure to get one. The verifier quotes it.
PROTECTION="$(gh api "repos/{owner}/{repo}/branches/${BRANCH}/protection" 2>/dev/null || true)"
if [ -z "${PROTECTION}" ]; then
  echo "✗ Could not read branch protection for '${BRANCH}'. State UNVERIFIED — do not" >&2
  echo "  describe branch protection as a working control on this evidence." >&2
  exit 2
fi

# The claims this script would otherwise have printed, now checked one by one.
if printf '%s' "${PROTECTION}" | python3 "${VERIFIER}" \
    --json - \
    --require-check "${CONTEXT}" \
    --require-admins \
    --require-no-force-push \
    --require-no-deletions
then
  echo ""
  echo "✓ Branch '${BRANCH}' is protected, confirmed by reading the API back."
  exit 0
fi

cat >&2 <<'MSG'

✗ Branch protection is NOT in force as requested (details above).
  This is the state DR-GITHUB-TIER-ENFORCEMENT-001 describes: the application-level
  `merge_once` gate remains the only thing gating a merge to `main`. Record the
  founder's Option A / Option B choice in docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md
  rather than re-running this script and hoping.
MSG
exit 1
