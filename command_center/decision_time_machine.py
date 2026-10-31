"""Executive Time-Machine (VOYN-MIN-EXEC): for every critical decision or
incident, one executable record — hypothesis, alternatives considered, the
decision made, and its actual effect measured at 1/7/30/90 days out.

A `decision package` is opened once, at the moment of decision, and is
re-visited on a fixed cadence (`EFFECT_HORIZONS_DAYS`): all four horizons
open as pending checkpoints the instant the package is created, so "did this
work?" is never a question someone has to remember to ask —
`due_checkpoints` surfaces it automatically once a due date passes.

Two acceptance-driving reads sit on top of the raw record:

- `build_post_mortem` assembles the hypothesis/alternatives/decision
  alongside the recorded effect timeline and a computed verdict, so a
  critical event always has a post-mortem-ready package rather than one
  assembled from memory after the fact.
- `find_similar_packages` matches a new critical event's framing against the
  packages already on file (the same dependency-free word-overlap technique
  `command_center.hero_playbooks` uses for "similar context") so a past
  decision's actual outcome is resurfaced before the same hypothesis is
  re-argued from scratch — the "application in similar scenarios"
  acceptance.

Storage follows the project's plain read-modify-write JSON convention
(`command_center.storage`), guarded end-to-end by `storage.file_lock` — the
same discipline `portfolio_launch`'s registry uses — rather than the sqlite
`runtime.db` engine: this module owns no other table family and has no
cross-process compare-and-set requirement beyond one whole-document lock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from command_center import storage

STORE_FILE_NAME = "decision_packages.json"
LOCK_FILE_NAME = "decision_packages.lock"

#: The fixed check-in cadence every decision package is scored against —
#: "1/7/30/90 days" is the acceptance's exact wording, not a configurable knob.
EFFECT_HORIZONS_DAYS: tuple[int, ...] = (1, 7, 30, 90)

#: Severities that make an event "critical" for this surface — mirrors the
#: `sev1` most-urgent rung of the `Conflict`/`Incident` severity ladder
#: (`api/models.py`) plus the plain `"critical"` value the audit engine's
#: `AuditSeverity` uses, so either vocabulary a caller hands in is recognized.
CRITICAL_SEVERITIES: frozenset[str] = frozenset({"sev1", "critical"})

CHECKPOINT_PENDING = "pending"
CHECKPOINT_RECORDED = "recorded"

#: A package with every checkpoint recorded is "complete"; otherwise "open".
PACKAGE_OPEN = "open"
PACKAGE_COMPLETE = "complete"

#: The only outcomes a recorded checkpoint may carry — a closed, explicit
#: judgement call from whoever records it, rather than something inferred
#: from free-text `effect_summary`, which would be guesswork.
OUTCOMES: frozenset[str] = frozenset(
    {"as_expected", "better_than_expected", "worse_than_expected", "mixed", "inconclusive"}
)
_VALIDATING_OUTCOMES: frozenset[str] = frozenset({"as_expected", "better_than_expected"})
_INVALIDATING_OUTCOMES: frozenset[str] = frozenset({"worse_than_expected"})

_STOPWORDS = {
    "и", "в", "во", "не", "на", "с", "со", "к", "ко", "по", "для", "от", "до",
    "the", "a", "an", "to", "of", "for", "in", "on", "and", "or",
}


class DecisionPackageNotFoundError(KeyError):
    """Raised when a package id has no matching stored record."""


class CheckpointNotFoundError(KeyError):
    """Raised when a package has no checkpoint at the requested horizon."""


def _store_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / STORE_FILE_NAME


def _lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / LOCK_FILE_NAME


def _lock(root: Path):
    return storage.file_lock(_lock_path(root))


def _load(root: Path) -> dict[str, dict]:
    return storage.read_json(_store_path(root), {})


def _save(root: Path, store: dict[str, dict]) -> None:
    storage.atomic_write_json(_store_path(root), store)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: str) -> datetime:
    """`datetime.fromisoformat`, normalized to timezone-aware (assumed UTC
    when the string carries no offset) so a naive caller-supplied timestamp
    can always be compared against `datetime.now(UTC)`."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def is_critical(severity: str | None) -> bool:
    """Whether `severity` names a critical event under this surface's
    vocabulary — see `CRITICAL_SEVERITIES`."""
    return (severity or "").lower() in CRITICAL_SEVERITIES


def _new_checkpoints(decided_at: str) -> list[dict]:
    decided = _parse_dt(decided_at)
    return [
        {
            "horizon_days": horizon,
            "due_at": (decided + timedelta(days=horizon)).isoformat(),
            "status": CHECKPOINT_PENDING,
            "outcome": None,
            "effect_summary": None,
            "metric_deltas": None,
            "recorded_at": None,
        }
        for horizon in EFFECT_HORIZONS_DAYS
    ]


