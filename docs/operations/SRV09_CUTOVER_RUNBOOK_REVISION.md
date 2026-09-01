# SRV-09 cutover runbook (revision)

This is the operator runbook for cutting the server deployment's runtime
store over from SQLite (authority today) to PostgreSQL. It supersedes
`SRV09_CUTOVER_RUNBOOK_DRAFT.md`, which was checked out at
`b0d2dfa6c467771933734be28397d2f218c99a20` (2026-08-19) — 66 commits behind
`origin/main` at the time this revision was written
(`67a996be26482b6579edc337c75af9e1913c7d56`, 2026-08-26). The two files are
kept side by side on purpose: Appendix A re-verifies every reference the
draft made, one at a time, against current `origin/main`, rather than
silently patching the draft in place. Of 51 references the draft made, 36
held, 12 changed, and 3 are now flatly wrong. **Read the draft as history,
run this one.**

The single most consequential change is **§4, step A0b**: the draft never
checked, and never acted on, the gap between the schema version the code
expects and the schema version the live database is actually running. That
gap is real, it is large, and closing it has to happen before backfill
starts, not during it.

**Re-verification pass 2 (2026-09-01, against `f799f78`):** this revision
itself drifted the same way the draft did, just faster — one relevant commit
landed within five days of it being written.
`VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY` (#473) added SQLite
schema migration 25 and its PostgreSQL counterpart (`0016`), and it breaks
the assumption A0b was built on: that every migration since 16 is a safe,
idempotent `ALTER TABLE ... ADD COLUMN`. Migration 25 is not that shape — it
fails closed against any active or unfinalized run, which means running A0b
exactly as originally written, before admission freeze, now stops partway.
See the corrected §4 and §8, and Appendix A's second pass, below.

## 1. Purpose and scope

Cut the server deployment's runtime store over from SQLite (authority
today) to PostgreSQL, without losing a write and without a rollback path
that turns out to be a lie. Assumes `VOYN-W0-AICC-SRV-01a` (PostgreSQL
foundation), `VOYN-W0-AICC-SRV-01B` (all-33-tables mirror) and
`VOYN-W0-AICC-SRV-09-FINALIZED-AT` are merged.

**Phase B — fully retiring the SQLite write path — is not authorized by
this document.** See §13.

## 2. Current state

- Roles: `aicc_migrator` / `aicc_app` / `aicc_worker`, per
  `docs/postgres-foundation.md` (unchanged since the draft).
- Pooling: `command_center/db/pool.py` now supports a hot pool swap —
  `replace_pool()` opens and verifies a replacement pool and atomically
  swaps the pointer while checked-out sessions drain, raising
  `PoolReplacedError` rather than silently orphaning callers holding the
  old pool. This shipped for `command_center/ops/credential_rotation.py`
  (worker-host credential rotation), not for cutover — but it is the
  mechanism `READ-POOL` would build on, and it means a credential change no
  longer implies a process restart. The draft's pooling model (one pool,
  opened once, restart to change it) is stale.
- Migrations applied: `0001_initial` through `0016_run_finalization_claim`
  (was `0014` when this revision was first written — `0015` and `0016`
  landed after). Migrations `0005`–`0016` add tables (`backlog_store`, the
  work-queue control plane, credential-expiry tracking, the tick-scheduler
  scan cursor, the run-finalization claim fence, ...) that are **outside**
  the 33-table correspondence map in `docs/srv01b-schema-map.md`. That map is
  still accurate for the tables it covers — it just no longer covers the
  whole schema, and it covers less of it with every wave. A reconciliation
  plan scoped to "the 33 tables in the map" now undercounts what PostgreSQL
  actually holds, by a growing margin.
- Generic PostgreSQL machinery still lives in `aios-db`, still consumed
  only through `command_center/db/adapter.py` — unchanged.
