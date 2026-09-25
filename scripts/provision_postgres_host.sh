#!/usr/bin/env bash
# Stand up a throwaway PostgreSQL for `tests/db` on a host that may have
# neither Docker-group membership nor local server binaries
# (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# Picks a route via scripts/check_postgres_host.sh (docker, podman, or the
# scripts/aicc_pg_harness.sh local-binary fallback) and drives it. On success,
# `start` prints:
#
#   AICC_POSTGRES_HOST_PROVISIONED=1
#   AICC_POSTGRES_HOST_DSN=postgres://...
#
# `AICC_POSTGRES_HOST_PROVISIONED=1` is only ever printed after the emitted
# DSN's published host endpoint (127.0.0.1:<port>) has itself accepted a TCP
# connection -- not merely after a container-internal `pg_isready`, which
# proves nothing about whether the host can actually reach the port it was
# just told to use.
#
# Usage:
#   scripts/provision_postgres_host.sh start  [--timeout SECONDS]
#   scripts/provision_postgres_host.sh stop
#   scripts/provision_postgres_host.sh status
#
# State (which route was used, the container name, the port, the DSN) lives
# under AICC_PG_PROVISION_STATE_DIR (default: $TMPDIR/aicc-pg-provision) so a
# later `stop` in a different process can find and tear down exactly what a
# given `start` created. Point AICC_PG_PROVISION_STATE_DIR at a fresh
# directory to run more than one instance at once.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/aicc_pg_net.sh
. "${SCRIPT_DIR}/lib/aicc_pg_net.sh"
# shellcheck source=lib/aicc_pg_password.sh
. "${SCRIPT_DIR}/lib/aicc_pg_password.sh"

CHECK_SCRIPT="${SCRIPT_DIR}/check_postgres_host.sh"
HARNESS="${SCRIPT_DIR}/aicc_pg_harness.sh"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.server.yml"

STATE_DIR="${AICC_PG_PROVISION_STATE_DIR:-${TMPDIR:-/tmp}/aicc-pg-provision}"
TIMEOUT="${AICC_PG_PROVISION_TIMEOUT:-60}"

# The harness keeps its own state under whatever AICC_PG_HARNESS_STATE
# resolves to, which defaults independently of this script's own STATE_DIR.
# Pinning it here -- and passing it explicitly on every harness invocation
# below, never relying on an inherited environment variable -- keeps `start`
# and a later `stop`/`status` (run as a different process, possibly with a
# different ambient environment) pointed at the same instance instead of one
# falling back to the harness's default and silently missing it.
HARNESS_STATE_DIR="${STATE_DIR}/harness"

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        start|stop|status) COMMAND="$1"; shift ;;
        --timeout) TIMEOUT="${2:?--timeout needs a number of seconds}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

: "${COMMAND:?usage: $0 {start|stop|status} [--timeout SECONDS]}"

# Derived from STATE_DIR's own path rather than $$: a container name with an
# identity independent of the state directory that records it is exactly how
# a prior version of this script could `start` a container and then have
# `stop` -- running as a different process, with a different $$ -- fail to
# find it, orphaning it.
container_name_for_state_dir() {
    local id
    id="$(printf '%s' "$1" | cksum | cut -d' ' -f1)"
    printf 'aicc-pg-provision-%s\n' "${id}"
}

# The image is pinned in exactly one place, docker-compose.server.yml; this
# reads it rather than carrying a second hand-copied digest that can silently
# diverge (a 65-character digest -- one hex character too many -- shipped in
# an earlier version of this script that hardcoded its own copy).
resolve_pinned_image() {
    local image
    image="$(grep -m1 -oE 'postgres:[^[:space:]]+@sha256:[0-9a-f]{64}' "${COMPOSE_FILE}" || true)"
    if [ -z "${image}" ]; then
        echo "provision_postgres_host: could not find a pinned postgres image in ${COMPOSE_FILE}" >&2
        return 1
    fi
    printf '%s\n' "${image}"
}

