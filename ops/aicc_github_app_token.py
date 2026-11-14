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

The same store is what gives the CONTROL ticks their own GitHub quota
(``command_center/orchestrator/gh_access.py``): they run as ``aicc-worker``
and so can read it, which is what stopped them dying with the operator's own
exhausted GraphQL budget
(VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS). Their extra read scopes
are requested on top of the lane set and degrade to it if ungranted -- see
``CONTROL_PERMISSIONS``.

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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PEM = Path("/etc/voyn/secrets/aicc-github-app.pem")
DEFAULT_ROOT = Path("/var/lib/aicc/github")
DEFAULT_REPOS = ("ai-command-center", "aios", "voyn-logistics-crm")
DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
#: What the CONTROL ticks additionally need once they run under this token
#: instead of a human's (VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS):
#: the check rollup and commit statuses a PR's eligibility is decided from,
#: and the workflow reruns the merge tick asks for on a cancelled or flaky
#: required check. Requested on top of the base set, never instead of it --
#: an installation that was granted only the base set still gets a token
#: (see `mint`), because the worker lanes' git access must not depend on a
#: permission the owner has not approved yet.
CONTROL_PERMISSIONS = {
    **DEFAULT_PERMISSIONS,
    "checks": "read",
    "statuses": "read",
    "actions": "write",
}
API = "https://api.github.com"
JWT_LIFETIME_SECONDS = 540
_PERMISSION_NAME = re.compile(r"^[a-z][a-z_]{0,63}$")
_PERMISSION_LEVELS = ("read", "write", "admin")
#: GitHub adds ``metadata: read`` to every installation token by itself.
_IMPLICIT_PERMISSIONS = {"metadata": "read"}


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


def mint(
    app_id: str,
    installation_id: str,
    pem: Path,
    *,
    repositories,
    permissions,
    fallback_permissions=None,
    opener=None,
) -> dict:
    """One installation token, narrowed to `repositories` and `permissions`.

    GitHub refuses (422) a token that asks for a permission the owner never
    granted the installation, and the refusal is all-or-nothing: one
    ungranted extra would leave the fleet with NO token at all -- no git
    fetch, no publish, no ticks. So an optional `fallback_permissions` set is
    retried once on that refusal, which is what lets this ask for the control
    ticks' extra read scopes without making the lanes' git access hostage to
    an approval that has not happened yet."""
    try:
        return _access_token(app_id, installation_id, pem, repositories, permissions, opener)
    except urllib.error.HTTPError as exc:
        if fallback_permissions is None or exc.code not in (403, 422):
            raise
    return _access_token(
        app_id, installation_id, pem, repositories, fallback_permissions, opener
    )


def _access_token(app_id, installation_id, pem, repositories, permissions, opener) -> dict:
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
    # The store directories themselves must be root:<lane group> 0750 on every
    # host.  A worker gets that from tmpfiles (aicc-agent.conf); a control host
    # does not install that file, so the first mint created them root:root and
    # the lane account (aicc-worker) could not even traverse to its own token
    # (control-01 2026-11-14: beacon "no_token_file").  The directories hold
    # only what the 0640 files already show to the same group.
    for directory in (root, root / "gh"):
        os.chmod(directory, 0o750)
        if chown:
            os.chown(directory, 0, gid)

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
    # gh's CURRENT config layout, written complete: given only the legacy
    # single-token hosts.yml, gh tries to migrate and rewrite the file, which
    # a lane under ProtectSystem=strict cannot ("failed to write config after
    # migration: read-only file system", worker-01 2026-09-09).
    put(root / "gh" / "config.yml", "version: 1\ngit_protocol: https\nprompt: disabled\n", 0o640)
    put(
        root / "gh" / "hosts.yml",
        "github.com:\n"
        "    users:\n"
        "        voyn-aicc-fleet[bot]:\n"
        f"            oauth_token: {token}\n"
        "    git_protocol: https\n"
        "    user: voyn-aicc-fleet[bot]\n"
        f"    oauth_token: {token}\n",
        0o640,
    )


def parse_permissions(items) -> dict:
    """``["contents=read", ...]`` -> ``{"contents": "read"}``; anything odd is a usage error."""
    result: dict = {}
    for item in items:
        name, sep, level = str(item).partition("=")
        if not sep or not _PERMISSION_NAME.match(name) or level not in _PERMISSION_LEVELS:
            raise ValueError(f"--permission expects NAME=read|write|admin, got {item!r}")
        if name in result and result[name] != level:
            raise ValueError(f"--permission {name} given twice with different levels")
        result[name] = level
    return result


