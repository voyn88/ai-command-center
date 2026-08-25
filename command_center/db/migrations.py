"""Forward/backward migrations for the AICC server database.

The *schema* is AICC's: the tables declared by the migration set, their indexes,
the `aicc_*` grants that go with them, and the plain SQL files in `sql/` named
`NNNN_slug.up.sql` with a matching `NNNN_slug.down.sql`. AIOS Core knows none of
that and must not. The table count is deliberately derived from those files by
the correspondence and mirror-coverage gates, never maintained here by hand.

The *running* of those files is not AICC's. Serializing two migrators in a
rolling deploy behind an advisory lock, committing each migration together with
its ledger row so an interrupted run leaves either the old schema or the new
one, verifying that an applied migration's file has not been edited since,
refusing a database migrated by a newer deploy — none of that is specific to
these tables, and every consumer of PostgreSQL needs exactly it. It lives in
`aios-db` (VOYN-W0-AIOS-DB-01) and is reached through
`command_center.db.adapter`.

This module is therefore what is left once the generic half is gone: where the
SQL lives, what the ledger is called, and the module-level functions the CLI,
the readiness probe and the tests already call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from command_center.db import adapter

__all__ = [
    "LEDGER_TABLE",
    "LOCK_NAMESPACE",
    "Migration",
    "MigrationError",
    "applied_versions",
    "current_version",
    "discover",
    "downgrade",
    "ensure_ledger",
    "runner",
    "upgrade",
]

_LOG = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"

#: The ledger table name is part of this database's shape, not the library's.
LEDGER_TABLE = "schema_migration"

#: Advisory-lock namespace for migration runs. Named rather than a hand-picked
#: integer: advisory locks share one flat key space per database, so a constant
#: copied into a second subsystem would silently serialise the two against each
#: other. `aios_db.lock_key` derives the 64-bit key from this string.
LOCK_NAMESPACE = "aicc:schema-migration"

# Re-exported so callers keep catching `migrations.MigrationError` and
# annotating `migrations.Migration` as they did before the split.
Migration = adapter.Migration
MigrationError = adapter.MigrationError


def runner(sql_dir: Path | None = None) -> adapter.MigrationRunner:
    """The migration runner for this database's SQL directory."""
    return adapter.MigrationRunner(
        SQL_DIR if sql_dir is None else sql_dir,
        ledger_table=LEDGER_TABLE,
        lock_namespace=LOCK_NAMESPACE,
        logger=_LOG,
    )


def discover(sql_dir: Path | None = None) -> tuple[Migration, ...]:
    """Load the migration set, rejecting gaps, duplicates and missing downgrades."""
    return adapter.discover(SQL_DIR if sql_dir is None else sql_dir)


def ensure_ledger(conn) -> None:
    """Create `schema_migration` if absent. Requires DDL rights (migrator role)."""
    runner().ensure_ledger(conn)


def applied_versions(conn) -> tuple[int, ...]:
    """Versions recorded as applied, oldest first.

    Read-only on purpose: the readiness probe runs this as `aicc_app`, which
    has no DDL rights, so creating the ledger here would turn a health check
    into a permission error.
    """
    return runner().applied_versions(conn)


def current_version(conn) -> int:
    """Highest applied version, or 0 on a fresh database."""
    return runner().current_version(conn)


def upgrade(conn, *, target: int | None = None, sql_dir: Path | None = None) -> tuple[int, ...]:
    """Apply every pending migration up to `target`. Returns versions applied."""
    return runner(sql_dir).upgrade(conn, target=target)


def downgrade(conn, *, target: int, sql_dir: Path | None = None) -> tuple[int, ...]:
    """Revert applied migrations down to (and including) version > `target`."""
    return runner(sql_dir).downgrade(conn, target=target)