# Retries on a lost bind race (the port was free when free_port checked it,
# but another process took it before `docker run` bound it) instead of
# letting `set -e` abort the whole provisioning run on what is, from the
# operator's perspective, a transient condition.
start_container() {
    local cli="$1" image="$2" password="$3"
    local attempt port out rc
    # attempt only bounds the retry count.
    # shellcheck disable=SC2034
    for attempt in 1 2 3 4 5; do
        port="$(aicc_pg_free_port)"
        if out="$("${cli}" run -d --name "${CONTAINER_NAME}" \
            -e POSTGRES_PASSWORD="${password}" \
            -p "127.0.0.1:${port}:5432" \
            "${image}" 2>&1)"; then
            printf '%s\n' "${port}"
            return 0
        fi
        rc=$?
        if printf '%s' "${out}" | grep -qiE 'address already in use|port is already allocated'; then
            continue
        fi
        printf '%s\n' "${out}" >&2
        return "${rc}"
    done
    echo "provision_postgres_host: could not bind a free host port after 5 attempts" >&2
    return 1
}

# Ready only once BOTH the container's own postgres answers pg_isready AND
# the published host endpoint accepts a TCP connection. The second half is
# the one a prior review found missing: a broken -p mapping or host firewall
# rule can make the first half pass while the DSN this script is about to
# hand back is unreachable from the very process that asked for it.
wait_ready() {
    local cli="$1" port="$2" timeout_s="$3"
    local elapsed=0
    while [ "${elapsed}" -lt "${timeout_s}" ]; do
        if "${cli}" exec "${CONTAINER_NAME}" pg_isready -U postgres >/dev/null 2>&1 \
            && aicc_pg_host_reachable 127.0.0.1 "${port}"; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    echo "provision_postgres_host: postgres never became reachable on 127.0.0.1:${port} within ${timeout_s}s" >&2
    return 1
}

start() {
    mkdir -p "${STATE_DIR}"
    CONTAINER_NAME="$(container_name_for_state_dir "${STATE_DIR}")"

    local route
    if ! route="$("${CHECK_SCRIPT}")"; then
        exit 1
    fi
    echo "${route}" > "${STATE_DIR}/route"

    local dsn
    case "${route}" in
        docker|podman)
            local image password port
            image="$(resolve_pinned_image)" || exit 1
            password="$(aicc_pg_generate_password)"
            echo "${CONTAINER_NAME}" > "${STATE_DIR}/container"
            if ! port="$(start_container "${route}" "${image}" "${password}")"; then
                exit 1
            fi
            if ! wait_ready "${route}" "${port}" "${TIMEOUT}"; then
                "${route}" rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
                exit 1
            fi
            dsn="postgres://postgres:${password}@127.0.0.1:${port}/postgres?sslmode=disable"
            ;;
        harness)
            dsn="$(AICC_PG_HARNESS_STATE="${HARNESS_STATE_DIR}" AICC_PG_HARNESS_TIMEOUT="${TIMEOUT}" "${HARNESS}" start)"
            ;;
        *)
            echo "provision_postgres_host: unknown route '${route}' from ${CHECK_SCRIPT}" >&2
            exit 1
            ;;
    esac

    echo "${dsn}" > "${STATE_DIR}/dsn"
    echo "AICC_POSTGRES_HOST_PROVISIONED=1"
    echo "AICC_POSTGRES_HOST_DSN=${dsn}"
}

stop() {
    if [ ! -d "${STATE_DIR}" ]; then
        return 0
    fi

    local route
    route="$(cat "${STATE_DIR}/route" 2>/dev/null || true)"
    case "${route}" in
        docker|podman)
            local cname
            cname="$(cat "${STATE_DIR}/container" 2>/dev/null || true)"
            if [ -n "${cname}" ]; then
                "${route}" rm -f "${cname}" >/dev/null 2>&1 || true
            fi
            ;;
        harness)
            AICC_PG_HARNESS_STATE="${HARNESS_STATE_DIR}" "${HARNESS}" stop
            ;;
    esac

    rm -rf "${STATE_DIR}"
}

status() {
    if [ ! -d "${STATE_DIR}" ]; then
        echo "stopped (not provisioned)"
        return 1
    fi

    local route
    route="$(cat "${STATE_DIR}/route" 2>/dev/null || true)"
    case "${route}" in
        docker|podman)
            local cname
            cname="$(cat "${STATE_DIR}/container" 2>/dev/null || true)"
            if [ -n "${cname}" ] && "${route}" ps -q --filter "name=^${cname}\$" 2>/dev/null | grep -q .; then
                echo "running (${route}): $(cat "${STATE_DIR}/dsn" 2>/dev/null)"
                return 0
            fi
            echo "stopped"
            return 1
            ;;
        harness)
            AICC_PG_HARNESS_STATE="${HARNESS_STATE_DIR}" "${HARNESS}" status
            ;;
        *)
            echo "stopped (unknown route)"
            return 1
            ;;
    esac
}

case "${COMMAND}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
esac
