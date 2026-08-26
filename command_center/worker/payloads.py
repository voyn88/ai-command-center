"""The work-item payload contract, version 1 (VOYN-W0-AICC-SRV-05, slice 2).

The queue accepts any jsonb; this module is where the daemon decides what a
payload *means*. The contract is versioned from the first day so a schema
change is an explicit refusal to old workers, never undefined behaviour:

- ``kind`` selects the handler (the daemon's registry key);
- ``v`` is the payload-schema version for that kind. A version this worker
  does not implement is a **non-retryable** refusal -- redelivery cannot make
  a worker understand a schema it does not ship, and the dead-letter row
  with the stated reason is the operator's signal to upgrade or re-enqueue.

Every refusal is data (a ``PayloadError`` with a reason string), mirroring
the queue protocol's own refusals-as-data discipline. Parsing never raises
out of this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["AgentRunRequest", "PayloadError", "parse_agent_run"]

AGENT_RUN_SCHEMA_VERSION = 1

# Bounds are refusals, not clamps: a payload asking for more than the queue's
# own visibility ceiling (3600s, clamped server-side in queue_claim) would
# outlive any lease its worker can hold, so it is refused at parse time where
# the operator can see why, instead of timing out opaquely mid-run.
_MAX_TIMEOUT_SECONDS = 3600
_MIN_TIMEOUT_SECONDS = 30
# Dot-runs are excluded by construction (separators carry exactly one
# non-alphanumeric), so no separate ".." check is needed.
_BACKLOG_TASK_ID = re.compile(r"^VOYN-[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class PayloadError:
    reason: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    project_id: str
    repository_path: str
    prompt: str
    task_type: str
    timeout_seconds: int
    model: str | None
    untrusted: bool
    #: BO-S2a: the ordered executor cascade. Empty means "no cascade" — the
    #: pre-cascade single-executor behaviour, byte-for-byte.
    cascade: tuple[dict[str, Any], ...] = ()
    #: The canonical backlog task_id, when the planner sent one (optional so
    #: an older enqueued payload -- pre-dating this field -- still parses).
    #: Threaded through to the publish branch name (`publish.py`:
    #: `backlog/<task>`) so two DIFFERENT tasks for the same project stop
    #: colliding on one shared branch: before this field existed, every
    #: agent_run for a project published to `backlog/<project_id>`, and a
    #: `--force-with-lease` push from task B silently overwrote task A's
    #: still-unmerged work on that same branch (VOYN-W0-AICC-PUBLISH-BRANCH-
    #: COLLISION, found 2026-08-21: 29 backlog tasks pointed at one PR with a
    #: single 7-line diff).
    backlog_task_id: str | None = None


def _string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def parse_agent_run(payload: dict[str, Any]) -> AgentRunRequest | PayloadError:
    version = payload.get("v")
    if version != AGENT_RUN_SCHEMA_VERSION:
        return PayloadError(
            reason=(
                f"agent_run payload version {version!r} is not supported; "
                f"this worker implements v{AGENT_RUN_SCHEMA_VERSION}"
            )
        )
    project_id = _string(payload, "project_id")
    repository_path = _string(payload, "repository_path")
    prompt = _string(payload, "prompt")
    task_type = _string(payload, "task_type")
    if (
        project_id is None
        or repository_path is None
        or prompt is None
        or task_type is None
    ):
        missing = [
            key
            for key in ("project_id", "repository_path", "prompt", "task_type")
            if _string(payload, key) is None
        ]
        return PayloadError(
            reason=f"agent_run payload missing required fields: {missing}"
        )

    timeout = payload.get("timeout_seconds", 900)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        return PayloadError(
            reason=f"timeout_seconds must be an integer, got {timeout!r}"
        )
    if not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS:
        return PayloadError(
            reason=(
                f"timeout_seconds {timeout} outside [{_MIN_TIMEOUT_SECONDS}, "
                f"{_MAX_TIMEOUT_SECONDS}] (the queue's own visibility ceiling)"
            )
        )

    model = payload.get("model")
    if model is not None and not (isinstance(model, str) and model.strip()):
        return PayloadError(
            reason=f"model must be a non-empty string or absent, got {model!r}"
        )

    # Provenance travels with the item. Absent means untrusted: the queue is
    # writable by the whole control plane, and a payload that *forgot* to
    # declare provenance must not be the one that gets the mutating profile.
    untrusted = payload.get("untrusted", True)
    if not isinstance(untrusted, bool):
        return PayloadError(reason=f"untrusted must be a boolean, got {untrusted!r}")

    # BO-S2a: the executor cascade, validated as data before any attempt
    # burns on it. Absent or empty is fine (single-executor behaviour); a
    # malformed cascade is a payload defect — non-retryable, because
    # redelivery re-reads the same broken plan.
    raw_cascade = payload.get("cascade", [])
    if not isinstance(raw_cascade, list):
        return PayloadError(
            reason=f"cascade must be a list, got {type(raw_cascade).__name__}"
        )
    cascade: list[dict[str, Any]] = []
    for index, link in enumerate(raw_cascade):
        if not isinstance(link, dict) or not _string(link, "executor"):
            return PayloadError(
                reason=f"cascade[{index}] must be an object with a non-empty 'executor'"
            )
        cascade.append(dict(link))

    backlog_task_id = _string(payload, "backlog_task_id")
    if backlog_task_id is not None and not _BACKLOG_TASK_ID.fullmatch(backlog_task_id):
        return PayloadError(
            reason=("backlog_task_id must use the canonical VOYN-... identifier format")
        )

    return AgentRunRequest(
        project_id=project_id,
        repository_path=repository_path,
        prompt=prompt,
        task_type=task_type,
        timeout_seconds=timeout,
        model=model,
        untrusted=untrusted,
        cascade=tuple(cascade),
        backlog_task_id=backlog_task_id,
    )
