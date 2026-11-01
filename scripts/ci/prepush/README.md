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
- Agents may run this worktree copy inside their own sandboxed run.
- `publish_run` (`command_center/orchestrator/publish.py`) does **not**
  execute this worktree copy: in that credentialed worker context the
  worktree is candidate content (verification finding on head `254154a`).
  The publish side instead:
  1. runs `_static_quality_gate` — ruff from the worker's trusted
     interpreter (tree as DATA only);
  2. runs the leak guard from the worker's trusted install;
  3. runs the impacted-test phase under the allowlisted `quality_band`
     profile of the privileged principal-isolation launcher
     (VOYN-W0-AICC-SANDBOX-PREPUSH-TESTS): fixed argv pointing at the
     root-owned `/usr/libexec/aicc-quality-band` (installed from
     `ops/aicc_quality_band.sh`), candidate worktree bind-mounted as
     `/workspace`, no model credentials, no network. A red sandbox run
     refuses the publish before the lease; broker/infra unavailability
     defers to CI via the exact principal-isolation transport envelope.

## What it is not

A gate. The required CI suite is unchanged and authoritative. The band can
only fail *sooner*, never *instead*: trigger-all selections (`mode=all`) and
hosts without a `.venv` defer to CI rather than blocking, and
`VOYN_QUALITY_BAND=off` bypasses it (printed, never silent).
`VOYN_QUALITY_BAND_BASE` pins the selection base for direct invocations;
`publish_run` neither runs this script nor sets any of its environment.
