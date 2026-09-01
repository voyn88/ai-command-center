"""NIGHT-W9-AICC-AUTHORITY: every data store is documented in the authority map.

A new file appearing under ``data/`` without a corresponding mention in
``docs/AUTHORITY_MAP.md`` fails this gate — a store cannot ship undocumented,
which is what keeps "one writer, one recovery source" a checked property
instead of a hope.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = REPO_ROOT / "docs/AUTHORITY_MAP.md"

# Operator-local noise that is not a store: logs, locks the map covers by
# family, example templates, macOS metadata, and SQLite's own WAL-mode
# sidecar files. The sidecars are mechanical artifacts of whichever `.db`
# file is already documented, not stores of their own, but they only exist
# once a developer has actually run the app locally — a fresh checkout never
# has them. Without ignoring them, this test passed or failed depending on
# which machine ran it rather than on what the tree actually ships, exactly
# the class of bug `docs/AUTHORITY_MAP.md` exists to make checkable.
_IGNORED_SUFFIXES = (
    ".log",
    ".lock",
    ".stderr.log",
    ".stdout.log",
    ".db-wal",
    ".db-shm",
    ".db-journal",
)
_IGNORED_NAMES = frozenset({".DS_Store", ".gitkeep"})
_IGNORED_PREFIXES = ("bench-",)


def _is_ignorable(path: Path) -> bool:
    if path.name in _IGNORED_NAMES:
        return True
    if path.name.endswith(_IGNORED_SUFFIXES):
        return True
    if any(path.name.startswith(prefix) for prefix in _IGNORED_PREFIXES):
        return True
    return ".example." in path.name


def test_sqlite_wal_sidecars_are_not_treated_as_undocumented_stores(tmp_path):
    """A developer who has actually run the app has `runtime.db-wal`/`-shm`
    on disk; a fresh checkout never does. These are mechanical WAL-mode
    sidecars of the already-documented `runtime.db`, not stores of their
    own, and must not make this gate's verdict depend on which machine
    happens to run it — the exact failure reproduced by creating them here
    before this test existed.
    """
    for name in ("runtime.db-wal", "runtime.db-shm", "runtime.db-journal"):
        assert _is_ignorable(tmp_path / name), name


def test_every_data_store_is_documented_in_the_authority_map():
    documented = MAP_PATH.read_text(encoding="utf-8")
    data_dir = REPO_ROOT / "data"
    undocumented = [
        entry.name
        for entry in sorted(data_dir.iterdir())
        if not _is_ignorable(entry) and f"{entry.name}" not in documented
    ]
    assert not undocumented, (
        "data stores missing from docs/AUTHORITY_MAP.md (add each with its "
        f"single writer and recovery source): {undocumented}"
    )


def test_map_names_a_single_writer_for_every_json_store():
    documented = MAP_PATH.read_text(encoding="utf-8")
    for store, writer in {
        "tasks.json": "tasks_repository.py",
        "execution_queue.json": "execution_queue.py",
        "pipeline_settings.json": "pipeline_settings.py",
        "project_config.json": "project_config.py",
        "runtime.db": "runtime/db.py",
    }.items():
        assert store in documented and writer in documented, (
            f"{store} must be documented with writer {writer}"
        )
