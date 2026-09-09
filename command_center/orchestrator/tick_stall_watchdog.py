"""System-owned watchdog for looping review/merge ticks
(VOYN-W0-AICC-TICK-STALL-WATCHDOG).

The review/merge ticks (`backlog-review`, `backlog-merge`) already classify
every task they decline to act on with a structured reason
(`no_accept_marker_on_head`, `review_chunk_verdict_missing:N`,
`review_chunk_not_succeeded:N:dead`, `no_review_result_yet`, ...) and, since
migration 0018, persist every one of those reasons to `tick_skip_event` --
see `review_merge.next_tick_seq`/`record_tick_skips`. What was still missing
was anything that *read* that ledger looking for the pathological case: the
SAME task logging the SAME reason tick after tick, forever. Every stall found
on 2026-09-07 (a verdict-less chunk on PR #774, quota-dead chunks on PR #707,
a stale-head verdict on PR #766) was found by a human running an ad-hoc
`journalctl -f | grep` session that died with the SSH session that started
it -- this module is the fix: a small, timer-driven, oneshot check with no
human in the loop.

Detection
---------
For a given `tick_name` (`backlog-review` or `backlog-merge`), the ticks that
actually ran form a strictly increasing sequence of `tick_seq` values --
every CLI invocation allocates one via `next_tick_seq`, whether or not it
skipped anything. `_recent_ticks` reads the distinct `tick_seq` values that
tick_name has produced ANY skip row for, most-recent first: a tick that
skipped nothing at all for that tick_name leaves no row and is invisible to
this query the same way it is invisible to everything else, which only
matters for a tick_name that goes an entire tick without skipping a single
task -- rare enough in a real backlog that a false "run" this produces is a
one-tick undercount, never an overcount (a real gap can only shorten a
detected run, not lengthen one).

For one (task_id, reason) pair, `_run_length` walks that tick_name's recent
ticks newest-first and counts how many of the LATEST ones all carry a skip
row for that exact pair -- stopping at the first tick that does not, which
also means stopping at a tick where the task was skipped for a DIFFERENT
reason (a change in classification breaks the run just as visibly as a
success would). That count, together with the oldest tick_seq inside it, is
the episode: `(task_id, reason, episode_start_tick_seq)`. The oldest tick_seq
in an unbroken run cannot change while the run stays unbroken, which is
exactly the property `tick_stall_escalation`'s unique constraint needs to
make a repeat run over the same still-open episode a no-op instead of a
second escalation.

Escalation
----------
`detect_and_escalate` inserts the episode into `tick_stall_escalation`
(`ON CONFLICT (task_id, reason, episode_start_tick_seq) DO NOTHING`) and,
ONLY if that insert actually happened (a brand-new episode, not a repeat
observation of one already recorded), opens a new OPEN/P1 backlog task
through the same `BacklogStore.upsert_task` path `review_merge.
_remediate_rejection` already uses to spawn remediation work -- both are
"the tick's own findings become a new addressable OPEN task," not a special
write path invented for this feature. Both writes happen in the SAME
transaction: if creating the backlog task fails for any reason, the
`tick_stall_escalation` insert is rolled back with it, so a half-escalated
episode (a ledger row with no corresponding task) can never persist -- the
next run sees the episode as still unescalated and retries the whole thing
atomically, rather than being permanently silenced by a ledger row nothing
ever comes of.

Refusal-as-data: this module never mutates the stalled task itself (no
`backlog_transition` call, no evidence write, no queue write against it) --
its only side effect on anything other than its own two tables is creating
one NEW, independent backlog task that names the stall. A run over a
database with no episode past the threshold is a pure no-op.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "WatchdogConfig",
    "WatchdogReport",
    "detect_and_escalate",
]


@dataclass
class WatchdogConfig:
    #: A (task_id, reason) pair skipped on every one of the last N tick
    #: invocations of the SAME tick_name is a stall.
    consecutive_threshold: int = 5
    #: How many of a tick_name's most recent tick_seq values to inspect per
    #: pass -- bounds the query even against a ledger nobody has pruned yet.
    #: Comfortably above the threshold so a run that just crossed it is
    #: never truncated by the window itself.
    lookback_ticks: int = 200
    #: New escalation tasks land in wave "0": this IS the operational
    #: control plane noticing its own pipeline is stuck, not a feature.
    escalation_wave: str = "0"
    escalation_priority: str = "P1"


@dataclass
class WatchdogReport:
    #: (task_id, reason, consecutive_count, escalation_task_id) -- a NEW
    #: episode this run escalated.
    escalated: list[tuple[str, str, int, str]] = field(default_factory=list)
    #: (task_id, reason, consecutive_count) -- at/above threshold, but
    #: already escalated by an earlier run over the same still-open episode.
    already_escalated: list[tuple[str, str, int]] = field(default_factory=list)


def _rows(factory: Any, sql: str, params: tuple = ()) -> list[tuple]:
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _recent_ticks(factory: Any, tick_name: str, limit: int) -> list[int]:
    """This tick_name's most recent distinct tick_seq values, newest first."""
    rows = _rows(
        factory,
        "SELECT DISTINCT tick_seq FROM tick_skip_event WHERE tick_name = %s "
        "ORDER BY tick_seq DESC LIMIT %s",
        (tick_name, limit),
    )
    return [int(r[0]) for r in rows]


