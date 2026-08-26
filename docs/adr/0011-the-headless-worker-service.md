# ADR-0011: the headless worker service

Status: **Accepted for `VOYN-W0-AICC-SRV-05`; implemented and deployed.** This ADR is written
after the fact — the service shipped across slice 1 (`#322`), slice 2 (`#323`) and a run of
follow-up hardening (`#337`, `#355`, `#358`, `#361`, `#365`) with no ADR recorded at the time. It
exists now to close that gap: the record of the design, and of what proves it, not a proposal for
new work.

## Context

`command_center/db/sql/0002_queue_claim.up.sql` (`VOYN-W0-AICC-SRV-04b`) shipped a complete
claim-execute-report protocol in PL/pgSQL — `queue_claim`, `queue_heartbeat`, `queue_complete`,
`queue_fail`, `queue_reap` — with no caller outside its own test suite. The protocol answers "who
is executing attempt N of work item W", deliberately independent of the repository writer lease
(`repo_lease`, which answers "who may mutate repository R"; see the migration's own header). A
protocol nobody calls is not infrastructure yet. This ADR covers the two things that made it one:
the daemon that calls it, and the bridge from a claimed payload to a real agent run.

This is a different queue than the one ADR-0007 covers: ADR-0007 dual-writes the desktop-facing
`data/execution_queue.json` into a SQLite `runtime.db.queue_entry` mirror. `work_item`/
`work_attempt` here are PostgreSQL tables under a separate protocol, with no shared code and no
shared authority — the two should not be conflated by "queue" alone.

## Decision

### The daemon: claim, heartbeat, drain, die

`command_center/worker/daemon.py` (`WorkerDaemon`) is a claim loop built to be killed at any
moment, not merely to tolerate being killed:

- **Identity is the connection.** The claimant is `session_user`, set by a trigger the daemon
  cannot influence (`work_attempt_claimant_is_derived`). The daemon carries no identity argument
  of its own; it is whatever per-host `aicc_w_*` role its DSN authenticates as. Enrolment is an
  operator act, out of scope for this daemon.
- **The heartbeat runs beside the handler, not inside it.** A blocking handler must not silence
  the heartbeat. The beat thread renews at a third of the visibility window and raises a
  `threading.Event` (`lease_lost`) the instant the database answers `attempt_superseded` — the
  same fence `_queue_owns` enforces server-side (`0002_queue_claim.up.sql`).
- **Shutdown finishes the item in hand.** SIGTERM stops claiming; the attempt already held runs to
  completion inside systemd's `TimeoutStopSec`. A second signal, or the timeout's SIGKILL,
  abandons it — safe by construction, because lease expiry plus `queue_reap()` (a scheduled job,
  not a human) recover the item elsewhere without requiring proof the old worker died.
- **Drain and credential reload are separate signals from shutdown.** SIGUSR1 closes the claim
  gate for an in-place credential rotation without exiting the process; a dedicated coordinator
  thread ACKs the drain through `sd_notify` only once it holds the same lock the claim loop takes
  before calling into the store, so the ACK cannot race an in-flight claim. SIGHUP reloads
  credentials and reopens the gate. Both exist because the daemon's failure story for a *rotated*
  credential is "stop, do not retry" (below), and rotating a live fleet without that story would
  mean restarting every worker to pick up a new secret.
- **The watchdog is fed from both threads, because each covers the other's blind spot** (`SRV-06`):
  the claim loop pings between claims but blocks for the whole of a handler's run; the heartbeat
  thread pings during exactly that run. A wedged process — a handler that hangs and takes the beat
  thread with it, or a claim loop stuck in a driver call — misses two pings and systemd restarts
  the unit, which is safe by the same lease-expiry-plus-reaper construction as SIGKILL.
- **Auth failure means stop, not retry.** A refused connection may mean the credential was rotated
  out from under the worker; hammering the server with a dead secret is indistinguishable from an
  attack, so the daemon exits non-zero (`RestartPreventExitStatus=2` in the unit) and leaves
  restart pacing to systemd rather than looping itself.
- **An unknown payload kind is a non-retryable failure**, decided by the handler registry
  (`build_handlers()`), not the daemon: redelivery cannot make a worker understand a schema it
  does not ship.

### The bridge: payload contract and outcome discipline

`command_center/worker/payloads.py` defines the `agent_run` payload contract, versioned from day
one (`AGENT_RUN_SCHEMA_VERSION`). A `kind` selects the handler; a `v` this worker does not
implement is refused, non-retryably, at parse time — an explicit dead letter with a stated reason,
never undefined behaviour a future worker has to reverse-engineer. Bounds (timeout, backlog task
id shape) are refusals, not silent clamps: a payload asking for a timeout past the queue's own
visibility ceiling would outlive any lease its worker can hold.

`command_center/worker/handlers.py` executes `agent_run` through the existing
`agent_runner.run_claude_code` rather than a second runner — sandbox-profile selection
(provenance-aware downgrade to read-only), VCS-credential scrubbing and timeout handling stay
where they already lived. The bridge owns only payload validation, repository validation, and
folding the run into a `HandlerOutcome`. Its outcome discipline is the load-bearing design
decision:

- a payload defect (bad version, missing fields, unknown repository) is **non-retryable** —
  redelivery cannot repair data;
- a run that *executed* is **ok regardless of the agent's own exit** — "the agent failed the task"
  is a result for the control plane to read, not a queue-level failure to redeliver, since
  retrying a completed mutating run would re-apply its side effects;
- only the case where **execution never started** stays retryable — an OS error before the
  process launched, or the executor's own infrastructure failing before any task work happened
  (rate limit, auth, overload), both distinguished from a genuine task failure by
  `agent_runner.RunResult.is_executor_api_error`.

Untrusted payloads requesting mutating task types are refused with the reason named, never
silently downgraded to read-only — a downgraded mutating prompt half-executes and looks completed,
which is a worse failure mode than a clean refusal. Result rows carry bounded stdout/stderr tails;
the full transcript stays on the worker host's journal, because a `jsonb` column is a coordination
record, not a log store.

### The writer lease: two lease systems, composed, not merged

The queue-visibility lease (above) answers "who is executing this attempt". A mutating run
additionally needs "who may write to this repository", answered by the external `voyn-lease` tool
and deliberately kept as a second, independently-recoverable protocol (the migration's own header
states why: proof-of-death and pure-timeout recovery have to differ, or a SIGKILLed worker on an
unreachable host blocks its task forever). Three modules compose around it:

- `command_center/worker/worktree_lease.py` (`blocking_lease`) is a **read-only preflight**: it
  never acquires anything, because the worker dispatching into a path is not that path's lease
  holder. It answers "is the worktree I am about to write into already held by a foreign writer"
  fail-closed — every failure to ask the authority (missing tool, non-zero exit, unparseable
  output, timeout) blocks the dispatch, because a guard that opens when it cannot see is not a
  guard. A lease held by one of the worker's own ancestors is not foreign (checked via
  `/proc/<pid>/stat` field 22, immune to pid reuse), which is what stops the guard from deadlocking
  its own supervisor's dispatch.
