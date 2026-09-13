# `queue_entry`, the SRV-07 parity gate, and why the claim protocol left it alone

Snapshot: 2026-09-06, against `main` tip (`1efba23d`). Written for
VOYN-W0-AICC-QUEUE-ENTRY-PARITY after the 2026-08-20 backlog triage
(`VOYN-W0-BACKLOG-RECONCILE-ALL`) flagged the status note below as reconciled
and asked for it to be closed with citable evidence rather than a triage
comment alone. This document is that evidence: it names, for each clause of
the note, the exact file/test that makes it true today, and disambiguates one
phrase ("outside the parity gate") that reads as an exemption from schema
checking and is not.

The status note this closes:

> `queue_entry` вне гейта паритета SRV-07. Это и есть причина, по которой
> миграция очереди её не трогает: таблица — зеркало, её синхронизация делает
> `DELETE` плюс массовую перевставку, поэтому claim-состояние уничтожалось бы
> молча, причём роль воркера не имеет `DELETE` и не увидела бы даже факта
> потери.

## The one thing "outside the gate" does not mean

`queue_entry` is a completely ordinary participant in the SRV-07 parity gate
(`tests/db/test_schema_correspondence.py`): it is `CREATE TABLE`d by
`0001_initial.up.sql` on the PostgreSQL side and by
`command_center/runtime/db/schema.py` on the SQLite side, so its columns,
primary key, and indexes are compared like every other table's in
`test_no_column_is_left_behind`, `test_primary_keys_agree`, and
`test_every_sqlite_index_keeps_its_column_set`. There is no exclusion list in
that test module and none is needed — nothing carves `queue_entry` out of the
0001 baseline.

What *is* true, and what `roles.py`'s "sits outside the SRV-07 parity gate"
comment (`command_center/db/roles.py:412-414`) is actually naming, is
narrower: the gate's coverage of migrations **after** 0001 is curated by
hand, not automatic. `test_schema_correspondence.py`'s
`CORRESPONDING_MIGRATIONS` tuple lists only the post-0001 migrations that
alter a table both engines share (currently `0004_run_finalized_at.up.sql`
and `0016_run_finalization_claim.up.sql`); a migration that only creates
PostgreSQL-native objects — 0002, the claim protocol — is deliberately left
out, because folding it in would make every table-count assertion fail for
`work_item` and its family, which have no SQLite source at all. `queue_entry`
being "outside" that later scope only matters because 0002 never touches it —
confirmed structurally, not by convention:

`tests/db/test_queue_claim.py::test_the_queue_mirror_is_untouched_by_this_migration`
greps the claim migration's up/down SQL for the literal string `queue_entry`
and asserts zero matches, then round-trips upgrade→downgrade→upgrade and
asserts `information_schema.columns` for `queue_entry` is byte-identical
before and after. If a future migration ever did reshape `queue_entry`, nothing
would automatically add it to `CORRESPONDING_MIGRATIONS`, and *that* — a
manually-curated list someone forgets to extend — is the real shape of "outside
the gate." It is a coverage-discipline note, not a claim that the table's
current shape goes unchecked.

## Why the claim protocol (0002) leaves `queue_entry` alone

`queue_entry`'s PostgreSQL mirror (`command_center/db/queue_store.py`,
`PostgresQueueMirror`) is documented as whole-list replacement: `replace_entries`
runs `DELETE FROM queue_entry` followed by a bulk `INSERT` inside one
transaction (`command_center/db/queue_store.py:88-98`), the same contract the
SQLite mirror already had (`command_center/runtime/db/execution.py:1237-1240`,
predating this migration under ADR 0007). That contract is *idempotent per
replace* by design (`command_center/queue_store.py:34-38`): the backfill from
`execution_queue.json` is expected to run more than once, and the whole point
of DELETE-then-reinsert is that a reader never observes a half-rebuilt queue.

