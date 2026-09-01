# Daily self-audit

The headless daily self-audit is an opt-in product and engineering campaign.
It is not a timer around `pytest`: every campaign must exercise the user path
from task creation/import through execution, remediation, review, CI, merge,
target-branch verification and final task projection.

## Mandatory coverage

- implementation, architecture, security, reliability and concurrency;
- real UI/user journey, usability, accessibility and recovery feedback;
- failure paths including invalid/dirty workspaces, concurrency, timeout,
  malformed output, failed checks, network errors, review rejection, conflicts
  and process restart;
- automated remediation with regression coverage and independent re-review;
- queue waves, dependencies, capacity, fairness and isolated failures;
- evidence-based task reprioritization;
- local quality gates, GitHub checks/review/mergeability and verified target
  branch state.

A campaign is never successful merely because an agent process exits zero or a
PR is merged. The persisted result is `completed` only after the target branch
is verified. Failures remain visible as `failed` or `requires_attention`.

## Enabling

The service is off by default. A persistent host must set:

```text
AICC_DAILY_AUDIT_ENABLED=1
AICC_COMPLETION_AUTOPILOT=1
AICC_DATA_DIR=/absolute/path/to/the/app/data
```

Optional bounds are `AICC_DAILY_AUDIT_MAX_REMEDIATION_ROUNDS` (1–10,
default 5), `AICC_DAILY_AUDIT_RUN_TIMEOUT_SECONDS` (30–3600, default 3600)
and `AICC_DAILY_AUDIT_COMPLETION_TIMEOUT_SECONDS` (60–43200, default 21600).
The route is pinned by `AICC_DAILY_AUDIT_PROVIDER_ID` (default `claude_code`).
Git transport retries only recognized transient failures, at most
`AICC_DAILY_AUDIT_TRANSPORT_RETRY_ATTEMPTS` times (1–5, default 3), with a
bounded exponential delay from `AICC_DAILY_AUDIT_TRANSPORT_RETRY_BASE_SECONDS`.
After `AICC_DAILY_AUDIT_MAX_CONSECUTIVE_FAILURES` failed campaigns (default 3),
the scheduler opens its circuit and stops dispatching until an explicit reset.
Git operations and each validation command also have fixed deadlines (120 and
900 seconds by default). A timed-out agent run is explicitly cancelled and a
timed-out completion remains recoverable instead of being reported as done.

Run one scheduler tick:

```text
python scripts/daily_audit_daemon.py --once
```

Inspect persisted scheduling state:

```text
python scripts/daily_audit_daemon.py --status
```

Run the separate, non-dispatching provider/network acceptance and re-arm an
idle circuit for the next normal interval:

```text
python scripts/daily_audit_daemon.py --preflight
python scripts/daily_audit_daemon.py --reset-circuit
```

`deploy/com.ai-command-center.daily-audit.plist` is a launchd template. Replace
`__ROOT__`, `__PYTHON__`, `__PATH__` and `__DATA_DIR__` with absolute paths
before installing it. `AICC_DATA_DIR` must be the same directory used by the
Streamlit application; otherwise campaigns run correctly but cannot appear in
the application UI. The
process is kept alive, while the SQLite due time and lease ensure that only one
campaign is dispatched per day and that another host cannot duplicate it.

### Install as a system daemon, not a GUI agent

The same plist must be bootstrapped into the **system** launchd domain, not a
per-user `gui/<uid>` domain. A `gui/<uid>` LaunchAgent only runs while that
user is logged into a graphical session -- on a headless host, or a laptop
that is closed or logged out, the campaign silently stops being scheduled.
The system domain has no such dependency:

```text
sudo cp com.ai-command-center.daily-audit.plist /Library/LaunchDaemons/com.ai-command-center.daily-audit.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.ai-command-center.daily-audit.plist
sudo launchctl enable system/com.ai-command-center.daily-audit
```

Check it with `launchctl print system/com.ai-command-center.daily-audit`, or
from the Streamlit panel, which probes the system domain directly.

### Migrating from a GUI-domain LaunchAgent

