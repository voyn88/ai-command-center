#!/usr/bin/env bash
# Enable branch protection on `main` requiring the "Final merge gate" status
# check before merging, plus standard rules (no force-push, no delete).
#
# The required context must be the `final-gate` job's own name
# (`tests/test_release_gate_policy.py::test_release_context_names_and_workflow_coverage_are_exact`
# pins it), not one of the jobs it aggregates -- `final-gate` is `if: always()`
# and fails closed if any upstream gate (including `security-gates`, which
# runs the fork-secret guard's regression tests) did not succeed.
#
# Do not enable the merge queue on top of this protection until
# `docs/adr/0011-merge-queue-fork-secret-policy.md` (VOYN-W0-AICC-MERGE-QUEUE-FORK-POLICY)
# has been read: the queue is a base-repository event that hands repository
# secrets to fork-authored code unless every secret-bearing step first passes
# `scripts/assert_trusted_head_repository.py`. That guard is already merged
# and pinned by `tests/test_release_gate_policy.py`; the ADR is the record
# that the fork policy question has actually been decided, not just coded.
#
# Requires: gh CLI authenticated with admin:repo_hook / repo scope.
# Usage:   bash scripts/enable-branch-protection.sh
set -euo pipefail

BRANCH="main"
CONTEXT="Final merge gate"

# `gh api` targets the current repo by default (from git remote).
echo "→ Enabling branch protection on '$BRANCH' for $(gh repo view --json nameWithOwner -q .nameWithOwner)"

gh api -X PUT "repos/{owner}/{repo}/branches/${BRANCH}/protection" \
  --input - <<JSON
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

echo "✓ Branch '$BRANCH' is now protected."
echo "  - Required check: ${CONTEXT}"
echo "  - Force-push: denied"
echo "  - Deletion: denied"
echo "  - Admins enforced: yes"