The cost of that contract is that anything written to `queue_entry` outside of
a `replace_entries` call does not survive the next sync — there is no
per-row upsert path for it to merge into. `roles.py` names the historical
version of this directly: `queue_entry` used to carry a worker-writable
`UPDATE` grant, labelled "claims, never enqueues," and it was removed because
`queue_store.replace_entries()` "rebuilds this mirror wholesale on every sync
from the authoritative JSON queue, so a claim written here is destroyed by the
next sync, silently" (`command_center/db/roles.py:403-410`). That is exactly
the failure mode the status note describes. The claim protocol added by 0002
does not reuse `queue_entry` for claim state for this reason — it gave
claims their own tables (`work_item`, `work_attempt`, `work_result`,
`work_event`), reachable only through the four `queue_*` functions
(`queue_claim`, `queue_heartbeat`, `queue_complete`, `queue_fail`), specifically
so that a claim is never a row a whole-list replace can silently discard.

## Why the worker's read-only access can't catch the loss either

`aicc_worker`'s grant on `queue_entry` is `SELECT` only
(`command_center/db/roles.py:415`, `_WORKER_TABLES["queue_entry"] = _READ`),
and no role — worker, app, or operator — is ever granted `DELETE` on any
table: `render_table_grants()`'s docstring states the policy directly ("this
schema is an append/update ledger, and row removal is a migration-time
operation performed by the owner," `command_center/db/roles.py:36-37`), and
the `PRIVILEGES` matrix has no `DELETE` entry for any role on any table. A
worker holding only `SELECT` can observe whatever `queue_entry` currently
contains, but a whole-list replace leaves no tombstone and no version column
to diff against, so a row present at one read and gone at the next is
indistinguishable from a row that was never dispatched to begin with — there
is nothing in the schema for a `SELECT`-only reader to notice the loss with.
That is what "не увидела бы даже факта потери" is asserting, and it is a
structural consequence of the grant model and the mirror contract together,
not a gap in either one on its own.

This is enforced live, generically, rather than by a `queue_entry`-specific
test: `tests/db/test_grant_compliance.py::test_compliance_passes_when_grants_are_applied`
connects to a fully migrated-and-granted database and asserts the catalog has
**zero** privileges beyond what `roles.PRIVILEGES` declares, for every table
including `queue_entry`; any stray `GRANT DELETE` on any table, to any role,
shows up as an `EXTRA:` violation and fails that test
(`tests/db/test_grant_compliance.py:227-231`). Its paired negative,
`test_compliance_fails_when_grants_are_not_applied`, proves the check is not a
tautology that would pass regardless. (Skipped without a live PostgreSQL —
`AICC_TEST_PG_ADMIN_DSN` unset — same as the rest of `tests/db/`; see that
module's own conftest.)

## Where the decision is recorded

`queue_entry` is a signed exclusion in the mirror-coverage gate
(`tests/db/test_mirror_coverage.py:147-159`, `UNMIRRORED_SCHEMA_TABLES` —
despite the name, this is the *shared-contract* exclusion list: `queue_entry`
has its own contract, `replace_entries`/`list_entries`, rather than the
`upsert`/`list_records` every other mirrored table shares via
`PostgresTableMirror`), already naming this task as the owner of the decision.
That gate (`tests/db/test_mirror_coverage.py`) fails on a table that is
neither mirrored under the shared contract nor declared here, so the
exclusion is provably load-bearing, not decorative — see that module's own
`test_the_coverage_gate_fails_on_...` cases.

## Conclusion

Every clause of the status note matches the live code and is defended by a
test that would fail if it stopped being true:

| Clause | Where it is enforced |
| --- | --- |
| `queue_entry` sits outside the (post-0001) parity gate | `tests/db/test_queue_claim.py::test_the_queue_mirror_is_untouched_by_this_migration` |
| Mirror sync is DELETE + bulk reinsert | `command_center/db/queue_store.py::PostgresQueueMirror.replace_entries` |
| A claim written only to the mirror is destroyed silently | `command_center/db/roles.py:403-410` (history), claims moved to `work_item` under `0002_queue_claim` |
| The worker role has no `DELETE` anywhere | `command_center/db/roles.py` `PRIVILEGES`, live-checked by `tests/db/test_grant_compliance.py::test_compliance_passes_when_grants_are_applied` |
| The decision is signed, not implicit | `tests/db/test_mirror_coverage.py:147-159` |

No code change is required to close this item: the design is deliberate, it
is the correct one given the mirror's replace-only contract, and it is
already gated on every axis the note raises. What was missing was a single
place naming all five, so the next reader does not have to re-derive it from
five files the way this document did.
