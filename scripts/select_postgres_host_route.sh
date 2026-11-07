#!/usr/bin/env bash
# Pick how this host can stand up a throwaway PostgreSQL for `tests/db`
# (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# Prints exactly one route name on stdout and exits 0:
#
#   docker   The `docker` CLI can reach a daemon without sudo.
#   podman   Rootless `podman` works.
#   harness  Neither container engine is usable, but real PostgreSQL server
#            binaries (initdb, pg_ctl, postgres) are on this host.
#
# For a read-only audit of production routes (native systemd cluster, Docker
# group membership, etc.), use scripts/check_postgres_host.sh instead.
#
# Usage: scripts/select_postgres_host_route.sh

set -euo pipefail

TIMEOUT="${AICC_PG_CHECK_TIMEOUT:-5}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/aicc_pg_bindir.sh
. "${SCRIPT_DIR}/lib/aicc_pg_bindir.sh"

docker_usable() {
    command -v docker >/dev/null 2>&1 && timeout "${TIMEOUT}" docker info >/dev/null 2>&1
}

podman_usable() {
    command -v podman >/dev/null 2>&1 && timeout "${TIMEOUT}" podman info >/dev/null 2>&1
}

harness_usable() {
    aicc_pg_find_bindir >/dev/null 2>&1
}

if docker_usable; then
    echo docker
    exit 0
fi

if podman_usable; then
    echo podman
    exit 0
fi

if harness_usable; then
    echo harness
    exit 0
fi

cat >&2 <<'EOF'
select_postgres_host_route: no usable route to a PostgreSQL server on this host.

  - `docker` is either missing or not runnable without sudo (join the
    `docker` group, or start the daemon, and retry).
  - `podman` is either missing or not runnable rootless.
  - No local PostgreSQL server binaries (initdb, pg_ctl, postgres) were
    found on PATH or in the usual install locations. Install
    postgresql-server (or set AICC_PG_BINDIR to point at one) and retry.

For fleet hosts with no container route, use deploy/provision-postgres-host.sh
(see docs/operations/DATABASE_HOST_PROVISIONING.md).
EOF
exit 1
