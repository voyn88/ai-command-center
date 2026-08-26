"""AdvisorService — orchestrates a full collection pass.

Pipeline for one pass::

    collectors -> candidates -> drop sensitive -> dedup -> score
               -> apply auto-rules -> persist (+ ProposalCreated)
               -> optional auto-promote (+ ProposalPromotedToTask)

Layering: this service persists **only** through the Wave-1 write service
(``wave1_service.create_proposal`` / ``promote_proposal``), which are the single
seams that touch the ``advisor_proposal`` repository, the tasks single-writer and
the event bus. The advisor engine therefore never opens the db or the tasks store
itself — it composes existing, audited write paths. Both seams are module-level
names so a test can substitute in-memory fakes and never hit sqlite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from command_center.advisor.config import DEFAULT_CONFIG, AdvisorConfig
from command_center.advisor.registry import CollectorRegistry, default_registry
from command_center.advisor.scorer import ProposalScorer
from command_center.advisor.types import Candidate, CollectorContext
from command_center.api import wave1_service
from command_center.api.wave1_schemas import ProposalCreate
from command_center.project_config import is_sensitive
from command_center.runtime import db

# Repo root: <root>/command_center/advisor/service.py
ROOT = Path(__file__).resolve().parents[2]


# Persistence seams (module-level so tests can monkeypatch). Both delegate to the
# Wave-1 write service, the documented single writer of the advisor surface.
def _default_persist(payload: ProposalCreate):
    return wave1_service.create_proposal(payload)


def _default_promote(proposal_id: str):
    return wave1_service.promote_proposal(proposal_id)


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What happened to one surviving candidate."""

    proposal_id: str
    kind: str
    title: str
    project_ref: str
    action: str  # "draft" | "promote"
    priority: float
    promoted_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Aggregate result of a collection pass."""

    collectors: list[str]
    candidates: int = 0
    created: int = 0
    promoted: int = 0
    deduped: int = 0
    skipped_sensitive: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    proposals: list[ProposalOutcome] = field(default_factory=list)


class AdvisorService:
    """Runs collection passes over a :class:`CollectorRegistry`."""

    def __init__(
        self,
        *,
        registry: CollectorRegistry | None = None,
        scorer: ProposalScorer | None = None,
        config: AdvisorConfig | None = None,
        root: Path = ROOT,
        persist_fn=_default_persist,
        promote_fn=_default_promote,
    ) -> None:
        self._registry = registry or default_registry()
        self._scorer = scorer or ProposalScorer()
        self._config = config or DEFAULT_CONFIG
        self._root = root
        self._persist_fn = persist_fn
        self._promote_fn = promote_fn

    @property
    def registry(self) -> CollectorRegistry:
        return self._registry

    def _existing_signatures(self, db_path: Path) -> set[str]:
        """Signatures of the currently-open proposals, so a re-run over the same
        unchanged signal does not raise a duplicate. Reconstructs each stored
        proposal's signature with the same formula a fresh candidate uses."""
        rows = db.list_advisor_proposals(
            db_path, statuses=list(self._config.dedup_statuses), limit=10_000
        )
        return {
            Candidate(kind=r["kind"], title=r.get("title") or "", project_ref=r["project_ref"]).signature()
            for r in rows
        }

    def run(
        self, *, collectors: list[str] | None = None, project: str | None = None
    ) -> RunSummary:
        """Execute one collection pass and return a :class:`RunSummary`.

        ``collectors`` restricts the pass to a named subset (default: all
        registered). ``project`` restricts persistence to candidates for that
        project (collectors still read the whole history; the filter is applied
        to their output)."""
        db_path = wave1_service._db_path()
        collector_objs = self._registry.create(collectors)
        ctx = CollectorContext(root=self._root, db_path=db_path)

        # 1. Collect.
        raw: list[Candidate] = []
        for collector in collector_objs:
            raw.extend(collector.collect(ctx))

        summary_names = [c.name for c in collector_objs]
        candidates_seen = len(raw)
        created = promoted = deduped = skipped_sensitive = 0
        by_kind: dict[str, int] = {}
        outcomes: list[ProposalOutcome] = []

        existing = self._existing_signatures(db_path)
        seen_this_pass: set[str] = set()

        for candidate in raw:
            if project is not None and candidate.project_ref != project:
                continue
            # Privacy: a sensitive project's proposal would be redacted on read
            # anyway; drop it before it is ever written so its title never lands.
            if is_sensitive(candidate.project_ref):
                skipped_sensitive += 1
                continue
            signature = candidate.signature()
            if signature in existing or signature in seen_this_pass:
                deduped += 1
                continue
            seen_this_pass.add(signature)

            if len(outcomes) >= self._config.max_candidates_per_pass:
                # Bound a single pass so a runaway collector cannot flood the
                # inbox; the rest are simply left for the next run.
                break

            score = self._scorer.score(candidate)
            action = self._config.decide(candidate, score)
            proposal = self._persist_fn(
                ProposalCreate(
                    kind=candidate.kind,
                    title=candidate.title,
                    project_ref=candidate.project_ref,
                    body=candidate.body,
                    expected_gain=score.value_bucket,
                    effort=score.effort_bucket,
                )
            )
            created += 1
            by_kind[candidate.kind] = by_kind.get(candidate.kind, 0) + 1

            promoted_task_id: str | None = None
            if action == "promote":
                result = self._promote_fn(proposal.id)
                if result is not None:
                    promoted += 1
                    promoted_task_id = result.task_id

            outcomes.append(
                ProposalOutcome(
                    proposal_id=proposal.id,
                    kind=candidate.kind,
                    title=candidate.title,
                    project_ref=candidate.project_ref,
                    action=action,
                    priority=score.priority,
                    promoted_task_id=promoted_task_id,
                )
            )

        return RunSummary(
            collectors=summary_names,
            candidates=candidates_seen,
            created=created,
            promoted=promoted,
            deduped=deduped,
            skipped_sensitive=skipped_sensitive,
            by_kind=by_kind,
            proposals=outcomes,
        )
