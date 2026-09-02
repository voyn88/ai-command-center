# SRV-04b — a reproducible two-role acceptance check of the claim protocol

`VOYN-W0-AICC-CLAIM-TWO-HOST-ACCEPTED`. Subject: `queue_claim()` and the rest
of `0002_queue_claim` (`command_center/db/sql/0002_queue_claim.up.sql`),
accepted as SRV-04b at `origin/main@f9bb889` (#311).

## Why this document was rewritten

The prior version of this record (`docs/srv04b-two-host-acceptance.md` as
submitted in PR #459, head `3d7ebe2010879cc960fabeb08fc819a51ccf7e74`) was
rejected on independent review for two reasons, both correct:

1. It said the two-host pass tested "this exact commit" while naming
   `origin/main@f9bb889` as the tested SHA, and it was cross-referenced from
   a document whose own head was neither — the claim of coverage was not
   backed by an equivalence check.
2. It made specific empirical claims (192 attempts across 8 runs with exactly
   8 winners, a 27.45s stale-owner expiry, a real userspace network blackhole,
   67ms of measured jitter, NAT collapsing client addresses) with **no
   command, configuration, raw output, or artifact reference** a reviewer
   could use to tell an executed test from an assertion. None of that evidence
   existed anywhere in the repository or was linked from it.

This rewrite does two things instead of one narrative pass: it states exactly
what SHA was tested and how this document's claims relate to the current
tree, and it replaces every unaudited empirical number with one produced by
a script committed alongside this document, whose raw output is committed
too.

## What is and isn't established here

**Established, mechanically, by this record:** the claim protocol's
role-identity guarantees — exclusivity under real concurrent connections, the
stale-owner fence, refusal of a stolen token, refusal of `SET ROLE`
laundering, claimant derivation, and independence from both server-side
timestamp parameters and client session time zone. These are proven by
running the protocol's own production code paths
(`command_center.db.roles`, `command_center.db.migrations`,
`render_worker_host_role` — the same functions `tests/db/test_queue_claim.py`
uses) against a real PostgreSQL server, with two independently-authenticated
per-host LOGIN roles standing in for two hosts, which is the actual
mechanism production uses to tell hosts apart.

**Not established here:** this check ran as two roles on *one* physical
machine, connected over a local socket, not across a real network link
between separate hardware. It cannot and does not demonstrate real
inter-host network jitter, a genuine dual-stack TCP partition ("bytes stop
flowing but neither FIN nor RST arrives"), or clock behavior across distinct
physical clocks. The previous record's claims of exactly those things (67ms
measured jitter, a live network blackhole, cross-continental clock
divergence) are not reasserted, because nothing durable backs them. If a
literal multi-physical-host run is performed in the future, it should
produce its own committed evidence rather than be folded into this one.

## Tested SHA and its relationship to the current tree

The protocol was accepted as SRV-04b at `origin/main@f9bb889`
(`feat(db): an atomic execution-attempt claim whose claimant cannot be
declared`, #311). This document's claims are about the protocol as defined in
`command_center/db/sql/0002_queue_claim.up.sql`, and that file's content is
**byte-identical** between `f9bb889` and the commit this document is attached
to — checked mechanically, not asserted:

```console
$ git diff f9bb889 HEAD -- command_center/db/sql/0002_queue_claim.up.sql
# (no output — empty diff)

$ git show f9bb889:command_center/db/sql/0002_queue_claim.up.sql | sha256sum
4899588ad421ce8cdc7dcccd12fcfa6539be019a1cb667153d344aec1a5ed5b6

$ git show HEAD:command_center/db/sql/0002_queue_claim.up.sql | sha256sum
4899588ad421ce8cdc7dcccd12fcfa6539be019a1cb667153d344aec1a5ed5b6
```

`docs/evidence/srv04b-two-host-acceptance/acceptance_check.py` prints this
same hash at the top of its own output, so a future re-run that finds a
different hash is a signal this equivalence has broken and this document
needs revisiting — it is not a standing guarantee, only a checked one as of
the SHA in the log referenced below.

## Reproducing this record

```console
$ AICC_TEST_PG_ADMIN_DSN="host=... port=... dbname=postgres user=<a role that can CREATEDB and CREATEROLE>" \
    python3 docs/evidence/srv04b-two-host-acceptance/acceptance_check.py
```

Requirements match `tests/db/conftest.py`: a real PostgreSQL server, reachable
by an admin-capable role. The script creates its own scratch database and two
scratch per-host roles, runs the checks below, and drops all of it — it
touches nothing else in the target cluster.

The full raw output of one such run is committed at
[`docs/evidence/srv04b-two-host-acceptance/run-2026-09-02.log`](evidence/srv04b-two-host-acceptance/run-2026-09-02.log)
(SHA-256 `a06fccd6f8492618184b5c3dc9dcea3b092ab4d04c3a39e42ba21a46d50e44d0`),
produced against PostgreSQL 16.15 on repo commit `c9d2e23d06814e04f8f44217730781f06f1308bf`.
The numbers quoted below are taken directly from that log.

## Exclusivity, under real concurrent connections

8 rounds of 1 item enqueued and 24 concurrent `queue_claim()` calls released
off one `threading.Barrier`, 12 calls from each of the two per-host roles per
round: 192 attempts, exactly 8 winners (one per round, `[1, 1, 1, 1, 1, 1, 1,
1]` in the log) — `FOR UPDATE SKIP LOCKED` holds exclusivity across two
distinct authenticated identities, not only across threads sharing one
connection's role.

## The stale-owner fence, timed rather than guessed

A claim taken with a 2-second visibility window, from a connection held open
and idle (`pg_stat_activity` confirms the backend is alive throughout — this
is what a stuck-but-connected owner looks like from the server's side, the
scenario the fence exists for, described in
`command_center/db/sql/0002_queue_claim.up.sql:31-38`). The log shows the
attempt reaching `expired` state 2.013s after the claim, against the 2s
window requested — measured by polling, not slept-and-hoped. The still-alive
owner then attempts `queue_complete()` and is refused with `attempt_expired`
(`_queue_owns`, `command_center/db/sql/0002_queue_claim.up.sql:645-650`); the
item was re-delivered to the second role as attempt 2.

## `session_user`, not `current_user`

The claimant check is `a.claimed_by_role IS DISTINCT FROM session_user`
(`command_center/db/sql/0002_queue_claim.up.sql:644-645`), deliberately not
`current_user`, which `SET ROLE` changes
(`command_center/db/sql/0002_queue_claim.up.sql:37-38`). The check granted
host B's role membership in host A's role — the shape a compromised
credential with an over-broad grant would produce — then had host B run `SET
ROLE` to host A before calling `queue_complete` on host A's own attempt. The
log shows `current_user` becoming host A's role while `session_user` stays
host B's, and the call refused with `not_claimant`. A second, simpler check
— host B replaying host A's claim token with no `SET ROLE` at all — is
refused the same way, showing the two attack shapes are both stopped by the
same guard.

## Claimant forgery

Run as the schema owner (`aicc_migrator`) rather than as a worker, so the
refusal is the trigger and not a missing table grant: a direct `INSERT` into
`work_attempt` naming a `claimed_by_role` other than the inserting
`session_user` is rejected by `trg_work_attempt_claimant_is_derived`
(`work_attempt_claimant_is_derived()`,
`command_center/db/sql/0002_queue_claim.up.sql:422-427`) with `claimant is
derived, not declared`. In production this path is doubly closed — workers
hold no table privilege on `work_attempt` at all, only `EXECUTE` on the
protocol's functions (`command_center/db/roles.py`) — so this check
deliberately runs as the one role that *does* have the table privilege, to
prove the trigger itself refuses, not the grant.

## Clocks: no dependency, checked both ways

`pg_get_function_arguments()` was read directly from `pg_catalog.pg_proc` for
every `queue_*`/`_queue_*` function: 11 functions, each listed with its full
signature in the log, zero with a timestamp-typed parameter. This is a
structural fact about the schema, not a sampled behavior — the query is
exhaustive over every function this migration defines. Separately, two claims
made in sessions with `SET TIME ZONE 'Etc/GMT-14'` (UTC+14) and `SET TIME
ZONE 'Etc/GMT+12'` (UTC-12) against a 100-second visibility window produced
`visible_until` values 0.012s apart when compared as absolute instants — the
window is computed from the server's `now()`, and the client's session time
zone does not participate.

## Address-based attribution

`inet_client_addr()` was read from both roles' sessions. Both connections
used the Unix domain socket most local test/dev PostgreSQL setups default to,
so both came back `NULL` — no IP address exists for either session to compare
at all, a stronger version of the same lesson the previous (unverified) NAT
claim was reaching for: this protocol's claimant check has never depended on
network-layer identity, and this record shows a case where that identity is
not merely shared but entirely absent, and the protocol's role-based
attribution (`session_user`) is unaffected either way.

## Named limits of this record

- **Single machine, not physically separate hosts.** Both "hosts" are
  per-host LOGIN roles on one PostgreSQL server reached over a local socket.
  No real network link, real jitter, or real host-to-host partition was
  involved. What is proven is the protocol's role-identity model; what is not
  attempted is a hardware network-partition drill.
- **This run's PostgreSQL server was on Linux** (Ubuntu 24.04, kernel per the
  host environment) — the opposite limitation from the previous, unverified
  record, which claimed a non-Linux database host and offered no artifact for
  it. This record makes no claim about behavior under a non-Linux database
  host in either direction; it simply was not exercised, so none is made.
- Timing numbers (the 2.013s expiry, the 0.012s time-zone delta) are this
  run's measurements, not architectural guarantees; they will vary run to
  run within the bounds the protocol's design implies (expiry cannot fire
  before the requested visibility window, and the time-zone delta should
  stay near zero because it reflects server-side clock skew during the test,
  not client time zone leaking in).

## Summary

Re-executed against a real PostgreSQL server on the SHA whose relevant SQL is
proven byte-identical to the SRV-04b acceptance commit (`f9bb889`):
exclusivity, the stale-owner fence, `SET ROLE`-laundering refusal, cross-role
token-theft refusal, claimant-forgery refusal, and independence from both
server-side timestamp parameters and client time zone all hold, with raw,
reproducible output committed at
[`docs/evidence/srv04b-two-host-acceptance/run-2026-09-02.log`](evidence/srv04b-two-host-acceptance/run-2026-09-02.log).
The claim is scoped to what a two-role, single-server rig can show; it is not
a substitute for a literal multi-physical-host network-partition test, and
does not claim to be one.
