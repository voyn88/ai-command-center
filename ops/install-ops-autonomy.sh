#!/bin/bash
# Install, check or remove the host self-care timers (disk guard, status
# beacon, self-heal).  Idempotent; run as root on a fleet host:
#
#   bash /opt/aicc/current/ops/install-ops-autonomy.sh --role worker
#   bash /opt/aicc/current/ops/install-ops-autonomy.sh --check
#   bash /opt/aicc/current/ops/install-ops-autonomy.sh --live all
#   bash /opt/aicc/current/ops/install-ops-autonomy.sh --uninstall
#
# The command line uses only lower-case letters, digits, dashes, dots and
# slashes on purpose: the provider's web console drops the Shift key, so
# anything else is mangled there.  The script lives in the release directory
# the host already deploys by itself, so nothing has to be downloaded or
# copied by hand.
#
# What install does (and nothing else):
#   1. refuses unless the deployed release holds the modules and unit files;
#   2. creates /etc/aicc/ops-autonomy.env ONCE (never overwritten) with every
#      path detected on this host and BOTH features in dry-run;
#   3. copies three service/timer pairs into /etc/systemd/system (existing
#      files of the same name are saved first), writes the disk guard's
#      sandbox drop-in from the settings, enables the timers;
#   4. changes nothing else: no package, no firewall, no user, no secret.
# Layout: units run in /opt/aicc/current (-> releases/<sha>) with its .venv;
# AICC_RELEASE_DIR / AICC_VENV_PYTHON override both (written as unit drop-ins).
# Rollback: --uninstall removes exactly what install added.
set -euo pipefail

# Layout of a fleet host: /opt/aicc/current -> releases/<sha>, and the
# interpreter is the release's own .venv (on a worker that is a link to the
# runtime venv).  There is no /opt/aicc/.venv.  Both are overridable.
DEFAULT_RELEASE_DIR=/opt/aicc/current
RELEASE_DIR="${AICC_RELEASE_DIR:-$DEFAULT_RELEASE_DIR}"
VENV_PY="${AICC_VENV_PYTHON:-$RELEASE_DIR/.venv/bin/python}"
SETTINGS="${AICC_OPS_SETTINGS:-/etc/aicc/ops-autonomy.env}"
STATE_DIR="${AICC_OPS_STATE_DIR:-/var/lib/voyn-ops}"
LOG_DIR="${AICC_OPS_LOG_DIR:-/var/log/voyn-ops}"
UNIT_DIR="${AICC_UNIT_DIR:-/etc/systemd/system}"
JOURNALD_DIR="${AICC_JOURNALD_DIR:-/etc/systemd/journald.conf.d}"
UNITS=(voyn-ops-disk-guard voyn-ops-beacon voyn-ops-self-heal)
SYSTEMCTL="${AICC_SYSTEMCTL:-systemctl}"

mode=install
role=""
live=""

say() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: install-ops-autonomy.sh [--role worker|control] [--check]
                               [--live disk|heal|all] [--dry-run all]
                               [--uninstall] [--help]
  (no option)      install; role is detected from the host name if omitted
  --check          read-only status report
  --live X         turn real deletion (disk) / real restarts (heal) on
  --dry-run all    turn both back to dry run
  --uninstall      remove the timers and unit files this script installed
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --role) [ $# -ge 2 ] || die "--role needs worker or control"; role="$2"; shift 2 ;;
    --check) mode=check; shift ;;
    --live) [ $# -ge 2 ] || die "--live needs disk, heal or all"; mode=live; live="$2"; shift 2 ;;
    --dry-run) [ $# -ge 2 ] || die "--dry-run needs disk, heal or all"; mode=dry; live="$2"; shift 2 ;;
    --uninstall) mode=uninstall; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

need_root() {
  # AICC_OPS_ALLOW_NONROOT exists only so the test suite can run this script
  # against a temporary directory tree; on a host it is never set.
  [ "${AICC_OPS_ALLOW_NONROOT:-}" = 1 ] && return 0
  [ "$(id -u)" -eq 0 ] || die "run as root (the provider web console is root already)"
}

