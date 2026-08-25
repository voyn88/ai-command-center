#!/usr/bin/env bash
# Restore an AICC server database from a pg_dump archive (VOYN-W0-AICC-SRV-01a).
#
# Restores into a database named by --target-db, which must NOT be the live
# database unless --allow-overwrite is passed. Restoring over a running system
# is the single most destructive operation in this repository, so the default
# is a side-by-side restore: that is also the shape of the drill that proves
# the backup works, and a drill that requires taking production down is a drill
# nobody runs.
#
# The checksum written by aicc_pg_backup.sh is verified before any writes, so a
# corrupted archive fails while the target database is still empty.
#
# Usage:
#   scripts/aicc_pg_restore.sh --archive /var/backups/aicc/aicc-...dump.age \
#       --target-db aicc_restore_check [--allow-overwrite] [--jobs 4]

set -euo pipefail

ARCHIVE=""
TARGET_DB=""
JOBS="1"
ALLOW_OVERWRITE=0

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --archive)         ARCHIVE="${2:?--archive needs a path}"; shift 2 ;;
        --target-db)       TARGET_DB="${2:?--target-db needs a name}"; shift 2 ;;
        --jobs)            JOBS="${2:?--jobs needs a count}"; shift 2 ;;
        --allow-overwrite) ALLOW_OVERWRITE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

[[ -n "$ARCHIVE" ]]   || { echo "--archive is required" >&2; exit 2; }
[[ -n "$TARGET_DB" ]] || { echo "--target-db is required" >&2; exit 2; }
[[ -f "$ARCHIVE" ]]   || { echo "archive not found: $ARCHIVE" >&2; exit 2; }

: "${AICC_PG_HOST:?AICC_PG_HOST is required}"
: "${AICC_PG_DB:?AICC_PG_DB is required}"
: "${AICC_PG_USER:?AICC_PG_USER is required}"
: "${AICC_PG_PASSWORD:?AICC_PG_PASSWORD is required}"
PGPORT_VALUE="${AICC_PG_PORT:-5432}"

if [[ "$TARGET_DB" == "$AICC_PG_DB" && "$ALLOW_OVERWRITE" != "1" ]]; then
    echo "refusing to restore over the live database ${AICC_PG_DB}." >&2
    echo "pass --allow-overwrite if that is genuinely what you want." >&2
    exit 3
fi

RESTORE_ARCHIVE="$ARCHIVE"
DECRYPTED=""
if [[ "$ARCHIVE" == *.age ]]; then
    : "${AICC_BACKUP_AGE_IDENTITY_FILE:?AICC_BACKUP_AGE_IDENTITY_FILE is required for encrypted archives}"
    [[ -f "$AICC_BACKUP_AGE_IDENTITY_FILE" ]] || {
        echo "age identity not found: $AICC_BACKUP_AGE_IDENTITY_FILE" >&2; exit 2;
    }
    command -v age >/dev/null || { echo "age not found in PATH" >&2; exit 127; }
fi

command -v pg_restore >/dev/null || { echo "pg_restore not found in PATH" >&2; exit 127; }
command -v psql >/dev/null       || { echo "psql not found in PATH" >&2; exit 127; }

CHECKSUM_FILE="${ARCHIVE}.sha256"
if [[ -f "$CHECKSUM_FILE" ]]; then
    echo "verifying checksum"
    # `--status` is silent on failure, so `set -e` would abort with no reason
    # printed — during a recovery that reads as a hang, not as bit rot. Name
    # the cause explicitly and keep the non-zero exit.
    if command -v sha256sum >/dev/null; then
        checksum_cmd=(sha256sum --check --status)
    else
        checksum_cmd=(shasum -a 256 --check --status)
    fi
    if ! (cd "$(dirname "$ARCHIVE")" && "${checksum_cmd[@]}" "$(basename "$CHECKSUM_FILE")"); then
        echo "checksum mismatch for ${ARCHIVE}: the archive does not match" \
             "${CHECKSUM_FILE}; refusing to restore a corrupted backup" >&2
        exit 5
    fi
    echo "checksum ok"
