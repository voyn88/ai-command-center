#!/usr/bin/env python3
"""Capture the pre-restore platform-write manifest.

VOYN-W0-AICC-SRV-09-PLATFORM-ROLLBACK-ASYMMETRY
(platform-writes-survive-aicc-restore).

``scripts/aicc_pg_restore.sh`` restores only ``AICC_PG_DB``. AICC's writer
lease (``repo_lease`` / ``repo_lease_event``, via ``voyn-lease``) and task
state (``tasks`` / ``task_events``, via ``aios_sdk``) live in the AIOS
platform database and survive that restore. The only AICC-side record of
which work was in flight — ``work_item`` / ``work_attempt`` /
``work_event`` — is inside the database the restore is about to wipe.

Run this **before** ``scripts/aicc_pg_restore.sh --allow-overwrite`` during
a cutover window, with dispatch frozen, and write the output somewhere the
restore cannot destroy. The JSON names every claimed work item, its
AICC↔AIOS task-map entry (when present), and every ``voyn-lease list`` row
the host can see. Post-restore reconciliation then has a concrete inventory
instead of reconstructing one from a rolled-back audit trail. See
``docs/operations/PLATFORM_ROLLBACK_ASYMMETRY.md``.

This script does not release leases, cancel platform tasks, or mutate
anything: it is the pre-restore half of the operator procedure only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "build_manifest",
    "list_held_leases",
    "load_task_map",
    "main",
]

_LEASE_LIST_TIMEOUT_SECONDS = 30
_MANIFEST_VERSION = 1


def load_task_map(path: Path) -> dict[str, str]:
    """Best-effort read of ``aios_task_map.json``.

    Absence is normal when the AIOS tasks backend was never enabled; a
    corrupt file is reported as an error entry rather than aborting the
    whole manifest, because the claimed-work list is still useful alone.
    """
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"aios_task_map unreadable at {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"aios_task_map must be an object at {path}")
    out: dict[str, str] = {}
    for key, remote in value.items():
        if isinstance(key, str) and key and isinstance(remote, str) and remote:
            out[key] = remote
    return out


def list_held_leases(
    *,
    lease_tool: str | None = None,
    env: dict[str, str] | None = None,
    runner: Any = subprocess.run,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return (``voyn-lease list`` rows, error-or-None).

    Missing ``VOYN_LEASE_DSN`` is not an error for the manifest: the host
    simply has no lease authority configured, so there is nothing lease-
    related to reconcile. A configured but unreachable authority *is*
    reported — the operator must know the lease half of the inventory is
    incomplete before they wipe the only other record of who held what.
    """
    environ = os.environ if env is None else env
    if not environ.get("VOYN_LEASE_DSN"):
        return [], None
    tool = lease_tool or environ.get("VOYN_LEASE_TOOL", "voyn-lease")
    try:
        listed = runner(
            [tool, "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LEASE_LIST_TIMEOUT_SECONDS,
            env=environ,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [], f"voyn-lease list unreachable: {error}"
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()[-400:]
        return [], f"voyn-lease list refused: {detail}"
    try:
        rows = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError as error:
        return [], f"voyn-lease list unreadable: {error}"
    if not isinstance(rows, list):
        return [], "voyn-lease list unreadable: not a list"
    return [row for row in rows if isinstance(row, dict)], None


def build_manifest(
    *,
    claimed_items: list[dict[str, Any]],
    task_map: dict[str, str],
    leases: list[dict[str, Any]],
    lease_error: str | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the operator-facing inventory from already-fetched pieces.

    Pure: unit tests pin the shape without a database or ``voyn-lease``.
    """
    rows: list[dict[str, Any]] = []
    for item in claimed_items:
        task_id = item.get("task_id")
        aios_id = task_map.get(task_id) if isinstance(task_id, str) else None
        matching = [
            lease
            for lease in leases
            if isinstance(task_id, str)
            and task_id
            and str(lease.get("task") or lease.get("task_id") or "") == task_id
        ]
        rows.append(
            {
                "work_item_id": item.get("work_item_id"),
                "queue": item.get("queue"),
                "task_id": task_id,
                "repository_id": item.get("repository_id"),
                "current_attempt_id": item.get("current_attempt_id"),
                "aios_task_id": aios_id,
                "held_leases": matching,
            }
        )
    return {
        "version": _MANIFEST_VERSION,
        "captured_at": captured_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "purpose": (
            "Pre-restore inventory of AICC work that may have written into "
            "the AIOS platform database (repo_lease/repo_lease_event via "
            "voyn-lease; tasks/task_events via aios_sdk). Capture before "
            "scripts/aicc_pg_restore.sh --allow-overwrite; see "
            "docs/operations/PLATFORM_ROLLBACK_ASYMMETRY.md."
        ),
        "claimed_work": rows,
        "task_map": dict(sorted(task_map.items())),
        "leases": leases,
        "lease_list_error": lease_error,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture the pre-restore platform-write manifest "
            "(VOYN-W0-AICC-SRV-09-PLATFORM-ROLLBACK-ASYMMETRY)."
        )
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Path for the JSON manifest (must survive the restore).",
    )
    parser.add_argument(
        "--task-map",
        type=Path,
        default=None,
        help=(
            "Path to aios_task_map.json. Defaults to "
            "<AICC_DATA_DIR or <repo>/data>/aios_task_map.json."
        ),
    )
    parser.add_argument(
        "--skip-queue",
        action="store_true",
        help="Do not query AICC_PG_DB for claimed work (leases/map only).",
    )
    parser.add_argument(
        "--skip-leases",
        action="store_true",
        help="Do not call voyn-lease list.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from command_center import storage

    data_dir = storage.resolve_data_dir(repo_root)
    task_map_path = args.task_map or (data_dir / "aios_task_map.json")
    try:
        task_map = load_task_map(task_map_path)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    claimed: list[dict[str, Any]] = []
    if not args.skip_queue:
        from command_center.db.work_queue_read import WorkQueueReadStore

        claimed = WorkQueueReadStore().list_items(state="claimed", limit=500)

    leases: list[dict[str, Any]] = []
    lease_error: str | None = None
    if not args.skip_leases:
        leases, lease_error = list_held_leases()

    manifest = build_manifest(
        claimed_items=claimed,
        task_map=task_map,
        leases=leases,
        lease_error=lease_error,
    )
    out_path: Path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {out_path} "
        f"({len(manifest['claimed_work'])} claimed, "
        f"{len(manifest['leases'])} leases, "
        f"{len(manifest['task_map'])} task-map entries)",
        file=sys.stderr,
    )
    if lease_error:
        print(f"warning: {lease_error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