def _normalize_alternative(alternative: str | dict) -> dict:
    if isinstance(alternative, str):
        return {"option": alternative, "why_not": None}
    return {"option": alternative.get("option", ""), "why_not": alternative.get("why_not")}


def _package_status(package: dict) -> str:
    if all(c["status"] == CHECKPOINT_RECORDED for c in package["checkpoints"]):
        return PACKAGE_COMPLETE
    return PACKAGE_OPEN


def create_decision_package(
    root: Path,
    *,
    event_ref: str,
    title: str,
    severity: str,
    hypothesis: str,
    alternatives: list[str | dict],
    decision: str,
    rationale: str = "",
    owner: str | None = None,
    project_ref: str | None = None,
    decided_at: str | None = None,
    package_id: str | None = None,
) -> dict:
    """Open one decision package for `event_ref`, with a pending checkpoint
    at each of `EFFECT_HORIZONS_DAYS` already scheduled.

    `alternatives` is the set of options considered but not chosen — each
    entry is either a plain string (the option's description) or a dict with
    an optional `"why_not"` explanation; both shapes normalize to
    `{"option": ..., "why_not": ...}` so `build_post_mortem` always renders a
    consistent shape.
    """
    if not event_ref:
        raise ValueError("decision package requires event_ref")
    if not hypothesis:
        raise ValueError("decision package requires a hypothesis")
    if not decision:
        raise ValueError("decision package requires a decision")

    now = _now_iso()
    decided_at = decided_at or now
    record: dict[str, Any] = {
        "id": package_id or str(uuid.uuid4()),
        "event_ref": event_ref,
        "title": title,
        "severity": severity,
        "hypothesis": hypothesis,
        "alternatives": [_normalize_alternative(a) for a in alternatives or []],
        "decision": decision,
        "rationale": rationale,
        "owner": owner,
        "project_ref": project_ref,
        "decided_at": decided_at,
        "created_at": now,
        "checkpoints": _new_checkpoints(decided_at),
    }

    with _lock(root):
        store = _load(root)
        store[record["id"]] = record
        _save(root, store)
    return record


