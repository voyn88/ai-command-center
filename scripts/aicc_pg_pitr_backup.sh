#!/usr/bin/env bash
# Create a physical PostgreSQL base backup for point-in-time recovery.
# Usage: scripts/aicc_pg_pitr_backup.sh --out-dir /var/backups/aicc-pitr

set -euo pipefail
OUT_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out-dir) OUT_DIR="${2:?--out-dir needs a path}"; shift 2 ;;
        -h|--help) sed -n '2,3p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$OUT_DIR" ]] || { echo "--out-dir is required" >&2; exit 2; }
: "${AICC_PG_HOST:?AICC_PG_HOST is required}"
: "${AICC_PG_USER:?AICC_PG_USER is required}"
: "${AICC_PG_PASSWORD:?AICC_PG_PASSWORD is required}"
PGPORT_VALUE="${AICC_PG_PORT:-5432}"
command -v pg_basebackup >/dev/null || { echo "pg_basebackup not found in PATH" >&2; exit 127; }
command -v pg_verifybackup >/dev/null || { echo "pg_verifybackup not found in PATH" >&2; exit 127; }
if [[ ! -d "$OUT_DIR" ]]; then mkdir -p "$OUT_DIR"; chmod 700 "$OUT_DIR"; fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${OUT_DIR}/base-${STAMP}"
PARTIAL="${BACKUP}.partial"
[[ ! -e "$BACKUP" && ! -e "$PARTIAL" ]] || { echo "backup path already exists" >&2; exit 3; }
cleanup() { rm -rf -- "$PARTIAL"; }
trap cleanup EXIT
# -X stream makes the backup self-consistent even if archiving is delayed.
PGPASSWORD="$AICC_PG_PASSWORD" pg_basebackup \
    --host="$AICC_PG_HOST" --port="$PGPORT_VALUE" --username="$AICC_PG_USER" \
    --pgdata="$PARTIAL" --format=plain --wal-method=stream \
    --checkpoint=fast --progress --manifest-checksums=SHA256
pg_verifybackup "$PARTIAL"
mv "$PARTIAL" "$BACKUP"
trap - EXIT
chmod 700 "$BACKUP"
echo "PITR base backup complete: ${BACKUP}"
