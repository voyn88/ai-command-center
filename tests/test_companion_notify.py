"""Companion Sync Service — notify (Phase C): state-transition detection, the
offline per-device notification queue, and bidirectional sync.

The contract these tests defend:

  * detection is a pure function of (current run state, last-notified state) —
    no timestamps, no polling interval;
  * `scan_and_enqueue` is idempotent — calling it twice for the same
    transition enqueues it once;
  * a device's pending backlog survives it being offline for any length of
    time, and is never truncated by anything except its own `ack` (the
    "state transitions are delivered on network recovery" acceptance case);
  * `ack` is monotonic and idempotent, so a duplicate or stale ack from a
    flaky mobile link can never resurrect already-delivered notifications.
"""

from __future__ import annotations

import pytest

from command_center.companion import notify
from command_center.runtime import api as runtime_api


@pytest.fixture
def api(tmp_path):
    return runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")


def _run(run_id="r1", state="QUEUED", task_id="t1", project="AIOS"):
    return {"id": run_id, "task_id": task_id, "project": project, "state": state}


# --------------------------------------------------------------------------
# Detection: pure, no I/O
# --------------------------------------------------------------------------


def test_no_notification_when_state_is_unchanged():
    assert notify.detect_transition(_run(state="RUNNING"), "RUNNING") is None


def test_notification_on_first_sighting_of_a_run():
    """A device pairing after a run already started must still learn where it
    stands, not just about future changes."""
    result = notify.detect_transition(_run(state="RUNNING"), None)
    assert result is not None
    assert result["state"] == "RUNNING"
    assert result["previous_state"] is None


def test_notification_on_a_real_transition():
    result = notify.detect_transition(_run(state="COMPLETED"), "RUNNING")
    assert result["state"] == "COMPLETED"
    assert result["previous_state"] == "RUNNING"
    assert result["run_id"] == "r1"
    assert result["task_id"] == "t1"
    assert result["project"] == "AIOS"


# --------------------------------------------------------------------------
# scan_and_enqueue: idempotent, watermark-backed
# --------------------------------------------------------------------------


def test_scan_and_enqueue_appends_one_notification_per_new_transition(tmp_path, api, monkeypatch):
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED"), _run("r2", "RUNNING")])
    enqueued = notify.scan_and_enqueue(api, root=tmp_path)
    assert {n["run_id"] for n in enqueued} == {"r1", "r2"}
    assert [n["seq"] for n in enqueued] == [1, 2]


def test_scan_and_enqueue_is_idempotent_across_repeated_calls(tmp_path, api, monkeypatch):
    """Calling it twice with no underlying change must not double-enqueue —
    the watermark is exactly what prevents that."""
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED")])
    first = notify.scan_and_enqueue(api, root=tmp_path)
    second = notify.scan_and_enqueue(api, root=tmp_path)
    assert len(first) == 1
    assert second == []


def test_scan_and_enqueue_detects_a_later_transition_after_the_first_scan(tmp_path, api, monkeypatch):
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED")])
    notify.scan_and_enqueue(api, root=tmp_path)

    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "RUNNING")])
    second = notify.scan_and_enqueue(api, root=tmp_path)
    assert len(second) == 1
    assert second[0]["previous_state"] == "QUEUED"
    assert second[0]["state"] == "RUNNING"
    assert second[0]["seq"] == 2


def test_push_sender_is_called_once_per_newly_enqueued_notification(tmp_path, api, monkeypatch):
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED"), _run("r2", "RUNNING")])
    sent = []
    notify.scan_and_enqueue(api, root=tmp_path, push_sender=sent.append)
    assert len(sent) == 2


