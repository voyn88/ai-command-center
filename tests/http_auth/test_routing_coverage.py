"""The endpoint nobody remembered is the one that stays open.

This file is the coverage proof, and it is deliberately in two halves:

* a **behavioural** sweep that issues an unauthenticated request at every
  mutating route both applications expose and requires a 401 from each. It
  knows nothing about FastAPI's internals, so no future version of FastAPI can
  make it pass vacuously;
* **structural** checks that the routing table, the operation inventory and the
  carve-out registry agree with the code in both directions — which the
  behavioural sweep cannot see, because a route can be guarded and still be
  routed to the wrong operation, and a schema can grow a client-declared actor
  without any route disappearing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app as create_api_app
from command_center.http_auth import authz, routing
from command_center.webapi.app import create_app as create_webapi_app

#: The size of the mutating surface, asserted as a literal.
#: The premise this task started from was "two endpoints"; the inventory found
#: 29. Pinning the number means growing the surface is a deliberate edit here
#: rather than a silent drift back towards an uncounted one.
EXPECTED_MUTATING_ROUTES = 30  # 29 from AUTH-HTTP-01 + queue:audit:enqueue (APP-CONTROL-S1/S4)


def _apps():
    return [create_api_app(), create_webapi_app()]


def _all_mutating() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for app in _apps():
        rows.extend((verb, path) for verb, path, _ in routing.mutating_routes(app))
    return rows


def _concrete(path: str) -> str:
    """Fill path parameters with a value that will never be looked up: these
    requests are refused before any handler runs, which is the assertion."""
    out = []
    for segment in path.split("/"):
        out.append("no-such-id" if segment.startswith("{") else segment)
    return "/".join(out)


# ---------------------------------------------------------------------------
# Behavioural
# ---------------------------------------------------------------------------


def test_the_mutating_surface_is_the_size_the_inventory_found():
    assert len(_all_mutating()) == EXPECTED_MUTATING_ROUTES


def test_every_mutating_route_refuses_an_unauthenticated_caller(platform, grants):
    """The version-independent half. No route may be reachable without a
    credential — including any route added after this test was written."""
    grants({})
    refused: dict[tuple[str, str], int] = {}
    for app in _apps():
        client = TestClient(app)
        for verb, path, _route in routing.mutating_routes(app):
            response = client.request(verb, _concrete(path), json={})
            refused[(verb, path)] = response.status_code

    not_401 = {route: status for route, status in refused.items() if status != 401}
    assert not_401 == {}, f"reachable without a credential: {not_401}"
    assert len(refused) == EXPECTED_MUTATING_ROUTES
    assert platform.calls == [], "an absent credential costs no platform round trip"


def test_a_credential_alone_still_does_not_open_any_of_them(platform, grants):
    """The same sweep one step further in: authenticated, granted nothing, 403
    everywhere. This is the confused-deputy property held across all 29."""
    platform.issue("t", "some-service-account")
    grants({})
    statuses: dict[tuple[str, str], int] = {}
    for app in _apps():
        client = TestClient(app)
        for verb, path, _route in routing.mutating_routes(app):
            response = client.request(
                verb, _concrete(path), json={}, headers={"Authorization": "Bearer t"}
            )
            statuses[(verb, path)] = response.status_code

    not_403 = {route: status for route, status in statuses.items() if status != 403}
    assert not_403 == {}, f"authenticated-but-ungranted reached: {not_403}"


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------


def test_every_mutating_route_has_a_routing_entry():
    missing = [row for row in _all_mutating() if row not in routing.ROUTE_OPERATIONS]
    assert missing == []


def test_the_routing_table_has_no_entry_for_a_route_that_no_longer_exists():
    """A renamed path leaves an entry that matches nothing — coverage that reads
    as present and is not. It has to be an error, not dead data."""
    stale = set(routing.ROUTE_OPERATIONS) - set(_all_mutating())
    assert stale == set()


def test_the_operation_inventory_and_the_routing_table_agree_exactly():
    """In both directions: an operation nothing routes to is dead, and an
    operation outside the inventory cannot be granted."""
    routed = set(routing.ROUTE_OPERATIONS.values())
    assert routed == set(authz.OPERATIONS)


def test_no_two_routes_share_an_operation():
    """Grants are written per operation, so a shared name silently widens one
    of them beyond what the operator who wrote it intended."""
    seen: dict[str, tuple[str, str]] = {}
    duplicates = []
    for route, operation in routing.ROUTE_OPERATIONS.items():
        if operation in seen:
            duplicates.append((operation, seen[operation], route))
        seen[operation] = route
    assert duplicates == []


def test_the_boot_check_runs_in_both_application_factories():
    """Asserted by *removal*: if the call site is deleted, every other check in
    this file still passes, so nothing else would notice."""
    import command_center.api.app as api_app
    import command_center.webapi.app as webapi_app

    for module, factory in ((api_app, "create_app"), (webapi_app, "create_app")):
        calls: list = []
        original = module.validate_routing
        module.validate_routing = lambda app: calls.append(app)
        try:
            getattr(module, factory)()
        finally:
            module.validate_routing = original
        assert len(calls) == 1, f"{module.__name__}.{factory} does not validate routing"


# ---------------------------------------------------------------------------
# Client-supplied identity: signed carve-outs, never silent ones
# ---------------------------------------------------------------------------


def _body_model_fields(route) -> set[str]:
    fields: set[str] = set()
    for parameter in getattr(route.dependant, "body_params", []):
        annotation = parameter.field_info.annotation
        for candidate in getattr(annotation, "__args__", (annotation,)):
            fields |= set(getattr(candidate, "model_fields", {}) or {})
    return fields


def test_every_client_supplied_identity_field_is_a_signed_carve_out():
    """The check that keeps this honest as schemas change.

    Authentication is mounted on all 29 routes; that is not in question here.
    What this asserts is narrower and easier to lose: where a handler *also*
    reads an identity from the request body, the fact is written down with a
    reason and a task — never passed over in silence, and never reintroduced by
    a later schema edit without this test going red.
    """
    unsigned: dict[tuple[str, str], set[str]] = {}
    for app in _apps():
        for verb, path, route in routing.mutating_routes(app):
            identity_fields = _body_model_fields(route) & routing.CLIENT_IDENTITY_FIELD_NAMES
            if not identity_fields:
                continue
            carve_out = routing.CLIENT_IDENTITY_CARVE_OUTS.get((verb, path))
            signed = set(carve_out.fields) if carve_out else set()
            if identity_fields - signed:
                unsigned[(verb, path)] = identity_fields - signed

    assert unsigned == {}, f"client-declared identity with no signed carve-out: {unsigned}"


def test_no_carve_out_outlives_the_field_it_describes():
    """The other direction: once a field is deleted, its carve-out must go too,
    or the registry slowly becomes a list of problems that were already fixed."""
    live: dict[tuple[str, str], set[str]] = {}
    for app in _apps():
        for verb, path, route in routing.mutating_routes(app):
            live[(verb, path)] = _body_model_fields(route)

    stale = {
        route: sorted(set(carve_out.fields) - live.get(route, set()))
        for route, carve_out in routing.CLIENT_IDENTITY_CARVE_OUTS.items()
        if set(carve_out.fields) - live.get(route, set())
    }
    assert stale == {}


@pytest.mark.parametrize("route", sorted(routing.CLIENT_IDENTITY_CARVE_OUTS))
def test_a_carve_out_names_a_reason_and_a_task(route):
    carve_out = routing.CLIENT_IDENTITY_CARVE_OUTS[route]
    assert carve_out.fields, route
    assert carve_out.task.startswith("VOYN-"), carve_out.task
    assert len(carve_out.reason) > 60, "a carve-out with no argument is a silent skip"


def test_the_carve_outs_are_all_on_routed_endpoints():
    assert set(routing.CLIENT_IDENTITY_CARVE_OUTS) <= set(routing.ROUTE_OPERATIONS)


def test_the_dispatch_endpoints_are_not_carved_out():
    """The two endpoints this task actually closed. If a carve-out ever appears
    for them, the fix was reverted."""
    for route in (
        ("POST", "/api/v1/dispatch/assign"),
        ("PUT", "/api/v1/dispatch/policy"),
    ):
        assert route not in routing.CLIENT_IDENTITY_CARVE_OUTS
