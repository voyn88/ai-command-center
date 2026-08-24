#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE_ROOT=${1:-}
readonly EXPECTED_SHA=${2:-}
readonly APP_ENV_SOURCE=${3:-}
readonly MIGRATOR_ENV_SOURCE=${4:-}
readonly DEPLOYER_ENV_SOURCE=${5:-}
readonly TASK_ID=${6:-}
readonly TARGET=/opt/aicc

[[ $(id -u) -eq 0 ]] || { echo "refusing: root required" >&2; exit 2; }
[[ "$SOURCE_ROOT" == /* && -d "$SOURCE_ROOT/.git" && ! -L "$SOURCE_ROOT" ]] || {
  echo "refusing: trusted absolute Git source required" >&2; exit 2;
}
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "refusing: exact merged SHA required" >&2; exit 2;
}
[[ $(/usr/bin/git -C "$SOURCE_ROOT" rev-parse HEAD) == "$EXPECTED_SHA" ]] || {
  echo "refusing: source HEAD mismatch" >&2; exit 2;
}
[[ -z $(/usr/bin/git -C "$SOURCE_ROOT" status --porcelain) ]] || {
  echo "refusing: source checkout is dirty" >&2; exit 2;
}
[[ "$TASK_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$ ]] || {
  echo "refusing: canonical deployment task id required" >&2; exit 2;
}
for source in "$APP_ENV_SOURCE" "$MIGRATOR_ENV_SOURCE" "$DEPLOYER_ENV_SOURCE"; do
  [[ "$source" == /* && -f "$source" && ! -L "$source" ]] || {
    echo "refusing: three root-owned DB environment sources are required" >&2; exit 2;
  }
  [[ $(/usr/bin/stat -c '%U:%a' "$source") =~ ^root:(400|600)$ ]] || {
    echo "refusing: DB environments must be root-owned mode 0400/0600" >&2; exit 2;
  }
done
[[ ! -e "$TARGET" && ! -L "$TARGET" ]] || {
  echo "refusing: canonical target already exists; use guarded deploy" >&2; exit 2;
}
command -v uv >/dev/null || { echo "refusing: uv is required" >&2; exit 2; }

/usr/bin/install -d -o root -g root -m 0755 /etc/aicc
/usr/bin/install -o root -g root -m 0600 "$APP_ENV_SOURCE" /etc/aicc/app.env
/usr/bin/install -o root -g root -m 0600 "$MIGRATOR_ENV_SOURCE" /etc/aicc/migrator.env
/usr/bin/install -o root -g root -m 0600 "$DEPLOYER_ENV_SOURCE" /etc/aicc/deployer.env
/usr/bin/git clone --local --no-hardlinks --no-checkout -- "$SOURCE_ROOT" "$TARGET"
bootstrap_failed() {
  /usr/bin/mv -- "$TARGET" "$TARGET.bootstrap-failed.$(/usr/bin/date -u +%Y%m%dT%H%M%SZ)"
}
trap bootstrap_failed ERR
/usr/bin/git -C "$TARGET" checkout --detach "$EXPECTED_SHA"
uv sync --directory "$TARGET" --frozen --no-dev
[[ -x "$TARGET/.venv/bin/python" ]]
[[ -z $(/usr/bin/git -C "$TARGET" status --porcelain) ]]
trap - ERR
exec "$TARGET/ops/install_control_plane.sh" "$TARGET" "$EXPECTED_SHA" "$TASK_ID"
