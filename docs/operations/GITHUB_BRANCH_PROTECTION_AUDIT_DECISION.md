# GitHub branch protection on `main`: audit scope and owner decision

Tracks `VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP-REM-REM`, superseding the rejected
`VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP` (PR #450) and
`VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP-REM` (PR #482) records. Both were rejected for drawing
a broader conclusion than the queried evidence supported. This record narrows the claim to what
was actually checked, lists what was not, and records the owner decision that follows from that
narrower claim.

## What has been confirmed

`gh api repos/voyn88/ai-command-center/branches/main/protection` returned, among other fields,
these three:

- `required_approving_review_count = 0`
- `required_status_checks` — absent from the response
- `enforce_admins = false`

This supports exactly one conclusion: **on classic branch protection for `main`, the
pull-request-review-count requirement, the required-status-checks requirement, and admin
enforcement are all inactive.** It does not support any claim about whether GitHub blocks or
permits any specific merge, because classic branch protection is not the only GitHub-side
mechanism that can constrain merges (see below).

## What has not been confirmed

The two prior attempts were each rejected for treating the three fields above as if they
described the complete protection surface. They do not. Left unqueried:

- The remaining fields of the same classic-protection response: `required_pull_request_reviews`
  sub-settings (`dismiss_stale_reviews`, `require_code_owner_reviews`, `require_last_push_approval`),
  `restrictions` (push/actor restrictions), `allow_force_pushes`, `allow_deletions`,
  `required_linear_history`, `required_conversation_resolution`, `required_signatures`,
  `block_creations`, `lock_branch`, `allow_fork_syncing`.
- Repository rulesets — `GET /repos/{owner}/{repo}/rulesets` — a separate, newer enforcement
  mechanism that can require reviews/checks/signed commits independently of, and even where,
  classic branch protection is empty.
- The effective merged view of every applicable control —
  `GET /repos/{owner}/{repo}/rules/branches/main` — which combines classic protection, repository
  rulesets, and any organization-level default rulesets that apply regardless of this repository's
  own settings.
- Merge queue configuration and required-deployment/environment protection rules, either of which
  could also gate a merge to `main`.

Because these are unqueried, this record does not claim "GitHub enforces nothing on `main`" and
does not claim application-level `merge_once` is the *only* gate — only that the three classic
fields above are confirmed inactive. Any document that goes further than that needs its own
verified evidence, not an extrapolation from this one.

**This delivery sandbox has outbound network access but no GitHub credentials** (no `gh auth`
session, no `GITHUB_TOKEN` in the environment), so it cannot run the queries above itself. The
follow-up audit below has to be run by a session with real API access against the live
repository — the orchestrator/publisher lane, or the owner directly.

## Follow-up audit (required before this is closed)

```
gh api repos/voyn88/ai-command-center/branches/main/protection
gh api repos/voyn88/ai-command-center/rulesets
gh api repos/voyn88/ai-command-center/rules/branches/main
```

Record the full JSON of all three responses (not a field subset) as an update to this document,
then resolve based on `rules/branches/main`, the effective merged view:

- If it lists an active `pull_request` (review-count) rule or a `required_status_checks` rule from
  *any* source (classic protection, a repo ruleset, or an org default ruleset), branch protection
  is a real, working control. Update this document, drop the parallelism constraint below, and
  correct any architecture doc that still hedges on this.
- If it lists nothing, `scripts/enable-branch-protection.sh` (classic protection) or an equivalent
  ruleset is genuinely absent, and the plan/org question below has to be answered before either
  can be turned on.

## Owner decision (recorded here, in force until the follow-up audit closes)

The original task framed two options: change the GitHub plan/org to get real ruleset enforcement,
or accept the current state as risk with a parallelism cap. Committing to the plan-change option
now would be premature — the ruleset-availability question above hasn't been checked, and a plan
change might not be needed at all. Committing to "no protection exists" would repeat the mistake
that got PR #450 and #482 rejected.

The decision made here is the accepted-risk option, scoped to what's actually known:

- Until the follow-up audit above is run and recorded, treat application-level `merge_once` as the
  only *confirmed* gate on `main` — not the only gate that could exist, but the only one this
  record has evidence for.
- Do not increase the number of concurrent autonomous publishing agents beyond the current count
  until either (a) the follow-up audit confirms an independent, working GitHub-side control, or
  (b) `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships.
- No document or audit written after this record may describe GitHub branch protection as a
  working control on `main` without first reading `rules/branches/main` (the effective merged
  view, not the classic-protection endpoint alone) and citing that result.

## Status

Open. Closes when the follow-up audit's full JSON is recorded here and the resulting branch
(real control confirmed vs. plan/org decision) is resolved.
