"""Adapts real, already-recorded run data into `dispatch.rating.LedgerEntry`
values (VOYN-W0-AGENT-MARKETPLACE).

`rating.py`, `task_class.py` and `routing_weight.py` were all built and left
unwired for the same stated reason: "there is no real ledger feed behind the
ratings this consumes." That is no longer true for every field. The runtime db
already records, per run, exactly the facts `rating.ACCEPTED_VERDICTS` and
`rating.LedgerEntry.accepted` need:

* `run.command_json` — the launched command's own argv. There is no executor
  column on `run` at all; `command_center.runtime.runs_read._agent_from_command`
  already treats argv[0] (`claude` / `codex` / `copilot` / ...) as the honest
  executor signal for the Runs page, and this module uses the same convention
  (duplicated, not imported, so `dispatch` keeps its existing zero-dependency
  seam on `runtime` — see `_agent_from_command` below).
* `run.project` / `run.task_type` — the same two attributes
  `task_class.task_class_for` already composes into a bucket key.
* `run.started_at` / `run.completed_at` — real wall-clock duration.
* `completion.review_verdict` — written by `task_pipeline._record_review_verdict`
  from an independent reviewer, never a self-approval (enforced in
  `db.roles`).
* `run_provenance.accepted_sha` (falling back to `completion.merge_commit`) —
  the SHA an accepted change actually landed as.

Two fields the owning idea calls for have *no* real source anywhere in this
codebase yet: `tokens` (no column records it) and `skills` (nothing yet
records which capability/MCP server an attempt used — that is
`VOYN-W0-AICC-SKILL-ACQUISITION`'s job). `escalation_reason` likewise has no
recorded source. Rather than inventing values for them, `ledger_entry_from_run`
defaults them honestly (`0`, `()`, `None`) — exactly the same posture
`task_class_for` took toward domain/language/risk: leave out what is not yet a
real observation instead of fabricating a taxonomy or a number.

Kept pure and DB-free like every other module in this seam: this is a
row-in/value-out adapter, not a query. The actual joins across `run`,
`completion`, `run_provenance` and `run_event` (for per-run cost) are a
separate, later seam — reading real rows through this adapter into
`rating.compute_ratings` and from there into `plan_dispatch`'s selection is
still deferred, the same "decorative витрина" reasoning the prior three
commits gave, because the two unfed fields above are still missing their real
source.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath

from command_center.dispatch.rating import LedgerEntry
from command_center.dispatch.task_class import task_class_for

#: `executor_id` when the run's `command_json` names no honest signal (missing,
#: unparseable, or an empty argv). Distinct from any real executor id, the same
#: way `task_class._UNASSIGNED_PROJECT` is distinct from a real project name.
_UNKNOWN_EXECUTOR = "unknown"


def _agent_from_command(command_json: object) -> str | None:
    """The same "argv[0] is the honest signal" convention
    `runtime.runs_read._agent_from_command` uses, generalized to also accept
    an already-decoded list (a `jsonb`-backed Postgres mirror read hands back a
    decoded object, not JSON text — `daily_spend_usd` hit the same shape).
    Returns `None`, never a sentinel string, so the caller decides the
    sentinel — this function only ever reports what it actually found.
    """
    argv = command_json
    if isinstance(argv, (str, bytes, bytearray)):
        try:
            argv = json.loads(argv)
        except (ValueError, TypeError):
            return None
    if not isinstance(argv, list) or not argv:
        return None
    first = argv[0]
    if not isinstance(first, str) or not first:
        return None
    return PurePosixPath(first).name or first


def _duration_seconds(started_at: object, completed_at: object) -> float:
    """Real wall-clock duration, or `0.0` when either timestamp is missing or
    unparseable — never negative (a clock skew or bad data must not read as
    negative XP)."""
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return 0.0
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
    except ValueError:
        return 0.0
    delta = (completed - started).total_seconds()
    return delta if delta >= 0 else 0.0


def _clean_str(value: object) -> str | None:
    """A non-empty string, or `None` for anything else (missing, blank, or a
    value of the wrong type from a defensive read)."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _clean_cost(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 0.0


def ledger_entry_from_run(
    run: Mapping[str, object],
    *,
    completion: Mapping[str, object] | None = None,
    provenance: Mapping[str, object] | None = None,
    cost_usd: float = 0.0,
) -> LedgerEntry:
    """One `run` row (plus its optional `completion`/`run_provenance` rows and
    its per-run cost, joined by the caller) turned into one `LedgerEntry`.

    Total: never raises on well-typed-ish input, matching every other function
    in this seam. `completion`/`provenance` default to empty so a run with
    neither row yet (still in flight, or never reviewed) still produces a
    well-formed entry — one `rating.compute_ratings` correctly counts as
    attempted but not accepted.
    """
    completion = completion or {}
    provenance = provenance or {}

    executor_id = _agent_from_command(run.get("command_json")) or _UNKNOWN_EXECUTOR
    merged_sha = _clean_str(provenance.get("accepted_sha")) or _clean_str(
        completion.get("merge_commit")
    )
    outcome = run.get("state")

    return LedgerEntry(
        executor_id=executor_id,
        task_class=task_class_for(
            project=run.get("project"),  # type: ignore[arg-type]
            task_type=run.get("task_type"),  # type: ignore[arg-type]
        ),
        merged_sha=merged_sha,
        review_verdict=_clean_str(completion.get("review_verdict")),
        skills=(),
        tokens=0,
        cost_usd=_clean_cost(cost_usd),
        duration_seconds=_duration_seconds(
            run.get("started_at"), run.get("completed_at")
        ),
        outcome=outcome if isinstance(outcome, str) else "",
        escalation_reason=None,
    )
