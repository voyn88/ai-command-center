# Database host provisioning (no Docker group, no server binaries)

Origin: `VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER`. A manual probe on
`voyn-control-01` and `voyn-worker-01` (2026-08-20) found `voynadmin` outside
the `docker` group (`permission denied` on `/var/run/docker.sock`), no
rootless Docker, no Podman, and no native `postgres`/`pg_ctl`/`initdb`
binaries — only `postgresql-client-16`. Consequence at the time: no route to
a real PostgreSQL server existed on either host, so `tests/db` could only
skip and `aicc-backlog-merge.service`/`aicc-backlog-review.service` (which
both declare `After=postgresql.service`) had nothing to start after.

Re-verified live on `voyn-worker-01` on 2026-08-26: `postgresql-16` (server)
is now installed and `postgresql.service` is active — that host already has a
working native route. The `docker` group is still empty; `voynadmin` still
cannot reach `/var/run/docker.sock`. `voyn-control-01`'s state was not
re-checked from here — self-deploy's own architecture note records that
control-01 and worker-01 cannot reach each other over SSH, so each host's
state has to be verified on that host. Run `scripts/check_postgres_host.sh`
on any host to get current, structured answers instead of another one-off
probe.

## Decision: native install, not Docker-group membership

Install a native PostgreSQL server (`deploy/provision-postgres-host.sh`) on
any host that `scripts/check_postgres_host.sh` reports has no route. Do not
add `voynadmin` to the `docker` group.

Why native, not Docker group:

- `aicc-backlog-merge.service` and `aicc-backlog-review.service` already
  declare `After=postgresql.service` — the deploy automation was written
  assuming a systemd-managed native server, not a container. Docker-group
  membership would not satisfy that dependency; a native install already
  does, as `voyn-worker-01` shows.
- The Docker group is root-equivalent (a member can bind-mount `/` into a
  container and read/write anything root can), which is a strictly larger
  grant to the deploying principal than a package install for a capability
  a native install already provides. Nothing in this project's actual
  requirement — a running PostgreSQL server reachable on localhost — needs
  that grant.
- `docker-compose.server.yml` keeps working exactly as documented in
  [`postgres-foundation.md`](../postgres-foundation.md) for the single-host
  compose deployment it names itself for; this decision does not touch it.

This host-local server is independent of the pinned `postgres:17.6-alpine`
image in `docker-compose.server.yml`. It is not standing in for the
production database — control-01's real database is reached from worker-01
over the SSH tunnel (`voyn-aicc-pgtunnel.service`), never a local install on
worker-01. A local native server on either host exists to give `tests/db`
and `python -m command_center.db upgrade` something to run against on a
host with no other route; it does not need to match the production pin, only
to be a real PostgreSQL server recent enough to run the schema.

## Runbook

1. `scripts/check_postgres_host.sh` — read-only; reports which routes (native,
   Docker, Podman) are usable on the current host. Exits non-zero only when
   none are.
2. If it reports no native server: `sudo deploy/provision-postgres-host.sh`.
   Installs the distribution's `postgresql`/`postgresql-client` packages and
   enables `postgresql.service`. Idempotent — rerunning on an
   already-provisioned host is a no-op.
3. Re-run `scripts/check_postgres_host.sh` to confirm.
4. Continue with the existing bootstrap/migrate steps in
   [`postgres-foundation.md`](../postgres-foundation.md#standing-up-a-database)
   — they take a DSN, not a deployment method, so nothing else about them
   changes.
