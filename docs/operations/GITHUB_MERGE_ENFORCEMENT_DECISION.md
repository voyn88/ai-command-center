# GitHub merge-time enforcement on `main`: what is verified, what is not, and the accepted-risk decision

Status: accepted for `VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP-REM-REM-REM`.
Decision id: `DR-GITHUB-MERGE-ENFORCEMENT-001` (see `DECISIONS.md`).

This is the fourth attempt at this record. The first three
(`VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP`, `-REM`, `-REM-REM`, PRs #450, #482,
#495) were each rejected for drawing a conclusion broader than the evidence
gathered: generalizing from three classic branch-protection fields to "GitHub
enforces nothing," presupposing a plan/org change before auditing rulesets,
and relaxing a parallelism cap that was never precisely defined or pointed at
an actual control. This record narrows every claim to what it can actually
show, and treats everything else as an open, precisely-scoped audit item
rather than a settled fact.

## 0. Method and its own limits

This session's Bash tool refuses every `gh` invocation (`gh auth status` and
`gh api repos/voyn88/ai-command-center/...` both returned a tool-level
permission denial, not a `gh` error) — this task-clone role has no live
GitHub API access. Two consequences:

- Nothing below that is described as "verified" was re-queried live in this
  session. Where this record repeats a prior session's `gh api` result, it is
  labeled historical, dated, and scoped to the exact fields that call
  returned — never generalized past them.
- Everything that requires a live API call is written up as an exact,
  runnable procedure (§3) for an operator or session that does have `gh`
  credentials, not executed here. Until someone runs it, its answer is
  "unknown," not "no" and not "yes."

Everything in §1 *is* verified in this session, because it comes from reading
this repository's own committed source, which this session can do directly
and reliably.

## 1. What actually gates a merge onto `main` today (verified from code)

1. The real, functioning merge gate is application code, not a GitHub
   setting: `command_center/orchestrator/review_merge.py`. `merge_once`
   (`review_merge.py:2005`) only calls `gh pr merge` on a PR for which
   `_pr_is_mergeable` (`review_merge.py:1843-1879`) returns true, which
   requires both:
   - an `ACCEPTANCE: ACCEPT <head sha>` marker posted as a PR review by
     `voyn88-acceptance-gate[bot]` — a login independent of the PR's own
     author (`_accept_marker_on_latest_review`, called with the PR's own
     `author.login` at `review_merge.py:1871-1874`), and
   - every check in the PR's status-check rollup green, not merely whichever
     subset GitHub's own branch-protection config would call "required"
     (`review_merge.py:1875-1878`).
2. Native GitHub required-reviews cannot express rule (a) on this repository
   regardless of plan or tier: every agent run shares one PR-author account,
   so a native "require N approving reviews" setting would only ever see a
   same-author situation and GitHub refuses self-approval at the API
   (`.github/workflows/acceptance-gate.yml:5-13`, module docstring
   `review_merge.py:1-45`). That is *why* this pipeline exists as
   application code instead of a branch-protection setting — it is not an
   oversight to fix by raising the plan tier, it is a structural
   workaround for a limitation no GitHub plan removes.
3. This code already knows, and says explicitly, that it does not stop a
   merge performed outside itself. `_merged_target_sha`'s docstring
   (`review_merge.py:1905-1923`) calls a PR merged without this evidence "an
   admin bypass, a hand merge around the queue" and treats it as "an
   incident for the operator" — the pipeline detects such a merge after the
   fact and refuses to silently mark the task done, but nothing in this
   repository causes GitHub itself to refuse that merge from landing on
   `main` in the first place. That refusal, if it exists at all, can only
   come from GitHub-side configuration — which is exactly what §2-§3 covers.
4. `voyn88` is a GitHub *organization*, not a personal account: both
   `command_center/orchestrator/github_app_auth.py:13` and
   `review_merge.py:21` reference "the 2026-08-20 org migration" as the
   event that broke the acceptance bot's App installation. This determines
   which ruleset and branch-protection features are even on the table, but
   which paid tier (if any) the organization is on is not recorded anywhere
   in this repository and must not be assumed from either direction —
   see §3.
