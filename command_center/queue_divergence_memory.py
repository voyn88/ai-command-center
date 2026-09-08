"""Windowed memory of ADR 0007 queue-divergence checks (VOYN-W0-AICC-SRV-07c).

`execution_queue.queue_divergence` recomputes fresh on every pipeline tick and
is never itself persisted — see that function's docstring. That is fine for
"is the queue correct right now" (it always is; JSON stays authoritative), but
ADR 0007 gates step 4 ("stop writing JSON") on "a session with no divergence
logged", which is a claim about a *window* of time, not one tick. Before this
module, a divergence that appeared on tick N and cleared by tick N+1 left no
trace: the operator would see the panel's "0" and have no way to know
something had disagreed ten minutes earlier.

This module records every tick's check into `runtime.db`
(`queue_divergence_check`, migration 25) and summarizes the rolling window,
pruning rows older than the window on every call so the table never grows
unbounded — the same "windowed, not append-forever" shape as
`command_center.runtime.db.apply_runtime_retention`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from command_center import models
from command_center.runtime import db as runtime_db

# ADR 0007 step 4 is gated on "a session with no divergence"; a session is
# read here as one working day, matching the ADR's own phrasing for the
# backfill-verification phase ("runs for at least one full working session").
DEFAULT_WINDOW_HOURS = 24


@dataclass(frozen=True)
class DivergenceWindowSummary:
    """What the panel needs to answer "has this session been clean" — not
    just the current tick's count, but whether *any* tick in the window saw
    one, and when the most recent of those was."""

    window_hours: int
    checks: int
    divergent_checks: int
    total_divergences: int
    last_divergence_at: str | None

    @property
    def clean(self) -> bool:
        return self.divergent_checks == 0

    def as_dict(self) -> dict:
        return {
            "window_hours": self.window_hours,
            "checks": self.checks,
            "divergent_checks": self.divergent_checks,
            "total_divergences": self.total_divergences,
            "last_divergence_at": self.last_divergence_at,
            "clean": self.clean,
        }


def _empty(window_hours: int) -> DivergenceWindowSummary:
    return DivergenceWindowSummary(
        window_hours=window_hours,
        checks=0,
        divergent_checks=0,
        total_divergences=0,
        last_divergence_at=None,
    )


def record_and_summarize(
    root: Path,
    divergence: list[dict],
    *,
    db_path: Path | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: str | None = None,
) -> DivergenceWindowSummary:
    """Record this tick's `queue_divergence()` result and return the summary
    over the last `window_hours`.

    Read-only on failure: remembering the divergence must never be able to
    break the tick it is observing, so any exception here is swallowed and the
    caller gets an empty (clean-looking) summary — the same posture
    `queue_divergence` itself takes toward the mirror it reads, and safe here
    because the per-tick `queue_divergence` warning (computed independently)
    still fires regardless of whether this memory could be written."""
    checked_at = now or models.iso_now()
    resolved_db_path = db_path or runtime_db.resolve_db_path(root)
    try:
        runtime_db.record_queue_divergence_check(
            resolved_db_path, checked_at=checked_at, divergence=divergence
        )
        cutoff = (
            datetime.fromisoformat(checked_at) - timedelta(hours=window_hours)
        ).isoformat(timespec="seconds")
        runtime_db.prune_queue_divergence_checks(resolved_db_path, before=cutoff)
        rows = runtime_db.list_queue_divergence_checks(resolved_db_path, since=cutoff)
    except Exception:  # noqa: BLE001 — see docstring
        return _empty(window_hours)

    divergent = [row for row in rows if row["divergence_count"] > 0]
    return DivergenceWindowSummary(
        window_hours=window_hours,
        checks=len(rows),
        divergent_checks=len(divergent),
        total_divergences=sum(row["divergence_count"] for row in divergent),
        last_divergence_at=divergent[-1]["checked_at"] if divergent else None,
    )
