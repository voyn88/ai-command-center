# Decision Record — GitHub Branch Protection Tier Gap

- **Record id**: DR-GITHUB-TIER-ENFORCEMENT-001
- **Status**: **Pending founder decision.** This record cannot be closed by an agent — the choice
  below is a commercial/GitHub-plan decision, which sits in the founder-reserved decision set
  alongside "commercial model" (`projects/AIOS.md` §13). It is drafted here so the founder can
  decide by picking Option A or Option B; nothing is authorized yet.
- **Date**: 2026-08-26
- **Task**: `VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP` (Wave 0, P0)
- **Scope**: Documentation/decision only. No runtime code changed, no GitHub settings changed, no
  repository/org plan changed.

## Finding

`gh api repos/voyn88/ai-command-center/branches/main/protection` was checked and confirms:

- `required_approving_review_count` = `0`
- `required_status_checks` — absent
- `enforce_admins` = `false`

Branch protection on `main` currently enforces **nothing**. It is not a required-reviews gate and
not a required-checks gate, regardless of what CI reports or what `merge_once`
(`command_center/orchestrator/review_merge.py`) decides. This matches what the codebase already
says about itself — README.md, `CURRENT_STATE.md` §"Current limitations", and ARCHITECTURE.md §13
already state that the workflow/CI does not itself configure or enforce branch protection, and that
the current private-repo plan does not expose branch protection/rulesets. No document audited here
overstates branch protection as a working control.

The only actual gate standing between an ACCEPT-marked PR and a merge to `main` is the application
layer: `merge_once` requires an ACCEPT verdict and green required checks *as GitHub reports them to
that code path*, then calls `gh pr merge`. There is no GitHub-side backstop if that application
logic has a bug, is bypassed, or is run against a misconfigured check set.

## Options

**Option A — Change plan/org.** Move the repository to a GitHub plan or organization that exposes
real enforcement (rulesets or classic branch protection with `required_status_checks` and
`required_approving_review_count` ≥ 1, `enforce_admins` = `true`). This closes the gap at the
platform level and makes `merge_once` a second, redundant gate instead of the only one. This is a
recurring commercial cost and an account/org change — a commercial decision, not an engineering one.

**Option B — Accept the gap as a documented risk, with a hard scaling constraint.** Explicitly
accept that until `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships, the application-level `merge_once`
gate is the *only* line of defense against a bad merge to `main`, and commit to **not increasing
the number of concurrent task agents/branches working against this repository** beyond the current
level while that is true — because every added concurrent writer increases the blast radius of a
`merge_once` bug or bypass with no GitHub-side backstop to catch it.

## Recommendation

Option B is the lower-friction default: it costs nothing and matches the actual current operating
pattern (one agent = one task = one branch = one worktree per `docs/roadmap/MASTER_PRODUCT_ROADMAP.md`
line 199). But "accept this risk" is itself a founder call, not something an agent should decide on
the business's behalf — so this record stays **Pending** rather than marking Option B accepted.

## What closes this record

The founder picks Option A or Option B (or a variant). Once chosen:

- If **A**: record the target plan/org and the date the change lands; re-verify via
  `gh api .../branches/main/protection` that `required_status_checks` and
  `required_approving_review_count` are actually non-empty/≥1 before marking this Accepted.
- If **B**: record the explicit concurrency cap (a number or rule, not just "be careful") that
  stays in force until `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships, then mark this Accepted.

Until either happens, no document or audit in this repository should describe branch protection as
an enforced control without re-checking the API first — the finding above can go stale the moment
someone changes the GitHub setting by hand.
