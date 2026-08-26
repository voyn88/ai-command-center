"""A 200 from ``whoami`` is not permission.

The failure this file exists to prevent is the confused deputy: ``whoami``
returns a principal id, a tenant and a few platform-*global* capabilities, with
no audience field and nothing AICC-specific. So it proves only that the caller
holds a live credential somewhere in the platform. Every service account any
operator ever issued gets a 200. Reading that as authorization would hand write
access to AICC to the platform's entire principal set.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from command_center.dispatch import api as dispatch_api
from command_center.dispatch.models import DispatchPolicy
from command_center.http_auth import authz
from command_center.webapi.app import create_app

_ASSIGN = "/api/v1/dispatch/assign"
_POLICY = "/api/v1/dispatch/policy"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        dispatch_api._policy_config,
        "update_policy",
        lambda root, changes, *, principal: DispatchPolicy(),
    )
    monkeypatch.setattr(
        dispatch_api._service,
        "assign",
        lambda root, principal, *, confirmed: {"applied": confirmed},
    )
    return TestClient(create_app())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_an_authenticated_but_ungranted_principal_is_refused(client, platform, grants):
    """The confused-deputy assertion. The credential is perfectly valid; the
    platform is perfectly happy; AICC still says no."""
    platform.issue("t", "some-service-account")
    grants({"operator:one": ["dispatch:policy:update"]})

    response = client.put(_POLICY, json={"changes": {}}, headers=_auth("t"))

    assert response.status_code == 403
    assert platform.calls == ["t"], "it authenticated fine — that is the point"


def test_an_empty_grant_map_denies_everyone(client, platform, grants):
    """Deny by default, including the default of having configured nothing."""
    platform.issue("t", "operator:one")
    grants({})

    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t")).status_code == 403


def test_no_grant_configuration_at_all_denies_everyone(
    client, platform, monkeypatch
):
    platform.issue("t", "operator:one")
    monkeypatch.delenv(authz.GRANTS_FILE_ENV, raising=False)
    authz.reset_grants_cache()

    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t")).status_code == 403


def test_a_grant_is_per_operation_not_per_principal(client, platform, grants):
    """Being allowed to assign work is not being allowed to rewrite the policy
    that governs assignment."""
    platform.issue("t", "control-plane")
    grants({"control-plane": ["dispatch:assign"]})

    assert client.post(_ASSIGN, json={"confirmed": True}, headers=_auth("t")).status_code == 200
    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t")).status_code == 403


def test_the_platform_capabilities_do_not_grant_anything_here(
    client, platform, grants
):
    """A principal carrying every platform-global capability still gets nothing
    at AICC's boundary: none of them names AICC."""
    platform.issue(
        "t",
        "operator:one",
        capabilities=(
            "global:memory:read",
            "global:memory:write",
            "operator:read",
            "operator:repair",
        ),
    )
    grants({})

    assert client.put(_POLICY, json={"changes": {}}, headers=_auth("t")).status_code == 403


# ---------------------------------------------------------------------------
# The grant map itself
# ---------------------------------------------------------------------------


def test_an_unknown_operation_name_is_an_error_not_a_denial():
    """A misspelled guard must fail at the call site.

    Returning ``False`` would turn the typo into a permanently denying check
    that looks like it works; returning ``True`` is worse. Neither is
    acceptable for an access-control primitive.
    """
    with pytest.raises(authz.UnknownOperationError):
        authz.is_permitted("operator:one", "dispatch:polciy:update")


def test_a_grant_file_naming_an_unknown_operation_is_refused(monkeypatch, tmp_path):
    """A stale operation name left by a rename is a guard that quietly stopped
    applying. It must stop the deploy, not be skipped."""
    path = tmp_path / "grants.json"
    path.write_text(json.dumps({"operator:one": ["dispatch:retired"]}), encoding="utf-8")
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
    authz.reset_grants_cache()

    with pytest.raises(authz.GrantsConfigurationError) as caught:
        authz.load_grants()
    assert "dispatch:retired" in str(caught.value)
    authz.reset_grants_cache()


@pytest.mark.parametrize(
    "body", ['["operator:one"]', '{"operator:one": "dispatch:assign"}', '{"": []}', "{"]
)
def test_a_grant_file_that_cannot_be_trusted_is_refused(monkeypatch, tmp_path, body):
    path = tmp_path / "grants.json"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
    authz.reset_grants_cache()

    with pytest.raises(authz.GrantsConfigurationError):
        authz.load_grants()
    authz.reset_grants_cache()


def test_an_absent_grant_file_is_an_empty_map_not_an_error(monkeypatch, tmp_path):
    """A path that does not exist yet is a deployment that has granted nobody
    anything — which denies everyone. It is not a reason to refuse to boot."""
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(tmp_path / "absent.json"))
    authz.reset_grants_cache()

    assert dict(authz.load_grants()) == {}
    assert authz.is_permitted("operator:one", "dispatch:assign") is False
    authz.reset_grants_cache()


def test_editing_the_grant_file_takes_effect_without_a_restart(monkeypatch, tmp_path):
    """The map is memoised per (path, mtime), so revoking a grant does not
    require a deploy — the same property the no-cache authentication path has."""
    path = tmp_path / "grants.json"
    path.write_text(json.dumps({"operator:one": ["dispatch:assign"]}), encoding="utf-8")
    monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
    authz.reset_grants_cache()
    assert authz.is_permitted("operator:one", "dispatch:assign") is True

    import os

    path.write_text(json.dumps({"operator:one": []}), encoding="utf-8")
    os.utime(path, (0, 0))  # force a distinct mtime rather than racing the clock

    assert authz.is_permitted("operator:one", "dispatch:assign") is False
    authz.reset_grants_cache()
