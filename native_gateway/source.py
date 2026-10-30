"""Projection source — the gateway's only data input.

AIOS (via its existing publisher/read-model pipeline) writes a JSON projection
artifact; the gateway reads that file and nothing else.  There is no second
source of truth here: every field below is a *projection* of AIOS state, the
file's `revision` is minted by the producer, and the gateway only derives
presentation-level facts (freshness, per-project rollups) that are pure
functions of the artifact and the clock.

The reader is fail-safe by construction:

- missing / unreadable / invalid file → an ``offline`` snapshot with empty
  collections (the client renders a calm "offline" state, never an error);
- a producer-declared ``degraded: true`` → ``degraded`` freshness;
- otherwise freshness decays fresh → stale → offline with artifact age.

Every string that enters from the artifact passes the redaction sanitizer
before it is placed into a DTO — the projection producer is trusted, but the
boundary is enforced here regardless (defense in depth).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import SCHEMA_VERSION
from .config import GatewaySettings
from .dto import (
    AgentLaneDTO,
    ConnectionDTO,
    DecisionDTO,
    DeliveryEvidence,
    DialogDTO,
    EvidenceState,
    Freshness,
    GoalDTO,
    MonitorVerdictDTO,
    OwnerEscalationDTO,
    PipelineHealthDTO,
    PipelineLaneDTO,
    ProjectDTO,
    QueueHealthDTO,
    SnapshotDTO,
    TaskDTO,
    TimelineEventDTO,
)
from .redaction import sanitize_value

# Producer vocabulary → client evidence states.  Unknown values map to
# ``unknown`` (never passed through raw).
_EVIDENCE_STATES = {
    "unknown": EvidenceState.unknown,
    "observed": EvidenceState.observed,
    "verified": EvidenceState.verified,
    "passed": EvidenceState.verified,
    "rejected": EvidenceState.rejected,
    "failed": EvidenceState.rejected,
    "pending": EvidenceState.pending,
}

_OFFLINE_REVISION = "offline"

# Allowlisted task execution states (DTO 1.0 additive field).
_TASK_STATES = frozenset(
    {"backlog", "next", "in_progress", "review", "done", "deferred"}
)


def _clean(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return sanitize_value(value.strip())


def _clean_required(value: object, fallback: str) -> str:
    return _clean(value) or fallback


def _evidence_state(value: object) -> EvidenceState:
    if isinstance(value, str):
        return _EVIDENCE_STATES.get(value.strip().lower(), EvidenceState.unknown)
    return EvidenceState.unknown


def _iso(value: object) -> str | None:
    """Normalize a producer timestamp to ISO-8601 UTC, or drop it."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Projection:
    """Parsed, sanitized artifact plus the collections the routes page over."""

    snapshot: SnapshotDTO
    dialogs: list[DialogDTO] = field(default_factory=list)
    decisions: list[DecisionDTO] = field(default_factory=list)


class FileProjectionSource:
    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings

    def load(self, now: datetime | None = None) -> Projection:
        now = now or datetime.now(UTC)
        raw = self._read(self._settings.projection_path)
        if raw is None:
            return _offline_projection(now)
        return _map_projection(raw, now, self._settings)

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None


def _offline_projection(now: datetime) -> Projection:
    generated = now.isoformat().replace("+00:00", "Z")
    snapshot = SnapshotDTO(
        schemaVersion=SCHEMA_VERSION,
        revision=_OFFLINE_REVISION,
        generatedAt=generated,
        freshness=Freshness.offline,
        tasks=[],
        lanes=[],
        events=[],
        projects=[],
        connection=ConnectionDTO(state=Freshness.offline, projectionAgeSeconds=None),
    )
    return Projection(snapshot=snapshot)


def _freshness(
    generated_at: datetime | None,
    degraded: bool,
    now: datetime,
    settings: GatewaySettings,
) -> tuple[Freshness, int | None]:
    if generated_at is None:
        return Freshness.offline, None
    age = max(0, int((now - generated_at).total_seconds()))
    if degraded:
        return Freshness.degraded, age
    if age <= settings.fresh_max_age_s:
        return Freshness.fresh, age
    if age <= settings.stale_max_age_s:
        return Freshness.stale, age
    return Freshness.offline, age


