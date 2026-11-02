"""The tick-stall watchdog (VOYN-W0-AICC-TICK-STALL-WATCHDOG).

The review/merge ticks in :mod:`command_center.orchestrator.review_merge` are
refusal-as-data: every task they decline to act on comes back as a
``(task_id, reason)`` skip. Until this module, those skips were only printed
— so the pathological pattern where the SAME task repeats the SAME reason
tick after tick (a verdict-less review chunk, a quota-dead chunk, a
stale-head verdict; all three found live on 2026-09-07 on PRs #774, #707 and
#766 by a human running ad-hoc ssh monitor loops) had no watcher that
survives a terminal.

Three pieces, all data:

- :func:`record_skips` — called by the tick CLI handlers at the point they
  print SKIP lines, persisting each skip as a ``tick_skip_event`` row keyed
  by a per-invocation ``tick_id``. Journald parsing was rejected: the
  journal is an operator convenience, and a detector built on log-line
  shapes breaks silently when a print statement is reworded.
- :func:`detect_stalls` — a pure function over the ordered tick history.
  An episode is a ``(task_id, reason)`` pair present in every one of the
  most recent N ticks of one kind (default 5). A tick where the pair is
  absent — the task moved on, was examined with a different reason, or
  simply left the scan window — resets the streak.
- :func:`watchdog_once` — the ``backlog-watchdog`` tick. For every detected
  episode it escalates AS DATA, exactly once per episode: it claims a row in
  the ``tick_stall_escalation`` ledger (UNIQUE on the episode identity, so a
  second run over the same episode cannot duplicate) and files a new OPEN
  backlog task through the existing ``backlog_upsert_task`` protocol — the
  same inbox ``_remediate_rejection`` already uses for machine-created
  follow-up work, so the escalation flows through the ordinary
  OPEN -> ... -> DONE pipeline with no new dispatch code. The watchdog never
  mutates the stalled items themselves: its whole write surface is the
  ledger and the new escalation task.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "StallEpisode",
    "TickSnapshot",
    "WatchdogConfig",
    "WatchdogReport",
    "detect_stalls",
    "escalation_task_id",
    "new_tick_id",
    "record_skips",
    "watchdog_once",
]


def new_tick_id() -> str:
    """One identity per tick invocation, shared by all its skip rows."""
    return uuid.uuid4().hex


def record_skips(
    conn: Any, tick_kind: str, tick_id: str, skipped: Iterable[tuple[str, str]]
) -> int:
    """Persist one tick's ``(task_id, reason)`` skips as ``tick_skip_event`` rows.

    Deduplicates within the tick: the chunked-review path can report the same
    pair more than once per invocation, and one tick contributes at most one
    row per pair to the streak arithmetic by definition.
    """
    pairs = sorted(set(skipped))
    if not pairs:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO tick_skip_event (tick_id, tick_kind, task_id, reason) "
            "VALUES (%s, %s, %s, %s)",
            [(tick_id, tick_kind, task_id, reason) for task_id, reason in pairs],
        )
    return len(pairs)


@dataclass(frozen=True, slots=True)
class TickSnapshot:
    """One tick of one kind: its identity, when it ran, and what it skipped."""

    tick_id: str
    observed_at: datetime
    pairs: frozenset[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class StallEpisode:
    """A (task, reason) pair skipped in >= threshold consecutive ticks."""

    tick_kind: str
    task_id: str
    reason: str
    first_tick_id: str
    consecutive: int
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    #: Consecutive same-reason ticks before a stall is escalated.
    threshold: int = 5
    #: How many recent ticks per kind the detector reads. Bounded so the
    #: watchdog's cost is a property of the window, not of table growth.
    window: int = 50


@dataclass(slots=True)
class WatchdogReport:
    #: (episode, escalation_task_id) — the ledger row was claimed by THIS run.
    escalated: list[tuple[StallEpisode, str]] = field(default_factory=list)
    #: Episodes already escalated by an earlier run (idempotent second pass).
    already_escalated: list[StallEpisode] = field(default_factory=list)
    #: (task_id, reason) — an escalation the ledger claimed but the upsert
    #: refused; surfaced rather than swallowed.
    refused: list[tuple[str, str]] = field(default_factory=list)


def detect_stalls(
    tick_kind: str, ticks: Sequence[TickSnapshot], *, threshold: int = 5
) -> list[StallEpisode]:
    """Episodes in ``ticks`` (ordered oldest -> newest) at ``threshold``.

    Only streaks that reach the NEWEST tick qualify: a streak that already
    broke resolved itself, and escalating it would page about the past. The
    streak for a pair is the run of trailing ticks that all contain it; a
    tick where the task appears with a different reason does not contain the
    pair and therefore resets it, which is exactly the "interleaved reasons
    are progress, not a stall" rule.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be >= 1, got {threshold}")
    if not ticks:
        return []
    newest = ticks[-1]
    episodes: list[StallEpisode] = []
    for task_id, reason in sorted(newest.pairs):
        first_index = len(ticks) - 1
        while first_index > 0 and (task_id, reason) in ticks[first_index - 1].pairs:
            first_index -= 1
        consecutive = len(ticks) - first_index
        if consecutive < threshold:
            continue
        episodes.append(
            StallEpisode(
                tick_kind=tick_kind,
                task_id=task_id,
                reason=reason,
                first_tick_id=ticks[first_index].tick_id,
                consecutive=consecutive,
                first_seen_at=ticks[first_index].observed_at,
                last_seen_at=newest.observed_at,
            )
        )
    return episodes


