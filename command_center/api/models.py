"""Typed skeletons for the "new engine" entities — contract only, no storage.

Wave 0 stood up the API service and, alongside it, made the *shape* of the
next wave's domain visible so both shells (and the eventual backend) agree on
one contract before any of it is persisted. Wave 1 (W1-DATA-EVENTS) wires the
first three — ``Proposal``, ``OwnerItem``, ``DigestItem`` — to a repository
(``runtime/db/wave1.py``), a table family (schema v15) and versioned routes
(``api/wave1_routes.py``); these serve as *response* models on that surface, so
their identity/routing fields (``id``; ``Proposal.project_ref``) are required
rather than defaulted. The remaining entities stay contract-only skeletons
until their own increment lands.

Grouping mirrors the product surfaces:

* Proposal            -- Советник (the advisor inbox).
* Motion/Vote/Decision -- Board (collective decisions on motions).
* AuditRun/AuditFinding -- automated audit runs and their findings.
* Incident            -- operational incidents.
* ModelEntry          -- the model catalog (external + local).
* OwnerItem           -- «Мой день» (the owner's action list).
* DigestItem          -- a periodic digest entry.

All values are placeholders with sensible defaults so a skeleton instance can
be constructed in a test without inventing every field. IDs are plain strings;
timestamps are ISO-8601 strings to match the rest of the read surface.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Советник — Proposal
# --------------------------------------------------------------------------

ProposalKind = Literal["trend", "ux", "optimization", "competitor", "feedback"]


class Proposal(BaseModel):
    """An advisor proposal surfaced to the owner for accept/dismiss.

    ``kind`` classifies where the suggestion came from (a spotted trend, a UX
    issue, an optimization, a competitor move, or inbound feedback).
    ``expected_gain`` and ``effort`` are the two-axis triage signals the
    Советник inbox sorts on. ``project_ref`` ties the proposal to a
    ``models.PROJECT_IDS`` namespace so it can be routed to the right board.
    """

    id: str
    kind: ProposalKind = "optimization"
    title: str = ""
    body: str = ""
    expected_gain: str | None = None
    effort: str | None = None
    project_ref: str
    status: Literal["new", "accepted", "dismissed", "converted"] = "new"
    created_at: str | None = None


# --------------------------------------------------------------------------
# Board / Council — Motion / Vote / Decision
# --------------------------------------------------------------------------

#: What kind of voter cast a vote. ``ai`` board members vote today; ``human`` is
#: the seam for external humans invited to the Board later (the identity wiring is
#: out of scope, but the contract already carries the distinction).
VoterKind = Literal["ai", "human"]

#: A vote's choice. ``abstain`` counts toward quorum but toward neither side of
#: the tally.
VoteChoice = Literal["yes", "no", "abstain"]

#: A decision's outcome. ``deferred`` is the tie outcome — quorum was met but
#: neither side carried.
DecisionOutcome = Literal["approved", "rejected", "deferred"]


class Motion(BaseModel):
    """A proposal put to the Board (Council) for a collective decision.

    ``proposed_by`` names who raised it; ``quorum`` is the number of votes
    required before it may close and a decision be recorded. ``proposal_ref`` /
    ``source_ref`` tie a motion back to the advisor proposal or operational
    incident it was raised from (the event seam), so the Board assembles itself
    from what actually happened rather than depending on every source to also
    remember to POST."""

    id: str = ""
    title: str = ""
    body: str = ""
    proposed_by: str = ""
    quorum: int = 1
    project_ref: str | None = None
    proposal_ref: str | None = None
    source_ref: str | None = None
    status: Literal["open", "decided", "withdrawn"] = "open"
    opened_at: str | None = None
    decided_at: str | None = None
    created_at: str | None = None


class Vote(BaseModel):
    """One board member's vote on a :class:`Motion`.

    Every vote records the voter's ``role`` at the moment it was cast (the
    roles-recorded invariant) and their ``voter_kind`` (ai/human), alongside the
    ``choice`` and its ``rationale`` — the explainability of an individual vote."""

    id: str = ""
    motion_ref: str = ""
    voter_id: str = ""
    voter_kind: VoterKind = "ai"
    role: str = ""
    choice: VoteChoice = "abstain"
    rationale: str | None = None
    created_at: str | None = None


class VoterRole(BaseModel):
    """One entry in a :class:`Decision`'s frozen roll-call: who voted, in what
    role, and how. This is the "roles of every voter" the acceptance requires the
    decision to carry — a snapshot taken when the decision is recorded, immutable
    thereafter."""

    voter_id: str = ""
    voter_kind: VoterKind = "ai"
    role: str = ""
    choice: VoteChoice = "abstain"


class Decision(BaseModel):
    """The resolved outcome of a :class:`Motion` once voting closes — an
    immutable, ADR-style record.

    It always carries the roll-call ``roles`` (every voter's role + choice), the
    frozen ``tally`` (``{yes, no, abstain}`` counts), and a ``rationale`` that
    explains *why* the motion was approved/rejected/deferred (explainability).
    Once recorded it is source of truth and is never edited."""

    id: str = ""
    motion_ref: str = ""
    outcome: DecisionOutcome = "deferred"
    tally: dict[str, int] = Field(default_factory=dict)
    rationale: str = ""
    roles: list[VoterRole] = Field(default_factory=list)
    quorum: int = 1
    decided_at: str | None = None


# --------------------------------------------------------------------------
# Audit — AuditRun / AuditFinding
# --------------------------------------------------------------------------

#: The check families an audit run selects from. Each is a pluggable collector
#: behind the audit ``CheckRegistry`` — mirrors ``command_center.audit`` and the
#: ``audit_finding.category`` column.
AuditCategory = Literal["security", "coverage", "code-quality", "deps", "lint"]

#: A finding's severity, ordered least→most urgent.
AuditSeverity = Literal["info", "low", "medium", "high", "critical"]

#: A finding's triage lifecycle. ``open`` is the initial state every finding is
#: written in; ``ack`` (acknowledged) and ``fixed`` are the operator-driven moves.
AuditFindingStatus = Literal["open", "ack", "fixed"]

#: An audit run's execution lifecycle.
AuditRunStatus = Literal["queued", "running", "completed", "failed"]


class AuditRun(BaseModel):
    """One execution of an automated audit over a project.

    ``checks`` records which check families ran on this pass; ``finding_count``
    is the number of findings persisted. ``project_ref`` ties the run to a
    ``models.PROJECT_IDS`` namespace so it can be redacted when that project is
    sensitive. Routed entity: ``id`` is required — a response never carries an
    empty id."""

    id: str
    project_ref: str
    status: AuditRunStatus = "queued"
    checks: list[str] = Field(default_factory=list)
    finding_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None


class AuditFinding(BaseModel):
    """A single finding produced by an :class:`AuditRun`.

    Every finding *always* carries a ``status`` and an ``owner`` — the write
    path refuses a finding with no owner, so the two triage axes can never be
    missing on the surface (VOYN-W2-AUD acceptance). ``file_path``/``loc``
    locate the finding in the tree; ``category`` says which check produced it."""

    id: str
    run_id: str
    category: AuditCategory
    severity: AuditSeverity = "info"
    summary: str = ""
    file_path: str | None = None
    loc: str | None = None
    status: AuditFindingStatus = "open"
    owner: str
    project_ref: str | None = None
    created_at: str | None = None


# --------------------------------------------------------------------------
# Incident
# --------------------------------------------------------------------------


class Incident(BaseModel):
    """An operational incident tracked to resolution."""

    id: str = ""
    title: str = ""
    severity: Literal["sev1", "sev2", "sev3", "sev4"] = "sev3"
    status: Literal["open", "mitigated", "resolved"] = "open"
    project_ref: str | None = None
    opened_at: str | None = None
    resolved_at: str | None = None


# --------------------------------------------------------------------------
# Conflicts / Incidents engine — Conflict
# --------------------------------------------------------------------------

ConflictKind = Literal["merge", "perf", "budget", "security"]


class Conflict(BaseModel):
    """A tracked conflict/incident moving through open → mitigating → resolved.

    ``kind`` classifies the friction (a ``merge`` collision, a ``perf``
    regression, a ``budget`` overrun, a ``security`` exposure). ``source_ref`` is
    the opaque origin reference the conflict was opened from (e.g.
    ``incident:<id>``, a PR ref) and is what the BANK/LEGAL redaction protects —
    a conflict whose ``project_ref`` is sensitive is dropped from every read so
    its ``source_ref`` never leaves the surface. ``owner`` and ``mitigation`` are
    the two facts a conflict must carry before it may reach ``resolved`` (the
    invariant is enforced in the service, not the DB).
    """

    id: str = ""
    kind: ConflictKind = "merge"
    source_ref: str = ""
    severity: Literal["sev1", "sev2", "sev3", "sev4"] = "sev3"
    status: Literal["open", "mitigating", "resolved"] = "open"
    owner: str | None = None
    mitigation: str | None = None
    project_ref: str | None = None
    opened_at: str | None = None
    resolved_at: str | None = None


# --------------------------------------------------------------------------
# Model catalog — ModelEntry / ModelEvent
# --------------------------------------------------------------------------

#: An entry's provenance kind: a hosted external API provider vs. a local model.
ModelKind = Literal["external", "local"]

#: A model's availability lifecycle. ``available`` is the initial state (an
#: external model is available the moment it is registered; a local model starts
#: available and moves through the download lifecycle). ``downloading`` and
#: ``installed`` are the local-download milestones; ``error`` is the failure
#: state a download or probe can land in.
ModelStatus = Literal["available", "downloading", "installed", "error"]

#: The governance-log action vocabulary. A model's *history* is the ordered list
#: of these actions (VOYN-W3 traceability acceptance).
ModelAction = Literal[
    "register", "download-request", "download-progress", "assign", "use", "status-change"
]


class ModelEntry(BaseModel):
    """One entry in the AI-model catalog — an external (hosted API) model or a
    local one. Routed entity: ``id`` is required (a response never carries an
    empty id).

    ``kind`` distinguishes external from local; ``status`` is the current point
    in its availability lifecycle, and ``download_progress`` (0..100) tracks a
    local model's download. ``cost``/``quality``/``latency_ms`` are the metadata
    the auto-select helper weighs (prefer local for cost); ``provenance`` records
    where the model came from so its origin is traceable."""

    id: str
    name: str = ""
    kind: ModelKind = "external"
    provider: str | None = None
    status: ModelStatus = "available"
    cost: float | None = None
    quality: float | None = None
    latency_ms: int | None = None
    provenance: str | None = None
    download_progress: int = 0
    created_at: str | None = None


class ModelEvent(BaseModel):
    """One entry in a model's governance log — the append-only record that makes
    its history fully traceable. ``seq`` orders the log per model; ``action`` is
    what happened; ``target_ref`` is the opaque task/agent an ``assign``/``use``
    acted on; ``provenance`` and ``metadata`` carry the coarse facts of the move."""

    model_config = {"protected_namespaces": ()}

    seq: int
    model_id: str
    action: ModelAction
    actor: str | None = None
    target_ref: str | None = None
    provenance: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_at: str | None = None


# --------------------------------------------------------------------------
# Marketplace — MarketItem / MarketInstallLogEntry
# --------------------------------------------------------------------------

#: What a listing *is*. A closed set — a new kind is a schema decision.
MarketItemKind = Literal["module", "domain_pack", "plugin"]

#: A listing's lifecycle position. ``listed → installed`` is the only edge;
#: this baseline wave has no un-install.
MarketItemStatus = Literal["listed", "installed"]


class MarketItem(BaseModel):
    """One catalogue listing an operator can browse and, once, install.

    ``provenance`` records where the listing came from (a publisher channel, a
    signature reference, a source URL — free-form for this wave); it is carried
    verbatim onto every install-log line so the trail attributes *what* was
    installed to *where it came from*. ``status`` starts at ``listed`` and moves
    to ``installed`` exactly once (the transition is enforced in the service and
    the repository, never fabricated here).
    """

    id: str = ""
    name: str = ""
    kind: MarketItemKind = "module"
    version: str = ""
    publisher: str = ""
    description: str = ""
    status: MarketItemStatus = "listed"
    provenance: str = ""
    created_at: str | None = None
    updated_at: str | None = None


class MarketInstallLogEntry(BaseModel):
    """One append-only install-audit line: *who* installed *what version* of
    *which* listing, *when*, and by which ``installer`` implementation.

    This is the acceptance artefact of the install path — a real, queryable
    record, not a placeholder. ``metadata`` carries any structured detail the
    installer chose to attach (kept as plain strings for this wave).
    """

    id: str = ""
    item_id: str = ""
    actor: str = ""
    version: str = ""
    kind: MarketItemKind = "module"
    provenance: str = ""
    installer: str = ""
    detail: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    installed_at: str | None = None


# --------------------------------------------------------------------------
# «Мой день» — OwnerItem / DigestItem
# --------------------------------------------------------------------------


class OwnerItem(BaseModel):
    """One item on the owner's day list — a thing only the owner can action."""

    id: str
    title: str = ""
    detail: str | None = None
    due: str | None = None
    done: bool = False
    source_ref: str | None = None
    created_at: str | None = None


class DigestItem(BaseModel):
    """One entry in a periodic digest (daily/weekly rollup)."""

    id: str
    title: str = ""
    body: str = ""
    category: str | None = None
    refs: list[str] = Field(default_factory=list)
    created_at: str | None = None


# --------------------------------------------------------------------------
# Networking — Contact / Message / Invitation (VOYN-W3-NET)
# --------------------------------------------------------------------------

MessageDirection = Literal["inbound", "outbound"]
MessageKind = Literal["note", "feedback"]
InvitationStatus = Literal["pending", "accepted", "declined"]


class Contact(BaseModel):
    """A person you network with. ``project_ref`` ties the contact to a
    ``models.PROJECT_IDS`` namespace and is the redaction key — a BANK/LEGAL
    contact is dropped from every read so its handle/name never leaves the
    surface."""

    id: str
    display_name: str = ""
    handle: str = ""
    org: str | None = None
    note: str | None = None
    project_ref: str | None = None
    created_at: str | None = None


class Message(BaseModel):
    """One message exchanged with a contact. ``kind`` distinguishes a plain
    logged ``note`` from inbound ``feedback`` (the intake turned into a task)."""

    id: str
    contact_id: str = ""
    direction: MessageDirection = "inbound"
    kind: MessageKind = "note"
    body: str = ""
    project_ref: str | None = None
    created_at: str | None = None


class Invitation(BaseModel):
    """An invitation of a networking contact to the Council. ``council_ref`` is
    the stable seam the Council engine consumes; no external identity/auth is
    wired here — this is the boundary only."""

    id: str
    contact_id: str = ""
    council_ref: str = ""
    status: InvitationStatus = "pending"
    note: str | None = None
    project_ref: str | None = None
    invited_at: str | None = None
    responded_at: str | None = None
