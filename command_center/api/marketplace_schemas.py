"""Request bodies and thin response wrappers for the Wave-3 Marketplace surface.

The *entities* returned on this surface are the shared contract models
:class:`command_center.api.models.MarketItem` and
:class:`~command_center.api.models.MarketInstallLogEntry`; the classes here only
describe the **inputs** a client POSTs (register a listing, request an install)
and the small composite responses (a list page, an install-log page) that wrap
those entities.

Kept separate from ``models.py`` on purpose: the entity skeletons are the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel

from command_center.api.models import MarketInstallLogEntry, MarketItem, MarketItemKind


class MarketItemCreate(BaseModel):
    """POST body for registering a catalogue listing. ``name`` and ``kind`` are
    required (a listing is always named and classified); the rest are optional
    descriptive fields. A freshly registered listing is always ``listed``."""

    name: str
    kind: MarketItemKind
    version: str = ""
    publisher: str = ""
    description: str = ""
    provenance: str = ""


class MarketInstallRequest(BaseModel):
    """POST body for ``/marketplace/items/{id}/install``.

    ``actor`` is required — the install log answers *who* installed, so an
    anonymous install is refused at the contract boundary."""

    actor: str


class MarketItemList(BaseModel):
    """A page of catalogue items plus the paging echo the client sent."""

    items: list[MarketItem]
    limit: int
    offset: int


class MarketInstallLog(BaseModel):
    """A page of install-log entries for one listing, newest first."""

    item_id: str
    entries: list[MarketInstallLogEntry]
    limit: int
    offset: int
