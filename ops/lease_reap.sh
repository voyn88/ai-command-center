#!/bin/bash
# VOYN-W0-AICC-LEASE-STUCK-EXPIRED-NO-RECLAIM: mechanical reap of expired
# voyn-lease rows, independent of any Claude session (systemd/cron only).
#
# --auto-takeover only succeeds against a row the authority itself confirms
# is past expiry with a dead recorded holder -- never overrides a live one.
# Empirically verified 2026-08-22, not merely assumed: acquired a real,
# non-expired lease under one identity, then attempted `acquire
# --auto-takeover` against the same repository under a second identity
# while the first was still live -- refused with `VOYN_LEASE_REFUSED
# active owner=...`. The safety property this whole script leans on is a
# tested fact about the external tool, not a comment.
#
# Independent review (2026-08-22) of an earlier revision found two real
# failure-mode bugs, both fixed here: (1) `date -d` failing on an
# unparseable `expires_at` silently fell back to epoch 0, which made
# EVERY row look expired -- the wrong default direction for a script that
# reaps unattended forever; a parse failure now skips that row and logs a
# warning instead. (2) a missing `jq` or malformed `voyn-lease list`
# output was swallowed silently (`while read` on an empty pipe still exits
# 0), so the "backstop" would do nothing with no signal that anything was
# wrong; both are checked explicitly now and exit nonzero with a logged
# reason.
#
# VOYN-W0-AICC-REAPER-NOT-RUNNING ("death of a connection does not release
# a claim"): a claim -- a lease row -- is not released when the holder's
# connection or process dies. Nothing in the protocol can release it; the
# only thing that ever does is expiry plus THIS sweep, which is why
# `worker/daemon.py` calls worker abandonment "safe by construction". That
# construction was missing its one load-bearing piece: the sweep had no
# unit in this repository at all, so "a cron-based reap sweep clears any
# future stuck row within 5 minutes" (#358) described an installation on
# one host that nothing versioned here reproduced or kept running.
# `deploy/systemd/voyn-aicc-lease-reap.{service,timer}` is that unit.
#
# The second half of "not running" is this script's own working directory.
# `voyn-lease` resolves its cwd to a repository, and the original cwd was
# the shared preprod clone -- a tree agents check out, detach and rewrite.
# A detached HEAD there (routine during worktree work) made every sweep
# die on `invalid branch` until a human intervened: a reaper that stops
# reaping exactly when the fleet is busiest. The sweep now runs from a
# disposable identity repository it owns outright.
#
# Independent review (2026-09-13) rejected the first revision of that
# bootstrap: it ran `git init` and only then committed, and skipped the
# whole block whenever `.git` already existed. An interrupted or failed
# first run therefore left an unborn, branchless repository that every
# later run accepted and `voyn-lease` refused forever -- the same
# permanent `invalid branch` failure, now self-inflicted, and invisible to
# tests that only covered clean creation and reuse. Two changes answer it:
# validity (a work tree, HEAD on a branch, that branch pointing at a real
# commit) is re-derived from disk on EVERY run rather than inferred from
# the existence of `.git`, and the repository is built to completion in a
# sibling temporary directory that is renamed into place only once it is
# valid. $REPO therefore never holds a half-initialized repository, and a
# repository that became invalid by any route -- interrupted init, failed
# commit, detached HEAD, deleted branch, truncated object store -- is
# rebuilt on the next tick instead of poisoning every tick after it.
set -euo pipefail

# Every path is overridable, so the unit -- not an edit to this script --
# is what pins a host's layout, and the tests can drive the real script
# against a throwaway tree. The fallback derives from the invoking
# operator's home rather than from one operator's absolute path written
# into a public repository (VOYN-OPS-PUBLIC-REPO-CLAUDE-MD-LEAK), which
# keeps the legacy hand-installed cron entry -- no environment, HOME set --
# reading and writing exactly the paths it always did.
if [ -z "${AICC_LEASE_REAP_ROOT:-}" ] && [ -z "${HOME:-}" ] &&
   { [ -z "${AICC_LEASE_REAP_REPO:-}" ] || [ -z "${AICC_LEASE_REAP_LOG:-}" ]; }; then
  echo "lease_reap: no HOME and no AICC_LEASE_REAP_ROOT/REPO/LOG -- nowhere to put the identity repository or the log" >&2
  exit 1
fi
REAP_ROOT=${AICC_LEASE_REAP_ROOT:-${HOME:-}/aicc-preprod}
REPO=${AICC_LEASE_REAP_REPO:-$REAP_ROOT/lease-reaper-repo}
LOG=${AICC_LEASE_REAP_LOG:-$REAP_ROOT/lease_reap.log}
LEASE_TOOL=${VOYN_LEASE_TOOL:-voyn-lease}
export PGPASSFILE=${PGPASSFILE:-/run/voyn-aicc-worker/pgpass}
export VOYN_LEASE_DSN=${VOYN_LEASE_DSN:-"host=10.20.0.2 port=5432 dbname=voyn_control user=voyn_lease_client connect_timeout=5"}

