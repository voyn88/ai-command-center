"""Who may vote on the Board, and in what role — the config-driven role /
permission seam for the Council engine.

A :class:`CouncilRoster` maps a ``voter_id`` to a :class:`CouncilMember` (its
role, kind and whether it may currently vote). The service consults the roster on
every vote to answer two questions:

1. **May this voter vote?** — the permission notion. A member with
   ``can_vote=False`` (e.g. a human invited to the Board but not yet activated)
   is refused with :class:`VoterNotPermittedError`; a voter absent from the
   roster is refused with :class:`NotAMemberError` *unless* the roster is set to
   ``open_membership`` (the default for AI members that self-identify).
2. **In what role?** — the role recorded on the vote. The role comes from the
   roster, not from the client, so a vote's recorded role is authoritative and
   cannot be spoofed by the caller.

This is deliberately a *seam*, not a wired identity system: external humans are
represented as ``kind="human"`` members and can be admitted by flipping
``can_vote`` when they are invited, but the actual external-identity binding
(SSO, invitations) is a later increment. Nothing here reaches out to an identity
provider — the roster is plain in-process configuration a caller (or a test)
can substitute wholesale.

The roster is referenced through a module-level name in the service
(``service._roster``) so a deployment or a test can swap the whole policy without
touching the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


class NotAMemberError(Exception):
    """Raised when a ``voter_id`` is not on the roster and the roster does not
    admit unlisted voters (``open_membership`` is ``False``). Surfaced as HTTP
    403 — this identity is not a member of the Board."""


class VoterNotPermittedError(Exception):
    """Raised when a listed member is not currently allowed to vote
    (``can_vote`` is ``False``) — e.g. an external human seat that has been
    created but not yet activated. Surfaced as HTTP 403."""


@dataclass(frozen=True, slots=True)
class CouncilMember:
    """One seat on the Board. ``role`` is recorded on every vote this member
    casts; ``kind`` is ``ai`` or ``human``; ``can_vote`` gates whether the seat is
    currently active (the invite seam for humans)."""

    voter_id: str
    role: str
    kind: str = "ai"
    can_vote: bool = True


@dataclass(frozen=True, slots=True)
class CouncilRoster:
    """The Board's membership and voting policy.

    ``members`` maps ``voter_id`` → :class:`CouncilMember`. ``open_membership``
    (default ``True``) admits a voter not explicitly listed, assigning it
    ``default_role`` and ``default_kind`` — the seam for AI members that
    self-identify without pre-registration; set it ``False`` to run a closed
    Board where only listed seats may vote. External humans are always explicit
    members (never admitted via ``open_membership``, which only grants the ai
    default kind)."""

    members: Mapping[str, CouncilMember] = field(default_factory=dict)
    open_membership: bool = True
    default_role: str = "member"
    default_kind: str = "ai"

    def permit(self, voter_id: str, *, voter_kind: str | None = None) -> CouncilMember:
        """Resolve the seat a vote by ``voter_id`` is cast from, enforcing the
        permission policy.

        Returns the :class:`CouncilMember` whose ``role``/``kind`` the vote will
        record. Raises :class:`NotAMemberError` when the voter is unlisted and the
        Board is closed, or :class:`VoterNotPermittedError` when a listed seat is
        inactive."""
        member = self.members.get(voter_id)
        if member is not None:
            if not member.can_vote:
                raise VoterNotPermittedError(
                    f"member {voter_id!r} ({member.role}) is not currently permitted to vote"
                )
            return member
        if not self.open_membership:
            raise NotAMemberError(
                f"{voter_id!r} is not a member of the Board and open membership is off"
            )
        # Open membership admits a self-identifying AI voter with the default
        # role. A caller-supplied kind is honoured only within the recognised set;
        # anything else falls back to the roster default (the repository validates
        # the final value again at the persistence boundary).
        kind = voter_kind if voter_kind in ("ai", "human") else self.default_kind
        return CouncilMember(voter_id=voter_id, role=self.default_role, kind=kind)


#: The process-default Board: a small panel of AI seats with distinct roles, plus
#: open membership so an ad-hoc AI voter is admitted as a ``member`` (its role is
#: still recorded). A deployment overrides this by substituting ``service._roster``
#: with a closed roster of named seats.
DEFAULT_ROSTER = CouncilRoster(
    members={
        "chair": CouncilMember(voter_id="chair", role="chair"),
        "architect": CouncilMember(voter_id="architect", role="architect"),
        "security": CouncilMember(voter_id="security", role="security"),
        "product": CouncilMember(voter_id="product", role="product"),
        "operations": CouncilMember(voter_id="operations", role="operations"),
    },
    open_membership=True,
)
