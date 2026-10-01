# DR-GITHUB-BRANCH-PROTECTION-001 — GitHub branch protection vs. the merge gateway

Date: 2026-09-02
Status: Decided (see "Decision" below); re-open if `voyn88/ai-command-center` visibility
or plan changes.

## Finding (independently re-verified)

The original audit assumed `voyn88/ai-command-center` was a private/free-tier repository and
that GitHub branch protection was therefore unavailable without a plan upgrade. Re-checking the
live repository directly contradicts that premise:

- The repository is **public**, not private.
- `required_approving_review_count = 0`
- `required_status_checks` is unset (no required check contexts)
- `enforce_admins = false`

Net effect: GitHub-level branch protection on `main` currently enforces nothing. The only
existing gate against unreviewed/unchecked merges is the application-level `merge_once` logic in
`command_center/orchestrator/review_merge.py`, not anything GitHub itself blocks on.

Separately, the plan-limitation premise itself was wrong in the other direction too: branch
protection (required status checks, required reviews, `enforce_admins`) has been available on
GitHub's free tier for both public and private repositories since 2021. There is no billing or
plan upgrade required to turn it on — `scripts/enable-branch-protection.sh` already implements the
`gh api` call to do so and only needs an operator with `repo` admin scope to run it.

## Decision

Adopt **"the merge gateway is the sole enforcement point, documented explicitly"** rather than
treating a plan upgrade as a blocker (there isn't one to unblock):

1. `merge_once` (`command_center/orchestrator/review_merge.py`) remains the authoritative gate
   against duplicate/unreviewed merges. This is not a stopgap pending a GitHub plan change — it is
   the actual enforcement mechanism today and stays that way until GitHub branch protection is
   deliberately enabled.
2. GitHub branch protection on `main` is **not currently configured**. Repository documentation
   must not imply otherwise or attribute the gap to plan/billing limits that do not exist.
3. Enabling branch protection (`bash scripts/enable-branch-protection.sh`, requires an operator
   with `repo` admin scope and `gh` authenticated — neither available to the agent that recorded
   this decision) is a straightforward, zero-cost hardening step and remains open for an operator
   to run at any time. It is not required for `merge_once` to keep functioning as the working
   gate; running it would add a second, GitHub-native layer on top.

## Why this decision, not the plan-upgrade path

The plan-upgrade path in the original finding is moot: there is no plan to upgrade to. The real
open question was only ever "do we rely on GitHub to also enforce this, in addition to
`merge_once`, or do we accept and document that `merge_once` is doing 100% of the enforcement
today." This record answers that: accept and document, because that is what is actually true of
the repository right now, and pretending otherwise (silently relying on protection that isn't
configured) is the harm this task exists to close.

## Documentation updated as a result

- `CURRENT_STATE.md` — removed the "private-repository plan does not expose branch
  protection/rulesets" claim (repo is public; the feature was never plan-gated) and replaced it
  with the actual state: unconfigured, `merge_once` is the real gate.
- `README.md` / `ARCHITECTURE.md` already correctly stated that the CI workflow itself does not
  configure branch protection — no change needed there; this record is the explicit decision
  those sections were previously missing.
