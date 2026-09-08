"""NIGHT-W9-AICC-AUTHORITY: every data store is documented in the authority map.

A new file appearing under ``data/`` without a corresponding mention in
``docs/AUTHORITY_MAP.md`` fails this gate — a store cannot ship undocumented,
which is what keeps "one writer, one recovery source" a checked property
instead of a hope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = REPO_ROOT / "docs/AUTHORITY_MAP.md"

# Operator-local noise that is not a store: logs, locks the map covers by
# family, example templates, and macOS metadata.
_IGNORED_SUFFIXES = (".log", ".lock", ".stderr.log", ".stdout.log")
_IGNORED_NAMES = frozenset({".DS_Store", ".gitkeep"})
_IGNORED_PREFIXES = ("bench-",)

# SQLite WAL-mode sidecars: whether these exist is host/timing-dependent (a
# checkpoint can remove them at any moment), so they are not a store in their
# own right. Ignoring them is only safe when the database they belong to
# ("<name>.db-wal" -> "<name>.db") both exists alongside them and is itself
# documented -- otherwise an orphaned sidecar could be hiding undocumented,
# recoverable SQLite data with no `.db` file to vouch for it.
_DB_SIDECAR_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")


def _sidecar_base_name(name: str) -> str | None:
    for suffix in _DB_SIDECAR_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)] + ".db"
    return None


def _is_ignorable(path: Path, documented: str, sibling_names: frozenset[str]) -> bool:
    if path.name in _IGNORED_NAMES:
        return True
    if path.name.endswith(_IGNORED_SUFFIXES):
        return True
    if any(path.name.startswith(prefix) for prefix in _IGNORED_PREFIXES):
        return True
    if ".example." in path.name:
        return True
    base_name = _sidecar_base_name(path.name)
    if base_name is not None:
        return base_name in sibling_names and base_name in documented
    return False


def test_every_data_store_is_documented_in_the_authority_map():
    documented = MAP_PATH.read_text(encoding="utf-8")
    data_dir = REPO_ROOT / "data"
    entries = sorted(data_dir.iterdir())
    sibling_names = frozenset(entry.name for entry in entries)
    undocumented = [
        entry.name
        for entry in entries
        if not _is_ignorable(entry, documented, sibling_names)
        and f"{entry.name}" not in documented
    ]
    assert not undocumented, (
        "data stores missing from docs/AUTHORITY_MAP.md (add each with its "
        f"single writer and recovery source): {undocumented}"
    )


@pytest.mark.parametrize("suffix", _DB_SIDECAR_SUFFIXES)
def test_wal_sidecar_ignored_when_base_db_is_present_and_documented(suffix):
    name = f"runtime{suffix}"
    documented = "the store `runtime.db` is the execution source of truth"
    siblings = frozenset({"runtime.db", name})
    assert _is_ignorable(Path(name), documented, siblings)


@pytest.mark.parametrize("suffix", _DB_SIDECAR_SUFFIXES)
def test_wal_sidecar_not_ignored_when_base_db_is_undocumented(suffix):
    name = f"shadow{suffix}"
    documented = "no mention of the base store here"
    siblings = frozenset({"shadow.db", name})
    assert not _is_ignorable(Path(name), documented, siblings)


@pytest.mark.parametrize("suffix", _DB_SIDECAR_SUFFIXES)
def test_wal_sidecar_not_ignored_when_base_db_is_missing_from_disk(suffix):
    name = f"orphan{suffix}"
    documented = "the store `orphan.db` is fully documented"
    siblings = frozenset({name})  # orphan.db itself does not exist alongside it
    assert not _is_ignorable(Path(name), documented, siblings)


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
