#!/usr/bin/env python3
"""Check -- or regenerate -- ``command_center/db/sql/released.lock.json``.

The lock records the SHA-256 of every migration file as it shipped.
``MigrationRunner.upgrade`` verifies EVERY recorded checksum before it applies
ANY pending migration, so editing an already-applied file does not correct
that migration: it stops every LATER one from being applied at all
(VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH). The lock puts that same
verdict where the edit is made, with no database needed.

Usage::

    python scripts/migration_lock.py            # check; exit 2 on drift
    python scripts/migration_lock.py --write    # after ADDING a migration

Why regeneration lives here rather than on ``python -m command_center.db``.
Writing the lock is a durable filesystem write inside a package named ``db``,
which is the exact signature ``tests/architecture/aios_boundary.py`` reads as
a persistence engine (docs/AIOS_BOUNDARY.md, ADR-0008/ADR-0015): "a JSON store
is a persistence engine even with no driver anywhere in it". The control
plane's database CLI is not one, and recording it in the frozen inventory to
say otherwise would freeze the operational CLI against growth on the strength
of a developer maintenance command. The boundary doc's own remedy for that is
the one taken here -- prefer moving the module over adding a baseline entry.
Regenerating a checked-in artifact is developer maintenance, the same shape as
``python -m tests.architecture.aios_boundary --write-baseline``; the read-only
check stays on ``python -m command_center.db migration-lock``, where a deploy
can reach it and where it persists nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from command_center.db import migrations  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/migration_lock.py",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Rewrite released.lock.json for the current set (use after ADDING "
        "a migration; changing an existing entry blocks deploys).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.write:
        # Whole-file and deterministic, so ADDING a migration diffs as one new
        # entry and nothing else -- an entry that *changes* is visible as a
        # change, which is the whole point of the file.
        rendered = migrations.render_released_lock()
        migrations.RELEASED_LOCK_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {migrations.RELEASED_LOCK_PATH}")
        return 0

    try:
        migrations.verify_released_checksums()
    except migrations.MigrationError as exc:
        # A refusal, not a crash: this command exists because a traceback hid
        # the real verdict from the person who needed to read it.
        print(f"migration lock: {exc}", file=sys.stderr)
        return 2
    print(f"migration lock: {len(migrations.discover())} migrations unchanged")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin entry point
    raise SystemExit(main())
