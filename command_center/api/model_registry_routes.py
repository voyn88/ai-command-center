"""HTTP routes for the Wave-3 model-registry surface.

Controllers only: each handler is a thin adapter that validates its inputs via
FastAPI, delegates to exactly one
:mod:`command_center.api.model_registry_service` function, and maps a
``None``/domain error onto the right HTTP status. No business logic, no data
access, and no event publishing live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every path
below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import model_registry_schemas as s
from command_center.api import model_registry_service as service
from command_center.api import models
from command_center.models_registry.policy import SensitiveModelRoutingError

router = APIRouter(prefix="/api/v1", tags=["models"])

# Shared paging bound for the list endpoint.
_MAX_LIMIT = 500


@router.post("/models", response_model=models.ModelEntry, status_code=201)
def register_model(payload: s.RegisterModelRequest) -> models.ModelEntry:
    """Register an external or local model in the catalog."""
    return service.register_model(payload)


@router.get("/models", response_model=s.ModelList)
def list_models(
    kind: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.ModelList:
    """The catalog, filterable by ``kind`` (external/local) and ``status``."""
    return service.list_models(
        kind=kind, status=status, limit=limit, offset=offset
    )


@router.get("/models/{model_id}", response_model=models.ModelEntry)
def get_model(model_id: str) -> models.ModelEntry:
    found = service.get_model(model_id)
    if found is None:
        raise HTTPException(status_code=404, detail="model not found")
    return found


@router.post("/models/{model_id}/download", response_model=s.DownloadModelResult)
def download_model(
    model_id: str, payload: s.DownloadModelRequest | None = None
) -> s.DownloadModelResult:
    """Run a local model's download lifecycle. An external model has nothing to
    download (409); an unknown model is a 404."""
    try:
        result = service.download_model(
            model_id, payload or s.DownloadModelRequest()
        )
    except service.ModelNotDownloadableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="model not found")
    return result


@router.post("/models/{model_id}/assign", response_model=s.AssignModelResponse)
def assign_model(
    model_id: str, payload: s.AssignModelRequest
) -> s.AssignModelResponse:
    """Assign a model to a task/agent. A sensitive context routed to an external
    model is rejected (400); an unknown model is a 404."""
    try:
        result = service.assign_model(model_id, payload)
    except SensitiveModelRoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="model not found")
    return result


@router.get("/models/{model_id}/history", response_model=s.ModelHistory)
def get_history(model_id: str) -> s.ModelHistory:
    """The model's full governance log (oldest → newest) — its traceable history."""
    found = service.get_history(model_id)
    if found is None:
        raise HTTPException(status_code=404, detail="model not found")
    return found
