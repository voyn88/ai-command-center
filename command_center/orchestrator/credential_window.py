"""Cross-process visibility for the Claude credential window
(VOYN-W0-AICC-WINDOW-AWARE-SCHEDULING).

`agent_runner`'s in-process circuit breaker (`claude_window_preflight`) stops
a single worker PROCESS from re-trying a Claude link it already watched fail
this window. By construction it cannot tell a different worker process (a
restart, another host) or the planner (which runs on the control-plane host
and never executes an agent at all) the same fact. So
`agent_runner.record_claude_window_exhaustion` also folds the observation
into the queue-visible outcome data every worker already writes -- the
grep-able ``[CREDENTIAL_WINDOW_RESET=claude:<reset_at>]`` trailer
(`agent_runner.credential_window_marker`) lands in
``work_attempt.outcome_reason`` on every failed attempt, and the structured
``credential_reset_at`` field lands in a succeeded delivery's
``route_failovers`` (the more common case: the cascade fails over to the
next executor WITHIN the same delivery, so the attempt itself succeeds).

This module reads the fact back out of whichever place it landed, so the
planner (`orchestrator.planner.Planner`) and the review dispatcher
(`orchestrator.review_merge`) can each make the same "is Claude worth trying
right now" decision the worker already made -- before a delivery is even
created, and regardless of which worker process eventually claims it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["claude_window_reset_at"]

# The inverse of `agent_runner.credential_window_marker`. Non-greedy up to
# the closing bracket rather than a negated character class: PostgreSQL's
# regex flavour treats a `]` right after `[^` specially, which makes
# `[^\]]` a trap (the backslash, not the bracket, becomes the class member),
# so this sidesteps character classes entirely. The reset time itself is an
# ISO-8601 timestamp and never contains `]`.
_MARKER_PATTERN = r"\[CREDENTIAL_WINDOW_RESET=claude:(.*?)\]"

# `aicc_app` (the role the planner and review dispatcher connect as) holds
# SELECT on `work_item`/`work_result` directly and on `work_attempt_public`
# (the view that omits `work_attempt.claim_token_hash`) -- see
# `command_center.db.roles._APP_QUEUE_TABLES` and `VIEW_PRIVILEGES`. Both are
# the same grants `backlog_eligible` and `work_dlq` already rely on, so this
# is a plain read: no new SECURITY DEFINER function or migration required.
_QUERY = f"""
    SELECT reset_at FROM (
        SELECT (regexp_match(outcome_reason, %s))[1]::timestamptz AS reset_at,
               updated_at AS observed_at
          FROM work_attempt_public
         WHERE outcome_reason ~ %s
        UNION ALL
        SELECT (link.value ->> 'credential_reset_at')::timestamptz AS reset_at,
               r.created_at AS observed_at
          FROM work_result r
          CROSS JOIN LATERAL jsonb_array_elements(
              coalesce(r.payload -> 'route_failovers', '[]'::jsonb)) AS link(value)
         WHERE link.value ->> 'executor' = 'claude'
           AND link.value ->> 'credential_reset_at' IS NOT NULL
    ) observations
    WHERE reset_at IS NOT NULL
    ORDER BY observed_at DESC
    LIMIT 1
"""


def _rows(connection_factory: Any, sql: str, params: tuple = ()) -> list[tuple]:
    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def claude_window_reset_at(
    connection_factory: Any, *, rows_fn: Any = _rows
) -> datetime | None:
    """The reset time of the MOST RECENT Claude-window exhaustion this
    control plane can see, whether or not it has already passed.

    Callers decide "is it still exhausted" for themselves by comparing the
    result to their own idea of now -- a stale-but-still-future reset means
    exhausted, a past one means the window reopened (and how recently is
    exactly what burst/front-load dispatch decisions need). Returns None if
    no worker has ever reported one.

    ``rows_fn`` defaults to this module's own row fetcher; the planner (its
    only caller before VOYN-W0-AICC-WINDOW-AWARE-SCHEDULING) always leaves it
    at that default. `orchestrator.review_merge` -- whose hermetic unit tests
    already monkeypatch ITS OWN `_rows` to fake the queue reads `review_once`
    makes -- passes its `_rows` here too, so this query goes through the same
    seam those tests already control instead of silently reaching past it to
    call `connection_factory` for real.
    """
    rows = rows_fn(connection_factory, _QUERY, (_MARKER_PATTERN, _MARKER_PATTERN))
    if not rows or rows[0][0] is None:
        return None
    reset_at = rows[0][0]
    return reset_at if reset_at.tzinfo is not None else reset_at.replace(tzinfo=timezone.utc)