- `command_center/worker/lease_client.py` is the one place the `voyn-lease` argv shape is defined,
  shared by `writer_lease.py` and `orchestrator.publish` so the two callers cannot drift on how a
  lease row's owner is constructed. `acquire` always carries `--auto-takeover`: a holder whose
  process died without releasing — a killed or OOM'd worker, a task that hit its timeout — used to
  leave an expired-but-present row that blocked every later `acquire` for that repository forever
  (`VOYN-W0-AICC-LEASE-STUCK-EXPIRED-NO-RECLAIM`, live-traced to 173 of 216 `DEFER_TO_USER`
  escalations on 2026-08-22, the majority not genuine task failures). `--auto-takeover` asks the
  authority to verify the recorded holder is actually dead before granting takeover — never an
  unconditional override of a live lease.
- `command_center/worker/writer_lease.py` (`hold`) acquires the full-lifecycle writer lease
  **before workspace provisioning** and holds it — via a background renewal thread at a third of
  its TTL, mirroring the daemon's own heartbeat cadence — through provisioning, the agent run, any
  tests the agent runs, and `publish_run`. This closed a real gap: `publish_run` on its own only
  held the lease around its final `git push`, seconds at the end of a run that could hold the
  workspace open, unprotected, for the whole of `timeout_seconds` beforehand. A renewal failure
  sets the same `lease_lost` event the queue-visibility heartbeat already uses, so a lost writer
  lease forcibly cancels the running agent through the identical mechanism a lost queue lease uses
  — one cancellation path, not two. The lease is scoped **per task**, not per repository
  (`VOYN-W0-AICC-LEASE-SCOPE-PER-TASK`) — changed from repository scope after measuring, on
  2026-08-23, that 96 of 115 return-to-pool events were `VOYN_LEASE_REFUSED active` from unrelated
  tasks blocking each other with no physical resource conflict, once per-task worktree isolation
  meant they no longer needed to. `publish_run`'s own push-time lease stays repository-scoped,
  because the push itself genuinely needs one clone-wide writer.

Process isolation for the agent subprocess itself — the separate Unix principal, the transient
per-run UID, the credential broker — is ADR-0010's domain, not this one's; `handlers.py` reaches it
through `agent_runner.run_claude_code` and `agent_runner.principal_isolation_required()` without
re-deciding any of it here.

