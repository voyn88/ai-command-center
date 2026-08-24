"""Typed queue-transport and executor-result outcomes.

The queue's terminal ``succeeded`` state means that a worker durably reported a
result.  It does not mean that the model accepted a review or completed a task.
Keeping those layers explicit prevents a failed or signal-killed executor from
being promoted merely because its transport acknowledgement succeeded.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = [
    "ExecutorResultKind",
    "QueueOutcomeKind",
    "completed_model_result",
    "model_result_payload",
]


class QueueOutcomeKind(StrEnum):
    TRANSPORT_SUCCEEDED = "transport_succeeded"
    EXECUTOR_INFRA_FAILURE = "executor_infrastructure_failure"
    REQUEST_REJECTED = "request_rejected"


class ExecutorResultKind(StrEnum):
    MODEL_RESULT = "model_result"


def model_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical durable payload for one executed model invocation."""
    payload = dict(result)
    payload["transport_status"] = QueueOutcomeKind.TRANSPORT_SUCCEEDED.value
    payload["executor_result_kind"] = ExecutorResultKind.MODEL_RESULT.value
    return payload


def completed_model_result(payload: object) -> bool:
    """Fail-closed gate used before any model verdict is aggregated."""
    return (
        isinstance(payload, dict)
        and payload.get("transport_status")
        == QueueOutcomeKind.TRANSPORT_SUCCEEDED.value
        and payload.get("executor_result_kind") == ExecutorResultKind.MODEL_RESULT.value
        and payload.get("status") == "completed"
    )
