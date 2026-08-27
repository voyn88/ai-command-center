"""The authenticated request, exercised against a real loopback ingest.

A fake transport would prove that the code calls the function it calls. These
tests run a genuine HTTP server so the assertions are about what actually
travels: which header, on which request, after which failure -- and, for the
redirect case, that a second server receives nothing at all.

Plaintext http is used throughout because the endpoint is 127.0.0.1, which is
the one exemption `command_center.otlp.config` grants and the topology the
deployed host really uses (an SSH tunnel terminating on loopback).
"""

from __future__ import annotations

import http.server
import os
import threading

import pytest

from command_center.otlp.config import ENDPOINT_ENV, TOKEN_FILE_ENV, load_config
from command_center.otlp.credential import CredentialError
from command_center.otlp.transport import (
    OtlpAuthRejected,
    OtlpIngestClient,
    OtlpIngestRefused,
    OtlpIngestUnavailable,
)

TOKEN = "s3cr3t-otlp-token-value"
PAYLOAD = b'{"resourceSpans":[]}'


class _Request:
    __slots__ = ("method", "path", "headers", "body")

    def __init__(self, method, path, headers, body):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name, default)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        ingest = self.server.ingest
        record = _Request("POST", self.path, dict(self.headers), body)
        ingest.received.append(record)

        status, payload, extra = ingest.responder(record)
        self.send_response(status)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep pytest output clean
        pass