def escalation_task_id(episode: StallEpisode) -> str:
    """Deterministic id for the episode's escalation task.

    Derived from the episode identity so two watchdog runs racing over the
    same episode compute the same id — the backlog upsert is then idempotent
    even before the ledger's UNIQUE constraint settles who claimed it. The
    first-tick prefix keeps a recurring stall's NEW episode distinct from the
    task filed for its previous one.
    """
    return f"{episode.task_id}-STALL-{episode.first_tick_id[:8]}"


def _load_ticks(conn: Any, window: int) -> dict[str, list[TickSnapshot]]:
    """The last ``window`` ticks per kind, each with its skip-pair set."""
    with conn.cursor() as cur:
        # Two steps so the read stays bounded by the window rather than by
        # table growth: first the identities of the last `window` ticks per
        # kind, then only those ticks' rows.
        cur.execute(
            "SELECT tick_kind, tick_id, started_at FROM ("
            "  SELECT tick_kind, tick_id, min(observed_at) AS started_at,"
            "         row_number() OVER (PARTITION BY tick_kind"
            "                            ORDER BY min(observed_at) DESC, tick_id DESC)"
            "         AS recency"
            "    FROM tick_skip_event GROUP BY tick_kind, tick_id) latest"
            " WHERE recency <= %s",
            (window,),
        )
        selected = cur.fetchall()
        if not selected:
            return {}
        cur.execute(
            "SELECT tick_kind, tick_id, task_id, reason FROM tick_skip_event"
            " WHERE tick_id = ANY(%s)",
            ([tick_id for _kind, tick_id, _started in selected],),
        )
        rows = cur.fetchall()
    pairs_by_tick: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for tick_kind, tick_id, task_id, reason in rows:
        pairs_by_tick.setdefault((tick_kind, tick_id), set()).add((task_id, reason))
    result: dict[str, list[TickSnapshot]] = {}
    for tick_kind, tick_id, started_at in sorted(
        selected, key=lambda row: (row[0], row[2], row[1])
    ):
        result.setdefault(tick_kind, []).append(
            TickSnapshot(
                tick_id=tick_id,
                observed_at=started_at,
                pairs=frozenset(pairs_by_tick.get((tick_kind, tick_id), set())),
            )
        )
    return result


