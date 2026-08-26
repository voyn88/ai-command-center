"""API client boundary for the operator dashboard (W1-DASHBOARD-UI).

The single seam between the Streamlit dashboard surface and the read/write API
already shipped under ``/api/v1``. The presentation module
(:mod:`command_center.ui.operator_dashboard`) never reaches into the domain
(``tasks_repository``, ``runtime.db``, the advisor collectors) directly — it
holds one of these clients and calls a method per endpoint, exactly mirroring
what an over-the-wire HTTP client would do. That keeps the UI honest about the
contract it consumes and keeps every privacy/redaction rule the API service
layer applies (BANK/LEGAL projects are dropped there, not here) in force.

Streamlit-free on purpose: this is data wiring, not rendering, so it is unit
testable with plain ``pytest`` and trivially replaced by a fake in the UI
tests. Each method returns the same typed wire schema the matching route
returns — no re-shaping, no business logic — so the response the dashboard
renders is byte-for-byte the response a client of the HTTP API would decode.

Endpoints consumed:

* ``GET  /api/v1/dashboard``          -> :class:`schemas.DashboardResponse`
* ``GET  /api/v1/advisor/proposals``  -> :class:`wave1_schemas.ProposalList`
* ``GET  /api/v1/digest/today``       -> :class:`wave1_schemas.DigestItemList`
* ``GET  /api/v1/owner-items``        -> :class:`wave1_schemas.OwnerItemList`
* ``POST /api/v1/proposals/{id}/promote`` -> :class:`wave1_schemas.PromoteResponse`
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Type-only: kept out of the module-load path so importing the desktop shell
    # (``import app``) stays free of the API layer's pydantic dependency. The
    # boundary-fitness gate installs only the desktop runtime deps and imports
    # ``app`` — this surface must not drag ``command_center.api`` (and pydantic)
    # into that graph until the dashboard actually renders.
    from command_center.api import schemas
    from command_center.api import wave1_schemas as w

# Paging bound the list endpoints accept (see ``wave1_routes._MAX_LIMIT``); the
# dashboard shows a bounded, most-recent window rather than an unbounded list.
_DASHBOARD_PAGE = 50


class DashboardClient(Protocol):
    """The narrow contract the dashboard renders against.

    A :class:`Protocol` so the UI tests can inject a hand-written fake (or a
    stub that raises, to exercise the error state) without subclassing — the
    render layer only ever depends on these five method shapes.
    """

    def dashboard(self) -> schemas.DashboardResponse: ...

    def advisor_proposals(self) -> w.ProposalList: ...

    def digest(self) -> w.DigestItemList: ...

    def owner_items(self) -> w.OwnerItemList: ...

    def promote_proposal(self, proposal_id: str) -> w.PromoteResponse | None: ...


class InProcessDashboardClient:
    """Default client: calls the API service layer in the same process.

    The desktop shell embeds the backend, so there is no socket to cross — but
    the seam is still the API's own aggregation/redaction layer
    (``command_center.api.service`` / ``wave1_service``), never the domain
    stores underneath it. That is what makes this "consume the API" rather than
    "bypass it": the methods below are the exact bodies the FastAPI route
    handlers delegate to.
    """

    def dashboard(self) -> schemas.DashboardResponse:
        from command_center.api import service as read_service

        return read_service.build_dashboard()

    def advisor_proposals(self) -> w.ProposalList:
        # The Советник inbox — newest first, actionable statuses surfaced as
        # "находки" cards by the render layer. Filtering by status is the
        # render layer's job; the endpoint returns the page.
        from command_center.api import wave1_service as write_service

        return write_service.list_proposals(limit=_DASHBOARD_PAGE, offset=0)

    def digest(self) -> w.DigestItemList:
        # Day-scoped: today's already-built digest in assembly order (backs
        # ``GET /api/v1/digest/today``), not the whole unbounded digest table.
        # The dashboard shows *today's* ordered feed; reading the full table
        # surfaced stale entries and risked double-notifying on already-seen
        # items (Wave-1 audit LOW-2).
        from command_center.api import wave1_service as write_service

        return write_service.list_digest_today()

    def owner_items(self) -> w.OwnerItemList:
        from command_center.api import wave1_service as write_service

        return write_service.list_owner_items(limit=_DASHBOARD_PAGE, offset=0)

    def promote_proposal(self, proposal_id: str) -> w.PromoteResponse | None:
        # Mirrors ``POST /proposals/{id}/promote``: the write goes through the
        # single-writer task path inside the service; a terminal-status
        # proposal raises there and is surfaced to the caller unchanged.
        from command_center.api import wave1_service as write_service

        return write_service.promote_proposal(proposal_id)
