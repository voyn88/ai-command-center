"""github_app_auth: JWT minting (via openssl, no JWT/crypto dependency) and
installation-token exchange, in isolation from review_merge.py's use of it."""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from command_center.orchestrator import github_app_auth


@pytest.fixture
def test_key(tmp_path):
    """A throwaway 2048-bit RSA key, generated once per test via the same
    `openssl` binary the module signs with -- not vendored, so this proves
    the real signing path, not a mock of it."""
    key_path = tmp_path / "test-app-key.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(key_path), "2048"],
        capture_output=True, check=True,
    )
    return key_path


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def test_mint_app_jwt_has_three_dot_separated_segments_and_a_valid_signature(test_key):
    creds = github_app_auth.GitHubAppCredentials("12345", "999", test_key)
    jwt = github_app_auth._mint_app_jwt(creds)
    parts = jwt.split(".")
    assert len(parts) == 3

    header = json.loads(_b64url_decode(parts[0]))
    assert header == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(_b64url_decode(parts[1]))
    assert payload["iss"] == "12345"
    assert payload["exp"] > payload["iat"]

    # The signature must actually verify against the same key's public half
    # -- proves _sign_rs256 produced a real RS256 signature, not junk bytes
    # that merely happen to be the right length.
    pub = subprocess.run(
        ["openssl", "rsa", "-in", str(test_key), "-pubout"],
        capture_output=True, check=True,
    ).stdout
    pub_path = test_key.parent / "pub.pem"
    pub_path.write_bytes(pub)
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    sig_path = test_key.parent / "sig.bin"
    sig_path.write_bytes(_b64url_decode(parts[2]))
    verify = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub_path), "-signature", str(sig_path)],
        input=signing_input, capture_output=True,
    )
    assert verify.returncode == 0, verify.stderr


def test_sign_rs256_raises_apiautherror_on_a_bad_key_path(tmp_path):
    creds = github_app_auth.GitHubAppCredentials("1", "2", tmp_path / "does-not-exist.pem")
    with pytest.raises(github_app_auth.AppAuthError):
        github_app_auth._mint_app_jwt(creds)


def test_installation_token_posts_the_jwt_and_returns_the_token(test_key, monkeypatch):
    creds = github_app_auth.GitHubAppCredentials("12345", "999", test_key)
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"token": "ghs_fake_token", "expires_at": "2026-01-01T00:00:00Z"}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse()

    monkeypatch.setattr(github_app_auth.urllib.request, "urlopen", fake_urlopen)
    token = github_app_auth.installation_token(creds)
    assert token == "ghs_fake_token"
    assert captured["url"] == "https://api.github.com/app/installations/999/access_tokens"
    assert captured["method"] == "POST"
    assert captured["auth"].startswith("Bearer ")


def test_installation_token_raises_on_a_missing_token_field(test_key, monkeypatch):
    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"message": "Bad credentials"}).encode()

    monkeypatch.setattr(
        github_app_auth.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse()
    )
    creds = github_app_auth.GitHubAppCredentials("1", "2", test_key)
    with pytest.raises(github_app_auth.AppAuthError):
        github_app_auth.installation_token(creds)


def test_installation_token_wraps_a_network_error(test_key, monkeypatch):
    import urllib.error

    def raise_it(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(github_app_auth.urllib.request, "urlopen", raise_it)
    creds = github_app_auth.GitHubAppCredentials("1", "2", test_key)
    with pytest.raises(github_app_auth.AppAuthError):
        github_app_auth.installation_token(creds)