# The identity repository is disposable by design, and `repo_replaceable`
# below is the exact rule for what this script will ever replace.
IDENTITY_BRANCH=lease-reaper
IDENTITY_MARKER=.aicc-lease-reaper-identity

if ! mkdir -p "$(dirname "$LOG")" 2>/dev/null; then
  echo "lease_reap: cannot create log directory $(dirname "$LOG")" >&2
  exit 1
fi

ts() { date -u +%FT%TZ; }
note() { echo "$(ts) $*" >>"$LOG"; }
fatal() { note "FATAL: $*"; exit 1; }

# A rebuild that dies half way must not leave its scratch tree behind for
# the next tick to trip over; $REPO itself is only ever the finished
# article (see the rename in `rebuild_identity_repo`).
SCRATCH=""
cleanup() { if [ -n "$SCRATCH" ]; then rm -rf "$SCRATCH"; fi; }
trap cleanup EXIT

# Usable means what `voyn-lease` actually requires of a cwd, checked
# against disk rather than assumed from a previous run: a work tree, HEAD
# a symbolic ref to a branch (not detached, not unborn), and that branch
# resolving to a real commit. Being inside SOME work tree is the whole
# requirement -- a $REPO an operator deliberately points at a subdirectory
# of a real clone still satisfies the tool, so this deliberately does not
# demand that $REPO be the top level.
repo_usable() {
  local dir=$1 branch
  if [ ! -d "$dir" ]; then return 1; fi
  if [ "$(git -C "$dir" rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]; then
    return 1
  fi
  if ! branch=$(git -C "$dir" symbolic-ref --quiet HEAD 2>/dev/null); then return 1; fi
  if [ -z "$branch" ]; then return 1; fi
  if ! git -C "$dir" rev-parse --verify --quiet "${branch}^{commit}" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

# Ours to replace: absent, empty, carrying the marker this script wrote, or
# holding nothing but a `.git` with no commits in it. That last case is the
# leftover the rejected revision could produce and an operator can produce by
# hand (`git init` and nothing else): there is no working file to lose and no
# commit to lose, and refusing it would strand the reaper permanently on
# exactly the state this task exists to recover from.
#
# Anything else is a directory someone meant something by. An unusable one is
# then an operator's problem to look at -- never this script's to delete. The
# rule is deliberately asymmetric: failing to reap delays recovery, whereas
# deleting a clone an operator pointed $REPO at (say, one mid-rebase, whose
# HEAD is legitimately detached) destroys work.
repo_replaceable() {
  local dir=$1 entries commits
  if [ ! -e "$dir" ]; then return 0; fi
  if [ ! -d "$dir" ]; then return 1; fi
  if [ -e "$dir/$IDENTITY_MARKER" ]; then return 0; fi
  entries=$(ls -A "$dir" 2>/dev/null || true)
  if [ -z "$entries" ]; then return 0; fi
  if [ "$entries" != ".git" ] || [ ! -d "$dir/.git" ]; then return 1; fi
  # An unreadable object store counts as no commits: the work tree is empty
  # either way, so nothing is lost, and a repository git itself cannot read
  # is never going to become a usable one.
  commits=$(git -C "$dir" rev-list --all --count 2>/dev/null || echo 0)
  if [ "$commits" = "0" ]; then return 0; fi
  return 1
}

# Every fallible step is guarded explicitly rather than left to `set -e`:
# the caller invokes this inside `if ! rebuild_identity_repo`, which turns
# errexit OFF for everything the function runs. Without these guards a
# failed `git commit` fell through to the rename below and published an
# unborn repository to $REPO -- the rejected revision's defect, reintroduced
# by the shape of the call site rather than by the initialization order.
rebuild_identity_repo() {
  local parent displaced=""
  parent=$(dirname "$REPO")
  mkdir -p "$parent" || return 1
  # Scratch trees from a run that was killed before its rename: bounded by
  # age so a concurrent tick's live scratch is never pulled out from under
  # it.
  find "$parent" -maxdepth 1 -name '.lease-reaper-init.*' -mmin +60 \
    -exec rm -rf {} + 2>/dev/null || true
  SCRATCH=$(mktemp -d "$parent/.lease-reaper-init.XXXXXX") || return 1
  # `git init -b` is git >= 2.28; the symbolic-ref form works on every
  # version this fleet has ever run and says the same thing.
  git -C "$SCRATCH" init -q || return 1
  git -C "$SCRATCH" symbolic-ref HEAD "refs/heads/$IDENTITY_BRANCH" || return 1
  git -C "$SCRATCH" config user.email "lease-reaper@voyn.invalid" || return 1
  git -C "$SCRATCH" config user.name "AICC lease reaper" || return 1
  git -C "$SCRATCH" config commit.gpgsign false || return 1
  cat >"$SCRATCH/$IDENTITY_MARKER" <<'MARKER' || return 1
Disposable identity repository for ops/lease_reap.sh
(VOYN-W0-AICC-REAPER-NOT-RUNNING).

`voyn-lease` resolves its working directory to a repository. This tree
exists only to be that working directory: it holds no work, is never
pushed, and is rebuilt from scratch by the next sweep whenever it stops
being a valid repository on a branch. Deleting it is always safe.
MARKER
  git -C "$SCRATCH" add "$IDENTITY_MARKER" || return 1
  git -C "$SCRATCH" commit -q -m "lease reaper identity repository" || return 1
  # The same predicate the next run will apply, asserted before anything is
  # published: a scratch tree that is not a valid repository never becomes
  # $REPO, so $REPO is never the place a half-built one appears.
  repo_usable "$SCRATCH" || return 1
  # The displaced tree is moved aside before the rename and deleted after
  # it, so neither step can leave $REPO missing or partial for longer than
  # one rename -- and a rename that fails leaves the old tree recoverable
  # rather than deleted.
  if [ -e "$REPO" ]; then
    displaced="$parent/.lease-reaper-replaced.$$"
    rm -rf "$displaced" || return 1
    mv "$REPO" "$displaced" || return 1
  fi
  if ! mv "$SCRATCH" "$REPO"; then
    if [ -n "$displaced" ]; then mv "$displaced" "$REPO" || true; fi
    return 1
  fi
  SCRATCH=""
  if [ -n "$displaced" ]; then rm -rf "$displaced"; fi
  return 0
}

ensure_identity_repo() {
  if repo_usable "$REPO"; then return 0; fi
  if [ -e "$REPO" ] && ! repo_replaceable "$REPO"; then
    fatal "identity repository $REPO is not a repository on a branch, and is neither empty, nor an empty unborn repository, nor marked by $IDENTITY_MARKER -- refusing to replace a directory this script does not own"
  fi
  if [ -e "$REPO" ]; then
    note "identity repository $REPO is invalid (no work tree, or HEAD detached/unborn) -- rebuilding"
  else
    note "identity repository $REPO is absent -- creating"
  fi
  if ! rebuild_identity_repo; then
    fatal "could not rebuild identity repository $REPO"
  fi
  if ! repo_usable "$REPO"; then
    fatal "identity repository $REPO is still unusable after a rebuild"
  fi
  note "identity repository $REPO ready on branch $IDENTITY_BRANCH"
}

if ! command -v git >/dev/null 2>&1; then
  fatal "git not found on PATH -- reap cannot run"
fi

if ! command -v jq >/dev/null 2>&1; then
  fatal "jq not found on PATH -- reap cannot run"
fi

# GNU `date -d` support check, up front and loud -- silently treating a
# `date` that doesn't support `-d` as "every expiry parses to epoch 0"
# is exactly the fail-dangerous default this script must not have.
if ! date -u -d "2026-01-01T00:00:00+00:00" +%s >/dev/null 2>&1; then
  fatal "this host's date does not support -d (GNU date required) -- reap cannot run"
fi

ensure_identity_repo
cd "$REPO" || fatal "cannot enter identity repository $REPO"

rows=$("$LEASE_TOOL" list 2>>"$LOG") || fatal "$LEASE_TOOL list failed"
if ! echo "$rows" | jq -e 'type == "array"' >/dev/null 2>&1; then
  fatal "$LEASE_TOOL list did not return a JSON array: ${rows:0:200}"
fi

now=$(date -u +%s)
count=$(echo "$rows" | jq 'length')
reaped=0
i=0
while [ "$i" -lt "$count" ]; do
  row=$(echo "$rows" | jq -c ".[$i]")
  i=$((i + 1))
  repo_id=$(echo "$row" | jq -r '.repository_id')
  expires=$(echo "$row" | jq -r '.expires_at')
  if ! exp_epoch=$(date -u -d "$expires" +%s 2>/dev/null); then
    # Fail closed: an unparseable expiry skips the row -- it must never
    # be treated as "already expired," which is what a numeric-default
    # fallback (e.g. `|| echo 0`) would do to every row on a date-format
    # regression.
    note "WARN: could not parse expires_at=$expires for repository_id=$repo_id -- skipping"
    continue
  fi
  if [ "$exp_epoch" -lt "$now" ]; then
    session="lease-reaper-$(date +%s)-$$"
    out=$("$LEASE_TOOL" acquire --repository "$repo_id" --owner lease-reaper \
      --session "$session" --task LEASE-REAPER --process-start 1 \
      --host "$(hostname)" --pid $$ --auto-takeover 2>&1) || {
      note "acquire failed for $repo_id: $out"
      continue
    }
    "$LEASE_TOOL" release --repository "$repo_id" --owner lease-reaper \
      --session "$session" --task LEASE-REAPER --process-start 1 \
      --host "$(hostname)" --pid $$ >>"$LOG" 2>&1 || true
    reaped=$((reaped + 1))
    note "reaped $repo_id (expires_at=$expires)"
  fi
done

# A sweep that reaps nothing is the normal case, so silence cannot be the
# signal that it ran. This line is what "the reaper is running" is read
# from -- by an operator tailing the log and by the unit's own journal.
note "OK: scanned $count row(s), reaped $reaped"
