"""FastAPI controller for the dispatch policy layer (`/api/v1/dispatch/*`).

Thin by construction — no business logic here. Each handler resolves the repo
root and delegates to `command_center.dispatch.service` / `.policy_config`,
then returns the value object's `as_dict()`. The service and policy-config
modules are referenced as **module globals** (`_service`, `_policy_config`) so a test can
`monkeypatch.setattr` them before issuing a request and never touch the real
board, runtime.db, or policy file — the same hermetic seam the existing
`webapi.app` uses for `ExecutionCenterAPI`.

Endpoints:

* `GET  /api/v1/dispatch/plan`   — dry run: what would be assigned + why.
* `POST /api/v1/dispatch/assign` — apply the plan (records executors; never launches).
* `GET  /api/v1/dispatch/policy` — the current config-driven policy.
* `PUT  /api/v1/dispatch/policy` — update limits/weights.

`actor` is gone from both write bodies (VOYN-W0-AICC-AUTH-HTTP-01), and gone is
the operative word: it is not validated, it is made impossible to express. That
follows `queue_claim()` in `command_center/db/sql/`, whose strongest property is
not that it checks a declared claimant but that it has no claimant parameter at
all — there is nothing to forge because there is nothing to pass. Four
independent layers here:

1. **Schema.** The field is deleted *and* the models set
   ``extra="forbid"``. Deleting it under Pydantic's default ``extra="ignore"``
   would make a forged actor safely ignored — but silently, so a client that has
   been sending one keeps getting 200s and nobody learns the contract changed.
   ``forbid`` turns the same request into a 422: refused out loud, at the
   boundary, before any handler code runs.
2. **Signature.** `service.assign(root, principal, *, confirmed)` has no `actor`
   parameter, so passing one is a `TypeError` — caught by the call, by a linter,
   and by a signature fitness test. This is the layer that survives a future
   handler being rewired; the request-level tests would all still pass if
   `actor` came back as a parameter, which is why the structural check exists.
3. **Type.** The handler's only identity is a `Principal`, constructible only
   from a platform `whoami` response. The untyped `dict = Body()` that made
   `body.get("actor")` possible is gone.
4. **Route.** Both write routes are in the routing table and carry the
   authentication dependency, verified at application build.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from command_center.dispatch import policy_config as _policy_config
from command_center.dispatch import service as _service
from command_center.http_auth.identity import Principal
from command_center.http_auth.routing import require_principal

# Repo root is three levels up: <root>/command_center/dispatch/api.py
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _root() -> Path:
    """Resolved as a function (not a captured constant) so a test can
    monkeypatch this module's `_root` to point at an isolated temp tree."""
    return _REPO_ROOT


class AssignRequest(BaseModel):
    """Body for ``POST /api/v1/dispatch/assign``.

    ``extra="forbid"`` is the load-bearing line — see this module's docstring.
    ``confirmed`` keeps its existing meaning: an explicit opt-in for the write,
    mirroring every other mutating action in this codebase.
    """

    model_config = ConfigDict(extra="forbid")

    confirmed: bool = False


class PolicyUpdateRequest(BaseModel):
    """Body for ``PUT /api/v1/dispatch/policy``.

    ``changes`` stays an open mapping because the policy's field set is owned by
    ``DispatchPolicy.from_dict``, which validates it; duplicating that here
    would create a second contract to keep in step. The bare-body form the old
    handler accepted (``{"prefer_local": true}`` with no ``changes`` wrapper) is
    no longer accepted: it was indistinguishable from a body carrying an
    unexpected top-level key, which is precisely what must now be refused.
    """

    model_config = ConfigDict(extra="forbid")

    changes: dict = Field(default_factory=dict)


def create_dispatch_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/dispatch", tags=["dispatch"])

    @router.get("/plan")
    def get_plan() -> dict:  # read-only, no mutation
        return _service.plan(_root()).as_dict()

    @router.post("/assign")
    def post_assign(
        payload: AssignRequest | None = None,
        principal: Principal = Depends(require_principal),
    ) -> dict:
        body = payload or AssignRequest()
        return _service.assign(_root(), principal, confirmed=body.confirmed)

    @router.get("/policy")
    def get_policy() -> dict:  # read-only, no mutation
        return _policy_config.load_policy(_root()).as_dict()

    @router.put("/policy")
    def put_policy(
        payload: PolicyUpdateRequest,
        principal: Principal = Depends(require_principal),
    ) -> dict:
        return _policy_config.update_policy(
            _root(), payload.changes, principal=principal
        ).as_dict()

    return router
