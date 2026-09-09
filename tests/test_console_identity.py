"""The Streamlit console sign-in gate itself (VOYN-W0-AICC-CONSOLE-NO-AUTH).

Every other UI suite runs with ``command_center.ui.console_identity`` bypassed
(``tests/conftest.py::bypass_console_identity_gate``) so it can exercise page
*behavior* without a sign-in step. This file is the one place that switches
the bypass off, via the ``console_identity_gate`` marker, and drives the gate
itself: the login form, the session-scoped cache in
:func:`console_identity.require_identity`, and the re-verify-fresh contract of
:func:`console_identity.require_console_operation`.

The platform is doubled at ``identity._http_get_json`` — one function above
the socket — exactly as ``tests/http_auth/conftest.py`` doubles it for the
HTTP boundary, so everything above that seam (status interpretation, the
401/503 split, the absence of any cache) is the real code under test.
"""

from __future__ import annotations

import json

import pytest

from command_center.http_auth import authz, identity
from command_center.ui import console_identity

pytestmark = pytest.mark.console_identity_gate


class _PlatformDouble:
    def __init__(self) -> None:
        self.credentials: dict[str, dict] = {}
        self.force_status: int | None = None
        self.transport_error: Exception | None = None
        self.calls: list[str] = []

    def issue(self, token: str, principal_id: str, *, tenant_id: str = "tenant-1") -> str:
        self.credentials[token] = {
            "principal_id": principal_id,
            "tenant_id": tenant_id,
            "capabilities": [],
        }
        return token

    def get(self, url: str, token: str, timeout: float) -> tuple[int, bytes]:
        self.calls.append(token)
        if self.transport_error is not None:
            raise self.transport_error
        if self.force_status is not None:
            return self.force_status, b'{"error":"upstream"}'
        record = self.credentials.get(token)
        if record is None:
            return 401, b'{"error":"unauthenticated"}'
        return 200, json.dumps({"data": record}).encode("utf-8")


@pytest.fixture
def platform(monkeypatch):
    double = _PlatformDouble()
    monkeypatch.setenv(identity.PLATFORM_URL_ENV, "https://platform.invalid")
    monkeypatch.setattr(identity, "_http_get_json", double.get)
    return double


@pytest.fixture
def grants(monkeypatch, tmp_path):
    def _write(mapping: dict[str, list[str]]) -> None:
        path = tmp_path / "grants.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
        authz.reset_grants_cache()

    monkeypatch.delenv(authz.GRANTS_FILE_ENV, raising=False)
    authz.reset_grants_cache()
    yield _write
    authz.reset_grants_cache()


def _identity_gate_script() -> None:
    import streamlit as st

    from command_center.ui import console_identity

    principal = console_identity.require_identity(page_title="Test Console", page_icon="🧭")
    st.write(f"signed-in:{principal.principal_id}")


def _console_operation_script() -> None:
    import streamlit as st

    from command_center.ui import console_identity

    principal = console_identity.require_console_operation("console:start_task")
    st.write(f"authorized:{principal.principal_id}")


def _run_identity_gate():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_function(_identity_gate_script, default_timeout=30)


def _run_console_operation(*, token: str, principal: object):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_console_operation_script, default_timeout=30)
    at.session_state[console_identity._TOKEN_KEY] = token
    at.session_state[console_identity._PRINCIPAL_KEY] = principal
    return at


# --- require_identity: the session-scoped sign-in gate ---------------------


def test_signed_out_operator_sees_the_login_form_not_the_app():
    at = _run_identity_gate().run()
    assert not at.exception
    assert any("Войти" in b.label for b in at.button)
    assert not any("signed-in:" in m.value for m in at.markdown)


def test_a_valid_token_signs_in_and_reaches_the_app(platform):
    platform.issue("good-token", "operator:one")
    at = _run_identity_gate().run()
    at.text_input[0].set_value("good-token").run()
    submit = next(b for b in at.button if b.label == "Войти")
    at = submit.click().run()

    assert not at.exception
    assert any("signed-in:operator:one" in m.value for m in at.markdown)
    assert at.session_state[console_identity._TOKEN_KEY] == "good-token"
    assert at.session_state[console_identity._PRINCIPAL_KEY].principal_id == "operator:one"