class _Ingest:
    """A one-endpoint OTLP collector stand-in, listening on loopback."""

    def __init__(self):
        self.received: list[_Request] = []
        self.responder = lambda request: (200, b"{}", {})
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.ingest = self
        # poll_interval is the granularity `shutdown()` waits on; the 0.5s
        # default would put half a second into every teardown in this file.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def ingest():
    server = _Ingest()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def token_path(tmp_path):
    path = tmp_path / "otlp.token"
    path.write_text(TOKEN, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def client(ingest, token_path):
    config = load_config(
        {ENDPOINT_ENV: ingest.url, TOKEN_FILE_ENV: str(token_path)}
    )
    assert config is not None
    return OtlpIngestClient(config)


def rotate(path, value):
    """Publish a new token the way an operator must: atomic replace."""
    replacement = path.with_suffix(".new")
    replacement.write_text(value, encoding="utf-8")
    os.chmod(replacement, 0o600)
    os.replace(replacement, path)


# -- the credential travels ----------------------------------------------


def test_the_request_carries_the_bearer_token(client, ingest):
    assert client.send("traces", PAYLOAD) == 200

    assert len(ingest.received) == 1
    request = ingest.received[0]
    assert request.header("Authorization") == f"Bearer {TOKEN}"
    assert request.method == "POST"
    assert request.path == "/v1/traces"
    assert request.body == PAYLOAD
    assert request.header("Content-Type") == "application/json"
    assert request.header("User-Agent") == "aicc-otlp/1"


@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_every_signal_posts_to_its_own_path(client, ingest, signal):
    client.send(signal, PAYLOAD)
    assert ingest.received[-1].path == f"/v1/{signal}"


def test_a_rotated_credential_is_used_without_a_restart(client, ingest, token_path):
    client.send("traces", PAYLOAD)
    assert ingest.received[-1].header("Authorization") == f"Bearer {TOKEN}"

    rotate(token_path, "second-token-value")
    client.send("traces", PAYLOAD)
    assert ingest.received[-1].header("Authorization") == "Bearer second-token-value"


def test_the_payload_must_already_be_encoded(client):
    with pytest.raises(TypeError, match="already-encoded bytes"):
        client.send("traces", '{"resourceSpans":[]}')


def test_an_unknown_signal_never_reaches_the_network(client, ingest):
    from command_center.otlp.config import ConfigError

    with pytest.raises(ConfigError, match="not an OTLP signal"):
        client.send("../../admin", PAYLOAD)
    assert ingest.received == []


# -- a 401 is retried once, against a re-read credential -----------------


def test_a_401_is_retried_once_after_forcing_a_re_read(client, ingest, token_path):
    """The rotation race: the token changed between the read and the verify."""

    def responder(request):
        if request.header("Authorization") == "Bearer rotated-token-value":
            return 200, b"{}", {}
        # Simulate the rotation having completed at the far end already.
        rotate(token_path, "rotated-token-value")
        return 401, b'{"error":"expired credential"}', {}

    ingest.responder = responder

    assert client.send("traces", PAYLOAD) == 200
    assert len(ingest.received) == 2
    assert ingest.received[0].header("Authorization") == f"Bearer {TOKEN}"
    assert ingest.received[1].header("Authorization") == "Bearer rotated-token-value"


@pytest.mark.parametrize("status", [401, 403])
def test_a_persistent_rejection_raises_loudly_and_is_not_retried_again(
    client, ingest, status
):
    ingest.responder = lambda request: (status, b'{"error":"nope"}', {})

    with pytest.raises(OtlpAuthRejected) as caught:
        client.send("traces", PAYLOAD)

    assert len(ingest.received) == 2, "exactly one retry, then stop"
    message = str(caught.value)
    assert str(status) in message
    assert "not being delivered" in message


def test_a_rejected_credential_is_not_a_transient_failure(client, ingest):
    """A caller must be able to tell 'fix me' from 'try again later'."""
    ingest.responder = lambda request: (401, b"", {})
    with pytest.raises(OtlpAuthRejected) as caught:
        client.send("traces", PAYLOAD)
    assert not isinstance(caught.value, OtlpIngestUnavailable)
    assert not isinstance(caught.value, OtlpIngestRefused)


def test_a_credential_that_breaks_during_the_retry_reports_that_fault(
    client, ingest, token_path
):
    """The unreadable file is the real fault, not the 401 it caused."""

    def responder(request):
        token_path.unlink(missing_ok=True)
        return 401, b"", {}

    ingest.responder = responder
    with pytest.raises(CredentialError, match="cannot be read"):
        client.send("traces", PAYLOAD)


# -- everything else the ingest can answer --------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_transient_status_is_unavailable_and_is_not_retried(client, ingest, status):
    ingest.responder = lambda request: (status, b"slow down", {})
    with pytest.raises(OtlpIngestUnavailable) as caught:
        client.send("traces", PAYLOAD)
    assert len(ingest.received) == 1, "retrying is the caller's decision"
    assert str(status) in str(caught.value)


@pytest.mark.parametrize("status", [400, 404, 413, 415])
def test_a_payload_fault_is_refused_and_is_not_retried(client, ingest, status):
    ingest.responder = lambda request: (status, b"bad payload", {})
    with pytest.raises(OtlpIngestRefused) as caught:
        client.send("traces", PAYLOAD)
    assert len(ingest.received) == 1
    assert str(status) in str(caught.value)


def test_an_unreachable_ingest_is_unavailable(token_path, ingest):
    url = ingest.url
    ingest.close()
    config = load_config({ENDPOINT_ENV: url, TOKEN_FILE_ENV: str(token_path)})
    assert config is not None
    with pytest.raises(OtlpIngestUnavailable, match="unreachable"):
        OtlpIngestClient(config).send("traces", PAYLOAD)


# -- the two disclosure paths --------------------------------------------


def test_a_redirect_is_refused_and_the_target_never_sees_the_token(client, ingest):
    """Following it would hand the bearer token to whatever Location names."""
    elsewhere = _Ingest()
    try:
        ingest.responder = lambda request: (
            302,
            b"",
            {"Location": f"{elsewhere.url}/v1/traces"},
        )

        with pytest.raises(OtlpIngestRefused) as caught:
            client.send("traces", PAYLOAD)

        assert elsewhere.received == [], "the redirect target received nothing"
        assert "Redirects are never followed" in str(caught.value)
    finally:
        elsewhere.close()


def test_an_echoed_token_is_redacted_out_of_the_exception(client, ingest):
    """An ingest that reflects request headers must not seed AICC's own logs."""
    ingest.responder = lambda request: (
        400,
        f'{{"error":"saw {request.header("Authorization")}"}}'.encode(),
        {},
    )

    with pytest.raises(OtlpIngestRefused) as caught:
        client.send("traces", PAYLOAD)

    message = str(caught.value)
    assert TOKEN not in message
    assert "<redacted>" in message


def test_an_echoed_token_is_redacted_out_of_an_auth_rejection(client, ingest):
    ingest.responder = lambda request: (
        401,
        f'seen={request.header("Authorization")}'.encode(),
        {},
    )
    with pytest.raises(OtlpAuthRejected) as caught:
        client.send("traces", PAYLOAD)
    assert TOKEN not in str(caught.value)


def test_a_huge_error_body_is_truncated(client, ingest):
    ingest.responder = lambda request: (400, b"x" * 100_000, {})
    with pytest.raises(OtlpIngestRefused) as caught:
        client.send("traces", PAYLOAD)
    assert len(str(caught.value)) < 2000


# -- no ambient configuration --------------------------------------------


def test_an_inherited_proxy_variable_cannot_reroute_the_telemetry(
    monkeypatch, ingest, token_path
):
    """urllib's default opener would honour these; this one registers none."""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    config = load_config({ENDPOINT_ENV: ingest.url, TOKEN_FILE_ENV: str(token_path)})
    assert config is not None
    assert OtlpIngestClient(config).send("traces", PAYLOAD) == 200
    assert len(ingest.received) == 1
