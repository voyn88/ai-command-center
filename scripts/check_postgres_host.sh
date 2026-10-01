#!/usr/bin/env bash
# Decide how this host can stand up a throwaway PostgreSQL for `tests/db`
# (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# Prints exactly one route name on stdout and exits 0:
#
#   docker   The `docker` CLI can reach a daemon without sudo -- the common
#            case when the invoking user is in the `docker` group.
#   podman   Rootless `podman` works. Chosen over docker requiring a group
#            change, since it needs no group membership at all.
#   harness  Neither container engine is usable, but real PostgreSQL server
#            binaries (initdb, pg_ctl, postgres) are on this host, so
#            scripts/aicc_pg_harness.sh can run one directly.
#
# If none of the three apply, nothing is printed, an explanation goes to
# stderr, and the script exits 1. That failure is the condition this task is
# named for: no docker group, no server binaries -- and it must be reported,
# not silently downgraded to a route that does not actually work.
#
# Usage:
#   scripts/check_postgres_host.sh

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
check_postgres_host: no usable route to a PostgreSQL server on this host.

  - `docker` is either missing or not runnable without sudo (join the
    `docker` group, or start the daemon, and retry).
  - `podman` is either missing or not runnable rootless.
  - No local PostgreSQL server binaries (initdb, pg_ctl, postgres) were
    found on PATH or in the usual install locations. Install
    postgresql-server (or set AICC_PG_BINDIR to point at one) and retry.

tests/db already skips itself without a database; this only affects running
it locally.
EOF
exit 1
