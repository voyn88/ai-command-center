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

## Where it runs

- `publish_run` (`command_center/orchestrator/publish.py`) invokes it in the
  worktree before acquiring the push lease. A failing band refuses the publish
  with `reason=quality_band_failed: …`, so agents get the verdict in about a
  minute instead of a CI round-trip later.
- Interactive writers: `make prepush`.

## What it is not

A gate. The required CI suite is unchanged and authoritative. The band can
only fail *sooner*, never *instead*: trigger-all selections (`mode=all`) and
hosts without a `.venv` defer to CI rather than blocking, and
`VOYN_QUALITY_BAND=off` bypasses it (printed, never silent).
`VOYN_QUALITY_BAND_BASE` pins the selection base; `publish_run` sets it to its
pinned base SHA so selection matches the exact publish diff.
