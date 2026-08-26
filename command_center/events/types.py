"""Typed domain events for the Wave-1 in-process event bus.

Events are immutable value objects (frozen dataclasses). Each carries only the
identifiers and coarse routing facts a subscriber needs to react — never a full
entity payload, and never secrets, tokens, environment dumps or repository
paths. A subscriber that needs the entity re-reads it from its repository by id,
so the event stays a small, stable notification rather than a second, drifting
copy of the row.

The base :class:`Event` exists so a subscriber can register for *all* events
(subscribe to ``Event``) — delivery matches by ``isinstance`` (see
:mod:`command_center.events.bus`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from command_center.models import iso_now


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for every bus event. ``occurred_at`` is an ISO-8601 local
    timestamp stamped at construction, matching the rest of the app's time
    convention (:func:`command_center.models.iso_now`)."""

    occurred_at: str = field(default_factory=iso_now)


@dataclass(frozen=True, slots=True)
class ProposalCreated(Event):
    """An advisor proposal (Советник) was persisted."""

    proposal_id: str = ""
    kind: str = ""
    project_ref: str = ""


@dataclass(frozen=True, slots=True)
class ProposalPromotedToTask(Event):
    """An advisor proposal was promoted into a task on the board. ``task_id`` is
    the id returned by the existing tasks path; downstream services (execution
    queue projections, digests, notifications) consume this."""

    proposal_id: str = ""
    task_id: str = ""
    project_ref: str = ""


@dataclass(frozen=True, slots=True)
class OwnerItemCreated(Event):
    """An item was added to «Мой день» (the owner's action list)."""

    item_id: str = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class DigestReady(Event):
    """A digest entry (Дайджест) was compiled and is ready to surface."""

    digest_id: str = ""
    category: str | None = None


@dataclass(frozen=True, slots=True)
class AuditRunCompleted(Event):
    """An audit run (VOYN-W2-AUD) finished. Carries only routing facts — the
    run id, its project, terminal status and how many findings it produced; a
    subscriber re-reads the run/findings by id rather than receiving them here."""

    run_id: str = ""
    project_ref: str = ""
    status: str = ""
    finding_count: int = 0


@dataclass(frozen=True, slots=True)
class AuditFindingCreated(Event):
    """One audit finding was persisted. Every finding carries a status and an
    owner by construction; the event surfaces the coarse triage facts."""

    finding_id: str = ""
    run_id: str = ""
    category: str = ""
    severity: str = ""


@dataclass(frozen=True, slots=True)
class AuditFindingPromotedToTask(Event):
    """An audit finding was promoted into a task on the board. ``task_id`` is the
    id returned by the existing tasks path; downstream services consume this."""

    finding_id: str = ""
    task_id: str = ""
    project_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRegistered(Event):
    """A model (VOYN-W3-MODELS) was added to the registry. Carries only routing
    facts — the model id, its kind and provider; a subscriber re-reads the entry
    by id rather than receiving the whole row here."""

    model_id: str = ""
    kind: str = ""
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class ModelStatusChanged(Event):
    """A model moved along its availability lifecycle (e.g. a local model finished
    downloading and is now ``installed``)."""

    model_id: str = ""
    status: str = ""


@dataclass(frozen=True, slots=True)
class ModelAssigned(Event):
    """A model was assigned to a task/agent. ``target_ref`` is the opaque
    task/agent reference; downstream services consume this. Never carries the
    payload routed to the model — only that an assignment happened."""

    model_id: str = ""
    target_ref: str = ""


@dataclass(frozen=True, slots=True)
class NetworkingFeedbackReceived(Event):
    """Inbound networking feedback was captured and turned into a board task
    (VOYN-W3-NET). ``task_id`` is the id returned by the existing tasks path;
    ``message_id`` is the captured intake message. This is also the ``feedback``
    signal the advisor can consume (reusing its collector concept) without this
    layer touching advisor internals — a subscriber re-reads the entity by id."""

    message_id: str = ""
    task_id: str = ""
    contact_id: str = ""
    project_ref: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkingContactInvited(Event):
    """A networking contact was invited to the Council (VOYN-W3-NET). ``council_ref``
    is the stable seam the Council engine consumes to resolve the invitation; this
    event carries only routing facts, no external identity. The Council engine
    subscribes here (or reads the invitation by id) — no auth is wired yet."""

    invitation_id: str = ""
    contact_id: str = ""
    council_ref: str = ""
    project_ref: str | None = None


@dataclass(frozen=True, slots=True)
class IncidentOpened(Event):
    """An operational incident was opened. Part of the typed event vocabulary
    the Wave-1 services publish/consume; the Incident entity itself is persisted
    in a later increment, but the event type is pinned here so subscribers can
    be written against a stable contract now."""

    incident_id: str = ""
    severity: str = ""
    project_ref: str | None = None