get_key() { # KEY -> value from the settings file (never sourced/executed)
  { grep -E "^$1=" "$SETTINGS" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2-
}

set_key() { # KEY VALUE  (portable: no sed -i; the key moves to the end)
  local key="$1" value="$2" tmp="$SETTINGS.tmp"
  { grep -vE "^$key=" "$SETTINGS" 2>/dev/null || true; printf '%s=%s\n' "$key" "$value"; } >"$tmp"
  mv "$tmp" "$SETTINGS"
  chmod 0644 "$SETTINGS"
}

detect_role() {
  case "$(hostname)" in
    *worker*) echo worker ;;
    *control*) echo control ;;
    *) echo "" ;;
  esac
}

detect_owner() { # account that owns the working clones (never a literal name)
  local d
  if [ -n "${AICC_OPS_OWNER:-}" ]; then echo "$AICC_OPS_OWNER"; return; fi
  for d in /home/*/Projects/ai-command-center-task-clones /home/*/Projects/ai-command-center; do
    [ -d "$d" ] || continue
    stat -c %U "$d" 2>/dev/null && return
  done
  if [ -n "${SUDO_USER:-}" ]; then echo "$SUDO_USER"; else echo ""; fi
}

join_existing_dirs() { # prints colon-joined list of the arguments that are dirs
  local out="" d
  for d in "$@"; do
    [ -d "$d" ] || continue
    out="${out:+$out:}$d"
  done
  printf '%s' "$out"
}

file_owner() { stat -c %U "$1" 2>/dev/null || stat -f %Su "$1" 2>/dev/null; }
file_mode() { stat -c %a "$1" 2>/dev/null || { stat -f %p "$1" 2>/dev/null | sed 's/.*\(....\)$/\1/'; }; }

# AICC_OPS_TRUST_OWNER lets the test suite trust its own account; on a host it
# is never set and only root is trusted.
trusted_owner() { [ "$1" = root ] || [ -n "${AICC_OPS_TRUST_OWNER:-}" -a "$1" = "${AICC_OPS_TRUST_OWNER:-}" ]; }