def test_an_invalid_token_shows_an_error_and_does_not_sign_in(platform):
    at = _run_identity_gate().run()
    at.text_input[0].set_value("not-a-real-token").run()
    submit = next(b for b in at.button if b.label == "Войти")
    at = submit.click().run()

    assert any("Неверный или недействующий" in e.value for e in at.error)
    assert not any("signed-in:" in m.value for m in at.markdown)


def test_platform_outage_fails_closed_at_login(platform):
    platform.transport_error = identity.PlatformUnavailable("platform unreachable")
    at = _run_identity_gate().run()
    at.text_input[0].set_value("any-token").run()
    submit = next(b for b in at.button if b.label == "Войти")
    at = submit.click().run()

    assert any("недоступна" in e.value for e in at.error)
    assert not any("signed-in:" in m.value for m in at.markdown)


def test_an_authenticated_session_skips_the_login_form():
    principal = identity.Principal(principal_id="operator:cached", tenant_id="t", capabilities=())
    at = _run_identity_gate()
    at.session_state[console_identity._TOKEN_KEY] = "cached-token"
    at.session_state[console_identity._PRINCIPAL_KEY] = principal
    at = at.run()

    assert not at.exception
    assert any("signed-in:operator:cached" in m.value for m in at.markdown)
    assert not any("Войти" in b.label for b in at.button)


# --- require_console_operation: re-verified fresh, never cached ------------


def test_require_console_operation_reverifies_live_not_cached(platform, grants):
    platform.issue("op-token", "operator:one")
    grants({"operator:one": ["console:start_task"]})
    principal = identity.Principal(principal_id="operator:one", tenant_id="t", capabilities=())

    _run_console_operation(token="op-token", principal=principal).run()
    _run_console_operation(token="op-token", principal=principal).run()

    assert platform.calls == ["op-token", "op-token"]


def test_require_console_operation_signs_out_on_revoked_token(platform, grants):
    grants({"operator:one": ["console:start_task"]})
    principal = identity.Principal(principal_id="operator:one", tenant_id="t", capabilities=())
    # `platform` has no credential for "revoked-token": whoami answers 401.

    at = _run_console_operation(token="revoked-token", principal=principal).run()

    assert any("Войдите заново" in e.value for e in at.error)
    assert console_identity._TOKEN_KEY not in at.session_state
    assert console_identity._PRINCIPAL_KEY not in at.session_state
    assert not any("authorized:" in m.value for m in at.markdown)


def test_require_console_operation_fails_closed_on_outage(platform, grants):
    grants({"operator:one": ["console:start_task"]})
    principal = identity.Principal(principal_id="operator:one", tenant_id="t", capabilities=())
    platform.transport_error = identity.PlatformUnavailable("platform unreachable")

    at = _run_console_operation(token="op-token", principal=principal).run()

    assert any("недоступна" in e.value for e in at.error)
    # Fail-closed, not signed out: an outage must not force a re-login.
    assert at.session_state[console_identity._TOKEN_KEY] == "op-token"
    assert not any("authorized:" in m.value for m in at.markdown)


def test_require_console_operation_denies_without_grant(platform, grants):
    platform.issue("op-token", "operator:one")
    grants({})
    principal = identity.Principal(principal_id="operator:one", tenant_id="t", capabilities=())

    at = _run_console_operation(token="op-token", principal=principal).run()

    assert any("Недостаточно прав" in e.value for e in at.error)
    assert not any("authorized:" in m.value for m in at.markdown)


def test_require_console_operation_allows_with_grant(platform, grants):
    platform.issue("op-token", "operator:one")
    grants({"operator:one": ["console:start_task"]})
    principal = identity.Principal(principal_id="operator:one", tenant_id="t", capabilities=())

    at = _run_console_operation(token="op-token", principal=principal).run()

    assert not at.exception
    assert any("authorized:operator:one" in m.value for m in at.markdown)
