# SRV-08B — RTO drill, measured

`VOYN-W0-AICC-SRV-08B-RTO-UNVERIFIED`. The SRV-07 design cited a recovery time
of "~5.4s" as measured, but no run artifact backed it and nobody had
reproduced it. Backlog reconciliation (`VOYN-W0-BACKLOG-RECONCILE-ALL`, checked
2026-08-20) flagged that: a rollback section resting on an unreproduced number
is a plan, not a procedure.

## What was run

The drill described in `docs/postgres-foundation.md` — `aicc_pg_backup.sh`
followed by `aicc_pg_restore.sh` into a side-by-side database — executed
end to end against a real PostgreSQL 16.15 server, after provisioning the
full schema (bootstrap + all 14 migrations, 55 relations) and seeding a
production-shaped dataset (50k task/session/run chains, 250k `run_event` rows,
~400k rows, 102 MB) through the `aicc_app` role.

`aicc_pg_restore.sh` gained a `--measure-out <path>` flag (this change) that
times the recovery window — target database creation through the post-restore
table count, i.e. what an operator actually waits on — and writes it to a JSON
artifact. That is the root-cause fix: the reason no artifact existed before is
that the script never produced one. It now always prints the elapsed time and
optionally records it as structured data.

## Result

**Measured restore time: 2s**, stable across 4 consecutive runs at this
dataset size with `--jobs 1`. Full data, environment, and reproduction steps
are in the paired artifact: `docs/audits/SRV08B_RTO_DRILL_2026-08-27.json`.

This is a measurement at a stated dataset size, not a universal constant — RTO
scales with data volume and the `--jobs` value passed to `aicc_pg_restore.sh`.
It replaces the unverified "~5.4s" folklore figure with a number that has a
command, a dataset description, and a checked-in JSON artifact behind it, and
it is reproducible on demand rather than trusted from memory.

## Disposition

The rollback section of the runbook (`docs/postgres-foundation.md`, "Backup
and restore") now points at this artifact and at `--measure-out` for
re-measuring against a real snapshot. Treat this task as **closed**: the drill
is real, reproduced, and self-documenting going forward.
