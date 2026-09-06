"""Per-device sliding-window rate limiter (in-process).

v1 runs as a single process, so an in-memory window is sufficient and keeps
the gateway free of shared infrastructure.  If the gateway is ever scaled
horizontally this module is the seam to replace — the interface (device id in,
allow/deny + retry-after out) stays.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_requests: int, window_s: int) -> None:
        self._max = max(1, max_requests)
        self._window = max(1, window_s)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, device_id: str, now: float | None = None) -> int | None:
        """Record a hit; return None if allowed, else seconds until retry."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(device_id, deque())
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if len(hits) >= self._max:
                return max(1, int(self._window - (now - hits[0])) + 1)
            hits.append(now)
            return None