else
    # Loud, not fatal: archives produced before this script existed, or copied
    # without their sidecar, are still restorable — but the operator should
    # know the integrity check did not run.
    echo "WARNING: no checksum file at ${CHECKSUM_FILE}; integrity not verified" >&2
fi

if [[ "$ARCHIVE" == *.age ]]; then
    DECRYPTED="$(mktemp "${TMPDIR:-/tmp}/aicc-restore.XXXXXX.dump")"
    trap 'rm -f "$DECRYPTED"' EXIT
    chmod 600 "$DECRYPTED"
    age --decrypt --identity "$AICC_BACKUP_AGE_IDENTITY_FILE" \
        --output "$DECRYPTED" "$ARCHIVE"
    RESTORE_ARCHIVE="$DECRYPTED"
    pg_restore --list "$RESTORE_ARCHIVE" >/dev/null
fi

run_psql() {
    PGPASSWORD="$AICC_PG_PASSWORD" psql \
        --host="$AICC_PG_HOST" --port="$PGPORT_VALUE" --username="$AICC_PG_USER" \
        --dbname=postgres --quiet --no-psqlrc --set=ON_ERROR_STOP=1 "$@"
}

echo "preparing target database ${TARGET_DB}"
if [[ "$ALLOW_OVERWRITE" == "1" ]]; then
    run_psql --command="DROP DATABASE IF EXISTS \"${TARGET_DB}\" WITH (FORCE);"
fi
run_psql --command="CREATE DATABASE \"${TARGET_DB}\";"

echo "restoring ${ARCHIVE} -> ${TARGET_DB}"
PGPASSWORD="$AICC_PG_PASSWORD" pg_restore \
    --host="$AICC_PG_HOST" \
    --port="$PGPORT_VALUE" \
    --username="$AICC_PG_USER" \
    --dbname="$TARGET_DB" \
    --jobs="$JOBS" \
    --no-owner \
    --no-privileges \
    --exit-on-error \
    "$RESTORE_ARCHIVE"

# Prove the restore produced a usable schema rather than an empty database that
# pg_restore happened not to complain about.
ROWS="$(PGPASSWORD="$AICC_PG_PASSWORD" psql \
    --host="$AICC_PG_HOST" --port="$PGPORT_VALUE" --username="$AICC_PG_USER" \
    --dbname="$TARGET_DB" --tuples-only --no-align --no-psqlrc \
    --command="SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")"

echo "restore complete: ${TARGET_DB} has ${ROWS} tables in schema public"
if [[ "$ROWS" -lt 1 ]]; then
    echo "restore produced no tables — treating as failure" >&2
    exit 4
fi

# `--no-owner --no-privileges` is what makes the archive portable between
# clusters, but it also means the restored tables are owned by whoever ran this
# script and carry no grants at all. Without the two commands below, aicc_app
# and aicc_worker have no access to the restored database, and re-running them
# is the only way to put ownership back where apply_table_grants expects it.
# Printed rather than run: this script's credentials are a restore role, not
# necessarily the superuser that bootstrap requires.
cat <<NOTICE

NOTE: the restored database has no roles or grants — pg_restore was run with
--no-owner --no-privileges, so every table is owned by '${AICC_PG_USER}'.
Before serving traffic from '${TARGET_DB}', re-assert the privilege matrix:

  AICC_PG_DB=${TARGET_DB} AICC_PG_USER=<superuser> ... \\
      python -m command_center.db bootstrap
  # then, as the owner of the restored tables:
  AICC_PG_DB=${TARGET_DB} AICC_PG_USER=${AICC_PG_USER} ... \\
      python -m command_center.db upgrade

For a drill this does not matter; for a real recovery it does.
NOTICE