def _pairs_in_window(
    factory: Any, tick_name: str, ticks: list[int]
) -> dict[tuple[str, str], set[int]]:
    """task_id/reason -> the subset of `ticks` it was skipped for in this
    tick_name, so `_run_length` can answer "did this pair appear in tick X"
    with a Python set lookup instead of one query per pair."""
    if not ticks:
        return {}
    rows = _rows(
        factory,
        "SELECT task_id, reason, tick_seq FROM tick_skip_event "
        "WHERE tick_name = %s AND tick_seq = ANY(%s)",
        (tick_name, ticks),
    )
    out: dict[tuple[str, str], set[int]] = {}
    for task_id, reason, tick_seq in rows:
        out.setdefault((str(task_id), str(reason)), set()).add(int(tick_seq))
    return out


def _run_length(ticks_newest_first: list[int], present: set[int]) -> tuple[int, int | None]:
    """How many of `ticks_newest_first`, counted from the front, are all in
    `present` -- and the oldest tick_seq among them (the episode's identity),
    or None if the run is empty (the pair was not skipped on the very latest
    tick, so there is no CURRENT run to report, however long a past one
    was)."""
    count = 0
    for tick in ticks_newest_first:
        if tick not in present:
            break
        count += 1
    if count == 0:
        return 0, None
    return count, ticks_newest_first[count - 1]


_TASK_ID_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    return _TASK_ID_SLUG_RE.sub("-", value).strip("-") or "x"


def _escalation_task_id(task_id: str, reason: str, episode_start_tick_seq: int) -> str:
    """Deterministic id for the episode's own backlog task -- so a second
    attempt against an episode whose `tick_stall_escalation` row is somehow
    missing (there is no path that produces this; see the module docstring's
    single-transaction argument) still reconciles onto the same task rather
    than minting a duplicate."""
    return (
        "VOYN-TICK-STALL-"
        f"{_slug(task_id)}-{_slug(reason)}-{episode_start_tick_seq}"
    )


def _escalation_body(
    task_id: str, reason: str, tick_name: str, count: int, episode_start_tick_seq: int
) -> str:
    return (
        f"The `{tick_name}` tick has skipped `{task_id}` for the same "
        f"structured reason (`{reason}`) on its last {count} consecutive "
        "runs (episode started at tick_seq "
        f"{episode_start_tick_seq}). This is a system-owned watchdog "
        "finding (VOYN-W0-AICC-TICK-STALL-WATCHDOG), written as data by "
        "reading `tick_skip_event` -- it has not inspected the stalled task "
        "or PR itself and has made no change to either.\n\n"
        "Investigate why the skip reason is not resolving on its own (a "
        "missing verdict, a dead review chunk, a stale head, or whatever "
        "the reason string names) and either fix the underlying condition "
        "or take the stalled task out of the automated pipeline's path."
    )


def detect_and_escalate(
    factory: Any, cfg: WatchdogConfig | None = None
) -> WatchdogReport:
    """Scan `tick_skip_event` for (task_id, reason) pairs skipped on every
    one of the last `cfg.consecutive_threshold` tick invocations, and
    escalate any NEW such episode into an OPEN/P1 backlog task.

    Idempotent per episode -- see the module docstring. Never mutates the
    stalled task: the only writes here are one `tick_stall_escalation` row
    and (for a newly detected episode) one brand-new `backlog_task` row.
    """
    from command_center.db.backlog_parser import ParsedTask
    from command_center.db.backlog_store import BacklogStore

    cfg = cfg or WatchdogConfig()
    report = WatchdogReport()

    tick_names = [
        str(r[0])
        for r in _rows(factory, "SELECT DISTINCT tick_name FROM tick_skip_event")
    ]
    for tick_name in tick_names:
        ticks = _recent_ticks(factory, tick_name, cfg.lookback_ticks)
        if len(ticks) < cfg.consecutive_threshold:
            continue
        pairs = _pairs_in_window(factory, tick_name, ticks)
        for (task_id, reason), present in pairs.items():
            count, episode_start = _run_length(ticks, present)
            if count < cfg.consecutive_threshold or episode_start is None:
                continue
            escalation_task_id = _escalation_task_id(task_id, reason, episode_start)
            with factory() as conn:
                conn.autocommit = False
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO tick_stall_escalation "
                            "(task_id, reason, episode_start_tick_seq, "
                            "consecutive_count, escalation_task_id) "
                            "VALUES (%s, %s, %s, %s, %s) "
                            "ON CONFLICT (task_id, reason, episode_start_tick_seq) "
                            "DO NOTHING RETURNING id",
                            (task_id, reason, episode_start, count, escalation_task_id),
                        )
                        inserted = cur.fetchone() is not None
                    if not inserted:
                        conn.rollback()
                        report.already_escalated.append((task_id, reason, count))
                        continue

                    from contextlib import nullcontext

                    store = BacklogStore(lambda: nullcontext(conn))
                    ok, _reason, _changed = store.upsert_task(
                        ParsedTask(
                            task_id=escalation_task_id,
                            wave=cfg.escalation_wave,
                            priority=cfg.escalation_priority,
                            status="OPEN",
                            kind="task",
                            title=(
                                f"Tick stall: {task_id} stuck on "
                                f"{reason} for {count}+ ticks ({tick_name})"
                            ),
                            body=_escalation_body(
                                task_id, reason, tick_name, count, episode_start
                            ),
                            repo=None,
                            line_no=0,
                        )
                    )
                    if not ok:
                        conn.rollback()
                        continue
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            report.escalated.append((task_id, reason, count, escalation_task_id))
    return report
