"""Service tier for the Wave-3 Council engine (routes → **service** →
repository → db).

The routes in :mod:`command_center.api.council_routes` hold no logic; they call
one function here per endpoint. This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* enforces the **role / permission** policy on every vote via the Board roster
  (:mod:`command_center.council.roles`) — who may vote, and in what role;
* **owns the tally + quorum policy**: a motion may close into a decision only
  once quorum is met; the tally decides the outcome and the rationale explains
  it (explainability);
* records the immutable, ADR-style decision — the roll-call of every voter's
  role + choice, the frozen tally, and the reasoning — and guarantees a decision,
  once recorded, is never re-opened or edited (the motion becomes terminal);
* maps stored rows onto the :mod:`command_center.api.models` contract;
* applies the BANK/LEGAL redaction policy — a motion whose ``project_ref`` is
  sensitive is dropped from every list and reads as *not found* on detail, and
  its decision is excluded from the decisions surface, so its content never
  leaves this surface.

Testability seam: every backing call (repository functions, the roster,
``is_sensitive``, ``resolve_db_path``) is referenced through a module-level name
so a test can monkeypatch it, and the runtime db path resolves under the per-test
``AICC_DATA_DIR`` sandbox (see ``tests/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import council_schemas as s
from command_center.api import models
from command_center.council.roles import DEFAULT_ROSTER, CouncilRoster
from command_center.models import SENSITIVE_PROJECT_IDS
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION

# Repo root is three levels up: <root>/command_center/council/service.py
ROOT = Path(__file__).resolve().parents[2]

#: The Board roster — the role/permission policy. Referenced through this
#: module-level name so a deployment or a test can substitute the whole policy
#: (a closed Board, an activated human seat) without touching the pipeline.
_roster: CouncilRoster = DEFAULT_ROSTER


class SensitiveProjectRefError(Exception):
    """Raised when a manual ``POST /council/motions`` names a BANK/LEGAL project.
    A sensitive motion is redacted on every read anyway, so the write is
    *rejected* (HTTP 400) rather than persisted. The event intake never trips
    this: it drops sensitive proposals/incidents before it ever opens a motion."""


class QuorumNotMetError(Exception):
    """Raised when a close is requested on a motion that has not yet gathered
    ``quorum`` votes. Surfaced as HTTP 409 — the motion cannot be decided in its
    current state, and more members must vote first."""


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags (the same
    lazy-migrate pattern the Wave-1/Wave-2 services use)."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


def _sensitive_projects() -> list[str]:
    """The redaction exclusion list handed to the repository, in a stable order —
    the same policy :func:`is_sensitive` enforces per-row, expressed so it can be
    applied inside the SQL query."""
    return sorted(SENSITIVE_PROJECT_IDS)


# --------------------------------------------------------------------------
# Row -> contract-model mapping
# --------------------------------------------------------------------------


def _motion_from_row(row: dict) -> models.Motion:
    return models.Motion(
        id=row["id"],
        title=row.get("title") or "",
        body=row.get("body") or "",
        proposed_by=row.get("proposed_by") or "",
        quorum=int(row.get("quorum") or 1),
        project_ref=row.get("project_ref"),
        proposal_ref=row.get("proposal_ref"),
        source_ref=row.get("source_ref"),
        status=row["status"],
        opened_at=row.get("opened_at"),
        decided_at=row.get("decided_at"),
        created_at=row.get("created_at"),
    )


def _vote_from_row(row: dict) -> models.Vote:
    return models.Vote(
        id=row["id"],
        motion_ref=row["motion_id"],
        voter_id=row["voter_id"],
        voter_kind=row.get("voter_kind") or "ai",
        role=row.get("role") or "",
        choice=row["choice"],
        rationale=row.get("rationale"),
        created_at=row.get("created_at"),
    )


def _decision_from_row(row: dict) -> models.Decision:
    return models.Decision(
        id=row["id"],
        motion_ref=row["motion_id"],
        outcome=row["outcome"],
        tally=dict(row.get("tally") or {}),
        rationale=row.get("rationale") or "",
        roles=[models.VoterRole(**r) for r in (row.get("roles") or [])],
        quorum=int(row.get("quorum") or 1),
        decided_at=row.get("decided_at"),
    )


def _journal_from_row(row: dict) -> s.JournalEntry:
    return s.JournalEntry(
        seq=int(row["seq"]),
        event_type=row["event_type"],
        actor=row.get("actor"),
        role=row.get("role"),
        message=row.get("message"),
        created_at=row.get("created_at"),
    )


# --------------------------------------------------------------------------
# Motions — create / list / get
# --------------------------------------------------------------------------


def create_motion(payload: s.MotionCreate) -> models.Motion:
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"motion for sensitive project {payload.project_ref!r} is rejected"
        )
    row = db.create_motion(
        _db_path(),
        title=payload.title,
        proposed_by=payload.proposed_by,
        body=payload.body,
        quorum=payload.quorum,
        project_ref=payload.project_ref,
        proposal_ref=payload.proposal_ref,
        source_ref=payload.source_ref,
    )
    return _motion_from_row(row)


def list_motions(
    *,
    status: str | None = None,
    project: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> s.MotionList:
    rows = db.list_motions(
        _db_path(),
        status=status,
        project=project,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    return s.MotionList(
        motions=[_motion_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_motion_detail(motion_id: str) -> s.MotionDetail | None:
    """A motion with its votes, decision (if any) and full journal. Returns
    ``None`` when the motion does not exist or is sensitive (treated as not
    found — its content must never leak)."""
    path = _db_path()
    row = db.get_motion(path, motion_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    votes = [_vote_from_row(v) for v in db.list_votes(path, motion_id)]
    decision_row = db.get_decision(path, motion_id)
    journal = [_journal_from_row(e) for e in db.list_events(path, motion_id)]
    return s.MotionDetail(
        motion=_motion_from_row(row),
        votes=votes,
        decision=_decision_from_row(decision_row) if decision_row else None,
        journal=journal,
    )


# --------------------------------------------------------------------------
# Voting
# --------------------------------------------------------------------------


def cast_vote(motion_id: str, payload: s.VoteCreate) -> models.Vote | None:
    """Cast one vote on a motion. The voter's role is resolved from the Board
    roster (permission + authoritative role), then the vote is recorded.

    Returns ``None`` when the motion does not exist or is sensitive (treated as
    not found). Propagates :class:`roles.NotAMemberError` /
    :class:`roles.VoterNotPermittedError` (→ 403), :class:`db.DoubleVoteError`
    (→ 409) and :class:`db.MotionNotOpenError` (→ 409) to the controller."""
    path = _db_path()
    row = db.get_motion(path, motion_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    # Permission + role resolution (raises on a non-member / inactive seat).
    member = _roster.permit(payload.voter_id, voter_kind=payload.voter_kind)
    vote_row = db.cast_vote(
        path,
        motion_id=motion_id,
        voter_id=payload.voter_id,
        role=member.role,
        choice=payload.choice,
        voter_kind=member.kind,
        rationale=payload.rationale,
    )
    return _vote_from_row(vote_row)


# --------------------------------------------------------------------------
# Closing a motion — tally → immutable decision
# --------------------------------------------------------------------------


def _tally(votes: list[dict]) -> dict[str, int]:
    """Count votes into ``{yes, no, abstain}``."""
    counts = {"yes": 0, "no": 0, "abstain": 0}
    for v in votes:
        counts[v["choice"]] = counts.get(v["choice"], 0) + 1
    return counts


def _outcome_and_rationale(tally: dict[str, int], quorum: int) -> tuple[str, str]:
    """Decide the outcome from the tally and explain it (the explainability
    contract). A simple majority of the decisive votes carries: more ``yes`` than
    ``no`` approves, more ``no`` than ``yes`` rejects, a tie defers. ``abstain``
    counted toward quorum but toward neither side."""
    yes, no, abstain = tally["yes"], tally["no"], tally["abstain"]
    total = yes + no + abstain
    if yes > no:
        outcome = "approved"
        why = f"a majority voted yes ({yes} yes vs {no} no)"
    elif no > yes:
        outcome = "rejected"
        why = f"a majority voted no ({no} no vs {yes} yes)"
    else:
        outcome = "deferred"
        why = f"the vote was tied ({yes} yes, {no} no)"
    rationale = (
        f"{outcome}: {why}; {abstain} abstained. "
        f"Quorum of {quorum} met with {total} votes cast."
    )
    return outcome, rationale


def _roles_snapshot(votes: list[dict]) -> list[dict]:
    """The frozen roll-call recorded on the decision: every voter's id, kind, role
    and choice — the "roles of every voter" the acceptance requires."""
    return [
        {
            "voter_id": v["voter_id"],
            "voter_kind": v.get("voter_kind") or "ai",
            "role": v.get("role") or "",
            "choice": v["choice"],
        }
        for v in votes
    ]


def close_motion(motion_id: str) -> s.DecisionRecord | None:
    """Close an open motion: tally its votes and record the immutable decision —
    but only if quorum is met.

    Raises :class:`QuorumNotMetError` (→ 409) when fewer than ``quorum`` votes
    were cast, and :class:`db.MotionNotOpenError` (→ 409) when the motion is
    already decided/withdrawn (so a decision is recorded exactly once and never
    re-opened). Returns ``None`` when the motion does not exist or is sensitive."""
    path = _db_path()
    row = db.get_motion(path, motion_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    if row["status"] != "open":
        raise db.MotionNotOpenError(
            f"motion {motion_id!r} is {row['status']!r}, not open — "
            "its decision is already recorded"
        )
    quorum = int(row["quorum"])
    votes = db.list_votes(path, motion_id)
    if len(votes) < quorum:
        raise QuorumNotMetError(
            f"motion {motion_id!r} has {len(votes)} vote(s), quorum is {quorum}"
        )
    tally = _tally(votes)
    outcome, rationale = _outcome_and_rationale(tally, quorum)
    roles = _roles_snapshot(votes)
    decision_row = db.record_decision(
        path,
        motion_id=motion_id,
        expected_version=row["version"],
        outcome=outcome,
        tally=tally,
        roles=roles,
        rationale=rationale,
        quorum=quorum,
    )
    journal = [_journal_from_row(e) for e in db.list_events(path, motion_id)]
    return s.DecisionRecord(
        decision=_decision_from_row(decision_row), journal=journal
    )


# --------------------------------------------------------------------------
# Decisions — list
# --------------------------------------------------------------------------


def list_decisions(
    *, outcome: str | None = None, limit: int = 100, offset: int = 0
) -> s.DecisionList:
    """List recorded decisions, newest first. Each carries its roll-call of roles
    and its journal (the acceptance's "roles + full journal"). Decisions whose
    motion is sensitive are excluded in the query (redaction)."""
    path = _db_path()
    excluded = _sensitive_motion_ids(path)
    rows = db.list_decisions(
        path,
        outcome=outcome,
        exclude_motions=excluded,
        limit=limit,
        offset=offset,
    )
    records: list[s.DecisionRecord] = []
    for row in rows:
        journal = [_journal_from_row(e) for e in db.list_events(path, row["motion_id"])]
        records.append(
            s.DecisionRecord(decision=_decision_from_row(row), journal=journal)
        )
    return s.DecisionList(decisions=records, limit=limit, offset=offset)


def _sensitive_motion_ids(path: Path) -> list[str]:
    """The ids of motions attributed to a sensitive project — handed to
    :func:`db.list_decisions` as the exclusion set so a redacted motion's
    decision never surfaces. Bounded by the same sensitive-project set the rest of
    the redaction policy uses."""
    ids: list[str] = []
    for project in _sensitive_projects():
        rows = db.list_motions(path, project=project, limit=1000, offset=0)
        ids.extend(r["id"] for r in rows)
    return ids
