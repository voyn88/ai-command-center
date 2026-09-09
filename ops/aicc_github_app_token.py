#!/usr/bin/python3
"""Mint a GitHub App installation token for the isolated worker lanes.

VOYN-W0-AICC-ISOLATED-WORKER-NEEDS-READ-ONLY-GIT-ACCESS: under principal
isolation (ADR-0010) the lanes carry no human credential, so ``git fetch``
and the publisher's ``git push``/``gh pr`` had nothing to authenticate with
(worker-01 2026-09-08: every implementation attempt died at
``resolve_remote_base_sha: Permission denied (publickey)``). The owner
registered the GitHub App ``voyn-aicc-fleet`` on org voyn88; this root timer
turns its private key into a short-lived installation token (60 minutes,
refreshed every 30) narrowed to the fleet's repositories, and stores it where
only the worker principal can read it:

- ``/var/lib/aicc/github/token`` (0640 root:aicc-worker) -- read by the git
  credential helper ``aicc-git-credential`` through ``/etc/aicc/gitconfig``;
- ``/var/lib/aicc/github/gh/hosts.yml`` (0640) -- ``gh`` reads it through the
  lane's ``GH_CONFIG_DIR`` (the AGENT's launcher points GH_CONFIG_DIR at a
  nonexistent path, so untrusted model code never sees the token).

Standard library plus ``openssl dgst`` for the RS256 JWT: no new dependency on
a host whose only job is to run the fleet. The private key stays root-only on
the host; rejected alternatives: a long-lived PAT (human credential), a
control-01 pusher over a new root ssh trust (more trust surface).
"""
from __future__ import annotations

import argparse
import base64
import grp
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_PEM = Path("/etc/voyn/secrets/aicc-github-app.pem")
DEFAULT_ROOT = Path("/var/lib/aicc/github")
DEFAULT_REPOS = ("ai-command-center", "aios", "voyn-logistics-crm")
DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
API = "https://api.github.com"
JWT_LIFETIME_SECONDS = 540


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def app_jwt(app_id: str, pem: Path, *, now: int | None = None, sign=None) -> str:
    """RS256 JWT for the App (iat 60s in the past for clock skew, 9-minute life)."""
    now = int(time.time()) if now is None else now
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(
        json.dumps(
            {"iat": now - 60, "exp": now + JWT_LIFETIME_SECONDS, "iss": app_id},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = (sign or _openssl_sign)(pem, signing_input)
    return f"{header}.{claims}.{_b64(signature)}"


def _openssl_sign(pem: Path, payload: bytes) -> bytes:
    return subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(pem)],
        input=payload,
        capture_output=True,
        check=True,
    ).stdout


def mint(app_id: str, installation_id: str, pem: Path, *, repositories, permissions, opener=None) -> dict:
    request = urllib.request.Request(
        f"{API}/app/installations/{installation_id}/access_tokens",
        data=json.dumps({"permissions": dict(permissions), "repositories": list(repositories)}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt(app_id, pem)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aicc-fleet-token-minter",
        },
    )
    with (opener or urllib.request.urlopen)(request, timeout=20) as response:
        return json.load(response)


def store(document: dict, root: Path, *, group: str = "aicc-worker", chown: bool = True) -> None:
    """Write token, expiry and a gh hosts.yml atomically, lane-readable only."""
    token = str(document["token"])
    # Installation tokens run to ~400 characters (a 200-char cap truncated the
    # live token and every private-repo fetch failed, worker-01 2026-09-09).
    if not token.startswith(("ghs_", "ghp_")) or len(token) > 1024 or any(c.isspace() for c in token):
        raise ValueError("installation token has an unexpected shape")
    gid = grp.getgrnam(group).gr_gid if chown else -1
    root.mkdir(mode=0o750, parents=True, exist_ok=True)
    (root / "gh").mkdir(mode=0o750, exist_ok=True)

    def put(path: Path, text: str, mode: int) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        if chown:
            os.chown(tmp, 0, gid)
        os.replace(tmp, path)

    put(root / "token", token + "\n", 0o640)
    put(root / "expires_at", str(document["expires_at"]) + "\n", 0o644)
    put(
        root / "gh" / "hosts.yml",
        "github.com:\n"
        f"    oauth_token: {token}\n"
        "    user: voyn-aicc-fleet[bot]\n"
        "    git_protocol: https\n",
        0o640,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-id", default=os.environ.get("AICC_GITHUB_APP_ID", ""))
    parser.add_argument("--installation-id", default=os.environ.get("AICC_GITHUB_INSTALLATION_ID", ""))
    parser.add_argument("--pem", type=Path, default=Path(os.environ.get("AICC_GITHUB_APP_PEM", str(DEFAULT_PEM))))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--repository", action="append", default=None)
    args = parser.parse_args(argv)
    if not args.app_id or not args.installation_id:
        print("AICC_GITHUB_APP_ID and AICC_GITHUB_INSTALLATION_ID are required", file=sys.stderr)
        return 2
    document = mint(
        args.app_id,
        args.installation_id,
        args.pem,
        repositories=args.repository or DEFAULT_REPOS,
        permissions=DEFAULT_PERMISSIONS,
    )
    store(document, args.root)
    print(json.dumps({
        "expires_at": document.get("expires_at"),
        "permissions": document.get("permissions"),
        "repositories": [r.get("full_name") for r in document.get("repositories", [])],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
