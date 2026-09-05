#!/usr/bin/env bash
# Enable branch protection on `main` requiring the CI status checks that gate
# merges (VOYN-W0-AICC-MERGE-QUEUE-ENABLE), plus standard rules (no
# force-push, no delete). `strict: true` because a GitHub merge queue is
# enabled on `main` and the queue needs branches to be caught up before it
# will build a candidate group.
#
# `required_pull_request_reviews` is intentionally left unset: authors and
# the independent-review agents run under the same account, so a
# GitHub-approval requirement is structurally unsatisfiable here. Acceptance
# instead comes from the independent-review marker keyed to the exact head
# SHA (`Acceptance gate (independent verdict on exact SHA)` below).
#
# This script does not enable the merge queue itself — that toggle
# (Settings → Branches → Require merge queue) is a repository-owner-only
# action outside the `branches/.../protection` endpoint this script calls.
#
# Requires: gh CLI authenticated with admin:repo_hook / repo scope.
# Usage:   bash scripts/enable-branch-protection.sh
set -euo pipefail

BRANCH="main"
CONTEXTS='["Final merge gate", "Acceptance gate (independent verdict on exact SHA)"]'

# `gh api` targets the current repo by default (from git remote).
echo "→ Enabling branch protection on '$BRANCH' for $(gh repo view --json nameWithOwner -q .nameWithOwner)"

gh api -X PUT "repos/{owner}/{repo}/branches/${BRANCH}/protection" \
  --input - <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": ${CONTEXTS}
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
echo "  - Required checks: ${CONTEXTS}"
echo "  - Strict (branch must be up to date): yes"
echo "  - Force-push: denied"
echo "  - Deletion: denied"
echo "  - Admins enforced: yes"
echo "  - Note: enabling the merge queue itself still requires a manual repository-owner step."