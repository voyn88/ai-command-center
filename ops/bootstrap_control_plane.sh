#!/usr/bin/env bash
set -euo pipefail

readonly SOURCE_ROOT=${1:-}
readonly EXPECTED_SHA=${2:-}
readonly APP_ENV_SOURCE=${3:-}
readonly MIGRATOR_ENV_SOURCE=${4:-}
readonly DEPLOYER_ENV_SOURCE=${5:-}
readonly TASK_ID=${6:-}
readonly PR_URL=${7:-}
readonly RELEASE_ROOT=/opt/aicc-releases
readonly TARGET="$RELEASE_ROOT/$EXPECTED_SHA"
readonly CURRENT=/opt/aicc

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
[[ "$PR_URL" =~ ^https://github.com/[^/]+/[^/]+/pull/[1-9][0-9]*$ ]] || {
  echo "refusing: canonical merged PR URL required" >&2; exit 2;
}
command -v gh >/dev/null || { echo "refusing: gh is required" >&2; exit 2; }
# GitHub's authoritative state and mergeCommit are checked before touching the
# live release pointer, credentials, units, database, or systemd.
merge_view=$(gh pr view "$PR_URL" --json state,mergeCommit)
python3 - "$EXPECTED_SHA" "$merge_view" <<'PY'
import json
import sys

expected, raw = sys.argv[1:]
view = json.loads(raw)
if view.get("state") != "MERGED" or (view.get("mergeCommit") or {}).get("oid") != expected:
    raise SystemExit("refusing: PR is not authoritatively merged at expected SHA")
PY
for source in "$APP_ENV_SOURCE" "$MIGRATOR_ENV_SOURCE" "$DEPLOYER_ENV_SOURCE"; do
  [[ "$source" == /* && -f "$source" && ! -L "$source" ]] || {
    echo "refusing: three root-owned DB environment sources are required" >&2; exit 2;
  }
  [[ $(/usr/bin/stat -c '%U:%a' "$source") =~ ^root:(400|600)$ ]] || {
    echo "refusing: DB environments must be root-owned mode 0400/0600" >&2; exit 2;
  }
done
command -v uv >/dev/null || { echo "refusing: uv is required" >&2; exit 2; }

if [[ ! -e "$TARGET" ]]; then
  /usr/bin/install -d -o root -g root -m 0755 "$RELEASE_ROOT"
  /usr/bin/git clone --local --no-hardlinks --no-checkout -- "$SOURCE_ROOT" "$TARGET"
  /usr/bin/git -C "$TARGET" checkout --detach "$EXPECTED_SHA"
  uv sync --directory "$TARGET" --frozen --no-dev
fi
[[ ! -L "$TARGET" && -d "$TARGET/.git" ]]
[[ $(/usr/bin/git -C "$TARGET" rev-parse HEAD) == "$EXPECTED_SHA" ]]
[[ -x "$TARGET/.venv/bin/python" ]]
[[ -z $(/usr/bin/git -C "$TARGET" status --porcelain) ]]

/usr/bin/install -d -o root -g root -m 0755 /etc/aicc
/usr/bin/install -o root -g root -m 0600 "$APP_ENV_SOURCE" /etc/aicc/app.env
/usr/bin/install -o root -g root -m 0600 "$MIGRATOR_ENV_SOURCE" /etc/aicc/migrator.env
/usr/bin/install -o root -g root -m 0600 "$DEPLOYER_ENV_SOURCE" /etc/aicc/deployer.env
old_release=""
[[ ! -e "$CURRENT" || -L "$CURRENT" ]] || {
  echo "refusing: legacy non-versioned /opt/aicc requires explicit migration" >&2
  exit 2
}
[[ ! -e "$CURRENT" && ! -L "$CURRENT" ]] || old_release=$(/usr/bin/readlink -f "$CURRENT")
link_dir=$(/usr/bin/mktemp -d /opt/.aicc-current.XXXXXX)
/usr/bin/ln -s -- "$TARGET" "$link_dir/current"
/usr/bin/mv -Tf -- "$link_dir/current" "$CURRENT"
/usr/bin/rmdir -- "$link_dir"
rollback_release() {
  if [[ -n "$old_release" ]]; then
    rollback_dir=$(/usr/bin/mktemp -d /opt/.aicc-rollback.XXXXXX)
    /usr/bin/ln -s -- "$old_release" "$rollback_dir/current"
    /usr/bin/mv -Tf -- "$rollback_dir/current" "$CURRENT"
    /usr/bin/rmdir -- "$rollback_dir"
  else
    /usr/bin/rm -f -- "$CURRENT"
  fi
  /bin/systemctl daemon-reload || true
  /bin/systemctl try-restart aicc-control-plane-reconciler.service || true
}
trap rollback_release ERR
"$TARGET/ops/install_control_plane.sh" "$TARGET" "$EXPECTED_SHA" "$TASK_ID"
trap - ERR
