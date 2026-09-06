"""NIGHT-W9-AICC-AUTHORITY: every data store is documented in the authority map.

A new file appearing under ``data/`` without a corresponding mention in
``docs/AUTHORITY_MAP.md`` fails this gate, and so does a new PostgreSQL table
or view (``command_center/db/roles.py``'s ``ALL_TABLES`` / ``ALL_VIEWS`` — the
server line's own schema inventory) that the map does not name. A store
cannot ship undocumented on either line, which is what keeps "one writer, one
recovery source" a checked property instead of a hope.
"""

from __future__ import annotations

import re
from pathlib import Path

from command_center.db import roles

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = REPO_ROOT / "docs/AUTHORITY_MAP.md"

# Operator-local noise that is not a store: logs, locks the map covers by
# family, example templates, and macOS metadata.
_IGNORED_SUFFIXES = (".log", ".lock", ".stderr.log", ".stdout.log")
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


def _mentioned(token: str, text: str) -> bool:
    """Whole-token containment, not a bare substring check.

    A plain ``token in text`` reports a false "documented" for any name that
    happens to be a substring of a longer documented one or of surrounding
    prose — ``role`` inside `` `roles` ``, ``audit`` inside
    ``daily_audit_events``. Word-boundary lookaround (treating ``.`` as part
    of the token, so ``runtime.db`` cannot be satisfied by a stray
    ``runtime.dbx``) closes that: a name only counts as mentioned when it
    appears as its own token, not as a fragment of one.
    """
    pattern = r"(?<![\w.])" + re.escape(token) + r"(?![\w.])"
    return re.search(pattern, text) is not None


def test_every_data_store_is_documented_in_the_authority_map():
    documented = MAP_PATH.read_text(encoding="utf-8")
    data_dir = REPO_ROOT / "data"
    undocumented = [
        entry.name
        for entry in sorted(data_dir.iterdir())
        if not _is_ignorable(entry) and not _mentioned(entry.name, documented)
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
        assert _mentioned(store, documented) and _mentioned(writer, documented), (
            f"{store} must be documented with writer {writer}"
        )


def test_every_postgres_table_and_view_is_documented_in_the_authority_map():
    """The server line's own schema inventory must all appear in the map.

    ``roles.ALL_TABLES`` / ``roles.ALL_VIEWS`` is not a sample — it is every
    table and view the migrations create, drawn from the same module the
    grant matrix and ``tests/db/test_role_privileges.py`` hold the live
    database to. A table added there without a matching mention here would
    let a duplicate-source-of-truth audit run against half the system and
    call it complete, which is the defect this test exists to close.
    """
    documented = MAP_PATH.read_text(encoding="utf-8")
    undocumented = [
        name
        for name in (*roles.ALL_TABLES, *roles.ALL_VIEWS)
        if not _mentioned(name, documented)
    ]
    assert not undocumented, (
        "PostgreSQL tables/views missing from docs/AUTHORITY_MAP.md (name "
        f"each with its authority, writer and recovery source): {undocumented}"
    )
