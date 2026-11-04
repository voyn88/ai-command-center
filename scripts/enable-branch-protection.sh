#!/usr/bin/env bash
# Apply (or re-apply) classic branch protection on `main` with the required
# status checks that match the autonomous merge gateway.
#
# Required contexts (must stay locked to
# command_center.orchestrator.review_merge._DEFAULT_REQUIRED_MERGE_CHECKS):
#   - Final merge gate
#   - Acceptance gate (independent verdict on exact SHA)
#
# SAFETY
# ------
# A prior revision of this script required a stale CI job label (the old
# whitespace/Ruff/compile/pytest aggregate name). Running that body against
# today's live settings would *replace* the real required checks and weaken
# GitHub-native enforcement. This script:
#   1. Prefers `scripts/verify-github-branch-protection.sh` first.
#   2. Applies only the expected contexts above.
#   3. Refuses to run unless `--i-understand-this-mutates-github-settings` is
#      passed (prevents casual/agent invocation from docs copy-paste).
#
# Requires: gh CLI authenticated with admin scope on the target repo.
# Usage:
#   bash scripts/enable-branch-protection.sh --i-understand-this-mutates-github-settings
set -euo pipefail

BRANCH="main"
CONTEXTS=(
  "Final merge gate"
  "Acceptance gate (independent verdict on exact SHA)"
)
CONFIRM_FLAG="--i-understand-this-mutates-github-settings"
SKIP_VERIFY=0
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    "$CONFIRM_FLAG") APPLY=1; shift ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      echo "refusing to mutate GitHub settings without ${CONFIRM_FLAG}" >&2
      exit 2
      ;;
  esac
done

if [[ "$APPLY" -ne 1 ]]; then
  echo "refusing to mutate GitHub settings without ${CONFIRM_FLAG}" >&2
  echo "tip: re-verify first with: bash scripts/verify-github-branch-protection.sh" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$SKIP_VERIFY" -eq 0 ]]; then
  echo "→ verifying live public protection signals before apply"
  bash "$ROOT/scripts/verify-github-branch-protection.sh"
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required" >&2
  exit 1
fi

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "→ Enabling branch protection on '${BRANCH}' for ${REPO}"
echo "  required contexts:"
for ctx in "${CONTEXTS[@]}"; do
  echo "    - ${ctx}"
done

# Build the contexts JSON array without requiring jq.
contexts_json="["
first=1
for ctx in "${CONTEXTS[@]}"; do
  if [[ $first -eq 1 ]]; then first=0; else contexts_json+=","; fi
  # Escape backslashes and quotes for JSON string safety.
  esc="${ctx//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  contexts_json+="\"${esc}\""
done
contexts_json+="]"

gh api -X PUT "repos/{owner}/{repo}/branches/${BRANCH}/protection" \
  --input - <<JSON
{
  "required_status_checks": {
    "strict": false,
    "contexts": ${contexts_json}
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false
}
JSON

echo "✓ Branch '${BRANCH}' protection applied."
echo "  - Required checks: ${CONTEXTS[*]}"
echo "  - Force-push: denied"
echo "  - Deletion: denied"
echo "  - Admins enforced: yes"
echo "  Re-check: bash scripts/verify-github-branch-protection.sh"
