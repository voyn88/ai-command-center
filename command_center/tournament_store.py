"""Publishing and reading the monthly Субъектные турниры protocol.

One JSON document (`data/tournament_protocols.json`), keyed by month
(`YYYY-MM`), mirroring `portfolio_config.py`'s locked read-modify-write
convention. Publishing a month is idempotent — rebuilding it overwrites that
month's entry with a freshly computed one rather than accumulating duplicates,
the same "rebuild replaces" semantics `digest.service.DigestService.build`
uses for a day.

Loading tasks/runs is factored into two module-level functions
(`_load_tasks_by_id` / `_load_completed_runs`) purely so a test can monkeypatch
them and drive `publish_month` off fixed fixtures, without a real task store or
runtime db — the same testability seam `digest.sources` uses.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from command_center import storage, tournament
from command_center.runtime.db.core import resolve_db_path
from command_center.runtime.runs_read import list_unified_runs
from command_center.tasks_repository import load_tasks

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = storage.resolve_data_dir(ROOT)
PROTOCOLS_FILE = DATA_DIR / "tournament_protocols.json"
PROTOCOLS_LOCK_FILE = DATA_DIR / "tournament_protocols.lock"


def _load_tasks_by_id(root: Path) -> dict[str, dict]:
    return {task["id"]: task for task in load_tasks(root) if task.get("id")}


def _load_completed_runs(root: Path) -> list[dict]:
    try:
        return list_unified_runs(resolve_db_path(root), root=root)
    except Exception:
        return []


def _read_all() -> dict[str, dict]:
    data = storage.read_json(PROTOCOLS_FILE, {})
    return data if isinstance(data, dict) else {}


def list_protocols() -> list[dict]:
    """Every published protocol, most recent month first."""
    return [protocol for _, protocol in sorted(_read_all().items(), reverse=True)]


def get_protocol(month: str) -> dict | None:
    """The published protocol for `month` (`YYYY-MM`), or `None` if that
    month has never been published."""
    return _read_all().get(month)


def publish_month(*, root: Path = ROOT, month: str | None = None, now: datetime | None = None) -> dict:
    """Build `month`'s (default: current month) protocol from the real task
    and run stores, persist it, and return it. Idempotent: re-publishing the
    same month replaces its prior entry rather than duplicating it."""
    month = month or tournament.current_month(now=now)
    protocol = tournament.build_monthly_protocol(
        _load_completed_runs(root),
        _load_tasks_by_id(root),
        month=month,
        now=now,
    )
    record = tournament.protocol_to_dict(protocol)
    with storage.file_lock(PROTOCOLS_LOCK_FILE):
        all_protocols = _read_all()
        all_protocols[month] = record
        storage.atomic_write_json(PROTOCOLS_FILE, all_protocols)
    return record


def ensure_current_month_published(*, root: Path = ROOT, now: datetime | None = None) -> dict:
    """The current month's protocol, publishing it first if this is the first
    call this month. Never re-publishes an already-published current month —
    that stays a deliberate action of `publish_month`, not an implicit
    side-effect of every dashboard load."""
    month = tournament.current_month(now=now)
    existing = get_protocol(month)
    if existing is not None:
        return existing
    return publish_month(root=root, month=month, now=now)
