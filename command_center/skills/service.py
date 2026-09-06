"""Service tier for Skill Acquisition (routes → **service** → repository →
db), modelled on ``command_center.marketplace.service``.

This module is the only place that:

* resolves and lazily migrates the runtime db;
* maps stored rows onto the ``api.models`` Skill* contracts;
* **owns the acquisition policy**: a capability need is formed into a
  :class:`~command_center.skills.finder.CapabilityRequest`
  (:func:`request_capability`), candidates are discovered only through
  ``approved`` sources (:func:`find_candidates`), the winner is chosen
  strictly by recorded evidence (:func:`select_and_register`, delegating the
  actual scoring to :mod:`command_center.skills.selection`), acquisition
  materialises through an injected, isolated
  :class:`~command_center.skills.executor.SkillExecutor` (never executing
  anything itself — see :func:`acquire_skill`), and effect is measured
  against real ``skill_outcome`` evidence so a skill that never earns its
  place is retired (:func:`sweep_retire_underperforming`).

Every mutating call here is idempotent where the repository already makes it
so (a repeat ``acquire``/``reject``/``revoke`` of a skill already in that
state is a no-op, exactly like marketplace's repeat-install policy) and every
lifecycle transition is attributed to an ``actor`` and audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from command_center.api import models
from command_center.api import skills_schemas as s
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION
from command_center.skills import selection
from command_center.skills.executor import NullSkillExecutor, SkillExecutor
from command_center.skills.finder import (
    CandidateFinder,
    CandidateProposal,
    CapabilityRequest,
    NullCandidateFinder,
)

ROOT = Path(__file__).resolve().parents[2]

#: A skill is only retired by the effect sweep once both phases have at
#: least this many samples — too small a sample never drives a revocation.
DEFAULT_MIN_EFFECT_SAMPLES = 5


class SkillSourceNotFoundError(Exception):
    """Raised when an operation names a source that does not exist."""


class SkillNotFoundError(Exception):
    """Raised when an operation names a skill that does not exist."""


def _db_path() -> Path:
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


def _source_from_row(row: dict) -> models.SkillSource:
    return models.SkillSource(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        origin=row["origin"],
        status=row["status"],
        proposed_by=row.get("proposed_by") or "",
        approved_by=row.get("approved_by") or "",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _item_from_row(row: dict) -> models.SkillItem:
    return models.SkillItem(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        version=row.get("version") or "",
        content_hash=row.get("content_hash") or "",
        source_id=row.get("source_id") or "",
        provenance=row.get("provenance") or "",
        task_class=row.get("task_class") or "",
        status=row["status"],
        selection_rationale=row.get("selection_rationale") or {},
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _log_entry_from_row(row: dict) -> models.SkillAcquisitionLogEntry:
    return models.SkillAcquisitionLogEntry(
        id=row["id"],
        skill_id=row["skill_id"],
        actor=row["actor"],
        action=row["action"],
        version=row.get("version") or "",
        content_hash=row.get("content_hash") or "",
        executor=row.get("executor") or "",
        detail=row.get("detail") or "",
        metadata={str(k): str(v) for k, v in (row.get("metadata") or {}).items()},
        created_at=row.get("created_at"),
    )


def _outcome_from_row(row: dict) -> models.SkillOutcome:
    return models.SkillOutcome(
        id=row["id"],
        skill_id=row["skill_id"],
        task_id=row["task_id"],
        phase=row["phase"],
        cost=row["cost"],
        accepted=row["accepted"],
        first_pass=row["first_pass"],
        created_at=row.get("created_at"),
    )


# --------------------------------------------------------------------------
# skill_source — the allowlist (propose / approve / revoke / list / get)
# --------------------------------------------------------------------------


def propose_source(payload: s.SkillSourceCreate) -> models.SkillSource:
    row = db.create_skill_source(
        _db_path(),
        name=payload.name,
        kind=payload.kind,
        origin=payload.origin,
        proposed_by=payload.proposed_by,
    )
    return _source_from_row(row)


def get_source(source_id: str) -> models.SkillSource | None:
    row = db.get_skill_source(_db_path(), source_id)
    return _source_from_row(row) if row is not None else None


def list_sources(
    *, kind: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0
) -> s.SkillSourceList:
    rows = db.list_skill_sources(_db_path(), kind=kind, status=status, limit=limit, offset=offset)
    return s.SkillSourceList(items=[_source_from_row(r) for r in rows], limit=limit, offset=offset)


def approve_source(source_id: str, *, actor: str) -> models.SkillSource:
    """The human gate on a new origin. Idempotent: approving an already
    ``approved`` source changes nothing and re-approves nobody."""
    path = _db_path()
    row = db.get_skill_source(path, source_id)
    if row is None:
        raise SkillSourceNotFoundError(f"skill source {source_id!r} not found")
    if row["status"] == "approved":
        return _source_from_row(row)
    updated = db.set_skill_source_status(
        path, source_id, expected_version=row["lock_version"], new_status="approved", actor=actor,
    )
    return _source_from_row(updated)


def revoke_source(source_id: str, *, actor: str, reason: str = "") -> models.SkillSource:
    """Idempotent: revoking an already ``revoked`` source changes nothing."""
    path = _db_path()
    row = db.get_skill_source(path, source_id)
    if row is None:
        raise SkillSourceNotFoundError(f"skill source {source_id!r} not found")
    if row["status"] == "revoked":
        return _source_from_row(row)
    updated = db.set_skill_source_status(
        path, source_id, expected_version=row["lock_version"], new_status="revoked", actor=actor,
    )
    return _source_from_row(updated)


# --------------------------------------------------------------------------
# Capability request + candidate discovery (acceptance criterion 1)
# --------------------------------------------------------------------------


def request_capability(
    *, task_id: str, task_class: str, need: str, requested_by: str
) -> CapabilityRequest:
    if not (task_id or "").strip():
        raise ValueError("capability request task_id must be non-empty")
    if not (need or "").strip():
        raise ValueError("capability request need must be non-empty")
    return CapabilityRequest(
        task_id=task_id,
        task_class=task_class or "",
        need=need,
        requested_by=requested_by or "",
        requested_at=db.iso_now(),
    )


def find_candidates(
    request: CapabilityRequest, *, finder: CandidateFinder | None = None
) -> list[CandidateProposal]:
    """Discover candidates for ``request``, handing the finder only
    ``approved`` sources — the allowlist gate. With no finder configured, no
    network access is ever attempted (see
    ``skills.finder.NullCandidateFinder``)."""
    used = finder or NullCandidateFinder()
    approved_sources = db.list_skill_sources(_db_path(), status="approved", limit=500)
    return list(used.find(request, approved_sources))


# --------------------------------------------------------------------------
# Selection + registration (acceptance criterion 2)
# --------------------------------------------------------------------------


def select_and_register(
    request: CapabilityRequest, candidates: list[CandidateProposal]
) -> tuple[models.SkillItem | None, dict]:
    """Score ``candidates`` measurably (never by name) and, if one wins,
    register it as a new ``candidate``-status skill row carrying the full
    rationale. Returns ``(None, rationale)`` when no candidate carried
    evidence -- a refusal, not a fallback pick."""
    result = selection.select_candidate(candidates)
    if result.winner is None:
        return None, result.rationale
    row = db.create_skill_candidate(
        _db_path(),
        name=result.winner.name,
        kind=result.winner.kind,
        version=result.winner.version,
        content_hash=result.winner.content_hash,
        source_id=result.winner.source_id,
        provenance=result.winner.provenance,
        task_class=request.task_class,
        selection_rationale=result.rationale,
    )
    return _item_from_row(row), result.rationale


def register_candidate(payload: s.SkillItemCreate) -> models.SkillItem:
    """Directly register a skill candidate (no discovery/selection step) --
    e.g. a human-curated addition, or a test fixture."""
    row = db.create_skill_candidate(
        _db_path(),
        name=payload.name,
        kind=payload.kind,
        version=payload.version,
        content_hash=payload.content_hash,
        source_id=payload.source_id,
        provenance=payload.provenance,
        task_class=payload.task_class,
    )
    return _item_from_row(row)


def get_item(item_id: str) -> models.SkillItem | None:
    row = db.get_skill_item(_db_path(), item_id)
    return _item_from_row(row) if row is not None else None


def list_items(
    *,
    kind: str | None = None,
    status: str | None = None,
    task_class: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> s.SkillItemList:
    rows = db.list_skill_items(
        _db_path(), kind=kind, status=status, task_class=task_class, limit=limit, offset=offset,
    )
    return s.SkillItemList(items=[_item_from_row(r) for r in rows], limit=limit, offset=offset)


# --------------------------------------------------------------------------
# Acquire / reject / revoke — the audited lifecycle (acceptance criteria
# 3 and 4: pinned + provenanced + revocable, isolated execution)
# --------------------------------------------------------------------------


def acquire_skill(
    item_id: str, *, actor: str, executor: SkillExecutor | None = None
) -> models.SkillItem:
    """Materialise a candidate through an isolated, injected executor
    (default: no network/secrets/push — see
    ``skills.executor.NullSkillExecutor``) and flip it ``candidate →
    acquired`` with an audited trail line. Idempotent: acquiring an already
    ``acquired`` skill is a no-op — the executor is not invoked again and no
    duplicate log line is written."""
    path = _db_path()
    row = db.get_skill_item(path, item_id)
    if row is None:
        raise SkillNotFoundError(f"skill {item_id!r} not found")
    if row["status"] == "acquired":
        return _item_from_row(row)

    used_executor: SkillExecutor = executor or NullSkillExecutor()
    item = _item_from_row(row)
    outcome = used_executor.acquire(item)
    item_row, _log_row = db.acquire_skill_item(
        path,
        item_id,
        expected_version=row["lock_version"],
        actor=actor,
        executor=getattr(used_executor, "name", used_executor.__class__.__name__),
        detail=outcome.detail,
        metadata=dict(outcome.metadata),
    )
    return _item_from_row(item_row)


def reject_candidate(item_id: str, *, actor: str, reason: str = "") -> models.SkillItem:
    """Idempotent: rejecting an already ``rejected`` skill is a no-op."""
    path = _db_path()
    row = db.get_skill_item(path, item_id)
    if row is None:
        raise SkillNotFoundError(f"skill {item_id!r} not found")
    if row["status"] == "rejected":
        return _item_from_row(row)
    item_row, _log_row = db.reject_skill_item(
        path, item_id, expected_version=row["lock_version"], actor=actor, detail=reason,
    )
    return _item_from_row(item_row)


def revoke_skill(item_id: str, *, actor: str, reason: str = "") -> models.SkillItem:
    """Idempotent: revoking an already ``revoked`` skill is a no-op. This is
    the only way a live skill leaves the registry -- explicit revocation,
    never deletion, and always audited (who, when, why)."""
    path = _db_path()
    row = db.get_skill_item(path, item_id)
    if row is None:
        raise SkillNotFoundError(f"skill {item_id!r} not found")
    if row["status"] == "revoked":
        return _item_from_row(row)
    item_row, _log_row = db.revoke_skill_item(
        path, item_id, expected_version=row["lock_version"], actor=actor, detail=reason,
    )
    return _item_from_row(item_row)


def get_acquisition_log(
    item_id: str, *, limit: int = 100, offset: int = 0
) -> s.SkillAcquisitionLog | None:
    path = _db_path()
    if db.get_skill_item(path, item_id) is None:
        return None
    rows = db.list_skill_acquisition_log(path, item_id, limit=limit, offset=offset)
    return s.SkillAcquisitionLog(
        skill_id=item_id, entries=[_log_entry_from_row(r) for r in rows], limit=limit, offset=offset,
    )


# --------------------------------------------------------------------------
# Effect measurement (acceptance criterion 5): cost per accepted change and
# first-pass acceptance rate, baseline vs with-skill -- and retirement of a
# skill that never earns its place.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectReport:
    skill_id: str
    baseline_samples: int
    with_skill_samples: int
    baseline_cost_per_accepted: float | None
    with_skill_cost_per_accepted: float | None
    baseline_first_pass_rate: float | None
    with_skill_first_pass_rate: float | None
    #: ``None`` until both phases reach the minimum sample count; ``True``
    #: only when with-skill is measurably better on *both* metrics (never
    #: worse on either) than baseline.
    improved: bool | None


def record_outcome(
    item_id: str, *, task_id: str, phase: str, cost: float, accepted: bool, first_pass: bool
) -> models.SkillOutcome:
    row = db.record_skill_outcome(
        _db_path(), skill_id=item_id, task_id=task_id, phase=phase, cost=cost,
        accepted=accepted, first_pass=first_pass,
    )
    return _outcome_from_row(row)


def _cost_per_accepted(rows: list[dict]) -> float | None:
    accepted_rows = [r for r in rows if r["accepted"]]
    if not accepted_rows:
        return None
    return sum(r["cost"] for r in rows) / len(accepted_rows)


def _first_pass_rate(rows: list[dict]) -> float | None:
    if not rows:
        return None
    return sum(1 for r in rows if r["first_pass"]) / len(rows)


def evaluate_effect(
    item_id: str, *, min_samples: int = DEFAULT_MIN_EFFECT_SAMPLES
) -> EffectReport:
    outcomes = db.list_skill_outcomes(_db_path(), item_id, limit=10_000)
    baseline = [o for o in outcomes if o["phase"] == "baseline"]
    with_skill = [o for o in outcomes if o["phase"] == "with_skill"]

    baseline_cost = _cost_per_accepted(baseline)
    with_cost = _cost_per_accepted(with_skill)
    baseline_fp = _first_pass_rate(baseline)
    with_fp = _first_pass_rate(with_skill)

    improved: bool | None = None
    if (
        len(baseline) >= min_samples
        and len(with_skill) >= min_samples
        and baseline_cost is not None
        and with_cost is not None
        and baseline_fp is not None
        and with_fp is not None
    ):
        no_worse = with_cost <= baseline_cost and with_fp >= baseline_fp
        strictly_better = with_cost < baseline_cost or with_fp > baseline_fp
        improved = no_worse and strictly_better

    return EffectReport(
        skill_id=item_id,
        baseline_samples=len(baseline),
        with_skill_samples=len(with_skill),
        baseline_cost_per_accepted=baseline_cost,
        with_skill_cost_per_accepted=with_cost,
        baseline_first_pass_rate=baseline_fp,
        with_skill_first_pass_rate=with_fp,
        improved=improved,
    )


def sweep_retire_underperforming(
    *, actor: str = "effect-sweep", min_samples: int = DEFAULT_MIN_EFFECT_SAMPLES
) -> list[str]:
    """Revoke every ``acquired`` skill whose :func:`evaluate_effect` comes
    back ``improved is False`` (enough samples exist, and with-skill did not
    measurably beat baseline). A skill with too little evidence
    (``improved is None``) is left alone -- absence of proof is not proof of
    absence, and revocation always needs a real comparison behind it."""
    path = _db_path()
    retired: list[str] = []
    for row in db.list_skill_items(path, status="acquired", limit=1000):
        report = evaluate_effect(row["id"], min_samples=min_samples)
        if report.improved is False:
            reason = (
                "no measurable improvement: cost/accepted "
                f"{report.baseline_cost_per_accepted:.4g} -> "
                f"{report.with_skill_cost_per_accepted:.4g}, first-pass "
                f"{report.baseline_first_pass_rate:.2%} -> "
                f"{report.with_skill_first_pass_rate:.2%}"
            )
            revoke_skill(row["id"], actor=actor, reason=reason)
            retired.append(row["id"])
    return retired
