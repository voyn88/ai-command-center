#!/usr/bin/env python3
"""Rehearse a runtime.db schema upgrade on a copy; never touch the original.

See docs/operations/SQLITE_SCHEMA_UPGRADE_WINDOW.md (VOYN-W0-AICC-SQLITE-SCHEMA-16-TO-23).

Examples:

    python scripts/rehearse_runtime_schema_upgrade.py \\
        --db data/runtime.db --archive-dir data/schema-upgrade --stop-after 23

    python scripts/rehearse_runtime_schema_upgrade.py \\
        --db data/runtime.db --archive-dir data/schema-upgrade --stop-after 24
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from command_center.runtime import schema_upgrade  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backup + timed schema upgrade rehearsal on a copy of runtime.db. "
            "Provenance backfill stays off unless --backfill is passed."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the live runtime.db (rehearsal copies it; never mutates it)",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        required=True,
        help="Directory for the backup + rehearsal copy + JSON report artefacts",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=23,
        help=(
            "Highest migration version to apply on the copy (default: 23, the "
            "ticket boundary). Pass 24 to include finalized_at. Pass 0 to walk "
            "as far as this binary can without the offline v25 cutover."
        ),
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "Also run the bounded provenance backfill on the copy. Off by "
            "default: the live window's backup must exist before that step."
        ),
    )
    args = parser.parse_args(argv)
    stop_after = None if args.stop_after == 0 else args.stop_after
    from command_center.runtime import db

    try:
        report = schema_upgrade.rehearse_schema_upgrade(
            args.db,
            archive_dir=args.archive_dir,
            stop_after_version=stop_after,
            run_backfill=args.backfill,
        )
    except (schema_upgrade.SchemaUpgradeError, db.FinalizationClaimCutoverRequired) as exc:
        # FinalizationClaimCutoverRequired is the expected gate when a rehearsal
        # asks to walk past v24 without the offline cutover; surface it cleanly.
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
