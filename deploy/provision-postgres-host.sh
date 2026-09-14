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
# `postgresql.service` is a Debian meta/umbrella unit: it can report active
# while the cluster underneath it is stopped or failed, so success here is
# gated on `pg_lsclusters` showing an online cluster and, where `pg_isready`
# is available, that cluster actually accepting connections -- not merely on
# the service's activation state.
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

if ! command -v pg_lsclusters >/dev/null 2>&1; then
  echo "pg_lsclusters not found after install -- cannot confirm a cluster is online" >&2
  exit 1
fi

online_cluster="$(pg_lsclusters 2>/dev/null | tail -n +2 | awk '$4 == "online" {print; exit}')"
if [ -z "$online_cluster" ]; then
  echo "postgresql.service is active but pg_lsclusters reports no cluster online:" >&2
  pg_lsclusters >&2 || true
  exit 1
fi

cluster_port="$(printf '%s\n' "$online_cluster" | awk '{print $3}')"
if command -v pg_isready >/dev/null 2>&1; then
  if ! pg_isready -h 127.0.0.1 -p "${cluster_port:-5432}" >/dev/null 2>&1; then
    echo "cluster is online per pg_lsclusters but pg_isready cannot reach it on port ${cluster_port:-5432}" >&2
    exit 1
  fi
fi

echo "AICC_POSTGRES_HOST_PROVISIONED"
