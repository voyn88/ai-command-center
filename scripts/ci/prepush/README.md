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
  execute this script: in that credentialed worker context the worktree is
  candidate content, and executing it would be candidate-controlled host
  command execution (verification finding on head `254154a`). The publish
  side enforces only `_static_quality_gate` — ruff (parse + lint, catches
  syntax errors) from the worker's own trusted interpreter with explicit
  argv and a minimal explicit env, treating the tree strictly as data. A red
  gate refuses the publish with `reason=quality_band_failed: …` before the
  lease is acquired. Enforced pre-push impacted tests for the agent path
  require an allowlisted profile in the privileged principal-isolation
  launcher — that is VOYN-W0-AICC-SANDBOX-PREPUSH-TESTS, a separate,
  security-designed task; until it lands, the impacted-test half of the
  red-rate reduction is delivered by CI (authoritative) and interactive
  `make prepush` only.

## What it is not

A gate. The required CI suite is unchanged and authoritative. The band can
only fail *sooner*, never *instead*: trigger-all selections (`mode=all`) and
hosts without a `.venv` defer to CI rather than blocking, and
`VOYN_QUALITY_BAND=off` bypasses it (printed, never silent).
`VOYN_QUALITY_BAND_BASE` pins the selection base for direct invocations;
`publish_run` neither runs this script nor sets any of its environment.