def _map_projection(raw: dict, now: datetime, settings: GatewaySettings) -> Projection:
    generated_iso = _iso(raw.get("generated_at"))
    generated_dt = datetime.fromisoformat(generated_iso) if generated_iso else None
    degraded = bool(raw.get("degraded", False))
    freshness, age = _freshness(generated_dt, degraded, now, settings)

    tasks = [_map_task(t) for t in _items(raw.get("tasks"))]
    lanes = [_map_lane(item) for item in _items(raw.get("lanes"))]
    events = [e for e in (_map_event(x) for x in _items(raw.get("events"))) if e]
    projects = [_map_project(p) for p in _items(raw.get("projects"))]
    dialogs = [_map_dialog(d) for d in _items(raw.get("dialogs"))]
    decisions = [_map_decision(d) for d in _items(raw.get("decisions"))]

    snapshot = SnapshotDTO(
        schemaVersion=SCHEMA_VERSION,
        revision=_clean_required(raw.get("revision"), _OFFLINE_REVISION),
        generatedAt=generated_iso or now.isoformat().replace("+00:00", "Z"),
        freshness=freshness,
        tasks=tasks,
        lanes=lanes,
        events=events,
        projects=projects,
        connection=ConnectionDTO(state=freshness, projectionAgeSeconds=age),
        goal=_map_goal(raw.get("goal")),
        pipelineHealth=_map_pipeline_health(raw.get("pipeline_health")),
    )
    return Projection(snapshot=snapshot, dialogs=dialogs, decisions=decisions)


def _map_goal(raw: object) -> GoalDTO | None:
    if not isinstance(raw, dict):
        return None
    title = _clean(raw.get("title"))
    total = raw.get("total")
    if title is None or not isinstance(total, int) or total <= 0:
        return None

    def _count(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) and value >= 0 else 0

    return GoalDTO(
        title=title,
        done=min(_count("done"), total),
        total=total,
        inProgress=_count("in_progress"),
        review=_count("review"),
    )


# Closed vocabularies for the pipeline_health block.
_BACKLOG_STATUSES = frozenset(
    {
        "OPEN",
        "IN_PROGRESS",
        "READY_TO_REVIEW",
        "DONE",
        "UNTRIAGED",
        "DEFER_TO_USER",
        "SPLIT",
        "NEEDS_REFINEMENT",
        "DECIDED",
    }
)
_MONITOR_VERDICTS = frozenset({"ok", "failing", "unknown"})


