"""The fleet's GitHub App token minter (VOYN-W0-AICC-ISOLATED-WORKER-NEEDS-READ-ONLY-GIT-ACCESS)."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import stat
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_github_app_token.py"
    spec = importlib.util.spec_from_file_location("aicc_github_app_token", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decode(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def test_app_jwt_has_rs256_header_skewed_iat_and_nine_minute_life(tmp_path):
    module = _module()
    signed = []

    def fake_sign(pem, payload):
        signed.append((pem, payload))
        return b"sig"

    token = module.app_jwt("4884422", tmp_path / "k.pem", now=1_000_000, sign=fake_sign)
    header, claims, signature = token.split(".")
    assert _decode(header) == {"alg": "RS256", "typ": "JWT"}
    assert _decode(claims) == {"iat": 999_940, "exp": 1_000_540, "iss": "4884422"}
    assert signature == base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    assert signed[0][1] == f"{header}.{claims}".encode()


def test_mint_narrows_the_token_to_the_fleet_repositories_and_permissions(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_openssl_sign", lambda pem, payload: b"s")
    seen = {}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["auth"] = request.get_header("Authorization")
        return _Response(json.dumps({"token": "ghs_abc", "expires_at": "2026-09-09T13:00:00Z"}).encode())

    out = module.mint(
        "4884422", "160309192", tmp_path / "k.pem",
        repositories=("ai-command-center", "aios"), permissions={"contents": "write"}, opener=opener,
    )
    assert out["token"] == "ghs_abc"
    assert seen["url"].endswith("/app/installations/160309192/access_tokens")
    assert seen["body"] == {"permissions": {"contents": "write"}, "repositories": ["ai-command-center", "aios"]}
    assert seen["auth"].startswith("Bearer ")


def test_store_writes_token_expiry_and_gh_hosts_lane_readable_and_atomic(tmp_path):
    module = _module()
    root = tmp_path / "github"
    module.store({"token": "ghs_secret", "expires_at": "2026-09-09T13:00:00Z"}, root, chown=False)
    assert (root / "token").read_text() == "ghs_secret\n"
    assert stat.S_IMODE((root / "token").stat().st_mode) == 0o640
    assert (root / "expires_at").read_text().strip() == "2026-09-09T13:00:00Z"
    hosts = (root / "gh" / "hosts.yml").read_text()
    assert "oauth_token: ghs_secret" in hosts and "git_protocol: https" in hosts
    assert stat.S_IMODE((root / "gh" / "hosts.yml").stat().st_mode) == 0o640
    assert not list(root.glob("*.tmp")) and not list((root / "gh").glob("*.tmp"))


def test_store_refuses_a_token_of_unexpected_shape(tmp_path):
    module = _module()
    for bad in ("", "not-a-token", "ghs_" + "x" * 300, "ghs_with space"):
        with pytest.raises(ValueError):
            module.store({"token": bad, "expires_at": "x"}, tmp_path / "g", chown=False)
    assert not (tmp_path / "g" / "token").exists()


def test_the_lane_contract_is_wired_end_to_end():
    """gitconfig -> helper, helper reads the store the timer writes, the worker
    template points gh at the lane-only config dir, tmpfiles creates the store
    lane-readable, the transaction installs every piece, and the AGENT's
    GH_CONFIG_DIR stays elsewhere so model code never holds the token."""
    root = Path(__file__).parents[2]
    gitconfig = (root / "deploy/aicc/gitconfig").read_text()
    assert "helper = /usr/local/libexec/aicc-git-credential" in gitconfig
    assert "insteadOf = git@github.com:" in gitconfig
    helper = (root / "ops/aicc_git_credential").read_text()
    assert "/var/lib/aicc/github/token" in helper and "username=x-access-token" in helper
    template = (root / "deploy/systemd/voyn-aicc-worker@.service").read_text()
    assert "Environment=GH_CONFIG_DIR=/var/lib/aicc/github/gh" in template
    assert "Environment=GIT_TERMINAL_PROMPT=0" in template
    tmpfiles = (root / "deploy/tmpfiles.d/aicc-agent.conf").read_text()
    assert "d /var/lib/aicc/github 0750 root aicc-worker -" in tmpfiles
    assert "d /var/lib/aicc/github/gh 0750 root aicc-worker -" in tmpfiles
    transaction = (root / "ops/aicc_install_transaction.py").read_text()
    for target in (
        "/usr/local/sbin/aicc-github-app-token",
        "/usr/local/libexec/aicc-git-credential",
        "/etc/systemd/system/voyn-aicc-github-token.timer",
        "/etc/aicc/github-app.env",
    ):
        assert target in transaction, target
    agent_runner = (root / "command_center/agent_runner.py").read_text()
    assert '"GH_CONFIG_DIR": "/nonexistent/aicc-agent-gh"' in agent_runner
