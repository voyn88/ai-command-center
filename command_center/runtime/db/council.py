"""Wave-3 Council / Board-of-Directors engine table-family (VOYN-W3-COUNCIL):
the persistence tier behind the collective-decision surface (routes → service →
repository → db).

This module owns four additive tables — ``motion``, ``council_vote``,
``council_decision`` and ``council_event`` — wholly separate from every other
family in :mod:`command_center.runtime.db`; the names never collide. Every write
goes through the shared ``connect()``/``transaction()`` primitives (WAL,
``BEGIN IMMEDIATE`` write lock, per-row ``version`` compare-and-set on the
mutable ``motion`` row), so the single-writer discipline the rest of
``runtime.db`` follows holds here too.

The pipeline is Motion → Vote → Decision:

* A **motion** is a mutable current-state row moving through an explicit status
  allowlist (:data:`MOTION_TRANSITIONS`, ``open → decided`` / ``open →
  withdrawn``). ``decided`` and ``withdrawn`` are terminal.
* A **vote** is append-only. ``UNIQUE(motion_id, voter_id)`` makes a second vote
  by the same voter a structural :class:`DoubleVoteError` (the service surfaces
  it as HTTP 409), so "one vote per voter" is enforced *at this boundary*, not
  merely in a higher tier. Every vote records the voter's ``role`` at the moment
  it was cast (the roles-recorded invariant) — there is no update path, so the
  record of how each member voted can never be rewritten.
* A **decision** is written exactly once (:func:`record_decision`), inside the
  same transaction that moves the motion to ``decided``. It is an ADR-style
  immutable record: the frozen tally, the snapshot of every voter's role +
  choice, and the rationale explaining the outcome. There is **no** update
  function — once recorded, a decision is source of truth.
* A **journal** (``council_event``) is append-only, ordered by a per-motion
  monotonic ``seq`` (mirrors ``run_event``/``proposal_event``): every critical
  action is one row, so a decision always carries a full, replayable audit trail.

``project_ref`` (nullable) is the redaction key: :func:`list_motions` and
:func:`list_decisions` drop BANK/LEGAL rows *in the SQL query* so a sensitive
motion's content never leaves the read surface (the exclude-in-SQL pattern the
Wave-1/Wave-2 families use).

Statuses/kinds/choices are stored as their stable string *values* (never a Python
enum's member name) so a column round-trips to exactly the Literal the API
contract (``api/models.py``) declares — the enum-name lesson.

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes keep intercepting internal calls exactly as they
do for the other table-family modules.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import command_center.runtime.db as db  # facade (late-bound; see docstring)

from command_center.runtime.db.wave1 import _exclude_projects_clause

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Allowlists (mirror ``api.models`` Literals; validated at the boundary)
# --------------------------------------------------------------------------

#: A motion's lifecycle (mirrors ``api.models.Motion.status``). ``decided`` and
#: ``withdrawn`` are terminal.
MOTION_STATUSES: frozenset[str] = frozenset({"open", "decided", "withdrawn"})

#: Allowed motion status edges. An ``open`` motion is either decided (quorum met,
#: tally recorded) or withdrawn; both are terminal, so a decided motion can never
#: reopen for more votes and its decision stays immutable.
MOTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"decided", "withdrawn"}),
    "decided": frozenset(),
    "withdrawn": frozenset(),
}

#: The kinds of voter recognised on a vote (mirrors ``api.models.VoterKind``).
VOTER_KINDS: frozenset[str] = frozenset({"ai", "human"})

#: The choices a voter may cast (mirrors ``api.models.VoteChoice``). ``abstain``
#: counts toward quorum but toward neither side of the tally.
VOTE_CHOICES: frozenset[str] = frozenset({"yes", "no", "abstain"})

#: A decision's outcome (mirrors ``api.models.DecisionOutcome``).
DECISION_OUTCOMES: frozenset[str] = frozenset({"approved", "rejected", "deferred"})


class InvalidMotionTransitionError(Exception):
    """Raised when a motion status change is not an allowed edge in
    :data:`MOTION_TRANSITIONS` (a backward jump, or any move out of a terminal
    state)."""


class DoubleVoteError(Exception):
    """Raised when a voter casts a second vote on the same motion — the
    ``UNIQUE(motion_id, voter_id)`` constraint refuses it. The service surfaces
    this as HTTP 409 (one vote per voter)."""


class MotionNotOpenError(Exception):
    """Raised when a vote or a close is attempted on a motion that is no longer
    ``open`` (already decided or withdrawn). Surfaced as HTTP 409 — the motion
    cannot accept more votes and its decision, once recorded, is immutable."""


class DecisionAlreadyRecordedError(Exception):
    """Raised when a decision already exists for a motion — a decision is written
    exactly once and never overwritten."""


# --------------------------------------------------------------------------
# motion
# --------------------------------------------------------------------------

_MOTION_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "body",
    "proposed_by",
    "quorum",
    "project_ref",
    "proposal_ref",
    "source_ref",
    "status",
    "opened_at",
    "decided_at",
    "version",
    "created_at",
    "updated_at",
)


def create_motion(
    db_path: Path,
    *,
    title: str,
    proposed_by: str,
    body: str = "",
    quorum: int = 1,
    project_ref: str | None = None,
    proposal_ref: str | None = None,
    source_ref: str | None = None,
    motion_id: str | None = None,
) -> dict:
    """Insert one ``motion`` row in its ``open`` state and return it (plus a
    ``motion_opened`` journal entry, atomically in the same transaction).

    ``title`` and ``proposed_by`` must be non-empty — a motion always has a
    subject and someone who raised it. ``quorum`` is the number of votes required
    before the motion may close; it must be at least 1."""
    if not title or not str(title).strip():
        raise ValueError("motion.title must be non-empty")
    if not proposed_by or not str(proposed_by).strip():
        raise ValueError("motion.proposed_by must be non-empty")
    if int(quorum) < 1:
        raise ValueError(f"motion.quorum must be >= 1, got {quorum!r}")
    now = db.iso_now()
    record = {name: None for name in _MOTION_COLUMNS}
    record.update(
        {
            "id": motion_id or db.new_id(),
            "title": title,
            "body": body or "",
            "proposed_by": proposed_by,
            "quorum": int(quorum),
            "project_ref": project_ref,
            "proposal_ref": proposal_ref,
            "source_ref": source_ref,
            "status": "open",
            "opened_at": now,
            "decided_at": None,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(_MOTION_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _MOTION_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                f"INSERT INTO motion ({columns}) VALUES ({placeholders})",
                record,
            )
            event = _append_event(
                conn,
                record["id"],
                event_type="motion_opened",
                actor=proposed_by,
                message=f"motion opened (quorum {int(quorum)})",
                metadata={"quorum": int(quorum), "source_ref": source_ref},
                now=now,
            )
    # Motion then journal, both after the commit: the journal's foreign key
    # would refuse a row whose motion is not mirrored yet.
    _mirror_motion(record)
    _mirror_council_event(event)
    return dict(record)

def _mirror_motion(record: dict) -> None:
    """Best-effort dual-write of one motion into PostgreSQL (SRV-01B slice 9).

    After the authoritative commit and silent on failure, as every mirror since
    slice 2. Parent of three foreign keys, so a swallowed failure here costs
    every vote, decision and event that follows for that motion — the
    compounding slice 5 measured.
    """
    try:
        from command_center.db.council_store import PostgresMotionMirror

        PostgresMotionMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror motion into PostgreSQL", exc_info=True)


def _mirror_vote(record: dict) -> None:
    """Best-effort dual-write of one vote into PostgreSQL (slice 9)."""
    try:
        from command_center.db.council_store import PostgresCouncilVoteMirror

        PostgresCouncilVoteMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror council_vote into PostgreSQL", exc_info=True)


def _mirror_decision(record: dict) -> None:
    """Best-effort dual-write of one decision into PostgreSQL (slice 9).

    Takes the **stored** record: `_decode_decision_row` pops `tally_json` and
    `roles_json`, and mirroring the decoded shape would write both columns'
    defaults on every row.
    """
    try:
        from command_center.db.council_store import PostgresCouncilDecisionMirror

        PostgresCouncilDecisionMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror council_decision into PostgreSQL", exc_info=True)


def _mirror_council_event(record: dict) -> None:
    """Best-effort dual-write of one journal row into PostgreSQL (slice 9).

    Takes the stored record for two reasons at once: the decoded row drops
    `metadata_json`, and it has no `id` — which an identity column refuses to
    take as `None`.
    """
    try:
        from command_center.db.council_store import PostgresCouncilEventMirror

        PostgresCouncilEventMirror().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror council_event into PostgreSQL", exc_info=True)




def get_motion(db_path: Path, motion_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM motion WHERE id = ?", (motion_id,)
        ).fetchone()
        return db._row_to_dict(row)


def get_motion_by_source_ref(db_path: Path, source_ref: str) -> dict | None:
    """Return the most recent motion opened from ``source_ref`` (or ``None``).

    The dedup primitive behind the event intake: a redelivered ``ProposalCreated``
    / ``IncidentOpened`` carrying the same ``source_ref`` must not open a second
    motion. An empty ``source_ref`` never dedups (manual API motions share the
    NULL default)."""
    if not source_ref:
        return None
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM motion WHERE source_ref = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (source_ref,),
        ).fetchone()
        return db._row_to_dict(row)


def list_motions(
    db_path: Path,
    *,
    status: str | None = None,
    project: str | None = None,
    exclude_projects: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List motions, newest first, optionally filtered by ``status`` and/or
    ``project``. ``limit``/``offset`` page the result (stable order:
    ``created_at DESC, id DESC``).

    ``exclude_projects`` drops rows for those projects *in the query* (redaction),
    so a page counts only visible rows. Un-attributed motions (``project_ref IS
    NULL``) are always kept."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if project is not None:
        clauses.append("project_ref = ?")
        params.append(project)
    exclude_clause, exclude_params = _exclude_projects_clause(
        exclude_projects, allow_null=True
    )
    if exclude_clause is not None:
        clauses.append(exclude_clause)
        params.extend(exclude_params)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM motion{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# council_vote
# --------------------------------------------------------------------------

_VOTE_COLUMNS: tuple[str, ...] = (
    "id",
    "motion_id",
    "voter_id",
    "voter_kind",
    "role",
    "choice",
    "rationale",
    "created_at",
)


def cast_vote(
    db_path: Path,
    *,
    motion_id: str,
    voter_id: str,
    role: str,
    choice: str,
    voter_kind: str = "ai",
    rationale: str | None = None,
    vote_id: str | None = None,
) -> dict:
    """Record one vote on a motion and return it (plus a ``vote_cast`` journal
    entry, atomically).

    Enforces at the persistence boundary:

    * the motion exists and is still ``open`` (else :class:`KeyError` /
      :class:`MotionNotOpenError`);
    * ``choice`` and ``voter_kind`` are in their allowlists, ``voter_id`` and
      ``role`` are non-empty (every vote records who voted and in what role);
    * one vote per voter — a second vote by the same voter raises
      :class:`DoubleVoteError` (the ``UNIQUE(motion_id, voter_id)`` constraint).

    The whole check-and-insert runs inside one ``BEGIN IMMEDIATE`` transaction, so
    two voters racing on the same motion serialise here rather than both slipping
    past the open-status check."""
    if not voter_id or not str(voter_id).strip():
        raise ValueError("vote.voter_id must be non-empty")
    if not role or not str(role).strip():
        raise ValueError("vote.role must be non-empty (every vote records a role)")
    if choice not in VOTE_CHOICES:
        raise ValueError(
            f"vote.choice must be one of {sorted(VOTE_CHOICES)}, got {choice!r}"
        )
    if voter_kind not in VOTER_KINDS:
        raise ValueError(
            f"vote.voter_kind must be one of {sorted(VOTER_KINDS)}, got {voter_kind!r}"
        )
    now = db.iso_now()
    record = {
        "id": vote_id or db.new_id(),
        "motion_id": motion_id,
        "voter_id": voter_id,
        "voter_kind": voter_kind,
        "role": role,
        "choice": choice,
        "rationale": rationale,
        "created_at": now,
    }
    columns = ", ".join(_VOTE_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _VOTE_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            motion = conn.execute(
                "SELECT status FROM motion WHERE id = ?", (motion_id,)
            ).fetchone()
            if motion is None:
                raise KeyError(f"No such motion: {motion_id!r}")
            if motion["status"] != "open":
                raise MotionNotOpenError(
                    f"motion {motion_id!r} is {motion['status']!r}, not open — "
                    "no further votes are accepted"
                )
            try:
                conn.execute(
                    f"INSERT INTO council_vote ({columns}) VALUES ({placeholders})",
                    record,
                )
            except sqlite3.IntegrityError as exc:
                # UNIQUE(motion_id, voter_id) — the voter already voted.
                raise DoubleVoteError(
                    f"voter {voter_id!r} has already voted on motion {motion_id!r}"
                ) from exc
            event = _append_event(
                conn,
                motion_id,
                event_type="vote_cast",
                actor=voter_id,
                role=role,
                message=f"{voter_id} ({role}) voted {choice}",
                metadata={"choice": choice, "voter_kind": voter_kind},
                now=now,
            )
    # Vote then journal, both after the commit. The foreign keys require the
    # motion to be mirrored first; on the happy path `create_motion` did that,
    # and when its own mirror failed these two are refused as well — silently,
    # like every mirror failure, and visible only to reconciliation.
    _mirror_vote(record)
    _mirror_council_event(event)
    return dict(record)


def list_votes(db_path: Path, motion_id: str) -> list[dict]:
    """Every vote cast on ``motion_id``, oldest first (the order they were cast)."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM council_vote WHERE motion_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (motion_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_votes(db_path: Path, motion_id: str) -> int:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM council_vote WHERE motion_id = ?",
            (motion_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0


# --------------------------------------------------------------------------
# council_decision  (immutable — written once, no update path)
# --------------------------------------------------------------------------

_DECISION_COLUMNS: tuple[str, ...] = (
    "motion_id",
    "id",
    "outcome",
    "tally_json",
    "roles_json",
    "rationale",
    "quorum",
    "decided_at",
    "created_at",
)


def record_decision(
    db_path: Path,
    *,
    motion_id: str,
    expected_version: int,
    outcome: str,
    tally: dict,
    roles: list[dict],
    rationale: str,
    quorum: int,
    decision_id: str | None = None,
) -> dict:
    """Write the immutable decision for a motion and move the motion to
    ``decided`` — both in one ``BEGIN IMMEDIATE`` transaction.

    Refuses:

    * an ``outcome`` outside :data:`DECISION_OUTCOMES`;
    * a motion that does not exist (:class:`KeyError`), is not ``open``
      (:class:`MotionNotOpenError`), or lost the version race
      (:class:`db.LostUpdateError`);
    * a second decision for the same motion (:class:`DecisionAlreadyRecordedError`
      — the ``motion_id`` primary key).

    ``roles`` is the frozen snapshot of every voter's ``{voter_id, voter_kind,
    role, choice}`` — the ADR "who decided, and how" record. Returns the decision
    row (with ``tally``/``roles`` decoded)."""
    if outcome not in DECISION_OUTCOMES:
        raise ValueError(
            f"decision.outcome must be one of {sorted(DECISION_OUTCOMES)}, got {outcome!r}"
        )
    now = db.iso_now()
    record = {
        "motion_id": motion_id,
        "id": decision_id or db.new_id(),
        "outcome": outcome,
        "tally_json": json.dumps(tally, ensure_ascii=False, sort_keys=True),
        "roles_json": json.dumps(roles, ensure_ascii=False),
        "rationale": rationale or "",
        "quorum": int(quorum),
        "decided_at": now,
        "created_at": now,
    }
    columns = ", ".join(_DECISION_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _DECISION_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            motion = conn.execute(
                "SELECT status, version FROM motion WHERE id = ?", (motion_id,)
            ).fetchone()
            if motion is None:
                raise KeyError(f"No such motion: {motion_id!r}")
            if motion["version"] != expected_version:
                raise db.LostUpdateError(
                    f"motion {motion_id!r} version mismatch: "
                    f"expected {expected_version}, actual {motion['version']}"
                )
            if motion["status"] != "open":
                raise MotionNotOpenError(
                    f"motion {motion_id!r} is {motion['status']!r}, not open — "
                    "its decision is already recorded"
                )
            try:
                conn.execute(
                    f"INSERT INTO council_decision ({columns}) VALUES ({placeholders})",
                    record,
                )
            except sqlite3.IntegrityError as exc:
                raise DecisionAlreadyRecordedError(
                    f"motion {motion_id!r} already has a recorded decision"
                ) from exc
            cur = conn.execute(
                "UPDATE motion SET status = 'decided', decided_at = :now, "
                "updated_at = :now, version = version + 1 "
                "WHERE id = :motion_id AND version = :expected_version",
                {"now": now, "motion_id": motion_id, "expected_version": expected_version},
            )
            if cur.rowcount != 1:
                raise db.LostUpdateError(
                    f"motion {motion_id!r} decide affected {cur.rowcount} rows"
                )
            event = _append_event(
                conn,
                motion_id,
                event_type="decision_recorded",
                actor="council",
                message=f"decision recorded: {outcome} — {rationale}",
                metadata={"outcome": outcome, "tally": tally},
                now=now,
            )
            motion_row = dict(
                conn.execute("SELECT * FROM motion WHERE id = ?", (motion_id,)).fetchone()
            )
    # The motion first: this path moves it to `decided`, so the mirrored parent
    # must carry that before its children arrive.
    _mirror_motion(motion_row)
    _mirror_decision(record)
    _mirror_council_event(event)
    return _decode_decision_row(record)


def get_decision(db_path: Path, motion_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM council_decision WHERE motion_id = ?", (motion_id,)
        ).fetchone()
        return _decode_decision_row(dict(row)) if row is not None else None


def list_decisions(
    db_path: Path,
    *,
    outcome: str | None = None,
    exclude_motions: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List decisions, newest first, optionally filtered by ``outcome``.

    ``exclude_motions`` drops decisions for those motion ids *in the query* — the
    redaction hook (the service resolves sensitive motions to their ids), so a
    ``LIMIT``/``OFFSET`` page counts only visible decisions."""
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    clauses: list[str] = []
    params: list[Any] = []
    if outcome is not None:
        clauses.append("outcome = ?")
        params.append(outcome)
    excluded = [m for m in (exclude_motions or []) if m]
    if excluded:
        placeholders = ", ".join("?" for _ in excluded)
        clauses.append(f"motion_id NOT IN ({placeholders})")
        params.extend(excluded)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM council_decision{where} "
            "ORDER BY created_at DESC, motion_id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_decode_decision_row(dict(row)) for row in rows]


# --------------------------------------------------------------------------
# council_event  (append-only journal)
# --------------------------------------------------------------------------


def _append_event(
    conn: sqlite3.Connection,
    motion_id: str,
    *,
    event_type: str,
    now: str,
    actor: str | None = None,
    role: str | None = None,
    message: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Append one journal row inside an already-open transaction, assigning the
    next per-motion ``seq``. The journal is append-only — this is the only writer,
    and there is no update/delete path."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM council_event WHERE motion_id = ?",
        (motion_id,),
    ).fetchone()
    seq = int(row["s"]) + 1
    record = {
        "motion_id": motion_id,
        "seq": seq,
        "event_type": event_type,
        "actor": actor,
        "role": role,
        "message": message,
        "metadata_json": json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
        "created_at": now,
    }
    cur = conn.execute(
        "INSERT INTO council_event "
        "(motion_id, seq, event_type, actor, role, message, metadata_json, created_at) "
        "VALUES (:motion_id, :seq, :event_type, :actor, :role, :message, :metadata_json, :created_at)",
        record,
    )
    # SQLite mints the id and the mirror needs *this* one: `council_event.id` is
    # `GENERATED ALWAYS AS IDENTITY` in the target, which refuses a non-DEFAULT
    # value without `OVERRIDING SYSTEM VALUE`, and reconciliation pairs rows by
    # id. Returned as the **stored** shape, not the decoded one — the decoded
    # row has no `metadata_json` and no id.
    record["id"] = cur.lastrowid
    return record


def list_events(db_path: Path, motion_id: str) -> list[dict]:
    """The full journal for a motion, in ``seq`` order (the order things
    happened) — the audit trail every decision carries."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM council_event WHERE motion_id = ? ORDER BY seq ASC",
            (motion_id,),
        ).fetchall()
        return [_decode_council_event_row(dict(row)) for row in rows]


def withdraw_motion(
    db_path: Path, motion_id: str, *, expected_version: int, actor: str = "council"
) -> dict:
    """Withdraw an ``open`` motion (a terminal move that records no decision) and
    journal it. CAS-guarded and transition-guarded; a decided/withdrawn motion
    cannot be withdrawn."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT status, version FROM motion WHERE id = ?", (motion_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No such motion: {motion_id!r}")
            if row["version"] != expected_version:
                raise db.LostUpdateError(
                    f"motion {motion_id!r} version mismatch: "
                    f"expected {expected_version}, actual {row['version']}"
                )
            if "withdrawn" not in MOTION_TRANSITIONS.get(row["status"], frozenset()):
                raise InvalidMotionTransitionError(
                    f"motion {motion_id!r} cannot transition "
                    f"{row['status']!r} -> 'withdrawn'"
                )
            conn.execute(
                "UPDATE motion SET status = 'withdrawn', updated_at = :now, "
                "version = version + 1 WHERE id = :motion_id AND version = :expected_version",
                {"now": now, "motion_id": motion_id, "expected_version": expected_version},
            )
            event = _append_event(
                conn,
                motion_id,
                event_type="motion_withdrawn",
                actor=actor,
                message="motion withdrawn",
                now=now,
            )
            updated = conn.execute(
                "SELECT * FROM motion WHERE id = ?", (motion_id,)
            ).fetchone()
            record = dict(updated)
    _mirror_motion(record)
    _mirror_council_event(event)
    return record


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def list_decisions_stored(db_path: Path) -> list[dict]:
    """Every decision in the shape SQLite **stores**, for reconciliation.

    :func:`get_decision`, :func:`list_decisions` and :func:`record_decision` all
    return :func:`_decode_decision_row` output, which pops ``tally_json`` and
    ``roles_json`` in favour of decoded ``tally``/``roles``. Reconciliation
    compares stored against stored, so fed a decoded row it reports every
    decision divergent on both JSON columns.

    Ordered by ``motion_id`` — this table's actual primary key.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM council_decision ORDER BY motion_id").fetchall()
        return [dict(row) for row in rows]


def list_events_stored(db_path: Path) -> list[dict]:
    """Every journal row in the shape SQLite **stores**, for reconciliation.

    :func:`list_events` decodes ``metadata_json`` away and the journal's id is a
    storage detail there; reconciliation needs both, and pairs rows by id.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM council_event ORDER BY id").fetchall()
        return [dict(row) for row in rows]


def _decode_decision_row(row: dict) -> dict:
    """Return a copy of a ``council_decision`` row with ``tally_json``/``roles_json``
    decoded to ``tally``/``roles`` (the JSON columns stay internal to the
    repository)."""
    out = dict(row)
    tally_raw = out.pop("tally_json", "{}")
    roles_raw = out.pop("roles_json", "[]")
    out["tally"] = json.loads(tally_raw) if tally_raw else {}
    out["roles"] = json.loads(roles_raw) if roles_raw else []
    return out


def _decode_council_event_row(row: dict) -> dict:
    """Return a copy of a ``council_event`` row with ``metadata_json`` decoded to
    ``metadata``."""
    out = dict(row)
    raw = out.pop("metadata_json", None)
    out["metadata"] = json.loads(raw) if raw else None
    return out
