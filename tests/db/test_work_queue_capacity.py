"""Capacity-wait defaults that keep quota outages out of queue dead letters."""

from __future__ import annotations

from command_center.db.work_queue_store import (
    DEFAULT_MAX_CAPACITY_WAITS,
    ClaimedWork,
    WorkQueueStore,
)


def test_capacity_wait_default_covers_the_monitor_health_window() -> None:
    """A 24-wait cap lasts only 22h45m and can reopen the finding too soon.

    The worker passes this value explicitly, so this small seam test protects
    the production route without requiring a PostgreSQL server.
    """
    calls: list[tuple[str, tuple[object, ...]]] = []

    class RecordingStore(WorkQueueStore):
        def _call(self, statement: str, params: tuple[object, ...]):  # type: ignore[override]
            calls.append((statement, params))
            return {"ok": True}

    work = ClaimedWork("item", "attempt", 1, "later", {}, "claim-token")
    assert RecordingStore().fail_capacity_wait(work, reason="usage limit") is True

    assert calls == [
        (
            "SELECT * FROM queue_fail_capacity_wait(%s, %s, %s, %s)",
            ("attempt", "claim-token", "usage limit", DEFAULT_MAX_CAPACITY_WAITS),
        )
    ]
    assert DEFAULT_MAX_CAPACITY_WAITS == 26

    # 15m, 30m, then 24 hour-long waits: the 27th refusal is the first that
    # can dead-letter the item, after 24h45m of scheduled capacity waiting.
    scheduled_seconds = 15 * 60 + 30 * 60 + (DEFAULT_MAX_CAPACITY_WAITS - 2) * 60 * 60
    assert scheduled_seconds > 24 * 60 * 60
