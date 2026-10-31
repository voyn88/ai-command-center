# ADR 0011 — The headless worker service: payload contract, lease lifecycle, and dispatch guards

Status: **Accepted, implemented.** This ADR is written after the fact, to give the headless worker
service (`VOYN-W0-AICC-SRV-05`) an architecture-tier record. The code it describes is already on
`main`: `command_center/worker/` (the claim-execute-report daemon, the versioned payload contract,
the full-lifecycle writer lease, the worktree preflight guard) and `deploy/systemd/aicc-worker.service`.

## Context

Before this slice, `queue_claim`/`queue_heartbeat`/`queue_complete`/`queue_fail` (migration
`0002_queue_claim`) had no caller outside tests, and there was no process that could run an
autonomous agent task without a human-attended `streamlit` session. `VOYN-W0-AICC-SRV-05` is the
daemon that claims a `work_item`, dispatches it to `agent_runner.run_claude_code`, and reports the
outcome, running unattended under systemd on a host with no console.

Three questions had to be answered with an architecture-tier record, not left to the code alone,
because each has already produced a wrong answer once during review of this same task:

1. **What does a claimed payload mean, and how long is a worker allowed to run one?** — the subject
   of the first rejection on this task (PR #424), which shipped a payload-contract bound whose stated
   rationale contradicted the ADR's own account of lease renewal.
2. **Which lease may a mutating dispatch actually rely on?** — the subject of the second rejection
   (PR #486), which described `worktree_lease.blocking_lease` as usable in place of
   `writer_lease.hold`.
3. **What keeps the daemon's own liveness legible under a process supervisor that cannot see inside
   it?** — answered by the systemd unit's watchdog wiring, included here because it is load-bearing
   for the two lease mechanisms above: a wedged daemon must be restarted, not left holding a lease
   open forever.

## Decision

### 1. The `agent_run` payload contract is versioned and refuses unknown shapes

`command_center/worker/payloads.py` parses every claimed payload against an explicit, versioned
schema (`AGENT_RUN_SCHEMA_VERSION = 1`). A `v` this worker does not implement is a **non-retryable**
refusal: redelivery cannot make a worker understand a schema it does not ship, so the item
dead-letters with the reason stated, which is the operator's signal to upgrade the worker or
re-enqueue under a version it supports. Every other defect (missing field, wrong type, an
out-of-range bound) is likewise `PayloadError` — data, not an exception — mirroring the queue
protocol's own refusals-as-data discipline.

### 2. `timeout_seconds` is bounded by the agent-run ceiling, not the queue's visibility window

`payloads.py` refuses any `timeout_seconds` outside `[30, 3600]`. That range is **not** derived from
the queue's own lease mechanism; `payloads.py` imports `agent_runner.MIN_TIMEOUT_SECONDS` /
`MAX_TIMEOUT_SECONDS` directly rather than re-typing the values, so it is the same single constant —
the existing hard cap on how long a single `run_claude_code` invocation may run, which
`resolve_timeout` and `timeout_for_task` already clamp every non-worker caller to. The payload
contract enforces that cap at parse time, where the operator can see *why* a request was refused,
instead of letting `agent_runner` silently narrow an out-of-range value and having the run time out
opaquely with no record of the original ask.

This is a distinct mechanism from the queue's visibility lease, and the two must not be conflated:

- `work_queue_store.claim`'s `visibility_seconds` (default 300, clamped server-side to `[1, 3600]`
  by `queue_claim`) is the width of a single lease window on the claimed `work_item` row.
- That window is **renewed, not lengthened**, by a heartbeat thread that runs beside the handler for
  the entire duration of a run (`WorkerDaemon._heartbeat_loop`, firing at a third of the visibility
  window) — the daemon docstring's own words: "the heartbeat runs beside the handler, not inside
  it... [it] renews at a third of the visibility window." A run many multiples of 300 seconds long
  survives on live heartbeats exactly as well as a 60-second one; only a *lapsed* heartbeat (the
  process wedged, the database unreachable) ends the lease early.
- A payload's `timeout_seconds` therefore never "outlives" the queue's visibility lease in the sense
  of exceeding a fixed budget that lease enforces — a heartbeating worker can hold the row for as
  long as `agent_runner` lets the agent run, and no longer. The two ceilings coincide at 3600s
  because that is where `agent_runner`'s own cap already sits, not because the queue could not
  support more.

An earlier draft of this contract stated the reverse — that a timeout beyond the range "would
outlive any lease its worker can hold" — which described a lease that cannot renew. That draft is
superseded by this section; `payloads.py`'s comment and refusal message now cite `agent_runner`'s
run-length ceiling by name.

### 3. Provenance defaults closed: an undeclared payload is untrusted

`untrusted` defaults to `True` when absent. The queue is writable by the whole control plane, so a
payload that forgot to declare its provenance must not be the one that silently receives the
mutating sandbox profile; `handlers.py` reads `untrusted` to select between the read-only and
mutating execution profiles the runner already provides.

### 4. The worktree dispatch boundary has two independent guards with different authorities

Before a mutating dispatch provisions a workspace, `handlers.py` checks, in order:

**`worktree_lease.blocking_lease(isolated_workspace)` — a read-only, non-acquiring preflight.** Its
own module docstring states the scope precisely: "it never acquires anything: the worker is not the
lease holder and must not become one here." It answers exactly one question — is this path already
covered by some other live lease, right now — and answers it fail-closed (any failure to ask blocks
the dispatch). Because it never acquires, a `blocking_lease` pass has a check/use race by
construction: nothing stops another writer from acquiring the path between the check and the
provisioning that follows it. It is a cheap, early rejection of the common case, not a guarantee.

**`writer_lease.hold(repository, cfg, lease_lost)` — the full-lifecycle writer lease, and the only
mechanism that confers mutation authority.** Acquired (via the shared `voyn-lease` tool) *after* the
preflight passes and *before* `provision_and_verify`, held through the agent run and any local
test/lint step, and released only when the handler's `finally` closes the stack — this is what
closes the window `blocking_lease` explicitly does not cover. It renews in a background thread at a
third of its TTL (mirroring the daemon's own heartbeat shape); a renewal failure sets the same
`lease_lost` event already wired into `run_claude_code` as `cancel_event`, so losing this lease
forcibly cancels the running agent through the identical path a lost queue-visibility lease already
uses. It is scoped per task, not per repository (`VOYN-W0-AICC-LEASE-SCOPE-PER-TASK`), because two
different tasks in two different worktrees share no mutable state during a run and must not refuse
each other.

These are not interchangeable, and the ordering is deliberate: the preflight is cheap and rejects
the obvious case before any lease acquisition or workspace provisioning is attempted; the
full-lifecycle lease is what a mutating dispatch actually depends on for correctness for the entire
window it is exposed. A dispatch that skipped straight to `blocking_lease` and treated its pass as
sufficient would provision, run, and let an agent write into a workspace with no lease actually held
against it — exactly the race `blocking_lease`'s own docstring disclaims responsibility for.

### 5. The systemd unit makes daemon liveness legible to its supervisor

`deploy/systemd/aicc-worker.service` runs `Type=notify` with a watchdog fed from both the claim loop
(between claims) and the heartbeat thread (during a run), so every healthy state pings within one
heartbeat interval (`visibility_seconds / 3`, ~100s by default). `WatchdogSec=240s` is two missed
intervals plus slack; a wedged process (a handler that hangs and takes the heartbeat thread with it)
is restarted, and that restart is safe by the same construction as a SIGKILL — lease expiry plus the
control-plane reaper resume the item elsewhere. `TimeoutStopSec=330s` outlives one visibility window
so a handler that finishes within its first lease survives a graceful shutdown intact; a
longer-running handler is abandoned at the timeout, which is safe but not free (the item re-runs).

## Consequences

- **Future mutating callers must acquire `writer_lease.hold` before writing.** `worktree_lease
  .blocking_lease` is reserved for preflight use only — a fail-closed, non-acquiring check performed
  before any lease is taken — and must never be treated as sufficient authority to mutate a
  workspace. Any new dispatch path that mutates a checkout follows the same order as §4: preflight
  with `blocking_lease`, then acquire and hold `writer_lease.hold` for the full window it writes in.
- `payloads.py`'s timeout bound is `agent_runner.MIN_TIMEOUT_SECONDS` / `MAX_TIMEOUT_SECONDS`,
  imported rather than re-typed, so there is no separately maintained constant to drift out of sync;
  a future change to `agent_runner`'s cap moves the payload contract's bound with it, with no second
  edit required.
- The queue's visibility window and the payload's run-length ceiling are documented as two
  independent numbers that presently share a value (3600s) by coincidence of design, not by a shared
  mechanism. A future change to either must not assume moving one moves the other.
- Operators reading a dead-lettered `agent_run` refusal for an out-of-range timeout now see
  `agent_runner`'s run-length ceiling named as the reason, not the queue's visibility mechanism —
  the two rejections on this task both trace back to that mislabeling being propagated into
  operator-facing text.

## Non-goals

- **Changing either ceiling.** This ADR records the existing values (`[30, 3600]`) and their correct
  provenance; it does not argue either bound should move.
- **A third lease mechanism.** §4 is a correction of terminology and calling discipline for the two
  mechanisms that already exist, not a proposal for a new one.
- **The `execution` payload kind.** The daemon's handler registry is deliberately open to a future
  payload kind whose schema and producer do not exist yet; this ADR covers only the `agent_run`
  kind shipped today.

## References

- `command_center/worker/payloads.py` — the `agent_run` payload contract; its `_MIN_TIMEOUT_SECONDS` /
  `_MAX_TIMEOUT_SECONDS` are aliases of the imported `agent_runner` constants, not independent values
- `command_center/agent_runner.py` — `MIN_TIMEOUT_SECONDS`, `MAX_TIMEOUT_SECONDS`, `resolve_timeout`,
  `timeout_for_task`
- `command_center/db/work_queue_store.py` — `claim`/`heartbeat`, `visibility_seconds`
- `command_center/worker/daemon.py` — `WorkerDaemon._heartbeat_loop`, the watchdog feed
- `command_center/worker/writer_lease.py` — `hold`, the full-lifecycle writer lease
  (`VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE`)
- `command_center/worker/worktree_lease.py` — `blocking_lease`
  (`VOYN-OPS-WORKER-DISPATCH-INTO-LEASED-WORKTREE`)
- `command_center/worker/handlers.py` — the dispatch boundary ordering `blocking_lease` then
  `writer_lease.hold`
- `deploy/systemd/aicc-worker.service` — `WatchdogSec`, `TimeoutStopSec`
- `tests/worker/test_handlers.py`, `tests/worker/test_writer_lease.py` — the executable form of the
  rules above