def test_push_sender_failure_does_not_break_the_offline_queue(tmp_path, api, monkeypatch):
    """A push attempt is best-effort; the queue is the actual delivery
    guarantee and must not be lost if the push transport raises or is a
    broken stub."""
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED")])

    def _boom(_notification):
        raise RuntimeError("no transport configured")

    with pytest.raises(RuntimeError):
        notify.scan_and_enqueue(api, root=tmp_path, push_sender=_boom)
    # The notification is already durably appended before push is attempted.
    assert notify.pending_for_device("device-1", root=tmp_path) != []


# --------------------------------------------------------------------------
# Per-device sync: bidirectional (pending + ack)
# --------------------------------------------------------------------------


def test_a_new_device_sees_the_full_backlog(tmp_path, api, monkeypatch):
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED"), _run("r2", "RUNNING")])
    pending = notify.sync("device-1", execution_center_api=api, root=tmp_path)
    assert len(pending) == 2


def test_state_transitions_missed_while_offline_are_delivered_on_reconnect(tmp_path, api, monkeypatch):
    """The literal acceptance criterion: a device offline through several
    transitions must receive all of them, in order, the next time it syncs —
    regardless of how much time passed."""
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED")])
    notify.sync("device-1", execution_center_api=api, root=tmp_path)
    notify.ack("device-1", up_to_seq=1, root=tmp_path)

    # Device goes offline. Several transitions happen while it is unreachable.
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "RUNNING")])
    notify.scan_and_enqueue(api, root=tmp_path)
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "COMPLETED")])
    notify.scan_and_enqueue(api, root=tmp_path)

    # Network recovers: the device syncs again.
    pending = notify.sync("device-1", execution_center_api=api, root=tmp_path)
    assert [p["state"] for p in pending] == ["RUNNING", "COMPLETED"]


def test_a_second_device_gets_its_own_independent_backlog(tmp_path, api, monkeypatch):
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED")])
    notify.sync("device-1", execution_center_api=api, root=tmp_path)
    notify.ack("device-1", up_to_seq=1, root=tmp_path)

    # device-2 has never synced before — it must still see r1's notification,
    # even though device-1 already acked it.
    pending = notify.sync("device-2", execution_center_api=api, root=tmp_path)
    assert len(pending) == 1


def test_sync_does_not_itself_advance_the_cursor(tmp_path, api, monkeypatch):
    """Reading is not acking: a client that fetches but crashes before acking
    must see the same backlog again next time."""
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED")])
    first = notify.sync("device-1", execution_center_api=api, root=tmp_path)
    second = notify.sync("device-1", execution_center_api=api, root=tmp_path)
    assert first == second


def test_ack_is_monotonic_and_never_rewinds(tmp_path):
    assert notify.ack("device-1", up_to_seq=5, root=tmp_path) == 5
    # A stale/duplicate ack for an earlier seq must not roll the cursor back.
    assert notify.ack("device-1", up_to_seq=2, root=tmp_path) == 5


def test_ack_advances_and_shrinks_the_pending_backlog(tmp_path, api, monkeypatch):
    monkeypatch.setattr(api, "list_runs", lambda **kw: [_run("r1", "QUEUED"), _run("r2", "RUNNING")])
    pending = notify.sync("device-1", execution_center_api=api, root=tmp_path)
    assert len(pending) == 2

    notify.ack("device-1", up_to_seq=pending[0]["seq"], root=tmp_path)
    remaining = notify.pending_for_device("device-1", root=tmp_path)
    assert len(remaining) == 1
    assert remaining[0]["run_id"] == "r2"


def test_device_ids_reflects_devices_that_have_acked(tmp_path):
    assert list(notify.device_ids(root=tmp_path)) == []
    notify.ack("device-1", up_to_seq=1, root=tmp_path)
    assert list(notify.device_ids(root=tmp_path)) == ["device-1"]


# --------------------------------------------------------------------------
# Package remains inert on import
# --------------------------------------------------------------------------


def test_importing_notify_binds_no_port():
    import importlib
    import socket

    before = socket.socket.bind
    module = importlib.import_module("command_center.companion.notify")
    importlib.reload(module)
    assert socket.socket.bind is before
