#!/bin/sh
# Install a native PostgreSQL server on a host that has neither Docker group
# access nor server binaries (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# This is the control-01 path: aicc-backlog-merge.service and
# aicc-backlog-review.service already declare `After=postgresql.service`, i.e.
# a systemd-managed native install, not a container. Granting the deploying
# principal Docker-group membership was the other option on the table, but it
# is a strictly larger privilege grant (the Docker socket is root-equivalent)
# for no capability this script does not already provide, so it is not what
# this installs. docker-compose.server.yml remains for the single-host/dev
# deployment it already documents; it is unaffected by this script.
#
# Idempotent: installing an already-installed package and enabling an
# already-enabled service are both no-ops. Safe to rerun after a partial
# failure or to confirm state on a host that may already be provisioned --
# see scripts/check_postgres_host.sh to check first without changing anything.
#
# Usage: sudo deploy/provision-postgres-host.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "provision-postgres-host.sh must run as root" >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "this installer targets Debian/Ubuntu (apt-get not found)" >&2
  exit 1
fi

# The distribution's own postgresql package, not a third-party repository:
# this host runs a local/production server independent of the pinned
# docker-compose image (see docs/operations/DATABASE_HOST_PROVISIONING.md),
# so it needs to be a recent PostgreSQL, not byte-identical to that pin.
# Adding a second apt source (e.g. PGDG) here would be an unforced increase
# in this host's trusted-package surface for no requirement this satisfies.
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql postgresql-client

systemctl enable --now postgresql

if ! systemctl is-active --quiet postgresql; then
  echo "postgresql.service did not reach active state" >&2
  exit 1
fi

echo "AICC_POSTGRES_HOST_PROVISIONED"
