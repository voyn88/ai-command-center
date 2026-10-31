# SRV-08B — RTO drill, third independent reproduction

`VOYN-W0-AICC-SRV-08B-RTO-UNVERIFIED` was dispatched again despite two prior
drills (`SRV08B_RTO_DRILL_2026-08-27` and `SRV08B_RTO_DRILL_2026-09-01`)
already having closed it on this branch. Rather than assume that work was
lost or wrong, this pass first checked: the branch's two commits
(`ab80048a`, `862cb988`) were present, the code and docs were consistent, and
`tests/db/test_postgres_integration.py` passed in full (28/28) against a
freshly provisioned PostgreSQL instance. The task being re-handed-out looks
like a backlog/branch-publication artifact, not evidence the fix regressed.

Given the task's own thesis — an unreproduced number is unverified regardless
of how confidently it's stated — a single passing test run plus reading two
prior write-ups is exactly the kind of secondhand confidence this task exists
to reject. So this pass ran the drill a third time, independently, rather
than only re-reading the first two.

## What was run

A standalone provisioning + seeding script (not checked into the repository,
same disposition as both prior drills), built directly against
`command_center.db.roles` / `command_center.db.migrations` rather than the
pytest fixtures, so the provisioning path matches what an operator's own
tooling would call:

- Provisioned via `voyn-artifacts/VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER/pg_test_harness.sh`
  on a fresh unprivileged cluster instance (no root/Docker on this host).
- Own scratch database, own cluster role passwords, full bootstrap + all 14
  migrations + `apply_table_grants` (55 relations).
- A production-shaped dataset seeded through `aicc_app` matching the
  task/session/run/run_event shape used by the test suite's `_seed_run`
  helper: 50k task, 50k session, 50k run, 250k run_event rows (~400k rows).
- `scripts/aicc_pg_backup.sh --verify` then `scripts/aicc_pg_restore.sh
  --measure-out`, run four times against one 2.4 MB backup archive.

## Result

**Measured restore time: 3s, flat across all 4 runs** (raw data:
`SRV08B_RTO_DRILL_2026-09-06.json`). This lands in the same order of
magnitude as both prior drills (2026-08-27: 2s flat; 2026-09-01: 2-3s) despite
a fully independent provisioning path — different scratch database, different
role credentials, a from-scratch seed script, and a freshly initialized
harness cluster rather than a reused one.

The full integration suite (`tests/db/test_postgres_integration.py`, 28
tests) was also re-run against the same harness and passed, confirming the
`--measure-out` code path and the round-trip test both still work.

## Disposition

No code or runbook change was needed. `docs/postgres-foundation.md` already
points at the prior two artifacts; this entry is added alongside them as a
third data point rather than replacing either. Task remains **closed**; the
restore drill is real, reproducible by independent parties using independent
provisioning code, and stays that way as long as `--measure-out` keeps
leaving an artifact behind.
