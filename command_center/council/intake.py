"""Raise motions from owner-relevant domain events.

``CouncilIntake`` subscribes to the in-process event bus and opens a
:class:`~command_center.api.models.Motion` when something happens that the Board
should collectively decide on: an advisor proposal was surfaced
(:class:`~command_center.events.ProposalCreated`) or an operational incident was
opened (:class:`~command_center.events.IncidentOpened`). The Council board
therefore assembles itself from what actually happened, rather than depending on
every source to also remember to POST a motion.

This is a *seam*, not tight coupling: the intake only consumes the small, stable
routing facts each event already carries (ids, kind, project) and re-reads
nothing from the advisor/conflicts internals — it never imports them. What class
of source raises a motion is configuration (:class:`CouncilIntakeConfig`), so an
operator can gate intake without touching the subscriber.

Idempotency: every event-raised motion carries a stable ``source_ref``
(``proposal:<id>`` / ``incident:<id>``); the subscriber skips creation when a
motion with that ref already exists, so a redelivered or replayed event never
opens a second motion (dedup by ``source_ref``).

Redaction: an event attributed to a BANK/LEGAL project is dropped — no motion is
opened, so a sensitive source's reference never lands on the board. This mirrors
the service's write-side rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from command_center.events import (
    EventBus,
    IncidentOpened,
    ProposalCreated,
    default_bus,
)
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path

# Repo root is three levels up: <root>/command_center/council/intake.py
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class CouncilIntakeConfig:
    """What raises a motion, and with what quorum.

    * ``raise_from_proposals`` / ``raise_from_incidents`` — which event classes
      open a motion (both on by default).
    * ``quorum`` — the quorum an event-raised motion opens with. An operator can
      set the bar higher for automatically-raised motions than the API default.
    * ``proposed_by`` — the ``proposed_by`` recorded on an event-raised motion
      (the subsystem that surfaced the source, not a person).
    """

    raise_from_proposals: bool = True
    raise_from_incidents: bool = True
    quorum: int = 1
    proposed_by: str = "council-intake"


#: The process default: both proposals and incidents raise a motion at quorum 1.
DEFAULT_INTAKE_CONFIG = CouncilIntakeConfig()


class CouncilIntake:
    """Subscribes to the bus and raises motions from owner-relevant events."""

    def __init__(
        self,
        *,
        root: Path = ROOT,
        config: CouncilIntakeConfig = DEFAULT_INTAKE_CONFIG,
    ) -> None:
        self._root = root
        self._config = config
        self._unsubscribes: list = []

    def _db_path(self) -> Path:
        path = resolve_db_path(self._root)
        if db.current_schema_version(path) < db.SCHEMA_VERSION:
            db.migrate(path)
        return path

    # -- registration ------------------------------------------------------

    def register(self, bus: EventBus) -> "CouncilIntake":
        """Subscribe on ``bus``. Returns self so callers keep the instance to
        :meth:`unregister` later (tests, shutdown)."""
        if self._config.raise_from_proposals:
            self._unsubscribes.append(
                bus.subscribe(ProposalCreated, self.on_proposal_created)
            )
        if self._config.raise_from_incidents:
            self._unsubscribes.append(
                bus.subscribe(IncidentOpened, self.on_incident_opened)
            )
        return self

    def unregister(self) -> None:
        for off in self._unsubscribes:
            off()
        self._unsubscribes.clear()

    # -- bus handlers ------------------------------------------------------

    def on_proposal_created(self, event: ProposalCreated) -> dict | None:
        """Raise a motion to adopt an advisor proposal, unless its project is
        sensitive or a motion already exists for it (dedup by ``source_ref``)."""
        return self._raise(
            source_ref=f"proposal:{event.proposal_id}",
            project_ref=event.project_ref or None,
            title=f"Adopt proposal {event.proposal_id}",
            body=f"Advisor proposal ({event.kind}) put to the Board for a decision.",
            proposal_ref=event.proposal_id or None,
        )

    def on_incident_opened(self, event: IncidentOpened) -> dict | None:
        """Raise a motion to decide the response to an operational incident,
        unless its project is sensitive or a motion already exists for it."""
        return self._raise(
            source_ref=f"incident:{event.incident_id}",
            project_ref=event.project_ref or None,
            title=f"Respond to incident {event.incident_id}",
            body=f"Operational incident (severity {event.severity}) put to the Board.",
        )

    # -- shared open path --------------------------------------------------

    def _raise(
        self,
        *,
        source_ref: str,
        project_ref: str | None,
        title: str,
        body: str,
        proposal_ref: str | None = None,
    ) -> dict | None:
        if project_ref and is_sensitive(project_ref):
            return None
        path = self._db_path()
        if db.get_motion_by_source_ref(path, source_ref) is not None:
            return None
        return db.create_motion(
            path,
            title=title,
            proposed_by=self._config.proposed_by,
            body=body,
            quorum=self._config.quorum,
            project_ref=project_ref,
            proposal_ref=proposal_ref,
            source_ref=source_ref,
        )


#: Guards against double-registration on the default bus (import is once per
#: process, but an explicit re-import or reload must not stack subscribers).
_DEFAULT_INTAKE: CouncilIntake | None = None


def install_default_intake() -> CouncilIntake:
    """Install (once) the default proposal/incident→motion intake on the
    process-wide bus and return it. Idempotent: repeated calls return the same
    instance without stacking subscriptions."""
    global _DEFAULT_INTAKE
    if _DEFAULT_INTAKE is None:
        _DEFAULT_INTAKE = CouncilIntake().register(default_bus())
    return _DEFAULT_INTAKE
