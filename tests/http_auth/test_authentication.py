"""Who gets in, who does not, and what happens when the authority is silent.

Every assertion here is driven through a real mutating route
(``PUT /api/v1/dispatch/policy``) rather than against the dependency in
isolation, so the wiring is under test too: a guard that is correct but not
mounted fails these.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.dispatch import api as dispatch_api
from command_center.dispatch.models import DispatchPolicy
from command_center.http_auth import identity
from command_center.webapi.app import create_app

_POLICY = "/api/v1/dispatch/policy"


@pytest.fixture
def client(monkeypatch):
    """A real app whose policy store is inert — this file is about the gate."""
    monkeypatch.setattr(
        dispatch_api._policy_config,
        "update_policy",
        lambda root, changes, *, principal: DispatchPolicy(),
    )
    return TestClient(create_app())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_request_with_no_credential_is_refused(client, platform, grants):
    grants({"operator:one": ["dispatch:policy:update"]})

    response = client.put(_POLICY, json={"changes": {"prefer_local": False}})

    assert response.status_code == 401
    assert platform.calls == [], "an absent credential must not cost a platform round trip"


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Basic abc",
        "Bearer  padded",
        "Bearer trailing ",
        "token-with-no-scheme",
    ],
)
def test_an_unparseable_authorization_header_is_not_a_credential(
    client, platform, grants, header
):
    """Strict parsing on purpose.

    A header AICC cannot read unambiguously is not a credential, and whitespace
    is never trimmed on the way through: AICC and the platform must never
    disagree about which bytes were presented.
    """
    grants({"operator:one": ["dispatch:policy:update"]})

    response = client.put(
        _POLICY, json={"changes": {}}, headers={"Authorization": header}
    )

    assert response.status_code == 401
    assert platform.calls == []


def test_a_credential_the_platform_rejects_is_refused(client, platform, grants):
    grants({"operator:one": ["dispatch:policy:update"]})

    response = client.put(_POLICY, json={"changes": {}}, headers=_auth("not-a-token"))

    assert response.status_code == 401
    assert platform.calls == ["not-a-token"]
    # The platform's reason vocabulary is never relayed: forwarding which of
    # unknown/wrong-secret/expired/revoked applied would make AICC a free
    # oracle for an attacker probing tokens.
    assert response.json()["detail"] == "unauthenticated"


def test_a_verified_and_granted_caller_is_admitted(client, platform, grants):
    platform.issue("t-good", "operator:one")
    grants({"operator:one": ["dispatch:policy:update"]})

    response = client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good"))

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_an_unreachable_authority_refuses_the_write(client, platform, grants):
    """Fail-open would make the availability of authentication an attacker-controlled
    variable: causing the outage is cheaper than defeating the check."""
    platform.issue("t-good", "operator:one")
    grants({"operator:one": ["dispatch:policy:update"]})
    platform.transport_error = identity.PlatformUnavailable("connection refused")

    response = client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good"))

    assert response.status_code == 503


@pytest.mark.parametrize("status", [429, 500, 502, 503, 302, 204])
def test_any_answer_that_is_not_200_or_401_is_not_permission(
    client, platform, grants, status
):
    """429 from the platform's verification gate is the important one.

    Exhausting a small semaphore is a cheap way to make the authority stop
    answering; reading anything other than a definite 200 as success would turn
    that into an open door.
    """
    platform.issue("t-good", "operator:one")
    grants({"operator:one": ["dispatch:policy:update"]})
    platform.force_status = status

    response = client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good"))

    assert response.status_code == 503


def test_an_unparseable_answer_is_not_permission(client, platform, grants):
    platform.issue("t-good", "operator:one")
    grants({"operator:one": ["dispatch:policy:update"]})
    platform.malformed_body = b'{"data": {"tenant_id": "t"}}'  # no principal_id

    response = client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good"))

    assert response.status_code == 503


def test_an_unconfigured_platform_url_refuses_rather_than_allows(
    client, platform, grants, monkeypatch
):
    """A deployment that was never told where its identity authority is cannot
    authenticate anyone, and must not therefore authorize everyone."""
    platform.issue("t-good", "operator:one")
    grants({"operator:one": ["dispatch:policy:update"]})
    monkeypatch.delenv(identity.PLATFORM_URL_ENV, raising=False)

    response = client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good"))

    assert response.status_code == 503


def test_the_outage_status_is_distinct_from_the_refusal_status(
    client, platform, grants
):
    """503 and 401 are different conditions and must stay distinguishable: they
    want different alerts and different client retry behaviour."""
    grants({"operator:one": ["dispatch:policy:update"]})
    refused = client.put(_POLICY, json={"changes": {}}, headers=_auth("nope"))

    platform.issue("t-good", "operator:one")
    platform.transport_error = identity.PlatformUnavailable("down")
    unknown = client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good"))

    assert (refused.status_code, unknown.status_code) == (401, 503)


# ---------------------------------------------------------------------------
# No cache on the write path
# ---------------------------------------------------------------------------


def test_every_write_verifies_again(client, platform, grants):
    platform.issue("t-good", "operator:one")
    grants({"operator:one": ["dispatch:policy:update"]})

    for _ in range(3):
        assert (
            client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good")).status_code
            == 200
        )

    assert platform.calls == ["t-good"] * 3


def test_a_revoked_credential_stops_working_on_the_next_request(
    client, platform, grants
):
    """The acceptance criterion, and the reason there is no cache.

    Any TTL AICC introduced would be staleness AICC *added* to a contract that
    does not have it — and the window it opened would be exactly the window in
    which a revoked operator can still mutate.
    """
    platform.issue("t-good", "operator:one")
    grants({"operator:one": ["dispatch:policy:update"]})
    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good")).status_code == 200

    platform.revoke("t-good")

    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t-good")).status_code == 401


def test_two_callers_cannot_be_confused_for_each_other(client, platform, grants):
    platform.issue("t-one", "operator:one")
    platform.issue("t-two", "operator:two")
    grants({"operator:one": ["dispatch:policy:update"]})

    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t-one")).status_code == 200
    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t-two")).status_code == 403


def test_reads_are_not_charged_for_the_write_path_gate(
    client, platform, grants, monkeypatch
):
    """Reads stay out of scope by acceptance criteria (VOYN-W0-AICC-AUTH-HTTP-02).

    The dependency is mounted on the whole router, reads included, so this is
    the assertion that it costs them nothing: a GET on the same router as the
    guarded PUT serves with no credential and no platform round trip.
    """
    monkeypatch.setattr(
        dispatch_api._policy_config, "load_policy", lambda root: DispatchPolicy()
    )
    grants({})

    assert client.get(_POLICY).status_code == 200
    assert platform.calls == []