5. No numeric cap on parallel autonomous publish/merge activity is enforced
   by code anywhere in this repository today. What exists:
   - `deploy/aicc/worker-lanes` (installed to `/etc/aicc/worker-lanes`,
     read by `ops/aicc_staged_worker_rollout.py:discover_units`,
     line 274) is the canonical worker-lane registry and today lists
     exactly two lanes (`deploy/aicc/worker-lanes:3-4`), with its own
     comment stating "deployments can scale by adding instance names."
     `_configured_units` (`ops/aicc_staged_worker_rollout.py:141-156`)
     validates lane names are well-formed and non-duplicate but never
     bounds their count — `tests/ops/test_aicc_staged_worker_rollout.py`'s
     `test_discovery_combines_configured_and_existing_lanes` asserts a
     *third* lane is accepted. So "two" is today's canonical default, not
     an enforced ceiling.
   - `command_center/pipeline_settings.py:44-47` is a separate mechanism
     (the local desktop app's own autopilot concurrency, not the
     server-side publish pipeline): `DEFAULT_MAX_GLOBAL_CONCURRENCY = 2`
     and `DEFAULT_MAX_AGENT_CONCURRENCY = 2`, clamped by a hard
     `MAX_CONCURRENCY = 16` ceiling that an operator can already reach
     through ordinary configuration — that ceiling was sized against
     "fork-bomb the host," not against this GitHub gap, and is out of
     scope for this decision.
   - The production merge tick itself (`deploy/systemd/aicc-backlog-merge.timer`,
     `OnUnitActiveSec=5min`) is a single timer with no explicit concurrency
     setting; whether an overrunning tick can overlap the next firing is
     `systemd`'s default oneshot-unit behavior, not a designed cap, and is
     not verified here either way.

## 2. What the prior sessions' GitHub API evidence does and does not show

