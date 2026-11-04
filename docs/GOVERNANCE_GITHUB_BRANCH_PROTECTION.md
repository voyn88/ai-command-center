# DR-GITHUB-BRANCH-PROTECTION-001 — GitHub branch protection vs. the merge gateway

Date: 2026-11-01 (supersedes the 2026-09-02 draft rejected on PR #663)
Status: Decided (see Decision). Re-open if visibility, required contexts, or
plan/org settings change — re-run `scripts/verify-github-branch-protection.sh`
and attach a fresh evidence directory under `docs/audits/`.

Supersedes: the documentation-only claim set on
https://github.com/voyn88/ai-command-center/pull/663
(`6752f60637a7015c79d8f6ab43626f2c381ac93b`), which asserted that GitHub
branch protection enforced nothing. Adversarial review rejected that PR for
unverifiable high-stakes facts; this record answers that rejection with a
cited evidence trail and a corrected decision.

## Security severity (not a routine doc tweak)

`voyn88/ai-command-center` is a **public** automation repository that hosts
autonomous review/merge orchestration (`review_once` / `merge_once`). Public
visibility of that code and history is a security-relevant fact: anyone can
read workflow definitions, scripts, and historical commits. That exposure is
independent of whether branch protection is configured.

This decision record:

1. Treats the public-visibility finding as owner-visible security posture, not
   as a calm "not a blocker" footnote.
2. Does **not** silently claim GitHub enforces nothing when live evidence shows
   required status checks on `main`.
3. Does **not** assert authenticated-only fields
   (`required_approving_review_count`, `enforce_admins`, force-push/delete
   flags) without an authenticated capture.
4. Does **not** invite operators to run a stale remediation script that would
   overwrite the live required contexts.

Owner follow-ups that remain outside this documentation PR (billing/org and
host credentials, not inventable here):

- Confirm secret/token hygiene for a public automation repo (history scan,
  GitHub secret scanning alerts, rotation of any credential that ever touched
  a public tree).
- Optionally capture an **authenticated**
  `GET /repos/{owner}/{repo}/branches/main/protection` dump (requires `repo`
  admin scope) and append it to a new evidence directory if review-count /
  `enforce_admins` must be pinned as decided facts.
- Keep `scripts/enable-branch-protection.sh` aligned with the live required
  contexts before any re-apply (see below).

## Evidence trail (reproducible)

Captured at **2026-11-01T09:49:38Z** by unauthenticated GitHub REST against
`voyn88/ai-command-center`. Artifacts:

| File | Endpoint / content |
|---|---|
| [`docs/audits/github-branch-protection-2026-11-01/SUMMARY.json`](audits/github-branch-protection-2026-11-01/SUMMARY.json) | Roll-up used by this decision |
| [`…/01-repo-visibility.json`](audits/github-branch-protection-2026-11-01/01-repo-visibility.json) | `GET /repos/voyn88/ai-command-center` |
| [`…/02-branch-main-protection-public-subset.json`](audits/github-branch-protection-2026-11-01/02-branch-main-protection-public-subset.json) | `GET /repos/…/branches/main` |
| [`…/03-rules-branches-main.json`](audits/github-branch-protection-2026-11-01/03-rules-branches-main.json) | `GET /repos/…/rules/branches/main` |
| [`…/04-rulesets.json`](audits/github-branch-protection-2026-11-01/04-rulesets.json) | `GET /repos/…/rulesets` |

Integrity of the committed roll-up (byte-stable JSON, `sort_keys=True`, trailing
newline):

- `SUMMARY.json` sha256: `0d618fed812b4c4c1b446f89f93c16160e546d69c75c83d0d88bd9827d81269a`
- `SUMMARY.json` byte length: `1549`

Re-verify at any time (no admin token required for the public subset):

```bash
bash scripts/verify-github-branch-protection.sh \
  --out /tmp/bp-verify-$(date -u +%Y%m%dT%H%M%SZ)
```

The script exits non-zero if visibility is not public, if `main` is not
protected, or if the required contexts drift from the expected pair below.

### Verified facts (public API)

- Repository **visibility = public** (`private: false`). The original audit's
  "private/free-tier, protection unavailable" premise was wrong.
- Branch `main` reports `protected: true` and `protection.enabled: true`.
- Required status checks (enforcement_level `everyone`):
  - `Final merge gate`
  - `Acceptance gate (independent verdict on exact SHA)`
- These contexts match `_DEFAULT_REQUIRED_MERGE_CHECKS` in
  `command_center/orchestrator/review_merge.py` and the check-run names emitted
  by `.github/workflows/ci.yml` / `.github/workflows/acceptance-gate.yml`.
- No repository ruleset currently targets branch `main` (only an active
  `release-tag-protection` tag ruleset was listed). Classic branch protection
  is what carries the required checks above.

### Explicitly unverified without admin auth

Unauthenticated `GET …/branches/main` does **not** return
`required_pull_request_reviews`, `enforce_admins`, `allow_force_pushes`, or
`allow_deletions`. This record therefore **does not** claim
`required_approving_review_count=0` or `enforce_admins=false`. Those were the
unverified assertions that sank PR #663.

Design note (code, not live settings): `scripts/assert_independent_acceptance.py`
documents why classic `required_approving_review_count` is a poor fit for the
acceptance bot (approvals from an app with `contents: read` are not counted by
GitHub). Independence is enforced via the **Acceptance gate** check-run, which
*is* in the required-check list above.

## Decision

Adopt a **documented dual-layer model**, not "gateway is the sole enforcement"
and not "GitHub protects nothing":

1. **GitHub-native layer (verified):** branch protection on `main` requires
   `Final merge gate` and `Acceptance gate (independent verdict on exact SHA)`
   for everyone. Merges that lack those green checks are blocked by GitHub
   itself for identities subject to that rule.
2. **Application layer (code):** `merge_once` in
   `command_center/orchestrator/review_merge.py` is the autonomous merge
   gateway. It re-checks the same required contexts (plus acceptance markers,
   privileged merge identity, duplicate-merge guards, etc.) before calling
   `gh pr merge`. It is **not** a substitute for missing GitHub protection; it
   is the orchestrated consumer of those checks and the place where
   product-specific merge policy lives.
3. **Documentation must not** imply that required-check enforcement is absent,
   is blocked by a private/free plan, or is "only an operator discipline."
   `CURRENT_STATE.md` / `README.md` / `ARCHITECTURE.md` / `docs/AUTHORITY_MAP.md`
   are updated accordingly.
4. **Remediation script discipline:** `scripts/enable-branch-protection.sh`
   exists in-tree and must apply the **same** required contexts as
   `merge_once` / the live settings. The pre-remediation script body pointed at
   a stale `Quality gates (whitespace · Ruff · compile · pytest)` context and
   must not be run against production as previously written — doing so would
   *weaken* live protection by replacing the real required checks. Re-apply
   only after `verify-github-branch-protection.sh` and only with repo admin
   credentials.
5. **Owner acceptance of residual risk:** public visibility plus any
   still-unverified classic-protection fields (review count / `enforce_admins`)
   remain owner-owned residual risk. Closing them is an authenticated
   operator action (evidence dump and/or settings change), not a silent
   documentation claim.

## Why not the original "gateway-only" or "plan upgrade" paths

- **Plan upgrade:** moot for enabling required status checks on this public
  repository — checks are already configured. Org/plan changes may still matter
  for other GitHub products, but they are not the blocker described in the
  original P0-6 write-up.
- **Gateway-only:** false against the 2026-11-01 evidence. Declaring
  `merge_once` the sole enforcement point would recreate the exact failure mode
  this task exists to prevent: documentation that pretends a control exists (or
  does not exist) differently from the live system.

## Rejected alternatives

- Re-landing PR #663's text unchanged (unverifiable claims; severity mismatch;
  stale script pointer).
- Running `enable-branch-protection.sh` from this agent session (no push/admin
  capability here; the pre-fix script was also context-stale and unsafe).
- Asserting `required_approving_review_count` / `enforce_admins` from the
  public subset.

## Documentation updated as a result

- `DECISIONS.md` — indexes this DR.
- `CURRENT_STATE.md` — removes the false "private-repository plan does not
  expose branch protection" claim.
- `README.md` / `ARCHITECTURE.md` — point at the dual-layer model and the
  verify script instead of "enable Quality gates to make CI a real gate."
- `docs/AUTHORITY_MAP.md` — records merge-enforcement authority explicitly so
  the map no longer implicitly relies on an unstated GitHub setting.
- `scripts/verify-github-branch-protection.sh` — reproducible public check.
- `scripts/enable-branch-protection.sh` — aligned to the live required
  contexts; refuses to apply a divergent set without an explicit override.
