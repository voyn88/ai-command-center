#!/usr/bin/env bash
# Point-in-time recovery: replay archived WAL on top of a physical base
# backup, up to a chosen moment (VOYN-W0-AICC-SRV-08).
#
# Restores into a fresh --target-dir and starts a standalone instance there —
# never the live database, and never in place of it. That is also the shape
# of the drill that proves the archive actually works: a recovery procedure
# that requires taking production down to rehearse is one nobody rehearses.
#
# Mechanics: copy the base backup, drop a `recovery.signal` file (PostgreSQL's
# one-shot "replay archived WAL, then stop" mode — distinct from
# `standby.signal`, which never stops), and point `restore_command` at the WAL
# archive. `recovery_target_action=promote` means the instance opens for
# read-write on its own once it reaches --target-time (or the end of the
# archive, if no target was given) instead of sitting paused waiting for an
# operator to say "go". `archive_mode=off` on the recovered copy so it never
# writes its own new timeline's WAL back into the *same* archive the live
# primary still uses — a drill must not be able to touch the archive it just
# read from. `hot_standby=off` so `pg_ctl start -w` blocks until recovery
# actually finishes rather than returning early because read-only connections
# became possible mid-replay.
#
# Usage:
#   scripts/aicc_pg_pitr_restore.sh --base-backup /var/backups/aicc-basebackups/aicc-basebackup-aicc-20260826T020000Z \
#       --wal-archive /var/lib/postgresql/wal-archive \
#       --target-dir /var/tmp/aicc-pitr-drill \
#       --port 55432 \
#       [--target-time "2026-08-26T14:32:07Z"] [--username postgres]
#       [--allow-overwrite] [--stop]
#
# --username (default "postgres") is only used to poll the recovered instance
# for readiness — a base backup is a byte copy of the whole cluster, so every
# role that existed on the primary exists here too, unlike AICC_PG_USER
# elsewhere in this repo which names one of the three product roles.

set -euo pipefail

BASE_BACKUP=""
WAL_ARCHIVE=""
TARGET_DIR=""
PORT=""
TARGET_TIME=""
PROBE_USER="postgres"
ALLOW_OVERWRITE=0
STOP_AFTER=0
START_TIMEOUT="120"

usage() {
    sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-backup)     BASE_BACKUP="${2:?--base-backup needs a path}"; shift 2 ;;
        --wal-archive)     WAL_ARCHIVE="${2:?--wal-archive needs a path}"; shift 2 ;;
        --target-dir)      TARGET_DIR="${2:?--target-dir needs a path}"; shift 2 ;;
        --port)            PORT="${2:?--port needs a number}"; shift 2 ;;
        --target-time)     TARGET_TIME="${2:?--target-time needs a timestamp}"; shift 2 ;;
        --username)        PROBE_USER="${2:?--username needs a name}"; shift 2 ;;
        --allow-overwrite) ALLOW_OVERWRITE=1; shift ;;
        --stop)            STOP_AFTER=1; shift ;;
        --start-timeout)   START_TIMEOUT="${2:?--start-timeout needs a number of seconds}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

[[ -n "$BASE_BACKUP" ]] || { echo "--base-backup is required" >&2; exit 2; }
[[ -n "$WAL_ARCHIVE" ]] || { echo "--wal-archive is required" >&2; exit 2; }
[[ -n "$TARGET_DIR" ]]  || { echo "--target-dir is required" >&2; exit 2; }
[[ -n "$PORT" ]]        || { echo "--port is required" >&2; exit 2; }
if [[ ! "$PORT" =~ ^[1-9][0-9]*$ ]]; then
    echo "--port must be a positive integer; got '${PORT}'" >&2
    exit 2
fi

for cmd in pg_ctl psql; do
    command -v "$cmd" >/dev/null || { echo "$cmd not found in PATH" >&2; exit 127; }
done

# A directory this script hasn't produced is not provably a base backup —
# proceeding anyway would copy arbitrary files into what becomes a live
# PostgreSQL data directory. `backup_manifest` in particular is only ever
# written by `pg_basebackup`.
[[ -f "${BASE_BACKUP}/PG_VERSION" ]]      || { echo "not a base backup (no PG_VERSION): ${BASE_BACKUP}" >&2; exit 2; }
[[ -f "${BASE_BACKUP}/backup_label" ]]    || { echo "not a base backup (no backup_label): ${BASE_BACKUP}" >&2; exit 2; }
[[ -f "${BASE_BACKUP}/backup_manifest" ]] || { echo "not a base backup (no backup_manifest): ${BASE_BACKUP}" >&2; exit 2; }

