# Test-impact analysis (`scripts/ci/test_impact/`)

A dependency-based **test selector** used by CI to run a *fast, advisory*
pre-check on pull requests. It answers one question:

> Given the files changed in this PR, which test files could possibly be
> affected?

It is a **speed optimisation only**. The full `pytest` suite still runs as the
required merge gate (`quality-gates` in `.github/workflows/ci.yml`), so the
impact job can never cause coverage loss — if it selects too few tests, the
full gate catches whatever it missed. It is wired as a *separate, advisory* job,
never as the sole gate.

## How the mapping is built

`select_tests.py` builds a static Python **import graph** on every invocation —
there is no committed cache to keep in sync:

1. Every `*.py` under `command_center/`, `tests/`, and the top-level `app.py` is
   parsed with the standard-library `ast` module. **No code is imported or
   executed.**
2. Each file is mapped to its dotted module name
   (`command_center/agent_runner.py` → `command_center.agent_runner`).
3. `import` / `from … import …` statements (including relative imports) are
   resolved against that module table to record "file X depends on first-party
   module Y". Third-party and stdlib imports are ignored.
4. The graph is inverted into a **reverse-dependency map** (module → everything
   that transitively imports it). From each changed source file we walk that map
   to collect every test file that reaches it.

Because the graph is derived from the current tree on each run, it updates
itself automatically — adding, deleting, or re-wiring a module changes the
selection on the next run with no manual maintenance step.

## Selection rules

| Changed file                                   | Result                          |
| ---------------------------------------------- | ------------------------------- |
| A test file (`tests/test_*.py`)                | selects itself                  |
| A source module                                | selects all tests that transitively import it |
| `conftest.py`, `pyproject.toml`, a requirements lock, anything under `scripts/ci/test_impact/` | **trigger-all** → run full suite |
| A first-party `*.py` not yet in the graph      | **trigger-all** (ambiguous → widen) |
| A non-Python file (docs, `web/`, workflows)    | selects nothing (full gate still runs) |

The guiding principle: **ambiguity always widens the selection, never narrows
it.**

## Usage

```bash
# Diff against origin/main (CI default)
python scripts/ci/test_impact/select_tests.py

# Diff against an explicit base
python scripts/ci/test_impact/select_tests.py --base HEAD~1

# Score an explicit file list (no git needed)
python scripts/ci/test_impact/select_tests.py --files command_center/aml_store.py

# Emit pytest-ready path args (prints `tests` when trigger-all)
python scripts/ci/test_impact/select_tests.py --format pytest --output selected.txt
```

* stdout: one selected test file per line, or the token `ALL` (list format) /
  `tests` (pytest format) when the full suite must run. Empty output means "no
  first-party code affected".
* stderr: a one-line human summary.
* exit code: always `0` on success.

## Tests

`test_select_tests.py` covers the graph logic on a synthetic module tree plus a
smoke test against the real repository. It runs as part of the normal suite.
