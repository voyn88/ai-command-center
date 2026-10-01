# ADR 0012 — Preflight gate and failure classification for `.github/workflows/` pushes lacking `workflow` scope

Status: **Accepted, implemented.**

## Context

Found live on 2026-08-30 publishing PR #502: `publish_run` on worker-01 pushed a candidate branch
whose diff added a file under `.github/workflows/`, and GitHub's guarded `publish_run` push was
rejected with `! [remote rejected] ... refusing to allow an OAuth App to create or update workflow`.
`publish.py`'s own docstring on `_https_push_target` already records why the push goes through
`gh`'s OAuth credential over HTTPS rather than the per-repo SSH deploy key: on 2026-08-21 deploy
keys were silently denied writes by this org even on a public repository, with no actionable
diagnostic on either side, and every manual recovery that session fell back to `gh`'s credential.
That decision is not revisited here — it is the reason this failure mode exists at all. The
worker's `gh` token carries `gist, read:org, repo`; GitHub additionally requires the `workflow`
scope on any OAuth token, PAT, or GitHub App installation before it will accept a push that touches
`.github/workflows/**`, regardless of the account's repository permissions. No amount of `repo`
access substitutes for it.

The same `publish_run`, same guarded lease, same leak guard, same `--force-with-lease`, run from a
Mac whose `gh` token does carry `workflow`, published the change without incident. That is a
one-off human workaround, not a mechanism a headless worker can rely on.

Two prior remediations on this task were rejected by adversarial review, both for the same class of
defect: a preflight/classification device that reads the *wrong* credential or the *wrong* stderr
signal and turns a would-have-succeeded push, or a mis-diagnosed one, into a worse outcome than the
original undifferentiated `push_failed`:

- **PR #689** (`VOYN-W0-AICC-PUBLISH-WORKFLOW-SCOPE`): the scope check scanned `gh auth status`'s
  entire combined output for the first `Token scopes:` line found anywhere, with no correlation to
  the host or account the push would actually use. On a worker logged into more than one account or
  host, this could read a *different* credential's scopes than the one performing the push —
  false-positive direction: **blocking a push that would have succeeded**, which is strictly worse
  than the prior behavior of just attempting it.
- **PR #773** (`VOYN-W0-AICC-PUBLISH-WORKFLOW-SCOPE-REM`): the network-failure classifier bucketed
  git's generic SSH fatal `Could not read from remote repository` as `push_network_failure`, but
  that line follows `Permission denied (publickey)` far more often in practice than it follows an
  actual outage. An auth/permission failure misreported as a network failure tells an operator or
  automation to retry a problem a retry cannot fix, while the real cause — a bad key, a missing
  grant — goes unaddressed.

## Decision

### 1. Do not widen the worker's OAuth token scope as part of this remediation

Granting `workflow` to the token every autonomous worker uses for every push, on every task,
regardless of whether that task's diff ever touches a workflow file, is a permanent expansion of
what an unattended process can do to this organization's CI/CD definitions — for a failure mode
that, in practice, only a small fraction of publishes ever hit. That tradeoff is an organizational
security decision (who is allowed to grant it, and under what review), not a change this codebase
can make unilaterally by editing `publish.py`. It is out of scope for this remediation and is
recorded here as the deliberately-deferred alternative, not silently dropped: **whether to grant
`workflow` broadly, or to stand up a distinct execution path/credential for workflow-touching
changes, is an operational decision for whoever administers the org's `gh` OAuth grants**, informed
by how often the new preflight gate below actually fires in practice.

### 2. A preflight gate refuses workflow-touching diffs the active credential cannot push, before the push

`_workflow_scope_gate` runs after the existing static-quality and leak-guard gates and before the
lease is acquired. It composes two independently fail-open checks:

- `_diff_touches_github_workflows(repo_path, base_sha, head_sha)` — `git diff --no-renames
  --name-only`, so a workflow file renamed *out of* `.github/workflows/` still shows under its old
  path rather than being hidden by rename detection folding it entirely into its new location.
- `_gh_oauth_workflow_scope_missing(repo_path, host)` — scoped by `--hostname` to the exact host
  `_https_push_target` resolved, and within that host's `gh auth status` output, narrowed further to
  the block carrying `Active account: true`. That is the account `gh` and git's credential helper
  actually select for any operation against that host, which is the direct fix for PR #689's defect:
  the check now reads the credential that will perform the push, not the first one printed.

Both helpers return `None` — fail open, defer to the real push — on anything they cannot read
unambiguously (unparseable diff, non-zero `gh auth status`, no active-account block, no scopes line
inside it). The gate can only ever *narrow* a refusal that the diff and the scope list both support;
it cannot manufacture one from state it could not read, and it cannot suppress the real push's own
outcome when it defers. `https_target is None` (the SSH deploy-key fallback path) is a no-op for
this gate: that credential is not an OAuth App and was never subject to this GitHub restriction.

A blocked push reports `workflow_scope_missing: ...`, naming the diff/scope condition — distinct
from every other refusal reason this module returns, and produced before any lease is acquired or
any lease-state mutation happens, so a redelivery attempt is cheap.

### 3. Failure classification distinguishes scope, auth, stale-lease, and network causes

For the (now rarer, but not eliminated — the gate is fail-open) case where a push still reaches
GitHub and is rejected there, `_classify_push_failure` replaces the previous single `push_failed`
bucket with five, checked in this order:

1. `push_rejected_workflow_scope` — GitHub's own OAuth-App-workflow-scope rejection text.
2. `push_auth_failure` — `Permission denied`, `publickey`, `403`, `Authentication failed`. Checked
   **before** the network bucket, which is the direct fix for PR #773's defect: git's generic
   `Could not read from remote repository` fatal, which follows an auth failure far more often than
   an outage in this environment, is now attributed to auth whenever an auth marker is also present.
3. `push_rejected_stale_lease` — `stale info`, `fetch first`, `non-fast-forward`: the next tick's
   retry against a freshly re-observed remote tip already resolves this; no scope or credential
   change is needed.
4. `push_network_failure` — DNS/connect/timeout markers, and `Could not read from remote repository`
   only when no auth marker is also present.
5. `push_failed` — unchanged fallback for anything the above heuristics do not recognize; no failure
   mode becomes silently invisible.

This directly satisfies acceptance criterion 2: a missing-scope refusal, a lease conflict, and a
genuine network fault are no longer indistinguishable in the reported reason.

## Consequences

- A worker whose `gh` token lacks `workflow` now fails a workflow-touching publish deterministically,
  before any push attempt, with a reason an operator or dispatcher can act on directly (grant the
  scope, or route the task to an executor that already has it) — satisfying acceptance criterion 1.
- The gate and the classifier are both fail-open on anything ambiguous, by design: a worker whose
  `gh auth status` output this cannot parse (CLI version drift, an unexpected login topology) simply
  defers to the real push and its own classification, rather than blocking every publish on that
  host. This trades a small false-negative rate (an occasional undifferentiated `push_failed` still
  surfaces) for zero risk of the false-positive regression PR #689 introduced.
- No worker's OAuth token scope changed as part of this remediation. Workflow-touching tasks
  dispatched to a worker without `workflow` will be refused pre-push every time, until the
  organizational decision in §1 is made and acted on separately.
