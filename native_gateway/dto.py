"""DTO 1.0 for the native client.

Field names are the exact keys the shipped Swift `SnapshotDecoder`
(clients/aicc-native lane) decodes with a default-key `JSONDecoder`
(camelCase, flat snapshot).  The Swift decoder ignores unknown keys, so the
DTO may grow additively (``projects``, ``connection``) without breaking
existing clients; removing or renaming any key below is a breaking change and
requires a new schema version.

Note: `docs/aicc_native/contracts/v1` (phase0 lane) sketches a snake_case
envelope that the shipped client does not decode.  The gateway serves the
client-decodable shape and records that delta in
`docs/aicc_native_gateway/GATEWAY_V1.md`; that lane's files are not touched
from here.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict


class Freshness(str, enum.Enum):
    fresh = "fresh"
    stale = "stale"
    offline = "offline"
    degraded = "degraded"


class EvidenceState(str, enum.Enum):
    unknown = "unknown"
    observed = "observed"
    verified = "verified"
    rejected = "rejected"
    pending = "pending"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeliveryEvidence(_Frozen):
    headSHA: str | None = None
    pullRequest: str | None = None
    ci: EvidenceState = EvidenceState.unknown
    acceptance: EvidenceState = EvidenceState.unknown
    mergedSHA: str | None = None
    deployedSHA: str | None = None


class TaskDTO(_Frozen):
    id: str
    title: str
    blocker: str | None = None
    evidence: DeliveryEvidence


class AgentLaneDTO(_Frozen):
    id: str
    state: str
    heartbeatAgeSeconds: int


class TimelineEventDTO(_Frozen):
    id: str
    occurredAt: str  # ISO-8601 UTC
    summary: str
    correlationID: str


class ProjectDTO(_Frozen):
    """Additive in DTO 1.0 — calm per-project rollup for the overview."""

    id: str
    name: str
    state: str
    activeTasks: int
    needsAttention: int


class ConnectionDTO(_Frozen):
    """Additive in DTO 1.0 — how live the projection behind this response is."""

    state: Freshness
    projectionAgeSeconds: int | None = None


class SnapshotDTO(_Frozen):
    schemaVersion: str
    revision: str
    generatedAt: str  # ISO-8601 UTC
    freshness: Freshness
    tasks: list[TaskDTO]
    lanes: list[AgentLaneDTO]
    events: list[TimelineEventDTO]
    projects: list[ProjectDTO]
    connection: ConnectionDTO


class DialogDTO(_Frozen):
    """Summary-level only: never raw messages, never raw model inputs."""

    id: str
    title: str
    state: str
    lastActivityAt: str | None = None
    messageCount: int = 0
    lastSummary: str | None = None


class DecisionDTO(_Frozen):
    id: str
    title: str
    status: str
    decidedAt: str | None = None
    summary: str | None = None


class PageMeta(_Frozen):
    nextCursor: str | None = None


class ErrorBody(_Frozen):
    code: str
    message: str
    traceId: str


class ErrorEnvelope(_Frozen):
    error: ErrorBody
