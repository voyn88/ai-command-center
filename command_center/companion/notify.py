"""Companion Sync Service — notify: state-transition detection, an offline
notification queue, and per-device bidirectional sync (Phase C).

Importing this module has **no side effects** — same rule as the rest of this
package (see `command_center.companion.__init__`): no port is bound, no file
is written, nothing is scheduled. Every function here is called explicitly, at
a checkpoint the caller chooses, exactly like `command_center.execution_queue`
(see that module's docstring): "no hidden scheduler", "the caller is
responsible for calling ... at the checkpoints that matter". `notify` invents
no new checkpoint policy of its own.

Design, end to end:

- **Detection is deterministic and stateless.** `detect_transition` takes a
  run's *current* state and the *last state this module already notified for
  that run* (its watermark) and returns a notification dict, or `None` when
  nothing changed. It never re-derives "did this run change" from anything
  except those two values — no timestamp heuristics, no polling interval.
- **The watermark is server-side and global** (`data/companion_notify_watermark.json`,
  one row per run id): it exists purely to make `scan_and_enqueue` idempotent —
  calling it twice for the same run transition must append the notification
  once, not once per call. This is *not* per-device: every device should
  eventually see the same transition once, not once per device it happens to
  have already synced.
- **The queue is per-device and append-only** (`data/companion_notifications.jsonl`,
  one row per enqueued notification, monotonically increasing `seq`). Being
  offline is normal, not a failure mode: a device's queue does not truncate or
  expire while it is unreachable, so reconnecting after any amount of time
  yields every transition it missed, in order. This is what makes "state
  transitions delivered on network recovery" true by construction rather than
  by a retry heuristic.
- **Sync is bidirectional.** `pending_for_device` is the server→device leg
  (what a device has not yet been shown); `ack` is the device→server leg (an
  explicit, idempotent "I have received through `seq`"), stored per device in
  `data/companion_notify_cursor.json`. Nothing is popped or deleted on read —
  a device that dies mid-delivery and re-syncs without ever acking simply sees
  the same notifications again, which is the safe failure mode for a
  notification (a duplicate is a UI no-op; a silently dropped one is not).
- **Push transport is a caller-supplied hook, not a dependency of this
  module.** `scan_and_enqueue` accepts an optional `push_sender` callable
  invoked once per newly enqueued notification. No APNs/FCM credentials or
  transport exist in this codebase yet; a real sender is wired in later
  without this module's queue/watermark/cursor contract changing at all —
  exactly the "future background worker can reuse this unmodified" shape
  `execution_queue` already established for its own queue.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Callable, Iterable

from command_center import models, storage
from command_center.runtime import api as runtime_api

NOTIFICATIONS_FILE_NAME = "companion_notifications.jsonl"
WATERMARK_FILE_NAME = "companion_notify_watermark.json"
CURSOR_FILE_NAME = "companion_notify_cursor.json"
LOCK_FILE_NAME = "companion_notify.lock"
LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05

# Every run-state change is watermark-worthy (queued, running, and every
# terminal state alike) — a mobile client cares just as much that a run
# started as that it finished, and picking a subset here would be exactly the
# kind of second opinion about run state `adapters.py`'s docstring warns
# against ("the mobile client and the desktop cannot drift into disagreeing").
NOTIFIABLE_EVENT = "run_state_changed"


# --------------------------------------------------------------------------
# Paths / locking — same shape as `command_center.execution_queue`
# --------------------------------------------------------------------------


def _notifications_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / NOTIFICATIONS_FILE_NAME


def _watermark_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / WATERMARK_FILE_NAME


def _cursor_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / CURSOR_FILE_NAME


def _lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / LOCK_FILE_NAME


@contextlib.contextmanager
def notify_lock(root: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Cross-process mutual exclusion for the notify store's read-modify-write
    cycles (watermark advance + notification append; cursor advance). One lock
    guards all three files because `scan_and_enqueue` reads-then-writes both
    the watermark and the notification log in the same pass and must not be
    interleaved with a concurrent caller doing the same."""
    with storage.file_lock(_lock_path(root), timeout=timeout, poll_seconds=_LOCK_POLL_SECONDS):
        yield


# --------------------------------------------------------------------------
# Detection — pure, no I/O
# --------------------------------------------------------------------------


def detect_transition(run: dict, last_notified_state: str | None) -> dict | None:
    """One run's current state vs. the last state this module already
    notified for it. Returns a notification payload dict, or `None` when the
    state has not changed since the watermark (including the first time a run
    is seen and its current state is already what the caller expects, e.g. a
    replay).

    A `None` `last_notified_state` (a run never notified before) always
    produces a notification for the run's current state — a device that pairs
    after a run has already started must still learn where it stands, not
    just about future changes."""
    current_state = run.get("state")
    if current_state is None or current_state == last_notified_state:
        return None
    return {
        "type": NOTIFIABLE_EVENT,
        "run_id": run.get("id"),
        "task_id": run.get("task_id"),
        "project": run.get("project"),
        "state": current_state,
        "previous_state": last_notified_state,
        "created_at": models.iso_now(),
    }


