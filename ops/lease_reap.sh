#!/bin/bash
# VOYN-W0-AICC-LEASE-STUCK-EXPIRED-NO-RECLAIM: mechanical reap of expired
# voyn-lease rows, independent of any Claude session (systemd/cron only).
# --auto-takeover only succeeds against a row the authority itself confirms
# is past expiry with a dead recorded holder -- never overrides a live one.
set -euo pipefail
export PGPASSFILE=/run/voyn-aicc-worker/pgpass
export VOYN_LEASE_DSN="host=10.20.0.2 port=5432 dbname=voyn_control user=voyn_lease_client connect_timeout=5"
REPO=/home/voynadmin/aicc-preprod/repo
LOG=/home/voynadmin/aicc-preprod/lease_reap.log
cd "$REPO"

rows=$(voyn-lease list 2>>"$LOG")
now=$(date -u +%s)
echo "$rows" | jq -c '.[]' 2>/dev/null | while read -r row; do
  repo_id=$(echo "$row" | jq -r '.repository_id')
  expires=$(echo "$row" | jq -r '.expires_at')
  exp_epoch=$(date -u -d "$expires" +%s 2>/dev/null || echo 0)
  if [ "$exp_epoch" -lt "$now" ]; then
    ts=$(date -u +%FT%TZ)
    echo "$ts reaping expired lease repository_id=$repo_id expires_at=$expires" >> "$LOG"
    session="lease-reaper-$(date +%s)-$$"
    out=$(voyn-lease acquire --repository "$repo_id" --owner lease-reaper \
      --session "$session" --task LEASE-REAPER --process-start 1 \
      --host "$(hostname)" --pid $$ --auto-takeover 2>&1) || {
        echo "$ts acquire failed for $repo_id: $out" >> "$LOG"; continue; }
    voyn-lease release --repository "$repo_id" --owner lease-reaper \
      --session "$session" --task LEASE-REAPER --process-start 1 \
      --host "$(hostname)" --pid $$ >> "$LOG" 2>&1 || true
    echo "$ts reaped $repo_id" >> "$LOG"
  fi
done
