# SRV-08B — RTO drill, independently reproduced

`VOYN-W0-AICC-SRV-08B-RTO-UNVERIFIED` was reopened after `SRV08B_RTO_DRILL_2026-08-27`
already closed it, so before touching anything this pass treated that prior
drill itself as the thing to verify — the task exists precisely because a
number ("~5.4s") had once been asserted as measured with nothing behind it,
and a second unreproduced number would be the same failure mode with better
prose.

## What was run

The same drill described in `docs/postgres-foundation.md` and recorded in
`SRV08B_RTO_DRILL_2026-08-27.*`, executed again from a cold start by a
different agent, against a **different** PostgreSQL instance than the first
drill used:

- Provisioned with `voyn-artifacts/VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER/pg_test_harness.sh`
  — an unprivileged `initdb`/`pg_ctl` cluster on a high port, needed because
  this host has no root/sudo access and `voynadmin` is not in the `docker`
  group (confirmed directly: `sudo -u postgres psql` and `docker ps` both
  refused before the harness was found).
- Same schema (bootstrap + all 14 migrations, 55 relations), a freshly seeded
  dataset of the same declared shape (50k task / 50k session / 50k run / 250k
  run_event, ~400k rows), bulk-loaded via `generate_series` rather than the
  first drill's unspecified seeding method.
- `scripts/aicc_pg_backup.sh --verify` then `scripts/aicc_pg_restore.sh
  --measure-out`, run four times against one backup archive.

## Result

**Measured restore time: 2-3s** across 4 runs (raw data:
`SRV08B_RTO_DRILL_2026-09-01.json`), versus the first drill's flat 2s. Same
order of magnitude, on different hardware, a different dataset, and a
different operator — the small spread is consistent with `SECONDS` (bash's
1-second-resolution timer, which `aicc_pg_restore.sh` uses) landing on either
side of a bucket boundary, not a real regression.

This is the check the task asked for: not a repeat of the same run, but a
second, independent one landing in the same range. `--measure-out`'s existing
integration-test coverage (`test_backup_restore_drill_round_trips_data` in
`tests/db/test_postgres_integration.py`) was also re-run here and passed,
confirming the artifact-writing code itself works, not just that a human
transcribed numbers plausibly.

## Disposition

No code or runbook change was needed — `SRV08B_RTO_DRILL_2026-08-27`'s fix
(the `--measure-out` flag and the doc pointer) was already correct and already
reproducible; this pass adds a second, independently-run data point rather
than replacing the first. Both artifacts stay checked in side by side as the
drill's history. Task remains **closed**.
