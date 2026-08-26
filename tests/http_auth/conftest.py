"""A platform double, wired in at the transport seam rather than above it.

The double answers ``GET /api/v1/whoami`` the way the real identity authority
does: a bearer credential it knows resolves to a principal id, a tenant and a
list of *platform-global* capabilities — and nothing AICC-specific, no audience
field. Withholding those is deliberate. It is what makes the confused-deputy
test meaningful: a 200 here must not be readable as permission to write to
AICC.

The seam is ``identity._http_get_json``, i.e. one function above the socket, so
everything above it — status interpretation, the 401/503 split, JSON parsing,
the absence of any cache — is the real code under test. Tests that need to
prove behaviour on a transport failure raise from the double exactly as the
real transport would.
"""

from __future__ import annotations

import json

import pytest

from command_center.http_auth import authz, identity


class PlatformDouble:
    """A stand-in for the identity authority. Every field of its behaviour is a knob."""

    def __init__(self) -> None:
        self.credentials: dict[str, dict] = {}
        #: Set to a status code to answer with it regardless of the credential.
        self.force_status: int | None = None
        #: Set to an exception to raise from the transport (an outage).
        self.transport_error: Exception | None = None
        #: Every token this double was asked about, in order. The evidence for
        #: "the write path does not cache": a second request must produce a
        #: second call.
        self.calls: list[str] = []
        #: A body that is not the documented shape, for the parse-failure test.
        self.malformed_body: bytes | None = None

    def issue(
        self,
        token: str,
        principal_id: str,
        *,
        tenant_id: str = "tenant-1",
        capabilities: tuple[str, ...] = ("operator:read",),
    ) -> str:
        self.credentials[token] = {
            "principal_id": principal_id,
            "tenant_id": tenant_id,
            "capabilities": list(capabilities),
        }
        return token

    def revoke(self, token: str) -> None:
        self.credentials.pop(token, None)

    def get(self, url: str, token: str, timeout: float) -> tuple[int, bytes]:
        self.calls.append(token)
        if self.transport_error is not None:
            raise self.transport_error
        if self.force_status is not None:
            return self.force_status, b'{"error":"upstream"}'
        if self.malformed_body is not None:
            return 200, self.malformed_body
        record = self.credentials.get(token)
        if record is None:
            # One indistinguishable refusal, exactly like the real authority:
            # unknown / wrong secret / expired / revoked are not told apart.
            return 401, b'{"error":"unauthenticated"}'
        return 200, json.dumps({"data": record}).encode("utf-8")


@pytest.fixture
def platform(monkeypatch):
    double = PlatformDouble()
    monkeypatch.setenv(identity.PLATFORM_URL_ENV, "https://platform.invalid")
    monkeypatch.setattr(identity, "_http_get_json", double.get)
    return double


@pytest.fixture
def grants(monkeypatch, tmp_path):
    """Write a grant map and point the configuration at it."""

    def _write(mapping: dict[str, list[str]]) -> None:
        path = tmp_path / "grants.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        monkeypatch.setenv(authz.GRANTS_FILE_ENV, str(path))
        authz.reset_grants_cache()

    monkeypatch.delenv(authz.GRANTS_FILE_ENV, raising=False)
    authz.reset_grants_cache()
    yield _write
    authz.reset_grants_cache()
