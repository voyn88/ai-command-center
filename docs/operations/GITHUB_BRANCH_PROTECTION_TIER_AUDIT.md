# GitHub branch protection / tier enforcement — audit and decision

Audited: 2026-09-01. Supersedes the unmerged closing attempts on
`VOYN-W0-AICC-GITHUB-TIER-ENFORCEMENT-GAP` and its three prior `-REM` links
(PRs #450, #482, #495, #513 — all rejected; none of their content is on
`main`). Each rejection asked for the same thing in a different shape:
**query the complete, current state before drawing a conclusion, and say
exactly which fields that conclusion rests on.** This record does that, and
draws a narrower line than any of its predecessors between what is verified,
what is stale, and what is still unknown.

## What changed since the original finding

The task's originating evidence — `required_approving_review_count=0`,
`required_status_checks` absent, `enforce_admins=false` from
`gh api repos/voyn88/ai-command-center/branches/main/protection` — is dated
evidence from an earlier session with authenticated `gh` access. This audit
was produced from an unprivileged sandbox with no `gh` credential and no
GitHub token of any kind (confirmed: `gh` itself is not invokable here, and
`GET /branches/main/protection` returns `401 Requires authentication`
against this repository even though the repository is public — that
endpoint always requires an authenticated collaborator, regardless of
visibility). Every claim below is instead built from endpoints that answer
without a credential, run against `voyn88/ai-command-center` on 2026-09-01,
and are independently reproducible by anyone with `curl`:

```bash
curl -s https://api.github.com/repos/voyn88/ai-command-center
curl -s https://api.github.com/repos/voyn88/ai-command-center/branches/main
curl -s https://api.github.com/repos/voyn88/ai-command-center/rules/branches/main
curl -s https://api.github.com/repos/voyn88/ai-command-center/rulesets
curl -s https://api.github.com/repos/voyn88/ai-command-center/commits/<sha>/check-suites
```

### Confirmed, live, unauthenticated (2026-09-01)

1. **The repository is public** (`"private": false`). The earlier record's
   framing ("the current private-repository plan does not expose branch
   protection/rulesets") is stale on the visibility premise alone: GitHub
   gives public repositories rulesets, required status checks, and merge
   queue at no additional cost — a plan/organization upgrade is not a
   precondition for any of those three, and this audit found no plan-gated
   feature actually blocking them (see next point).

2. **`main` now carries real, non-classic-endpoint-invisible enforcement.**
   `GET /repos/voyn88/ai-command-center/branches/main` (the lightweight,
   unauthenticated branch endpoint — distinct from the full protection
   endpoint) returns:
   ```json
   "protected": true,
   "protection": {
     "enabled": true,
     "required_status_checks": {
       "enforcement_level": "non_admins",
       "contexts": [
         "Final merge gate",
         "Acceptance gate (independent verdict on exact SHA)"
       ]
     }
   }
   ```
   Both named contexts are this repository's own jobs —
   `.github/workflows/ci.yml`'s `final-gate` ("Final merge gate") and
   `.github/workflows/acceptance-gate.yml`'s job (already referenced by
   name in `tests/test_release_gate_policy.py` and
   `tests/test_acceptance_gate.py`). GitHub is now configured to require
   the *same two checks* that `command_center/orchestrator/review_merge.py`
   (`_pr_is_mergeable`) already required at the application level. That is
   a materially different state than "branch protection enforces nothing":
   `required_status_checks` is no longer absent.

3. **This is newly active, not long-standing.** Sampling recent merge
   commits' check-suites shows the change landing between PR #534 and PR
   #537: commits for PR #534, #533, #532, #531 all ran their check-suites
   directly against `head_branch: "main"`; the most recent commit, PR #537
   (`f9fef7d`, current `main` tip), shows three of its five check-suites
   with `head_branch: "gh-readonly-queue/main/pr-537-..."` — GitHub's
   native merge queue. `command_center/orchestrator/review_merge.py`
   (`_merged_target_sha`'s docstring, `_rerun_failed_ci_once`) independently
   corroborates this: it already describes "a merge-queue-protected
   repository" and a live incident with it dated 2026-08-26, and already
   defends against "an externally merged PR (an admin bypass, a hand merge
   around the queue)" being silently marked `DONE` without acceptance
   evidence. The enforcement layer and the application code's awareness of
   it both predate this audit; this audit is the first to confirm the
   GitHub-side configuration directly via API rather than inferring it from
   application behavior.

4. **No ruleset adds or narrows anything on `main`.**
   `GET /repos/voyn88/ai-command-center/rules/branches/main` returns `[]`.
   Per GitHub's documentation this endpoint returns every *active* rule
   that applies to the named branch "regardless of the level at which
   [rulesets are] configured" (repository, organization, or enterprise),
   covering every rule type the ruleset system can express — required
   status checks, required reviews, merge queue, required deployments,
   required signatures, linear history, non-fast-forward, commit message
   patterns, and more. An empty result is authoritative for that entire
   surface: no ruleset, at any level, currently governs `main`. (The one
   active ruleset this repository has —
   `GET /repos/voyn88/ai-command-center/rulesets` returns one, id 20717451,
   `"release-tag-protection"` — targets `tag`, not `branch`, and does not
   apply here.) The enforcement in point 2 is therefore coming from
   classic branch protection, not a ruleset.

