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


# --------------------------------------------------------------------------
# Reputation (VOYN-MIN-LINK-REPUTE): trust scores from vote quality + influence
# --------------------------------------------------------------------------


class VoteTrustScoreOut(BaseModel):
    """The explainable trust score for one cast vote — see
    :mod:`command_center.council.reputation` for how ``score`` is derived and
    what ``basis`` means."""

    vote_id: str
    voter_id: str
    motion_id: str
    score: float | None = None
    basis: str
    explanation: str
    votes_considered: int = 0


class VoterReputationOut(BaseModel):
    """A voter's aggregate reputation across every decided motion they voted
    on — the roll-up :func:`command_center.council.reputation.compute_voter_reputation`
    produces."""

    voter_id: str
    score: float | None = None
    basis: str
    alignment_rate: float | None = None
    influence_rate: float | None = None
    votes_considered: int = 0
    explanation: str


class VoterReputationList(BaseModel):
    """A page of voter reputations plus the paging echo the client sent."""

    reputations: list[VoterReputationOut] = Field(default_factory=list)
    limit: int
    offset: int


class ReputationCoverage(BaseModel):
    """The acceptance metric behind VOYN-MIN-LINK-REPUTE: the fraction of
    (non-redacted) cast votes that carry an explainable trust score — a vote
    only fails to count when its motion is undecided *and* its voter has no
    decided-vote history yet (``basis == "insufficient_data"``). The acceptance
    bar is ``coverage >= 0.9``."""

    total_votes: int
    explainable_votes: int
    coverage: float
