#!/usr/bin/env bash
# Run a throwaway PostgreSQL server directly from local server binaries, for
# hosts that have neither Docker-group membership nor a container engine
# (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER). Not used in CI, which always has
# Docker; this is the fallback for a bare dev host or sandbox.
#
# `start` is idempotent and crash-safe: it keys "already initialised" off the
# data directory's own PG_VERSION marker (written by initdb only on success),
# not off the directory merely existing. A prior run that crashed leaves a
# populated data directory and no live postmaster -- start recognises that,
# skips initdb, and starts the server on the existing data, exactly as it
# would after a clean `stop`. It never requires a manual `stop` to recover.
#
# Commands:
#   scripts/aicc_pg_harness.sh start   # idempotent; prints the DSN on stdout
#   scripts/aicc_pg_harness.sh stop    # stops the server; keeps the data dir
#   scripts/aicc_pg_harness.sh status  # exit 0 + prints DSN if running
#
# State lives under AICC_PG_HARNESS_STATE (default: $TMPDIR/aicc-pg-harness),
# overridable so tests and concurrent instances don't collide.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/aicc_pg_bindir.sh
. "${SCRIPT_DIR}/lib/aicc_pg_bindir.sh"
# shellcheck source=lib/aicc_pg_net.sh
. "${SCRIPT_DIR}/lib/aicc_pg_net.sh"
# shellcheck source=lib/aicc_pg_password.sh
. "${SCRIPT_DIR}/lib/aicc_pg_password.sh"

STATE_DIR="${AICC_PG_HARNESS_STATE:-${TMPDIR:-/tmp}/aicc-pg-harness}"
DATA_DIR="${STATE_DIR}/data"
PW_FILE="${STATE_DIR}/pw"
PORT_FILE="${STATE_DIR}/port"
LOG_FILE="${STATE_DIR}/server.log"
TIMEOUT="${AICC_PG_HARNESS_TIMEOUT:-30}"

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

print_dsn() {
    local port password
    port="$(cat "${PORT_FILE}")"
    password="$(cat "${PW_FILE}")"
    printf 'postgres://postgres:%s@127.0.0.1:%s/postgres?sslmode=disable\n' "${password}" "${port}"
}

is_running() {
    local bindir="$1"
    [ -e "${DATA_DIR}/PG_VERSION" ] && "${bindir}/pg_ctl" status -D "${DATA_DIR}" >/dev/null 2>&1
}

start() {
    local bindir port
    if ! bindir="$(aicc_pg_find_bindir)"; then
        echo "aicc_pg_harness: no PostgreSQL server binaries found (initdb, pg_ctl, postgres)" >&2
        exit 1
    fi

    mkdir -p "${STATE_DIR}"

    if is_running "${bindir}"; then
        echo "aicc_pg_harness: already running" >&2
        print_dsn
        return 0
    fi

    if [ ! -e "${DATA_DIR}/PG_VERSION" ]; then
        echo "aicc_pg_harness: initialising a new data directory at ${DATA_DIR}" >&2
        mkdir -p "${DATA_DIR}"
        chmod 700 "${DATA_DIR}"
        aicc_pg_generate_password > "${PW_FILE}"
        chmod 600 "${PW_FILE}"
        "${bindir}/initdb" \
            --username=postgres \
            --pwfile="${PW_FILE}" \
            --auth-local=trust \
            --auth-host=scram-sha-256 \
            -D "${DATA_DIR}" >/dev/null
    else
        echo "aicc_pg_harness: reusing existing data directory at ${DATA_DIR}" >&2
    fi

    port="$(aicc_pg_free_port)"
    echo "${port}" > "${PORT_FILE}"

    # -s: pg_ctl's own "waiting for server to start...." chatter goes to
    # stdout by default, which would otherwise land in the DSN this function
    # prints on its last stdout line -- and any caller capturing that line
    # (provision_postgres_host.sh does) would get pg_ctl's prose instead of a
    # DSN.
    "${bindir}/pg_ctl" start \
        -D "${DATA_DIR}" \
        -s -w -t "${TIMEOUT}" \
        -l "${LOG_FILE}" \
        -o "-c listen_addresses=127.0.0.1 -c port=${port} -c unix_socket_directories=${STATE_DIR}"

    # pg_ctl -w considers the server "up" once it accepts connections on
    # *some* address it listens on; this repeats the check against the exact
    # DSN endpoint we are about to hand back, the same host-endpoint gap a
    # prior review of this feature's Docker route was rejected for leaving
    # unchecked.
    if ! aicc_pg_wait_tcp 127.0.0.1 "${port}" "${TIMEOUT}"; then
        echo "aicc_pg_harness: server started but 127.0.0.1:${port} never became reachable" >&2
        "${bindir}/pg_ctl" stop -D "${DATA_DIR}" -w -m fast >/dev/null 2>&1 || true
        exit 1
    fi

    print_dsn
}

stop() {
    local bindir
    if ! bindir="$(aicc_pg_find_bindir)"; then
        return 0
    fi
    if [ -e "${DATA_DIR}/PG_VERSION" ]; then
        "${bindir}/pg_ctl" stop -D "${DATA_DIR}" -w -m fast >/dev/null 2>&1 || true
    fi
}

status() {
    local bindir
    if ! bindir="$(aicc_pg_find_bindir)"; then
        echo "stopped (no server binaries found)"
        return 1
    fi
    if is_running "${bindir}"; then
        echo "running: $(print_dsn)"
        return 0
    fi
    echo "stopped"
    return 1
}

case "${1:-}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    -h|--help) usage 0 ;;
    *) echo "usage: $0 {start|stop|status}" >&2; usage 1 ;;
esac
