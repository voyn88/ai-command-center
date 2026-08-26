# Local developer shortcuts. CI does not use this file.
# Uses .venv/bin/python directly (not `uv run`) so uv.lock is never touched
# by a plain test run.

PY := .venv/bin/python

.PHONY: preflight test test-fast test-impacted test-impacted-seed

## Fast pre-push checks: whitespace, ruff, byte-compile (mirrors the fast part
## of the CI Quality gates job).
preflight:
	./scripts/preflight.sh

## Full test suite, serial.
test:
	$(PY) -m pytest -q

## Full test suite in parallel with pytest-xdist (local speedup only; CI is
## unchanged). Tests that cannot run concurrently are marked `serial` and run
## in a second, single-process phase.
test-fast:
	$(PY) -m pytest -q -n 8 -m "not serial"
	$(PY) -m pytest -q -m serial

## One-time (or after a large rebase/dependency bump): build the coverage-
## derived test<->source map `test-impacted` reads. Slower than a normal run
## (coverage instrumentation on every test) -- expected only occasionally,
## not on every iteration.
test-impacted-seed:
	$(PY) -m pytest -q --testmon

## The local dev-loop accelerator: re-runs only the tests whose covered
## source actually changed since the last `--testmon` run (seed with
## `test-impacted-seed` first). Local-only, like `test-fast` -- CI's
## required gate always runs the full suite regardless of this cache.
## `--testmon` needs the serial runner (not compatible with `-n`/xdist).
test-impacted:
	$(PY) -m pytest -q --testmon
