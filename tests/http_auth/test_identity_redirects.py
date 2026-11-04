"""Exercise the real urllib transport: a stub at _http_get_json hides leaks."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from command_center.http_auth import identity


@contextmanager
def endpoint(status=200, location=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(status)
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(b'{"data":{"principal_id":"test","tenant_id":"test"}}')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_cross_origin_redirect_never_receives_bearer(status, monkeypatch):
    # Different loopback ports are different origins. No real credential is used.
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with endpoint() as (other, leaked), endpoint(status, other + "/stolen") as (base, sent):
        monkeypatch.setenv(identity.PLATFORM_URL_ENV, base)
        with pytest.raises(identity.PlatformUnavailable, match=f"whoami returned {status}"):
            identity._whoami("dummy-redirect-token")
        assert sent == [(identity.WHOAMI_PATH, "Bearer dummy-redirect-token")]
        assert leaked == []


def test_relative_redirect_is_not_an_identity_response(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with endpoint(302, "/another-path") as (base, requests):
        monkeypatch.setenv(identity.PLATFORM_URL_ENV, base)
        with pytest.raises(identity.PlatformUnavailable, match="whoami returned 302"):
            identity._whoami("dummy-redirect-token")
        assert len(requests) == 1


@pytest.mark.parametrize("status", [200, 401])
def test_direct_identity_response_keeps_authentication_semantics(status, monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with endpoint(status) as (base, requests):
        monkeypatch.setenv(identity.PLATFORM_URL_ENV, base)
        principal = identity._whoami("dummy-direct-token")
        assert (principal.principal_id if principal else None) == ("test" if status == 200 else None)
        assert requests == [(identity.WHOAMI_PATH, "Bearer dummy-direct-token")]
