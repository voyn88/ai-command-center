# AICC observability, backup and recovery runbook

Prometheus scrapes control `/metrics` and every worker on port `9108`. Import
`deploy/observability/grafana-dashboard.json` and load
`deploy/observability/aicc-alerts.yml`. The dashboard identifies the control,
worker, task, source SHA, attempt/lease age, queue lag, task compute time,
process CPU and peak resident memory. Payloads
and claim capabilities are intentionally excluded from metric labels. Service
logs go to journald; use the task and attempt labels to correlate them with the
queue audit trail. Each attempt emits `trace_start` and `trace_end` structured
log records with a deterministic `trace_id`, attempt, task, SHA and outcome, so
an operator can follow one execution without logging its payload or claim
capability:

```bash
journalctl -u aicc-worker -o cat | grep 'trace_id=<id>'
```

The packaged systemd unit listens on all interfaces so Prometheus can reach it;
firewall port 9108 to the monitoring/private network only. Standalone workers
default to loopback unless `AICC_WORKER_METRICS_HOST` is explicitly set.
Set `AICC_WORKER_COST_PER_HOUR` to the blended worker-hour price (using one
fleet-wide currency) to populate estimated compute cost; its default is zero.

## Worker loss

`AICCWorkerLost` pages after two minutes of a failed worker scrape. Confirm
`up{job="aicc-worker"} == 0`, then inspect `journalctl -u aicc-worker`. Do not
manually complete its attempt. The visibility lease expires and the queue
reaper safely redelivers it; verify lease age and queue lag return to normal.

## Encrypted backup

Set `AICC_BACKUP_AGE_RECIPIENT` to the offline recovery public key and run:

```bash
scripts/aicc_pg_backup.sh --out-dir /var/backups/aicc --verify --keep 14
```

Copy the encrypted archive and checksum off-host. Never store the age identity
on the database host.

Restore verifies the checksum against the encrypted archive and decrypts to an
owner-only temporary file that is removed on exit, so a corrupted or truncated
archive is refused (exit 5, `checksum mismatch`) before the target database is
created and no plaintext dump outlives the run.

## Clean-control recovery drill

Provision a clean PostgreSQL/control host, install the same application SHA,
set `AICC_BACKUP_AGE_IDENTITY_FILE` to the mounted offline identity, and run:

```bash
scripts/aicc_pg_restore.sh --archive /recovery/aicc-….dump.age --target-db aicc_recovery
AICC_PG_DB=aicc_recovery AICC_PG_USER=<superuser> python -m command_center.db bootstrap
AICC_PG_DB=aicc_recovery AICC_PG_USER=<owner> python -m command_center.db upgrade
```

Start control against `aicc_recovery`; require `/readyz` 200, `/metrics`
`aicc_metrics_scrape_error 0`, matching counts for `work_item`, `task`, and
`run`, and one read-only queue API request. Record archive checksum, application
SHA, timestamps, counts and probe output in the incident/drill ticket. Destroy
the drill database and unmount the identity after evidence is retained.
