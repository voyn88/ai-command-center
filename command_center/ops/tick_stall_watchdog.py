"""The tick-stall watchdog (VOYN-W0-AICC-TICK-STALL-WATCHDOG).

The review/merge ticks in :mod:`command_center.orchestrator.review_merge` are
refusal-as-data: every task they decline to act on comes back as a
``(task_id, reason)`` skip. Until this module, those skips were only printed
— so the pathological pattern where the SAME task repeats the SAME reason
tick after tick (a verdict-less review chunk, a quota-dead chunk, a
stale-head verdict; all three found live on 2026-09-07 on PRs #774, #707 and
#766 by a human running ad-hoc ssh monitor loops) had no watcher that
survives a terminal.

This is an operational findings projection over existing ticks, not an
execution engine: it owns no connection, worker, scheduler, claim/lease,
retry, or task dispatch. The caller provides the existing PostgreSQL
connection and the backlog protocol owns all lifecycle transitions.

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
  ledger and the new escalation task. Existing review/merge timers invoke
  it after recording each observation; no extra daemon or unit is needed.
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
    # Waiting outside a bounded window and an already published verdict are
    # expected progress states, not actionable stalls. Persist the tick itself
    # so these observations still break an earlier failure streak.
    benign = {"review_window_inactive", "merge_window_inactive", "marker_already_posted"}
    pairs = sorted({pair for pair in skipped if pair[1] not in benign})
    # Serialize observations of one kind and commit the tick with its rows.
    # Streak identity/count travel with the latest row, so a bounded history
    # window cannot invent a new episode after a long-running stall.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("aicc-watchdog:" + tick_kind,))
        cur.execute("SELECT tick_id FROM tick_observation WHERE tick_kind=%s "
                    "ORDER BY id DESC LIMIT 1", (tick_kind,))
        previous = cur.fetchone()
        previous_id = previous[0] if previous else None
        cur.execute("INSERT INTO tick_observation(tick_kind,tick_id) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING observed_at", (tick_kind,tick_id))
        row = cur.fetchone()
        if row is None:
            return 0
        observed_at = row[0]
        for task_id, reason in pairs:
            cur.execute(
                "INSERT INTO tick_skip_event "
                "(tick_id,tick_kind,task_id,reason,observed_at,first_tick_id,"
                " first_seen_at,consecutive_ticks) "
                "SELECT %s,%s,%s,%s,%s,coalesce(p.first_tick_id,%s),"
                " coalesce(p.first_seen_at,%s),coalesce(p.consecutive_ticks,0)+1 "
                "FROM (SELECT 1) seed LEFT JOIN tick_skip_event p "
                "ON p.tick_kind=%s AND p.tick_id=%s AND p.task_id=%s AND p.reason=%s",
                (tick_id,tick_kind,task_id,reason,observed_at,tick_id,observed_at,
                 tick_kind,previous_id,task_id,reason),
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
    full identity keeps a recurring stall's NEW episode distinct from the
    task filed for its previous one.
    """
    import hashlib

    identity = "\0".join((episode.tick_kind,episode.task_id,episode.reason,episode.first_tick_id))
    return f"{episode.task_id}-STALL-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"


def _load_episodes(conn: Any, threshold: int) -> list[StallEpisode]:
    """Read only each kind's latest snapshot; counters retain episode history."""
    if threshold < 1:
        raise ValueError("threshold must be >= 1")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.tick_kind,s.task_id,s.reason,s.first_tick_id,"
            " s.consecutive_ticks,s.first_seen_at,s.observed_at "
            "FROM (SELECT DISTINCT ON (tick_kind) tick_kind,tick_id "
            " FROM tick_observation ORDER BY tick_kind,id DESC) latest "
            "JOIN tick_skip_event s USING(tick_kind,tick_id) "
            "JOIN backlog_task b ON b.task_id=s.task_id "
            "WHERE b.status IN ('IN_PROGRESS','READY_TO_REVIEW') "
            "AND NOT EXISTS (SELECT 1 FROM tick_stall_escalation e "
            "WHERE e.escalation_task_id=s.task_id OR "
            "starts_with(s.task_id,e.escalation_task_id || '-REM')) "
            "AND s.consecutive_ticks >= %s ORDER BY s.tick_kind,s.task_id,s.reason",
            (threshold,),
        )
        return [StallEpisode(*row) for row in cur.fetchall()]


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
    and the task upsert share the caller transaction. A legacy ledger row
    without its task can also be repaired using the deterministic identity.
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
    with factory() as conn, conn.transaction():
        for episode in _load_episodes(conn, config.threshold):
            outcome, detail = _escalate(conn, episode)
            if outcome == "escalated":
                report.escalated.append((episode, detail))
            elif outcome == "already":
                report.already_escalated.append(episode)
            else:
                report.refused.append((episode.task_id, detail))
    return report