def store_narrow(document: dict, path: Path, *, fleet_root: Path = DEFAULT_ROOT) -> None:
    """Write a narrow-purpose token to ``path`` only: one file, 0600, never lane-readable.

    The fleet store (`DEFAULT_ROOT`) is read by the agents, so a token minted for
    one private repository must never land in it: the target may not be inside
    the fleet store (checked on resolved paths, symlinks included). The file is
    created owner-only from the first byte (no chmod window) and replaced
    atomically; when run as root (the only way the App key is readable) the
    owner is root. The directory the file lands in must be owned by the
    caller and closed to group/other writes -- in a writable directory another
    user could swap the temp file before the rename. The consumer must delete
    the file after use; the token lives at most 60 minutes anyway."""
    token = str(document["token"])
    if not token.startswith(("ghs_", "ghp_")) or len(token) > 1024 or any(c.isspace() for c in token):
        raise ValueError("installation token has an unexpected shape")
    if not path.is_absolute():
        raise ValueError("--token-file must be an absolute path")
    target = path.parent.resolve() / path.name
    for forbidden in {fleet_root.resolve(), DEFAULT_ROOT.resolve()}:
        if target == forbidden or forbidden in target.parents:
            raise ValueError(f"--token-file must not be inside the fleet token store {forbidden}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = target.parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError(
            f"{target.parent} must be owned by the current user and not writable by group or others"
        )
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    dir_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def check_narrow_response(document: dict, repositories, permissions) -> None:
    """Refuse a token GitHub issued wider than what was asked for.

    Compared before anything is written: every returned permission must be one
    that was requested (at most the requested level; GitHub's implicit
    ``metadata: read`` is fine) and the returned repositories must be exactly
    the requested names."""
    rank = {level: i for i, level in enumerate(_PERMISSION_LEVELS)}
    granted = document.get("permissions")
    if not isinstance(granted, dict) or not granted:
        raise ValueError("GitHub's reply carries no permissions to verify; token not stored")
    allowed = {**_IMPLICIT_PERMISSIONS, **permissions}
    for name, level in granted.items():
        if name not in allowed or rank.get(level, 99) > rank[allowed[name]]:
            raise ValueError(
                f"GitHub issued a wider token than requested ({name}={level}); token not stored"
            )
    returned = document.get("repositories")
    if not isinstance(returned, list):
        raise ValueError("GitHub's reply carries no repository list to verify; token not stored")
    names = {str(r.get("name") or str(r.get("full_name", "")).rpartition("/")[2]) for r in returned if isinstance(r, dict)}
    if len(names) != len(returned) or names != set(repositories):
        raise ValueError("GitHub issued the token for different repositories than requested; token not stored")


def _refusal_text(exc: urllib.error.HTTPError) -> str:
    try:
        message = str(json.load(exc).get("message", ""))[:300]
    except Exception:  # noqa: BLE001 - the body is advisory only
        message = ""
    return f"HTTP {exc.code}" + (f": {message}" if message else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-id", default=os.environ.get("AICC_GITHUB_APP_ID", ""))
    parser.add_argument("--installation-id", default=os.environ.get("AICC_GITHUB_INSTALLATION_ID", ""))
    parser.add_argument("--pem", type=Path, default=Path(os.environ.get("AICC_GITHUB_APP_PEM", str(DEFAULT_PEM))))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--repository", action="append", default=None)
    parser.add_argument(
        "--permission",
        action="append",
        default=None,
        metavar="NAME=LEVEL",
        help="narrow-purpose token: request exactly these permissions (repeatable). "
        "Requires --repository and --token-file; without it the fleet token is minted as before.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="narrow-purpose token: absolute output file (0600, never inside the fleet store)",
    )
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="narrow-purpose token: required to request a write/admin level (read-only otherwise)",
    )
    args = parser.parse_args(argv)
    if not args.app_id or not args.installation_id:
        print("AICC_GITHUB_APP_ID and AICC_GITHUB_INSTALLATION_ID are required", file=sys.stderr)
        return 2
    if args.permission is None and args.token_file is not None:
        print("--token-file is only valid together with --permission", file=sys.stderr)
        return 2
    if args.permission is not None:
        return _main_narrow(args)
    document = mint(
        args.app_id,
        args.installation_id,
        args.pem,
        repositories=args.repository or DEFAULT_REPOS,
        permissions=CONTROL_PERMISSIONS,
        fallback_permissions=DEFAULT_PERMISSIONS,
    )
    store(document, args.root)
    print(json.dumps({
        "expires_at": document.get("expires_at"),
        "permissions": document.get("permissions"),
        "repositories": [r.get("full_name") for r in document.get("repositories", [])],
    }))
    return 0


def _main_narrow(args) -> int:
    """A separate, narrow installation token (e.g. contents:read on one repository).

    No defaults and no fallback: the caller names every repository and
    permission, and a request the App cannot satisfy fails loudly instead of
    silently minting something broader."""
    try:
        permissions = parse_permissions(args.permission)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.allow_write and any(level != "read" for level in permissions.values()):
        print("--permission with write/admin needs the explicit --allow-write", file=sys.stderr)
        return 2
    if not args.repository or args.token_file is None:
        print("--permission requires --repository and --token-file (no defaults for a narrow token)", file=sys.stderr)
        return 2
    try:
        document = mint(
            args.app_id,
            args.installation_id,
            args.pem,
            repositories=args.repository,
            permissions=permissions,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404, 422):
            print(
                "GitHub refused the narrow token (" + _refusal_text(exc) + "): the App installation "
                f"must include the repositories {list(args.repository)} and already hold "
                f"{permissions}; a token cannot ask for more than the App has been granted",
                file=sys.stderr,
            )
            return 3
        print(f"GitHub token request failed ({_refusal_text(exc)})", file=sys.stderr)
        return 4
    except (urllib.error.URLError, OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
        print(f"narrow token could not be minted ({type(exc).__name__})", file=sys.stderr)
        return 4
    try:
        check_narrow_response(document, args.repository, permissions)
        store_narrow(document, args.token_file, fleet_root=args.root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "expires_at": document.get("expires_at"),
        "permissions": document.get("permissions"),
        "repositories": [r.get("full_name") for r in document.get("repositories", [])],
        "token_file": str(args.token_file),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
