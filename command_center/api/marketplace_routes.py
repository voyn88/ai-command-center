"""HTTP routes for the Wave-3 Marketplace surface.

Controllers only: each handler is a thin adapter that validates its inputs via
FastAPI, delegates to exactly one :mod:`command_center.marketplace.service`
function, and maps a ``None``/domain error onto the right HTTP status. No
business logic, no data access and no install work live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every path
below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import marketplace_schemas as w
from command_center.api import models
from command_center.marketplace import service

router = APIRouter(prefix="/api/v1", tags=["marketplace"])

# Shared paging bound for the list endpoints on this surface.
_MAX_LIMIT = 500


@router.get("/marketplace/items", response_model=w.MarketItemList)
def list_items(
    kind: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.MarketItemList:
    return service.list_items(kind=kind, status=status, limit=limit, offset=offset)


@router.get("/marketplace/items/{item_id}", response_model=models.MarketItem)
def get_item(item_id: str) -> models.MarketItem:
    found = service.get_item(item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="market item not found")
    return found


@router.post("/marketplace/items", response_model=models.MarketItem, status_code=201)
def register_item(payload: w.MarketItemCreate) -> models.MarketItem:
    try:
        return service.register_item(payload)
    except ValueError as exc:
        # Bad kind — a client error, refused before it reaches SQL.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/marketplace/items/{item_id}/install", response_model=models.MarketItem
)
def install_item(item_id: str, payload: w.MarketInstallRequest) -> models.MarketItem:
    try:
        return service.install_item(item_id, actor=payload.actor)
    except service.MarketItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/marketplace/items/{item_id}/log", response_model=w.MarketInstallLog
)
def get_install_log(
    item_id: str,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.MarketInstallLog:
    found = service.get_install_log(item_id, limit=limit, offset=offset)
    if found is None:
        raise HTTPException(status_code=404, detail="market item not found")
    return found
