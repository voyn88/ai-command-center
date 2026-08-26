"""Request bodies and response wrappers for the Wave-3 Council surface.

The *entities* returned here are the shared contract models in
:mod:`command_center.api.models` (``Motion``, ``Vote``, ``Decision``); the
classes here describe the **inputs** a client POSTs (raise a motion, cast a vote)
and the small composite responses (a motion with its votes + journal, a decision
with its journal, list pages) that wrap those entities.

Kept separate from ``models.py`` on purpose: the entity skeletons are the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from command_center.api.models import Decision, Motion, Vote, VoteChoice, VoterKind


class MotionCreate(BaseModel):
    """POST body for opening a motion. ``title`` and ``proposed_by`` are required
    — a motion always has a subject and someone who raised it. ``quorum`` is the
    number of votes required before it may close (default 1). ``project_ref``,
    when it names a BANK/LEGAL project, is rejected (redaction)."""

    title: str
    proposed_by: str
    body: str = ""
    quorum: int = 1
    project_ref: str | None = None
    proposal_ref: str | None = None
    source_ref: str | None = None


class VoteCreate(BaseModel):
    """POST body for casting a vote on a motion. ``voter_id`` and ``choice`` are
    required; ``role`` is resolved from the Board roster (not trusted from the
    client) so the recorded role is authoritative — a supplied ``role`` is
    ignored. ``voter_kind`` distinguishes an ai member from a human seat."""

    voter_id: str
    choice: VoteChoice
    voter_kind: VoterKind = "ai"
    rationale: str | None = None


class JournalEntry(BaseModel):
    """One entry in a motion's append-only journal (audit trail)."""

    seq: int
    event_type: str
    actor: str | None = None
    role: str | None = None
    message: str | None = None
    created_at: str | None = None


class MotionDetail(BaseModel):
    """A motion with everything decided about it: the motion, every vote cast, the
    decision (once recorded) and the full journal."""

    motion: Motion
    votes: list[Vote] = Field(default_factory=list)
    decision: Decision | None = None
    journal: list[JournalEntry] = Field(default_factory=list)


class DecisionRecord(BaseModel):
    """The canonical decision representation: the immutable :class:`Decision`
    (carrying the roll-call of roles + tally + rationale) together with the full
    ``journal`` of how the motion got there. This is what the acceptance means by
    "a Decision always carries roles + full journal"."""

    decision: Decision
    journal: list[JournalEntry] = Field(default_factory=list)


class MotionList(BaseModel):
    """A page of motions plus the paging echo the client sent."""

    motions: list[Motion] = Field(default_factory=list)
    limit: int
    offset: int


class DecisionList(BaseModel):
    """A page of decision records (each carrying roles + journal)."""

    decisions: list[DecisionRecord] = Field(default_factory=list)
    limit: int
    offset: int
