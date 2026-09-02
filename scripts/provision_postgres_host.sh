#!/usr/bin/env bash
# Provision a throwaway PostgreSQL server on whichever route
# `scripts/check_postgres_host.sh` reports usable, for `tests/db`
# (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# A prior version of this script printed AICC_POSTGRES_HOST_PROVISIONED as
# soon as its container-launch or process-start command exited zero -- proof
# only that `docker run` or `pg_ctl start` was accepted, not that anything is
# listening yet (review on
# https://github.com/voyn88/ai-command-center/pull/435). A later version fixed
# that but still declared the container ready from `docker exec ... pg_isready`
# alone, which probes the daemon over its in-container Unix socket and so
# stays blind to the one thing that route's own DSN actually depends on: the
# published `127.0.0.1:$port` forward (review on
# https://github.com/voyn88/ai-command-center/pull/568). Every route below
# waits on the daemon's own readiness probe *and*, for the container routes,
# a real TCP connection to the exact host:port the printed DSN points at,
# before declaring success -- and prints nothing at all if either probe never
# succeeds.
#
# `native` is deliberately not provisioned here: it names a cluster the
# operator already runs, and this script has no credentials for it. Run
# `scripts/check_postgres_host.sh` for that route's diagnostics and export
# AICC_TEST_PG_ADMIN_DSN by hand.
#
# Usage: eval "$(scripts/provision_postgres_host.sh start)"
#        scripts/provision_postgres_host.sh stop
set -euo pipefail

REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
HARNESS="$REPO_ROOT/scripts/aicc_pg_harness.sh"
STATE_DIR="${AICC_PG_PROVISION_STATE:-${XDG_RUNTIME_DIR:-/tmp}/aicc-pg-provision}"
CONTAINER_NAME="aicc-pg-provision-$$"
IMAGE="postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
READY_TIMEOUT_S="${AICC_PG_PROVISION_TIMEOUT:-30}"

route() {
    "$REPO_ROOT/scripts/check_postgres_host.sh" 2>/dev/null | sed -n 's/^AICC_POSTGRES_HOST_ROUTE=//p'
}

free_port() {
    local p
    for p in $(seq 55532 55580); do
        ss -ltn 2>/dev/null | grep -q ":$p " || {
            printf '%s' "$p"
            return 0
        }
    done
    echo "no free port in 55532-55580" >&2
    return 1
}

# A bare TCP connect, not a full libpq handshake -- deliberately so, since a
# route with no Docker/Podman client on the host still must not require a
# `pg_isready` (or any PostgreSQL) client on the host either. It only needs
# to run after the in-container probe below already reports the daemon
# itself is accepting connections, so "the port accepts a connection" is
# sufficient evidence the forward actually reaches it.
host_port_reachable() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# Waits for the daemon's own readiness probe, then for the published DSN
# endpoint to actually be reachable from outside the container -- `docker
# exec` always succeeds against the daemon's in-container socket even when
# the `-p 127.0.0.1:$port:5432` forward is broken, which is exactly the gap
# a broken port mapping or host firewall rule hides behind (PR #568 review).
wait_ready() {
    local engine="$1" container="$2" port="$3" tries=0 max=$((READY_TIMEOUT_S * 2))
    while ((tries < max)); do
        if "$engine" exec "$container" pg_isready -U postgres >/dev/null 2>&1 \
            && host_port_reachable "$port"; then
            return 0
        fi
        sleep 0.5
        tries=$((tries + 1))
    done
    return 1
}

start_container() {
    local engine="$1" port pw
    port=$(free_port)
    pw=$(openssl rand -hex 16)
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    "$engine" run -d --name "$CONTAINER_NAME" \
        -e POSTGRES_PASSWORD="$pw" \
        -p "127.0.0.1:$port:5432" \
        "$IMAGE" >/dev/null

    if ! wait_ready "$engine" "$CONTAINER_NAME" "$port"; then
        "$engine" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        echo "$engine container never became ready within ${READY_TIMEOUT_S}s" >&2
        return 1
    fi

    printf '%s\n%s\n' "$engine" "$CONTAINER_NAME" >"$STATE_DIR/container"
    printf 'host=127.0.0.1 port=%s user=postgres password=%s dbname=postgres' \
        "$port" "$pw" >"$STATE_DIR/dsn"
    printf 'export AICC_TEST_PG_ADMIN_DSN=%q\n' "$(cat "$STATE_DIR/dsn")"
    echo "AICC_POSTGRES_HOST_PROVISIONED=1"
}

start() {
    local chosen="${1:-$(route)}"
    case "$chosen" in
        docker) start_container docker ;;
        podman) start_container podman ;;
        harness)
            "$HARNESS" start
            echo "AICC_POSTGRES_HOST_PROVISIONED=1"
            ;;
        native)
            echo "native route is an operator-managed cluster; export AICC_TEST_PG_ADMIN_DSN yourself" >&2
            return 1
            ;;
        *)
            echo "no usable route to a PostgreSQL server on this host" >&2
            return 1
            ;;
    esac
}

stop() {
    if [[ -f "$STATE_DIR/container" ]]; then
        local engine container
        read -r engine <"$STATE_DIR/container"
        container=$(sed -n '2p' "$STATE_DIR/container")
        "$engine" rm -f "$container" >/dev/null 2>&1 || true
        rm -rf "$STATE_DIR"
    fi
    "$HARNESS" stop
}

case "${1:-start}" in
    start) start "${2:-}" ;;
    stop) stop ;;
    *)
        echo "usage: $0 {start|stop}" >&2
        exit 2
        ;;
esac