def _nonneg(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _map_pipeline_health(raw: object) -> PipelineHealthDTO | None:
    if not isinstance(raw, dict):
        return None
    observed = _iso(raw.get("observed_at"))
    queue = raw.get("queue")
    if observed is None or not isinstance(queue, dict):
        return None
    oldest = queue.get("oldest_ready_age_seconds")
    by_status = raw.get("backlog_by_status")
    by_status = by_status if isinstance(by_status, dict) else {}
    monitor = raw.get("monitor")
    monitor_dto = None
    if isinstance(monitor, dict) and monitor.get("verdict") in _MONITOR_VERDICTS:
        failures = monitor.get("failures")
        failures = failures if isinstance(failures, list) else []
        monitor_dto = MonitorVerdictDTO(
            verdict=monitor["verdict"],
            openFindings=_nonneg(monitor.get("open_findings")),
            failures=[code for code in (_clean(f) for f in failures) if code],
            lastSeenAt=_iso(monitor.get("last_seen_at")),
        )
    return PipelineHealthDTO(
        observedAt=observed,
        queue=QueueHealthDTO(
            ready=_nonneg(queue.get("ready")),
            claimed=_nonneg(queue.get("claimed")),
            dead=_nonneg(queue.get("dead")),
            succeededLastHour=_nonneg(queue.get("succeeded_last_hour")),
            oldestReadyAgeSeconds=(
                oldest if isinstance(oldest, int) and oldest >= 0 else None
            ),
        ),
        activeLanes=[
            PipelineLaneDTO(
                role=_clean_required(lane.get("role"), "unknown"),
                attempts=_nonneg(lane.get("attempts")),
                heartbeatAgeSeconds=(
                    lane["heartbeat_age_seconds"]
                    if isinstance(lane.get("heartbeat_age_seconds"), int)
                    else None
                ),
            )
            for lane in _items(raw.get("active_lanes"))
        ],
        executorAuthFailures30m=_nonneg(raw.get("executor_auth_failures_30m")),
        backlogByStatus={
            k: _nonneg(v) for k, v in by_status.items() if k in _BACKLOG_STATUSES
        },
        ownerEscalations=[
            OwnerEscalationDTO(
                taskID=_clean_required(item.get("task_id"), "unknown"),
                title=_clean_required(item.get("title"), "Untitled"),
                priority=_clean(item.get("priority")),
                updatedAt=_iso(item.get("updated_at")),
            )
            for item in _items(raw.get("owner_escalations"))
        ],
        monitor=monitor_dto,
    )


def _items(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _map_task(raw: dict) -> TaskDTO:
    evidence_raw = raw.get("evidence")
    evidence_raw = evidence_raw if isinstance(evidence_raw, dict) else {}
    state = _clean(raw.get("state"))
    return TaskDTO(
        id=_clean_required(raw.get("id"), "unknown"),
        title=_clean_required(raw.get("title"), "Untitled"),
        blocker=_clean(raw.get("blocker")),
        state=state if state in _TASK_STATES else None,
        evidence=DeliveryEvidence(
            headSHA=_clean(evidence_raw.get("head_sha")),
            pullRequest=_clean(evidence_raw.get("pr")),
            ci=_evidence_state(evidence_raw.get("ci")),
            acceptance=_evidence_state(evidence_raw.get("acceptance")),
            mergedSHA=_clean(evidence_raw.get("merged_sha")),
            deployedSHA=_clean(evidence_raw.get("deployed_sha")),
        ),
    )


def _map_lane(raw: dict) -> AgentLaneDTO:
    heartbeat = raw.get("heartbeat_age_seconds")
    return AgentLaneDTO(
        id=_clean_required(raw.get("id"), "unknown"),
        state=_clean_required(raw.get("state"), "unknown"),
        heartbeatAgeSeconds=heartbeat if isinstance(heartbeat, int) else -1,
    )


def _map_event(raw: dict) -> TimelineEventDTO | None:
    occurred = _iso(raw.get("occurred_at"))
    if occurred is None:
        return None  # an event without a valid timestamp cannot be ordered
    return TimelineEventDTO(
        id=_clean_required(raw.get("id"), "unknown"),
        occurredAt=occurred,
        summary=_clean_required(raw.get("summary"), ""),
        correlationID=_clean_required(raw.get("correlation_id"), ""),
    )


def _map_project(raw: dict) -> ProjectDTO:
    active = raw.get("active_tasks")
    attention = raw.get("needs_attention")
    return ProjectDTO(
        id=_clean_required(raw.get("id"), "unknown"),
        name=_clean_required(raw.get("name"), "Untitled"),
        state=_clean_required(raw.get("state"), "unknown"),
        activeTasks=active if isinstance(active, int) else 0,
        needsAttention=attention if isinstance(attention, int) else 0,
    )


def _map_dialog(raw: dict) -> DialogDTO:
    count = raw.get("message_count")
    return DialogDTO(
        id=_clean_required(raw.get("id"), "unknown"),
        title=_clean_required(raw.get("title"), "Untitled"),
        state=_clean_required(raw.get("state"), "unknown"),
        lastActivityAt=_iso(raw.get("last_activity_at")),
        messageCount=count if isinstance(count, int) else 0,
        lastSummary=_clean(raw.get("last_summary")),
    )


def _map_decision(raw: dict) -> DecisionDTO:
    return DecisionDTO(
        id=_clean_required(raw.get("id"), "unknown"),
        title=_clean_required(raw.get("title"), "Untitled"),
        status=_clean_required(raw.get("status"), "unknown"),
        decidedAt=_iso(raw.get("decided_at")),
        summary=_clean(raw.get("summary")),
    )
