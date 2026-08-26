"""In-process, synchronous event bus for the Wave-1 "new engine" flows.

Public surface::

    from command_center.events import default_bus, ProposalPromotedToTask

    bus = default_bus()
    unsubscribe = bus.subscribe(ProposalPromotedToTask, my_handler)
    bus.publish(ProposalPromotedToTask(proposal_id="p1", task_id="t1", project_ref="AICC"))

See :mod:`command_center.events.bus` for delivery/ordering/error semantics and
:mod:`command_center.events.types` for the event vocabulary.
"""

from __future__ import annotations

from command_center.events.bus import (
    EventBus,
    EventDispatchError,
    Subscriber,
    default_bus,
)
from command_center.events.types import (
    AuditFindingCreated,
    AuditFindingPromotedToTask,
    AuditRunCompleted,
    DigestReady,
    Event,
    IncidentOpened,
    ModelAssigned,
    ModelRegistered,
    ModelStatusChanged,
    NetworkingContactInvited,
    NetworkingFeedbackReceived,
    OwnerItemCreated,
    ProposalCreated,
    ProposalPromotedToTask,
)

__all__ = [
    "EventBus",
    "EventDispatchError",
    "Subscriber",
    "default_bus",
    "Event",
    "ProposalCreated",
    "ProposalPromotedToTask",
    "OwnerItemCreated",
    "DigestReady",
    "IncidentOpened",
    "AuditRunCompleted",
    "AuditFindingCreated",
    "AuditFindingPromotedToTask",
    "ModelRegistered",
    "ModelStatusChanged",
    "ModelAssigned",
    "NetworkingFeedbackReceived",
    "NetworkingContactInvited",
]
