"""Application entry the controller calls: run a pass, return the API response.

The HTTP route stays a one-liner — it calls :func:`run_pass`, which builds a
default :class:`~command_center.advisor.service.AdvisorService`, executes one
collection pass and maps the internal :class:`RunSummary` onto the wire
:class:`~command_center.advisor.schemas.AdvisorRunResponse`. Keeping the mapping
here (not in the route) means the same one call is reusable from a scheduler or a
CLI later without going through FastAPI.
"""

from __future__ import annotations

from command_center.advisor.schemas import (
    AdvisorProposalOutcome,
    AdvisorRunResponse,
)
from command_center.advisor.service import AdvisorService, RunSummary


def _to_response(summary: RunSummary) -> AdvisorRunResponse:
    return AdvisorRunResponse(
        collectors=summary.collectors,
        candidates=summary.candidates,
        created=summary.created,
        promoted=summary.promoted,
        deduped=summary.deduped,
        skipped_sensitive=summary.skipped_sensitive,
        by_kind=summary.by_kind,
        proposals=[
            AdvisorProposalOutcome(
                proposal_id=o.proposal_id,
                kind=o.kind,
                title=o.title,
                project_ref=o.project_ref,
                action=o.action,
                priority=o.priority,
                promoted_task_id=o.promoted_task_id,
            )
            for o in summary.proposals
        ],
    )


def run_pass(
    *,
    collectors: list[str] | None = None,
    project: str | None = None,
    service: AdvisorService | None = None,
) -> AdvisorRunResponse:
    """Run one collection pass with the default engine (or an injected
    ``service``) and return the API response model."""
    engine = service or AdvisorService()
    summary = engine.run(collectors=collectors, project=project)
    return _to_response(summary)
