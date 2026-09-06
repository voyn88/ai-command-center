"""NIGHT-W9-AICC-AUTHORITY: every data store is documented in the authority map.

A new file appearing under ``data/`` without a corresponding mention in
``docs/AUTHORITY_MAP.md`` fails this gate — a store cannot ship undocumented,
which is what keeps "one writer, one recovery source" a checked property
instead of a hope. The same is true of the server line: a PostgreSQL table
declared in ``command_center/db/roles.ALL_TABLES`` without a mention here
fails the gate too — this map is one authority inventory across both
databases, not just the desktop/SQLite one (VOYN-W0-AICC-AUTHORITY-MAP-STALE).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = REPO_ROOT / "docs/AUTHORITY_MAP.md"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from command_center.db import roles  # noqa: E402

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


def test_every_postgresql_table_is_documented_in_the_authority_map():
    documented = MAP_PATH.read_text(encoding="utf-8")
    undocumented = [
        table for table in roles.ALL_TABLES if table not in documented
    ]
    assert not undocumented, (
        "PostgreSQL tables missing from docs/AUTHORITY_MAP.md (add each with "
        f"its authority/writer, or mark it a mirror if SRV-01b covers it): "
        f"{undocumented}"
    )
