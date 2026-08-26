#!/usr/bin/env bash
# Enable branch protection on `main` requiring the two aggregator status checks
# (Final merge gate, Acceptance gate) before merging, plus standard rules (no
# force-push, no delete). Idempotent: re-running re-applies the same state.
#
# `strict: true` ties the check to the merge queue: a queued branch must be
# current with `main`, which is what makes the queue's "test the prospective
# merged result" behavior meaningful. Approvals are 0 on purpose — every agent
# shares the account that opens the pull request, so GitHub self-approval is
# structurally impossible here, and the acceptance-gate verdict is the real
# gate instead (see acceptance-gate.yml).
#
# This endpoint does not cover the merge queue itself (queue enable/build
# strategy/batch size); that is a separate repository setting under
# Settings -> Branches, applied once by the repo owner.
#
# Requires: gh CLI authenticated with admin:repo_hook / repo scope.
# Usage:   bash scripts/enable-branch-protection.sh
set -euo pipefail

BRANCH="main"
CONTEXT_1="Final merge gate"
CONTEXT_2="Acceptance gate (independent verdict on exact SHA)"

# `gh api` targets the current repo by default (from git remote).
echo "→ Enabling branch protection on '$BRANCH' for $(gh repo view --json nameWithOwner -q .nameWithOwner)"

gh api -X PUT "repos/{owner}/{repo}/branches/${BRANCH}/protection" \
  --input - <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["${CONTEXT_1}", "${CONTEXT_2}"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false
}
JSON

echo "✓ Branch '$BRANCH' is now protected."
echo "  - Required checks: ${CONTEXT_1}; ${CONTEXT_2}"
echo "  - Strict (must be up to date with main): yes"
echo "  - Required approvals: 0 (acceptance-gate verdict is the real gate)"
echo "  - Force-push: denied"
echo "  - Deletion: denied"
echo "  - Admins enforced: yes"
echo "  - Merge queue (enable, batch size, build strategy) is NOT set by this"
echo "    script — configure it once in Settings -> Branches."