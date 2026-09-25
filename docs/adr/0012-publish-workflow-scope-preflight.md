# ADR-0012: workflow-scope preflight instead of granting `workflow` to worker tokens

Status: accepted for `VOYN-W0-AICC-PUBLISH-WORKFLOW-SCOPE-REM`.

## Context

Found live 2026-08-30 publishing PR #502: a guarded `publish_run` on
worker-01 got GitHub's `! [remote rejected] ... refusing to allow an OAuth
App to create or update workflow`. The change touched `.github/workflows/`;
the worker's `gh` OAuth token carries `gist, read:org, repo` — no
`workflow`. This is not a lease or publisher defect — `_https_push_target`
(see its docstring) deliberately routes the push through `gh`'s own OAuth
credential rather than the per-repo deploy key, because deploy keys were
proven unreliable on 2026-08-21: this org's GitHub silently blocked them on
a private repo, and denied a write even on a public one, with no actionable
diagnostic either time. The one-off recovery was running the same
`publish_run`, same lease, same `--force-with-lease`, on a Mac whose `gh`
token happens to carry `workflow` — a working escape, not a mechanism.

## Decision

Worker `gh` tokens keep their current scope. `workflow` is not added to the
credential every autonomous worker process carries, because it is a
standing grant of push-and-modify authority over CI/CD definitions, not a
one-time read: any task dispatched to that worker — not just the ones that
legitimately need it — runs under a token that could rewrite what CI
executes. Widening it to close one preflight-observable gap trades a
diagnosable, self-contained failure for a permanent increase in every
worker's blast radius.

Instead, `publish.py` gates the push itself:

- `_workflow_scope_gate` fires only when the push goes over `gh`'s
  OAuth-credentialed HTTPS `origin` (the SSH deploy-key fallback is
  untouched by `gh` scopes and is out of scope here), the diff actually
  touches `.github/workflows/**`, and `gh auth status` shows the *specific*
  account that host's push will use is missing `workflow`. It refuses
  before the lease is even acquired, with `reason="workflow_scope_missing: ..."`
  — a report an operator can act on (reroute the task to a worker whose
  token has `workflow`, or grant it to that one credential) instead of a
  bare `push_failed` indistinguishable from a lease race or a network blip.
- The scope check correlates to the *active* account within the matching
  host's own block in `gh auth status`, not the first `Token scopes:` line
  anywhere in the output. An environment logged into more than one account
  (a second `gh auth login`, or a GHE host alongside github.com) can
  otherwise have the preflight read a scope list belonging to a credential
  the push never touches — independent review caught this on the first cut
  (HEAD_SHA d670c34e4d754d009db6ddcf5a383be7dec85fe1) as a false-positive
  risk: blocking a push that would have succeeded is strictly worse than
  the old undifferentiated behavior, which at least attempted it.
- Every fail-open branch (diff unreadable, no matching host block, no
  account marked active, no scopes line for it) defers to the actual push
  rather than guessing. `_classify_push_failure` is the backstop for those
  cases: it recognizes GitHub's own `refusing to allow an OAuth App ...
  workflow` text, a `--force-with-lease` stale-info rejection, and common
  network-failure stderr markers, and reports each under its own distinct
  reason instead of the generic `push_failed` all three used to share. Text
  it doesn't recognize still falls back to `push_failed` — no failure mode
  becomes silently invisible.

## Rejected alternatives

- Grant `workflow` to every worker's `gh` token: closes the gap
  unconditionally, but permanently widens the authority of a credential
  autonomous, unattended processes carry on every task, not just the ones
  that touch `.github/workflows/`.
- Route only workflow-touching tasks to a worker whose token already has
  `workflow` (the Mac used for the live recovery): a real fix, but it is a
  dispatch-routing decision outside this module, needs a place to record
  which workers hold the elevated token, and is not blocked by anything
  here — `_workflow_scope_gate`'s refusal reason is exactly the machine
  signal such a router would key off. Left for whichever task adds
  worker-capability-aware dispatch; this preflight is deliberately useful
  with or without it.
- Detect the scope gap only after a failed push (i.e., `_classify_push_failure`
  alone, no preflight): correct but strictly more expensive — a lease
  acquire, `install-hooks`, and a real rejected push before the worker
  learns what a local `gh auth status` read could have told it up front.

## Operational cost and revisit condition

None of the worker's ambient authority changes; the cost is one additional
`gh auth status` call on pushes that touch `.github/workflows/**` when
using the HTTPS path, plus a `git diff --name-only` already cheap at this
point in `publish_run`. Revisit if worker-capability-aware dispatch lands
and can guarantee workflow-touching tasks never reach a token without
`workflow` in the first place, at which point this preflight becomes a
defense-in-depth check rather than the primary signal.
