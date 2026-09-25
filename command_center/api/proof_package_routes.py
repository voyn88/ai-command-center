"""HTTP routes for the VOYN-MIN-WOW-1 proof-package surface.

Controllers only: the one handler below validates its input via FastAPI,
delegates to :mod:`command_center.api.proof_package_service`, and maps the
domain error onto the right HTTP status. No business logic and no data access
live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); the path
below is relative to that. Read-only — no entry in
``http_auth.routing.ROUTE_OPERATIONS`` is needed, the same as every other
GET-only surface.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from command_center.api import proof_package_schemas as p
from command_center.api import proof_package_service as service

router = APIRouter(prefix="/api/v1", tags=["proof-package"])


@router.get("/proof-package/{project}", response_model=p.ProofPackage)
def get_proof_package(project: str) -> p.ProofPackage:
    """The assembled, hash-stamped proof package for ``project``: Digital
    Memory, Counterfactual, Decision P&L and Audit Vault. A sensitive
    (BANK/LEGAL) project is rejected (400) rather than returning an empty
    package."""
    try:
        return service.get_proof_package(project)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
