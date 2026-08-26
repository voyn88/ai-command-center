"""`actor` is not validated at the dispatch boundary. It is made impossible.

The model is ``queue_claim()``: its strongest property is not that it checks a
declared claimant but that it has no claimant parameter — there is nothing to
forge because there is nothing to pass. The HTTP analogue is tested here at all
four layers, because each one survives a different mistake:

* the **schema** layer survives a handler rewrite that forgets to read the field;
* the **signature** layer survives a request-level test suite that would happily
  pass if ``actor`` came back as a parameter;
* the **type** layer survives a body being widened back to an untyped ``dict``;
* the **route** layer survives all of the above being correct on an endpoint
  nobody mounted a guard on.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from command_center.dispatch import api as dispatch_api
from command_center.dispatch import policy_config, service
from command_center.dispatch.models import DispatchPolicy
from command_center.http_auth.identity import Principal
from command_center.webapi.app import create_app

_ASSIGN = "/api/v1/dispatch/assign"
_POLICY = "/api/v1/dispatch/policy"


@pytest.fixture
def recorder(monkeypatch):
    """Capture what the service layer was actually asked to do."""
    seen: dict = {}

    def fake_assign(root, principal, *, confirmed):
        seen["assign"] = {"principal": principal, "confirmed": confirmed}
        return {"applied": confirmed, "actor": principal.principal_id}

    def fake_update(root, changes, *, principal):
        seen["update"] = {"principal": principal, "changes": changes}
        return DispatchPolicy()

    monkeypatch.setattr(dispatch_api._service, "assign", fake_assign)
    monkeypatch.setattr(dispatch_api._policy_config, "update_policy", fake_update)
    return seen


@pytest.fixture
def client(platform, grants):
    platform.issue("t", "operator:one")
    grants({"operator:one": ["dispatch:assign", "dispatch:policy:update"]})
    return TestClient(create_app())


_AUTH = {"Authorization": "Bearer t"}


# --- 1. schema -------------------------------------------------------------


@pytest.mark.parametrize("path,body", [
    (_ASSIGN, {"confirmed": True, "actor": "someone-else"}),
    (_POLICY, {"changes": {}, "actor": "someone-else"}),
])
def test_a_forged_actor_is_refused_out_loud_not_ignored(client, recorder, path, body):
    """``extra="forbid"`` is the load-bearing line.

    Deleting the field under Pydantic's default ``extra="ignore"`` would make a
    forged actor *safe* but *silent*: a client that has been sending one keeps
    getting 200s and nobody learns the contract changed. 422 is the difference
    between a fixed vulnerability and a fixed vulnerability somebody notices.
    """
    verb = client.post if path == _ASSIGN else client.put

    response = verb(path, json=body, headers=_AUTH)

    assert response.status_code == 422
    assert recorder == {}, "nothing reached the service layer"


def test_an_actor_nested_inside_policy_changes_is_not_an_identity(client, recorder):
    """``changes`` stays an open mapping (``DispatchPolicy.from_dict`` owns that
    contract), so this asserts the obvious-in-hindsight thing: a key named
    ``actor`` in there is policy data, and provenance still comes from the
    caller."""
    response = client.put(
        _POLICY, json={"changes": {"actor": "someone-else"}}, headers=_AUTH
    )

    assert response.status_code == 200
    assert recorder["update"]["principal"].principal_id == "operator:one"


# --- 2. signature ----------------------------------------------------------


@pytest.mark.parametrize(
    "function", [service.assign, policy_config.update_policy]
)
def test_the_service_layer_has_no_actor_parameter(function):
    """The structural layer. Every request-level assertion in this file would
    still pass if ``actor`` came back as a parameter — this is the one that
    would not."""
    parameters = inspect.signature(function).parameters
    assert "actor" not in parameters
    assert "principal" in parameters


def test_passing_an_actor_to_the_service_is_a_type_error(tmp_path):
    with pytest.raises(TypeError):
        service.assign(tmp_path, confirmed=True, actor="someone-else")


def test_the_principal_type_cannot_be_produced_from_a_request_body():
    """A ``Principal`` is not a Pydantic model and is not parsed from anything a
    client sends; it exists only as the return of platform verification."""
    from pydantic import BaseModel

    assert not issubclass(Principal, BaseModel)
    assert Principal.__dataclass_params__.frozen


# --- 3. type ---------------------------------------------------------------


def test_the_write_bodies_are_typed_not_open_dicts():
    for model in (dispatch_api.AssignRequest, dispatch_api.PolicyUpdateRequest):
        assert model.model_config.get("extra") == "forbid"
        assert "actor" not in model.model_fields


def test_the_recorded_actor_is_the_authenticated_caller(client, recorder):
    response = client.post(_ASSIGN, json={"confirmed": True}, headers=_AUTH)

    assert response.status_code == 200
    assert response.json()["actor"] == "operator:one"
    assert recorder["assign"]["principal"] == Principal(
        principal_id="operator:one", tenant_id="tenant-1", capabilities=("operator:read",)
    )


def test_two_callers_are_recorded_as_themselves(client, recorder, platform, grants):
    platform.issue("t2", "operator:two")
    grants({
        "operator:one": ["dispatch:assign"],
        "operator:two": ["dispatch:assign"],
    })

    for token, expected in (("t", "operator:one"), ("t2", "operator:two")):
        response = client.post(
            _ASSIGN, json={"confirmed": True}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.json()["actor"] == expected


# --- 4. route --------------------------------------------------------------


def test_neither_write_is_reachable_without_a_credential(client, recorder):
    assert client.post(_ASSIGN, json={"confirmed": True}).status_code == 401
    assert client.put(_POLICY, json={"changes": {}}).status_code == 401
    assert recorder == {}
