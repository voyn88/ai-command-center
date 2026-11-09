#!/usr/bin/env bash
# Prepare, but never start, a PostgreSQL PITR data directory.
# Usage: scripts/aicc_pg_pitr_prepare.sh --base DIR --wal-dir DIR \
#   --target-dir DIR --target-time 2026-11-04T14:30:00Z

set -euo pipefail
BASE=""; WAL_DIR=""; TARGET_DIR=""; TARGET_TIME=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE="${2:?--base needs a path}"; shift 2 ;;
        --wal-dir) WAL_DIR="${2:?--wal-dir needs a path}"; shift 2 ;;
        --target-dir) TARGET_DIR="${2:?--target-dir needs a path}"; shift 2 ;;
        --target-time) TARGET_TIME="${2:?--target-time needs a timestamp}"; shift 2 ;;
        -h|--help) sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[[ -d "$BASE" ]] || { echo "base backup directory not found: $BASE" >&2; exit 2; }
[[ -f "$BASE/backup_manifest" ]] || { echo "base backup has no backup_manifest" >&2; exit 2; }
[[ -d "$WAL_DIR" ]] || { echo "WAL archive directory not found: $WAL_DIR" >&2; exit 2; }
[[ -n "$TARGET_DIR" ]] || { echo "--target-dir is required" >&2; exit 2; }
[[ "$WAL_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || {
    echo "--wal-dir must be an absolute path containing only letters, digits, _, ., /, or -" >&2; exit 2;
}
[[ "$TARGET_TIME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || {
    echo "--target-time must be UTC RFC3339 (YYYY-MM-DDTHH:MM:SSZ)" >&2; exit 2;
}
if [[ -e "$TARGET_DIR" ]]; then
    [[ -d "$TARGET_DIR" && -z "$(find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
        echo "target directory must not exist or must be empty: $TARGET_DIR" >&2; exit 3;
    }
else
    mkdir -p "$TARGET_DIR"
fi
BASE_REAL="$(realpath "$BASE")"
TARGET_REAL="$(realpath "$TARGET_DIR")"
[[ "$TARGET_REAL" != "$BASE_REAL" && "$TARGET_REAL" != "$BASE_REAL/"* ]] || {
    echo "target directory must not be the base backup or inside it" >&2; exit 3;
}
command -v pg_verifybackup >/dev/null || { echo "pg_verifybackup not found in PATH" >&2; exit 127; }
pg_verifybackup "$BASE"
cp -a "$BASE/." "$TARGET_DIR/"
rm -f -- "$TARGET_DIR/postmaster.pid" "$TARGET_DIR/postmaster.opts"
touch "$TARGET_DIR/recovery.signal"
chmod 700 "$TARGET_DIR"
{
    printf "\n# Added by aicc_pg_pitr_prepare.sh\n"
    printf "restore_command = 'cp \"%s/%%f\" \"%%p\"'\n" "$WAL_DIR"
    printf "recovery_target_time = '%s'\n" "$TARGET_TIME"
    printf "recovery_target_action = 'pause'\n"
} >> "$TARGET_DIR/postgresql.auto.conf"
echo "PITR directory prepared at ${TARGET_DIR}; recovery will pause at ${TARGET_TIME}."
echo "Inspect it, then start PostgreSQL explicitly. The live data directory was not touched."
