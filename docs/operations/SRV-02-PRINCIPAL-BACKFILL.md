# SRV-02 principal backfill: reconciliation and retirement

`0015_principal_worker_role_backfill` (VOYN-W0-AICC-SRV-02-MIGRATION) is the
EXPAND half of an expand/contract pair. It adds
`identity_backfill_worker_role()` and makes `render_worker_host_role()`
(`command_center/db/roles.py`) call it for every newly created role, so the
gap between "an `aicc_worker` PostgreSQL role exists" and "a `principal` row
exists for it" stops growing. It deliberately does not touch roles that were
created before this migration ran, and it does not remove
`render_worker_host_role()`'s bare `CREATE ROLE` path. Those are the CONTRACT
step, described below, and they are an operational change against a live
fleet, not a schema migration — running them is out of scope for any
automated deploy.

## Step 1 — reconcile existing roles

Find every `aicc_worker` member with no matching `principal` row:

```sql
SELECT r.rolname
FROM pg_auth_members m
JOIN pg_roles r ON r.oid = m.member
JOIN pg_roles g ON g.oid = m.roleid AND g.rolname = 'aicc_worker'
LEFT JOIN principal p ON p.db_role = r.rolname
WHERE p.principal_id IS NULL
ORDER BY r.rolname;
```

For every role the query returns, run:

```sql
SELECT identity_backfill_worker_role('<rolname>');
```

`identity_backfill_worker_role()` is idempotent (it returns the existing
`principal_id` unchanged if one already exists) and fails closed with
`SQLSTATE 28000` if the argument is not a current `aicc_worker` member, so it
is safe to run against the reconciliation query's output without first
diffing it against a previous run.

Re-run the reconciliation query afterwards and confirm it returns zero rows
before moving to step 2. Do this on every environment (staging, then
production) separately — the query is server-local.

## Step 2 — contract (retire the hand-provisioned path)

Only once step 1 returns zero rows on production:

1. Drain the worker fleet with `ops/aicc_staged_worker_rollout.py` (SRV-03
   stop). This must happen before the code change below lands, so no host is
   mid-connection on a role the reconciliation in step 1 has not reached —
   `render_worker_host_role()` is still the only role-creation path in use
   until the fleet is stopped, so a host that starts between the
   reconciliation query and the drain would be invisible to it.
2. With the fleet drained, remove the bare `CREATE ROLE` branch from
   `render_worker_host_role()` (`command_center/db/roles.py`) so ticket
   enrolment (`identity_enroll_worker()`) is the only way to produce a worker
   role, and drop the `to_regclass`/`to_regprocedure` guard in the same
   function that exists only to let it run against a database older than
   0015.
3. Roll the fleet back up under the ticket-enrolment path and verify every
   lane reaches `aicc-ready` before considering the contract step complete.

This is a reviewed, scheduled maintenance change coordinated with the fleet
owner, not something to automate opportunistically — it is written down here
rather than scripted because it depends on the live state of the fleet at
execution time.
