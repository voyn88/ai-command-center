"""The morning-digest engine (Дайджест).

``DigestService`` assembles one ordered rollup per calendar day out of the real
in-repo sources (:mod:`command_center.digest.sources`): overnight activity
(runs + commits), advisor proposals awaiting the owner, things needing attention
now, and the live agent-status line. Each assembled entry is *actionable* — it
carries at least one ``ref`` (``run:`` / ``commit:`` / ``proposal:`` / ``task:``
/ ``action:``) so the surfacing UI can link straight to the thing to act on.

Layering: this is the domain/service tier (routes → **service** → repository).
It reads through the source adapters and writes through the ``runtime.db.wave1``
digest repository — never raw SQL. After a build commits it publishes one
:class:`~command_center.events.DigestReady` per entry on the event bus.

Idempotency: :meth:`build` is *idempotent per day*. It deletes the target day's
existing rows and re-inserts the freshly assembled set, so rebuilding a day
replaces rather than duplicates — the item ids change, the content and order do
not.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from command_center.digest import sources
from command_center.events import DigestReady, EventBus, default_bus
from command_center.models import SENSITIVE_PROJECT_IDS
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path

# Repo root is three levels up: <root>/command_center/digest/service.py
ROOT = Path(__file__).resolve().parents[2]

#: Digest section categories, in the fixed order they are laid out each day.
CATEGORY_OVERNIGHT = "overnight"
CATEGORY_ADVISOR = "advisor"
CATEGORY_ATTENTION = "attention"
CATEGORY_STATUS = "status"


def today_str(*, now: datetime | None = None) -> str:
    """The ``YYYY-MM-DD`` a digest built now belongs to (naive-local, matching
    the app's timestamp convention)."""
    return (now or datetime.now()).date().isoformat()


class DigestService:
    """Assembles and persists the per-day morning digest.

    Every collaborator (root path, event bus, and each source function) is held
    on the instance so a test can inject fakes; the defaults wire the real repo,
    the real sources and the process-wide bus."""

    def __init__(
        self,
        *,
        root: Path = ROOT,
        bus: EventBus | None = None,
    ) -> None:
        self._root = root
        self._bus = bus or default_bus()

    def _db_path(self) -> Path:
        path = resolve_db_path(self._root)
        if db.current_schema_version(path) < db.SCHEMA_VERSION:
            db.migrate(path)
        return path

    # -- assembly ----------------------------------------------------------

    def _assemble(self) -> list[dict]:
        """Gather every section into one ordered list of pending entries
        (``title``/``body``/``category``/``refs``), newest-relevant first within
        each section. Pure read — no writes, no events."""
        pending: list[dict] = []

        overnight = sources.overnight_runs(root=self._root)
        commits = sources.recent_commits()
        for row in overnight:
            pending.append(
                {
                    "title": _prefixed(row["project"], row["title"]),
                    "body": row.get("detail", ""),
                    "category": CATEGORY_OVERNIGHT,
                    "refs": _refs(row["ref"]),
                    "project_ref": row.get("project"),
                }
            )
        for row in commits:
            pending.append(
                {
                    "title": row["title"],
                    "body": row.get("detail", ""),
                    "category": CATEGORY_OVERNIGHT,
                    "refs": _refs(row["ref"]),
                    "project_ref": None,
                }
            )

        for row in sources.open_proposals(root=self._root):
            pending.append(
                {
                    "title": _prefixed(row["project"], row["title"]),
                    "body": row.get("detail", ""),
                    "category": CATEGORY_ADVISOR,
                    "refs": _refs(row["ref"], "action:/api/v1/proposals"),
                    "project_ref": row.get("project"),
                }
            )

        for row in sources.attention_items(root=self._root):
            pending.append(
                {
                    "title": _prefixed(row["project"], row["title"]),
                    "body": row.get("detail", ""),
                    "category": CATEGORY_ATTENTION,
                    "refs": _refs(row["ref"]),
                    "project_ref": row.get("project"),
                }
            )

        status = sources.agent_status(root=self._root)
        pending.append(
            {
                "title": (
                    f"Статус агентов: {status['running']} выполняется, "
                    f"{status['queued']} в очереди, {status['attention']} требуют внимания"
                ),
                "body": f"всего запусков: {status['total']}",
                "category": CATEGORY_STATUS,
                "refs": _refs("action:/api/v1/agents"),
                "project_ref": None,
            }
        )
        return pending

    # -- public API --------------------------------------------------------

    def build(self, *, day: str | None = None) -> list[dict]:
        """Assemble, persist and announce the digest for ``day`` (default:
        today). Deletes the day's existing rows first (idempotent rebuild),
        inserts the freshly assembled set in order, publishes one
        :class:`DigestReady` per entry, and returns the created rows."""
        day = day or today_str()
        path = self._db_path()
        pending = self._assemble()

        db.delete_digest_items_for_day(path, day)
        created: list[dict] = []
        for position, entry in enumerate(pending):
            row = db.create_digest_item(
                path,
                title=entry["title"],
                body=entry["body"],
                category=entry["category"],
                refs=entry["refs"],
                project_ref=entry.get("project_ref"),
                day=day,
                position=position,
            )
            created.append(row)
        for row in created:
            self._bus.publish(
                DigestReady(digest_id=row["id"], category=row.get("category")),
                raise_errors=False,
            )
        return created

    def today(self, *, day: str | None = None) -> list[dict]:
        """The already-built digest for ``day`` (default: today), in assembly
        order. Read-only: never triggers a build. Sensitive-project rows are
        dropped in the query (defence in depth — the sources already exclude
        them at build time)."""
        return db.list_digest_items_for_day(
            self._db_path(),
            day or today_str(),
            exclude_projects=sorted(SENSITIVE_PROJECT_IDS),
        )


def _refs(*refs: str) -> list[str]:
    """Drop empties so an entry never carries a blank ref."""
    return [r for r in refs if r]


def _prefixed(project: str | None, title: str) -> str:
    """Prefix an entry title with its project code when it has one, so the
    reader sees *which* project a line is about at a glance."""
    return f"[{project}] {title}" if project else title
