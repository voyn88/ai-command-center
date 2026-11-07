"""NIGHT-W9-AICC-AUTHORITY: every data store is documented in the authority map.

A new file appearing under ``data/`` without a corresponding mention in
``docs/AUTHORITY_MAP.md`` fails this gate — a store cannot ship undocumented,
which is what keeps "one writer, one recovery source" a checked property
instead of a hope. The same gate covers the server line's PostgreSQL schema:
a table or view in ``command_center/db/roles.ALL_TABLES`` / ``ALL_VIEWS`` with
no mention in the map fails too, closing the blind spot from
VOYN-W0-AICC-AUTHORITY-MAP-STALE — the map used to describe only what lives
under ``data/``, so a PostgreSQL-only store never tripped it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from command_center.db import roles

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


def _mentioned(token: str, text: str) -> bool:
    """True if `token` appears in `text` as a whole word/identifier, not a
    substring of a longer one.

    A plain substring check (``token in text``) false-passes an undocumented
    name that happens to sit inside a documented one or its surrounding prose
    — e.g. ``role`` inside ``roles``, or ``audit`` inside
    ``daily_audit_events`` — which would let an actually-undocumented
    PostgreSQL table slip through this gate by accident of spelling.

    The boundary is "not adjacent to another word character" (``\\w``, i.e.
    letters/digits/underscore), deliberately *not* "not adjacent to any
    punctuation": excluding e.g. ``.`` from the allowed boundary as well would
    reject real mentions — a sentence ending right after the token
    (``...backed by runtime.db.``) or a schema-qualified name
    (``public.roles``) — which is a wrong-boundary bug in its own right, not
    just an over-strict one, since it makes the gate fail on documentation
    that is actually complete.
    """
    pattern = r"(?<!\w)" + re.escape(token) + r"(?!\w)"
    return re.search(pattern, text) is not None


def test_every_data_store_is_documented_in_the_authority_map():
    documented = MAP_PATH.read_text(encoding="utf-8")
    data_dir = REPO_ROOT / "data"
    entries = sorted(data_dir.iterdir())
    sibling_names = frozenset(entry.name for entry in entries)
    undocumented = [
        entry.name
        for entry in entries
        if not _is_ignorable(entry, documented, sibling_names)
        and not _mentioned(entry.name, documented)
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
        assert _mentioned(store, documented) and _mentioned(writer, documented), (
            f"{store} must be documented with writer {writer}"
        )


def test_every_postgres_table_and_view_is_documented_in_the_authority_map():
    """The server line's PostgreSQL schema gets the same completeness gate as
    ``data/`` — ``roles.ALL_TABLES``/``ALL_VIEWS`` is the same inventory the
    grant matrix and the live-catalog checker in
    ``tests/db/test_grant_compliance.py`` hold the database to, so a table or
    view absent here is a store this map does not name.
    """
    documented = MAP_PATH.read_text(encoding="utf-8")
    undocumented = [
        name
        for name in (*roles.ALL_TABLES, *roles.ALL_VIEWS)
        if not _mentioned(name, documented)
    ]
    assert not undocumented, (
        "PostgreSQL tables/views missing from docs/AUTHORITY_MAP.md (add each "
        f"with its single writer and recovery source): {undocumented}"
    )


def test_map_documents_the_sqlite_to_postgresql_seam():
    """VOYN-W0-AICC-SRV-01b is the follow-up slice that moves the runtime
    store off SQLite onto PostgreSQL; until it lands, ``runtime.db`` is the
    authority. That target state must stay written down, not just true in
    ``command_center/db/__init__.py``'s docstring — otherwise the mirror
    tables in the PostgreSQL section above read as an undocumented second
    authority instead of a documented, unfinished seam.
    """
    documented = MAP_PATH.read_text(encoding="utf-8")
    assert _mentioned("VOYN-W0-AICC-SRV-01b", documented)
    assert _mentioned("command_center/db/__init__.py", documented)
