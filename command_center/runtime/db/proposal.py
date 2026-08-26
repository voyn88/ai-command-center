"""Proposal table-family: autonomy proposals, evidence/event
append-only side tables and the atomic composition helpers (split out of
the former single-file ``runtime/db.py``; pure move).

Every cross-reference to another db name goes through the package facade
(``import command_center.runtime.db as db``) so tests and callers that
monkeypatch facade attributes (``db.MIGRATIONS``, ``db.iso_now``,
``db._proposal_update``, ...) keep intercepting internal calls exactly as
they did against the single module.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from command_center.runtime import autonomy as autonomy_domain

import command_center.runtime.db as db  # facade (late-bound; see docstring)

_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Autonomy proposals (schema 7) — the pre-execution decision layer.
#
# Mirrors the completion-row idioms: a `create_proposal` write-once insert, a
# compare-and-set `update_proposal` guarded by both a lifecycle-scoped field
# allowlist and the `autonomy.is_valid_proposal_transition` structural guard,
# evidence that is append-only until assessment and frozen afterward, and an
# append-only `append_proposal_event` audit trail. Identity columns
# (id/kind/project/created_at) are write-once and deliberately absent from the
# updatable allowlist below.
# --------------------------------------------------------------------------

_UPDATABLE_PROPOSAL_FIELDS: frozenset[str] = frozenset(
    {
        "task_id",
        "title",
        "rationale",
        "state",
        "risk_level",
        "policy_json",
        "eligibility_json",
        "plan_json",
        "parameters_json",
        "evidence_digest",
        "requires_human",
        "last_reason_code",
        "decided_by",
        "decision_reason",
        "dispatched_run_id",
        "dispatched_task_id",
    }
)


_PROPOSAL_AUTHORITY_FIELDS: frozenset[str] = frozenset(
    {
        "task_id",
        "title",
        "rationale",
        "risk_level",
        "policy_json",
        "eligibility_json",
        "plan_json",
        "parameters_json",
        "evidence_digest",
    }
)

_PROPOSAL_DECISION_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "requires_human",
        "last_reason_code",
        "decided_by",
        "decision_reason",
    }
)

_PROPOSAL_FIELDS_BY_STATE: dict[str, frozenset[str]] = {
    # Assessment may start from either DRAFT or PROPOSED. These are the only
    # states in which authority-bearing fields may be written.
    autonomy_domain.ProposalState.DRAFT: _PROPOSAL_AUTHORITY_FIELDS | _PROPOSAL_DECISION_FIELDS,
    autonomy_domain.ProposalState.PROPOSED: _PROPOSAL_AUTHORITY_FIELDS | _PROPOSAL_DECISION_FIELDS,
    # From this point onward, policy, evidence digest, action parameters,
    # eligibility, risk, and plan are immutable. Lifecycle decisions may still
    # advance the row and record the responsible actor/reason.
    autonomy_domain.ProposalState.ELIGIBLE: _PROPOSAL_DECISION_FIELDS,
    autonomy_domain.ProposalState.BLOCKED: _PROPOSAL_DECISION_FIELDS,
    autonomy_domain.ProposalState.AWAITING_APPROVAL: _PROPOSAL_DECISION_FIELDS,
    autonomy_domain.ProposalState.APPROVED: _PROPOSAL_DECISION_FIELDS,
    # Result links are legal only while confirming a dispatched action.
    autonomy_domain.ProposalState.DISPATCHED: frozenset(
        {"state", "last_reason_code", "dispatched_run_id", "dispatched_task_id"}
    ),
    # Terminal rows permit only an idempotent same-state CAS; their authority
    # and decision attribution cannot be rewritten.
    autonomy_domain.ProposalState.REJECTED: frozenset({"state"}),
    autonomy_domain.ProposalState.EXECUTED: frozenset({"state"}),
    autonomy_domain.ProposalState.WITHDRAWN: frozenset({"state"}),
}


def _validate_updatable_proposal_fields(fields: dict) -> None:
    unknown = set(fields) - db._UPDATABLE_PROPOSAL_FIELDS
    if unknown:
        raise db.UnknownRunFieldError(f"Not an updatable proposal field: {sorted(unknown)}")


def _validate_proposal_fields_for_state(
    state: str,
    fields: dict,
    *,
    assessment_persisted: bool,
) -> None:
    allowed = db._PROPOSAL_FIELDS_BY_STATE.get(state, frozenset())
    # Assessment is normally persisted and routed onward in one transaction,
    # so callers never observe a DRAFT/PROPOSED row with a verdict. Keep the DB
    # boundary safe even if a lower-level caller writes the verdict without its
    # transitions: once the marker exists, authority is frozen immediately.
    if (
        assessment_persisted
        and state
        in {
            db.autonomy_domain.ProposalState.DRAFT,
            db.autonomy_domain.ProposalState.PROPOSED,
        }
    ):
        allowed = db._PROPOSAL_DECISION_FIELDS
    forbidden = set(fields) - allowed
    if forbidden:
        raise db.ProposalFieldFrozenError(
            f"Proposal fields {sorted(forbidden)} cannot be updated in state {state!r}"
        )


def _canonical_proposal_parameters_json(raw: str) -> str:
    """Validate and canonicalize the structured action payload.

    A proposal approves a named parameter object, never an arbitrary JSON
    scalar/list. Canonical serialization makes the persisted authority stable
    for hashing, comparison, and later execution binding.
    """
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("proposal.parameters_json must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("proposal.parameters_json must encode a JSON object")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_PROPOSAL_INSERT_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "project",
    "task_id",
    "title",
    "rationale",
    "state",
    "risk_level",
    "policy_json",
    "eligibility_json",
    "plan_json",
    "parameters_json",
    "evidence_digest",
    "requires_human",
    "last_reason_code",
    "decided_by",
    "decision_reason",
    "dispatched_run_id",
    "dispatched_task_id",
    "version",
    "created_at",
    "updated_at",
)


def create_proposal(
    db_path: Path,
    *,
    kind: str,
    project: str,
    title: str,
    rationale: str,
    state: str,
    risk_level: str,
    proposal_id: str | None = None,
    task_id: str | None = None,
    policy_json: str | None = None,
    parameters_json: str = "{}",
    requires_human: bool = True,
) -> dict:
    """Create one `proposal` row in its initial state.

    `rationale` is required and never blank — a proposal that cannot explain why
    it exists is rejected here, enforcing the "all proposals must explain why
    they were created" rule at the persistence boundary."""
    if not rationale or not str(rationale).strip():
        raise ValueError("proposal.rationale must be non-empty — every proposal must explain itself")
    parameters_json = db._canonical_proposal_parameters_json(parameters_json)
    now = db.iso_now()
    record = {name: None for name in db._PROPOSAL_INSERT_COLUMNS}
    record.update(
        {
            "id": proposal_id or db.new_id(),
            "kind": kind,
            "project": project,
            "task_id": task_id,
            "title": title,
            "rationale": rationale,
            "state": state,
            "risk_level": risk_level,
            "policy_json": policy_json,
            "parameters_json": parameters_json,
            "requires_human": 1 if requires_human else 0,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(db._PROPOSAL_INSERT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in db._PROPOSAL_INSERT_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(f"INSERT INTO proposal ({columns}) VALUES ({placeholders})", record)
            stored = dict(
                conn.execute("SELECT * FROM proposal WHERE id = ?", (record["id"],)).fetchone()
            )
    _mirror("PostgresProposalMirror", stored, "proposal")
    return record

def _mirror(mirror_name: str, record: dict, table: str) -> None:
    """Best-effort dual-write of one proposal-family row (SRV-01B slice 15).

    After the authoritative commit, silent on failure, lazily imported. One
    helper for three tables because the rule is identical.
    """
    try:
        from command_center.db import proposal_store

        getattr(proposal_store, mirror_name)().upsert(record)
    except Exception:  # noqa: BLE001 - the mirror must never break the real write
        _LOG.debug("Could not mirror %s into PostgreSQL", table, exc_info=True)


def _mirror_children(records: list[tuple[str, dict, str]]) -> None:
    """Mirror a transaction's children in the order they were written."""
    for mirror_name, record, table in records:
        _mirror(mirror_name, record, table)




def get_proposal(db_path: Path, proposal_id: str) -> dict | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone()
        return db._row_to_dict(row)


def list_proposals(
    db_path: Path,
    *,
    project: str | None = None,
    states: Iterable[str] | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List proposal rows, newest first, optionally filtered by project, a set
    of lifecycle `states`, and/or `kind`."""
    clauses: list[str] = []
    params: list[Any] = []
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    states = list(states) if states is not None else None
    if states is not None:
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        clauses.append(f"state IN ({placeholders})")
        params.extend(states)
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with db.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM proposal{where} ORDER BY created_at DESC, id DESC LIMIT ?", params
        ).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Connection-level primitives (single-transaction building blocks). Each
# operates on an already-open connection inside a caller-managed transaction,
# so several can be composed into ONE atomic unit — see `create_proposal_atomic`
# / `apply_assessment_atomic` / `transition_proposal_atomic`. The public
# wrappers below open their own `connect()`/`transaction()` and delegate to a
# single primitive. Atomicity is NEVER composed by nesting public functions
# (each of which would open and commit its own connection).
# --------------------------------------------------------------------------


def _proposal_next_seq(conn: sqlite3.Connection, table: str, proposal_id: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM {table} WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    return row["next_seq"]


def _proposal_evidence_insert(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    kind: str,
    source: str,
    summary: str | None,
    observed_at: str,
    is_blocker: bool,
    data: dict | None,
    now: str,
) -> dict:
    seq = db._proposal_next_seq(conn, "proposal_evidence", proposal_id)
    data_json = json.dumps(data, ensure_ascii=False) if data is not None else None
    cur = conn.execute(
        """INSERT INTO proposal_evidence
               (proposal_id, seq, kind, source, summary, observed_at, is_blocker, data_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (proposal_id, seq, kind, source, summary, observed_at, 1 if is_blocker else 0, data_json, now),
    )
    # The stored record, not the sequence number: the target's `id` is
    # `GENERATED ALWAYS AS IDENTITY` and reconciliation pairs rows by it, so the
    # mirror needs the id SQLite just minted. Callers that owe a `seq` take it
    # from the record.
    return {
        "id": cur.lastrowid,
        "proposal_id": proposal_id,
        "seq": seq,
        "kind": kind,
        "source": source,
        "summary": summary,
        "observed_at": observed_at,
        "is_blocker": 1 if is_blocker else 0,
        "data_json": data_json,
        "created_at": now,
    }


def _proposal_event_insert(
    conn: sqlite3.Connection,
    proposal_id: str,
    event_type: str,
    *,
    now: str,
    from_state: str | None = None,
    to_state: str | None = None,
    actor: str | None = None,
    reason_code: str | None = None,
    message: str | None = None,
    metadata: dict | None = None,
) -> dict:
    seq = db._proposal_next_seq(conn, "proposal_event", proposal_id)
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    cur = conn.execute(
        """INSERT INTO proposal_event
               (proposal_id, seq, event_type, from_state, to_state, actor,
                reason_code, message, metadata_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (proposal_id, seq, event_type, from_state, to_state, actor,
         reason_code, message, metadata_json, now),
    )
    return {
        "id": cur.lastrowid,
        "proposal_id": proposal_id,
        "seq": seq,
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state,
        "actor": actor,
        "reason_code": reason_code,
        "message": message,
        "metadata_json": metadata_json,
        "created_at": now,
    }


def _proposal_event_from_spec(conn: sqlite3.Connection, proposal_id: str, spec: dict, *, now: str) -> dict:
    """Append one audit event from a spec dict (`append_proposal_event` kwargs;
    `event_type` key optional). Used by the atomic composers so callers can pass
    plain dicts."""
    spec = dict(spec)
    return db._proposal_event_insert(
        conn,
        proposal_id,
        spec.pop("event_type", "transition"),
        now=now,
        from_state=spec.get("from_state"),
        to_state=spec.get("to_state"),
        actor=spec.get("actor"),
        reason_code=spec.get("reason_code"),
        message=spec.get("message"),
        metadata=spec.get("metadata"),
    )


def _proposal_update(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    expected_version: int,
    fields: dict,
    now: str,
) -> tuple[int, str]:
    """CAS + transition-guarded UPDATE inside an open transaction. Returns
    (new_version, resulting_state). Validates `fields` against the allowlist,
    checks the caller's version before interpreting its requested mutation,
    enforces the lifecycle-scoped field allowlist, then refuses an illegal
    `state` transition (via `is_valid_proposal_transition`). Bumps `version`
    and sets `updated_at`. Raises `KeyError`/`LostUpdateError`/
    `ProposalFieldFrozenError`/`InvalidProposalTransitionError` as appropriate."""
    fields = dict(fields)
    fields.pop("version", None)
    fields.pop("created_at", None)
    db._validate_updatable_proposal_fields(fields)
    row = conn.execute(
        "SELECT state, version, eligibility_json FROM proposal WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"No such proposal: {proposal_id!r}")
    # CAS is deliberately checked before the state-aware mutation/transition
    # guards. A stale writer must always lose as a stale writer; otherwise a
    # winner's newer state could make the loser's old request look like a hard
    # policy/transition violation and misclassify benign concurrency.
    if row["version"] != expected_version:
        raise db.LostUpdateError(
            f"Proposal {proposal_id!r} version mismatch: expected {expected_version}, actual {row['version']}"
        )
    db._validate_proposal_fields_for_state(
        row["state"],
        fields,
        assessment_persisted=row["eligibility_json"] is not None,
    )
    if "requires_human" in fields and isinstance(fields["requires_human"], bool):
        fields["requires_human"] = 1 if fields["requires_human"] else 0
    if "parameters_json" in fields:
        fields["parameters_json"] = db._canonical_proposal_parameters_json(fields["parameters_json"])
    new_state = fields.get("state")
    if new_state is not None and not db.autonomy_domain.is_valid_proposal_transition(row["state"], new_state):
        raise db.InvalidProposalTransitionError(
            f"Proposal {proposal_id!r} cannot transition {row['state']!r} -> {new_state!r}"
        )
    fields["updated_at"] = now
    set_clause = ", ".join(f"{key} = :{key}" for key in fields)
    params = dict(fields)
    params["proposal_id"] = proposal_id
    params["expected_version"] = expected_version
    cur = conn.execute(
        f"""UPDATE proposal SET {set_clause}, version = version + 1
            WHERE id = :proposal_id AND version = :expected_version""",
        params,
    )
    if cur.rowcount != 1:
        raise db.LostUpdateError(f"Proposal {proposal_id!r} update affected {cur.rowcount} rows")
    return expected_version + 1, (new_state if new_state is not None else row["state"])


def update_proposal(db_path: Path, proposal_id: str, *, expected_version: int, fields: dict) -> dict:
    """Compare-and-set update of a `proposal` row, mirroring `update_completion`:
    validates the global field vocabulary, checks CAS first, enforces the
    lifecycle-scoped allowlist, then validates any state transition. Authority
    fields (policy/evidence digest/action parameters/verdict/plan) are mutable
    only before assessment. `requires_human` is coerced to 0/1 if supplied as
    a bool."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            db._proposal_update(conn, proposal_id, expected_version=expected_version, fields=fields, now=now)
            updated = dict(
                conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone()
            )
    _mirror("PostgresProposalMirror", updated, "proposal")
    return updated


def append_proposal_evidence(
    db_path: Path,
    proposal_id: str,
    *,
    kind: str,
    source: str,
    summary: str | None = None,
    observed_at: str,
    is_blocker: bool = False,
    data: dict | None = None,
) -> int:
    """Append one immutable evidence row and return its per-proposal `seq`.
    Evidence may be enriched only before assessment (DRAFT/PROPOSED with no
    persisted verdict). Once assessment has begun, the set is frozen so the
    stored digest and verdict remain reproducible."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            row = conn.execute(
                "SELECT state, eligibility_json FROM proposal WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"No such proposal: {proposal_id!r}")
            if (
                row["state"] not in {
                    db.autonomy_domain.ProposalState.DRAFT,
                    db.autonomy_domain.ProposalState.PROPOSED,
                }
                or row["eligibility_json"] is not None
            ):
                raise db.ProposalEvidenceFrozenError(
                    f"Proposal {proposal_id!r} evidence is frozen in state {row['state']!r}"
                )
            evidence_record = db._proposal_evidence_insert(
                conn, proposal_id, kind=kind, source=source, summary=summary,
                observed_at=observed_at, is_blocker=is_blocker, data=data, now=now,
            )
    _mirror("PostgresProposalEvidenceMirror", evidence_record, "proposal_evidence")
    return evidence_record["seq"]


def list_proposal_evidence(db_path: Path, proposal_id: str) -> list[dict]:
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM proposal_evidence WHERE proposal_id = ? ORDER BY seq ASC",
            (proposal_id,),
        ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            raw = item.pop("data_json")
            item["data"] = json.loads(raw) if raw else None
            item["is_blocker"] = bool(item["is_blocker"])
            events.append(item)
        return events


def list_proposal_evidence_stored(db_path: Path, proposal_id: str) -> list[dict]:
    """Evidence rows in the shape SQLite **stores**, for reconciliation.

    :func:`list_proposal_evidence` pops `data_json` and returns a parsed `data`
    key instead, which is right for its callers and wrong for reconciliation:
    fed those rows it reports every evidence row divergent on `data_json` and
    agrees about a `data` column the target does not have.

    The decoding here is written inline rather than in a `_decode_*` helper,
    which is how it got past the fitness gate until that gate learned to treat
    a `.pop("<column>")` as the same act.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM proposal_evidence WHERE proposal_id = ? ORDER BY seq ASC",
            (proposal_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def append_proposal_event(
    db_path: Path,
    proposal_id: str,
    event_type: str,
    *,
    from_state: str | None = None,
    to_state: str | None = None,
    actor: str | None = None,
    reason_code: str | None = None,
    message: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Append one proposal audit event and return its per-proposal `seq`.
    `metadata` is JSON-encoded; callers must never place credentials, tokens, or
    environment dumps in it."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            event_record = db._proposal_event_insert(
                conn, proposal_id, event_type, now=now, from_state=from_state,
                to_state=to_state, actor=actor, reason_code=reason_code,
                message=message, metadata=metadata,
            )
    _mirror("PostgresProposalEventMirror", event_record, "proposal_event")
    return event_record["seq"]


def list_proposal_events_stored(db_path: Path, proposal_id: str) -> list[dict]:
    """Every proposal event in the shape SQLite **stores**, for reconciliation.

    :func:`list_proposal_events` hands out the shape callers want, which is not
    the shape the mirror holds — the same split `digest_item`, `model_event`,
    `audit_run`, `run_event`, `council_event` and `completion_event` all have.
    Six tables into that pattern, the gate that requires this reader is worth
    more than the memory that used to.
    """
    with db.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM proposal_event WHERE proposal_id = ? ORDER BY seq ASC",
            (proposal_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_proposal_events(db_path: Path, proposal_id: str, *, limit: int = 500) -> list[dict]:
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT proposal_id, seq, event_type, from_state, to_state, actor,
                      reason_code, message, metadata_json, created_at
               FROM proposal_event WHERE proposal_id = ? ORDER BY seq ASC LIMIT ?""",
            (proposal_id, limit),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            raw = event.pop("metadata_json")
            event["metadata"] = json.loads(raw) if raw else None
            events.append(event)
        return events


# --------------------------------------------------------------------------
# Atomic proposal composers — each owns its whole multi-row sequence in ONE
# connection and ONE transaction, so either all rows commit or none do. This is
# the F2 remediation: a crash can never leave a proposal without its required
# evidence/digest/creation-event, nor a verdict without its state transition and
# ASSESSED event, nor a lifecycle move without its audit event.
# --------------------------------------------------------------------------


def create_proposal_atomic(
    db_path: Path,
    *,
    kind: str,
    project: str,
    title: str,
    rationale: str,
    state: str,
    risk_level: str,
    proposal_id: str | None = None,
    task_id: str | None = None,
    policy_json: str | None = None,
    parameters_json: str = "{}",
    requires_human: bool = True,
    evidence_digest: str | None = None,
    evidence: list[dict] | None = None,
    created_event: dict | None = None,
) -> dict:
    """Create a proposal row, its (immutable) evidence rows, its evidence digest,
    and its CREATED audit event in ONE transaction. Either all commit or none do
    — there is never a persisted proposal without its rationale (rejected here if
    blank), evidence, digest, and creation event. `evidence` is a list of dicts
    (kind/source/summary/observed_at/is_blocker/data); `created_event` is a dict
    of `append_proposal_event` kwargs (`event_type` defaults to 'created').
    Returns the persisted proposal row. Idempotency is by `proposal_id`: a retry
    with the same id hits the PRIMARY KEY (IntegrityError) rather than creating a
    duplicate; a retry after a rolled-back attempt succeeds cleanly."""
    if not rationale or not str(rationale).strip():
        raise ValueError("proposal.rationale must be non-empty — every proposal must explain itself")
    parameters_json = db._canonical_proposal_parameters_json(parameters_json)
    now = db.iso_now()
    pid = proposal_id or db.new_id()
    record = {name: None for name in db._PROPOSAL_INSERT_COLUMNS}
    record.update(
        {
            "id": pid,
            "kind": kind,
            "project": project,
            "task_id": task_id,
            "title": title,
            "rationale": rationale,
            "state": state,
            "risk_level": risk_level,
            "policy_json": policy_json,
            "parameters_json": parameters_json,
            "evidence_digest": evidence_digest,
            "requires_human": 1 if requires_human else 0,
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    columns = ", ".join(db._PROPOSAL_INSERT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in db._PROPOSAL_INSERT_COLUMNS)
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(f"INSERT INTO proposal ({columns}) VALUES ({placeholders})", record)
            children: list[tuple[str, dict, str]] = []
            for e in evidence or []:
                children.append(
                    (
                        "PostgresProposalEvidenceMirror",
                        db._proposal_evidence_insert(
                            conn, pid, kind=e["kind"], source=e["source"],
                            summary=e.get("summary"), observed_at=e["observed_at"],
                            is_blocker=bool(e.get("is_blocker", False)),
                            data=e.get("data"), now=now,
                        ),
                        "proposal_evidence",
                    )
                )
            if created_event is not None:
                children.append(
                    (
                        "PostgresProposalEventMirror",
                        db._proposal_event_from_spec(conn, pid, created_event, now=now),
                        "proposal_event",
                    )
                )
            stored = dict(conn.execute("SELECT * FROM proposal WHERE id = ?", (pid,)).fetchone())
    # Parent first, then its children in write order: the target refuses a
    # child whose proposal is not mirrored yet.
    _mirror("PostgresProposalMirror", stored, "proposal")
    _mirror_children(children)
    return stored


def apply_assessment_atomic(
    db_path: Path,
    proposal_id: str,
    *,
    expected_version: int,
    verdict_fields: dict,
    assessed_event: dict,
    transitions: list[dict],
) -> dict:
    """Persist an assessment verdict, its ASSESSED audit event, and the resulting
    ordered state transitions (each with its own audit event) in ONE transaction.

    A single CAS on `expected_version` guards the whole unit: a stale/concurrent
    assessor loses with `LostUpdateError` and writes nothing. Guarantees: no
    verdict without its transitions/events (and vice versa); the audit sequence
    stays monotonic; and — because the caller only invokes this while the
    proposal is still in a pre-assessment state — exactly one ASSESSED event per
    committed assessment. `transitions` is an ordered list of
    ``{"new_state": str, "event": {<append_proposal_event kwargs>},
    "extra_fields": {<optional proposal columns>}}``; each transition's version
    is chained from the previous update within the same locked transaction."""
    now = db.iso_now()
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            version, _state = db._proposal_update(
                conn, proposal_id, expected_version=expected_version,
                fields=dict(verdict_fields), now=now,
            )
            children = [
                (
                    "PostgresProposalEventMirror",
                    db._proposal_event_from_spec(
                        conn, proposal_id, {"event_type": "assessed", **assessed_event}, now=now
                    ),
                    "proposal_event",
                )
            ]
            for t in transitions:
                fields = dict(t.get("extra_fields") or {})
                fields["state"] = t["new_state"]
                event_spec = dict(t["event"])
                reason_code = event_spec.get("reason_code")
                if reason_code is not None and "last_reason_code" not in fields:
                    fields["last_reason_code"] = reason_code
                version, _state = db._proposal_update(
                    conn, proposal_id, expected_version=version, fields=fields, now=now,
                )
                children.append(
                    (
                        "PostgresProposalEventMirror",
                        db._proposal_event_from_spec(conn, proposal_id, event_spec, now=now),
                        "proposal_event",
                    )
                )
            updated = dict(
                conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone()
            )
    _mirror("PostgresProposalMirror", updated, "proposal")
    _mirror_children(children)
    return updated


def transition_proposal_atomic(
    db_path: Path,
    proposal_id: str,
    *,
    expected_version: int,
    new_state: str,
    event: dict,
    fields: dict | None = None,
) -> dict:
    """Apply one state transition and its audit event atomically (CAS update +
    event in one transaction), so a lifecycle move can never persist a state
    change without its audit event, or an audit event without the state change."""
    now = db.iso_now()
    upd_fields = dict(fields or {})
    upd_fields["state"] = new_state
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            db._proposal_update(conn, proposal_id, expected_version=expected_version, fields=upd_fields, now=now)
            event_record = db._proposal_event_from_spec(conn, proposal_id, event, now=now)
            updated = dict(
                conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone()
            )
    _mirror("PostgresProposalMirror", updated, "proposal")
    _mirror("PostgresProposalEventMirror", event_record, "proposal_event")
    return updated