dir_or_file_safe() { # PATH -> owned by a trusted account, no group/other write
  local owner mode g o
  owner="$(file_owner "$1")" || return 1
  mode="$(file_mode "$1")" || return 1
  trusted_owner "$owner" || return 1
  while [ ${#mode} -lt 4 ]; do mode="0$mode"; done
  g="${mode:2:1}"; o="${mode:3:1}"
  if [ -d "$1" ] && [ $((${mode:0:1} & 1)) -eq 1 ]; then
    # sticky directory (/tmp style): writers cannot rename or replace entries
    # they do not own, so a trusted-owner sticky directory is safe to pass
    return 0
  fi
  [ $((g & 2)) -eq 0 ] && [ $((o & 2)) -eq 0 ]
}

root_only_writable() { # PATH -> the object AND every directory above it are root-only
  # Whoever can rename /opt/aicc/current or write /opt/aicc/releases swaps the
  # code root runs, so the whole chain counts, above the link and above its
  # target (security review U2).
  local p="$1" real d
  real="$(readlink -f "$p")" || return 1
  dir_or_file_safe "$real" || { BAD_PATH="$real"; return 1; }
  for d in "$(dirname "$p")" "$(dirname "$real")"; do
    d="$(readlink -f "$d")" || return 1
    while :; do
      dir_or_file_safe "$d" || { BAD_PATH="$d"; return 1; }
      [ "$d" = / ] && break
      d="$(dirname "$d")"
    done
  done
  return 0
}

preflight() {
  # these run as root from systemd: whoever can write there controls root
  local p BAD_PATH=""
  printf '%s\n%s\n' "$RELEASE_DIR" "$VENV_PY" | grep -vqE '^/[A-Za-z0-9._/-]+$' \
    && die "release and interpreter paths may only contain letters, digits, dot, dash, underscore and slash"
  for p in "$RELEASE_DIR" "$RELEASE_DIR/.venv" "$VENV_PY"; do
    [ -e "$p" ] || continue
    root_only_writable "$p" || die "$BAD_PATH must be owned by root and not writable by group or others before root timers may run code from $p (the directories above it count too). Owner fix: chown root:root $BAD_PATH and chmod 755 $BAD_PATH, then run this again"
  done
  [ -x "$VENV_PY" ] || die "no interpreter at $VENV_PY (is the fleet release deployed on this host?)"
  local u
  for u in "${UNITS[@]}"; do
    [ -f "$RELEASE_DIR/deploy/systemd/$u.service" ] && [ -f "$RELEASE_DIR/deploy/systemd/$u.timer" ] \
      || die "release $RELEASE_DIR has no $u unit files yet: wait until this host has deployed the release that contains them, then run this again"
  done
  (cd "$RELEASE_DIR" && "$VENV_PY" - <<'PY') || die "the deployed release does not import the self-care modules"
import command_center.ops.disk_guard
import command_center.ops.status_beacon
import command_center.ops.self_heal
PY
  [ -f "$RELEASE_DIR/deploy/aicc/ops-autonomy.env" ] || die "settings template missing in the release"
}

write_settings_once() {
  mkdir -p "$(dirname "$SETTINGS")"
  if [ -f "$SETTINGS" ]; then
    say "settings kept as they are: $SETTINGS"
    [ -z "$role" ] || set_key OPS_HOST_ROLE "$role"
    return
  fi
  cp "$RELEASE_DIR/deploy/aicc/ops-autonomy.env" "$SETTINGS"
  chmod 0644 "$SETTINGS"
  set_key OPS_HOST_ROLE "${role:-worker}"
  local owner home roots caches models
  owner="$(detect_owner)"
  if [ -n "$owner" ]; then
    home="$(getent passwd "$owner" | cut -d: -f6)"
    roots="$(join_existing_dirs "$home/Projects/ai-command-center-task-clones" "$home/Projects/aios-task-clones")"
    caches="$(join_existing_dirs "$home/.npm/_cacache" "$home/.cache/pip" "$home/.cache/uv")"
    models="$(join_existing_dirs "$home/.ollama/models" "/usr/share/ollama/.ollama/models")"
    models="${models%%:*}"
    set_key OPS_CACHE_DIRS "$caches"
    set_key OPS_OLLAMA_MODELS_DIR "$models"
  else
    roots=""
    warn "no owner account found: clone roots, cache dirs and model dir left empty"
  fi
  # per-repository workspace roots of the worker lanes (children are clones)
  if [ -d /srv/aicc-workspaces ]; then
    local ws="" d
    for d in /srv/aicc-workspaces/*/; do
      [ -d "$d" ] && ws="${ws:+$ws:}${d%/}"
    done
    roots="${roots:+$roots:}$ws"
  fi
  set_key OPS_CLONE_ROOTS "${roots%:}"
  set_key OPS_DISK_GUARD_DRY_RUN 1
  set_key OPS_SELF_HEAL_DRY_RUN 1
  say "settings written: $SETTINGS (both features start in DRY RUN)"
}

write_disk_guard_dropin() {
  local dir="$UNIT_DIR/voyn-ops-disk-guard.service.d" paths="" p
  for p in $(get_key OPS_CLONE_ROOTS | tr ':' ' ') \
           $(get_key OPS_CACHE_DIRS | tr ':' ' ') \
           $(get_key OPS_OLLAMA_MODELS_DIR | tr ':' ' '); do
    [ -d "$p" ] && paths="$paths -$p"
  done
  mkdir -p "$dir"
  {
    echo "[Service]"
    echo "# written by install-ops-autonomy.sh from $SETTINGS"
    [ -z "$paths" ] || echo "ReadWritePaths=${paths# }"
  } >"$dir/paths.conf"
}

write_layout_dropins() {
  # The shipped units name /opt/aicc/current and its .venv.  A host (or a
  # test) that overrides either gets the same paths written into a drop-in.
  local u dir mod
  for u in "${UNITS[@]}"; do
    dir="$UNIT_DIR/$u.service.d"
    case "$u" in
      voyn-ops-disk-guard) mod=disk_guard ;;
      voyn-ops-beacon) mod=status_beacon ;;
      *) mod=self_heal ;;
    esac
    if [ "$RELEASE_DIR" = "$DEFAULT_RELEASE_DIR" ] && [ "$VENV_PY" = "$DEFAULT_RELEASE_DIR/.venv/bin/python" ]; then
      rm -f "$dir/layout.conf"
      continue
    fi
    mkdir -p "$dir"
    {
      echo "[Service]"
      echo "# written by install-ops-autonomy.sh: this host's release layout"
      echo "WorkingDirectory=$RELEASE_DIR"
      echo "ExecStart="
      echo "ExecStart=$VENV_PY -m command_center.ops.$mod"
    } >"$dir/layout.conf"
  done
}

prepare_beacon_account() {
  # Security review M1: the beacon runs as the fleet account, not as root.
  local dir="$UNIT_DIR/voyn-ops-beacon.service.d"
  if getent passwd aicc-worker >/dev/null 2>&1; then
    [ "${AICC_OPS_ALLOW_NONROOT:-}" = 1 ] || chown aicc-worker:aicc-worker "$STATE_DIR/beacon" "$LOG_DIR/beacon"
    rm -rf "$dir"
  else
    warn "no aicc-worker account on this host: the status beacon will run as root (weaker); create the account or accept this"
    mkdir -p "$dir"
    printf '[Service]\nUser=root\nGroup=root\nSupplementaryGroups=\n' >"$dir/user.conf"
  fi
}

write_journald_cap() {
  # The journal is capped by journald itself (SystemMaxUse), so no timer needs
  # the privilege to vacuum it.  Existing owner configuration is never replaced.
  local mb file="$JOURNALD_DIR/voyn-ops.conf" want
  mb="$(get_key OPS_JOURNAL_MAX_MB)"
  case "$mb" in ''|*[!0-9]*) return 0 ;; esac
  [ "$mb" -gt 0 ] || return 0
  want="[Journal]
SystemMaxUse=${mb}M"
  mkdir -p "$JOURNALD_DIR"
  if [ -f "$file" ] && [ "$(cat "$file")" = "$want" ]; then return 0; fi
  if grep -rqs '^SystemMaxUse=' /etc/systemd/journald.conf /etc/systemd/journald.conf.d 2>/dev/null && [ ! -f "$file" ]; then
    say "journald already has a size cap: left as it is"
    return 0
  fi
  printf '%s\n' "$want" >"$file"
  "$SYSTEMCTL" restart systemd-journald
  say "journal capped at ${mb}M ($file)"
}

install_all() {
  need_root
  [ -z "$role" ] || case "$role" in worker|control) ;; *) die "role must be worker or control" ;; esac
  [ -n "$role" ] || role="$(detect_role)"
  [ -n "$role" ] || die "cannot tell the host role from its name: add --role worker or --role control"
  preflight
  mkdir -p "$STATE_DIR/beacon" "$LOG_DIR/beacon"
  # World-readable on purpose: the unprivileged beacon reads the self-heal
  # state, and neither directory holds a secret (decisions and counters only).
  chmod 0755 "$STATE_DIR" "$LOG_DIR"
  write_settings_once
  prepare_beacon_account
  write_journald_cap
  local stamp backup u f own=(-o root -g root)
  [ "${AICC_OPS_ALLOW_NONROOT:-}" != 1 ] || own=()
  stamp="$(date +%Y%m%d%H%M%S)"
  backup="$STATE_DIR/backup-$stamp"
  for u in "${UNITS[@]}"; do
    for f in "$u.service" "$u.timer"; do
      if [ -f "$UNIT_DIR/$f" ] && ! cmp -s "$UNIT_DIR/$f" "$RELEASE_DIR/deploy/systemd/$f"; then
        mkdir -p "$backup"
        cp -a "$UNIT_DIR/$f" "$backup/$f"
      fi
      install ${own[@]+"${own[@]}"} -m 0644 "$RELEASE_DIR/deploy/systemd/$f" "$UNIT_DIR/$f"
    done
  done
  write_disk_guard_dropin
  write_layout_dropins
  "$SYSTEMCTL" daemon-reload
  for u in "${UNITS[@]}"; do
    "$SYSTEMCTL" enable --now "$u.timer"
  done
  say "installed: ${UNITS[*]} (role $(get_key OPS_HOST_ROLE))"
  say "next: wait one day, read the dry-run journal, then:  bash $0 --live all"
}

dry_run_seen_recently() { # LOGFILE -> at least one run logged in the last 24 h
  [ -n "$(find "$1" -mmin -1440 2>/dev/null)" ] && grep -q '"event"' "$1" 2>/dev/null
}

set_live() { # disk|heal|all  value 0|1
  need_root
  [ -f "$SETTINGS" ] || die "install first (no $SETTINGS)"
  if [ "$1" = 0 ]; then
    case "$live" in disk|all)
      dry_run_seen_recently "$LOG_DIR/disk-guard.jsonl" \
        || die "no dry-run of the disk guard in the last 24 hours: let it run in dry run first, read $LOG_DIR/disk-guard.jsonl, then try again" ;;
    esac
    case "$live" in heal|all)
      dry_run_seen_recently "$LOG_DIR/self-heal.jsonl" \
        || die "no dry-run of self-heal in the last 24 hours: let it run in dry run first" ;;
    esac
  fi
  case "$live" in
    disk) set_key OPS_DISK_GUARD_DRY_RUN "$1" ;;
    heal) set_key OPS_SELF_HEAL_DRY_RUN "$1" ;;
    all) set_key OPS_DISK_GUARD_DRY_RUN "$1"; set_key OPS_SELF_HEAL_DRY_RUN "$1" ;;
    *) die "say which: disk, heal or all" ;;
  esac
  say "disk guard dry run: $(get_key OPS_DISK_GUARD_DRY_RUN)   self-heal dry run: $(get_key OPS_SELF_HEAL_DRY_RUN)"
}

check_all() {
  say "== settings ($SETTINGS)"
  if [ -f "$SETTINGS" ]; then
    grep -E '^OPS_(HOST_ROLE|DISK_GUARD_DRY_RUN|SELF_HEAL_DRY_RUN|DISK_LOW_FREE_PCT|DISK_TARGET_FREE_PCT|BEACON_PUBLISH)=' "$SETTINGS" || true
  else
    say "missing: not installed"
  fi
  say "== timers"
  local u
  for u in "${UNITS[@]}"; do
    printf '%s: %s\n' "$u" "$("$SYSTEMCTL" is-active "$u.timer" 2>/dev/null || true)"
  done
  say "== disk"
  df -h / | tail -n 1
  say "== last journal lines"
  for u in disk-guard beacon self-heal; do
    printf -- '-- %s\n' "$u"
    tail -n 3 "$LOG_DIR/${u}.jsonl" 2>/dev/null || say "(no log yet)"
  done
}

uninstall_all() {
  need_root
  local u
  for u in "${UNITS[@]}"; do
    "$SYSTEMCTL" disable --now "$u.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$u.timer" "$UNIT_DIR/$u.service"
  done
  rm -rf "$UNIT_DIR/voyn-ops-disk-guard.service.d" "$UNIT_DIR/voyn-ops-beacon.service.d" "$UNIT_DIR/voyn-ops-self-heal.service.d"
  if [ -f "$JOURNALD_DIR/voyn-ops.conf" ]; then
    rm -f "$JOURNALD_DIR/voyn-ops.conf"
    "$SYSTEMCTL" restart systemd-journald
  fi
  local newest f
  newest="$(ls -d "$STATE_DIR"/backup-* 2>/dev/null | tail -n 1 || true)"
  if [ -n "$newest" ]; then
    for f in "$newest"/*; do
      [ -f "$f" ] && cp -a "$f" "$UNIT_DIR/$(basename "$f")" && say "restored $(basename "$f")"
    done
  fi
  "$SYSTEMCTL" daemon-reload
  say "removed the timers and unit files; settings, logs and state are kept ($SETTINGS, $LOG_DIR, $STATE_DIR)"
}

case "$mode" in
  install) install_all ;;
  check) check_all ;;
  live) set_live 0 ;;
  dry) set_live 1 ;;
  uninstall) uninstall_all ;;
esac