def record_effect(
    root: Path,
    package_id: str,
    horizon_days: int,
    *,
    outcome: str,
    effect_summary: str,
    metric_deltas: dict[str, Any] | None = None,
    recorded_at: str | None = None,
) -> dict:
    """Fill in the actual effect observed at `horizon_days` out. Checkpoints
    may be recorded out of order (a 30-day check-in landing before the 7-day
    one was ever filled is still a valid observation)."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {outcome!r}")
    with _lock(root):
        store = _load(root)
        record = store.get(package_id)
        if record is None:
            raise DecisionPackageNotFoundError(package_id)
        checkpoint = next(
            (c for c in record["checkpoints"] if c["horizon_days"] == horizon_days), None
        )
        if checkpoint is None:
            raise CheckpointNotFoundError(f"{package_id!r} has no {horizon_days}-day checkpoint")
        checkpoint["status"] = CHECKPOINT_RECORDED
        checkpoint["outcome"] = outcome
        checkpoint["effect_summary"] = effect_summary
        checkpoint["metric_deltas"] = metric_deltas or {}
        checkpoint["recorded_at"] = recorded_at or _now_iso()
        _save(root, store)
        return record


def get_decision_package(root: Path, package_id: str) -> dict | None:
    return _load(root).get(package_id)


def list_decision_packages(
    root: Path,
    *,
    status: str | None = None,
    event_ref: str | None = None,
    project_ref: str | None = None,
) -> list[dict]:
    """List packages, newest decision first, optionally filtered by
    completion `status` (`"open"`/`"complete"`), `event_ref` or `project_ref`."""
    packages = list(_load(root).values())
    if event_ref is not None:
        packages = [p for p in packages if p["event_ref"] == event_ref]
    if project_ref is not None:
        packages = [p for p in packages if p.get("project_ref") == project_ref]
    if status is not None:
        packages = [p for p in packages if _package_status(p) == status]
    packages.sort(key=lambda p: p["decided_at"], reverse=True)
    return packages


def due_checkpoints(root: Path, *, as_of: str | None = None) -> list[dict]:
    """Every pending checkpoint whose due date has passed, across all
    packages, oldest-due first — the automatic "go measure this" queue so a
    1/7/30/90-day check-in is never dependent on someone remembering it."""
    as_of_dt = _parse_dt(as_of) if as_of else datetime.now(UTC)
    due: list[dict] = []
    for package in _load(root).values():
        for checkpoint in package["checkpoints"]:
            if checkpoint["status"] != CHECKPOINT_PENDING:
                continue
            if _parse_dt(checkpoint["due_at"]) <= as_of_dt:
                due.append(
                    {
                        "package_id": package["id"],
                        "title": package["title"],
                        "event_ref": package["event_ref"],
                        "horizon_days": checkpoint["horizon_days"],
                        "due_at": checkpoint["due_at"],
                    }
                )
    due.sort(key=lambda d: d["due_at"])
    return due


def build_post_mortem(package: dict) -> dict:
    """Assemble the post-mortem-ready view of one decision package: the
    hypothesis and alternatives as they stood at decision time, the decision
    itself, the recorded effect timeline in horizon order, and a verdict
    computed from however many checkpoints have been recorded so far.

    The verdict is `"pending_data"` until at least one checkpoint is
    recorded — a critical event has a *package* immediately, even though its
    outcome is necessarily unknown at t=0. Once checkpoints start coming in,
    it becomes `"validated"` when validating outcomes strictly outnumber
    invalidating ones, `"invalidated"` the other way round, and `"mixed"` on
    a tie (including when every recorded outcome is itself neutral —
    `"mixed"`/`"inconclusive"`).
    """
    recorded = sorted(
        (c for c in package["checkpoints"] if c["status"] == CHECKPOINT_RECORDED),
        key=lambda c: c["horizon_days"],
    )

    if not recorded:
        verdict = "pending_data"
    else:
        validating = sum(1 for c in recorded if c["outcome"] in _VALIDATING_OUTCOMES)
        invalidating = sum(1 for c in recorded if c["outcome"] in _INVALIDATING_OUTCOMES)
        if validating > invalidating:
            verdict = "validated"
        elif invalidating > validating:
            verdict = "invalidated"
        else:
            verdict = "mixed"

    return {
        "package_id": package["id"],
        "event_ref": package["event_ref"],
        "title": package["title"],
        "severity": package["severity"],
        "hypothesis": package["hypothesis"],
        "alternatives": package["alternatives"],
        "decision": package["decision"],
        "rationale": package["rationale"],
        "owner": package["owner"],
        "decided_at": package["decided_at"],
        "effect_timeline": recorded,
        "outstanding_checkpoints": [
            c["horizon_days"] for c in package["checkpoints"] if c["status"] == CHECKPOINT_PENDING
        ],
        "verdict": verdict,
    }


def _tokenize(text: str) -> set[str]:
    words = "".join(ch.lower() if ch.isalnum() else " " for ch in text or "").split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _package_text(package: dict) -> str:
    return f"{package.get('title', '')} {package.get('hypothesis', '')} {package.get('decision', '')}"


def find_similar_packages(
    root: Path,
    *,
    title: str = "",
    hypothesis: str = "",
    decision: str = "",
    project_ref: str | None = None,
    exclude_package_id: str | None = None,
    limit: int = 3,
    min_similarity: float = 0.2,
) -> list[dict]:
    """Match a new critical event's framing against packages already on
    file, so a past hypothesis/decision and its *actual* recorded effect
    resurface before the same ground is re-argued from scratch (the
    "application in similar scenarios" acceptance).

    Ranks by word overlap over title+hypothesis+decision — the same
    dependency-free Jaccard technique `hero_playbooks.suggest_hero_playbook`
    uses for "similar context" — with a same-`project_ref` package ranked
    ahead of an equally-similar package from elsewhere. Only packages with at
    least one recorded checkpoint are considered: an untested hypothesis has
    no proven effect yet to apply.
    """
    query_tokens = _tokenize(f"{title} {hypothesis} {decision}")
    if not query_tokens:
        return []

    scored: list[tuple[bool, float, dict]] = []
    for package in _load(root).values():
        if package["id"] == exclude_package_id:
            continue
        if not any(c["status"] == CHECKPOINT_RECORDED for c in package["checkpoints"]):
            continue
        candidate_tokens = _tokenize(_package_text(package))
        if not candidate_tokens:
            continue
        similarity = len(query_tokens & candidate_tokens) / len(query_tokens | candidate_tokens)
        if similarity < min_similarity:
            continue
        same_project = project_ref is not None and package.get("project_ref") == project_ref
        scored.append((same_project, similarity, package))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [
        {"package": package, "similarity": similarity, "post_mortem": build_post_mortem(package)}
        for same_project, similarity, package in scored[:limit]
    ]


def missing_decision_packages(root: Path, critical_events: list[dict]) -> list[dict]:
    """Given candidate events (each at least `{"ref": ..., "severity": ...}`,
    plus optional `"title"`/`"project_ref"`), return the ones that are
    critical (`is_critical`) and have no decision package on file yet — the
    coverage check behind the acceptance: *any* critical event must have a
    decision package."""
    covered_refs = {p["event_ref"] for p in _load(root).values()}
    return [
        event
        for event in critical_events
        if is_critical(event.get("severity")) and event.get("ref") not in covered_refs
    ]