This record is written from the rejection feedback on the three prior
attempts (quoted in this task's own brief), not from the prior PRs' full
bodies, which are not available in this repository — so it describes what
each round's evidence *established*, per its reviewer, rather than
reproducing raw API output this session cannot re-verify.

- **Round 1 (`#450`, head `002e545`).** `gh api repos/voyn88/ai-command-center/branches/main/protection`
  reported `required_approving_review_count=0`, `required_status_checks`
  absent, `enforce_admins=false`. Rejected for generalizing "these three
  classic-protection fields are permissive" into "branch protection enforces
  nothing," when push restrictions, conversation resolution, signed commits,
  linear history, force-push/deletion controls and review-policy settings
  were never queried.
- **Round 2 (`#482`, head `e1d9ed6`).** Rejected on the same "three
  classic-protection fields don't prove GitHub blocks nothing" ground, this
  time naming rulesets, merge-queue requirements, deployment gates and actor
  restrictions explicitly as unqueried controls that could independently
  block a merge — plus a Medium finding that Option A (a plan/org change)
  was presupposed without first establishing repo visibility, current plan
  capabilities, or ruleset availability.
- **Round 3 (`#495`, head `21adab6`).** This round evidently *did* query
  rulesets — its rejection describes it treating "the mere presence of an
  active `pull_request` or `required_status_checks` rule" as proof of a
  working control, which is only meaningful if such a rule exists. It was
  rejected because presence alone proves nothing: the rule's approval count
  could be zero, its required-checks list could be empty, and its
  bypass-actor list could include the autonomous publisher or repo admins —
  none of which that round inspected — and because it relaxed the
  parallelism cap without ever stating the current count or the control that
  enforces it.

Net effect: three classic-protection fields are confirmed permissive; at
least one active ruleset rule of type `pull_request` and/or
`required_status_checks` apparently exists (per round 3's description, not
independently re-confirmed here); and its parameters, enforcement state and
bypass actors — along with every other control listed above — remain
unverified by any of the four attempts, including this one. **This record
does not claim `main` is unprotected by GitHub, and does not claim it is
protected either. It claims that the actual working gate today is the
application code in §1, and that GitHub's own enforcement is an open
question with a precise procedure (§3) to close it.**

## 3. The audit that must run before any stronger claim is made

None of this ran in this session (§0). Whoever has `gh` credentials for
`voyn88/ai-command-center` should run all of it in one sitting, since a
partial re-run is exactly the failure mode this record exists to stop:

```sh
# 1. Full classic branch-protection object, not a field subset.
gh api repos/voyn88/ai-command-center/branches/main/protection

# 2. Rulesets are a separate, additive system — list every one, then fetch
#    each in full, including its bypass actors and enforcement mode.
gh api repos/voyn88/ai-command-center/rulesets
gh api repos/voyn88/ai-command-center/rulesets/<id>   # for each id above

# 3. Org-level rulesets can also apply to this repo and are invisible to
#    the repo-scoped calls above.
gh api orgs/voyn88/rulesets

# 4. Repo/org plan and visibility — do not infer feature availability from
#    memory of GitHub's pricing page; confirm what's actually enabled here.
gh api repos/voyn88/ai-command-center --jq '{private, visibility}'
gh api orgs/voyn88 --jq '{plan}'

# 5. Merge queue and required-check identity as GitHub currently computes
#    them for a real PR (rulesets/protection can each name different check
#    contexts than what merge_once/_pr_is_mergeable checks in app code).
gh api repos/voyn88/ai-command-center/pulls/<any-open-pr>/merge --method GET 2>&1 | true
```

A rule or ruleset *existing* is not evidence it blocks anything — a
`pull_request` rule can require zero approvals, a required-checks list can be
empty, and a bypass-actor entry can name the autonomous publisher's own
identity or repo admins. Read every returned rule's parameters and
`bypass_actors`, not just its presence.

The only way to move from "the config says X" to "GitHub actually does X" is
behavioral, not textual: open a disposable branch/PR against a scratch file,
and — using the actual autonomous-publisher `gh` identity, not a personal
admin account — attempt `gh pr merge` on it with no ACCEPT marker and/or a
deliberately red check. If GitHub refuses, that specific path is really
enforced; if it succeeds, it is not, regardless of what the configuration
API reported. Delete the scratch branch/PR afterward either way.

## 4. Decision

Given §1.2-§1.3 — native review approval is structurally inapplicable here
regardless of tier, the actual functioning gate is application code that is
already independent of GitHub's protection tier, and the specific residual
gap is that GitHub itself is not confirmed to refuse a bypass merge performed
outside that application code — this record does **not** presuppose a plan
or organization change (rejecting that as a foregone conclusion was
`-REM`'s Medium finding). Whether a tier change is even necessary depends on
what §3 finds: if rulesets already close this gap on the current plan,
upgrading buys nothing; if they don't, and can't on this plan, that is a
genuine commercial decision for the founder, out of this session's authority
per the reserved list.

**Accepted risk, pending §3:** operate as though a bypass merge outside the
application-code gate in §1 is possible, because §2's evidence — three
permissive classic-protection fields, plus an unverified ruleset rule that
may or may not actually block anything — has not disproven it, and bound the
resulting exposure rather than waiting on the audit to bound it:

- Do not raise the worker-lane count past the two lanes `deploy/aicc/worker-lanes`
  lists today (§1.5) until either §3 confirms a real GitHub-side control with
  no exploitable bypass actor, or `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY`
  ships. This ceiling is now enforced in CI, not just written here:
  `tests/ops/test_aicc_staged_worker_rollout.py::test_canonical_worker_lane_registry_stays_at_the_accepted_risk_ceiling`
  fails the moment a third lane is added to that file, and its failure
  message points back to this document.
- No document in this repository may describe GitHub branch protection or
  rulesets as an active, working control until §3 has been run against
  current state and the result recorded here (see §5). §1 already corrects
  two that did: `CURRENT_STATE.md` and `ARCHITECTURE.md`.

## 5. Revisit log

| Date | Trigger | Result |
|---|---|---|
| 2026-08-31 | This record's creation | §3 not yet run in any session with live credentials; accepted-risk posture of §4 is the current state. |

Update this table, not just §4's prose, the next time §3 actually runs —
including a run that finds GitHub enforces nothing further, so a future
reader doesn't have to re-derive that this gap is still open.
