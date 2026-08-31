"""The privileged merge gateway (VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY).

Before this module existed, ``review_merge.py`` published the ACCEPT marker
and called ``gh pr merge`` through the same ``_gh()`` helper — one ambient
``gh`` credential doing both jobs, with the author's identity a third use of
that same credential (``publish.py``'s push/``gh pr create``). Three acts that
must stay independent — *open* the change, *accept* it, *merge* it — shared
one bearer of trust. Whoever held that credential could self-accept and
self-merge; nothing about the code path made that impossible, only unlikely
given what the code happened to check.

This module is the fix, and it is a credential boundary, not a code-review
boundary: it is the **only** place in this codebase that may call
``gh pr merge`` (enforced mechanically by
``tests/architecture/test_merge_gateway_boundary.py``, which fails the build
if any other file gains a merge call site), and it reads its own GitHub
credential from :data:`GATEWAY_TOKEN_ENV` — a token that belongs to a service
identity distinct from the one that pushed the branch and opened the pull
request (``publish.py``, the worker's ambient ``gh auth``) and distinct from
the one that posted the acceptance verdict (the independent reviewer
identity ``acceptance_policy.evaluate`` checks for). Deployment keeps that
distinctness real: ``deploy/systemd/aicc-backlog-merge.service`` — the only
unit that invokes ``merge_once`` and therefore this module — runs as its own
dedicated ``aicc-merge-gateway`` OS user with its own env file, never
``aicc-worker``'s, so the token in :data:`GATEWAY_TOKEN_ENV` never lands in
the same process environment as the worker daemon's or the planner's.

If :data:`GATEWAY_TOKEN_ENV` is unset, this module refuses every merge; it
never falls back to the ambient ``gh`` credential the rest of the pipeline
uses to push and open pull requests, because that credential is exactly what
this module exists to keep separate from merge authority. A worker or planner
process that has no reason to merge anything is verified to hold none of this
credential by ``tests/architecture/test_merge_gateway_boundary.py`` reading
the deployed systemd unit files directly, not by inspecting this module's own
logic — the property that matters is what secret the OS handed a process, not
what the application code chose to do with it.

Every check below is a refusal, not a warning: an unreadable API response, a
missing credential, a stale policy version, a check still pending, all return
``GatewayResult(ok=False, ...)`` and merge nothing. A gate that guesses on
GitHub's unavailability accepts nothing in particular.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from command_center.orchestrator.acceptance_policy import AcceptanceError, evaluate

__all__ = [
    "GATEWAY_TOKEN_ENV",
    "POLICY_VERSION",
    "GatewayConfig",
    "GatewayResult",
    "merge_pr",
]

# Bumped whenever the checks this module performs change in a way that makes
# an older verdict about "is this safe to merge" no longer trustworthy (a
# required check added or dropped, the acceptance policy itself tightened or
# loosened). `GatewayConfig.policy_version` is compared against this constant
# before anything else runs: a deployed config pinned to a version the running
# code no longer implements is a version-skew bug, not a merge decision, and
# is refused the same way a missing credential is.
POLICY_VERSION = "2026-08-26.1"

# The one environment variable this module reads for its merge credential.
# Deliberately not GH_TOKEN/GITHUB_TOKEN: those are the names `gh`, this
# codebase's `_VCS_CREDENTIAL_ENV_VARS` scrub list, and every ambient
# credential helper already know, and a merge credential that answered to the
# same name as the push/open credential would collapse right back into the
# single shared secret this module exists to split apart.
GATEWAY_TOKEN_ENV = "VOYN_MERGE_GATEWAY_TOKEN"

_TIMEOUT = 30
_TERMINAL_SUCCESS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_PR_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    repo_path: str
    token_env: str = GATEWAY_TOKEN_ENV
    policy_version: str = POLICY_VERSION
    merge_method: str = "squash"


@dataclass(frozen=True, slots=True)
class GatewayResult:
    ok: bool
    head_sha: str = ""
    reviewer: str = ""
    reason: str = ""


def _parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    match = _PR_URL.fullmatch((pr_url or "").strip())
    if match is None:
        return None
    owner, repo, number = match.group(1), match.group(2), match.group(3)
    return owner, repo, int(number)


def _gh(argv: list[str], repo_path: str, token: str) -> subprocess.CompletedProcess[str]:
    """Run `gh` under the gateway's own credential, never the ambient one.

    `gh` prefers `GH_TOKEN` over any cached `gh auth login` session, so
    setting it here — after clearing whatever the host process happened to
    inherit — is what makes every call this module makes provably run as the
    gateway identity rather than whatever `gh` would have picked on its own.
    """
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env["GH_TOKEN"] = token
    return subprocess.run(
        ["gh", *argv], cwd=repo_path, capture_output=True, text=True,
        check=False, timeout=_TIMEOUT, env=env,
    )


def _credential(cfg: GatewayConfig) -> str | None:
    token = os.environ.get(cfg.token_env, "").strip()
    return token or None


def merge_pr(pr_url: str, cfg: GatewayConfig) -> GatewayResult:
    """Verify independently, then merge — or refuse. The only call site of
    `gh pr merge`/the merge endpoint in this codebase.

    Order matters: the credential and policy-version checks are cheap and
    config-only, so they run first and never touch the network. Every
    network call after that is made with the gateway's own token, so even
    the read-only verification step is provably independent of whatever
    identity pushed the branch or posted the marker.
    """
    if cfg.policy_version != POLICY_VERSION:
        return GatewayResult(
            ok=False,
            reason=(
                f"policy_version_mismatch: configured={cfg.policy_version!r} "
                f"code={POLICY_VERSION!r}"
            ),
        )
    token = _credential(cfg)
    if token is None:
        return GatewayResult(ok=False, reason=f"gateway_credential_missing: {cfg.token_env} is not set")
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        return GatewayResult(ok=False, reason=f"unresolvable_pr_url: {pr_url!r}")
    owner, repo, number = parsed

    try:
        view = _gh(
            ["pr", "view", pr_url, "--json",
             "state,headRefOid,author,reviews,statusCheckRollup"],
            cfg.repo_path, token,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GatewayResult(ok=False, reason=f"gh_view_error: {exc}")
    if view.returncode != 0:
        return GatewayResult(ok=False, reason=f"gh_view_failed: {view.stderr.strip()[:200]}")
    try:
        data: Any = json.loads(view.stdout or "{}")
    except json.JSONDecodeError as exc:
        return GatewayResult(ok=False, reason=f"gh_view_unparseable: {exc}")
    if not isinstance(data, dict):
        return GatewayResult(ok=False, reason="gh_view_not_an_object")

    if data.get("state") != "OPEN":
        return GatewayResult(ok=False, reason=f"pr_not_open: {data.get('state')!r}")

    head = data.get("headRefOid")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        return GatewayResult(ok=False, reason=f"head_sha_unreadable: {head!r}")

    author = data.get("author")
    author_login = author.get("login") if isinstance(author, dict) else None
    try:
        reviewer = evaluate(data.get("reviews"), head, author_login)
    except AcceptanceError as exc:
        return GatewayResult(ok=False, reason=f"acceptance_refused: {exc}")

    rollup = data.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return GatewayResult(ok=False, reason="no_status_checks_reported")
    not_green = [
        (c.get("name", "?") if isinstance(c, dict) else "?")
        for c in rollup
        if not isinstance(c, dict) or c.get("conclusion") not in _TERMINAL_SUCCESS
    ]
    if not_green:
        return GatewayResult(ok=False, reason=f"checks_not_terminal_success: {not_green[:5]}")

    # `sha=` pins the merge to the exact commit just verified: GitHub refuses
    # (409) if the head has moved since the `pr view` above, closing the
    # check-then-act race a second `gh pr view` re-read could not.
    try:
        merge = _gh(
            ["api", "-X", "PUT", f"repos/{owner}/{repo}/pulls/{number}/merge",
             "-f", f"merge_method={cfg.merge_method}", "-f", f"sha={head}"],
            cfg.repo_path, token,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GatewayResult(ok=False, reason=f"gh_merge_error: {exc}")
    if merge.returncode != 0:
        return GatewayResult(ok=False, reason=f"gh_merge_failed: {merge.stderr.strip()[:200]}")

    return GatewayResult(ok=True, head_sha=head, reviewer=reviewer)
