"""Trust engine — Human+AI review policy for critical decisions (VOYN-MIN-CI).

Public surface::

    from command_center.trust import RoleVerdict, evaluate

See :mod:`command_center.trust.triple_review` for the executor/audit/stress
3-role review and its consensus rule.
"""

from __future__ import annotations

from command_center.trust.triple_review import (
    ConsensusResult,
    DuplicateRoleError,
    IncompleteReviewError,
    MissingExplanationError,
    ROLES,
    RoleVerdict,
    evaluate,
)

__all__ = [
    "ConsensusResult",
    "DuplicateRoleError",
    "IncompleteReviewError",
    "MissingExplanationError",
    "ROLES",
    "RoleVerdict",
    "evaluate",
]
