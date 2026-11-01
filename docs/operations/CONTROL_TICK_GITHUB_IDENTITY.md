# The control ticks' GitHub identity (VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS)

## The incident

On 2026-09-09, 21:15–22:10 UTC, every control tick on control-01 failed with

```
GraphQL: API rate limit already exceeded for user ID 297853521
```

`gh pr list` / `gh pr view` are GraphQL calls, GraphQL quota is **per user**,
and the ticks ran under the ambient credential in that host's
`~/.config/gh/hosts.yml` — one human's OAuth token (`dimastov-lab`), the same
one spent by that human's laptop tools. The PR-window tick could not label and
the review/merge ticks skipped every task until the human's hour-long window
reset. Nothing in the control plane had a quota of its own.

## What runs now

`command_center/orchestrator/gh_access.py` points every tick's `gh` at the
**fleet App's** installation token — `voyn-aicc-fleet`, the App already minted
for the isolated worker lanes — which carries its own REST and GraphQL budget:

* the token store is `/var/lib/aicc/github` (`gh/hosts.yml` + `expires_at`),
  written by `voyn-aicc-github-token.timer` every 30 minutes, `0640
  root:aicc-worker`;
* the tick units run as `User=aicc-worker`, so they can already read it;
* `GH_TOKEN` / `GITHUB_TOKEN` are cleared for the call, since either would
  override `GH_CONFIG_DIR` inside `gh` and silently keep spending the human's
  quota.

A host **without** the store (a laptop, CI, a control host whose App key was
never placed) falls back to the ambient credential and says so in the tick
report — nothing breaks, it is just back to the old quota.

The PR-window tick — the only loop that touches every open pull request — also
moved off GraphQL entirely: listing, per-PR details and label writes are REST
(`gh api repos/...`), details are cached per `(repo, PR, head sha)` for 30
minutes, and its timer went from 5 to 15 minutes.

## What an operator has to do on a control host

1. Place the App's private key, root-only, exactly as on a worker host:
   `/etc/voyn/secrets/aicc-github-app.pem` (0600 root:root).
2. `systemctl enable --now voyn-aicc-github-token.timer` — the unit and
   `/etc/aicc/github-app.env` are installed by the transaction on both
   profiles; only the key and the enablement are host state.
3. Confirm the store exists and is group-readable:
   `ls -l /var/lib/aicc/github/gh/hosts.yml` → `-rw-r----- root aicc-worker`.

Nothing else changes: the ticks discover the store themselves, so a host that
has not done this keeps running exactly as it did before.

## Verifying it

Every tick ends with one line:

```
QUOTA     identity=fleet-app calls=37 (rest=37 graphql=0) cache=12/24 core=4931/5000 graphql=5000/5000
```

* `identity=fleet-app` — the App's token. `identity=ambient reason=...` means
  it fell back, and the reason says why (`fleet_store_unreadable`,
  `fleet_token_expired`, `requested_by_env`).
* `rate_limited=N` — calls GitHub refused for rate limiting. This is the
  incident's own signal; it should be 0.
* `ambient_fallbacks=N` — calls the App was refused for (a scope it was not
  granted, or a repository it is not installed on) and that were retried on the
  ambient credential.

The budget half is read with `gh api rate_limit`, which is exempt from rate
limiting, so the measurement never costs what it measures.

## Escape hatches

* `AICC_GH_IDENTITY=ambient` — force the ambient credential (use when the App
  installation itself is the broken thing).
* `AICC_GH_FLEET_CONFIG_DIR` — a store somewhere other than the default.
* `AICC_GH_CACHE_DIR` / `AICC_GH_CACHE_TTL_SECONDS` — move or disable (TTL `0`)
  the PR-detail cache. The merge tick never reads it: an ACCEPT marker and a
  green rollup are always fetched fresh.
