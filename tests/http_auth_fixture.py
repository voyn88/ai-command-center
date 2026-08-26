"""A test caller that is already authenticated, for the suites that are not about auth.

Every mutating HTTP route now requires a verified platform principal
(VOYN-W0-AICC-AUTH-HTTP-01). The endpoint suites predate that and are about
endpoint *semantics*, so they opt into this fixture from their package
``conftest.py`` rather than each growing a bearer token.

What it replaces is deliberately narrow: only
``http_auth.routing.authenticate`` — the network call to the platform. The
routing-table lookup, the operation resolution and the local authorization
check all still run, against a real grant file written to a temp directory, so
these suites still exercise the guard rather than removing it.

What it does **not** do is disable the guard. There is no bypass flag in the
production code and this fixture cannot create one: a route that lost its
authentication dependency would still pass here, which is exactly why
``tests/http_auth/test_routing_coverage.py`` drives an unauthenticated request
at all 29 mutating routes and requires a 401 from every one. That test, not
this fixture, is what proves coverage.
"""

from __future__ import annotations

import json

import pytest

from command_center.http_auth import authz
from command_center.http_auth import routing
from command_center.http_auth.identity import Principal

#: The identity every legacy endpoint test acts as.
TEST_PRINCIPAL = Principal(
    principal_id="test:suite", tenant_id="test-tenant", capabilities=()
)


@pytest.fixture
def authenticated_caller(monkeypatch, tmp_path):
    """Act as :data:`TEST_PRINCIPAL`, granted every operation in the inventory."""
    grants_file = tmp_path / "http_grants.json"
    grants_file.write_text(
        json.dumps({TEST_PRINCIPAL.principal_id: sorted(authz.OPERATIONS)}),
        encoding="utf-8",
    )
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(grants_file))
    authz.reset_grants_cache()
    monkeypatch.setattr(routing, "authenticate", lambda request: TEST_PRINCIPAL)
    try:
        yield TEST_PRINCIPAL
    finally:
        authz.reset_grants_cache()
