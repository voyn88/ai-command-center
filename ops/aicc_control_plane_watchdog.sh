#!/usr/bin/env bash
set -euo pipefail

readonly AICC_ROOT=/opt/aicc
readonly PYTHON="$AICC_ROOT/.venv/bin/python"

control_healthy=0
notification_healthy=0
"$PYTHON" -m command_center.db control-plane-health --max-age-seconds 180 \
  && control_healthy=1
"$PYTHON" -m command_center.db control-plane-notification-health \
  --max-age-seconds 900 && notification_healthy=1
if (( control_healthy == 1 && notification_healthy == 1 )); then
  exit 0
fi

# This monitor is a distinct root-owned timer.  It repairs the reconciler's
# wake-up path once, then proves the repair produced a fresh healthy heartbeat.
if (( control_healthy == 0 )); then
  /bin/systemctl start aicc-control-plane-reconciler.timer
  /bin/systemctl start aicc-control-plane-reconciler.service
fi
if (( notification_healthy == 0 )); then
  /bin/systemctl start aicc-control-plane-notify.timer
  /bin/systemctl start aicc-control-plane-notify.service || true
fi
"$PYTHON" -m command_center.db control-plane-health --max-age-seconds 180
"$PYTHON" -m command_center.db control-plane-notification-health --max-age-seconds 900
