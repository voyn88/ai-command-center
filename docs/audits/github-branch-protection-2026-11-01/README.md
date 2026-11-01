# Evidence: GitHub branch protection (2026-11-01)

Unauthenticated GitHub REST captures supporting
[DR-GITHUB-BRANCH-PROTECTION-001](../../GOVERNANCE_GITHUB_BRANCH_PROTECTION.md).

Re-run:

```bash
bash scripts/verify-github-branch-protection.sh --out /tmp/bp-verify-$(date -u +%Y%m%dT%H%M%SZ)
```

Do not edit `SUMMARY.json` by hand; replace the whole directory with a fresh
capture if the live settings change.
