"""The process refuses to start rather than serving an unguarded route.

A CI test catches a forgotten guard only if CI runs. A boot check also catches a
route added by a hotfix, by a plugin, or by a merge that skipped the suite — and
it fails in the environment that matters instead of in a report.
"""

from __future__ import annotations

import json

import pytest
from fastapi import APIRouter, Depends, FastAPI

from command_center.http_auth import authz
from command_center.http_auth.routing import (
    RouteInventoryError,
    enforce,
    validate_routing,
)


def _app_with(path: str, *, guarded: bool, verb: str = "post") -> FastAPI:
    router = APIRouter()
    getattr(router, verb)(path)(lambda: {})
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(enforce)] if guarded else [])
    return app


def test_a_mutating_route_outside_the_routing_table_stops_the_process():
    with pytest.raises(RouteInventoryError) as caught:
        validate_routing(_app_with("/api/v1/newly-added", guarded=True))
    assert "/api/v1/newly-added" in str(caught.value)


def test_a_routed_route_without_the_dependency_stops_the_process(monkeypatch):
    """A table entry for a route the dependency never runs on is worse than no
    entry at all, because it reads as coverage."""
    monkeypatch.setitem(
        __import__(
            "command_center.http_auth.routing", fromlist=["ROUTE_OPERATIONS"]
        ).ROUTE_OPERATIONS,
        ("POST", "/api/v1/newly-added"),
        "dispatch:assign",
    )

    with pytest.raises(RouteInventoryError) as caught:
        validate_routing(_app_with("/api/v1/newly-added", guarded=False))
    assert "dependency not mounted" in str(caught.value)


def test_an_inventory_of_zero_routes_is_treated_as_a_broken_walker():
    """Not a clean bill of health.

    This is not hypothetical: FastAPI wraps included routers in a node that
    exposes neither ``.methods`` nor ``.routes``, and the first version of the
    walker inspected nothing and reported success. Everything else in this file
    would have passed.
    """
    with pytest.raises(RouteInventoryError) as caught:
        validate_routing(FastAPI())
    assert "0 mutating routes" in str(caught.value)


def test_reads_alone_also_count_as_a_zero_inventory():
    app = FastAPI()
    router = APIRouter()
    router.get("/api/v1/health")(lambda: {})
    app.include_router(router, dependencies=[Depends(enforce)])

    with pytest.raises(RouteInventoryError):
        validate_routing(app)


def test_a_broken_grant_file_stops_the_process_too(monkeypatch, tmp_path):
    """Configuration that cannot be parsed must fail the deploy, not surface at
    03:00 as an operator who has quietly lost their access."""
    from command_center.webapi.app import create_app

    path = tmp_path / "grants.json"
    path.write_text(json.dumps({"operator:one": ["dispatch:typo"]}), encoding="utf-8")
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
    authz.reset_grants_cache()

    with pytest.raises(authz.GrantsConfigurationError):
        create_app()
    authz.reset_grants_cache()


def test_a_route_added_after_startup_is_denied_rather_than_waved_through(
    platform, grants
):
    """The one case the boot check cannot cover, so the dependency covers it.

    `validate_routing` runs once, at build time. A router that grows a route
    afterwards — a plugin, a hot reload, a late `include_router` — is past that
    gate, and the process is already serving. Reaching the handler because the
    routing table has nothing to say about it would be exactly backwards, so
    "no entry" denies.
    """
    from fastapi.testclient import TestClient

    router = APIRouter()
    router.put("/api/v1/dispatch/policy")(lambda: {})
    app = FastAPI()
    app.include_router(router, dependencies=[Depends(enforce)])
    validate_routing(app)

    platform.issue("t", "operator:one")
    grants({"operator:one": sorted(authz.OPERATIONS)})
    router.post("/api/v1/added-after-the-fact")(lambda: {"reached": True})

    response = TestClient(app).post(
        "/api/v1/added-after-the-fact", json={}, headers={"Authorization": "Bearer t"}
    )

    assert response.status_code == 403, response.text


def test_the_real_applications_pass_the_check():
    from command_center.api.app import create_app as api_app
    from command_center.webapi.app import create_app as webapi_app

    validate_routing(api_app())
    validate_routing(webapi_app())
