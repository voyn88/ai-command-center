#!/usr/bin/env bash
# Provision a throwaway PostgreSQL server for `tests/db` without root or
# Docker (VOYN-W0-AICC-HOSTS-LACK-DB-AND-DOCKER).
#
# `initdb`/`postgres` ship world-executable in the `postgresql-16` package and
# need no privilege beyond write access to their own data directory, so an
# unprivileged account can run a private cluster on a high port. This is the
# supported route on a host where the operator is not in the `docker` group
# and cannot sudo, as long as the server package (not just `-client`) is
# installed -- `scripts/check_postgres_host.sh` is what decides this route
# applies before this script is invoked.
#
# Usage:
#   eval "$(scripts/aicc_pg_harness.sh start)"   # exports AICC_TEST_PG_ADMIN_DSN
#   pytest tests/db -q
#   scripts/aicc_pg_harness.sh stop
set -euo pipefail

STATE_DIR="${AICC_PG_HARNESS_STATE:-${XDG_RUNTIME_DIR:-/tmp}/aicc-pg-harness}"

# Newest first: `pg_dump` refuses to dump a server newer than the client, and
# the suite's backup/restore drill shells out to the client on PATH.
find_bindir() {
    local d
    for d in $(printf '%s\n' /usr/lib/postgresql/*/bin | sort -rV); do
        [[ -x "$d/initdb" && -x "$d/postgres" ]] && { printf '%s' "$d"; return 0; }
    done
    echo "no PostgreSQL server binaries under /usr/lib/postgresql/*/bin" >&2
    echo "install the 'postgresql-16' package (server), not just -client" >&2
    return 1
}

free_port() {
    local p
    for p in $(seq 55432 55480); do
        ss -ltn 2>/dev/null | grep -q ":$p " || { printf '%s' "$p"; return 0; }
    done
    echo "no free port in 55432-55480" >&2
    return 1
}

start() {
    if [[ -f "$STATE_DIR/dsn" ]] && pg_ctl_cmd status >/dev/null 2>&1; then
        printf 'export AICC_TEST_PG_ADMIN_DSN=%q\n' "$(cat "$STATE_DIR/dsn")"
        return 0
    fi
    local bin port pw data
    bin=$(find_bindir)
    port=$(free_port)
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    data="$STATE_DIR/data"
    pw=$(openssl rand -hex 16)
    umask 077
    printf '%s' "$pw" >"$STATE_DIR/pw"

    # SCRAM rather than trust: the suite reads a password out of the admin DSN
    # (test_backup_restore_drill_round_trips_data passes it to the backup
    # script via AICC_PG_PASSWORD), and the enrollment tests exercise real
    # SCRAM verifiers -- a trust-auth cluster fails both.
    "$bin/initdb" -D "$data" -U "$(id -un)" -E UTF8 \
        --auth-local=scram-sha-256 --auth-host=scram-sha-256 \
        --pwfile="$STATE_DIR/pw" >"$STATE_DIR/initdb.log" 2>&1

    # Unix socket inside the data dir keeps this cluster off
    # /var/run/postgresql, which an unprivileged user cannot write to anyway.
    "$bin/pg_ctl" -D "$data" -l "$STATE_DIR/server.log" -w \
        -o "-p $port -k $data -c listen_addresses=127.0.0.1" start >/dev/null

    printf '%s' "$bin" >"$STATE_DIR/bindir"
    printf 'host=127.0.0.1 port=%s user=%s password=%s dbname=postgres' \
        "$port" "$(id -un)" "$pw" >"$STATE_DIR/dsn"
    printf 'export AICC_TEST_PG_ADMIN_DSN=%q\n' "$(cat "$STATE_DIR/dsn")"
}

pg_ctl_cmd() {
    [[ -f "$STATE_DIR/bindir" ]] || return 1
    "$(cat "$STATE_DIR/bindir")/pg_ctl" -D "$STATE_DIR/data" "$@"
}

stop() {
    [[ -d "$STATE_DIR" ]] || {
        echo "no harness state at $STATE_DIR" >&2
        return 0
    }
    pg_ctl_cmd -m immediate stop >/dev/null 2>&1 || true
    rm -rf "$STATE_DIR"
    echo "harness stopped and $STATE_DIR removed" >&2
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    dsn) cat "$STATE_DIR/dsn" 2>/dev/null || {
        echo "harness not running" >&2
        exit 1
    } ;;
    *)
        echo "usage: $0 {start|stop|dsn}" >&2
        exit 2
        ;;
esac
