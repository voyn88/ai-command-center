#!/usr/bin/env bash
# Decide which route this host can use to reach a real PostgreSQL server for
# `tests/db` (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# A prior version of this script declared Docker usable whenever the operator
# was in the `docker` group and Podman usable whenever its binary was on
# PATH. Neither proves a daemon is actually reachable -- group membership
# without a running daemon, a rootless-docker misconfiguration, or a socket
# permission mismatch all satisfy those checks and then fail the moment
# something tries to use the route (review on
# https://github.com/voyn88/ai-command-center/pull/435). The same version
# also accepted `systemctl is-active postgresql` as proof of a serving
# cluster; on Debian that unit is an umbrella the package manager marks
# active even when every real cluster under it is stopped or failed.
#
# Every route below is proved by an operation that can only succeed against
# something actually listening -- never by the presence of a binary or a
# group membership.
#
# Usage: scripts/check_postgres_host.sh
# On success, prints exactly one line to stdout and exits 0:
#   AICC_POSTGRES_HOST_ROUTE=docker
#   AICC_POSTGRES_HOST_ROUTE=podman
#   AICC_POSTGRES_HOST_ROUTE=native
#   AICC_POSTGRES_HOST_ROUTE=harness
# Otherwise prints nothing to stdout, prints a remediation to stderr, and
# exits 1.
set -euo pipefail
shopt -s nullglob

docker_usable() {
    command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

podman_usable() {
    command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1
}

# A cluster `pg_lsclusters` marks online, confirmed by an actual connection
# attempt against the port it names. `pg_isready` is what proves "online"
# means "accepting connections" rather than "the postmaster process forked".
native_usable() {
    command -v pg_lsclusters >/dev/null 2>&1 || return 1
    command -v pg_isready >/dev/null 2>&1 || return 1
    local line port
    line=$(pg_lsclusters --no-header 2>/dev/null | awk '$4 == "online" {print; exit}') || true
    [[ -n "$line" ]] || return 1
    port=$(awk '{print $3}' <<<"$line")
    [[ "$port" =~ ^[0-9]+$ ]] || return 1
    pg_isready -h 127.0.0.1 -p "$port" >/dev/null 2>&1
}

# `initdb`/`postgres` ship world-executable in the server package and need no
# privilege beyond a writable data directory -- but nothing is running yet for
# this route to probe, unlike the three above. `scripts/provision_postgres_host.sh`
# is what proves it, immediately before it declares the harness provisioned.
harness_usable() {
    local dir
    for dir in /usr/lib/postgresql/*/bin; do
        [[ -x "$dir/initdb" && -x "$dir/postgres" ]] && return 0
    done
    return 1
}

if docker_usable; then
    echo "AICC_POSTGRES_HOST_ROUTE=docker"
elif podman_usable; then
    echo "AICC_POSTGRES_HOST_ROUTE=podman"
elif native_usable; then
    echo "AICC_POSTGRES_HOST_ROUTE=native"
elif harness_usable; then
    echo "AICC_POSTGRES_HOST_ROUTE=harness"
else
    {
        echo "no usable route to a PostgreSQL server on this host:"
        echo "  - docker: not installed, or the daemon is not reachable"
        echo "  - podman: not installed, or not functional"
        echo "  - native: no cluster online (checked via pg_lsclusters + pg_isready)"
        echo "  - harness: no PostgreSQL server binaries under /usr/lib/postgresql/*/bin"
        echo "an operator must join the docker group or install the postgresql-16 package"
    } >&2
    exit 1
fi
