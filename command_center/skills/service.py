"""Service tier for skill acquisition (routes -> **service** -> repository ->
db). See :mod:`command_center.runtime.db.skills` for the persistence tier this
sits on and the two structural invariants it enforces (atomic
approved-source check on candidate registration; claim-before-execute on
acquisition).

The routes in :mod:`command_center.api.skills_routes` hold no business logic;
each handler calls one function here and maps its ``None``/domain-error result
onto the right HTTP status. This module is the only place that:

* resolves and lazily migrates the runtime db;
* maps stored rows onto the :mod:`command_center.api.models` contract;
* drives skill acquisition through the **injected**
  :class:`~command_center.skills.executor.SkillExecutor` — claim (a guarded
  ``candidate -> acquiring`` compare-and-set) *before* the executor ever runs,
  so two concurrent callers can never both materialise the same skill, and a
  failed acquisition reverts the claim rather than leaving the item stuck;
* selects a candidate for a task class **measurably**: by its recorded
  cost/first-pass-acceptance track record where one exists, never merely "by
  name" (:func:`select_best_candidate`), and refuses rather than fabricating a
  choice when nothing qualifies;
* computes the with-skill-vs-baseline effect comparison
  (:func:`get_effect`) and retires a skill with no proven improvement
  (:func:`sweep_retire_underperforming`) — the measurable off-ramp the owner
  idea requires for a skill that never earns its keep.

Testability seam: the executor is a parameter (default
:class:`~command_center.skills.executor.NullSkillExecutor` — no network, no
code execution), and every backing call (repository functions,
``resolve_db_path``) is referenced through a module-level name so a test can
monkeypatch it, exactly as the model-registry and marketplace service tiers
do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

from command_center.api import models
from command_center.api import skills_schemas as s
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION
from command_center.skills.executor import NullSkillExecutor, SkillExecutor

# Repo root is three levels up: <root>/command_center/skills/service.py
ROOT = Path(__file__).resolve().parents[2]

#: A page size large enough that a real deployment's row count usually fits in
#: one page, but nothing here trusts that: see :func:`_paginate`.
_PAGE_SIZE = 500


def _paginate(fetch_page: Callable[[int, int], list[dict]], *, page_size: int = _PAGE_SIZE) -> Iterator[dict]:
    """Yield every row ``fetch_page(limit, offset)`` can return, walking pages
    until one comes back short. No caller-visible cap: a fixed ``limit=N``
    silently truncates past N rows with nothing to say so happened, which is
    exactly the failure mode this exists to rule out for both the retirement
    sweep and the approved-source allowlist lookup."""
    offset = 0
    while True:
        page = fetch_page(page_size, offset)
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        offset += page_size


class SkillSourceNotFoundError(Exception):
    """Raised when an operation names a ``skill_source`` that does not exist.
    Surfaced as HTTP 404."""


class SkillNotFoundError(Exception):
    """Raised when an operation names a ``skill_item`` that does not exist.
    Surfaced as HTTP 404."""


class NoSkillCandidateError(Exception):
    """Raised by :func:`select_best_candidate` when nothing qualifies for the
    requested task class. A refusal, never a fabricated fallback choice.
    Surfaced as HTTP 404."""


class SkillAcquisitionFailedError(Exception):
    """Raised when the injected executor's :meth:`acquire` call fails. The
    claim has already been reverted to ``candidate`` (retryable) before this
    is raised. Surfaced as HTTP 502 — the failure is in materialising the
    skill, not in the request itself."""


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags (the
    same lazy-migrate pattern the other Wave-3 services use)."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


# --------------------------------------------------------------------------
# Row -> contract-model mapping
# --------------------------------------------------------------------------


def _source_from_row(row: dict) -> models.SkillSource:
    return models.SkillSource(
        id=row["id"],
        kind=row["kind"],
        origin=row["origin"],
        status=row["status"],
        proposed_by=row.get("proposed_by") or "",
        lock_version=int(row["lock_version"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _item_from_row(row: dict) -> models.SkillItem:
    return models.SkillItem(
        id=row["id"],
        source_id=row["source_id"],
        name=row["name"],
        kind=row["kind"],
        version=row.get("version") or "",
        content_hash=row["content_hash"],
        task_class=row.get("task_class") or "",
        provenance=row.get("provenance") or "",
        status=row["status"],
        lock_version=int(row["lock_version"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _log_entry_from_row(row: dict) -> models.SkillAcquisitionLogEntry:
    return models.SkillAcquisitionLogEntry(
        id=int(row["id"]),
        skill_id=row["skill_id"],
        seq=int(row["seq"]),
        action=row["action"],
        actor=row.get("actor"),
        from_status=row.get("from_status") or "",
        to_status=row.get("to_status") or "",
        detail=row.get("detail") or "",
        metadata=row.get("metadata") or {},
        created_at=row.get("created_at"),
    )


def _outcome_from_row(row: dict) -> models.SkillOutcome:
    return models.SkillOutcome(
        id=int(row["id"]),
        skill_id=row["skill_id"],
        task_id=row["task_id"],
        used=bool(row["used"]),
        cost_usd=float(row["cost_usd"]),
        accepted=bool(row["accepted"]),
        latency_seconds=float(row["latency_seconds"]),
        detail=row.get("detail") or "",
        created_at=row.get("created_at"),
    )


def _stats_from_dict(raw: dict) -> models.SkillEffectStats:
    return models.SkillEffectStats(
        count=int(raw["count"]),
        avg_cost_usd=raw.get("avg_cost_usd"),
        first_pass_rate=raw.get("first_pass_rate"),
    )


def compute_improved(
    baseline: models.SkillEffectStats, with_skill: models.SkillEffectStats
) -> bool | None:
    """Whether ``with_skill`` is a measured improvement over ``baseline``.

    ``None`` when either side has no recorded outcomes — "no evidence yet" is
    never conflated with "measured, and not an improvement" (a brand-new
    acquisition must not read as a failed one). Otherwise ``True`` only when
    the with-skill side is strictly better on cost **and** strictly better on
    first-pass rate — never worse on either — matching the acceptance
    criterion that a skill without a *proven* improvement is retired: a skill
    that is merely a wash on one axis does not get the benefit of the doubt.
    """
    if baseline.count == 0 or with_skill.count == 0:
        return None
    if baseline.avg_cost_usd is None or with_skill.avg_cost_usd is None:
        return None
    if baseline.first_pass_rate is None or with_skill.first_pass_rate is None:
        return None
    return (
        with_skill.avg_cost_usd < baseline.avg_cost_usd
        and with_skill.first_pass_rate > baseline.first_pass_rate
    )


# --------------------------------------------------------------------------
# skill_source — propose / approve / revoke / get / list
# --------------------------------------------------------------------------


def propose_source(payload: s.ProposeSourceRequest) -> models.SkillSource:
    row = db.create_skill_source(
        _db_path(),
        kind=payload.kind,
        origin=payload.origin,
        proposed_by=payload.actor or "",
    )
    return _source_from_row(row)


def list_sources(
    *, kind: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0
) -> s.SkillSourceList:
    rows = db.list_skill_sources(_db_path(), kind=kind, status=status, limit=limit, offset=offset)
    return s.SkillSourceList(
        sources=[_source_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_source(source_id: str) -> models.SkillSource | None:
    row = db.get_skill_source(_db_path(), source_id)
    return _source_from_row(row) if row is not None else None


def approve_source(
    source_id: str, *, expected_version: int, actor: str
) -> models.SkillSource:
    """The human gate: flips a source ``proposed -> approved``. Raises
    :class:`SkillSourceNotFoundError` (404) if the source does not exist,
    ``ValueError``/``InvalidSkillSourceTransitionError`` (422) for any edge
    other than that one, and ``db.LostUpdateError`` (409) on a concurrent
    writer."""
    try:
        row = db.transition_skill_source(
            _db_path(), source_id, expected_version=expected_version,
            to_status="approved", actor=actor,
        )
    except KeyError as exc:
        raise SkillSourceNotFoundError(str(exc)) from exc
    return _source_from_row(row)


def revoke_source(source_id: str, *, expected_version: int, actor: str) -> models.SkillSource:
    """Terminal: flips a source to ``revoked`` from any non-terminal state. No
    candidate may be registered against a revoked source afterwards (enforced
    atomically in the repository, not by caller discipline)."""
    try:
        row = db.transition_skill_source(
            _db_path(), source_id, expected_version=expected_version,
            to_status="revoked", actor=actor,
        )
    except KeyError as exc:
        raise SkillSourceNotFoundError(str(exc)) from exc
    return _source_from_row(row)


# --------------------------------------------------------------------------
# skill_item — register / get / list
# --------------------------------------------------------------------------


def register_candidate(payload: s.RegisterSkillRequest) -> models.SkillItem:
    """Register a candidate skill against an approved source.

    Raises :class:`SkillSourceNotFoundError` (404) if ``source_id`` names no
    source, and ``ValueError`` (422) if the source exists but is not
    ``approved``, or if the candidate's own fields are invalid. The
    approved-source check and the insert are atomic in the repository — see
    the module docstring."""
    try:
        row = db.create_skill_candidate(
            _db_path(),
            source_id=payload.source_id,
            name=payload.name,
            kind=payload.kind,
            content_hash=payload.content_hash,
            version=payload.version or "",
            task_class=payload.task_class or "",
            provenance=payload.provenance or "",
        )
    except KeyError as exc:
        raise SkillSourceNotFoundError(str(exc)) from exc
    return _item_from_row(row)


def list_items(
    *, source_id: str | None = None, kind: str | None = None, status: str | None = None,
    task_class: str | None = None, limit: int = 100, offset: int = 0,
) -> s.SkillItemList:
    rows = db.list_skill_items(
        _db_path(), source_id=source_id, kind=kind, status=status,
        task_class=task_class, limit=limit, offset=offset,
    )
    return s.SkillItemList(items=[_item_from_row(r) for r in rows], limit=limit, offset=offset)


def get_item(item_id: str) -> models.SkillItem | None:
    row = db.get_skill_item(_db_path(), item_id)
    return _item_from_row(row) if row is not None else None


# --------------------------------------------------------------------------
# Discovery — find candidates from allowlisted sources, select measurably
# --------------------------------------------------------------------------


def find_candidates(
    task_class: str, *, kind: str | None = None, limit: int = 100
) -> list[models.SkillItem]:
    """Candidate skills registered for ``task_class``, scoped to sources that
    are *currently* ``approved`` — the allowlist the owner idea requires, read
    fresh on every call rather than trusted from whatever a candidate's source
    looked like at registration time (a source revoked since would otherwise
    still surface as a usable candidate)."""
    path = _db_path()
    approved_ids = {
        row["id"]
        for row in _paginate(
            lambda limit, offset: db.list_skill_sources(
                path, status="approved", limit=limit, offset=offset
            )
        )
    }
    candidates = db.list_skill_items(
        path, status="candidate", kind=kind, task_class=task_class, limit=limit,
    )
    return [_item_from_row(r) for r in candidates if r["source_id"] in approved_ids]


def _track_record(item_id: str) -> tuple[float, float]:
    """``(avg_cost_usd, -first_pass_rate)`` for the with-skill side of
    ``item_id``'s effect, defaulting to values that sort *after* anything with
    real evidence when there is none — an unproven candidate is eligible, but
    never preferred over one with a measured track record."""
    effect = db.get_skill_effect(_db_path(), item_id)
    if effect is None:
        return (float("inf"), 0.0)
    with_skill = effect["with_skill"]
    if with_skill["count"] == 0 or with_skill["avg_cost_usd"] is None:
        return (float("inf"), 0.0)
    return (with_skill["avg_cost_usd"], -with_skill["first_pass_rate"])


def select_best_candidate(
    task_class: str, *, kind: str | None = None
) -> models.SkillItem:
    """Pick the best candidate for ``task_class``, ranked by its measured
    track record (lower cost per task, then higher first-pass rate; an
    unproven candidate ranks after any with real evidence, then by
    ``created_at`` so the choice is deterministic) — never "by name". Raises
    :class:`NoSkillCandidateError` if nothing qualifies: a refusal, not a
    fabricated fallback."""
    candidates = find_candidates(task_class, kind=kind, limit=500)
    if not candidates:
        raise NoSkillCandidateError(
            f"no candidate skill for task_class={task_class!r}"
            + (f" kind={kind!r}" if kind else "")
        )
    ranked = sorted(
        candidates,
        key=lambda item: (*_track_record(item.id), item.created_at or "", item.id),
    )
    return ranked[0]


# --------------------------------------------------------------------------
# Acquisition — claim (CAS) before the executor ever runs
# --------------------------------------------------------------------------


def acquire_skill(
    item_id: str, *, expected_version: int, actor: str, executor: SkillExecutor | None = None,
) -> models.SkillItem:
    """Claim ``item_id`` and, only once the claim durably commits, invoke the
    injected executor to materialise it.

    The claim (``candidate -> acquiring``, a compare-and-set) happens first
    and on its own transaction; the executor never runs unless this caller won
    it. On executor success the item moves to ``acquired``; on executor
    failure the claim is reverted to ``candidate`` (retryable) and
    :class:`SkillAcquisitionFailedError` is raised. Raises
    :class:`SkillNotFoundError` (404) if the item does not exist, and
    ``ValueError``/``InvalidSkillItemTransitionError`` (422) if it is not
    ``candidate``, or ``db.LostUpdateError`` (409) on a concurrent writer —
    all of that *before* the executor is ever called.
    """
    path = _db_path()
    try:
        claimed = db.claim_skill_item(path, item_id, expected_version=expected_version, actor=actor)
    except KeyError as exc:
        raise SkillNotFoundError(str(exc)) from exc

    used_executor: SkillExecutor = executor or NullSkillExecutor()
    item = _item_from_row(claimed)
    try:
        outcome = used_executor.acquire(item)
    except Exception as exc:
        db.fail_skill_item_acquisition(
            path, item_id, expected_version=claimed["lock_version"], actor=actor,
            detail=str(exc),
        )
        raise SkillAcquisitionFailedError(
            f"skill {item_id!r} acquisition failed: {exc}"
        ) from exc

    acquired = db.finalize_skill_item_acquisition(
        path, item_id, expected_version=claimed["lock_version"], actor=actor,
        detail=outcome.detail,
        metadata={**outcome.metadata, "executor": used_executor.name},
    )
    return _item_from_row(acquired)


def reject_item(item_id: str, *, expected_version: int, actor: str, detail: str = "") -> models.SkillItem:
    try:
        row = db.reject_skill_item(
            _db_path(), item_id, expected_version=expected_version, actor=actor, detail=detail,
        )
    except KeyError as exc:
        raise SkillNotFoundError(str(exc)) from exc
    return _item_from_row(row)


def revoke_item(item_id: str, *, expected_version: int, actor: str, detail: str = "") -> models.SkillItem:
    try:
        row = db.revoke_skill_item(
            _db_path(), item_id, expected_version=expected_version, actor=actor, detail=detail,
        )
    except KeyError as exc:
        raise SkillNotFoundError(str(exc)) from exc
    return _item_from_row(row)


def get_acquisition_log(item_id: str, *, limit: int = 100, offset: int = 0) -> s.SkillAcquisitionLog | None:
    path = _db_path()
    if db.get_skill_item(path, item_id) is None:
        return None
    rows = db.list_skill_acquisition_log(path, item_id, limit=limit, offset=offset)
    return s.SkillAcquisitionLog(
        skill_id=item_id, entries=[_log_entry_from_row(r) for r in rows],
        limit=limit, offset=offset,
    )


# --------------------------------------------------------------------------
# Outcomes + effect measurement
# --------------------------------------------------------------------------


def record_outcome(item_id: str, payload: s.RecordOutcomeRequest) -> models.SkillOutcome | None:
    """Append one outcome row. Returns ``None`` if ``item_id`` does not exist
    (mapped to 404 by the route) — the existence check and the insert are
    atomic in the repository, so there is no gap a concurrent revoke could
    land in between the two."""
    row = db.record_skill_outcome(
        _db_path(), item_id, task_id=payload.task_id, used=payload.used,
        cost_usd=payload.cost_usd, accepted=payload.accepted,
        latency_seconds=payload.latency_seconds, detail=payload.detail or "",
    )
    return _outcome_from_row(row) if row is not None else None


def list_outcomes(item_id: str, *, limit: int = 100, offset: int = 0) -> s.SkillOutcomeList | None:
    path = _db_path()
    if db.get_skill_item(path, item_id) is None:
        return None
    rows = db.list_skill_outcomes(path, item_id, limit=limit, offset=offset)
    return s.SkillOutcomeList(
        skill_id=item_id, outcomes=[_outcome_from_row(r) for r in rows],
        limit=limit, offset=offset,
    )


def get_effect(item_id: str) -> models.SkillEffect | None:
    """The baseline-vs-with-skill effect comparison for one skill. Returns
    ``None`` if the skill does not exist (404)."""
    raw = db.get_skill_effect(_db_path(), item_id)
    if raw is None:
        return None
    baseline = _stats_from_dict(raw["baseline"])
    with_skill = _stats_from_dict(raw["with_skill"])
    return models.SkillEffect(
        skill_id=item_id, baseline=baseline, with_skill=with_skill,
        improved=compute_improved(baseline, with_skill),
    )


def sweep_retire_underperforming(*, actor: str, min_samples: int = 1) -> list[models.SkillItem]:
    """Revoke every ``acquired`` skill whose effect is measured and *not* an
    improvement, walking the full registry regardless of size (see
    :func:`_paginate`) rather than silently stopping at a fixed row cap.

    ``min_samples`` is the minimum outcome count required on *each* side
    (baseline and with-skill) before a skill is judged at all — a skill with
    no recorded outcomes yet (``improved is None``) is left alone rather than
    retired on no evidence. Returns the list of revoked items."""
    path = _db_path()
    revoked: list[models.SkillItem] = []
    acquired = _paginate(
        lambda limit, offset: db.list_skill_items(
            path, status="acquired", limit=limit, offset=offset
        )
    )
    for row in acquired:
        item_id = row["id"]
        effect = get_effect(item_id)
        if effect is None:
            continue
        if effect.baseline.count < min_samples or effect.with_skill.count < min_samples:
            continue
        if effect.improved:
            continue
        updated = revoke_item(
            item_id, expected_version=row["lock_version"], actor=actor,
            detail="retired: no measured improvement over baseline",
        )
        revoked.append(updated)
    return revoked
