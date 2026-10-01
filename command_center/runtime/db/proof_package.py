"""VOYN-MIN-WOW-1: the client-facing "proof package" — one artifact per
project that assembles four pillars from data this system already commits,
rather than a new store of its own:

* **Digital Memory** — the chronological narrative of what happened: every
  council journal entry (``council_event``) and proposal lifecycle event
  (``proposal_event``) attributed to the project, merged into one timeline.
* **Counterfactual** — the paths considered and not taken: motions withdrawn
  before a decision, and decisions the Board rejected, each carrying the
  rationale that explains why.
* **Decision P&L** — the subset of decisions that recorded an estimated
  financial/time impact (``council_decision.impact_json``, VOYN-MIN-WOW-1's
  additive migration 26).
* **Audit Vault** — the immutable evidence rows (``proposal_evidence``)
  backing every proposal raised for the project: append-only, one row per
  observation, never edited after assessment begins.

This module is a pure, read-only aggregator: it composes existing repository
functions (``council.py``, ``proposal.py``) and adds no new write path of its
own, no new mutable state, and no sensitivity policy — the BANK/LEGAL
redaction decision belongs to the service tier (:mod:`command_center.council`'s
``is_sensitive`` pattern), the same layering every other Wave family uses.

``build_proof_package`` stamps the assembled document with a SHA-256
``integrity_hash`` over its own canonical JSON — the "доказуемый" (provable)
half of the acceptance criterion: a client holding the JSON can recompute the
hash and detect any change to the package after it was generated.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import command_center.runtime.db as db  # facade (late-bound; see council.py's docstring)


# --------------------------------------------------------------------------
# Digital Memory — the merged, chronological narrative
# --------------------------------------------------------------------------


def _council_memory_events(db_path: Path, project: str) -> list[dict]:
    events: list[dict] = []
    for motion in db.list_motions(db_path, project=project, limit=1000):
        for row in db.list_events(db_path, motion["id"]):
            events.append(
                {
                    "source": "council",
                    "ref_id": motion["id"],
                    "ref_title": motion.get("title") or "",
                    "event_type": row["event_type"],
                    "actor": row.get("actor"),
                    "message": row.get("message"),
                    "created_at": row["created_at"],
                }
            )
    return events


def _proposal_memory_events(db_path: Path, project: str) -> list[dict]:
    events: list[dict] = []
    for proposal in db.list_proposals(db_path, project=project, limit=1000):
        for row in db.list_proposal_events(db_path, proposal["id"]):
            events.append(
                {
                    "source": "proposal",
                    "ref_id": proposal["id"],
                    "ref_title": proposal.get("title") or "",
                    "event_type": row["event_type"],
                    "actor": row.get("actor"),
                    "message": row.get("message"),
                    "created_at": row["created_at"],
                }
            )
    return events


def build_digital_memory(db_path: Path, *, project: str) -> list[dict]:
    """Every council and proposal journal entry attributed to ``project``,
    merged into one timeline ordered oldest first (the order things
    happened)."""
    events = _council_memory_events(db_path, project) + _proposal_memory_events(
        db_path, project
    )
    events.sort(key=lambda e: (e["created_at"], e["source"], e["ref_id"]))
    return events


# --------------------------------------------------------------------------
# Counterfactual — the paths not taken
# --------------------------------------------------------------------------


def build_counterfactual(db_path: Path, *, project: str) -> list[dict]:
    """The alternatives the Board considered and did not take, for
    ``project``: motions withdrawn before a decision, and decisions the Board
    rejected. Each entry carries the rationale on record, ordered oldest
    first."""
    entries: list[dict] = []
    for motion in db.list_motions(db_path, project=project, limit=1000):
        if motion["status"] == "withdrawn":
            entries.append(
                {
                    "kind": "withdrawn_motion",
                    "ref_id": motion["id"],
                    "title": motion.get("title") or "",
                    "rationale": "withdrawn before a decision was recorded",
                    "at": motion.get("updated_at") or motion.get("opened_at"),
                }
            )
            continue
        decision = db.get_decision(db_path, motion["id"])
        if decision is not None and decision["outcome"] == "rejected":
            entries.append(
                {
                    "kind": "rejected_decision",
                    "ref_id": motion["id"],
                    "title": motion.get("title") or "",
                    "rationale": decision.get("rationale") or "",
                    "tally": decision.get("tally") or {},
                    "at": decision.get("decided_at"),
                }
            )
    entries.sort(key=lambda e: e["at"] or "")
    return entries


# --------------------------------------------------------------------------
# Decision P&L — decisions that recorded an estimated impact
# --------------------------------------------------------------------------


def build_decision_pnl(db_path: Path, *, project: str) -> list[dict]:
    """Every decision for ``project`` that recorded an estimated
    financial/time impact (``council_decision.impact_json``), oldest first.
    Decisions that never recorded one are omitted — silence, not a zero."""
    entries: list[dict] = []
    for motion in db.list_motions(db_path, project=project, limit=1000):
        decision = db.get_decision(db_path, motion["id"])
        if decision is not None and decision.get("impact"):
            entries.append(
                {
                    "ref_id": motion["id"],
                    "title": motion.get("title") or "",
                    "outcome": decision["outcome"],
                    "impact": decision["impact"],
                    "decided_at": decision.get("decided_at"),
                }
            )
    entries.sort(key=lambda e: e["decided_at"] or "")
    return entries


# --------------------------------------------------------------------------
# Audit Vault — immutable evidence backing every proposal
# --------------------------------------------------------------------------


def build_audit_vault(db_path: Path, *, project: str) -> list[dict]:
    """Every immutable evidence row backing a proposal raised for
    ``project`` (``proposal_evidence``), oldest first."""
    entries: list[dict] = []
    for proposal in db.list_proposals(db_path, project=project, limit=1000):
        for row in db.list_proposal_evidence(db_path, proposal["id"]):
            entries.append(
                {
                    "proposal_id": proposal["id"],
                    "proposal_title": proposal.get("title") or "",
                    "seq": row["seq"],
                    "kind": row["kind"],
                    "source": row["source"],
                    "summary": row.get("summary"),
                    "is_blocker": row.get("is_blocker", False),
                    "data": row.get("data"),
                    "observed_at": row["observed_at"],
                }
            )
    entries.sort(key=lambda e: e["observed_at"])
    return entries


# --------------------------------------------------------------------------
# The package
# --------------------------------------------------------------------------


def _content_hash(package: dict) -> str:
    """SHA-256 over the package's canonical JSON (sorted keys, stable
    separators) — recomputable by a holder of the document to detect any
    change made after generation."""
    canonical = json.dumps(package, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_proof_package(db_path: Path, *, project: str) -> dict:
    """Assemble the four-pillar proof package for ``project``: Digital Memory,
    Counterfactual, Decision P&L and Audit Vault, stamped with an
    ``integrity_hash`` over the rest of the document.

    Pure aggregation over existing tables — no sensitivity policy is applied
    here; a caller that must honour BANK/LEGAL redaction (the service tier)
    checks ``is_sensitive(project)`` before calling this."""
    package = {
        "project": project,
        "generated_at": db.iso_now(),
        "digital_memory": build_digital_memory(db_path, project=project),
        "counterfactual": build_counterfactual(db_path, project=project),
        "decision_pnl": build_decision_pnl(db_path, project=project),
        "audit_vault": build_audit_vault(db_path, project=project),
    }
    package["integrity_hash"] = _content_hash(package)
    return package
