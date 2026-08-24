#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s nullglob

readonly endpoint=${VOYN_FINDINGS_ENDPOINT:?VOYN_FINDINGS_ENDPOINT is required}
readonly remote_user=${VOYN_FINDINGS_USER:-voynadmin}
readonly identity=${VOYN_FINDINGS_IDENTITY:-/home/voynadmin/.ssh/voyn-findings-ed25519}
readonly known_hosts=${VOYN_FINDINGS_KNOWN_HOSTS:-/etc/voyn/findings-known-hosts}
readonly outbox=${VOYN_FINDINGS_OUTBOX:-/var/spool/voyn-worker/backlog-outbox}
readonly sent=${VOYN_FINDINGS_SENT:-/var/spool/voyn-worker/backlog-sent}
readonly ssh_bin=${SSH_BIN:-/usr/bin/ssh}
readonly flock_bin=${FLOCK_BIN:-/usr/bin/flock}
readonly mv_bin=${MV_BIN:-/usr/bin/mv}
readonly mode=${1:-run}
identity_mode=$(/usr/bin/stat -f '%Lp' "$identity" 2>/dev/null || /usr/bin/stat -c '%a' "$identity")

[[ "$endpoint" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo "invalid endpoint" >&2; exit 2; }
[[ "$remote_user" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "invalid remote user" >&2; exit 2; }
[[ "$identity" == /* && -f "$identity" && ! -L "$identity" ]] || {
  echo "refusing unpinned identity: $identity" >&2; exit 2;
}
[[ -O "$identity" && "$identity_mode" =~ ^(400|600)$ ]] || {
  echo "refusing identity ownership/mode" >&2; exit 2;
}
[[ "$known_hosts" == /* && -r "$known_hosts" && ! -L "$known_hosts" ]] || {
  echo "refusing unpinned known-hosts file: $known_hosts" >&2; exit 2;
}
[[ -d "$outbox" && -d "$sent" ]] || { echo "findings spool missing" >&2; exit 2; }
[[ "$mode" == run || "$mode" == check ]] || { echo "usage: $0 [check]" >&2; exit 2; }

if [[ "$mode" == check ]]; then
  "$ssh_bin" -F /dev/null -i "$identity" \
    -o IdentitiesOnly=yes -o BatchMode=yes \
    -o ConnectTimeout=15 -o ConnectionAttempts=1 \
    -o UserKnownHostsFile="$known_hosts" \
    -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 \
    -- "$remote_user@$endpoint" true >/dev/null
  exit 0
fi

exec 9>"$outbox/.sync.lock"
"$flock_bin" -n 9 || exit 0
for finding in "$outbox"/*.json; do
  [[ -f "$finding" && ! -L "$finding" ]] || { echo "unsafe finding: $finding" >&2; exit 2; }
  "$ssh_bin" -F /dev/null -i "$identity" \
    -o IdentitiesOnly=yes -o BatchMode=yes \
    -o UserKnownHostsFile="$known_hosts" -o StrictHostKeyChecking=yes \
    -o HostKeyAlgorithms=ssh-ed25519 \
    -o ConnectTimeout=15 -o ConnectionAttempts=1 \
    -o ServerAliveInterval=10 -o ServerAliveCountMax=2 \
    -- "$remote_user@$endpoint" <"$finding"
  "$mv_bin" -- "$finding" "$sent/"
done
