"""A lightweight, synchronous, in-process event bus.

Design goals (W1-DATA-EVENTS): typed events, deterministic ordering, no
external broker, and trivially testable. Publishing is synchronous — when
:meth:`EventBus.publish` returns, every matching subscriber has already run —
so a test can publish an event and immediately assert on what the subscribers
did, with no polling, threads or sleeps.

Matching is by ``isinstance``: a subscriber registered for class ``C`` receives
every published event ``e`` for which ``isinstance(e, C)`` holds. Subscribing to
:class:`~command_center.events.types.Event` therefore receives *all* events.

Ordering is FIFO across a single registration list: subscribers are invoked in
the order they subscribed, regardless of which type each registered for. This
gives one obvious, testable order rather than a per-type ordering that depends
on dict iteration.

Error isolation: by default a subscriber that raises does not prevent the
remaining subscribers from running — every matching handler is invoked, and the
collected failures are raised together as an :class:`EventDispatchError` after
delivery completes. Pass ``raise_errors=False`` to swallow them (fire-and-forget
notification). This keeps one buggy consumer from silently dropping an event for
every other consumer.
"""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

from command_center.events.types import Event

_E = TypeVar("_E", bound=Event)

#: A subscriber is any callable taking one event. It is invoked with the exact
#: event instance that was published.
Subscriber = Callable[[Event], None]


class EventDispatchError(Exception):
    """Raised after :meth:`EventBus.publish` finishes delivering an event when
    one or more subscribers raised. Carries the individual exceptions so a
    caller (or test) can inspect every failure, not just the first."""

    def __init__(self, event: Event, errors: list[BaseException]) -> None:
        self.event = event
        self.errors = errors
        super().__init__(
            f"{len(errors)} subscriber(s) raised while dispatching "
            f"{type(event).__name__}: {[repr(e) for e in errors]}"
        )


class EventBus:
    """A synchronous publish/subscribe bus for :class:`Event` instances.

    Thread-safe registration and a snapshot-based dispatch: :meth:`publish`
    iterates a copy of the subscription list taken under the lock, so a
    subscriber may itself subscribe/unsubscribe (or publish) without mutating
    the list mid-iteration or deadlocking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # One ordered list of (event_type, subscriber). FIFO registration order
        # is the delivery order; matching is by isinstance against event_type.
        self._subscriptions: list[tuple[type[Event], Subscriber]] = []

    def subscribe(
        self, event_type: type[_E], handler: Callable[[_E], None]
    ) -> Callable[[], None]:
        """Register ``handler`` for every published event that is an instance of
        ``event_type``. Returns a zero-arg ``unsubscribe`` callable that removes
        exactly this registration (idempotent — calling it twice is harmless)."""
        if not (isinstance(event_type, type) and issubclass(event_type, Event)):
            raise TypeError("event_type must be an Event subclass")
        entry: tuple[type[Event], Subscriber] = (event_type, handler)  # type: ignore[assignment]
        with self._lock:
            self._subscriptions.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscriptions.remove(entry)
                except ValueError:
                    pass  # already removed — idempotent

        return unsubscribe

    def publish(self, event: Event, *, raise_errors: bool = True) -> int:
        """Deliver ``event`` synchronously to every matching subscriber, in
        registration order, and return the number of subscribers invoked.

        With ``raise_errors`` (the default), any subscriber exceptions are
        collected and re-raised together as :class:`EventDispatchError` *after*
        all matching subscribers have run — one failing consumer never robs the
        others. With ``raise_errors=False`` the failures are swallowed."""
        if not isinstance(event, Event):
            raise TypeError(f"can only publish Event instances, got {type(event)!r}")
        with self._lock:
            matching = [h for (etype, h) in self._subscriptions if isinstance(event, etype)]
        errors: list[BaseException] = []
        for handler in matching:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - isolate one bad subscriber
                errors.append(exc)
        if errors and raise_errors:
            raise EventDispatchError(event, errors)
        return len(matching)

    def clear(self) -> None:
        """Remove every subscription. Intended for test teardown so state never
        leaks between cases that share the default bus."""
        with self._lock:
            self._subscriptions.clear()

    def subscriber_count(self) -> int:
        """Total number of active registrations (across all types)."""
        with self._lock:
            return len(self._subscriptions)


#: The process-wide default bus the Wave-1 services publish onto. Tests can
#: subscribe to it and call :meth:`EventBus.clear` in teardown, or construct an
#: isolated :class:`EventBus` and inject it.
_DEFAULT_BUS = EventBus()


def default_bus() -> EventBus:
    """Return the process-wide default event bus."""
    return _DEFAULT_BUS