- The mutating HTTP surface now requires authentication
  (`VOYN-W0-AICC-AUTH-HTTP-01`, #316, merged the commit immediately after
  the draft's checkout). Its own merge description retracts an assumption
  worth repeating here: "the surface was 29 mutating routes, not the two
  that were assumed." Any cutover step that pauses admission by calling an
  API route directly must go through the same authenticated path
  operators use for everything else; there is no unauthenticated back door
  left to lean on.

## 3. Schema correspondence baseline

Unchanged from the draft (§3): 33 domain tables, 1:1, no orphans, 114/395
columns change type and 105 need value conversion — 75 `TEXT` →
`timestamptz` (the naive-local-time risk in `iso_now()`), 7 identity
columns. See caveat in §2 about the tables added since: they are real
PostgreSQL tables with no SQLite backfill story, for two different reasons.
Most (`command_center/db/work_queue_store.py`, the backlog stores) were
never SQLite tables at all — PostgreSQL is native authority for them from
birth. `run_finalization_claim` (`0016`) is different again: it has a SQLite
counterpart (migration 25, see §4), but the two are deliberately *not*
mirrored to each other, because the claim's `owner_pid` / process-identity
fields are only meaningful on the host that took the claim — a best-effort
cross-engine mirror cannot preserve that compare-and-swap semantics.
`tests/db/test_mirror_coverage.py`'s `UNMIRRORED_SCHEMA_TABLES` carries a
signed exclusion, with reason and owning task, for every one of these —
that is the authoritative list, not this paragraph's count. None of them
block this cutover (none have a SQLite side to reconcile against) but they
must not be swept into "the 33 tables" bucket when someone counts what got
mirrored.

## 4. Preflight (A0)

1. Confirm `python -m command_center.db status` reports every PostgreSQL
   migration applied through `0016` (was `0014` at this revision's first
   pass — re-check the current highest-numbered file in
   `command_center/db/sql/` before relying on this digit) and its checksum
   verified.
2. Confirm `GET /readyz` is `200`.
3. Confirm the three-role grant matrix with
   `tests/db/test_postgres_integration.py` — now also covering the
   backlog-table grants added by `0005`–`0014`.
4. Take a verified backup: `scripts/aicc_pg_backup.sh --out-dir
   /var/backups/aicc --verify --keep 14`, then run the restore drill
   (`scripts/aicc_pg_restore.sh ... --target-db aicc_restore_check`)
   immediately before, not weeks before.

### A0b — raise the live SQLite schema before anything else

Do this **before** A0.1–A0.4, not alongside them.

`command_center/runtime/db/core.py:current_schema_version()` reads
`MAX(version) FROM schema_version` from the live database file. Run it
against the production SQLite file, not against a checkout of the code:

```
python -c "from command_center.runtime.db.core import current_schema_version; \
  print(current_schema_version(PATH_TO_PROD_DB))"
```

The draft observed this returning **16** against a codebase whose
`SCHEMA_VERSION` was **23** at the time, named the seven-version gap, and
then never scheduled a step to close it (draft §2, §4 — no such step
exists). Left alone, that gap only grows: `SCHEMA_VERSION` is **25** as of
this revision's second pass (migration 24, `VOYN-W0-AICC-SRV-09-FINALIZED-AT`,
added `run.finalized_at` — see §9; migration 25,
`VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY`, added
`run_finalization_claim` — see below). If A0b is not run, the backfill
importer reads a live schema that is now **nine** migrations behind the code
doing the reading, not seven, and the importer's column list is generated
from the *code's* schema, not the live one, so a live database still on 16
is missing columns the importer will try to read.

**Everything up through migration 24 is still the idempotent
`ALTER TABLE ... ADD COLUMN` shape (2, 3, 4, 9, 11, 24 are named examples)
and is safe to run against a live production database with in-flight work —
that part of A0b is unchanged. Migration 25 breaks the pattern: running
plain `migrate()` against a live database will now stop at 24→25.**
`_migration_25_add_finalization_claim`
(`command_center/runtime/db/schema.py`) fails closed — raises
`FinalizationClaimCutoverRequired` — against *any* row that is not both in a
terminal state and already finalized, i.e. against any active run at all.
That is deliberate: version-24 writers cannot honor the new claim table, so
a rolling upgrade could let v25 code mistake a legacy in-flight row's
missing claim for an abandoned one. There is no bypass in the ordinary
migration path — only an explicit, separately-invoked offline procedure
(below) is allowed to cross this specific version boundary.

