"""The fleet's GitHub App token minter (VOYN-W0-AICC-ISOLATED-WORKER-NEEDS-READ-ONLY-GIT-ACCESS)."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import stat
import urllib.error
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


def test_the_control_ticks_scopes_are_requested_on_top_of_the_lane_set(tmp_path, monkeypatch):
    """VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS: the ticks read a
    head's check rollup and commit statuses and rerun a cancelled required
    check, so the token asks for those scopes as well as the lanes' git
    ones."""
    module = _module()
    monkeypatch.setattr(module, "_openssl_sign", lambda pem, payload: b"s")
    seen = {}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        seen["body"] = json.loads(request.data)
        return _Response(json.dumps({"token": "ghs_abc", "expires_at": "x"}).encode())

    module.mint(
        "1", "2", tmp_path / "k.pem", repositories=("aios",),
        permissions=module.CONTROL_PERMISSIONS, opener=opener,
    )
    assert seen["body"]["permissions"] == {
        "contents": "write", "pull_requests": "write", "metadata": "read",
        "checks": "read", "statuses": "read", "actions": "write",
    }
    assert module.DEFAULT_PERMISSIONS.items() <= module.CONTROL_PERMISSIONS.items(), (
        "the control scopes are additive; the lanes' git access is unchanged"
    )


def test_an_ungranted_extra_permission_falls_back_to_the_lane_set(tmp_path, monkeypatch):
    """GitHub refuses the WHOLE token (422) when it is asked for a permission
    the installation was never granted. Without a fallback that would leave
    the fleet with no token at all -- no git fetch, no publish, no ticks --
    the moment this asked for a scope the owner has not approved yet."""
    module = _module()
    monkeypatch.setattr(module, "_openssl_sign", lambda pem, payload: b"s")
    requested = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        body = json.loads(request.data)
        requested.append(body["permissions"])
        if "checks" in body["permissions"]:
            raise urllib.error.HTTPError(
                request.full_url, 422, "Unprocessable Entity", {}, None
            )
        return _Response(json.dumps({"token": "ghs_ok", "expires_at": "x"}).encode())

    document = module.mint(
        "1", "2", tmp_path / "k.pem", repositories=("aios",),
        permissions=module.CONTROL_PERMISSIONS,
        fallback_permissions=module.DEFAULT_PERMISSIONS,
        opener=opener,
    )
    assert document["token"] == "ghs_ok"
    assert requested == [module.CONTROL_PERMISSIONS, module.DEFAULT_PERMISSIONS]


def test_a_refusal_that_is_not_about_permissions_is_not_retried(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_openssl_sign", lambda pem, payload: b"s")
    attempts = []

    def opener(request, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        module.mint(
            "1", "2", tmp_path / "k.pem", repositories=("aios",),
            permissions=module.CONTROL_PERMISSIONS,
            fallback_permissions=module.DEFAULT_PERMISSIONS,
            opener=opener,
        )
    assert len(attempts) == 1, "a bad JWT is not fixed by asking for less"


def test_store_writes_token_expiry_and_gh_hosts_lane_readable_and_atomic(tmp_path):
    module = _module()
    root = tmp_path / "github"
    module.store({"token": "ghs_secret", "expires_at": "2026-09-09T13:00:00Z"}, root, chown=False)
    assert (root / "token").read_text() == "ghs_secret\n"
    assert stat.S_IMODE((root / "token").stat().st_mode) == 0o640
    assert (root / "expires_at").read_text().strip() == "2026-09-09T13:00:00Z"
    hosts = (root / "gh" / "hosts.yml").read_text()
    assert "oauth_token: ghs_secret" in hosts and "git_protocol: https" in hosts
    # Current gh layout (users: map) plus config.yml, so gh never tries to
    # migrate/rewrite inside the read-only lane.
    assert "    users:\n        voyn-aicc-fleet[bot]:\n            oauth_token: ghs_secret" in hosts
    assert (root / "gh" / "config.yml").read_text().startswith("version: 1\n")
    assert stat.S_IMODE((root / "gh" / "hosts.yml").stat().st_mode) == 0o640
    assert not list(root.glob("*.tmp")) and not list((root / "gh").glob("*.tmp"))


def test_store_accepts_the_long_installation_tokens_github_issues(tmp_path):
    """Live tokens are ~400 characters; a 200-char cap in the helper truncated
    the token and every private-repo fetch failed (worker-01 2026-09-09)."""
    module = _module()
    long_token = "ghs_" + "a" * 387
    module.store({"token": long_token, "expires_at": "x"}, tmp_path / "g", chown=False)
    assert (tmp_path / "g" / "token").read_text() == long_token + "\n"
    helper = (Path(__file__).parents[2] / "ops/aicc_git_credential").read_text()
    assert "head -c 1024" in helper and "head -c 200" not in helper


def test_store_refuses_a_token_of_unexpected_shape(tmp_path):
    module = _module()
    for bad in ("", "not-a-token", "ghs_" + "x" * 1100, "ghs_with space"):
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


def test_the_control_ticks_read_the_same_store_for_their_own_quota():
    """VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS: the ticks run as
    `aicc-worker`, which is exactly the group the token store is readable
    by, so the control plane's GitHub identity is this same minted token --
    no second credential, no second store."""
    root = Path(__file__).parents[2]
    gh_access = (root / "command_center/orchestrator/gh_access.py").read_text()
    assert 'DEFAULT_FLEET_CONFIG_DIR = "/var/lib/aicc/github/gh"' in gh_access
    for unit in ("review", "merge", "pr-window"):
        text = (root / f"deploy/systemd/aicc-backlog-{unit}.service").read_text()
        assert "User=aicc-worker" in text, unit
        assert "CacheDirectory=aicc-gh" in text, unit


# --- narrow-purpose token (contents:read on one repository, e.g. voyn-backlog) ---

_TOKEN = "ghs_" + "n" * 36


def _fake_urlopen(module, monkeypatch, *, error=None, calls=None):
    monkeypatch.setattr(module, "_openssl_sign", lambda pem, payload: b"s")

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        body = json.loads(request.data)
        if calls is not None:
            calls.append(body)
        if error is not None:
            raise urllib.error.HTTPError(
                request.full_url, error, "x", {},
                io.BytesIO(json.dumps({"message": "The permissions requested are not granted to this installation."}).encode()),
            )
        return _Response(json.dumps({
            "token": _TOKEN,
            "expires_at": "2026-11-13T13:00:00Z",
            "permissions": body["permissions"],
            "repositories": [{"full_name": f"voyn88/{r}"} for r in body["repositories"]],
        }).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)


def _args(tmp_path, *extra):
    return ["--app-id", "1", "--installation-id", "2", "--pem", str(tmp_path / "k.pem"), *extra]


def test_without_the_flag_the_fleet_request_is_unchanged(tmp_path, monkeypatch):
    module = _module()
    calls = []
    _fake_urlopen(module, monkeypatch, calls=calls)
    stored = []
    monkeypatch.setattr(module, "store", lambda document, root: stored.append(root))
    assert module.main(_args(tmp_path)) == 0
    assert calls == [{"permissions": dict(module.CONTROL_PERMISSIONS), "repositories": list(module.DEFAULT_REPOS)}]
    assert stored == [module.DEFAULT_ROOT]


def test_the_permission_flag_mints_exactly_the_requested_scope_into_an_explicit_0600_file(tmp_path, monkeypatch, capsys):
    module = _module()
    calls = []
    _fake_urlopen(module, monkeypatch, calls=calls)
    monkeypatch.setattr(module, "store", lambda *a, **k: pytest.fail("fleet store must not be written"))
    target = tmp_path / "secret" / "backlog.token"
    rc = module.main(_args(
        tmp_path, "--permission", "contents=read", "--repository", "voyn-backlog", "--token-file", str(target),
    ))
    assert rc == 0
    assert calls == [{"permissions": {"contents": "read"}, "repositories": ["voyn-backlog"]}]
    assert target.read_text() == _TOKEN + "\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert not list(target.parent.glob("*.tmp"))
    out = capsys.readouterr()
    assert _TOKEN not in out.out + out.err
    assert json.loads(out.out)["repositories"] == ["voyn88/voyn-backlog"]


def test_a_narrow_token_needs_explicit_repositories_and_output_path(tmp_path, monkeypatch, capsys):
    module = _module()
    calls = []
    _fake_urlopen(module, monkeypatch, calls=calls)
    assert module.main(_args(tmp_path, "--permission", "contents=read", "--token-file", str(tmp_path / "t"))) == 2
    assert module.main(_args(tmp_path, "--permission", "contents=read", "--repository", "voyn-backlog")) == 2
    assert module.main(_args(tmp_path, "--token-file", str(tmp_path / "t"))) == 2
    assert calls == []
    assert not (tmp_path / "t").exists()


@pytest.mark.parametrize("bad", ["contents", "contents=", "contents=owner", "Contents=read", "=read", "a b=read"])
def test_malformed_permissions_are_a_usage_error(bad, tmp_path, monkeypatch):
    module = _module()
    calls = []
    _fake_urlopen(module, monkeypatch, calls=calls)
    rc = module.main(_args(tmp_path, "--permission", bad, "--repository", "r", "--token-file", str(tmp_path / "t")))
    assert rc == 2 and calls == []


def test_the_narrow_token_can_never_be_written_into_the_fleet_store(tmp_path, monkeypatch):
    module = _module()
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    link = tmp_path / "link"
    link.symlink_to(fleet)
    _fake_urlopen(module, monkeypatch)
    for target in (fleet / "token", fleet / "sub" / "t", link / "token"):
        rc = module.main(_args(
            tmp_path, "--root", str(fleet), "--permission", "contents=read",
            "--repository", "voyn-backlog", "--token-file", str(target),
        ))
        assert rc == 2
    assert list(fleet.iterdir()) == []
    with pytest.raises(ValueError):
        module.store_narrow({"token": _TOKEN}, module.DEFAULT_ROOT / "backlog.token")
    with pytest.raises(ValueError):
        module.store_narrow({"token": _TOKEN}, Path("relative.token"))


@pytest.mark.parametrize("code", [403, 422])
def test_asking_for_more_than_the_app_has_is_a_clear_error_and_not_retried(code, tmp_path, monkeypatch, capsys):
    module = _module()
    calls = []
    _fake_urlopen(module, monkeypatch, error=code, calls=calls)
    target = tmp_path / "t"
    rc = module.main(_args(
        tmp_path, "--permission", "contents=read", "--permission", "administration=write",
        "--repository", "voyn-backlog", "--token-file", str(target),
    ))
    err = capsys.readouterr().err
    assert rc == 3
    assert len(calls) == 1  # no fallback to a broader or different set
    assert f"HTTP {code}" in err and "not granted" in err and "voyn-backlog" in err
    assert not target.exists()


def test_an_existing_narrow_token_file_is_replaced_not_widened(tmp_path):
    module = _module()
    target = tmp_path / "t"
    target.write_text("old")
    target.chmod(0o644)
    module.store_narrow({"token": _TOKEN}, target)
    assert target.read_text() == _TOKEN + "\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