Installations from before this change used a `gui/<uid>` LaunchAgent. That
agent does **not** stop on its own when the system daemon above is installed:
both can end up loaded at once, each independently dispatching the daily
campaign against the same lease -- unlikely to duplicate work outright, since
the lease still serializes dispatch, but two live schedulers is one more
failure mode than zero, and it defeats the point of moving to the system
domain. Before or immediately after installing the system daemon, remove the
old agent:

```text
launchctl bootout gui/$(id -u)/com.ai-command-center.daily-audit
rm -f ~/Library/LaunchAgents/com.ai-command-center.daily-audit.plist
```

Note the target form: `gui/$(id -u)/com.ai-command-center.daily-audit` is one
argument -- domain and service label joined by `/` -- not two separate
arguments. `launchctl bootout gui/$(id -u) com.ai-command-center.daily-audit`
(a space instead of the slash) is parsed as removing a *plist file* named
`com.ai-command-center.daily-audit` from the `gui/$(id -u)` domain, which is
not what is loaded, and the legacy agent is left running.

The Streamlit panel reports a legacy agent it detects as loaded (running or
not) with the exact `bootout` command to run. Coverage of other logged-in
accounts on the same host is best-effort: a normal user's launchd session
cannot inspect another user's, so a scope this process could not verify is
reported as unverified, not as clear. Do not read "no warning shown" as proof
that no legacy agent remains under an account other than the one running the
UI; check other accounts directly if this host is shared.

## Safety and recovery contract

- The active scheduler renews its lease throughout the campaign. A replacement
  may claim an expired lease, and the abandoned campaign is terminalized as
  `interrupted`; a late owner cannot overwrite the replacement's result. The
  backend rechecks ownership before publication/completion side effects and
  cancels an active agent run as soon as lease loss is observed. Heartbeats
  extend leases monotonically and cannot shorten a longer publication-action
  fence.
- `SIGTERM` and `SIGINT` request an orderly service stop. The scheduler wakes
  immediately, stops heartbeating, cancels an active agent run, and terminates
  a running validation process group with bounded TERM/KILL escalation. The
  campaign is persisted as `interrupted` and remains due for restart recovery.
- Terminal agent state handling includes `INTERRUPTED`, `UNKNOWN` and future
  unknown states. Result events are paginated, so the final result is not lost
  after the first 1000 events.
- Transient Git transport failures receive a bounded in-operation retry.
  Campaign failures back off from one hour to one day, retain provider reset
  timestamps, and open a persistent circuit after the configured limit. A
  manual preflight and reset are required before scheduling resumes.
- Validation runs before the final gate. The gate receives the audit and
  remediation evidence, full review diff, validation evidence and exact SHA-256
  digests. It must return structured findings and evidence; missing evidence,
  digest drift, contradictory verdicts, or any Blocker/High/Medium finding
  fails closed.
- A clean, independently approved no-change audit is a successful campaign.
  A change campaign can stage only the exact reviewed manifest. Runtime output,
  generated, binary, oversized, sensitive and unexpected files are rejected;
  staged and committed blob hashes and modes are revalidated, and publication
  never uses `git add --all`.
- Completion advancement is targeted to the campaign's run and uses the same
  concurrency, transient Git/GitHub retry and merge-slot envelope as the global
  poller. The campaign/owner fence is persisted in completion policy, so the
  global poller enforces it too. Immediately before commit, push, PR creation,
  recovery or merge it atomically renews the same lease beyond the bounded
  action timeout; if renewal fails, completion is cancelled into attention
  without the publication side effect. A transient completion race cannot
  restart the whole audit campaign.
- Clean completed, failed and attention worktrees are removed. Dirty worktrees
  and worktrees with an active completion timeout are retained for investigation
  or recovery.

The agent prompt, blocked commands and removal of environment-provided VCS
tokens are defence in depth. They do not provide credential-level isolation
from credentials already cached in a user keychain or credential helper. A
headless deployment that requires a hard publication boundary must run agents
under an identity without such credentials and grant publication credentials
only to the completion service.
