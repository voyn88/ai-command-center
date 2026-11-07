# SRV-08B — RTO drill, fourth independent reproduction

`VOYN-W0-AICC-SRV-08B-RTO-UNVERIFIED` was dispatched again despite three prior
drills (`SRV08B_RTO_DRILL_2026-08-27`, `SRV08B_RTO_DRILL_2026-09-01`, and
`SRV08B_RTO_DRILL_2026-09-06`) already having closed it on this branch. The
branch's three commits were present and the docs pointed at them; the task
being re-handed-out looks like a backlog/branch-publication artifact, not
evidence the fix regressed.

Given the task's own thesis — an unreproduced number is unverified regardless
of how confidently it's stated — reading three prior write-ups is exactly the
kind of secondhand confidence this task exists to reject. So this pass ran the
drill a fourth time, independently, rather than only re-reading the first three.

## What was run

A standalone provisioning + seeding script (not checked into the repository,
same disposition as prior drills), built directly against
`command_center.db.roles` / `command_center.db.migrations` rather than the
pytest fixtures:

- Unprivileged `initdb`/`pg_ctl` cluster on `127.0.0.1:55432` (no root, no
  Docker — `docker.sock` permission denied on this host).
- Own scratch database, own cluster role passwords, full bootstrap + all 14
  migrations + `apply_table_grants` (55 relations).
- A production-shaped dataset seeded through `aicc_app`: 50k task, 50k
  session, 50k run, 250k run_event rows (~400k rows, 91 MB live).
- `scripts/aicc_pg_backup.sh --verify` then `scripts/aicc_pg_restore.sh
  --measure-out`, run four times against one 2.3 MB backup archive. Each
  restore targeted a separate side-by-side database; row counts were verified
  in the restored DB before it was dropped.

## Result

**Measured restore time: 1–2s across 4 runs** (raw data:
`SRV08B_RTO_DRILL_2026-10-31.json`). Same order of magnitude as all three
prior drills (2026-08-27: 2s flat; 2026-09-01: 2–3s; 2026-09-06: 3s flat).
The single 1s reading is consistent with bash `SECONDS` (1-second granularity)
landing just inside a bucket boundary, not a real change in restore cost.

The backup/restore integration suite (`tests/db/test_postgres_integration.py`
plus `tests/db/test_backup_script_guards.py`, 36 tests) was also re-run against
the same harness and passed, confirming the `--measure-out` code path still
works.

## Disposition

No code or runbook change was needed beyond pointing
`docs/postgres-foundation.md` at this fourth artifact alongside the prior
three. Task remains **closed**; the restore drill is real, reproducible by
independent parties using independent provisioning code, and stays that way as
long as `--measure-out` keeps leaving an artifact behind. The unverified
"~5.4s" figure cited in the SRV-07 design remains superseded.
