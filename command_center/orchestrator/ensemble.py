"""Agent ensembles (VOYN-AGT-COUPON): named, control-typed routes over the
executor cascades `routing.py` already proves live on the fleet.

The pipeline already runs three distinct roles on every task, each with its
own tool profile enforced by `agent_runner` (a role is not a suggestion, it
is a capability grant):

* **writer** — `implementation`: `agent_runner.PROFILE_TRUSTED_DEVELOPMENT`
  (Bash, Edit, Write, git read + commit). The only role that may mutate the
  tree.
* **verifier** — `verification_review`: `agent_runner.PROFILE_READ_ONLY`
  (Read/Grep/Glob, or `--sandbox read-only`). Reads the tree at the exact PR
  head to confirm or refute a claim with real evidence; cannot write.
* **reviewer** — `independent_review`: MODEL_ONLY (zero tools). Renders a
  verdict from the diff/prompt alone.

This module does not add a fourth role or change when each one is dispatched
(`planner.py` still dispatches the writer, `review_merge.py` still owns
verifier/reviewer scheduling) — it is the single, testable place that names
the three-stage route and pins the invariant that makes it a *control*
route rather than three agents that merely happen to run in sequence: each
stage's task_type must resolve to strictly the capability its role needs,
never more. `tests/orchestrator/test_ensemble.py` checks that pin against
`agent_runner`'s own profile tables, so a stage silently regaining a
capability its role must not have (e.g. a verifier task_type reclassified
into `MUTATING_TASK_TYPES`) fails a test instead of shipping quietly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from command_center.orchestrator.routing import (
    cascade_for,
    model_only_review_cascade,
    verification_review_cascade,
)

__all__ = [
    "ROLE_WRITER",
    "ROLE_VERIFIER",
    "ROLE_REVIEWER",
    "EnsembleStage",
    "write_verify_review_route",
]

#: One writes.
ROLE_WRITER = "writer"
#: A second verifies.
ROLE_VERIFIER = "verifier"
#: A third reviews.
ROLE_REVIEWER = "reviewer"


@dataclass(frozen=True, slots=True)
class EnsembleStage:
    """One role in the route: its name and the executor cascade it may
    fail over across, each link already carrying the `task_type` (and, for
    the verdict roles, `capability`) the worker resolves a tool profile
    from."""

    role: str
    cascade: list[dict[str, Any]]


def write_verify_review_route() -> list[EnsembleStage]:
    """The control-layout route this task names: one writer stage, then one
    verifier stage, then one reviewer stage, each stage's cascade built fresh
    from the same live-proven executor cascades `routing.py` already serves
    `planner.py` and `review_merge.py` from — so this route can never drift
    from what those modules actually dispatch."""
    return [
        EnsembleStage(role=ROLE_WRITER, cascade=cascade_for("implementation")),
        EnsembleStage(role=ROLE_VERIFIER, cascade=verification_review_cascade()),
        EnsembleStage(role=ROLE_REVIEWER, cascade=model_only_review_cascade()),
    ]
