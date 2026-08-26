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
# VOYN-W0-AICC-REAPER-NOT-RUNNING: found live on worker-01 -- this reaper had
# refused with `invalid branch` on EVERY tick since 2026-08-23T23:55Z (2.5+
# days, 271 consecutive failures, zero successful reaps in that window).
# `voyn-lease acquire` derives its identity's `branch` field by running `git
# branch --show-current` in `--repo` (defaulting to cwd), and refuses an empty
# result. The shared preprod checkout this script used to `cd` into
# (`aicc-preprod/repo`) had been left in a detached-HEAD state by an unrelated
# operation, where `branch --show-current` prints nothing -- and stayed
# detached indefinitely, since nothing that mutates that checkout (including
# the self-deploy tick) re-attaches it to a branch.
#
# This reaper's identity is synthetic (owner=lease-reaper, task=LEASE-REAPER)
# and has no real branch, worktree or HEAD to report in the first place -- it
# should never depend on the live, shared, concurrently-mutated checkout
# another process happens to be using. It now runs against its own dedicated,
# throwaway git repo instead, created idempotently on first use and never
# touched by anything else, so no other process's checkout state can ever
# break it again.
set -euo pipefail
export PGPASSFILE=/run/voyn-aicc-worker/pgpass
export VOYN_LEASE_DSN="host=10.20.0.2 port=5432 dbname=voyn_control user=voyn_lease_client connect_timeout=5"
# Overridable so a test can point both at a scratch directory without
# touching the real production paths.
IDENTITY_REPO="${IDENTITY_REPO:-/home/voynadmin/aicc-preprod/lease-reap-identity}"
LOG="${LOG:-/home/voynadmin/aicc-preprod/lease_reap.log}"

ts() { date -u +%FT%TZ; }

if [ ! -d "$IDENTITY_REPO/.git" ]; then
  mkdir -p "$IDENTITY_REPO"
  git -C "$IDENTITY_REPO" init -q -b lease-reaper
  git -c user.name=lease-reaper -c user.email=lease-reaper@localhost \
      -C "$IDENTITY_REPO" commit -q --allow-empty -m "lease-reaper identity anchor"
fi
cd "$IDENTITY_REPO"

if ! command -v jq >/dev/null 2>&1; then
  echo "$(ts) FATAL: jq not found on PATH -- reap cannot run" >>"$LOG"
  exit 1
fi

# GNU `date -d` support check, up front and loud -- silently treating a
# `date` that doesn't support `-d` as "every expiry parses to epoch 0"
# is exactly the fail-dangerous default this script must not have.
if ! date -u -d "2026-01-01T00:00:00+00:00" +%s >/dev/null 2>&1; then
  echo "$(ts) FATAL: this host's date does not support -d (GNU date required) -- reap cannot run" >>"$LOG"
  exit 1
fi

rows=$(voyn-lease list 2>>"$LOG")
if ! echo "$rows" | jq -e 'type == "array"' >/dev/null 2>&1; then
  echo "$(ts) FATAL: voyn-lease list did not return a JSON array: ${rows:0:200}" >>"$LOG"
  exit 1
fi

now=$(date -u +%s)
count=$(echo "$rows" | jq 'length')
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
    echo "$(ts) WARN: could not parse expires_at=$expires for repository_id=$repo_id -- skipping" >>"$LOG"
    continue
  fi
  if [ "$exp_epoch" -lt "$now" ]; then
    session="lease-reaper-$(date +%s)-$$"
    out=$(voyn-lease acquire --repository "$repo_id" --owner lease-reaper \
      --session "$session" --task LEASE-REAPER --process-start 1 \
      --host "$(hostname)" --pid $$ --auto-takeover 2>&1) || {
      echo "$(ts) acquire failed for $repo_id: $out" >>"$LOG"
      continue
    }
    voyn-lease release --repository "$repo_id" --owner lease-reaper \
      --session "$session" --task LEASE-REAPER --process-start 1 \
      --host "$(hostname)" --pid $$ >>"$LOG" 2>&1 || true
    echo "$(ts) reaped $repo_id (expires_at=$expires)" >>"$LOG"
  fi
done
