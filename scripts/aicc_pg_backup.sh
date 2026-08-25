#!/usr/bin/env bash
# Back up the AICC server database (VOYN-W0-AICC-SRV-01a).
#
# Produces an age-encrypted pg_dump custom-format archive plus a SHA-256
# checksum file. Custom
# format rather than plain SQL because it restores selectively and in parallel,
# and because pg_restore validates the archive's own structure — a truncated
# plain-SQL dump looks like a valid, shorter database.
#
# The checksum is written next to the archive so `aicc_pg_restore.sh` can refuse
# a corrupted file *before* it starts writing into a database. A backup that is
# never verified is not a backup; `--verify` checks the dump before encryption,
# while restore verifies the encrypted checksum and decrypts it before writes.
#
# Credentials come from the environment (AICC_PG_*) and are handed to libpq via
# PGPASSWORD, which is never echoed. Nothing here writes a password to disk.
#
# Usage:
#   scripts/aicc_pg_backup.sh --out-dir /var/backups/aicc [--verify] [--keep 14]

set -euo pipefail
# The transient plaintext dump is just as sensitive as the final archive.
# Apply owner-only permissions at creation time, before pg_dump writes a byte.
umask 077

OUT_DIR=""
KEEP=""
VERIFY=0

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
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
# every archive — including the one just written.
if [[ -n "$KEEP" && ! "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
    echo "--keep must be a positive integer; got '${KEEP}'" >&2
    exit 2
fi

: "${AICC_PG_HOST:?AICC_PG_HOST is required}"
: "${AICC_PG_DB:?AICC_PG_DB is required}"
: "${AICC_PG_USER:?AICC_PG_USER is required}"
: "${AICC_PG_PASSWORD:?AICC_PG_PASSWORD is required}"
PGPORT_VALUE="${AICC_PG_PORT:-5432}"

command -v pg_dump >/dev/null || { echo "pg_dump not found in PATH" >&2; exit 127; }
if [[ "$VERIFY" == "1" ]]; then
    command -v pg_restore >/dev/null || { echo "pg_restore not found in PATH" >&2; exit 127; }
fi
: "${AICC_BACKUP_AGE_RECIPIENT:?AICC_BACKUP_AGE_RECIPIENT is required (backups must be encrypted)}"
command -v age >/dev/null || { echo "age not found in PATH" >&2; exit 127; }

# Backups routinely contain every row in the system, so a directory this script
# creates is owner-only. An existing directory is left alone: it may be an
# operator-managed shared location whose permissions are a deliberate choice.
if [[ ! -d "$OUT_DIR" ]]; then
    mkdir -p "$OUT_DIR"
    chmod 700 "$OUT_DIR"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${OUT_DIR}/aicc-${AICC_PG_DB}-${STAMP}.dump.age"
TMP_DUMP="${OUT_DIR}/.aicc-${STAMP}.dump.partial"
TMP_ARCHIVE="${ARCHIVE}.partial"

cleanup() { rm -f "$TMP_DUMP" "$TMP_ARCHIVE"; }
trap cleanup EXIT

echo "backing up ${AICC_PG_USER}@${AICC_PG_HOST}:${PGPORT_VALUE}/${AICC_PG_DB} -> ${ARCHIVE}"

# Both plaintext and encrypted output use .partial names. The plaintext is
# deleted by the EXIT trap, and the encrypted file receives its final name only
# on success, so a crash cannot leave a half-written file looking valid.
PGPASSWORD="$AICC_PG_PASSWORD" pg_dump \
    --host="$AICC_PG_HOST" \
    --port="$PGPORT_VALUE" \
    --username="$AICC_PG_USER" \
    --dbname="$AICC_PG_DB" \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges \
    --file="$TMP_DUMP"

if [[ "$VERIFY" == "1" ]]; then
    echo "verifying archive is readable before encryption"
    pg_restore --list "$TMP_DUMP" > /dev/null
fi

age --recipient "$AICC_BACKUP_AGE_RECIPIENT" --output "$TMP_ARCHIVE" "$TMP_DUMP"
rm -f "$TMP_DUMP"
mv "$TMP_ARCHIVE" "$ARCHIVE"
trap - EXIT
chmod 600 "$ARCHIVE"

if command -v sha256sum >/dev/null; then
    (cd "$OUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
else
    (cd "$OUT_DIR" && shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
fi
chmod 600 "${ARCHIVE}.sha256"

if [[ -n "$KEEP" ]]; then
    echo "pruning all but the newest ${KEEP} archives"
    # shellcheck disable=SC2012 — names are generated above and contain no spaces.
    ls -1t "${OUT_DIR}"/aicc-"${AICC_PG_DB}"-*.dump.age 2>/dev/null \
        | tail -n "+$((KEEP + 1))" \
        | while read -r stale; do
            rm -f "$stale" "${stale}.sha256"
            echo "  removed $(basename "$stale")"
        done
fi

echo "backup complete: ${ARCHIVE}"
echo "checksum:        ${ARCHIVE}.sha256"
