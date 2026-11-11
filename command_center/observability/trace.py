"""One trace_id per backlog task, from the planner tick to the merge.

Confirmed false by audit: `trace_id`/OpenTelemetry/`correlation_id` had zero
hits anywhere in this codebase, so a single backlog task's journey through
planning, claim, lease, workspace, agent, tests, publish, review and merge
could not be followed in logs — each stage runs in its own process
(planner tick, worker daemon, review/merge tick) with no shared request
context between them.

A generated-and-passed-along id would need a wire to carry it, and none of
these stages share one: they are independent oneshot/daemon processes that
communicate only through the Postgres backlog/queue tables, keyed on the
backlog `task_id`. Deriving `trace_id` deterministically from `task_id`
(instead of minting one at plan time and threading it through payloads and
schema columns) means every stage computes the identical id from data it
already has — no new column, no payload field a worker built before this
existed would be missing.

Every span is one self-contained JSON log line — trace_id, stage, task_id
and whatever the caller knows — so `grep <trace_id>` (or `jq` over the
`command_center.trace` logger's output) shows one task's full pipeline
journey regardless of how the surrounding logging is configured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "PIPELINE_STAGES",
    "new_trace_id",
    "trace_id_for_task",
    "log_span",
]

#: The one canonical journey a backlog task's trace_id is expected to cover,
#: in order. Not enforced anywhere — a skipped or reordered stage is a fact
#: about that run, not a defect in this module — but named here once so
#: "plan to merge" has one spelling every instrumented call site shares.
PIPELINE_STAGES = (
    "plan",
    "claim",
    "lease",
    "workspace",
    "agent",
    "tests",
    "publish",
    "review",
    "merge",
)

_LOGGER = logging.getLogger("command_center.trace")


def new_trace_id() -> str:
    """A fresh id for work that has no backlog task_id to derive one from."""
    return uuid.uuid4().hex


def trace_id_for_task(task_id: str) -> str:
    """The one trace_id every stage computes for this task, with no shared
    process, database column or payload field required to agree on it."""
    digest = hashlib.sha256(f"aicc-trace:{task_id}".encode("utf-8")).hexdigest()
    return digest[:32]


def log_span(
    stage: str,
    *,
    task_id: str,
    trace_id: str | None = None,
    logger: logging.Logger | None = None,
    **fields: Any,
) -> str:
    """Emit one structured span log line and return the trace_id used.

    ``fields`` travels verbatim (work_item_id, pr_url, executor, whatever
    the caller already has in hand) — this never re-derives anything the
    stage already knows.
    """
    resolved_trace_id = trace_id or trace_id_for_task(task_id)
    record = {
        "trace_id": resolved_trace_id,
        "stage": stage,
        "task_id": task_id,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **fields,
    }
    (logger or _LOGGER).info(json.dumps(record, sort_keys=True, default=str))
    return resolved_trace_id
