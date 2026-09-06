# Pre-push quality band (`scripts/ci/prepush/`)

`quality_band.sh` runs the cheap end of the CI feedback loop locally, before a
push spends a full CI round-trip: `scripts/preflight.sh` (whitespace, ruff,
byte-compile) followed by the **impacted tests** chosen by
`scripts/ci/test_impact/select_tests.py`, executed in the same two phases as
the CI advisory job (xdist for `-m "not serial"`, serial tail without xdist).

It exists because red PR CI runs are dominated by pytest failures a local
impacted run would have caught (measured 2026-08-26: 22% of recent PR CI runs
red, failures almost entirely in the pytest shard jobs), and every red run
costs an agent a full diagnose → fix → new SHA → CI → review cycle.

## Where it runs — and the trust boundary

- Interactive writers: `make prepush` (the full band, including the
  impacted-test phases — a human running their own code).
- Agents may run it inside their own sandboxed run (the script is in their
  worktree); nothing on the publish path enforces that today.
- `publish_run` (`command_center/orchestrator/publish.py`) does **not**
  execute this script itself: in that credentialed worker context the
  worktree is candidate content, and executing it directly there would be
  candidate-controlled host command execution (verification finding on head
  `254154a`). It enforces `_static_quality_gate` first — ruff (parse + lint,
  catches syntax errors) from the worker's own trusted interpreter with
  explicit argv and a minimal explicit env, treating the tree strictly as
  data.
- The impacted-test phase (VOYN-W0-AICC-SANDBOX-PREPUSH-TESTS) runs under the
  isolated `aicc-agent` principal instead of the credentialed worker:
  `publish_run` calls `_quality_band_isolated_gate`, which hands a fixed
  manifest to the root broker's allowlisted `quality_band` profile
  (`ops/aicc_agent_launcher.py`) over its Unix socket
  (`agent_runner.run_quality_band_gate`). The broker runs its own trusted,
  deployed copy of *this exact file*
  (`/usr/libexec/aicc-agent-quality-band`, installed by
  `ops/aicc_install_transaction.py`'s `default_specs`) — never the candidate
  worktree's copy — inside its own unprivileged transient unit, with no
  network and no model credential of any kind
  (`NO_MODEL_AUTH_EXECUTORS`/`RestrictAddressFamilies=AF_UNIX`). Either gate
  refuses the publish with `reason=quality_band_failed: …` before the lease
  is acquired.

## What it is not

A gate. The required CI suite is unchanged and authoritative. The band can
only fail *sooner*, never *instead*: trigger-all selections (`mode=all`) and
hosts without a `.venv` defer to CI rather than blocking, and
`VOYN_QUALITY_BAND=off` bypasses both the static and the isolated gate
(printed, never silent). `VOYN_QUALITY_BAND_BASE` pins the selection base for
direct invocations. The isolated gate also fails open — proceeding as if
clean, never widening what it refuses — whenever principal isolation or its
`quality_band` profile is not deployed on this host, or the broker itself
fails for an infrastructure reason rather than a candidate-tree finding: this
band can only fail a publish sooner than CI, never replace it.