_ESCALATION_BODY = (
    "Tick-stall watchdog escalation (VOYN-W0-AICC-TICK-STALL-WATCHDOG).\n\n"
    "The {tick_kind} tick skipped task {task_id} with the same reason for "
    "{consecutive} consecutive ticks:\n\n"
    "- reason: {reason}\n"
    "- first seen: {first_seen_at:%Y-%m-%d %H:%M:%S%z} (tick {first_tick_id})\n"
    "- last seen: {last_seen_at:%Y-%m-%d %H:%M:%S%z}\n\n"
    "A repeating identical skip means the tick will never make progress on "
    "this task without intervention. Investigate why the condition persists "
    "and fix the underlying cause; the watchdog never touches the stalled "
    "item itself. The stalled task's own record and history are unchanged "
    "and addressable under its task_id."
)


def _escalate(conn: Any, episode: StallEpisode) -> tuple[str, str]:
    """Escalate one episode; returns ('escalated'|'already'|'refused', detail).

    The ledger insert is the idempotency gate: ``ON CONFLICT DO NOTHING`` on
    the episode's UNIQUE identity means exactly one run claims it. The claim
    and the task upsert are separate autocommit statements, so a crash
    between them could leave a claimed episode without its task — handled by
    re-upserting (deterministic task_id, same content) when a later run finds
    the ledger row but not the task.
    """
    from contextlib import nullcontext

    from command_center.db.backlog_parser import ParsedTask
    from command_center.db.backlog_store import BacklogStore

    new_task_id = escalation_task_id(episode)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tick_stall_escalation"
            " (tick_kind, task_id, reason, first_tick_id, consecutive_ticks,"
            "  first_seen_at, last_seen_at, escalation_task_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT ON CONSTRAINT tick_stall_episode_once DO NOTHING"
            " RETURNING id",
            (
                episode.tick_kind,
                episode.task_id,
                episode.reason,
                episode.first_tick_id,
                episode.consecutive,
                episode.first_seen_at,
                episode.last_seen_at,
                new_task_id,
            ),
        )
        claimed = cur.fetchone() is not None
        cur.execute(
            "SELECT wave, priority, repo FROM backlog_task WHERE task_id = %s",
            (episode.task_id,),
        )
        stalled = cur.fetchone()
        if not claimed:
            cur.execute(
                "SELECT 1 FROM backlog_task WHERE task_id = %s", (new_task_id,)
            )
            if cur.fetchone() is not None:
                return "already", new_task_id
            # Claimed by a run that died before its upsert: self-heal below.
    wave, priority, repo = stalled if stalled is not None else ("0", None, None)
    title = f"Stall: {episode.tick_kind} tick looping on {episode.task_id}"
    body = _ESCALATION_BODY.format(
        tick_kind=episode.tick_kind,
        task_id=episode.task_id,
        consecutive=episode.consecutive,
        reason=episode.reason,
        first_seen_at=episode.first_seen_at,
        first_tick_id=episode.first_tick_id,
        last_seen_at=episode.last_seen_at,
    )
    store = BacklogStore(lambda: nullcontext(conn))
    ok, reason, _changed = store.upsert_task(
        ParsedTask(
            task_id=new_task_id, wave=wave, priority=priority, status="OPEN",
            kind="task", title=title, body=body, repo=repo, line_no=0,
        )
    )
    if not ok:
        return "refused", reason
    return "escalated", new_task_id


def watchdog_once(factory: Any, config: WatchdogConfig | None = None) -> WatchdogReport:
    """One watchdog tick: detect stall episodes, escalate each exactly once."""
    config = config or WatchdogConfig()
    report = WatchdogReport()
    with factory() as conn:
        for tick_kind, ticks in sorted(_load_ticks(conn, config.window).items()):
            for episode in detect_stalls(tick_kind, ticks, threshold=config.threshold):
                outcome, detail = _escalate(conn, episode)
                if outcome == "escalated":
                    report.escalated.append((episode, detail))
                elif outcome == "already":
                    report.already_escalated.append(episode)
                else:
                    report.refused.append((episode.task_id, detail))
    return report
