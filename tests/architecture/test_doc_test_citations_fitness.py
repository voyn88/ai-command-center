"""VOYN-W0-AICC-DOC-STALE-TESTREF fitness gate: docs cite tests that exist.

``docs/aml/ACCEPTANCE_PACKAGE.md`` shipped a test-coverage table naming
``tests/test_phase1_alerts.py`` — a file that never existed in this repository
at any commit. The Phase 1 Alert Store suite has always been
``tests/test_alert_store.py``. A bank-facing acceptance package that points an
auditor at a nonexistent evidence file is worse than one that stays silent, so
the property is kept as a checked gate rather than a hope: every test file a
living document cites must resolve to a real file.

Two citation shapes are recognised, matching how the docs actually write them:

* **Rooted paths** — a backticked ``tests/...``-prefixed ``.py`` path must
  exist at exactly that path. This is the shape that broke.
* **Bare basenames** — a backticked ``test_*.py`` / ``*_test.py`` with no
  directory part must exist *somewhere* in the tree. Docs use this as
  shorthand (``see `test_git_info.py```) and pinning it to one directory would
  be false precision.

Point-in-time records are exempt (see ``ARCHIVAL_TREES``): an audit report or
daily log describes the tree as it stood when it was written, and rewriting
history to survive a later rename would defeat its purpose.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Point-in-time records: frozen accounts of a past state, not live docs.
ARCHIVAL_TREES = frozenset({"archive", "daily", "reports", "generated", "docs/audits"})

# Directories that are never part of the source tree.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# A backticked path/filename ending in .py.
_CITATION = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_\-./]*\.py)`")


def _is_test_basename(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py")


def _iter_source_files() -> list[Path]:
    found: list[Path] = []
    stack = [REPO_ROOT]
    while stack:
        current = stack.pop()
        for entry in current.iterdir():
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            else:
                found.append(entry)
    return found


def _is_archival(relative: Path) -> bool:
    posix = relative.as_posix()
    return any(posix == tree or posix.startswith(f"{tree}/") for tree in ARCHIVAL_TREES)


def test_docs_only_cite_test_files_that_exist() -> None:
    source_files = _iter_source_files()
    known_paths = {path.relative_to(REPO_ROOT).as_posix() for path in source_files}
    known_basenames = {path.name for path in source_files}

    broken: list[str] = []
    for path in sorted(source_files):
        if path.suffix != ".md":
            continue
        relative = path.relative_to(REPO_ROOT)
        if _is_archival(relative):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for citation in sorted(set(_CITATION.findall(text))):
            name = citation.rsplit("/", 1)[-1]
            if "/" in citation:
                # Only rooted test-suite paths are checked: a doc naming a
                # module path is a different claim, covered elsewhere.
                if not citation.startswith("tests/"):
                    continue
                if citation not in known_paths:
                    broken.append(f"{relative.as_posix()}: `{citation}` (no such file)")
            elif _is_test_basename(name) and name not in known_basenames:
                broken.append(
                    f"{relative.as_posix()}: `{citation}` (no file with this name)"
                )

    assert not broken, (
        "Documentation cites test files that do not exist:\n" + "\n".join(broken)
    )