[[ -d "$WAL_ARCHIVE" ]] || { echo "WAL archive directory not found: ${WAL_ARCHIVE}" >&2; exit 2; }
if [[ -z "$(ls -A "$WAL_ARCHIVE" 2>/dev/null)" ]]; then
    echo "WAL archive is empty: ${WAL_ARCHIVE} — nothing to replay" >&2
    exit 2
fi

# `pg_ctl start` execs the `postgres` binary from its own installation to run
# against $TARGET_DIR. That binary refuses a data directory initialized by a
# different major version outright ("database files are incompatible") — a
# clearer, earlier error is better than that one three log lines in.
BACKUP_MAJOR="$(cat "${BASE_BACKUP}/PG_VERSION")"
RESTORE_MAJOR="$(postgres --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)"
if [[ -n "$RESTORE_MAJOR" && "$RESTORE_MAJOR" != "$BACKUP_MAJOR" ]]; then
    echo "PostgreSQL major version mismatch: backup is PG_VERSION ${BACKUP_MAJOR}, this host's postgres is ${RESTORE_MAJOR}" >&2
    exit 2
fi

if [[ -d "$TARGET_DIR" && -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]]; then
    if [[ "$ALLOW_OVERWRITE" != "1" ]]; then
        echo "refusing to restore into a non-empty directory: ${TARGET_DIR}" >&2
        echo "pass --allow-overwrite if that is genuinely what you want." >&2
        exit 3
    fi
    rm -rf "${TARGET_DIR:?}"/*
fi

WAL_ARCHIVE_ABS="$(cd "$WAL_ARCHIVE" && pwd)"

echo "restoring base backup ${BASE_BACKUP} -> ${TARGET_DIR}"
mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"
cp -a "${BASE_BACKUP}/." "$TARGET_DIR/"

touch "${TARGET_DIR}/recovery.signal"
{
    printf "restore_command = 'cp \"%s/%%f\" \"%%p\"'\n" "$WAL_ARCHIVE_ABS"
    if [[ -n "$TARGET_TIME" ]]; then
        printf "recovery_target_time = '%s'\n" "$TARGET_TIME"
    fi
    echo "recovery_target_action = 'promote'"
    echo "recovery_target_timeline = 'latest'"
    echo "archive_mode = off"
    echo "hot_standby = off"
    printf "port = %s\n" "$PORT"
    echo "listen_addresses = 'localhost'"
    printf "unix_socket_directories = '%s'\n" "$TARGET_DIR"
} >> "${TARGET_DIR}/postgresql.auto.conf"

if [[ -n "$TARGET_TIME" ]]; then
    echo "recovering to ${TARGET_TIME}"
else
    echo "recovering to the end of the available WAL archive (latest)"
fi

LOGFILE="${TARGET_DIR}/aicc-pitr-recovery.log"
pg_ctl -D "$TARGET_DIR" -l "$LOGFILE" -w -t "$START_TIMEOUT" start

# `pg_ctl -w` only proves the postmaster answered *something* — with
# hot_standby=off it answers every connection attempt with a "not accepting
# connections" FATAL for as long as archive recovery is still running, and
# `pg_ctl` counts that response itself as "started". Whether recovery has
# actually reached --target-time (or promoted, with no target) is a separate
# question this loop answers by polling until the FATAL stops or the timeout
# this script was given elapses.
IN_RECOVERY=""
DEADLINE=$((SECONDS + START_TIMEOUT))
while (( SECONDS < DEADLINE )); do
    if IN_RECOVERY="$(psql -h "$TARGET_DIR" -p "$PORT" -U "$PROBE_USER" -d postgres --tuples-only --no-align --no-psqlrc \
        --command="SELECT pg_is_in_recovery();" 2>/dev/null)"; then
        break
    fi
    IN_RECOVERY=""
    sleep 0.5
done
if [[ "$IN_RECOVERY" != "f" ]]; then
    echo "recovery did not complete within ${START_TIMEOUT}s — instance is still in recovery. See ${LOGFILE}" >&2
    exit 4
fi

echo "recovery complete and promoted: ${TARGET_DIR} is running on port ${PORT}"
echo "connect with: psql -h ${TARGET_DIR} -p ${PORT} -U ${PROBE_USER} -d postgres"
echo "stop with:    pg_ctl -D ${TARGET_DIR} stop"

if [[ "$STOP_AFTER" == "1" ]]; then
    pg_ctl -D "$TARGET_DIR" -w stop
    echo "stopped (--stop was given)"
fi