So A0b now runs in two parts:

1. Before A0.1–A0.4, as originally written: run
   `python -m command_center.runtime.db migrate` against production. This
   carries the live database from whatever it is on up through **24** —
   still safe with production live and admission open — and will stop
   there with `FinalizationClaimCutoverRequired` if it tries to cross into
   25 while runs are active. That refusal is expected at this point in the
   sequence, not a bug to route around.
2. **The 24→25 leg moves into the cutover sequence, at §8, step 4** (new —
   see below): only after admission is frozen (§8.2) and
   `count_unfinalized_runs()` has reached zero (§8.3), run
   `python scripts/execution_center_debug.py offline-finalization-cutover
   --confirm-offline`
   (`command_center/runtime/db/schema.py`'s
   `bootstrap_finalization_claim_cutover`, invoked from
   `scripts/execution_center_debug.py`). It re-checks the same
   zero-active/zero-unfinalized precondition itself — the drain in §8.3 is
   necessary for it to succeed, but the command does not trust the operator
   to have verified that, it verifies again. Confirm
   `current_schema_version()` reads **25** before proceeding to step 8.5
   (the backfill importer's final pass).

Re-check `SCHEMA_VERSION` at execution time regardless — 25 is today's
number and it moves, same caveat as the draft's 23 and this revision's
original 24.

Do not confuse this with the "16 domain tables" figure in
`docs/srv01b-schema-map.md` — that is an unrelated, already-retired
early-snapshot table *count*, not a schema *version*. Same digit, two
different measurements; the map itself already calls its own figure
obsolete.

## 5. Backfill / dual-write posture

Unchanged mechanism from the draft (§5): every domain table dual-writes
through `command_center/record_mirror.py`'s `RecordMirror` protocol
(`upsert`, safe to re-run), `command_center/db/table_mirror.py`, and
`command_center/db/mirror_support.py`, direction SQLite (authority) →
PostgreSQL (mirror), fired from the `_mirror_*` calls in
`command_center/runtime/db/schema.py` after each SQLite commit.

One addition since the draft: `command_center/db/run_store.py`'s mirrored
`run` row grew from 41 columns to 42 — `finalized_at` is appended at the
end of `RUN_COLUMNS`, both engines add it in the same migration-order
position. It is nullable and never backfilled for pre-existing rows on
either side (see §9), so the extra column does not change how the backfill
importer's reconciliation math works, only what it should expect to see
`NULL` for.

## 6. Reconciliation gate

Unchanged automated gates: `tests/db/test_schema_correspondence.py` and
`tests/db/test_mirror_coverage.py` (`UNMIRRORED_SCHEMA_TABLES`). Both must
be green on the commit being cut over. They cover the 33-table
correspondence map only (§3) — they do not, and are not meant to, cover
the PostgreSQL-only tables added since the draft, each of which instead
carries its own signed `UNMIRRORED_SCHEMA_TABLES` exclusion (reason +
owning task) — `run_finalization_claim` and `backlog_scan_cursor` are the
two newest, both landing after this revision's first pass. Grep that dict
for the current count; do not copy a number from this paragraph into an
operator checklist, it will already be stale by the time it's read.

## 7. Read path

Still unchanged, still out of scope: reads stay on SQLite through cutover
day. `READ-SWITCH-MISSING` names exactly this absence and remains
undelivered — there is no `AICC_DB_BACKEND`-style flag or equivalent
anywhere in this codebase. Do not infer one exists because `replace_pool()`
now does (§2); a swappable pool is necessary for a read switch, not
sufficient.

## 8. Cutover sequence

1. Complete A0b part 1 (through schema **24**), then A0.1–A0.4.
2. Freeze new work admission (stop the dispatcher / queue enqueue) through
   the now-authenticated mutating surface (§2).
3. Wait for `count_unfinalized_runs()` — not `run.state` — to reach zero
   for every in-flight run. See §9 for why this replaces the draft's
   state-polling step.
4. **(new)** Complete A0b part 2: run `offline-finalization-cutover
   --confirm-offline` to cross schema **24→25**. Zero-unfinalized from step
   3 is what makes this succeed instead of refusing — do not reorder it
   ahead of step 3.
5. Run the backfill importer's final incremental pass.
6. Flip the application's write authority flag to PostgreSQL.
7. Resume admission.

## 9. Point of no return

**The point of no return is not the flag flip in step 8.6, not a service
restart, and not a gate turning green.** It is the first write that lands
in PostgreSQL as authority and is *not* also mirrored back into SQLite.

That distinction matters because mirroring today is one-directional:
SQLite → PostgreSQL, never the reverse (§5). `REVERSE-MIRROR` — the
mechanism that would make a post-flip PostgreSQL write show up back in
SQLite — is designed and prototyped, not built (§13). Until it exists,
flipping the flag back after the first authoritative PostgreSQL write is
not a rollback; it is a rollback to a SQLite database that is missing
every write that happened while PostgreSQL was authoritative. The flag
flip itself is trivially reversible in isolation — the danger is
everything that gets written in the seconds or minutes after it, before
anyone notices something is wrong.

This is also why step 8.3 uses `count_unfinalized_runs()`
(`command_center/runtime/db/execution.py`) instead of polling
`run.state == 'COMPLETED'`, which is what the draft did (draft §8.2, "Wait
for `run.state` to reach a terminal value"). `run.finalized_at` — added by
migration 24, `VOYN-W0-AICC-SRV-09-FINALIZED-AT` — exists precisely
because state alone lies here: `_supervise` commits the terminal row
before appending the `process_exited` audit event, auto-committing the
agent's work and saving the report, and measurement over 20 runs put that
window at a 6.1 ms median on a clean tree and 139 ms median (152 ms max)
on a changed one. A run read as `COMPLETED` in that window has not
actually finished writing everything a cutover needs durable. The
migration's own docstring calls this out by name: `finalized_at` is
readable "from *another process*", which "the operator running a cutover
does not have" from `Supervisor.wait_for_run`'s in-memory registry alone.
Waiting on `run.state` instead of `count_unfinalized_runs()` is exactly
the mistake that comment exists to prevent, and it is the mistake the
draft made.

## 10. Rollback triggers (S1–S8)

Roll back — flip the write-authority flag back to SQLite — if any of:

- **S1** — `/readyz` reports a schema mismatch against
  `EXPECTED_SCHEMA_VERSION` after the flip.
- **S2** — sustained `AICC_PG_POOL_TIMEOUT` breaches (pool exhaustion) for
  longer than one minute.
- **S3** — `tests/db/test_schema_correspondence.py` or the mirror-coverage
  check, re-run against the post-flip database, reports any divergence.
- **S4** — `count_unfinalized_runs()` does not reach zero within the
  bounded window from step 8.3, or rises again after the flip.
- **S5** — the queue's dead-letter rate (`VOYN-W0-AICC-SRV-06` watchdog)
  exceeds its pre-cutover baseline.
- **S6** — the immediate post-flip backup/restore drill
  (`scripts/aicc_pg_restore.sh`) fails its integrity check.
- **S7** — any write is confirmed to have landed in PostgreSQL with no
  corresponding SQLite row (the point-of-no-return condition in §9 has
  actually occurred). This is a "stop and assess," not an automatic
  rollback — by the time S7 fires, rolling back sheds data rather than
  protecting it. Whether to roll back anyway is an operator judgment call
  against the size of what would be lost.
- **S8** — elevated failure rate on the (now-authenticated, §2) mutating
  HTTP surface attributable to the cutover.

**S8 is the only deliberately weakened trigger in this list, and it is
weakened on purpose.** Every other trigger fires on the first occurrence.
S8 does not: it fires on a *rate over a rolling window*, not on any single
failed request, because `VOYN-W0-AICC-AUTH-HTTP-01` added a network round
trip to every mutating request — AICC forwards the caller's platform
bearer credential to `GET /api/v1/whoami` and authorizes from the
reflected principal id. That round trip's own latency and transient-error
jitter is indistinguishable, on a single failure, from a real
cutover-caused failure, and it existed before this runbook and will exist
after it — treating it as a rollback signal would make S8 fire on
authentication-service noise unrelated to database integrity. The
compensating control is an **absolute ceiling**: regardless of rate, a
fixed count of mutating-surface failures within the window trips S8
unconditionally, so a real outage cannot hide inside "it's just under the
rate threshold." Tune the rate and the ceiling from the pre-cutover
baseline error rate on that surface; do not ship this runbook with
placeholder numbers in a production cutover.

## 11. Backup and operational safety

Unchanged from the draft: nightly `scripts/aicc_pg_backup.sh` and the
`scripts/aicc_pg_restore.sh` drill are proven end to end by
`test_backup_restore_drill_round_trips_data`. Run the drill again
immediately before step 8.1, not on the general nightly cadence — see A0b
for why "already scheduled" and "verified for this cutover" are not the
same claim.

One addition: worker-host database credential rotation
(`command_center/ops/credential_rotation.py`) did not exist when the draft
was written and is now a live, independently-scheduled process. It can run
concurrently with a cutover window because it uses the same hot pool-swap
mechanism (§2) rather than a restart. Confirm with whoever owns that
service that no rotation is scheduled to land inside the cutover window
before step 8.1 — not because it would fail, but because two independent
changes to database connectivity in the same window make S1–S8 harder to
attribute correctly.

## 12. Post-cutover validation

Confirm `/readyz` stays `200`, confirm the queue keeps draining, confirm
`count_unfinalized_runs()` stays at its steady-state baseline (not
necessarily zero — new runs will be in flight), and watch S1–S8 for one
full business day before considering the SQLite write path a candidate
for decommission. Decommissioning it is Phase B and is not authorized by
this document (§13).

## 13. Phase B authorization status

**Not authorized.** The mechanisms Phase B depends on are designed and
prototyped, not built, in this codebase as of `67a996b` — still true as of
the second pass's `f799f78`; none of the six items below were touched by
the drift documented in Appendix A's second pass:

- `READ-SWITCH-MISSING` — no read-path selector exists (§7).
- `REVERSE-MIRROR` — no PostgreSQL → SQLite mirror exists (§9); this is
  the one that actually gates the point of no return.
- `READ-POOL` — `replace_pool()` (§2) is a prerequisite building block,
  built for credential rotation, not yet wired to a read-side pool split.
- `DAILY-AUDIT-UNMIRRORED` — no scheduled job audits for rows that reached
  PostgreSQL without a mirrored SQLite counterpart (or the reverse, once
  `REVERSE-MIRROR` exists).
- `QUERY-DIALECT-FIX` — SQL dialect differences between the two engines'
  query paths are not yet reconciled outside the mirrored-write path.
- `PARITY-QUERY-EQUIVALENCE` — no proof exists that reads against either
  engine return equivalent results for the same logical query.

This runbook covers write-authority cutover only. Treat any request to
retire the SQLite write path, or to serve reads from PostgreSQL, as a
separate, not-yet-scoped piece of work until the six items above land.

## Appendix A — reference verification

Every reference the draft made, checked against `origin/main` at
`67a996b`. Draft baseline: `b0d2dfa` (2026-08-19). 51 references: **36
held, 12 changed, 3 disappeared.**

### Held (36) — unchanged in identity and behavior

| # | Reference | Draft claim |
| --- | --- | --- |
| 1 | `command_center/db/adapter.py` | sole `aios_db` import boundary |
| 2 | `docs/postgres-foundation.md` | env var table (all 11 vars) |
| 3 | `docs/postgres-foundation.md` | `AICC_PG_HOST` required, no default |
| 4 | `docs/postgres-foundation.md` | `AICC_PG_SSLMODE=verify-full` default, loopback exemption |
| 5 | `docs/postgres-foundation.md` | three-role table (`aicc_migrator`/`aicc_app`/`aicc_worker`) |
| 6 | `docs/postgres-foundation.md` | worker column carve-outs (`completion.review_*`) |
| 7 | `docs/postgres-foundation.md` | `schema_migration` read-only for non-migrator roles |
| 8 | `docs/postgres-foundation.md` | no `DELETE` grant on any table |
| 9 | `docs/postgres-foundation.md` | advisory lock key `aicc:schema-migration` |
| 10 | `docs/postgres-foundation.md` | `/healthz` never touches the database |
| 11 | `docs/postgres-foundation.md` | `/readyz` 200 only on schema match |
| 12 | `docs/postgres-foundation.md` | `scripts/aicc_pg_backup.sh --out-dir ... --verify --keep 14` |
| 13 | `docs/postgres-foundation.md` | `--allow-overwrite` gate on restore |
| 14 | `docs/postgres-foundation.md` | `pg_dump` client must be ≥ server version |
| 15 | `docs/postgres-foundation.md` | type strategy; `owner_item.due` / `digest_item.day` stay text |
| 16 | `docs/srv01b-schema-map.md` | 33 domain tables, 1:1, no orphans |
| 17 | `docs/srv01b-schema-map.md` | 114/395 columns change type, 105 need conversion |
| 18 | `docs/srv01b-schema-map.md` | 75 `TEXT`→`timestamptz`, `iso_now()` naive-local-time risk |
| 19 | `docs/srv01b-schema-map.md` | 7 identity columns |
| 20 | `command_center/record_mirror.py` | `RecordMirror.upsert`, safe to re-run |
| 21 | `command_center/db/table_mirror.py` | one mirror implementation per table |
| 22 | `command_center/db/mirror_support.py` | shared mirror machinery |
| 23 | `command_center/db/queue_store.py` | whole-list replacement contract |
| 24 | `command_center/db/owner_item_store.py` | row-oriented, versioned upsert contract |
| 25 | `command_center/db/digest_item_store.py` | jsonb comparison + delete path |
| 26 | `command_center/db/conflict_store.py` | dual-write conflict mirror |
| 27 | `command_center/db/model_registry_store.py` | identity-column mirror |
| 28 | `command_center/db/provenance_store.py` | composite-key mirror |
| 29 | `command_center/db/council_store.py` | council-family mirror |
| 30 | `command_center/runtime/db/core.py` | `current_schema_version()` reads `MAX(version) FROM schema_version` |
| 31 | `command_center/runtime/db/schema.py` | idempotent `ALTER TABLE ... ADD COLUMN` migration shape |
| 32 | `tests/db/test_schema_correspondence.py` | CI-enforced structural parity |
| 33 | `tests/db/test_mirror_coverage.py` | `UNMIRRORED_SCHEMA_TABLES` staleness gate |
| 34 | `command_center/db/sql/0001_initial.up.sql` | initial 33-table PostgreSQL schema |
| 35 | `command_center/db/sql/0002_queue_claim.up.sql` | queue claim functions |
| 36 | `command_center/db/sql/0003_worker_enrollment.up.sql` | worker enrollment/ticket |

### Changed (12) — same reference, different current behavior

| # | Reference | Draft said | Now |
| --- | --- | --- | --- |
| 37 | `command_center/runtime/db/schema.py` `SCHEMA_VERSION` | 23 | **24** (migration 24, `finalized_at`) |
| 38 | `command_center/db/run_store.py` `RUN_COLUMNS` | 41 columns | **42** — `finalized_at` appended |
| 39 | `command_center/db/pool.py` `open_pool()` / `get_pool()` | one pool, opened once, restart to change | **hot-swappable**: `replace_pool()` / `PoolReplacedError` |
| 40 | `command_center/db/roles.py` `render_table_grants` | fixed grant set | gained `_WORKER_BACKLOG_TABLES` and existence-checking helpers |
| 41 | `command_center/db/work_queue_store.py` | claim/complete/fail only | gained `enqueue()`, exactly-once by idempotency key, `aicc_app`-only |
| 42 | `command_center/runtime/run_finalizer.py` | terminal-state persistence only | now owns the `finalized_at` durability contract |
| 43 | `command_center/runtime/supervisor.py` | — | terminal-state path restructured around finalization ordering |
| 44 | `command_center/runtime/db/execution.py` | no automated "all runs finished writing" signal (draft "Open items") | **`count_unfinalized_runs()` / `list_unfinalized_runs()` now exist** |
| 45 | `scripts/preflight.sh` | 3-stage (whitespace / ruff / byte-compile) | **4-stage** — added the public-repo leak guard |
| 46 | `scripts/assert_independent_acceptance.py` | — | verification-gate logic materially expanded |
| 47 | mutating HTTP surface (`command_center/webapi/app.py`, `http_auth/routing.py`) | unauthenticated (draft's implicit assumption, checked out one commit before `VOYN-W0-AICC-AUTH-HTTP-01`) | **requires forwarded platform auth**, 29 routes in scope |
| 48 | `command_center/ops/credential_rotation.py` | did not exist | live, independently-scheduled worker-credential rotation |

### Disappeared (3) — the draft's own basis is gone

| # | Reference | Why it no longer holds |
| --- | --- | --- |
| 49 | "the mutating surface is two routes" | `VOYN-W0-AICC-AUTH-HTTP-01`'s own merge description retracts this: "the surface was 29 mutating routes, not the two that were assumed." Not drift — the assumption was already wrong when the draft's checkout was cut, the codebase just hadn't said so yet. |
| 50 | "16 domain tables" (as a table-count baseline, distinct from the A0b schema-version reading of the same digit) | `docs/srv01b-schema-map.md` — unchanged since the draft — already calls this figure obsolete in its own text: taken before waves W1–W3, and understated by more than half. Citing it for anything is citing a number the codebase disowns. |
| 51 | "`run.state == 'COMPLETED'` tells another process a run is done" | Migration 24's own docstring retracts this for the cutover use case specifically: the window between the terminal-state commit and the report/auto-commit finishing is real and measured (§9), and `finalized_at` exists because state alone was an insufficient signal for exactly this operator. |

### Second pass (2026-09-01, against `f799f78`) — this revision re-verified

This revision was itself checked out at `67a996b`. Five days later,
`f799f78` is 28 commits ahead. Most of that drift is unrelated to cutover
(installer control profiles, systemd unit handling, CI build changes, a
publish-window scheduler) and does not appear below — this is not a full
51-reference re-audit, only the commits that touch something this document
makes a claim about. One did: `VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY`
(#473). A second, unrelated commit
(`VOYN-OPS-AICC-PUBLISH-WINDOW-STARVATION`, #443) also lands one of the two
new PostgreSQL tables cited below, incidentally.

| # | Reference | First-pass claim | Now |
| --- | --- | --- | --- |
| 52 | `command_center/runtime/db/schema.py` `SCHEMA_VERSION` | 24 | **25** — migration 25 adds `run_finalization_claim` |
| 53 | A0b's "every migration since 16 is a safe idempotent `ADD COLUMN`" | true of 17–24 | **false of 25** — `_migration_25_add_finalization_claim` fails closed against any active/unfinalized run; requires the separate `offline-finalization-cutover` procedure, which only succeeds after admission freeze + drain (§8.2–8.3), not during preflight |
| 54 | PostgreSQL migrations (`command_center/db/sql/`) | `0001`–`0014` | **`0001`–`0016`** — `0015_backlog_scan_cursor` (PG-native tick-scheduler state, unrelated to the 33-table map), `0016_run_finalization_claim` (PG target for migration 25's table, deliberately not dual-written) |
| 55 | `command_center/db/roles.py` `PRIVILEGES` | no finalization-claim entry | gained `_FINALIZATION_CLAIM_TABLES`, granted to `aicc_app`, `aicc_worker`, and the operator enrolment role |
| 56 | `tests/db/test_mirror_coverage.py` `UNMIRRORED_SCHEMA_TABLES` | did not name either table | both `run_finalization_claim` and `backlog_scan_cursor` now carry signed exclusions with reason and owning task |

The lesson this second pass adds to the one A0b already teaches: **a
revision's own baseline commit is a claim with a shelf life, not a fact.**
Treat the `f799f78` baseline above the same way this document treats
`67a996b` — re-run this check against current `main` before executing this
runbook, don't trust either date.
