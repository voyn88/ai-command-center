"""Forward/backward migrations for the AICC server database.

The *schema* is AICC's: 33 tables, their indexes, the `aicc_*` grants that go
with them, and the plain SQL files in `sql/` named `NNNN_slug.up.sql` with a
matching `NNNN_slug.down.sql`. AIOS Core knows none of that and must not.

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

import hashlib
import json
import logging
from pathlib import Path

from command_center.db import adapter

__all__ = [
    "LEDGER_TABLE",
    "LOCK_NAMESPACE",
    "RELEASED_LOCK_PATH",
    "Migration",
    "MigrationError",
    "applied_versions",
    "current_version",
    "discover",
    "downgrade",
    "ensure_ledger",
    "released_lock",
    "render_released_lock",
    "runner",
    "upgrade",
    "verify_released_checksums",
]

_LOG = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"

#: Every migration file's SHA-256, recorded when the migration shipped. Lives
#: beside the files it pins so adding a migration and locking it are one place.
RELEASED_LOCK_PATH = SQL_DIR / "released.lock.json"

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


# ---------------------------------------------------------------------------
# A shipped migration file is immutable (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-
# GROWTH).
#
# `schema_migration` stores the SHA-256 of each up-file as it was applied, and
# `MigrationRunner.upgrade` verifies EVERY recorded checksum before it applies
# ANY pending migration. So a one-character edit to an already-applied file is
# not a small correction with a small blast radius -- it is a hard stop on the
# whole upgrade, and the migrations it blocks are the NEW ones, which is
# exactly the set a deploy is running for.
#
# That happened here. Commit ffd2042c fixed a stale `-- 0020:` header in
# 0022's comment while landing the queue's no-fault refund. 0022 had shipped
# long before, so on any database that applied it -- control-01's included --
# `db upgrade` raises MigrationChecksumMismatch and applies NOTHING: the queue
# migrations 0024-0027 that were the rest of that same fix cannot land, and
# `self-deploy --migrate` rolls the checkout back, so the Python half does not
# either. `control-01:queue` goes on measuring `dead_letter_growth` from the
# unpatched functions, while the branch's own tests stay green -- nothing
# outside a real, already-migrated database can see it.
#
# The lock below moves that verdict off the control host and into the test
# suite, where the edit is made. It is not a second authority -- `up` is
# literally `Migration.checksum`, the value the ledger holds -- it is the same
# authority, reachable without a database. It cannot stop someone who edits the
# file and re-locks it in one commit; that is the point. The failure mode was
# never someone deciding to break the ledger, it was someone tidying a comment
# without knowing the ledger existed, and a diff that says
# `-"633f0e94..." +"509bdb47..."` cannot be read that way.
# ---------------------------------------------------------------------------


_LOCK_NOTE = (
    "SHA-256 of every migration file, recorded when the migration shipped. "
    "`up` is the value the database's own schema_migration ledger holds; "
    "MigrationRunner.upgrade verifies it BEFORE applying anything pending, so "
    "editing an already-applied migration does not correct that migration -- "
    "it stops the deploy from applying every LATER one. Changing an entry "
    "below is a deliberate act with a live consequence, not bookkeeping. "
    "After ADDING a migration, regenerate with: "
    "python -m command_center.db migration-lock --write"
)


def _pin(checksum: str) -> str:
    """A recorded checksum, algorithm-prefixed.

    The prefix names the algorithm, and it is also load-bearing for the
    repository's secret scanner: detect-secrets' `HexHighEntropyString` reads
    a quoted run of pure hex as a possible credential, so 54 bare digests
    would be 54 baseline entries and every future migration would fail the
    "baseline changed" gate until someone re-ran the scanner. `sha256:` is
    not pure hex (`s`, `h` and `:` are not hex digits), so the file scans
    clean and stays that way. Do not "tidy" it off -- that is the same shape
    of harmless-looking edit this whole section exists because of.
    """
    return f"sha256:{checksum}"


def _short(pin: str) -> str:
    """Enough of a pinned digest to compare two by eye, past the prefix."""
    return pin.rpartition(":")[2][:12]


def released_lock() -> dict[int, dict[str, str]]:
    """The recorded checksums, keyed by version.

    An unreadable lock is reported as a `MigrationError` like every other
    verdict here. The command this feeds exists because a traceback hid the
    real answer from the operator, so it must not have a path of its own that
    prints one -- and a missing file is a plausible way to reach it, since
    "delete the thing that is failing" is the obvious wrong move when the
    guard refuses.
    """
    try:
        raw = json.loads(RELEASED_LOCK_PATH.read_text(encoding="utf-8"))
        return {int(version): entry for version, entry in raw["migrations"].items()}
    except FileNotFoundError:
        raise MigrationError(
            f"{RELEASED_LOCK_PATH.name} is missing; it records the checksum every "
            "migration shipped with and cannot be regenerated from a database. "
            "Restore it from version control rather than rewriting it: "
            "`--write` would re-record whatever the files currently say, "
            "including an edit that is already blocking deploys."
        ) from None
    except (ValueError, KeyError, TypeError) as exc:
        raise MigrationError(
            f"{RELEASED_LOCK_PATH.name} is not readable as a migration lock "
            f"({exc}); restore it from version control"
        ) from None


def _down_checksum(migration: Migration) -> str:
    """The ledger records only the up-file. A down-file is pinned here too
    because it is the SQL an operator would actually run to revert an applied
    migration -- editing it after the fact makes the revert silently disagree
    with what was applied, with nothing to catch it."""
    return hashlib.sha256(migration.down_sql.encode("utf-8")).hexdigest()


def render_released_lock(sql_dir: Path | None = None) -> str:
    """The lock file's exact contents for the current migration set.

    Regeneration is deliberately whole-file and deterministic, so the diff of
    ADDING a migration is one new entry and nothing else -- an entry that
    changes shows up as a change. The note is rendered from `_LOCK_NOTE`
    rather than carried over from the file, so `--write` works on a deleted
    lock and the explanation cannot drift from the code that enforces it.
    """
    payload = {
        "note": _LOCK_NOTE,
        "migrations": {
            f"{migration.version:04d}": {
                "slug": migration.slug,
                "up": _pin(migration.checksum),
                "down": _pin(_down_checksum(migration)),
            }
            for migration in discover(sql_dir)
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def verify_released_checksums(sql_dir: Path | None = None) -> None:
    """Raise `MigrationError` if the migration set on disk has drifted from
    the lock -- an edited file, or a new one nobody recorded."""
    recorded = released_lock()
    found = {migration.version: migration for migration in discover(sql_dir)}

    unlocked = sorted(set(found) - set(recorded))
    if unlocked:
        raise MigrationError(
            f"migration(s) {unlocked} are not recorded in "
            f"{RELEASED_LOCK_PATH.name}; regenerate it so the new file's "
            "checksum is pinned from the moment it ships"
        )
    dropped = sorted(set(recorded) - set(found))
    if dropped:
        raise MigrationError(
            f"migration(s) {dropped} are recorded in {RELEASED_LOCK_PATH.name} "
            "but no file defines them; a shipped migration cannot be deleted, "
            "because databases that applied it still name it in their ledger"
        )

    for version, migration in sorted(found.items()):
        entry = recorded[version]
        for direction, actual in (
            ("up", _pin(migration.checksum)),
            ("down", _pin(_down_checksum(migration))),
        ):
            if entry[direction] != actual:
                raise MigrationError(
                    f"{migration.name}.{direction}.sql changed after it "
                    f"shipped (recorded {_short(entry[direction])}…, file "
                    f"{_short(actual)}…). A database that already applied it "
                    "verifies the recorded checksum before applying ANY "
                    "pending migration, so this edit does not correct "
                    f"{migration.name} -- it blocks every LATER migration "
                    "from being applied at all. Add a new migration instead."
                )
