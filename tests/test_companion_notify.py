"""Companion Sync Service — notify (Phase C, VOYN-W0-F5).

The acceptance criterion this suite defends: state transitions are delivered
once the network comes back. That decomposes into three properties, one per
test group below —

  * a state transition, once recorded, is never lost regardless of how many
    devices have or have not seen it yet (the durable offline queue);
  * a device that was offline resumes from exactly where it left off, not
    from "now" — even across a fresh read of on-disk state (bidirectional
    sync via a persisted cursor);
  * a failed push attempt (the network-down case) leaves the queue untouched,
    so the next successful attempt redelivers everything that was missed.
"""

from __future__ import annotations

from command_center.companion import notify


# --------------------------------------------------------------------------
# record_transition: a durable, ordered log
# --------------------------------------------------------------------------


def test_record_transition_assigns_gap_free_monotonic_seq(tmp_path):
    first = notify.record_transition(
        "task", "t1", from_status="Backlog", to_status="Ready", at="2026-01-01T00:00:00", root=tmp_path
    )
    second = notify.record_transition(
        "task", "t1", from_status="Ready", to_status="In Progress", at="2026-01-01T00:05:00", root=tmp_path
    )
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert notify.latest_seq(tmp_path) == 2


def test_record_transition_persists_across_a_fresh_read(tmp_path):
    notify.record_transition(
        "run", "r1", from_status="queued", to_status="running", at="t0", root=tmp_path
    )
    # A brand-new call reads only what is on disk -- no in-process cache.
    assert notify.latest_seq(tmp_path) == 1


# --------------------------------------------------------------------------
# The durable per-device offline queue and bidirectional sync
# --------------------------------------------------------------------------


def test_a_newly_registered_device_starts_at_the_current_tip(tmp_path):
    """Pairing today must not flood a device with all pre-existing history."""
    notify.record_transition("task", "t1", from_status=None, to_status="Backlog", at="t0", root=tmp_path)
    device = notify.register_device("phone-1", root=tmp_path)
    assert device["cursor"] == 1
    assert notify.pending_for_device("phone-1", root=tmp_path) == []


def test_register_device_is_idempotent(tmp_path):
    notify.record_transition("task", "t1", from_status=None, to_status="Backlog", at="t0", root=tmp_path)
    notify.register_device("phone-1", root=tmp_path)
    notify.ack("phone-1", seq=1, root=tmp_path)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)
    # Re-registering must not reset the cursor back to the (now stale) tip.
    again = notify.register_device("phone-1", root=tmp_path)
    assert again["cursor"] == 1
    assert [e["seq"] for e in notify.pending_for_device("phone-1", root=tmp_path)] == [2]


def test_an_offline_device_receives_every_transition_it_missed(tmp_path):
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)
    notify.record_transition("task", "t1", from_status="Ready", to_status="In Progress", at="t2", root=tmp_path)
    notify.record_transition("task", "t1", from_status="In Progress", to_status="Done", at="t3", root=tmp_path)

    pending = notify.pending_for_device("phone-1", root=tmp_path)
    assert [e["to_status"] for e in pending] == ["Ready", "In Progress", "Done"]


def test_sync_is_bidirectional_ack_then_pull_in_one_call(tmp_path):
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)
    first = notify.sync("phone-1", root=tmp_path)
    assert [e["to_status"] for e in first["events"]] == ["Ready"]
    assert first["cursor"] == 0  # not yet acknowledged

    notify.record_transition("task", "t1", from_status="Ready", to_status="Done", at="t2", root=tmp_path)
    second = notify.sync("phone-1", root=tmp_path, client_ack=1)
    assert second["cursor"] == 1
    assert [e["to_status"] for e in second["events"]] == ["Done"]


def test_a_client_ack_wins_over_a_concurrent_notification(tmp_path):
    """The ack is durably recorded before pending is recomputed: a transition
    recorded in between must never be handed back as if unacknowledged when it
    is already covered by the ack the client just sent."""
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)
    result = notify.sync("phone-1", root=tmp_path, client_ack=1)
    assert result["events"] == []
    assert result["cursor"] == 1


def test_ack_never_moves_the_cursor_backwards(tmp_path):
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.ack("phone-1", seq=5, root=tmp_path)
    notify.ack("phone-1", seq=2, root=tmp_path)
    assert notify.device_cursor("phone-1", root=tmp_path) == 5


def test_an_unregistered_device_has_cursor_zero(tmp_path):
    assert notify.device_cursor("ghost", root=tmp_path) == 0


def test_pending_is_capped_by_limit(tmp_path):
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    for i in range(5):
        notify.record_transition("task", "t1", from_status=str(i), to_status=str(i + 1), at=f"t{i}", root=tmp_path)
    assert len(notify.pending_for_device("phone-1", root=tmp_path, limit=2)) == 2


# --------------------------------------------------------------------------
# deliver: push, and the offline-queue survival guarantee
# --------------------------------------------------------------------------


def test_deliver_hands_pending_events_to_the_sender(tmp_path):
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)
    received = []
    ok = notify.deliver("phone-1", root=tmp_path, sender=lambda events: received.append(events) or True)
    assert ok is True
    assert [e["to_status"] for e in received[0]] == ["Ready"]


def test_deliver_success_does_not_itself_advance_the_cursor(tmp_path):
    """A push is a notification, not a receipt: the device only counts itself
    caught up once it explicitly `sync`s and acks."""
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)
    notify.deliver("phone-1", root=tmp_path, sender=lambda events: True)
    assert notify.device_cursor("phone-1", root=tmp_path) == 0


def test_a_network_down_delivery_failure_leaves_the_queue_untouched(tmp_path):
    """The core acceptance criterion: a state transition recorded while a
    device is offline (sender returns False, simulating no connectivity) must
    still be there, unchanged, on the very next delivery attempt."""
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)

    failed = notify.deliver("phone-1", root=tmp_path, sender=lambda events: False)
    assert failed is False
    assert [e["to_status"] for e in notify.pending_for_device("phone-1", root=tmp_path)] == ["Ready"]

    # The network "comes back": a later attempt with a working sender redelivers
    # the very same event, and only then does the client ack it via sync.
    received = []
    recovered = notify.deliver("phone-1", root=tmp_path, sender=lambda events: received.append(events) or True)
    assert recovered is True
    assert [e["to_status"] for e in received[0]] == ["Ready"]
    notify.sync("phone-1", root=tmp_path, client_ack=received[0][-1]["seq"])
    assert notify.pending_for_device("phone-1", root=tmp_path) == []


def test_a_sender_exception_is_treated_as_delivery_failure(tmp_path):
    """A raising sender (a raised `ConnectionError`, say) must not crash the
    caller and must not lose the event, exactly like a returned `False`."""
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    notify.record_transition("task", "t1", from_status="Backlog", to_status="Ready", at="t1", root=tmp_path)

    def _raises(events):
        raise ConnectionError("offline")

    assert notify.deliver("phone-1", root=tmp_path, sender=_raises) is False
    assert len(notify.pending_for_device("phone-1", root=tmp_path)) == 1


def test_deliver_with_nothing_pending_reports_success_without_calling_sender(tmp_path):
    notify.register_device("phone-1", root=tmp_path, start_cursor=0)
    calls = []
    assert notify.deliver("phone-1", root=tmp_path, sender=lambda events: calls.append(events)) is True
    assert calls == []