### Explicitly not confirmed by this audit

`GET /repos/voyn88/ai-command-center/branches/main/protection` — the only
endpoint that reports `required_approving_review_count`, `enforce_admins`,
push restrictions, `required_linear_history`, `required_conversation_resolution`,
`allow_force_pushes`, `allow_deletions`, `required_signatures`, and
`lock_branch` — requires an authenticated collaborator and returned `401`
in this sandbox. None of those fields are re-verified here. Specifically:

- The task's originating `required_approving_review_count=0` and
  `enforce_admins=false` are **not re-confirmed or re-refuted** by this
  audit. They should be treated as stale (from an earlier, authenticated
  session) rather than restated as current fact until someone with a live
  `gh` credential re-runs the full protection query.
- `enforcement_level: "non_admins"` on `required_status_checks` (point 2
  above) is consistent with the earlier `enforce_admins=false` reading — it
  means the two required checks bind non-admin actors but not repository
  admins. **Whether that matters for this repository's autonomous merge
  pipeline depends on whether the identity `command_center/orchestrator/
  review_merge.py`'s `_gh` calls run as (the ambient `gh` auth of the
  publisher principal, per ADR-0010) is itself a repository or
  organization admin.** This audit cannot determine that from
  unauthenticated endpoints (it requires
  `GET /repos/.../collaborators/{username}/permission` or org-membership
  data, both authenticated) and does not assume an answer either way. This
  is the single open question that determines whether point 2's
  enforcement is a real backstop for the autonomous pipeline specifically,
  or only for any other, non-admin contributor.

## Decision

**No commercial plan or organization change is required.** The repository
is public, rulesets/required-checks/merge-queue are already available on
that tier, and two of the three original gaps (`required_status_checks`
absent; no GitHub-side merge queue) are now closed — verified live, not
assumed. This audit does not construct that as a deliberate choice made in
response to this task; it records the state as found. Nothing here directs
the owner to take a paid-tier action, so the commercial-decision escalation
this task chain carried is resolved by removal, not by exercising it.

**The accepted-risk posture from the prior (unmerged) attempts is carried
forward, not relaxed, pending the one open question above.** Until either
(a) an authenticated audit confirms `required_approving_review_count > 0`
and that the autonomous publisher's merge identity is not admin-exempt, or
(b) `VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY` ships an independent enforcement
layer that does not depend on GitHub-side configuration at all, this
repository has two confirmed, evidenced lines of defense against a bad
merge reaching `main` through the autonomous pipeline:

1. The application-level gate: `merge_once` / `_pr_is_mergeable`
   (`command_center/orchestrator/review_merge.py`) — requires green
   `statusCheckRollup` and an ACCEPT marker from a reviewer login distinct
   from the PR author, and `_merged_target_sha` refuses to record a task
   `DONE` if a merge landed without that evidence, even if the merge itself
   happened around this gate.
2. The GitHub-side gate confirmed in this audit: `required_status_checks`
   on `main` (contexts `Final merge gate`, `Acceptance gate (independent
   verdict on exact SHA)`), binding at least every non-admin actor, plus an
   active merge queue.

Neither is proven to bind an admin-identity merge. Until that is settled,
this record keeps the operating constraint from the earlier attempts:

**No more than 2 autonomous task-agents run concurrently against this
repository until the open question above is resolved or the gateway
ships.** This is recorded as an *operator/process discipline*, not a
code-enforced ceiling. The prior attempt (PR #513) tried to enforce it by
capping `ops/aicc_staged_worker_rollout.py`'s `discover_units`, and was
correctly rejected: that function's registry-plus-discovered-instances
behavior is pre-existing, intentional, and already covered by
`test_discovery_combines_configured_and_existing_lanes` in
`tests/ops/test_aicc_staged_worker_rollout.py` — a contract from the
unrelated `VOYN-W0-AICC-AGENT-PUBLISHER-PRINCIPAL-ISOLATION` line of work
(ADR-0010), which deliberately treats any already-enabled
`voyn-aicc-worker@N.service` template instance as a legitimate lane
regardless of the registry file. Bolting a hard ceiling onto that function
would either silently break that accepted contract or be trivially
sidestepped by enabling an instance outside the registry, which is exactly
what the prior rejection found. `deploy/aicc/worker-lanes` freezing at two
entries constrains new lanes provisioned through the canonical
config-managed path; it is not, and this record does not claim it is, a
technical ceiling on this repository's overall autonomous concurrency.
Raising the 2-agent limit above is a decision that belongs in this record,
not in that file.

**Documentation discipline going forward:** no document or audit in this
repository may describe branch protection as an enforcing control without
citing which specific field, from which specific (authenticated or
unauthenticated) endpoint, on which date, supports the claim. "Branch
protection enforces nothing" is no longer an accurate summary of `main`'s
state and must not be repeated; "branch protection fully enforces
independent review" is equally unsupported and must not be claimed either.
The accurate statement, as of 2026-09-01, is exactly the two Confirmed/Not
confirmed sections above.
