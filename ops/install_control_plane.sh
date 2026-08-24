#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE_ROOT=${1:-/opt/aicc}
readonly EXPECTED_SHA=${2:-}
readonly UNIT_SOURCE="$SOURCE_ROOT/deploy/systemd"
readonly UNIT_TARGET=/etc/systemd/system
readonly EVIDENCE_TARGET=/var/lib/aicc-control-plane
readonly UNITS=(
  aicc-backlog-planner.service
  aicc-backlog-planner.timer
  aicc-backlog-review.service
  aicc-backlog-review.timer
  aicc-backlog-merge.service
  aicc-backlog-merge.timer
  aicc-queue-reaper.service
  aicc-queue-reaper.timer
  aicc-control-plane-reconciler.service
  aicc-control-plane-reconciler.timer
  aicc-control-plane-watchdog.service
  aicc-control-plane-watchdog.timer
)

if [[ $(id -u) -ne 0 ]]; then
  echo "refusing: install_control_plane.sh must run as root" >&2
  exit 2
fi
if [[ "$SOURCE_ROOT" != /opt/aicc ]]; then
  echo "refusing: versioned units execute only the canonical /opt/aicc checkout" >&2
  exit 2
fi
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "refusing: pass the exact 40-character merged SHA as argument 2" >&2
  exit 2
fi
if [[ $(/usr/bin/git -C "$SOURCE_ROOT" rev-parse HEAD) != "$EXPECTED_SHA" ]]; then
  echo "refusing: deployed checkout is not the expected merged SHA" >&2
  exit 2
fi
if [[ -n $(/usr/bin/git -C "$SOURCE_ROOT" status --porcelain) ]]; then
  echo "refusing: deployed checkout is not clean" >&2
  exit 2
fi
if [[ ! -x "$SOURCE_ROOT/.venv/bin/python" ]]; then
  echo "refusing: missing runtime $SOURCE_ROOT/.venv/bin/python" >&2
  exit 2
fi
if [[ ! -r /etc/aicc/app.env ]]; then
  echo "refusing: missing /etc/aicc/app.env" >&2
  exit 2
fi

# Validate the release before mutating systemd. A migration or import failure
# leaves the live units untouched.
"$SOURCE_ROOT/.venv/bin/python" -m command_center.db status >/dev/null
"$SOURCE_ROOT/.venv/bin/python" -m command_center.db control-plane-reconcile \
  --repo-path "$SOURCE_ROOT" --dry-run >/dev/null

for unit in "${UNITS[@]}"; do
  test -f "$UNIT_SOURCE/$unit"
  /usr/bin/install -o root -g root -m 0644 "$UNIT_SOURCE/$unit" "$UNIT_TARGET/$unit"
done
/usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec
/usr/bin/install -o root -g root -m 0755 \
  "$SOURCE_ROOT/ops/aicc_control_plane_watchdog.sh" \
  /usr/local/libexec/aicc-control-plane-watchdog

/bin/systemctl daemon-reload
/bin/systemctl enable --now \
  aicc-backlog-planner.timer aicc-backlog-review.timer \
  aicc-backlog-merge.timer aicc-queue-reaper.timer
/bin/systemctl enable --now aicc-control-plane-reconciler.timer
/bin/systemctl enable --now aicc-control-plane-watchdog.timer
/bin/systemctl start aicc-control-plane-reconciler.service
/bin/systemctl is-active --quiet \
  aicc-backlog-planner.timer aicc-backlog-review.timer \
  aicc-backlog-merge.timer aicc-queue-reaper.timer \
  aicc-control-plane-reconciler.timer aicc-control-plane-watchdog.timer
"$SOURCE_ROOT/.venv/bin/python" -m command_center.db control-plane-health \
  --max-age-seconds 180

# Durable deployment evidence is written only after the exact merged checkout,
# dry-run, installed units, live tick and readiness probe have all succeeded.
/usr/bin/install -d -o root -g root -m 0700 "$EVIDENCE_TARGET"
evidence_tmp=$(/usr/bin/mktemp "$EVIDENCE_TARGET/.deployed-sha.XXXXXX")
trap '/usr/bin/rm -f -- "$evidence_tmp"' EXIT
printf '%s\n' "$EXPECTED_SHA" >"$evidence_tmp"
/usr/bin/chown root:root "$evidence_tmp"
/usr/bin/chmod 0444 "$evidence_tmp"
/usr/bin/mv -f -- "$evidence_tmp" "$EVIDENCE_TARGET/deployed-sha"
trap - EXIT
[[ $(/usr/bin/stat -c '%U:%a' "$EVIDENCE_TARGET/deployed-sha") == root:444 ]]
