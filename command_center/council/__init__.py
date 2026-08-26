"""Wave-3 Council / Board-of-Directors engine (VOYN-W3-COUNCIL).

The domain tier of the collective-decision stack (routes → **service** →
repository → db). Public surface::

    from command_center.council import service
    from command_center.council.roles import CouncilRoster, DEFAULT_ROSTER
    from command_center.council.intake import install_default_intake

The pipeline is Motion → Vote → Decision: a motion is raised (manually or from a
proposal/incident event), board members vote once each, and when quorum is met
the motion closes into an immutable, ADR-style decision that carries the roles of
every voter and a full journal. See :mod:`command_center.council.service`.
"""

from __future__ import annotations

from command_center.council.intake import install_default_intake
from command_center.council.roles import (
    DEFAULT_ROSTER,
    CouncilMember,
    CouncilRoster,
    NotAMemberError,
    VoterNotPermittedError,
)

__all__ = [
    "install_default_intake",
    "CouncilMember",
    "CouncilRoster",
    "DEFAULT_ROSTER",
    "NotAMemberError",
    "VoterNotPermittedError",
]
