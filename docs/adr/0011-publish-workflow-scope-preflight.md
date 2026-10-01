# ADR-0011: refuse workflow-touching publishes by reason, don't widen the worker token

Status: accepted for `VOYN-W0-AICC-PUBLISH-WORKFLOW-SCOPE`.

## Context

Found live 2026-08-30 publishing PR #502: `publish_run` on worker-01 pushed a
candidate that included a `.github/workflows/` file and GitHub rejected it —
`! [remote rejected] ... refusing to allow an OAuth App to create or update
workflow`. The push goes out over `gh`'s own OAuth credential
(`_https_push_target`, chosen over the per-repo deploy key after deploy keys
were found to be silently blocked by this org on 2026-08-21). That credential
carries `gist, read:org, repo` — never `workflow` — because it is the same
token every autonomous worker uses for every task, and it was scoped to what
an ordinary content/code publish needs.

The immediate symptom was a generic `push_failed: <stderr>`, indistinguishable
from a stale `--force-with-lease` (another writer's push landed first) or a
dropped connection. All three are recoverable in principle, but only one of
them recovers by itself on retry (network); a stale lease recovers after a
rebase; a missing scope never recovers by retrying the same worker at all.

## Decision

Two changes to `command_center/orchestrator/publish.py`, no change to any
worker's `gh` credential:

1. `_workflow_scope_gate` runs before the writer lease is acquired. When the
   push target is `gh`'s OAuth credential and the candidate's diff touches
   `.github/workflows/**` and the credential's `gh auth status` scopes are
   readable and do not include `workflow`, it refuses immediately with
   `workflow_scope_missing` — no lease taken, no push attempted.
2. `_classify_push_failure` inspects a rejected push's stderr and returns
   `workflow_scope_missing`, `push_rejected_stale_lease`, or
   `push_network_failed` before falling back to the previous generic
   `push_failed: <stderr>`. This is the safety net for the case the preflight
   can't cover — an unreadable scope list, or a scope that changes between
   the preflight and the push — so the same distinguishing reason still
   surfaces even when GitHub itself is the one saying no.

Both gates fail *open* when they cannot read a precondition (no diff, no
scope list): an unreadable state defers to the push attempt itself rather
than blocking a publish that might have succeeded.

**Granting the worker token `workflow` scope was considered and rejected as
the fix.** It would make this one failure mode disappear, but the token is
shared ambient authority across every autonomous worker and every future
task, not a credential scoped to this task. `workflow` scope lets its holder
rewrite this repository's CI — add steps, change triggers, alter what runs
with what permissions — from any publish, unreviewed, the same way `repo`
scope already lets it rewrite application code. Widening it is a real
increase in blast radius for an unattended fleet, and deciding that here as
a side effect of unblocking one PR is not a decision this module should
make silently. It stays an explicit, separately-reviewed operator action —
choosing to grant the scope, or to route workflow-touching changes to a
principal that already holds it — and this change's job is only to make
that choice legible: a `workflow_scope_missing` reason a router or a human
can act on, instead of an opaque `push_failed` retried forever unchanged.

## Rejected alternatives

- **Grant `workflow` scope to the worker token now.** Fixes the symptom but
  permanently broadens what every autonomous worker can push, for every task,
  not just the ones that need it — see above.
- **Silently drop `.github/workflows/**` changes from the candidate diff.**
  Would make the publish "succeed" while quietly discarding part of the
  requested change — worse than a legible refusal.
- **Leave it as a generic `push_failed` and rely on the operator reading raw
  stderr.** This is the status quo the bug report is about: indistinguishable
  from a lease race or a network blip, so nothing downstream can act on it
  differently.

## Operational cost and revisit condition

No deployment change. Revisit if a worker principal that legitimately needs
to publish workflow changes routinely (a dedicated CI-maintenance task class,
say) is introduced — at that point granting `workflow` scope to *that*
principal specifically, rather than the general worker token, is the
matching-blast-radius fix.
