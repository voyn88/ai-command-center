# Changelog

All notable changes to AI Command Center are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project does not yet follow strict semantic versioning tags in Git; versions below refer to
functional application milestones of `app.py`.

## [Unreleased]

### Fixed — the reaper's batch bound had stopped being a bound (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- `control-01:queue` reported `queue_stalled` again (`monitor_finding` #13145).
  The only change on this branch since the last healthy measurement is one
  line: `WorkQueueAdmin.REAP_BATCH` went from `100` to `10**9`, with no test,
  no changelog and no argument, directly contradicting the comment two lines
  above it ("Small enough that an interrupted tick loses a batch rather than a
  backlog") and the whole of 0028. `10**9` is a legal `integer`, so nothing
  refused it — `queue_reap(1000000000)` is simply the unbounded reap under a
  bounded arity's name. Reverted to `100`.

  **MEASURED against a real PostgreSQL 16 server.** 1000 lapsed leases, the
  tick cancelled 100ms in — which is how `aicc-queue-reaper.service`'s
  `TimeoutStartSec=60s` kill arrives, and how the restart of the tunnel the
  credential rotation cycles arrives:

      REAP_BATCH = 100    tick killed after 104.8 ms -> recovered 600 of 1000
      REAP_BATCH = 10**9  tick killed after 105.5 ms -> recovered   0 of 1000

  Same server, same backlog, same interruption, the same ~100ms of work done
  in both: the bounded tick had banked six durable batches, the unbounded one
  rolled back every expiration it had performed. That is 0028's defect
  verbatim — "the older unbounded form recovered nothing at all when it was
  interrupted, not everything up to where it stopped" — and the tick is
  interruptible by design.

  **Why this is the finding and not merely a regression.** `evaluate` weighs
  two starvation clocks and only one of them is unconditional: due ready work
  is excused by a full fleet and bounded by the fleet clock, while
  `lapsed_claim_age_seconds` is neither. No lane is holding a lapsed claim, so
  no lane being free says anything about it; only the reaper clears it, and no
  lane restart and no `queue_redrive` — which reaches only items already
  `dead` — is an exit. A reaper that commits nothing is therefore not a slow
  reaper: it is that age climbing without limit, and `queue_stalled` with
  nothing the fleet can do about it.

  The normal case hides it, which is why it could ship. A handful of lapses
  comes back short of any bound and is committed by the first call, so a quiet
  queue reaps identically at `100` and at `10**9`. The two diverge only once
  there is a backlog — after a worker host is lost, after a rotation window,
  after a database blip — at exactly the moment recovery is the thing being
  measured.

- **The suite could not see it, and that is the part worth fixing.** Every
  existing batching test sizes its backlog as `WorkQueueAdmin.REAP_BATCH + 3`.
  They are proofs about the LOOP and they read the constant, so they follow it
  wherever it goes: at `10**9` they do not go red, they stop being runnable —
  a backlog of a billion claims is a gate that hangs, not a test that fails.
  0028's fix is the loop AND a bound small enough to make the loop mean
  something; only the loop was pinned.

  `tests/db/test_reap_bound.py` pins the other half, from outside the
  constant. It needs no server — deliberately, since the regression reached
  HEAD past a suite whose only witnesses to it could not be collected without
  one — and it names both edges:

    * **Ceiling.** One batch is what an interruption throws away, so it must
      be a small fraction of the tick: at a measured 0.206 ms per expiration
      and its audit row (rounded to 1 ms, because production crosses
      `voyn-aicc-pgtunnel.service` to a loaded server), a batch may spend at
      most a tenth of the kill budget. `TimeoutStartSec` is parsed from
      `deploy/systemd/aicc-queue-reaper.service` rather than restated, so the
      ceiling follows the unit if the tick is ever retuned.
    * **Floor.** `REAP_BATCH <= 0` is the same stall in a worse shape, found
      while deriving the first edge: `queue_reap` clamps its own argument
      (`greatest(p_max_items, 1)`) while `reap()` tests termination against
      the bound it ASKED for, so the two never agree — the server reaps one
      and answers `1`, `1 < 0` is false; the queue empties and answers `0`,
      `0 < 0` is false. Reproduced in-process: still looping after 51 calls
      against an empty queue. The tick would spin until systemd killed it,
      recover nothing, and do it again the next minute.

  Each edge fails on exactly one mutation and neither fails on the other.

### Fixed — the reaper expired leases that were still live (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- **`queue_reap`'s re-test under the item's row lock dropped half the predicate
  it was re-testing.** Migration 0029. The scan selects on two conditions,
  `state = 'active' AND visible_until <= now()`; the re-test carried only the
  first, under 0002's comment "a completion may have landed since the scan". A
  completion is not the only thing that can land in that window, and
  `queue_heartbeat` is the other one: a beat moves `visible_until` and never
  touches `state`, so a renewal that committed between the scan and the lock
  passed the re-test and had its lease expired anyway.

  The window is structural. The scan is one statement, read at the
  transaction's snapshot; the re-test is a later statement in the same READ
  COMMITTED transaction and sees everything committed since. Between them sits
  the rest of the loop — up to `WorkQueueAdmin.REAP_BATCH` items, each a row
  lock, two UPDATEs and an audit INSERT. 0028's `SKIP LOCKED` narrowed it and
  could not close it: while a beat's transaction is open it holds the item
  `FOR UPDATE` (`_queue_owns`), so a reap arriving *then* defers the item,
  which is correct. What remained is the beat that has already COMMITTED and
  released — the reap finds the row free, finds the attempt `active`, and
  expires a lease with time left on it.

  **MEASURED against a real PostgreSQL 16 server**, with the beat's
  transaction pinned open across the expiry instant and the reap blocked
  mid-loop on another attempt's row:

      queue_heartbeat -> (ok = true)     lease renewed, an hour of it left
      queue_reap      -> 2
      beating attempt: state = expired, visible_until > now()
      beating item:    state = ready,   current_attempt_id = NULL

  An attempt marked `expired` while its lease is still in the future, and an
  item handed back to the queue out from under the lane running it.

  **What it costs, none of which is the reap's to give away.** The lane is
  still executing: its next beat and its `queue_complete` both resolve to
  `attempt_superseded`, so `worker.daemon._execute` discards an outcome worth
  up to one whole `TimeoutStopSec=3660s` attempt. The item is `ready` again and
  any lane may take it, so the work is done twice — the one-owner promise holds
  on paper (the fence refuses the loser's *write*) while the side effects have
  already re-run. And `queue_claim` charged `attempt_count` for the delivery
  that was taken away; `queue_fail_lease_wait` is the only refund and nothing
  on this path calls it, so enough repeats dead-letter a healthy item
  (`dead_letter_growth`), reachable after that only by an operator's
  `queue_redrive`.

  **Why it is filed under the stall.** 0028 gave the reaper liveness because
  `lapsed_claim_age_seconds` is the one starvation class
  `infra_monitor.evaluate` neither excuses by capacity nor bounds by the fleet
  clock — "only the reaper" clears it. A reaper that also expires LIVE claims
  is not merely failing to recover; it mints that class one attended claim at a
  time, and every occurrence pushes an item back onto the due-ready pile the
  same probe weighs. It fires when a beat straddles its own deadline — begun
  live, committed lapsed — which is the fleet coming *back* from a database
  blip or a `voyn-aicc-pgtunnel.service` restart that cost it two beats. That
  is exactly the moment the lane is recoverable and the reap is most likely to
  be running.

  The re-test now carries both halves, against the tick's own `now()` (the
  value the scan compared against), so a lease renewed past this tick is left
  alone and an attempt that lapses *during* a reap is next tick's work — the
  same answer 0028 gives a contended row, for the same reason: recovery a
  minute late costs a minute, recovery that is wrong costs an attempt.
  `CREATE OR REPLACE` on the bounded arity only; `queue_reap()` is one line
  over that body (0028) and inherits the fix without being re-declared.
- **Both halves of that re-test are now pinned, including the one 0002 wrote.**
  Deleting `state = 'active'` from it left the entire suite green — the same
  shape of unproven guard this branch has been closing everywhere else, and a
  regression that only pinned the new half would have repeated the original
  mistake. `test_a_lease_renewed_mid_reap_is_not_expired_anyway` and
  `test_a_completion_landed_mid_reap_is_not_expired_over` stage the two
  interleavings against a real server with nothing simulated: a real
  `queue_heartbeat` / `queue_complete` from a transaction that opened while the
  lease was live (`transaction_timestamp()` freezes there, which is what makes
  it a call that raced its own deadline), committing while a real `queue_reap`
  is blocked mid-loop on another attempt's row. The block is what makes the
  interleaving deterministic rather than a race the suite would only sometimes
  lose, and it is waited for in `pg_stat_activity` rather than slept at. Each
  mutation fails exactly one of them.

### Tested — the reaper's own entrypoint, not just the method behind it (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- **`aicc-queue-reaper.service` execs a line nothing in this repository ran.**
  The entries below prove `WorkQueueAdmin.reap` against a real PostgreSQL
  server as `aicc_app`. What systemd starts every minute is

      ExecStart=/opt/aicc/.venv/bin/python -m command_center.db queue-reap

  and `tests/db/test_cli_queue.py` reached that subcommand at the argparse
  layer and stopped there. Between the two sat a seam nothing exercised —
  and it is not incidental plumbing, because the properties this branch's
  recovery fix rests on live in it rather than in the method:

  * **The connection is autocommit.** Batching is only a fix because each
    `queue_reap(REAP_BATCH)` is durable before the next starts, so "an
    interruption costs one batch". That is a fact about `pool.connection()`
    (`autocommit=True`), not about the loop — handed a transactional
    connection, `reap` returns the same count while an interrupted tick
    throws away the whole backlog again, which is the exact defect 0028
    removed.
  * **The identity is `aicc_app`.** Recovery is deliberately not a worker
    privilege and 0028's `GRANT EXECUTE … queue_reap(integer)` names that
    role; the CLI derives it from `AICC_PG_*`, not from a DSN a test built.
  * **And it is the only exit from the one starvation class the probe weighs
    unconditionally.** `evaluate` excuses due ready work behind a full fleet
    and bounds it by the fleet clock; `lapsed_claim_age_seconds` is neither
    excused nor bounded, and no lane restart and no `queue_redrive` reaches
    a lapsed claim.

  So a regression anywhere in that seam is `queue_stalled` on
  `control-01:queue` with no exit reachable by fleet action, while every
  existing test stays green. `test_the_reaper_timers_own_entrypoint_recovers_a_lapsed_claim`
  now drives the unit's actual command end to end against a real server —
  claim, lapse the lease, `cli.main(["queue-reap"])`, item claimable again
  with its attempt numbering continuing — and pins the ExecStart's module and
  subcommand to the command it proves. Verified to have teeth by mutation: a
  non-autocommit pool, a CLI that stops calling `reap`, and an ExecStart that
  drifts to another subcommand each fail it.

  Same reason `test_readyz_is_200_against_a_real_database` exists on the
  serving side: the unit tests there monkeypatched `pool.connection` and so
  stayed green while the served process never opened the pool at all.

### Fixed — a recovery fault must surface, not be answered with the bug it replaced (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- **`WorkQueueAdmin.reap`'s fallback caught every first-batch failure, and the
  two it caught by mistake are the two 0028 exists for.** Found auditing the
  batching this branch had just added, and it is on the one path the probe
  measures with no escape clause: `evaluate` excuses due ready work behind a
  full fleet and bounds it by the fleet clock, but `lapsed_claim_age_seconds`
  is weighed unconditionally, and `queue_reap` is the only thing that clears a
  lapsed claim. Restarting a lane does not reap, and `queue_redrive` only
  reaches items already `dead`.

  The fallback exists so a control host newer than its database still reaps:
  `queue_reap(integer)` is absent before 0028, so the call is retried against
  the unbounded arity. It was written as a bare `except Exception` on the
  first batch. Measured against PostgreSQL 16 as `aicc_app`, three different
  faults reach that handler:

      statement timeout            QueryCanceled          57014
      GRANT missed the new arity   InsufficientPrivilege  42501
      database still at 0027       UndefinedFunction      42883

  Only the last one may be answered by re-running the unbounded form.

  **57014 is the worse miss.** The tick is interruptible by design —
  `aicc-queue-reaper.service` is a `Type=oneshot` with `TimeoutStartSec=60s`,
  and the connection crosses the tunnel the credential rotation restarts — and
  an interruption arrives as a cancellation. Answering it with the unbounded
  arity re-runs the whole-table scan whose all-or-nothing rollback is the
  exact defect 0028 removed, over strictly MORE rows than the batch that just
  failed, at the one moment the server has already shown it cannot finish that
  much work. The fallback reintroduced the bug precisely when it mattered.

  **42501 it swallowed silently.** A role re-provision that misses 0028's
  `GRANT EXECUTE ... queue_reap(integer)` leaves the no-argument arity working
  (it carries 0002's grant) and the bounded one refused. Measured: `reap()`
  returned a count and reported success while a real deploy fault went
  unrecorded — on the path whose absence becomes a `queue_stalled` finding
  with no exit reachable by fleet action.

  Now keyed on SQLSTATE `42883` alone. Keyed on the code rather than an
  exception class because this module never imports psycopg — it is handed a
  connection — and a driver that reports no SQLSTATE gets the raise, which is
  the fail-closed answer. Two regression tests against a real server pin both
  halves; both fail against the previous code, one on each mis-caught fault.

### Fixed — recovery must make progress, not merely avoid corruption (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- `control-01:queue` reported `queue_stalled` again (`monitor_finding` #3321).
  Every fix on this branch has closed one way for the fleet to end up UP AND
  CLAIMING NOTHING. This one closes the other half of the finding: not the
  lanes, but the RECOVERY PATH those lanes depend on — the only exit from the
  one starvation class no lane restart and no redrive can reach.

  **Why the reaper is what `queue_stalled` is measuring.** `evaluate` weighs
  two clocks, and only one of them is unconditional:

      starved_ages = [queue.lapsed_claim_age_seconds]
      if spare_capacity: starved_ages.append(ready_due_starved)

  Due ready work is excused by a full fleet and bounded by the fleet clock. A
  lapsed claim is neither — "no lane is holding it, so no lane being free is
  irrelevant to it … only the reaper does, and the lapse age is what measures
  whether `aicc-queue-reaper.timer` (every minute) is still recovering." So
  for that half of the finding the probe is measuring the reaper directly,
  and a reaper that stops recovering is a `queue_stalled` with no exit
  reachable by fleet action: restarting a lane does not reap, and
  `queue_redrive` only reaches items that are already `dead`.

  **0002 shipped the safety property and stopped there.** Its header states
  the row lock as the reason a reap "cannot race a concurrent `complete()`",
  and `WorkQueueAdmin.reap`'s docstring repeated it as the reason "a missed
  tick delays recovery and never corrupts it". Both are true about
  CORRECTNESS. Neither is a statement about PROGRESS — and `queue_claim`'s own
  comment, two hundred lines above in the same file, draws exactly that
  distinction for the claim path:

      `SKIP LOCKED` BUYS THROUGHPUT, NOT CORRECTNESS … one slow claim
      transaction stalls every other claimer.
      `test_a_claimer_is_never_blocked_by_another_transactions_row_lock` pins
      that property, and pins it as liveness rather than dressing it up as
      exclusivity.

  The claim path has that liveness property and a test pinning it. The
  recovery path had neither, and it is the path where being stuck is
  unbounded rather than merely slow.

  **MEASURED against a real PostgreSQL 16 server.** Three items claimed,
  three leases lapsed, one of the three items' rows held by another
  transaction — any `_queue_owns` caller, a duplicate `queue_enqueue` since
  0027, a `queue_redrive`, a migration's `ALTER`, an operator at a psql
  prompt:

      reap interrupted: canceling statement due to statement timeout
      CONTEXT: while locking tuple (0,4)
      recovered by the interrupted reap: 0 of 3

  Both halves of that are the defect, and the second is the worse one. It
  WAITED on the held row rather than stepping past it, so one contended item
  stopped the recovery of every item behind it. And it is ALL OR NOTHING: the
  first item had already been expired and requeued before the block, and the
  interrupted transaction took that back with it. An interrupted tick
  recovers nothing — not "everything up to where it stopped" — so its work is
  not delayed, it is discarded, and the next tick starts from the same place.

  **And the tick is interruptible by design.**
  `aicc-queue-reaper.service` is a `Type=oneshot` with
  `TimeoutStartSec=60s`, so systemd kills a tick that runs long; the
  connection crosses `voyn-aicc-pgtunnel.service`, which the credential
  rotation restarts; and the scan is over every expired attempt in the table
  with no bound at all. Each of those turns a reap into a rollback, and every
  rollback returns the fleet to the state that produced it.

  **Migration 0028, in the two pieces that make recovery monotonic.**
  `FOR UPDATE SKIP LOCKED` on the item, exactly as `queue_claim` takes it, so
  a contended item is DEFERRED to the next tick (60 seconds) instead of
  stopping this one — and the deferral is AUDITED rather than silent, with a
  NULL `work_item_id` and the id in `detail`, because `_queue_audit` requires
  the caller to hold the item's row lock (0027) and not holding it is the
  whole reason the branch exists. Then a BOUNDED BATCH: `queue_reap(p_max_items)`
  stops at a number the caller chose rather than at the size of the table, and
  `WorkQueueAdmin.reap` loops batches on its autocommit connection so each is
  durable before the next begins. An interruption now costs one batch.

  Correctness is untouched. The re-test under the lock already handled
  "somebody else finished this attempt", and whoever holds the row is by
  definition one of the parties that resolves it — a completion, a claim, a
  redrive. Skipping is how the reaper waits for them without making every
  other item wait too.

  **WHY AN OVERLOAD, which is 0024's lesson applied.** `CREATE OR REPLACE`
  cannot add a parameter, so `p_max_items integer DEFAULT NULL` would define a
  SECOND function and make the no-argument call AMBIGUOUS — `function
  queue_reap() is not unique`, measured, and it breaks every existing caller.
  `queue_reap()` therefore keeps its exact signature and 0002's `EXECUTE`
  grant and delegates to the bounded form, so the reaper timer running from an
  older checkout keeps working AND inherits the liveness fix, because the fix
  is in the body both arities share. `WorkQueueAdmin.reap` falls back to the
  unbounded arity against a database still at 0027 for the same
  deploy-independence reason.

  **WHAT THIS DOES NOT CLAIM.** It is not the cause of any one `queue_stalled`
  reading. A reaper that is never interrupted and never contended behaves
  exactly as before, and this branch's earlier commits removed the causes that
  were producing the finding. It is why the lapsed-claim half of that finding
  had no reliable exit: the recovery path could lose a tick's work to a fault
  that had nothing to do with the items it was recovering, and nothing on the
  host would ever have said so.

- **The probe's poll floor was half the daemon's real poll ceiling.** Found
  while auditing the same route from "work enqueued" to "work attended", and
  NOT a cause of `queue_stalled` — it produces the sibling failure
  `throughput_stalled`, which 0024 made independently clearable.

  `CLAIM_POLL_CEILING_SECONDS` is "the longest a free lane can take to notice
  due work", and `throughput_stalled` — the one check that fires INSIDE the
  stall window — will not call anything younger than that starved. It was
  `30.0`, pinned by a test asserting only
  `>= WorkerConfig().idle_max_seconds`, which `30.0` satisfies. But
  `idle_max_seconds` caps THE BACKOFF, not the sleep:

      self._sleep(min(idle + random.uniform(0, idle), cap))
      idle = min(idle * 2, self._config.idle_max_seconds)

  Once the backoff saturates at 30s, every gap between polls is uniform on
  [30s, 60s), and `cap` (the watchdog half-interval, 120s for the unit's
  `WatchdogSec=240s`) never clamps it. So half of that range sat above the
  floor: an item enqueued onto a queue that had been quiet all night could sit
  30–60s unoffered and be read at the probe's next two-minute sample as
  `throughput_stalled` — precisely the reading the floor exists to prevent
  ("a queue that had been empty all night would otherwise go red the second
  the first task was enqueued"). Not a corner: reachable on every poll cycle.

  The floor is now `60.0`, and the test DERIVES the bound by running the
  daemon's own backoff to saturation instead of restating one of its
  constants, so a future change to either the cap or the jitter fails the test
  rather than the fleet. The shared `_queue()` fixture's starved age is
  derived from the constant for the same reason — it was a literal `60.0`
  chosen against the old ceiling, and raising the ceiling would otherwise have
  looked like a broken test instead of a moved band.

### Fixed — a drained lane must find its own way back (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- `control-01:queue` reported `queue_stalled` again (`monitor_finding` #3078).
  Every fix on this branch so far has closed one way for the fleet to end up
  UP AND CLAIMING NOTHING — a raising claim killing the lane, a poisoned
  head-of-line row nothing could claim past, a recovery path that reported
  success and did nothing. This is the last one of that shape still open, and
  the only one where the lane was never in trouble at all: it was told to stop
  claiming, and then nothing ever told it to start.

  **The reload that failed once and was never tried again.** `SIGUSR1` closes
  the claim gate (`_drain`), and the ONLY thing that reopens it is
  `_credential_reload_loop` clearing the flags after `reload_credentials()`
  returns. The request is cleared BEFORE the attempt — deliberately, so a
  `SIGHUP` arriving mid-reload is coalesced rather than dropped (review finding
  on `c4001c4`) — so a reload that RAISED had consumed the request and left
  nothing behind to retry it. The lane stayed drained for the life of the
  process.

  Nothing on the host notices, and that is what makes it an outage rather than
  an incident. The lane holds a working pool (`replace_pool` builds and
  authenticates the replacement BEFORE detaching the old one, so a failed
  rebuild leaves the previous pool intact), and `run_forever`'s draining branch
  feeds the systemd watchdog on every pass — so the unit is `active (running)`,
  its watchdog is fed, its database connection is fine, and it will never claim
  again. `Restart=` never fires because nothing ever fails.

  It is also the likeliest reload to fail: the rebuild runs against a
  credential the rotator has just changed, over a tunnel
  (`voyn-aicc-pgtunnel.service`) that the same rotation restarts, and
  `worker/__main__.py`'s `reload_credentials` raises on any of a missing
  `AICC_WORKER_ENV_FILE`, an unreadable or half-published env file, a config
  the loader refuses, or a replacement pool that cannot connect.

  That state is exactly what the probe reports, and reports correctly: lanes
  that are up but claiming nothing stop the fleet clock
  (`fleet_idle_seconds`), due ready work is attended by nobody, and
  `queue_stalled` opens with no exit reachable by fleet action — restarting is
  not something anything on the host will do for a unit that looks healthy.

  The reload now degrades the way the claim and report paths already do: log
  it, back off `idle_max_seconds` (the interval every other "cannot proceed
  yet" answer uses, because none of them is cured by asking again faster), and
  keep trying. The backoff waits on `_reload_stop`, so a shutdown during it is
  still prompt.

  **The gate is deliberately NOT reopened on failure.** The drain is the
  rotator's instruction to stop claiming while the credential it issued is
  retired, so a lane that resumed because its reload kept failing would claim
  under exactly the credential being invalidated. It stays shut until a reload
  actually succeeds — which now happens on its own, with no operator and no
  second `SIGHUP`, the moment whatever broke the rebuild is over. Both halves
  are pinned by their own regression.

- **Migration 0027 — the one audited path that held no lock.** Found while
  auditing the same route from "work enqueued" to "work attended", proven
  against a real server, and NOT a cause of the stall above. `_queue_audit`
  numbers `work_event.seq` as `max(seq) + 1` and states the precondition that
  makes it collision-free without a retry loop: "every caller passing a
  non-null work_item_id already holds that item's row lock". Every caller did
  — `queue_claim`, `queue_reap`, `queue_redrive` and everything reached
  through `_queue_owns` take the item `FOR UPDATE`, and `queue_enqueue`'s
  granted path inserts the item itself — except `queue_enqueue`'s DUPLICATE
  path, which resolved the existing item with a bare `SELECT`.

  The FK's `FOR KEY SHARE` is not that lock and is why the bug survived
  review: it does not conflict with itself, and it is taken by the audit's
  INSERT *after* the `max(seq)` subquery in the same statement has already
  been evaluated. So a duplicate enqueue settles its `seq` on one snapshot,
  waits behind whoever holds the item, and commits a number that party has
  since used:

      duplicate key value violates unique constraint "idx_work_event_item_seq"
      DETAIL:  Key (work_item_id, seq)=(wki_..., 3) already exists.

  The unlocked caller is always the loser, which is why this showed up as
  dispatch failing rather than as the queue protocol failing. And the race is
  routine, not exotic: the duplicate path is what an idempotency key is FOR
  ("the dispatcher may retry an enqueue after a timeout without knowing
  whether the first landed", 0002), `aicc-backlog-review.timer`,
  `aicc-backlog-merge.timer` and the planner all re-enqueue keys for work
  still in flight, and the counterparty needs no coincidence — the worker
  holding that item beats every ~100s (`visibility_seconds / 3`), and every
  beat is an audited, row-locked write to the same item. The exception aborts
  the caller's transaction, taking down whatever else that tick had batched
  with it, and loses the very refusal record the audit exists to keep —
  the lesson `queue_redrive`'s unknown-item branch already carries in its own
  comment.

  0027 resolves the existing item `FOR UPDATE`, so the one caller outside the
  precondition is inside it. The granted path and enqueues of different keys
  are untouched.

### Fixed — a claim that raises must not take the lane with it (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- `control-01:queue` reported `queue_stalled` again (`monitor_finding` #2873).
  It is the same measurement as #2840 in the entry below, whose root cause —
  a refunded lease wait leaving `attempt_no` taken, so `queue_claim` raised a
  unique violation on the oldest due row — migration 0025 removed; that fix is
  on this branch and not yet deployed, so the probe kept recording against the
  fleet still running the old function. What follows are the two defects found
  while VERIFYING that fix, both proven and both still open: the amplifier that
  made one raising claim a fleet-wide outage rather than one stuck item, and a
  recovery path that had gone quietly dead.
- Neither of these is the cause of the stall — 0025 is. This one is why the
  cause was not survivable.
- `self._store.claim(...)` was the one protocol call `run_forever` makes on
  EVERY iteration and the only one with no guard around it. Its three siblings
  had each been closed after the same failure: a raising report write
  (`_execute`, "it would propagate out of `run_forever`'s loop and kill the
  whole daemon over the one attempt it was reporting"), a raising handler
  (`_dispatch`), and a non-object payload. The claim was the worst place to
  leave open, because it fires BEFORE any item is in hand — so the cost is not
  one attempt but every attempt. The exception leaves the loop, the lane
  exits, systemd restarts it, and the next claim raises on the same row.
  `queue_claim` takes the OLDEST DUE ROW, so every lane selects it and dies:
  the lane crash-loops and claims nothing.
- That is the state the probe reads as a stall, and reads correctly. Lanes
  that are up but claiming nothing stop the fleet clock
  (`fleet_idle_seconds`), due ready work is attended by nobody, and
  `queue_stalled` opens with no exit reachable by fleet action — restarting a
  crash-looping lane only restarts the crash. Migration 0025 removed one
  cause; any next one does it again, and a dropped connection or a PostgreSQL
  restart is enough.
- The claim path now degrades the way the report path already does: log it,
  back off `idle_max_seconds` — the same interval every other unclaimable
  answer uses, because none of them is cured by asking again faster — and keep
  claiming. The backoff sits OUTSIDE `_claim_gate_lock` on purpose: the drain
  coordinator takes that lock to emit the `aicc-drained` ACK the credential
  rotator waits on, so sleeping under it would trade a dead lane for a wedged
  one. A transient fault now costs one poll; a permanent one leaves a lane
  that is up and visibly claiming nothing, which the queue probe still catches
  on the fleet-idle clock. Neither ends with the lane dead.
- Two regressions, both mutation-checked (both fail at the previous commit,
  where the exception propagates straight out of `run_forever`): a claim that
  raises the real unique-violation text, after which the lane must claim and
  complete the item behind it; and the gate lock proved free from inside the
  backoff itself.
- MEASURED, including what it does NOT buy. Driven against a real PostgreSQL
  16 server as the deployed fleet (2 lanes, 40-minute attempts, a 4-deep
  backlog, writer-lease contention, retries, lane deaths, the reaper on its
  1-minute timer, sampled every 2 minutes as the timer samples): 24 simulated
  hours in which every claim raises from hour 6 gives 530 `queue_stalled`
  samples, the first 22 minutes after the fault — the #2873 signature, and the
  mechanism by which a claim fault becomes this finding at all. Against an
  INTERMITTENT fault (30% of claims for 12 hours) the guarded and the
  crash-looping fleet both stay green, because two lanes on 40-minute attempts
  absorb a restart cycle; the guard is worth a poll instead of `RestartSec=10s`
  plus a cold start there, not a change of verdict. And a deterministically
  poisoned row stalls the queue either way — that is the cause, and removing it
  is 0025's job, not this one's.

### Fixed — the dead-letter queue had no exit for the class 0022 created (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- `queue_redrive` is "the DLQ's exit", written when a `work_item` had ONE
  budget: it widens `max_attempts` explicitly so each widening is a recorded
  act rather than a silent reset. 0022 gave the item a SECOND budget and a
  second way to die — `lease_wait_count` against the caller's
  `p_max_lease_waits`, dead-lettering as `lease_wait_exhausted` — and did not
  tell the redrive. The same omission 0025 fixed in `queue_claim`, in the same
  family, from the same migration.
- So for the items it most needed to recover, the exit REPORTED SUCCESS AND
  DID NOTHING. Measured against a real PostgreSQL 16 server (cap 2 for
  brevity): three lease refusals dead-letter the item with
  `lease_wait_count = 2`; `queue_redrive(item, 3)` returns true, audits
  `granted`, moves it to `ready` and widens `max_attempts` 3 → 6; one further
  refusal dead-letters it again. The count was still at the cap, so the very
  next writer-lease refusal — the exact condition that dead-lettered it, and
  the one most likely to still be true moments later — killed it, and the
  widened attempt budget was never touched because the item never died of it.
- The work stranded that way is FINISHED work: `lease_unavailable` names no
  fault in it, only that a sibling lane held the repository's writer lease,
  which is why 0022 exists at all. `backlog_dispatch` bounds concurrency by
  per-repository writer leases across three repositories with two lanes, so
  sustained contention is a state this fleet produces by design.
- Migration 0026 restores BOTH budgets on redrive, unconditionally: an item
  dead-lettered on `max_attempts` can carry a nearly spent `lease_wait_count`
  from earlier contention, and redriving it into a handful of refusals before
  the widened attempts are ever tried is the same defect wearing a different
  `dead_reason`. It does not weaken the rule it sits under — `lease_wait_count`
  counts contention against one delivery, not a property of the work; every
  redrive is audited, and the previous count now travels in that audit beside
  the previous `dead_reason`, so a redrive loop stays as visible as the
  `max_attempts` widening beside it. The attempt HISTORY is still not reset:
  `work_attempt` is untouched and 0025 still numbers the next delivery from it.
- The regression asserts the SECOND refusal after a redrive, not the first —
  with the budget unrestored the first already dead-letters, so a test that
  stopped at one would pass against the broken function. It also proves the
  bound still terminates: restoring a budget is not removing it.


### Fixed — one writer-lease refusal stopped the whole queue (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- `control-01:queue` reported `queue_stalled` again (`monitor_finding` #2840),
  and this time the probe was right: the queue really had stopped. Every
  earlier entry below made the MEASUREMENT honest; this is the thing it was
  honestly measuring.
- `queue_claim` derived the new `work_attempt.attempt_no` from
  `work_item.attempt_count`. That was free while the two moved together, and
  migration 0022 separated them: `queue_fail_lease_wait` exists so a lost
  writer-lease race does not spend the work's budget, so it requeues with
  `attempt_count = greatest(attempt_count - 1, 0)` while deliberately KEEPING
  the refunded `work_attempt` row — it is the audit trail 0002 promises, and
  `work_event` references it. The item then carries `attempt_count = N-1`
  beside an attempt that still holds `attempt_no = N`, the next claim
  recomputes `N`, and `UNIQUE (work_item_id, attempt_no)` refuses it:
  `duplicate key value violates unique constraint "idx_work_attempt_item_no"`.
- The cost is the whole fleet, not the one item. The exception aborts
  `queue_claim`, so the item is not merely un-retried but UNCLAIMABLE — and
  `queue_claim` takes the oldest due row (`ORDER BY priority DESC,
  available_at, created_at`), so it is head-of-line: every lane's next claim
  selects it and raises, and healthy work behind it is never reached.
  `worker.daemon.run_forever` has no handler for it either — it handles
  `QueueRefusal`, which an exception is not — so the lane crash-loops under
  systemd. One refusal of a kind the fleet is built to produce
  (`backlog_dispatch` bounds concurrency by per-repository writer leases across
  three repositories with two lanes; 0022's header records the live contention
  that motivated the refund) stops every queue consumer.
- That is why the finding kept reopening and could never be cleared by fleet
  action: the monitor measures due ready work that no lane is holding while the
  fleet clock stands still, which is exactly this state, and no amount of
  correct measuring or restarting can claim an item whose next `attempt_no` is
  already taken.
- Migration 0025 numbers the delivery from the history that constrains it —
  `attempt_no` is `max(attempt_no) + 1` over the item's own attempts, computed
  under the row lock the claim already holds, so the unique index stays the
  backstop its comment says it is rather than the thing that decides.
  `attempt_count` stays the budget. It is also the recovery: an item already
  poisoned carries `attempt_count = N-1` and a stuck `attempt_no = N`, and its
  next claim takes `N+1` and succeeds — no data fix-up, no redrive, no
  operator. Routing is unaffected: `worker.handlers._cascade_step` reads
  `attempt_no` modulo the cascade length and already tolerates a number that
  climbs independently of the budget (`queue_redrive` does the same), so a
  lease-wait retry advances a cascade link exactly as it did before 0022, when
  it was a plain `queue_fail(retryable => true)`.
- Nothing had exercised the round trip, which is how this landed: the daemon's
  routing to `fail_lease_wait`, the grant, and 0022's refund arithmetic were
  all covered, but no test CLAIMED THE ITEM AGAIN afterwards — the only place
  the collision can appear. Four regressions now do, each mutation-checked
  against the migration: the refunded attempt taking the next number, the
  head-of-line item that must not wedge the queue behind it, the lease-wait
  bound still terminating in the DLQ with every delivery numbered afresh, and
  — in the monitor's own suite — the stalled shape draining to a green probe.
  Re-driven as a simulation of the deployed fleet against a real PostgreSQL 16
  server (2 lanes, 40-minute attempts, a 4-deep backlog, retries, the reaper on
  its 1-minute timer, sampled every 2 minutes as the timer samples): 24
  simulated hours with writer-lease contention in the mix, zero red samples,
  where the same run without the migration dies at the first refusal.

### Fixed — the queue monitor called a working fleet stalled (`VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED`)
- The measurement spanned EVERY queue while the verdict knew exactly one fleet
  (`monitor_finding` #2766). `work_item.queue` is a real dimension —
  `queue_enqueue` writes it, `UNIQUE (queue, idempotency_key)` keys on it, and
  `queue_claim(p_queue, ...)` serves exactly the one queue it is given, which
  the lanes pass as `WorkerConfig.queue`. `_QUEUE_SNAPSHOT_SQL` filtered on
  none of it, so every class it reports — "claimable by a free lane", "no lane
  is holding it", "lanes doing their job" — was computed over rows belonging to
  fleets that do not exist, and then judged against `--claim-capacity`. Both
  directions were wrong, and the second is the one a fail-closed monitor must
  never get wrong:
  - A due `ready` row on any other queue counted as work this fleet was
    ignoring. No lane can ever claim it, so no amount of healthy fleet
    behaviour could retire the finding — an `open` `queue_stalled`, and the
    task the planner mints from it, with no reachable exit. "The monitor clears
    the finding when it measures healthy" was not a promise the measurement
    could keep.
  - FAIL-OPEN: claims on another queue counted toward this queue's capacity.
    Two attended claims anywhere in the table made `attended_claims == 2`,
    `spare_capacity` false, and a genuine hours-old unclaimed item on
    `execution` was excused as backpressure behind a fleet that was not serving
    it at all — the probe silent through exactly the stall it exists to catch.
  The statement now binds the queue name twice: once to the pending work, and
  once to the `fleet_idle_seconds` subquery added for #2471 — an attempt on
  another queue's item is another fleet's lane, and letting it wind this clock
  forward would excuse a stall of any age here through `min(due_age,
  fleet_idle)`, reintroducing the fail-open through the fix for the one before
  it. `--queue` defaults to the lanes' own queue, pinned to `WorkerConfig` by
  `test_the_probes_queue_default_matches_the_daemons_own` the way capacity is
  pinned to `deploy/aicc/worker-lanes`; the deployed `voyn-queue-monitor
  .service` therefore needs no new flag. A fleet running a second queue gets a
  second probe with its own capacity and finding source, because every
  threshold on that ExecStart line is already a fact about one queue's fleet.
  Regressions in both layers, all mutation-checked: at `evaluate`/`main` the
  default, the flag's path into the statement's parameters, and that the name
  is bound to BOTH questions; and against a real PostgreSQL server the foreign
  ready row, the foreign claims that must not excuse a stall, and the foreign
  attempt that must not wind the fleet clock.
- The capacity test gated the COMPARISON and left the CLOCK running
  (`monitor_finding` #2471). A due `ready` item's age is time since it became
  due, and the queue is designed to hold work no lane can attend yet, so that
  age climbs for hours while the fleet is legitimately full and invisible
  behind `attended_claims == claim_capacity`. The moment occupancy drops the
  same hours-old number is measured against 900s — and occupancy drops at
  every attempt boundary (`queue_complete` commits before the daemon's next
  claim), at every lane restart the 5-minute self-deploy tick issues, and at
  every drain. With the probe sampling every two minutes, `control-01:queue`
  goes red against lanes doing exactly their job, mints a task, and clears on
  the next tick — a monitor that cannot stay green for 24h on a healthy fleet.
  The stall clock is now bounded by how long the FLEET has been standing
  still: an item is only being ignored while there is somebody to ignore it.
  `_QUEUE_SNAPSHOT_SQL` reports `fleet_idle_seconds` — time since any lane took
  an item (`work_attempt.created_at`) or handed one back (an attempt leaving
  `active`) — and `evaluate` takes the lesser of that and the due age.
  Heartbeats are deliberately excluded: `queue_heartbeat` writes
  `updated_at = now()` on a row that stays `active`, and a clock a stalled
  fleet could wind forward by beating would excuse every stall there is. A
  lapsed claim is not bounded by it either — a neighbouring lane claiming away
  beside a zombie says nothing about the zombie — and `None` (no attempt has
  ever been made) bounds nothing, so a fleet that never started still fails
  closed. Regressions in both layers: the attempt boundary, the fleet that has
  not moved all window, the unmeasurable clock and the lapsed claim at
  `evaluate`; and against a real server, the boundary driven through
  `queue_complete`, a heartbeat that must not wind the clock, and an empty
  `work_attempt`.
- `tests/db/test_postgres_integration.py`: `test_execute_grants_match_the
  _declared_protocol_steps` resolved declared signatures to catalog functions
  by name prefix, so 0024's `monitor_clear_finding(text, text[])` — this
  schema's first overload — made it call both declared forms ambiguous and
  both granted overloads surplus on an exactly compliant database. It failed
  on a correct schema rather than catching an incorrect one, which would have
  held the whole branch out of CI. It now keys on name and arity via
  `roles._function_key` against `pg_proc.pronargs`, the same way
  `render_table_grants` and `test_grant_compliance` were taught to in the same
  migration.
- `command_center/ops/infra_monitor.py`: the stall clock was
  `now() - min(work_item.updated_at)` over every ready or claimed row. For a
  claimed row that timestamp is the moment it was CLAIMED — `queue_heartbeat`
  renews `work_attempt.visible_until` and never touches the item — so any
  attempt outrunning `--max-stalled-seconds` (900s) read as a stall, and the
  deployment expects that to be ordinary: `voyn-aicc-worker@.service` gives one
  attempt `TimeoutStopSec=3660s`, plus a 600s worktree clone before the agent
  starts. `control-01:queue` therefore went red with `queue_stalled` on healthy
  work, opened a `monitor_finding` (#481) and had the planner mint a task for
  it — a fail-closed monitor that could not be green while the fleet worked.
  The clock now measures only UNATTENDED pending work: ready and *due* (an item
  inside its retry backoff is waiting by design), or claimed with no live
  lease — the zombie the check was written for, which is still caught. A claim
  under a live lease is reported separately as `live_claim_age_seconds` and
  bounded by the new `--max-claim-seconds` (default 5400s), so a handler wedged
  behind a still-beating heartbeat thread is caught at a ceiling above one
  legitimate attempt instead of below it. `throughput_stalled` is gated on the
  same unattended set, so the fix does not merely rename the false positive.
- Excluding attended claims was half of it. The queue is *designed* to hold
  more dispatched work than the fleet can claim — `PlanLimits.wip_limit` is 4
  against the 2 lanes of `deploy/aicc/worker-lanes`, and `backlog_dispatch`
  bounds concurrency by per-repository writer leases across three fleet
  repositories — so the surplus item sits `ready` and *due* until a lane frees,
  which takes a whole attempt and tripped the same clock on its own. A due
  ready item is now a stall only when a lane was FREE to take it: the snapshot
  reports three disjoint classes (`ready_due`, `lapsed_claims`,
  `attended_claims`, each with its own age) and `evaluate` weighs the attended
  claims against the new `--claim-capacity` (default 2, the lane registry). A
  lapsed claim stays starved at any capacity — no lane is holding it, so a busy
  fleet is no explanation for it. Scaling past two lanes means passing
  `--claim-capacity`; until it is passed the probe under-reports rather than
  crying wolf, which is the direction a red tick that mints a task should err.
- `throughput_stalled` also gained a floor: it is the only check that fires
  inside the stall window, and an hour with no successes is ordinary here (one
  attempt may run longer), so a queue that had been empty all night went red
  the second the planner dispatched the first task. It now needs the starved
  work to be older than `CLAIM_POLL_CEILING_SECONDS` — the daemon's idle poll
  backs off to `WorkerConfig.idle_max_seconds` (30s) and no further, so below
  that nothing has been offered to a claimer yet.
- `tests/db/test_infra_monitor_queue_snapshot.py`: the measurement proved
  against a real server, as `aicc_app` — claims taken through `queue_claim`,
  leases expired the way the protocol expires them, and the lease read through
  `work_attempt_public` (`work_attempt` itself is granted to nobody).
- `tests/ops/test_infra_monitor.py`: `test_new_pending_work_does_not_turn_an_idle_queue_red`
  now sets the count alongside the age. Gating the stall check on the
  unattended count made that snapshot unreachable (the count is 0 exactly when
  the age is `None`), so the test passed through the gate without ever reaching
  the `10s < 900s` comparison it exists to pin — it no longer failed when the
  threshold was mutated away. The redundant count gates are gone with the
  restructure; the ages alone drive the verdicts. Two defaults are pinned to
  their sources rather than restated: `--claim-capacity` against
  `deploy/aicc/worker-lanes`, and the throughput floor against
  `WorkerConfig.idle_max_seconds`.
- A healthy `queue_stalled` measurement could still leave its finding open:
  the monitor recorded findings independently but cleared them only when every
  check sharing the source was green. On `control-01:queue`, an unrelated
  `dead_letter_growth` failure therefore held finding #481 open after the stall
  fix. Migration 0024 adds a compatible two-argument clear function that closes
  only findings absent from the current measurement; every monitor tick now
  reconciles that set. Unit and PostgreSQL integration regressions cover red-tick
  partial clears, stable finding/task identity, empty/NULL clears, source scope,
  and grants for both control- and worker-host probes.
- `deploy/systemd/voyn-queue-monitor.service`: the probe's finding source
  (`control-01:queue`) is set as `Environment=AICC_MONITOR_FINDING_SOURCE`,
  which `--record-findings` now defaults to, rather than appended to
  `ExecStart`. That line spells the control host's absolute install path, and
  `scripts/ci/prepush/leak_guard.sh` refuses any ADDED line carrying one in
  this public repository — a rule the guarded publisher enforces too
  (`_leak_guard_gate` fails the publish, it does not warn), so re-typing the
  line to append a flag would have made the fix unpublishable. The line
  already in history is untouched context. An explicit `--record-findings`
  still wins over the environment, and the worker-host probe
  (`voyn-infra-monitor.service`) sets neither, so it keeps recording nothing.

- `monitor_clear_finding` is this schema's first OVERLOADED function, and two
  places assumed a function name identified exactly one signature.
  `roles.render_table_grants`' existence filter — the one that lets grants be
  re-asserted against a database at an intermediate migration version — matched
  on the bare name, so between 0021 and 0024 it would have emitted a GRANT for
  the two-argument form while only the one-argument form existed; the whole
  matrix applies in one transaction, so that is not one skipped privilege but
  every grant in the run aborting. `tests/db/test_grant_compliance.py` resolved
  declared signatures by name prefix the same way and would have called both
  forms `AMBIGUOUS` and both granted overloads `EXTRA` on a schema that is
  exactly compliant. Both now key on name AND arity (`roles._function_key`,
  read from `pg_proc.pronargs`) — arity because it is what PostgreSQL resolves
  this pair by and it is an integer, so no type-name spelling has to agree
  between the declared matrix and the catalog; matching rendered types would
  put `timestamptz` against `timestamp with time zone` and skip a grant
  SILENTLY, and a missing grant is an outage the next deploy inherits while a
  surplus statement is a loud error. A same-name/same-arity clash is still
  reported as ambiguous.

### Added — Home screen widget snippets (`VOYN-MIN-WIDGET-SNIP`)
- `AICCNativeCore.WidgetIntentSnippet` / `WidgetFlow` / `WidgetDestination`
  (`clients/aicc-native/apple/Sources/AICCNativeCore/AICCNativeCore.swift`):
  a one-status, one-action snapshot for each of the three flows with a single
  next action on iPhone — Work, Dialogues, Decisions. `Snapshot.
  widgetSnippets(dialogs:)` always returns exactly three, one per flow, and
  a widget's one action is always a deep link into the exact item, never a
  mutation — `POST /v1/commands` is still out of the v1 read-only contract.
  See `docs/aicc_native/WIDGET_SNIPPETS.md`.

### Added — Decision-memory graph (`VOYN-MIN-GRAPH-SQL`)
- `command_center/decision_graph_store.py`: a standalone SQLite store for a
  semantic graph of decisions, errors, dependencies and effects — nodes typed
  by kind, directed/typed edges, and `path_to_failure()`, one `WITH RECURSIVE`
  query finding the shortest cycle-safe chain from any node to the nearest
  reachable failure.
- `command_center/decision_graph_render.py`: a hand-rolled layered-DAG-to-SVG
  renderer (no `graphviz`/`pydot`/`networkx` dependency exists in this repo)
  that highlights the path-to-failure edges in red and draws each incident's
  corrective decision as a dashed `mitigates` arrow rather than a step on the
  road to its own fix.
- `scripts/seed_decision_graph_incidents.py` /
  `scripts/render_decision_graph.py`: seed the graph from six real incidents
  mined from this repository's own migration comments, docstrings and commit
  messages, and render it. The rendered artifact —
  [`docs/operations/decision_memory_graph.svg`](docs/operations/decision_memory_graph.svg)
  — is the acceptance criterion: one visual graph with a path-to-failure for
  past incidents. See `docs/operations/DECISION_MEMORY_GRAPH.md`.

### Added — Executive Time-Machine (`VOYN-MIN-EXEC`)
- `command_center/decision_time_machine.py`: for a critical decision or
  incident, one executable `decision package` — hypothesis, alternatives
  considered (with why each was passed over), the decision and its
  rationale — created with a pending effect checkpoint already scheduled at
  each of 1/7/30/90 days out. `record_effect` fills in the actual outcome at
  a horizon (in any order); `due_checkpoints` surfaces whichever check-ins
  have passed their due date without being asked; `build_post_mortem`
  assembles the package into a post-mortem view with a verdict
  (`validated`/`invalidated`/`mixed`/`pending_data`) computed from the
  checkpoints recorded so far; `find_similar_packages` matches a new
  critical event's framing against past packages' actual outcomes (the
  "application in similar scenarios" acceptance), and
  `missing_decision_packages` flags any critical event with no package on
  file yet.
- `tests/test_decision_time_machine.py`: coverage for checkpoint scheduling,
  out-of-order effect recording, post-mortem verdict derivation, similar-
  scenario matching and the critical-event coverage gap check.

### Added — SRV-04b two-host acceptance record (`VOYN-W0-AICC-CLAIM-TWO-HOST-ACCEPTED`)
- `docs/srv04b-two-host-acceptance.md`: records a separate, two-physical-host
  acceptance pass of the `0002_queue_claim` protocol against
  `origin/main@f9bb889` — exclusivity under real network jitter (192 attempts
  across 8 runs, exactly 8 winners); a real userspace network blackhole that
  forces `queue_reap()` to expire and requeue the stale owner's attempt
  (`attempt_expired` after 27.45s) followed by a genuine second, winning
  claim, with the old and new owners confirmed never simultaneously valid
  (the stale owner's post-reap token use was rejected `attempt_expired` only
  *after* the second claim had already won); cross-host token theft/
  `SET ROLE` laundering refused by the `session_user` claimant check; and
  clock independence (0 of 11 protocol functions take a timestamp parameter).
  Named limit: the database host's OS was not Linux in this pass.
  `docs/AIOS_BOUNDARY.md` cross-references it from the SRV-04b exception note.

### Added — Fleet status and lifecycle (`VOYN-MIN-FARM`)
- `command_center/db/fleet_admin.py` (`FleetAdmin`): the single-panel view
  over enrolled worker-host devices — one query joins `principal`,
  `principal_credential_public` and `principal_event` into state, host, live
  credential expiry and last audit event per device, plus an operator-only
  `suspend()` over the existing `identity_revoke_principal`. Additive: no new
  table, grant or privileged function.
- `python -m command_center.db fleet-status` / `fleet-suspend`: the CLI
  surface — "10 devices managed by one operational panel" as a runnable
  command rather than five hand-run queries. See
  `docs/operations/FLEET_STATUS.md`.

### Added (SRV-05 slice 2)
- [`docs/adr/0011-headless-worker-service.md`](docs/adr/0011-headless-worker-service.md) — the
  architecture record for the headless worker: the versioned payload contract, why its timeout bound
  is `agent_runner`'s run-length ceiling and not the queue's own (continuously renewed) visibility
  window, and why only `writer_lease.hold` — never `worktree_lease.blocking_lease` — confers mutation
  authority for a dispatch.
- `command_center/worker/payloads.py` — versioned `agent_run` payload contract
  (v1): refusals as data, timeout bounded by `agent_runner`'s run-length ceiling,
  provenance defaults to untrusted.
- `command_center/worker/handlers.py` — the payload→execution bridge through
  the existing `agent_runner.run_claude_code` (sandbox profiles, credential
  scrubbing and timeouts stay the runner's decisions); untrusted mutating
  payloads are refused, not silently downgraded; results travel as bounded
  tails.
- `deploy/systemd/aicc-worker.service` — declared cgroup resource envelope
  (MemoryMax/MemoryHigh/CPUQuota/TasksMax); sandbox-directive acceptance
  stays measurement-from-inside per SRV-05-B.

### Added — the headless worker service (`VOYN-W0-AICC-SRV-05`, slice 1)

- `command_center/db/work_queue_store.py`: the first Python surface over the
  `0002_queue_claim` protocol — until now `queue_claim`/`queue_heartbeat`/
  `queue_complete`/`queue_fail` had no caller outside tests. Token generated
  locally, only its SHA-256 travels on claim; refusals are data, not
  exceptions.
- `command_center/worker/`: the claim-execute-report daemon. Heartbeat runs
  beside the handler and stops the work when the lease is lost; SIGTERM
  finishes the item in hand and claims no more; an unknown payload kind is a
  non-retryable failure; auth loss exits non-zero and leaves restart pacing to
  systemd. Handlers are a registry — the bridge from a claimed payload to a
  real agent run is deliberately a follow-up slice, because the `execution`
  payload schema and its producer do not exist yet.
- `deploy/systemd/aicc-worker.service`: the first systemd unit in the repo.
  `TimeoutStopSec` outlives the visibility window so a healthy handler is
  never killed mid-item; hardened (`NoNewPrivileges`, `ProtectSystem=strict`).
- Test conftest guards: two autouse fixtures hard-imported streamlit (directly
  and via `app.py`), turning every test on a headless host red at setup — the
  exact host the worker ships to. Both are now guarded, with the reason
  recorded at the guard.

### Security — container deployment no longer exposes the console (`VOYN-W0-AICC-STREAMLIT-EXPOSED-NO-AUTH`)

An earlier audit (`9761459`, BLOCKER-1) pinned the Streamlit console to localhost for the bare
`streamlit run` and `scripts/start-ui.sh` paths, but no test guarded that decision and the container
deployment path reintroduced the same exposure: `scripts/aml-entrypoint.sh` defaulted
`--server.address` to `0.0.0.0` and `docker-compose.aml.yml` published the port unqualified, so a
routine `docker compose up` put an unauthenticated console that performs privileged git/gh and
subprocess operations on every host interface — below any host firewall rule, since Docker installs
its own.

- `scripts/aml-entrypoint.sh` no longer has a default bind address. It exits `78` (`EX_CONFIG`) with
  an explanatory message unless `STREAMLIT_SERVER_ADDRESS` is set, so the choice cannot be inherited
  unseen.
- `docker-compose.aml.yml` publishes on `${AML_BIND_HOST:-127.0.0.1}` instead of every interface, and
  states the container-internal `0.0.0.0` explicitly with the reason it is correct there.
- `tests/test_deployment_exposure.py` gates all four launch paths, executing the entrypoint rather
  than pattern-matching it.

This closes the *exposure*, not the underlying absence of authentication: the console still has no
auth layer, which is tracked separately as `AUTH-HTTP-01`. Widening `AML_BIND_HOST` therefore still
means publishing an unauthenticated privileged surface.

### H1 sprint history

**H1** is the committed-next horizon defined by
[`docs/roadmap/MASTER_PRODUCT_ROADMAP.md`](docs/roadmap/MASTER_PRODUCT_ROADMAP.md) under
`DR-ROADMAP-AUTHORITY-001`. It commits to one goal — a **native-desktop, local-first, single-user
developer control plane** that reaches and then exceeds the Streamlit feature set, with fail-closed
safety on every privileged action — across three tracks: **Desktop Increment 1** (15 rows),
**Audit remediation** (13 rows), and **Governance** (6 rows).

This section is the chronological index for that horizon. Every date is derived from the commit
history on `main` (first appearance of the relevant module or document), not from planning
documents; the detailed entries for each item are the sections below and in the released versions.
Work that ran alongside H1 but is not one of its three tracks is listed separately.

#### Timeline

| Date | Milestone | Track |
|---|---|---|
| 2026-07-15 | `1.0.0` initial Streamlit application; `1.1.0` (Sprint 2) Executive Dashboard, Command Palette, Focus Mode, Timeline, AI Agents, Smart Tasks, Git Center, Workspace Launcher | Pre-H1 baseline |
| 2026-07-16 | `1.2.0` `command_center/` package, Project Chat, Claude Code runner, report parser; **Sprint 1** v2 Session Supervisor + `runtime.db` (ADR 0003) | Pre-H1 baseline |
| 2026-07-17…18 | **Sprint 3** Workspace Home — read model, redaction stage, `git_info`/`artifacts` extraction, Workspace Home page | Platform |
| 2026-07-18 | **Desktop D0** — the canonical `docs/desktop/` documentation set (vision, architecture, IA, design directions, design system, Workspace Home spec, platform behavior, frozen D1–D4 scope, implementation roadmap). Documentation only | Desktop |
| 2026-07-19 | Execution queue, Kanban state separation, upgraded recommendations | Platform |
| 2026-07-20 | Portfolio task planning and safe launch | Platform |
| 2026-07-21 | Founder Functional Audit `9761459` recorded | Audit remediation |
| 2026-07-22 | Autonomous Task Completion Pipeline (`AICC-AUTONOMY-001`, ADR 0004) | Platform |
| 2026-07-23 | Autonomy Proposal Foundation (`AICC-AUTONOMY-002`, ADR 0005) | Platform |
| 2026-07-27 | **D1A–D1C** — the native PySide6 shell lands: `QApplication` assembly, `AppShell` main window, sidebar/top bar, nine-section navigation, theme controller, settings and window-geometry persistence | Desktop |
| 2026-07-28 | `MASTER_PRODUCT_ROADMAP.md` — the `AICC-D1-001` epic decomposed into 15 rows (11 increments + 4 gates) | Governance |
| 2026-07-29 | **D2A** application adapter, **D2B** `QThreadPool` worker framework, **D2C** status/card/row components and live-data wiring, Russian UI + i18n registry with an automated language gate | Desktop |
| 2026-07-29 | Audit batch: Copilot executor fails closed for untrusted tasks (`SEC-1`/`D-01`); full read-modify-write lock on project/portfolio config (`AR-5`); Done tasks not backed by a verified completion reported (`DATA-D1`) | Audit remediation |
| 2026-07-30 | **D2D** edge states, loading skeletons, accessibility, and a BANK/LEGAL redaction regression test; **D3A** Projects page with repository paths; **D3B** persistent settings form and platform preference abstraction; read-only AIOS Core status and provider-readiness boundary | Desktop |
| 2026-07-31 | **D4A** unsigned macOS bundle and **D4B** unsigned Windows 11 x64 bundle via PyInstaller; self-contained Windows runbook | Desktop |
| 2026-08-01 | **D1 final gate**: macOS Apple Silicon PASS recorded on real hardware; Windows interactive leg still blocked (no hardware). `windows-latest` CI job added for the automated half | Desktop |
| 2026-08-03 | `run-desktop` project skill (verified macOS launch recipe); live workspace data resolved correctly inside the packaged app; autonomous daily-audit publication and shutdown fenced | Desktop / Governance |
| 2026-08-03…06 | AML Service phases 1–7 — risk scoring, rule engine, evidence store, case management, 115-ФЗ country pack, Docker, bank acceptance package | Alongside H1 |
| 2026-08-04 | Report-derived child tasks stamped `untrusted_import` (`SEC-D-02`); PID-reuse recovery covered and single-host lock scope documented | Audit remediation |
| 2026-08-06 | **Sprint 4** AIOS Tasks backend behind `AICC_TASKS_BACKEND`; ESF/AML project registry; D2 Native Workspace Home tests + `ErrorState` widget | Platform / Desktop |
| 2026-08-07 | Task-aware executor preflight; load-aware executor selection; `TasksStoreUnreadable` instead of a silent empty read; audit-closure verdict turned into an executable gate with W1/W2 remediation confirmed merged | Audit remediation / Governance |

#### Track 1 — Desktop Increment 1

The D1A→D4 sequence is the approved decomposition of the `AICC-D1-001` epic and closes the §2.1
desktop parity gate. As of 2026-08-07 the **code** for D1A through D4B has landed on `main`:

- `command_center/desktop/` is a working PySide6/Qt Widgets client launched with
  `python -m command_center.desktop`. Its startup path imports PySide6 and nothing else — no
  `app.py`, no Streamlit, no HTTP client — enforced by `tests/desktop/test_lifecycle.py` running a
  clean interpreter.
- Three of the nine sections are active (Home, Projects, Settings); the other six render visibly
  disabled so the sidebar never reflows between increments.
- Home is a native Workspace Home over `command_center.application.WorkspaceHomeAdapter`, a thin
  wrapper that returns `build_workspace_home_snapshot`'s output unchanged, inheriting every
  BANK/LEGAL redaction guarantee verbatim. It loads through the D2B worker framework, so the GUI
  thread is never blocked.
- The client is read-only except for repository-path configuration, theme/density preferences, and
  window geometry — binding decisions 11 and 12 of `DESKTOP_INCREMENT_1.md`.
- `tests/desktop/` is an offscreen pytest-qt suite: **175 passed** as of 2026-08-07.
- Packaging produces **unsigned development bundles** for macOS Apple Silicon and Windows 11 x64.
  No signing, notarization, or auto-update exists.

**Gates remain open.** `AICC-D1-GATE` is still **Review**, not Done: the interactive Windows 11 x64
acceptance pass has never been performed on real hardware, and that gate's forbidden-scope note
required it to close before `AICC-D2A` began. The D2/D3/D4 implementations landed anyway, so the
verification gates lag the merged code rather than leading it.

#### Track 2 — Audit remediation

The Still-Open rows of Founder Functional Audit `9761459`, closing the §2.2 safety gate and §2.5
audit-closure gate. Landed across 2026-07-29 → 2026-08-07: fail-closed handling of untrusted tasks
in the Copilot executor, provenance stamping on report-derived child tasks, a full read-modify-write
lock on project and portfolio configuration, per-warning launch confirmation (each warning
acknowledged under its own stable issue code, with the launch blocked until every one is ticked),
Done tasks reported when not backed by a verified completion, and a store-read failure that now
raises `TasksStoreUnreadable` instead of silently returning an empty list.

The closure verdict itself was made **executable** on 2026-08-07 rather than left as prose, and the
W1/W2 remediation set was confirmed merged.

#### Track 3 — Governance

The `§8` required follow-ups F1–F5 of the authority record, closing the §2.3 data-integrity gate and
§2.4 documentation-truth gate: the canonical master roadmap and its machine-readable companion
(2026-07-28), audit reconciliation and current-state updates, an AIOS boundary fitness baseline, and
fencing of autonomous daily-audit publication and shutdown.

#### Alongside H1 — not one of the three tracks

The AML Service (phases 1–7, 115-ФЗ compliance, Docker packaging, bank acceptance package) and the
ESF/AML project registry additions landed during the H1 window but are outside the horizon's three
committed tracks — recorded here so the timeline is not read as an H1 scope claim.

#### Known status drift

`MASTER_PRODUCT_ROADMAP.md` is a planning snapshot reconciled against `main` @ `bd9f05b` on
2026-07-28 and still lists `AICC-D2A` through `AICC-D4-GATE` as **Backlog**. The corresponding code
merged between 2026-07-29 and 2026-07-31. `docs/desktop/README.md` and `CURRENT_STATE.md` likewise
still describe the desktop client as a pure shell with no data wiring, or as documentation and design
work only. The code, its tests, and this changelog are the current authority; those three documents
need reconciliation.

### HTTP authentication for the mutating API surfaces (VOYN-W0-AICC-AUTH-HTTP-01) — 2026-08-15

Both FastAPI applications served no authentication at all. Every mutating route
now requires a verified platform principal and an explicit AICC-local grant.

This delivery also fulfils `VOYN-W0-AICC-SRV-02` ("principal-and-permission-model").
`SRV-02` was filed in the `SRV` sequence between `SRV-01`/`SRV-01a`/`SRV-01b`
(PostgreSQL foundation) and `SRV-03` (worker host admission), but every
dispatch of it was refused by the writer-lease bug fixed in
`VOYN-W0-AICC-LEASE-STUCK-EXPIRED-NO-RECLAIM` (PR #358) — so it never ran, and
this AUTH-HTTP-01 delivery (filed and completed independently) covers its
intended scope in full: `command_center/http_auth/` is the HTTP-layer
principal-and-permission model, and `command_center/db/roles.py` (`SRV-01a`)
is its database-layer counterpart. `SRV-02` needs no further code and should
not be re-attempted; this note is the closure record for anyone who finds the
gap in the `SRV` numbering, the same gap that prompted its retry
(`VOYN-W0-AICC-SRV-02-RETRY`) on 2026-08-26.

#### Added
- **`command_center/http_auth/`** — `identity.py` (forwards the caller's platform
  bearer credential to `GET /api/v1/whoami`; stores no credential, hashes
  nothing, holds no key; fails closed with `503` when the authority is
  unreachable, which is deliberately distinct from the `401` for a rejected
  credential; no cache, so a revoked principal is refused on its next request),
  `authz.py` (a closed, deny-by-default operation inventory and a
  configuration-driven grant map — a 200 from `whoami` is authentication, never
  permission), and `routing.py` (the table of all 29 mutating routes, the
  dependency, and `validate_routing`).
- **A boot check.** `validate_routing` runs in both app factories: a mutating
  route with no routing entry, no mounted dependency, or an operation outside
  the inventory stops the process from starting, as does an unparseable grant
  file. It also refuses a zero-route inventory, because a route walker that
  inspects nothing must not report success.
- **`tests/http_auth/`** — 84 checks, including an unauthenticated sweep of all
  29 routes, and `tests/http_auth/negative_control.py`, which removes each
  control in turn on a throwaway copy of the tree and requires the suite to go
  red (15 mutants, 15 killed, 0 survived).

#### Changed
- **The mutating surface is 29 routes across two apps, not two.**
  `command_center/api/app.py` mounts 27 of them while its package docstring
  still called the application read-only; the docstrings are corrected
  (`VOYN-W0-AICC-AUTH-HTTP-01a`).
- **`actor` is gone from the dispatch write bodies.** Not validated — made
  impossible, following `queue_claim()`: the field is deleted, the request
  models set `extra="forbid"` so a forged actor is a `422` rather than a silent
  ignore, and `dispatch.service.assign` / `dispatch.policy_config.update_policy`
  take a `Principal` and have no `actor` parameter to pass.
  `PUT /api/v1/dispatch/policy` no longer accepts an unwrapped body as
  `changes`: that form was indistinguishable from a body carrying an
  unexpected top-level key.

#### Known limitations
- **Read paths remain unauthenticated** — out of scope by acceptance criteria,
  not by cost (measured: 47–76 ms median per verification, a ceiling of roughly
  105–169 authentications/second). Filed as `VOYN-W0-AICC-AUTH-HTTP-02`.
- **Seven routes still accept a client-supplied identity field** (`voter_id`,
  `owner` ×2, `actor` ×4). They are authenticated and authorized like every
  other route; removing the fields needs per-endpoint product decisions, so each
  is a signed carve-out in `routing.CLIENT_IDENTITY_CARVE_OUTS` with a reason
  and a task (`VOYN-W0-AICC-AUTH-HTTP-01b`), and a test refuses any *unsigned*
  one.
- **The Streamlit console is untouched** and remains the most exposed surface
  (`VOYN-W0-AICC-STREAMLIT-EXPOSURE-01`).

### AIOS Tasks backend (Sprint 4) — 2026-08-06

Feature flag `AICC_TASKS_BACKEND=json|aios` selects the tasks persistence layer at runtime.
Default is `json` (no behaviour change). Set to `aios` and provide `AICC_AIOS_URL` + `AICC_AIOS_TOKEN`
to route all task reads/writes through the AIOS Tasks API.

#### Added
- **`command_center/application/aios_tasks.py`**: `aicc_dict_to_create_request` / `aios_task_to_aicc_dict`
  pure mapping functions; `AIOSIdMap` (local JSON file for AICC-id ↔ AIOS-uuid correlation);
  `AIOSTasksRepository` (read/create/update/upsert/upsert_all via `aios_sdk.AIOSClient`).
- **`tasks_repository.get_repository(root)`** factory: returns `JSONTasksRepository` (default) or
  `AIOSTasksRepository` based on `AICC_TASKS_BACKEND`. AIOS variant is lazily imported so
  the JSON path carries zero new overhead.
- **`scripts/migrate_tasks_to_aios.py`**: one-shot migration of `data/tasks.json` into AIOS;
  writes `data/.aios_id_map.json` for continuity; dry-run mode via `--dry-run`.
- **`JSONTasksRepository.upsert_all(tasks)`**: atomic batch write that replaces the previous
  individual-upsert loop in `app.py:upsert_tasks()`.
- **Tests**: `tests/test_aios_tasks_adapter.py`, `tests/test_aios_tasks_repository.py`,
  `tests/test_tasks_backend_routing.py` — skipped via `pytest.importorskip("aios_sdk")`
  when the local SDK path dep is unavailable (CI).

#### Known limitations (AIOS v1)
- C1: Titles longer than 512 chars are silently truncated on create.
- C2: AICC-specific fields (`duration_estimate`, `assignee`, etc.) round-trip as notes/tags only.
- C3: `AIOSIdMap` is per-process; multi-worker Streamlit deployments need a shared store.
- I3: AIOS auth token is not refreshed mid-session (assumed long-lived).
- I4: `list_tasks()` fetches only the first page (AIOS v1 has no cursor pagination).
- I5: `aios_sdk` is a local path dep; not in `requirements.txt` — CI skips AIOS tests.

### D1 final gate — cross-platform smoke pass (partial)

- **Verification record added**: `docs/desktop/D1_FINAL_GATE_SMOKE_TEST.md` records the D1 final gate
  (`docs/desktop/IMPLEMENTATION_ROADMAP.md` §"D1 final gate") smoke pass against
  `DESKTOP_INCREMENT_1.md` §2's acceptance criteria. macOS Apple Silicon (real hardware): pass —
  `pytest-qt` desktop suite green (28/28), `ruff check .` clean, real native-Qt launch with no
  `streamlit`/`app.py` import on the startup path, and window-geometry/theme persistence verified
  across a simulated restart. Windows 11 x64: not performed — no such machine was reachable from
  this session, so the gate remains open (`AICC-D1-GATE` stays **Review**, not **Done**) pending a
  Windows-hardware pass.

### Version contract

- **Canonical application version**: `command_center.__version__` now exposes the
  current `2.0.0` release line. The historical `v2.0.0-sprint1` tag remains an
  immutable prerelease milestone; the final `v2.0.0` tag is created only from a
  validated commit after this change reaches `main`.

### Integrated runtime safety and architecture reconciliation

#### Added

- **CI workflow**: Python 3.14 validation for pull requests and `main` pushes, with committed-diff
  whitespace checks, Ruff, byte compilation of `command_center scripts tests app.py`, and the
  complete pytest suite. Actions are SHA-pinned, the token is read-only, and superseded runs are
  cancelled.
- **Fail-closed task workspace provisioning**: normal task-v2 launch surfaces provision or attach
  an isolated branch/worktree only after explicit confirmation, then pass a persisted
  `WorkspaceSpec` through a second Supervisor verification immediately before spawn. Source
  repository, exact launch path, branch, worktree isolation, and status are verified without a
  network fetch; low-level ad-hoc runtime calls remain a separate boundary.
- **Deterministic scheduler planner**: `runtime/scheduler.py` produces explainable
  `ASSIGN`/`DEFER`/`BLOCKED` decisions from immutable work, agent, load, dependency, capability,
  capacity, and retry inputs. It is a read-only planner: it creates no claim, lease, queue entry,
  run, poller, or automatic launch.

#### Fixed

- **Execution queue concurrency**: application-owned enqueue, dequeue, reevaluation, Portfolio
  insertion/rollback, and launch-result commits now hold a bounded same-host OS advisory lock
  across the complete load-transform-save cycle. Atomic replacement remains the write primitive;
  process launch is never performed while the queue lock is held, and raw load/save helpers remain
  intentionally uncoordinated primitives.
- **Launch and scheduler races**: workspace verification is registered before lifecycle evidence
  can trigger reconciliation; confirmation precedes worktree mutation; remote-tracking branches,
  persisted provisioning outcomes, active task IDs, deterministic agent tie-breaking, and corrupt
  retry state now fail safely.
- **Autonomy authority (schema 11)**: migration 7 adds canonical immutable
  `proposal.parameters_json`; malformed policies close completely; proposal authority and evidence
  freeze at assessment; CAS is checked before lifecycle guards; plans carry an exact action digest;
  dispatch rechecks policy, kind, payload, evidence digest, blockers, and staleness; confirmations
  must bind to a matching persisted result. Only TASK_CREATION and TASK_EXECUTION are currently
  dispatchable; priority, dependency, and merge plans remain advisory.
- **Runtime documentation**: README, current-state, architecture, changelog, and ADR 0005 are
  reconciled with schema 11, queue locking, workspace provisioning, scheduler and autonomy
  boundaries, and CI.
- **Per-warning launch confirmation** (Founder audit MAJOR-4): the launch confirmation dialog no
  longer clears a dirty working tree and a branch mismatch with one shared "подтверждаю несмотря на
  предупреждения" checkbox. Each warning now renders its own acknowledgement, keyed by its stable
  issue code, and the launch stays blocked — button `disabled=` plus the server-side re-check —
  until every one is ticked. Acknowledgements are also cleared each time the dialog is opened, so a
  previous launch's confirmations are never inherited.

### Portfolio Execution and Intelligence

#### Added
- **Portfolio Execution**: parses ready task cards from a separate Portfolio checkout, validates
  dependencies/conflicts and repository mappings, previews a launch plan, creates or attaches an
  isolated branch/worktree after explicit confirmation, and launches through the asynchronous
  Execution Center. A persisted launch registry and lock files prevent duplicate claims; bounded
  rollback removes only resources created by a failed launch attempt.
- **Portfolio Overview**: read-only project health, dependency waves, cycles, critical path,
  capacity, readiness and deterministic recommendations from Portfolio task cards and current
  launch state.
- **Portfolio batch launch**: an explicit, concurrency-capped orchestration flow with collision
  preflight. It does not introduce autonomous scheduling.

### Autonomy Proposal Foundation (AICC-AUTONOMY-002)

The first safe, explainable autonomy foundation: a persisted, evidence-backed proposal lifecycle
that makes the boundary between **recommendation, approval, and execution** explicit. The
autonomy layer governs decisions but **never executes anything** — `dispatch` records the
boundary crossing and returns a dry-run plan the caller must run explicitly via `start_run`. See
`docs/adr/0005-autonomy-proposal-foundation.md`.

#### Added
- **`command_center/runtime/autonomy.py`**: the pure domain core — proposal state machine
  (`DRAFT → PROPOSED → ELIGIBLE/BLOCKED → AWAITING_APPROVAL → APPROVED → DISPATCHED → EXECUTED`,
  plus `REJECTED`/`WITHDRAWN`) with an explicit transition guard; deterministic `classify_risk`
  and `evaluate_eligibility` (pure, reproducible, hardest-block-first); an attributable,
  immutable `Evidence` model with an order-independent digest; a conservative-by-default
  `AutonomyPolicy` (closed on construction; CRITICAL risk never auto-approved); and side-effect-
  free dry-run `ExecutionPlan`.
- **`command_center/runtime/autonomy_service.py`**: the orchestration engine
  (`create_proposal`, `assess`, `plan`, `approve`, `reject`, `withdraw`, `dispatch`,
  `confirm_execution`, `fail_dispatch`) writing an append-only audit event per move. Dispatch is
  refused unless the proposal is `APPROVED` **and** the policy explicitly enables execution
  dispatch; a refusal is itself audited and leaves the proposal untouched.
- **`runtime.db` migration 6**: `proposal`, `proposal_evidence` (append-only and frozen after
  assessment), `proposal_event` (append-only) tables with CAS-guarded, transition-guarded updates;
  CRUD in `db.py`; reads and
  gates exposed via `ExecutionCenterAPI` (`create_proposal`/`assess_proposal`/`plan_proposal`/
  `approve_proposal`/`reject_proposal`/`withdraw_proposal`/`dispatch_proposal`/
  `confirm_proposal_execution` + projections).
- **Tests**: `tests/test_autonomy_domain.py`, `tests/test_autonomy_db.py`,
  `tests/test_autonomy_service.py`, `tests/test_autonomy_api.py` — policy, risk, state
  transitions, denials, malformed input, the full lifecycle, and reproducibility.
- **`scripts/demo_autonomy_proposals.py`**: four end-to-end scenarios (disabled/blocked,
  human-gate, full dispatch, critical merge) against a throwaway store; launches nothing.

#### Safety
- No silent execution, no automatic merge, no hidden repository modifications, no fabricated
  evidence, no execution without an explicit policy and approval state. Hard denials block;
  eligible actions outside the auto-approval ceiling require a human. Runtime code depends on no
  UI framework.

### Autonomous Task Completion Pipeline (AICC-AUTONOMY-001)

Closes the gap between "Claude process finished" and "the engineering task is completed and
merged into the target branch". See `docs/adr/0004-autonomous-task-completion-pipeline.md` and
`docs/completion-pipeline.md`.

#### Added
- **`command_center/runtime/completion.py`**: the pure domain core — completion state machine
  (`EXECUTION_FINISHED → … → COMPLETED`, plus `VALIDATION_FAILED`/`PR_CLOSED_UNMERGED`/
  `MERGE_BLOCKED`/`REQUIRES_ATTENTION`/`RECOVERY_PENDING`/`RECOVERY_FAILED`), reason codes,
  `CompletionPolicy`, and `CompletionEvaluator` returning a structured `CompletionAssessment`
  (never a bare boolean). Completion is evidence-based: a task is `COMPLETED` only when its
  change is reachable from the target branch — exit code 0 is never sufficient.
- **`command_center/runtime/completion_service.py`**: the restart-safe, idempotent orchestrator
  (`begin_completion`, `advance`, `advance_pending`) that turns evaluator verdicts into real
  side effects (validation, push, PR open/merge, target verification, closed-unmerged recovery)
  with exponential backoff and a full audit trail.
- **`command_center/runtime/repo_state.py`** (read-only git inspection), **`git_ops.py`** (git
  write adapter — never force-pushes), **`github.py`** (first `gh` CLI integration; a closed PR
  is never treated as merged; includes an in-memory `FakeGitHubClient`), **`validation.py`**
  (configurable, allowlisted, bounded validation-plan execution).
- **`runtime.db` migration 5**: `completion`, `completion_validation`, `completion_event` tables
  with CAS-guarded updates; CRUD in `db.py`; reads exposed via `ExecutionCenterAPI`.
- **Supervisor**: `advance_completions()` and an opt-in background autopilot
  (`AICC_COMPLETION_AUTOPILOT`) that advances due completions off the UI thread.
- **`task_sync`**: seeds completion rows for completed runs and projects completion state onto
  the Kanban task (`launch_status`, and on success stage "Merged"/progress 100 +
  `pull_request_status="merged"`).
- **Execution Center UI**: a compact completion panel distinguishing "process finished" from
  "task completed and merged" (state, validation, branch/commit, PR number+state, merge status,
  last-checked, recommended action).
- **`command_center/project_config.py`**: per-project completion policy defaults (merge mode/
  method, PR recovery, validation plan) — conservative by default.
- **`scripts/demo_completion_pipeline.py`**: deterministic Scenario A/B/C demonstration against
  real git + a fake GitHub client.

### Desktop Architecture D0

#### Added
- **`docs/desktop/`**: canonical, implementation-ready documentation set for a native
  PySide6/Qt Widgets desktop application — product vision, target architecture, information
  architecture, design directions (Professional Control Plane approved), design system, a
  Workspace Home native-page spec built on the existing `build_workspace_home_snapshot` read
  model, macOS/Windows platform behavior, frozen Desktop Increment 1 (D1–D4) scope, and a
  commit-sized implementation roadmap. Documentation only — no desktop code, dependencies, or
  packaging exist yet. Next implementation stage: D1A.

### Sprint 3 Increment 1: Workspace Home

Implements `WORKSPACE_HOME_ARCHITECTURE.md` in full (all 10 steps of §17's implementation
plan). That document's own status header ("architecture only, no code changed") is now stale —
the design is implemented, not just approved.

#### Added
- **`command_center/git_info.py`**: per-project git/worktree discovery (`get_status`,
  `get_worktrees`, `get_log`, `get_diff_stat`, `get_branches`, `get_remotes`), extracted from
  `app.py`'s original ROOT-only helpers and parameterized by `cwd: Path`. `app.py`'s Git Center
  and Workspace Launcher pages are now thin wrappers over it (zero behavior change).
- **`command_center/artifacts.py`**: `list_markdown_files`, `project_from_path`,
  `infer_task_type_from_filename`, `read_text` — extracted verbatim from `app.py`, Streamlit-free,
  a leaf module. Every existing `app.py` call site repointed at it.
- **`db.list_runs`/`ExecutionCenterAPI.list_runs`** gained `states` (plural, `IN (...)`) and
  `limit` (SQL `LIMIT`) parameters, additive and backward compatible; `state`+`states` together
  raise `ValueError`. `EXECUTION_CENTER_ACTIVE_STATES` moved to `runtime/db.py` beside
  `TERMINAL_STATES`.
- **`command_center/workspace_home.py`**: the Workspace Home read model
  (`build_workspace_home_snapshot`) and its sensitivity redaction stage
  (`sanitize_workspace_project_entry`) — cross-project rollup of projects, git worktrees, active/
  recent runs (v1.2 + v2, merged and source-tagged), reports, artifacts, and activity, with every
  BANK/LEGAL entry passed through a field allowlist *before* it reaches the renderer.
- **Workspace Home page** (`workspace_home` nav entry): a new, additional page — Dashboard and
  Workspace Launcher are unchanged. Read-only; every Quick Action (Open Project, New Task, Launch
  Run, view Run/Report/Artifact) delegates to the existing gated forms, never mutates directly.
- Tests: `test_git_info.py`, `test_artifacts.py`, `test_workspace_home.py`,
  `test_workspace_home_ui.py`, plus extensions to `test_runtime_db.py`/`test_runtime_api.py` —
  389 tests total (up from 333), including a dual-layer (snapshot + rendered-page) regression
  test that no BANK/LEGAL prompt/log/report-body/raw-path content ever reaches the page.

#### Deviation from the architecture document
- §4's data-source map lists `load_tasks()` (the v1.2 Kanban store, which lives only in `app.py`)
  as a Projects-section input. `workspace_home.py` cannot import `app.py` under any circumstance
  (§6/§9.2, a hard constraint stated three times in the document) and `load_tasks` was not in
  Condition 4's extraction scope, so the per-project task count instead uses
  `ExecutionCenterAPI.list_tasks(project=...)` (v2 SQLite tasks, an explicitly allowed read
  method). This counts v2 orchestration tasks, not v1.2 Kanban cards — recorded in
  `workspace_home.py`'s module docstring.

## [1.2.0] - 2026-07-16

### Added
- **`command_center/` package**: `models`, `storage`, `project_config`, `agent_runner`,
  `report_parser`, `chat_service`, `workflow`, `activity_log` — see
  [Application and domain services](ARCHITECTURE.md#22-application-and-domain-services).
- **Project Chat** (`chat` page): per-project conversations with a provider abstraction (local
  manual mode, Claude Code CLI, optional OpenAI Responses API gated on `OPENAI_API_KEY` +
  `OPENAI_MODEL`); save any message into `reports/`, or convert it into a task.
- **Claude Code runner**: launch Claude Code from a Kanban task, the Agents page, Project Chat, or a
  generated-task preview, with an explicit repository/branch/agent/prompt confirmation step, a
  synchronous timeout-bounded execution, and full stdout/stderr capture.
- **Full report storage**: every completed run's untruncated report is saved under
  `reports/<PROJECT>/`.
- **Structured result extraction** (`report_parser.py`): deterministic verdict/findings/files/
  commit/branch/PR/validation/git-status/next-action parsing with evidence, a confidence level, and
  a manual-correction UI that never discards the original extraction.
- **Create Next Task**: verdict-driven task-type/workflow-stage/objective suggestion on a completed
  run, always requiring review before creating anything and never auto-executing.
- **Run journal** (`runs` page): filterable list of every run plus a full detail view; Executive
  Dashboard gained run metrics (today's runs, success/failure, awaiting remediation/final review,
  approved-for-commit, average duration by agent, open Blocker/High findings).
- **Task workflow fields**: `parent_task_id`, `prior_run_id`, `current_run_id`, `workflow_stage`,
  `latest_verdict`, `report_path`, `repository_path`, `branch`, `agent`, `last_run_at` — additive,
  backfilled on load, parallel to (not a replacement for) the existing Kanban `status`.
- **Project repository configuration**: Projects → "Настройки репозитория" tab; local overrides in
  gitignored `data/project_config.json`; no path ever guessed (only ever a verified-existing git
  repo, shown as a suggestion the user must save).
- **Sensitive-project handling**: BANK/LEGAL show an explicit warning before any agent launch or
  chat call and never auto-attach context files.
- `AICOS` added to the project registry (repository path unconfigured — no known local path).
- `requirements-dev.txt` (adds `pytest`), `.env.example`, and a `tests/` suite (pytest +
  Streamlit `AppTest`) covering storage, migration, path validation, the report parser, next-task
  mapping, report persistence, run filtering, sensitive-project warnings, and refusal to run
  against unconfigured paths or via a shell.

### Changed
- `data/runs.jsonl` and `data/activity.jsonl` use JSON Lines instead of a single JSON array — see
  [Persistence architecture](ARCHITECTURE.md#3-persistence-architecture) for why. `reports/` is now
  gitignored (may contain BANK/LEGAL content).

### Security
- The Claude Code runner never calls git-write subcommands itself, and refuses to run against any
  repository path not present in project configuration.
- Read-only task types (`review`/`final_gate`/`architecture_review`) run with the model's tool set
  restricted to `Read,Grep,Glob` via `--tools` — `Bash` and every file-edit tool are entirely absent
  from that run, not merely pattern-denied. Implementation/remediation task types keep `Bash` but
  have the specific git-write subcommands denied via `--disallowedTools` — see
  [Git and GitHub privileged boundaries](ARCHITECTURE.md#9-git-and-github-privileged-boundaries)
  for what each task-type class enforces.
- Fixed during independent review (F-01/F-02): an earlier version of this control denied specific
  `Bash(git ...)` patterns for read-only task types while leaving the general-purpose `Bash` tool
  available, which left `git apply`/`checkout`/`stash` and plain shell writes unrestricted for task
  types documented as unable to modify any file. Replaced with the `--tools` allowlist above.

## [1.1.0] - 2026-07-15

### Added
- Executive Dashboard: cross-project rollup (totals, active/blocked/completed, workload estimate),
  per-project status parsed from `CURRENT_STATE.md`, priority breakdown chart, workload by owner.
- Command Palette (`Mod+K`): searchable dialog to jump to any page or start a task for a project.
- Focus Mode: single-task distraction-reduced view with a quick status/"mark done" control.
- Timeline: unified, day-grouped, project-filterable feed of task events and file activity.
- AI Agents page: catalog of the task types supported by `scripts/start-task.sh`, with execution
  rules, live usage stats, and a shortcut into the task creator.
- Smart Tasks: task records gained `priority`, `owner`, `estimate_hours`, and `depends_on`;
  Kanban cards show priority/owner/estimate badges and a "Заблокировано" (blocked) badge for
  tasks with unmet dependencies; Kanban gained a priority filter.
- Git Center: expanded read-only Git view with commit history, full changed-file list,
  `git diff --stat` (staged/unstaged), branches, and remotes.
- Workspace Launcher: `git worktree list` overview plus per-project quick-jump cards (in-app
  navigation and copyable file paths).

### Changed
- `data/tasks.json` records are now backfilled with default Smart Tasks fields on load, so task
  files created before this release keep working without migration.
- The former "Git и активность" page was split: Git-only content moved to the new **Git Center**
  page, and the activity log moved to the new **Timeline** page.
- `scripts/start-ui.sh` now forwards its arguments to `streamlit run` (e.g. `--server.port`)
  instead of silently dropping them.

### Fixed
- Cross-page navigation actions (command palette, AI Agents shortcuts, Workspace Launcher,
  Focus Mode exit) no longer raise `StreamlitAPIException` when triggered — navigation targets
  are now staged in `pending_*` session-state keys and applied before the sidebar navigation
  widget is instantiated on the next run, instead of writing directly to an already-instantiated
  widget's key.

## [1.0.0] - 2026-07-15

### Added
- Initial working Streamlit application (`app.py`) launched via `python -m streamlit run app.py`.
- Dashboard: project/task counts, generated/report file counts, latest activity, active tasks
  grouped by project.
- Task creator: form (project, task type, objective, Kanban status) that runs
  `scripts/start-task.sh` as a subprocess (no `shell=True`, fixed argument list, 30s timeout,
  captured stdout/stderr) and records a matching task.
- Kanban board: Backlog / Next / In Progress / Review / Done columns, project filter, status
  change via dropdown, delete, and a task-details expander. Persisted to `data/tasks.json` with
  atomic writes.
- Project browser: per-project status, generated tasks, reports, and context, each with file
  modification time.
- Generated tasks browser and Reports browser: recursive, project-filterable, newest-first,
  markdown preview.
- Global context view: `CURRENT_STATE.md`, `DECISIONS.md`, `INBOX.md`.
- Git status: read-only branch/dirty/modified/untracked/last-commit summary.
- `requirements.txt` and `scripts/start-ui.sh` for one-command startup.
