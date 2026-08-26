"""Auto-fill «Мой день» from owner-relevant domain events.

``OwnerAutofill`` subscribes to the in-process event bus and, when an event
crosses an owner-gate (:mod:`command_center.digest.owner_gates`), creates an
:class:`OwnerItem` through the ``runtime.db.wave1`` repository. The owner's day
list therefore assembles itself from what actually happened — a proposal the
owner took, an incident that opened, a gated proposal that appeared — instead of
being hand-curated.

Event hooks (real, on the bus today):

* ``ProposalPromotedToTask`` → follow-up item (the owner *took* this proposal).
* ``ProposalCreated`` in a gated project → the owner must clear this gate.
* ``IncidentOpened`` at a gated severity → non-delegable owner decision.

Board and networking hooks are defined as **direct methods**, not bus
subscriptions, because their source events (a board motion opening for a vote, a
non-delegable networking reply) are not yet on the shared bus — the Board engine
lands in a later wave. Pinning them as typed, tested seams here means the moment
those events exist, wiring them is a one-line ``subscribe`` with no change to the
item-creation contract, and nothing fabricates a fake event in the meantime.

Idempotency: every auto-created item carries a stable ``source_ref`` (e.g.
``promotion:<id>``); the subscriber skips creation when an item with that ref
already exists, so a redelivered or replayed event never doubles the list.
"""

from __future__ import annotations

from pathlib import Path

from command_center.digest.owner_gates import DEFAULT_OWNER_GATES, OwnerGateConfig
from command_center.events import (
    EventBus,
    IncidentOpened,
    OwnerItemCreated,
    ProposalCreated,
    ProposalPromotedToTask,
    default_bus,
)
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path

# Repo root is three levels up: <root>/command_center/digest/owner_autofill.py
ROOT = Path(__file__).resolve().parents[2]

# How many existing items the dedupe scan looks back over. The day list is
# small and short-lived; this bound keeps the scan cheap while covering any
# realistic same-day replay.
_DEDUPE_SCAN_LIMIT = 500


class OwnerAutofill:
    """Subscribes to the bus and mirrors owner-relevant events into owner items."""

    def __init__(
        self,
        *,
        root: Path = ROOT,
        gates: OwnerGateConfig = DEFAULT_OWNER_GATES,
    ) -> None:
        self._root = root
        self._gates = gates
        # The bus auto-created items are announced on. Overridden to whatever bus
        # :meth:`register` binds to, so an item created in response to an event
        # announces on the same bus; direct seams fall back to the default bus.
        self._bus: EventBus = default_bus()
        self._unsubscribes: list = []

    def _db_path(self) -> Path:
        path = resolve_db_path(self._root)
        if db.current_schema_version(path) < db.SCHEMA_VERSION:
            db.migrate(path)
        return path

    # -- registration ------------------------------------------------------

    def register(self, bus: EventBus) -> "OwnerAutofill":
        """Subscribe every handler on ``bus``. Returns self so callers can keep
        the instance to :meth:`unregister` later (tests, shutdown)."""
        self._bus = bus
        self._unsubscribes.append(bus.subscribe(ProposalPromotedToTask, self.on_proposal_promoted))
        self._unsubscribes.append(bus.subscribe(ProposalCreated, self.on_proposal_created))
        self._unsubscribes.append(bus.subscribe(IncidentOpened, self.on_incident_opened))
        return self

    def unregister(self) -> None:
        for off in self._unsubscribes:
            off()
        self._unsubscribes.clear()

    # -- shared creation seam ---------------------------------------------

    def _ensure_item(
        self, *, title: str, source_ref: str, detail: str | None = None,
        due: str | None = None, project_ref: str | None = None,
    ) -> dict | None:
        """Create one owner item unless its ``source_ref`` is already present.
        Returns the created row, or ``None`` when it already existed (idempotent
        event → item), or ``None`` when ``project_ref`` names a sensitive
        (BANK/LEGAL) project — such an item is never created, so its subject
        never lands on «Мой день» (audit MED-1)."""
        if project_ref and is_sensitive(project_ref):
            return None
        path = self._db_path()
        if self._existing_source_refs(path) & {source_ref}:
            return None
        row = db.create_owner_item(
            path, title=title, detail=detail, due=due, source_ref=source_ref,
            project_ref=project_ref,
        )
        # Announce on the same bus the manual POST path uses, so the day list has
        # one consistent event trail whether an item was typed or auto-filled.
        self._bus.publish(
            OwnerItemCreated(item_id=row["id"], title=row["title"]),
            raise_errors=False,
        )
        return row

    def _existing_source_refs(self, path: Path) -> set[str]:
        return {
            r.get("source_ref")
            for r in db.list_owner_items(path, limit=_DEDUPE_SCAN_LIMIT)
            if r.get("source_ref")
        }

    # -- bus handlers ------------------------------------------------------

    def on_proposal_promoted(self, event: ProposalPromotedToTask) -> None:
        if not self._gates.promotions_are_owner_items:
            return
        self._ensure_item(
            title="Отследить задачу из принятого предложения",
            detail=f"task {event.task_id}",
            source_ref=f"promotion:{event.proposal_id}",
            project_ref=event.project_ref or None,
        )

    def on_proposal_created(self, event: ProposalCreated) -> None:
        if event.project_ref not in self._gates.gate_projects:
            return
        self._ensure_item(
            title="Решение владельца по предложению (owner-gate)",
            detail=f"{event.kind} · {event.project_ref}",
            source_ref=f"proposal:{event.proposal_id}",
            project_ref=event.project_ref or None,
        )

    def on_incident_opened(self, event: IncidentOpened) -> None:
        if event.severity not in self._gates.incident_severities:
            return
        self._ensure_item(
            title="Инцидент требует решения владельца",
            detail=f"{event.severity}"
            + (f" · {event.project_ref}" if event.project_ref else ""),
            source_ref=f"incident:{event.incident_id}",
            project_ref=event.project_ref or None,
        )

    # -- direct seams (no bus event yet — Board / networking land later) ---

    def board_motion_awaiting_vote(
        self, *, motion_id: str, title: str, due: str | None = None
    ) -> dict | None:
        """A board motion is open and awaiting the owner's vote. Wire this to a
        ``MotionOpened`` bus event when the Board engine lands; until then it is
        called directly by whatever surfaces motions. Idempotent per motion."""
        return self._ensure_item(
            title=f"Голосование по motion: {title}",
            detail=f"motion {motion_id}",
            due=due,
            source_ref=f"motion:{motion_id}",
        )

    def networking_reply_nondelegable(
        self, *, reply_ref: str, title: str
    ) -> dict | None:
        """A networking reply was flagged as one only the owner can send.
        Idempotent per reply."""
        return self._ensure_item(
            title=f"Ответить лично: {title}",
            detail="non-delegable",
            source_ref=f"reply:{reply_ref}",
        )


def complete_owner_item(item_id: str, *, root: Path = ROOT) -> dict | None:
    """Mark an owner item done (compare-and-set on the current version), the
    write behind ``POST /owner-items/{id}/complete``. Idempotent: an item that
    is already done is returned unchanged. Returns ``None`` if no such item."""
    path = resolve_db_path(root)
    if db.current_schema_version(path) < db.SCHEMA_VERSION:
        db.migrate(path)
    row = db.get_owner_item(path, item_id)
    if row is None:
        return None
    if row.get("done"):
        return row
    return db.set_owner_item_done(
        path, item_id, expected_version=row["version"], done=True
    )
