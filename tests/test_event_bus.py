"""Tests for the in-process event bus (``command_center.events``).

Hermetic: each test builds its own :class:`EventBus`, so nothing leaks through
the process-wide default bus.
"""

from __future__ import annotations

import pytest

from command_center.events import (
    DigestReady,
    Event,
    EventBus,
    EventDispatchError,
    ProposalCreated,
    ProposalPromotedToTask,
    default_bus,
)


def test_publish_delivers_to_matching_subscriber() -> None:
    bus = EventBus()
    seen: list[ProposalCreated] = []
    bus.subscribe(ProposalCreated, seen.append)

    event = ProposalCreated(proposal_id="p1", kind="trend", project_ref="AICC")
    delivered = bus.publish(event)

    assert delivered == 1
    assert seen == [event]


def test_subscriber_only_receives_its_own_type() -> None:
    bus = EventBus()
    created: list[Event] = []
    promoted: list[Event] = []
    bus.subscribe(ProposalCreated, created.append)
    bus.subscribe(ProposalPromotedToTask, promoted.append)

    bus.publish(ProposalCreated(proposal_id="p1", kind="ux", project_ref="AICC"))

    assert len(created) == 1
    assert promoted == []


def test_base_event_subscription_receives_everything() -> None:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(Event, seen.append)

    bus.publish(ProposalCreated(proposal_id="p1", kind="trend", project_ref="AICC"))
    bus.publish(DigestReady(digest_id="d1", category="ops"))

    assert [type(e).__name__ for e in seen] == ["ProposalCreated", "DigestReady"]


def test_delivery_is_in_registration_order() -> None:
    bus = EventBus()
    order: list[str] = []
    bus.subscribe(ProposalCreated, lambda e: order.append("first"))
    bus.subscribe(Event, lambda e: order.append("second"))
    bus.subscribe(ProposalCreated, lambda e: order.append("third"))

    bus.publish(ProposalCreated(proposal_id="p", kind="trend", project_ref="AICC"))

    assert order == ["first", "second", "third"]


def test_unsubscribe_stops_delivery_and_is_idempotent() -> None:
    bus = EventBus()
    seen: list[Event] = []
    unsubscribe = bus.subscribe(ProposalCreated, seen.append)

    unsubscribe()
    unsubscribe()  # second call must be a harmless no-op
    bus.publish(ProposalCreated(proposal_id="p", kind="trend", project_ref="AICC"))

    assert seen == []
    assert bus.subscriber_count() == 0


def test_one_failing_subscriber_does_not_rob_the_others() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(ProposalCreated, lambda e: seen.append("a"))

    def boom(_e: Event) -> None:
        raise RuntimeError("subscriber blew up")

    bus.subscribe(ProposalCreated, boom)
    bus.subscribe(ProposalCreated, lambda e: seen.append("c"))

    with pytest.raises(EventDispatchError) as excinfo:
        bus.publish(ProposalCreated(proposal_id="p", kind="trend", project_ref="AICC"))

    # Every non-failing subscriber still ran, and the failure is reported.
    assert seen == ["a", "c"]
    assert len(excinfo.value.errors) == 1
    assert isinstance(excinfo.value.errors[0], RuntimeError)


def test_raise_errors_false_swallows_subscriber_failures() -> None:
    bus = EventBus()

    def boom(_e: Event) -> None:
        raise RuntimeError("nope")

    bus.subscribe(ProposalCreated, boom)
    # Must not raise.
    delivered = bus.publish(
        ProposalCreated(proposal_id="p", kind="trend", project_ref="AICC"),
        raise_errors=False,
    )
    assert delivered == 1


def test_publish_rejects_non_events() -> None:
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.publish(object())  # type: ignore[arg-type]


def test_subscribe_rejects_non_event_types() -> None:
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.subscribe(str, lambda e: None)  # type: ignore[arg-type]


def test_default_bus_is_a_shared_singleton() -> None:
    assert default_bus() is default_bus()
