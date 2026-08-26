#!/usr/bin/env bash
# Take a physical base backup for WAL-archiving point-in-time recovery
# (VOYN-W0-AICC-SRV-08).
#
# This is the base half of PITR: `scripts/aicc_pg_pitr_restore.sh` replays
# archived WAL on top of whatever this script produces to reach any point in
# time after it, not just the moment the backup was taken. It does not
# replace the nightly logical dump (`aicc_pg_backup.sh`) — that stays the
# fastest path to "give me last night's data" and works even if WAL archiving
# is misconfigured or its archive is unreachable; this is the path to "give
# me 14:32:07 this afternoon."
#
# Plain format (`--format=plain`), not tar: the restore script's whole job is
# copying this directory back into a fresh PGDATA, and a plain directory
# needs no unpacking step to get there. `--wal-method=none` deliberately does
# not stream WAL alongside the backup — this deployment already archives WAL
# continuously, and shipping it twice would only make backups bigger without
# making restores more reliable; consistency at restore time comes from
# replaying the archive, which is the point of PITR.
#
# Takes a base backup, not a logical dump, so it needs a role with the
# REPLICATION attribute (or a superuser) — none of aicc_migrator/aicc_app/
# aicc_worker qualify. Use AICC_PG_SUPERUSER, same as `db bootstrap`.
#
# Usage:
#   scripts/aicc_pg_base_backup.sh --out-dir /var/backups/aicc-basebackups \
#       [--verify] [--keep 4]

set -euo pipefail

OUT_DIR=""
KEEP=""
VERIFY=0

usage() {
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out-dir) OUT_DIR="${2:?--out-dir needs a path}"; shift 2 ;;
        --keep)    KEEP="${2:?--keep needs a count}"; shift 2 ;;
        --verify)  VERIFY=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

[[ -n "$OUT_DIR" ]] || { echo "--out-dir is required" >&2; exit 2; }

# Validated before use because the retention step feeds it to `tail -n +N` via
# bash arithmetic, where a non-numeric value evaluates to 0 and would delete
# every backup — including the one just written. Same guard as
# aicc_pg_backup.sh, for the same reason.
if [[ -n "$KEEP" && ! "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
    echo "--keep must be a positive integer; got '${KEEP}'" >&2
    exit 2
fi

: "${AICC_PG_HOST:?AICC_PG_HOST is required}"
: "${AICC_PG_DB:?AICC_PG_DB is required}"
: "${AICC_PG_USER:?AICC_PG_USER is required}"
: "${AICC_PG_PASSWORD:?AICC_PG_PASSWORD is required}"
PGPORT_VALUE="${AICC_PG_PORT:-5432}"

command -v pg_basebackup >/dev/null || { echo "pg_basebackup not found in PATH" >&2; exit 127; }

# Backups routinely contain every row in the system, so a directory this
# script creates is owner-only. An existing directory is left alone: it may
# be an operator-managed shared location whose permissions are a deliberate
# choice.
if [[ ! -d "$OUT_DIR" ]]; then
    mkdir -p "$OUT_DIR"
    chmod 700 "$OUT_DIR"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${OUT_DIR}/aicc-basebackup-${AICC_PG_DB}-${STAMP}"
TMP_DIR="${BACKUP_DIR}.partial"

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

echo "base backup of ${AICC_PG_USER}@${AICC_PG_HOST}:${PGPORT_VALUE}/${AICC_PG_DB} -> ${BACKUP_DIR}"

# Written under a `.partial` name and renamed only on success (a directory
# rename is a single syscall on the same filesystem, so this is as atomic as
# the single-file `.partial` -> final rename in aicc_pg_backup.sh), so a
# crashed or out-of-disk run cannot leave a half-written directory that looks
# like a complete backup.
PGPASSWORD="$AICC_PG_PASSWORD" pg_basebackup \
    --host="$AICC_PG_HOST" \
    --port="$PGPORT_VALUE" \
    --username="$AICC_PG_USER" \
    --pgdata="$TMP_DIR" \
    --format=plain \
    --wal-method=none \
    --checkpoint=fast \
    --label="aicc-basebackup-${STAMP}" \
    --manifest-checksums=SHA256 \
    --progress

mv "$TMP_DIR" "$BACKUP_DIR"
trap - EXIT
chmod -R go-rwx "$BACKUP_DIR"

if [[ "$VERIFY" == "1" ]]; then
    echo "verifying backup manifest checksums"
    command -v pg_verifybackup >/dev/null || { echo "pg_verifybackup not found in PATH" >&2; exit 127; }
    # --no-parse-wal: this backup was taken with --wal-method=none, so its
    # pg_wal/ is empty on purpose (see the header comment above) — without
    # this flag pg_verifybackup tries to validate WAL continuity from files
    # that were never meant to be here and fails on every backup this script
    # produces.
    pg_verifybackup --no-parse-wal "$BACKUP_DIR"
fi

if [[ -n "$KEEP" ]]; then
    echo "pruning all but the newest ${KEEP} base backups"
    # shellcheck disable=SC2012 — names are generated above and contain no spaces.
    ls -1dt "${OUT_DIR}"/aicc-basebackup-"${AICC_PG_DB}"-*/ 2>/dev/null \
        | tail -n "+$((KEEP + 1))" \
        | while read -r stale; do
            rm -rf "$stale"
            echo "  removed $(basename "$stale")"
        done
fi

echo "base backup complete: ${BACKUP_DIR}"
echo "WAL archived up to and including this backup is required to restore it —"
echo "do not prune the WAL archive past the oldest base backup you still keep."