### Deployment posture

`deploy/systemd/aicc-worker.service` (a single canonical unit) and `voyn-aicc-worker@.service` (a
versioned, per-lane preprod template with `Type=notify-reload`) both harden the unit beyond what
the protocol itself requires: `NoNewPrivileges`, `ProtectSystem=strict`, a bind-mounted read-only
source path (not a bare `ProtectHome`, since the preprod clone lives under the operator's home
directory and the rest of it must stay invisible), no new capabilities, and a declared cgroup
resource envelope (`MemoryMax`/`MemoryHigh`/`CPUQuota`/`TasksMax`). The two units' `TimeoutStopSec`
differ for a stated reason: the canonical unit's 330s outlives one full visibility window plus
slack, while the `@` template's 3660s spans a full agent-run timeout ceiling, because its
`KillMode=mixed` lets SIGTERM reach only the daemon and not its child agent subprocess, which may
keep running past the daemon's own shutdown. A handler that finishes within its first lease
survives shutdown intact and a longer one is abandoned safely rather than corrupted. Acceptance of the
sandbox *directives* themselves stays measurement-from-inside per `SRV-05-B` and is deliberately
not claimed by this ADR — the resource envelope is enforced by cgroups regardless, which is a
narrower and already-verified claim.

## Proof

The design above is backed by tests that exercise the failure modes it names, not only the happy
path:

- `tests/worker/` holds 162 test functions across six files: `test_daemon.py` (claim/fail/raise,
  unknown-payload-kind refusal, lost-lease discard, SIGTERM/SIGUSR1/SIGHUP semantics, idle-backoff
  growth and reset), `test_handlers.py` (the outcome-discipline matrix above, one test per refusal
  reason), `test_writer_lease.py` (acquire failure, renewal loop, `lease_lost` propagation),
  `test_isolated_workspace.py` and `test_credential_rotation.py` (added after the initial two
  slices, covering per-task worktree isolation and the drain/reload protocol respectively), and
  `test_sdnotify.py`.
- `command_center/db/work_queue_store.py` is tested separately against real PostgreSQL as a real
  per-host worker role — round trip, stale-owner refusal after complete, retry incrementing the
  attempt with a fresh secret, and claim exclusivity through the store itself — skipped without a
  DSN and run in CI otherwise, so the protocol's own guarantees are checked against the database,
  not a mock of it.
- The original slice 2 review left three surviving mutants (a `timed_out` run silently marked
  `ok`/non-redelivered; a payload-defect site not pinned `retryable=False`; the CLI-preflight
  refusal path uncovered); all three now have a pinning test, verified by re-applying each mutant
  and confirming it fails.
- `ops/aicc_staged_worker_rollout.py`, covered by `tests/ops/test_aicc_staged_worker_rollout.py`,
  is the operational proof for a fleet-wide change: it discovers every `voyn-aicc-worker@`
  systemd lane, rolls it, and verifies the expected `ExecStart`, environment files and isolation
  drop-in are actually in effect before calling the rollout done — proof against the running
  system, not against the unit file's text.
- `ops/lease_reap.sh` (`VOYN-W0-AICC-LEASE-STUCK-EXPIRED-NO-RECLAIM`) is the independent,
  cron-driven backstop for the writer-lease side: it reaps any row the authority confirms is past
  expiry with a dead recorded holder, on a five-minute cycle, whether or not any worker is
  currently running — closing the failure mode that produced the majority of `DEFER_TO_USER`
  escalations on 2026-08-22.

## Consequences

- The queue-visibility lease and the writer lease are two independently-recoverable protocols with
  deliberately different recovery policies (timeout alone vs. proof of death), composed at the
  handler level rather than merged into one lock. Any future caller that needs "may I mutate this
  repository" must go through `writer_lease.hold` or `worktree_lease.blocking_lease`, not invent a
  third check.
- The handler registry (`build_handlers()`) is the seam for every future payload kind. A new kind
  ships its own parser (mirroring `payloads.parse_agent_run`) and its own outcome-discipline
  decisions; the daemon and the queue protocol do not change.
- `SRV-05-B` (measurement-from-inside acceptance of the systemd sandbox directives) remains a
  distinct, unclaimed piece of work. This ADR documents the resource envelope and hardening as
  configured, not as independently verified from inside the sandbox.

## Non-goals

- Re-deriving the `0002_queue_claim` SQL protocol's own guarantees — see the migration file's
  header, which is the authoritative source for the claim/heartbeat/complete/fail/reap semantics.
- A general executor-cascade design (`BO-S2a`) beyond the one hook (`_cascade_link`) this bridge
  exposes to it.
- Sandbox-directive acceptance from inside the unit (`SRV-05-B`, explicitly deferred above).
