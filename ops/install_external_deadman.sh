#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE_ROOT=${1:?source root required}
readonly ENV_SOURCE=${2:?root-owned environment source required}
readonly UNIT_SOURCE="$SOURCE_ROOT/deploy/systemd-external"

[[ $(id -u) -eq 0 ]] || { echo "refusing: root required" >&2; exit 2; }
[[ "$SOURCE_ROOT" == /* && -d "$SOURCE_ROOT" && ! -L "$SOURCE_ROOT" ]] || exit 2
[[ "$ENV_SOURCE" == /* && -f "$ENV_SOURCE" && ! -L "$ENV_SOURCE" ]] || exit 2
[[ $(/usr/bin/stat -c '%U:%a' "$ENV_SOURCE") =~ ^root:(400|600)$ ]] || exit 2

set -a
# shellcheck disable=SC1090
source "$ENV_SOURCE"
set +a
[[ ${AICC_DEADMAN_TARGET:-} == *@* && ${AICC_DEADMAN_TARGET#*@} != "$(hostname -f)" ]] \
  || { echo "refusing: dead-man must target a different host" >&2; exit 2; }
[[ ${AICC_DEADMAN_ALERT_URL:-} == https://* ]] || exit 2

/usr/bin/id aicc-deadman >/dev/null 2>&1 \
  || /usr/sbin/useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin aicc-deadman
/usr/bin/install -d -o root -g aicc-deadman -m 0750 /etc/aicc-deadman
/usr/bin/install -o root -g aicc-deadman -m 0440 "$ENV_SOURCE" /etc/aicc-deadman/deadman.env
/usr/bin/install -o root -g root -m 0755 "$SOURCE_ROOT/ops/aicc_external_deadman.sh" \
  /usr/local/libexec/aicc-external-deadman
/usr/bin/systemd-analyze verify "$UNIT_SOURCE/aicc-external-deadman.service" \
  "$UNIT_SOURCE/aicc-external-deadman.timer"
for unit in aicc-external-deadman.service aicc-external-deadman.timer; do
  /usr/bin/install -o root -g root -m 0644 "$UNIT_SOURCE/$unit" "/etc/systemd/system/$unit"
done
/bin/systemctl daemon-reload
/bin/systemctl enable --now aicc-external-deadman.timer