# --------------------------------------------------------------------------
# Watermark — dedupes `scan_and_enqueue` across repeated calls
# --------------------------------------------------------------------------


def _load_watermark(root: Path) -> dict[str, str]:
    return storage.read_json(_watermark_path(root), {})


def _save_watermark(root: Path, watermark: dict[str, str]) -> None:
    storage.atomic_write_json(_watermark_path(root), watermark)


# --------------------------------------------------------------------------
# Notification log — append-only, one row per enqueued notification
# --------------------------------------------------------------------------


def _load_notifications(root: Path) -> list[dict]:
    storage.ensure_seeded_jsonl(_notifications_path(root))
    return storage.read_jsonl(_notifications_path(root))


def _next_seq(notifications: list[dict]) -> int:
    return 1 + max((record.get("seq", 0) for record in notifications), default=0)


def scan_and_enqueue(
    execution_center_api: runtime_api.ExecutionCenterAPI,
    *,
    root: Path,
    push_sender: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Detect every run whose state has changed since this function last saw
    it, and append one notification per change to the append-only log.

    Runs entirely inside `notify_lock`: the watermark read, the notification
    append, and the watermark write are one atomic cycle, so two concurrent
    callers (a manual refresh and a future poller) can never both observe the
    same transition as new and double-enqueue it.

    `push_sender`, when given, is invoked once per newly enqueued notification
    *after* it is durably appended — a push attempt that fails or is never
    wired up must never cost the notification its place in the offline queue,
    since the queue (not the push) is what guarantees eventual delivery."""
    with notify_lock(root):
        watermark = _load_watermark(root)
        notifications = _load_notifications(root)
        seq = _next_seq(notifications)
        newly_enqueued: list[dict] = []
        runs = execution_center_api.list_runs(limit=None)
        for run in runs:
            run_id = run.get("id")
            if run_id is None:
                continue
            notification = detect_transition(run, watermark.get(run_id))
            if notification is None:
                continue
            notification["seq"] = seq
            seq += 1
            notifications.append(notification)
            newly_enqueued.append(notification)
            watermark[run_id] = notification["state"]
        if newly_enqueued:
            for notification in newly_enqueued:
                storage.append_jsonl(_notifications_path(root), notification)
            _save_watermark(root, watermark)
        for notification in newly_enqueued:
            if push_sender is not None:
                push_sender(notification)
        return newly_enqueued


# --------------------------------------------------------------------------
# Per-device sync — the bidirectional leg
# --------------------------------------------------------------------------


def _load_cursors(root: Path) -> dict[str, int]:
    return storage.read_json(_cursor_path(root), {})


def _save_cursors(root: Path, cursors: dict[str, int]) -> None:
    storage.atomic_write_json(_cursor_path(root), cursors)


def pending_for_device(device_id: str, *, root: Path) -> list[dict]:
    """Every notification with `seq` greater than `device_id`'s last
    acknowledged `seq`, oldest first. Read-only: a device that calls this
    without ever acking sees the same backlog again next time, which is the
    intended "at least once" delivery guarantee for a device that was offline
    when the transitions happened (the network-recovery acceptance case)."""
    cursors = _load_cursors(root)
    last_acked = cursors.get(device_id, 0)
    notifications = _load_notifications(root)
    return [record for record in notifications if record.get("seq", 0) > last_acked]


def ack(device_id: str, *, up_to_seq: int, root: Path) -> int:
    """Record that `device_id` has received every notification through
    `up_to_seq`. Idempotent and monotonic: acking a `seq` at or below the
    device's current cursor is a no-op (never rewinds the cursor), so a
    duplicate or out-of-order ack from a flaky mobile link cannot resurrect
    already-delivered notifications. Returns the device's cursor after the
    call."""
    with notify_lock(root):
        cursors = _load_cursors(root)
        current = cursors.get(device_id, 0)
        if up_to_seq > current:
            cursors[device_id] = up_to_seq
            _save_cursors(root, cursors)
            return up_to_seq
        return current


def sync(
    device_id: str,
    *,
    execution_center_api: runtime_api.ExecutionCenterAPI,
    root: Path,
    push_sender: Callable[[dict], None] | None = None,
) -> list[dict]:
    """The one call a companion sync checkpoint needs: detect and enqueue any
    transitions that happened since the last sync (any device's), then return
    this device's full pending backlog (its own missed notifications,
    regardless of who else has already synced). The caller is expected to
    follow a successful delivery with `ack`; `sync` itself never advances the
    device's cursor, so a client that reads but does not yet ack has changed
    nothing and can safely retry."""
    scan_and_enqueue(execution_center_api, root=root, push_sender=push_sender)
    return pending_for_device(device_id, root=root)


def device_ids(*, root: Path) -> Iterable[str]:
    """Every device with a recorded cursor — i.e. every device that has acked
    at least once. Useful for diagnostics/tests; not required by `sync` or
    `ack` themselves, which take a `device_id` explicitly."""
    return list(_load_cursors(root).keys())
