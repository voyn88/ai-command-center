#!/usr/bin/env bash
set -euo pipefail

readonly SSH_BIN=${SSH_BIN:-/usr/bin/ssh}
readonly CURL_BIN=${CURL_BIN:-/usr/bin/curl}
readonly TARGET=${AICC_DEADMAN_TARGET:?AICC_DEADMAN_TARGET is required}
readonly IDENTITY=${AICC_DEADMAN_IDENTITY:?AICC_DEADMAN_IDENTITY is required}
readonly KNOWN_HOSTS=${AICC_DEADMAN_KNOWN_HOSTS:?AICC_DEADMAN_KNOWN_HOSTS is required}
readonly ALERT_URL=${AICC_DEADMAN_ALERT_URL:?AICC_DEADMAN_ALERT_URL is required}
readonly ALERT_TOKEN=${AICC_DEADMAN_ALERT_TOKEN:-}

ssh_target() {
  "$SSH_BIN" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS" \
    -i "$IDENTITY" "$TARGET" "$@"
}

alert() {
  local state=$1 detail=$2
  local -a headers=(-H 'Content-Type: application/json')
  [[ -z "$ALERT_TOKEN" ]] || headers+=(-H "Authorization: Bearer $ALERT_TOKEN")
  "$CURL_BIN" --fail --silent --show-error --max-time 15 \
    "${headers[@]}" --data-binary \
    "{\"component\":\"aicc-control-plane\",\"state\":\"$state\",\"detail\":\"$detail\"}" \
    "$ALERT_URL"
}

readonly PROBE='/bin/systemctl is-active --quiet aicc-control-plane-reconciler.timer aicc-control-plane-reconciler.service aicc-control-plane-notify.timer'
if ssh_target "$PROBE"; then
  exit 0
fi

# Recovery is issued from an independent host/runtime. It starts only the
# versioned wake-up units; durable DB lanes decide what work is safe to run.
if ssh_target '/bin/systemctl start aicc-control-plane-reconciler.timer aicc-control-plane-reconciler.service aicc-control-plane-notify.timer' \
   && ssh_target "$PROBE"; then
  alert recovered external_deadman_restarted_control_plane
  exit 0
fi
alert failed external_deadman_could_not_restore_control_plane
exit 1
