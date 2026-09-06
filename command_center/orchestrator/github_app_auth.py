"""GitHub App authentication for the independent acceptance identity.

Why this exists
----------------
`review_merge.py`'s marker was, until this module, posted under the SAME
ambient `gh` credential that authors the pull request and later merges it --
a self-issued acceptance, indistinguishable at the API level from no review
at all, live-confirmed on PRs #354/#355 (both merged by the account that
also posted their own marker). `.github/workflows/acceptance-gate.yml` and
`scripts/assert_independent_acceptance.py` already specify the correct fix
-- a GitHub App reviewing under its own `login`, immune to GitHub's own
self-approval refusal because it never authors anything -- but the App's
installation did not survive the 2026-08-20 org migration and was never
reconnected until now (`voyn88-acceptance-gate`, app id 4685414,
installation id 155776352, live-verified 2026-08-22: posts as
`voyn88-acceptance-gate[bot]`).

This module mints that identity's short-lived installation tokens so
`review_merge.py` can post the marker AS the bot -- the same identity
`assert_independent_acceptance.py` already checks for -- closing the gap
without inventing a second acceptance mechanism.

Why `openssl` and not a JWT library
------------------------------------
No JWT/crypto library is a dependency of this project (grep of
requirements*.txt turns up neither PyJWT nor `cryptography`), and hand-rolled
RSA-PKCS1v15 signing in pure Python is exactly the kind of primitive that
should never be reimplemented. `openssl dgst -sha256 -sign` is already
present on every target host (verified: server, CI) and performs the
identical RSASSA-PKCS1-v1_5 signature GitHub's JWT verification expects --
this module only assembles the JWT's own non-cryptographic parts (base64url
header/payload) around that one subprocess call, the same shell-out
discipline `review_merge.py`'s own `_gh()` already uses throughout.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["GitHubAppCredentials", "AppAuthError", "installation_token"]

_TOKEN_URL = "https://api.github.com/app/installations/{installation_id}/access_tokens"
_API_VERSION = "2022-11-28"


class AppAuthError(RuntimeError):
    """Minting or exchanging the App's JWT for an installation token failed."""


class GitHubAppCredentials:
    """The three values that identify one GitHub App installation.

    Read from `VOYN_ACCEPTANCE_APP_ID` / `VOYN_ACCEPTANCE_INSTALLATION_ID` /
    `VOYN_ACCEPTANCE_PRIVATE_KEY_PATH` by the caller, not this class -- this
    module only knows how to use credentials, not where an operator keeps
    them, matching `lease_client.LeaseIdentity`'s separation.
    """

    __slots__ = ("app_id", "installation_id", "private_key_path")

    def __init__(self, app_id: str, installation_id: str, private_key_path: Path) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_path = private_key_path


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256(signing_input: bytes, private_key_path: Path) -> bytes:
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
        input=signing_input, capture_output=True, timeout=15, check=False,
    )
    if proc.returncode != 0:
        raise AppAuthError(f"openssl signing failed: {proc.stderr.decode()[:200]}")
    return proc.stdout


def _mint_app_jwt(creds: GitHubAppCredentials) -> str:
    # `iat` a minute in the past absorbs clock skew between this host and
    # GitHub's; `exp` well inside GitHub's own 10-minute ceiling for App
    # JWTs (never used past the single token-exchange call below anyway).
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": creds.app_id}).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(_sign_rs256(signing_input, creds.private_key_path))
    return f"{header}.{payload}.{signature}"


def installation_token(creds: GitHubAppCredentials) -> str:
    """A short-lived (~1h) installation access token, minted fresh on every
    call -- this module deliberately does not cache one across the
    once-per-tick callers in `review_merge.py`, since a token's whole
    lifetime is a handful of HTTP calls immediately after minting it."""
    jwt = _mint_app_jwt(creds)
    req = urllib.request.Request(
        _TOKEN_URL.format(installation_id=creds.installation_id),
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise AppAuthError(f"installation token exchange failed: {exc}") from exc
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise AppAuthError("installation token exchange returned no token")
    return token
