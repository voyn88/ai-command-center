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

`gh api repos/voyn88/ai-command-center/branches/main/protection` was checked and confirms, for the
three fields queried:

- `required_approving_review_count` = `0`
- `required_status_checks` — absent
- `enforce_admins` = `false`

That is the full extent of what this audit establishes: `main`'s branch protection requires **zero
approving reviews**, has **no required status checks**, and does **not** enforce its rules against
repository admins. It does **not** establish the state of the protection response's other fields —
push restrictions, required conversation resolution, required signed commits, required linear
history, force-push/deletion controls, and any other review-policy settings were neither queried
nor reported here, and their state is unknown as of this record. **This record does not claim that
branch protection enforces nothing at all** — only that these three specific controls, the ones
that would gate a PR on review or CI, are absent. A future audit must re-query the full protection
response before making any broader claim about the branch's protection posture (see "What closes
this record" below).

Within that narrower, evidence-backed claim, the practical consequence is unchanged: because the
review-count and status-check gates are the two controls capable of blocking an unreviewed or
red-CI PR from merging, their absence means GitHub itself is not blocking such a merge to `main`,
regardless of what CI reports or what `merge_once` (`command_center/orchestrator/review_merge.py`)
decides. This matches what the codebase already says about itself — README.md, `CURRENT_STATE.md`
§"Current limitations", and ARCHITECTURE.md §13 already state that the workflow/CI does not itself
configure or enforce branch protection. (Those documents' further claim that the plan "does not
expose branch protection/rulesets" is a separate claim this audit neither confirms nor refutes: the
protection endpoint above returned data rather than 404, which is consistent with some protection
configuration existing on `main` — just not the review/status-check/admin-enforcement rules this
audit checked. That distinction is out of scope for this record and is not resolved here.)

The only actual gate confirmed by this audit standing between an ACCEPT-marked PR and a merge to
`main` is the application layer: `merge_once` requires an ACCEPT verdict and green required checks
*as GitHub reports them to that code path*, then calls `gh pr merge`. Given the absence of the two
GitHub-side gates above, there is no confirmed GitHub-side backstop if that application logic has a
bug, is bypassed, or is run against a misconfigured check set — but this record cannot rule out that
some other, unqueried protection setting (e.g. push restrictions) provides a partial backstop.

## Options

**Option A — Change plan/org.** Move the repository to a GitHub plan or organization that exposes
real enforcement (rulesets or classic branch protection with `required_status_checks` and
`required_approving_review_count` ≥ 1, `enforce_admins` = `true`). This closes the gap at the
platform level and makes `merge_once` a second, redundant gate instead of the only confirmed one.
This is a recurring commercial cost and an account/org change — a commercial decision, not an
engineering one.

**Option B — Accept the gap as a documented risk, with a hard scaling constraint.** Explicitly
accept that until `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships, the application-level `merge_once`
gate is the *only confirmed* line of defense against a bad merge to `main` on the required-review
and required-status-check axes, and commit to **not increasing the number of concurrent task
agents/branches working against this repository** beyond the current level while that is true —
because every added concurrent writer increases the blast radius of a `merge_once` bug or bypass
with no confirmed GitHub-side backstop to catch it.

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

Either way, before closing this record, re-query the **complete** protection response (not just the
three fields above) and evaluate every relevant field — push restrictions, required conversation
resolution, required signed commits, required linear history, force-push/deletion controls, and any
other review-policy settings — so the closed record states the branch's full protection posture
rather than only the three-field subset audited here.

Until either happens, no document or audit in this repository should describe branch protection as
an enforced control, or as enforcing nothing, without re-checking the full API response first — the
finding above can go stale the moment someone changes a GitHub setting by hand, and it was never a
complete picture to begin with.
