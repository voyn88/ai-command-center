"""Companion Sync Service — notify: state-transition notifications, a
durable per-device offline queue, and bidirectional sync (Phase C of
`docs/mobile/API_REQUIREMENTS.md`, referenced by `command_center.companion`'s
package docstring).

`adapters.py` (M1A/M1B) lets a *connected* mobile client read current state.
It does not help a client that was offline while a task or run changed status:
polling `adapters.dashboard` again only shows where things ended up, never
what happened while the device was unreachable. This module closes that gap
with three pieces, each addressing one part of "push + offline queue +
bidirectional sync":

- `record_transition` — appends one durable, ordered notification event for a
  task/run state change. Like every module in this package, it is additive:
  it never recomputes a status itself, it only records one a caller already
  derived (`command_center.runtime.task_sync`, `command_center.execution_queue`,
  or a future caller), exactly as `adapters.py` never recomputes a snapshot.
- a durable **offline queue**: `data/companion_notifications.jsonl` (the
  append-only event log — never rewritten, only ever appended to, same
  convention as `data/runs.jsonl`) plus `data/companion_devices.json` (one
  small whole-file JSON registry of per-device cursors). Because the cursor is
  persisted, not held in memory, "the network came back" always resumes a
  device from exactly the sequence number it last acknowledged — even across a
  server restart — never from "whatever is current now".
- `sync` — the one **bidirectional** entrypoint: a device reports what it has
  already applied (`client_ack`) and receives every event still pending for
  it, in the same call. `deliver` is the **push** half: a best-effort attempt
  to hand pending events to a device-specific sender (APNs, FCM, a websocket
  push, a test double); on any failure the events are left completely
  untouched in the log, so the guarantee this task's acceptance criterion asks
  for — state transitions are delivered once the network comes back — holds
  by construction: delivery is re-derived from "everything after this
  device's durable cursor", never patched up by retry bookkeeping that could
  itself go stale.

Storage follows `command_center.execution_queue`'s precedent exactly: an
append-only JSONL log for history that must never be rewritten, a small
whole-file JSON registry for mutable per-device state, and
`command_center.storage.file_lock` held across each registry's entire
read-modify-write span (never only around the final write) so two concurrent
callers — two API workers, a poller and a request — cannot silently lose one
another's update.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from command_center import storage

NOTIFICATIONS_FILE_NAME = "companion_notifications.jsonl"
DEVICES_FILE_NAME = "companion_devices.json"


def _notifications_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / NOTIFICATIONS_FILE_NAME


def _notifications_lock_path(root: Path) -> Path:
    return _notifications_path(root).with_suffix(".lock")


def _devices_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / DEVICES_FILE_NAME


def _devices_lock_path(root: Path) -> Path:
    return _devices_path(root).with_suffix(".lock")


def _device_record(devices: dict, device_id: str) -> dict:
    """Return `device_id`'s registry entry, creating a fresh (cursor 0) one in
    `devices` if it is not already present. Mutates `devices` in place — every
    caller already holds `_devices_lock_path` and writes `devices` back."""
    return devices.setdefault(device_id, {"device_id": device_id, "cursor": 0})


def latest_seq(root: Path) -> int:
    """The highest `seq` recorded so far, or `0` when the log is empty."""
    events = storage.read_jsonl(_notifications_path(root))
    return events[-1]["seq"] if events else 0


def record_transition(
    entity_type: str,
    entity_id: str,
    *,
    from_status: str | None,
    to_status: str,
    at: str,
    payload: dict | None = None,
    root: Path,
) -> dict:
    """Append one durable notification event for a state transition a caller
    already derived, and return the stored record (with its assigned `seq`).

    `at` is the caller's own timestamp — this module never calls a clock, so a
    test drives it and the recorded event matches the transition it describes
    bit-for-bit, and two processes recording transitions at "the same moment"
    never disagree about what that moment was.

    `seq` is one past the highest `seq` already on disk: monotonic and
    gap-free, which is all a durable device cursor needs to answer "have I
    seen everything up to here". The read-then-append is done under
    `_notifications_lock_path` so two concurrent recorders can never compute
    the same next `seq`.
    """
    path = _notifications_path(root)
    with storage.file_lock(_notifications_lock_path(root)):
        existing = storage.read_jsonl(path)
        seq = (existing[-1]["seq"] + 1) if existing else 1
        record = {
            "seq": seq,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "from_status": from_status,
            "to_status": to_status,
            "at": at,
            "payload": payload or {},
        }
        storage.append_jsonl(path, record)
    return record


def register_device(device_id: str, *, root: Path, start_cursor: int | None = None) -> dict:
    """Register `device_id` for sync if it is not already known, and return
    its (possibly pre-existing) registry entry. Idempotent: re-registering an
    already-known device is a no-op that returns its current entry unchanged,
    so a client may call this on every app launch without resetting progress.

    A newly registered device defaults to starting at the *current* tip
    (`start_cursor=None` resolves to `latest_seq`) — matching a push
    subscription: pairing today should not flood a device with the entire
    pre-existing history. Pass `start_cursor=0` explicitly for a device that
    wants full history from the beginning.
    """
    with storage.file_lock(_devices_lock_path(root)):
        devices = storage.read_json(_devices_path(root), {})
        if device_id not in devices:
            cursor = latest_seq(root) if start_cursor is None else start_cursor
            devices[device_id] = {"device_id": device_id, "cursor": cursor}
            storage.atomic_write_json(_devices_path(root), devices)
        return devices[device_id]


def device_cursor(device_id: str, *, root: Path) -> int:
    """The device's durably persisted cursor, or `0` for an unregistered
    device (equivalent to "has seen nothing yet")."""
    devices = storage.read_json(_devices_path(root), {})
    return devices.get(device_id, {}).get("cursor", 0)


def pending_for_device(device_id: str, *, root: Path, limit: int = 100) -> list[dict]:
    """Every notification event `device_id` has not yet acknowledged, oldest
    first, capped at `limit`. This is the offline queue's read side: a device
    whose cursor is far behind because it was offline for an hour gets
    everything it missed, not just what happened since the last poll."""
    cursor = device_cursor(device_id, root=root)
    events = storage.read_jsonl(_notifications_path(root))
    pending = [event for event in events if event["seq"] > cursor]
    return pending[:limit]


def ack(device_id: str, *, seq: int, root: Path) -> dict:
    """Durably advance `device_id`'s cursor to `seq`, and return the updated
    registry entry. Never moves the cursor backwards: an out-of-order or
    duplicate acknowledgement from a flaky link cannot regress a cursor that
    has already advanced further."""
    with storage.file_lock(_devices_lock_path(root)):
        devices = storage.read_json(_devices_path(root), {})
        record = _device_record(devices, device_id)
        if seq > record["cursor"]:
            record["cursor"] = seq
        storage.atomic_write_json(_devices_path(root), devices)
        return record


def sync(
    device_id: str,
    *,
    root: Path,
    client_ack: int | None = None,
    limit: int = 100,
) -> dict:
    """The bidirectional sync entrypoint a future HTTP layer (M1D `api.py`)
    calls: a device reports what it has already applied (`client_ack`) and
    receives everything it is still missing, in one round trip.

    Order is deliberate: the acknowledgement is durably recorded *before*
    pending events are computed, so the client's own confirmation always wins
    over a concurrent notification — there is no window where recomputing
    `pending` from a stale cursor could hand back an event the client just
    acknowledged.

    Returns `{"events": [...], "cursor": <device's cursor after this call>}`.
    A device that went offline mid-session simply calls this again on
    reconnect with no `client_ack` (or its last-known one) and receives every
    transition it missed — the events never left the durable log, and the
    device's own cursor never advanced past what it actually received, which
    is exactly the guarantee this task's acceptance criterion asks for.
    """
    register_device(device_id, root=root)
    if client_ack is not None:
        ack(device_id, seq=client_ack, root=root)
    events = pending_for_device(device_id, root=root, limit=limit)
    return {"events": events, "cursor": device_cursor(device_id, root=root)}


def deliver(
    device_id: str,
    *,
    root: Path,
    sender: Callable[[list[dict]], bool],
    limit: int = 100,
) -> bool:
    """The push half: best-effort delivery of every event still pending for
    `device_id` via `sender` (an APNs/FCM/websocket adapter, or a test
    double). Returns whether `sender` reported success; never raises.

    Deliberately does **not** advance the device's cursor on success: a push
    is a *notification* that something changed, not an authoritative receipt,
    exactly like a phone that shows a banner but only marks itself caught up
    once the app is opened and calls `sync`. Advancing the cursor here would
    let a push that a device dismissed without reading silently mark a
    transition as delivered.

    On failure — `sender` raises, or returns a falsy value, the network-down
    case this task's acceptance criterion is about — the events are left
    completely untouched in the durable log. Nothing here retries, backs off,
    or marks anything as failed: the very next call (to `deliver`, or to
    `sync` once the device reconnects) re-derives the same "pending since
    cursor" set from scratch, so a network outage of any length is harmless by
    construction rather than by a retry policy that itself has to be correct.
    """
    events = pending_for_device(device_id, root=root, limit=limit)
    if not events:
        return True
    try:
        return bool(sender(events))
    except Exception:
        return False
