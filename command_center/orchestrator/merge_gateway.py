"""The privileged merge gateway: the ONE place `gh pr merge` is ever invoked.

Why this exists
----------------
Before this module, `review_merge.py`'s `merge_once` called
``_gh(["pr", "merge", pr_url, "--squash"], repo_path)`` directly -- the same
ambient `gh` credential every other read in that file uses, and the same
credential the marker-publishing and PR-opening paths use. That module's own
docstring named this as a deliberate, temporary decision ("author and merger
are the same account"). It means the same identity that can author a change
can also be the one that lands it: a bug in `merge_once`'s own readiness
check, or a credential leaked from the worker/planner side, is a direct path
to `main` with no independent gate at the credential layer -- only at the
application layer, which is exactly the layer a bug lives in.

This module is that independent gate. It is the only code in this codebase
that invokes `gh pr merge`, it does so under its OWN GitHub App installation
(`VOYN_MERGE_GATEWAY_APP_ID` / `_INSTALLATION_ID` / `_PRIVATE_KEY_PATH` --
gated exactly the way `review_merge._acceptance_app_credentials` gates the
acceptance identity, but a THIRD identity, distinct from both the acceptance
bot that posts the marker and the identity that authored/opened the PR), and
every one of its own reads is fetched fresh over the GitHub REST API with
that identity's own token -- never trusted from the caller, never read
through the ambient `gh` credential the rest of `review_merge.py` uses.
Deployment must keep this identity's credentials out of the worker's and
planner's environment entirely (see `deploy/systemd/aicc-backlog-merge.
service`): the acceptance criterion is that attempting a merge without
passing every check below is impossible at the credential level, not just
unreachable through this module's own logic.

Why the merge itself still goes through `gh`, not a raw REST call
-------------------------------------------------------------------
`review_merge.py` already carries substantial, live-debugged logic for what
happens AFTER a `gh pr merge` invocation -- a merge-queue-protected repo
only enqueues (`_merged_target_sha`'s "not_merged" leg, live 2026-08-26 on
PR #399), and the target-branch commit, never the PR head, is the only
evidence that counts. Reimplementing that against the raw REST merge
endpoint would either drop those cases or duplicate them under a new set of
edge cases nobody has hit in production yet. Keeping `gh pr merge` as the
actual write -- authenticated as THIS identity via a `GH_TOKEN` environment
override, never the ambient one -- gets the credential separation this task
exists for without touching that already-hardened aftermath logic at all.
`GH_TOKEN` takes precedence over any stored `gh auth login` state, so the
subprocess it launches is provably running as the gateway identity and
nothing else, regardless of what the calling process's own `gh` credential
is (or, per the acceptance criterion, is not).

Why every read is re-fetched here instead of trusted from the caller
------------------------------------------------------------------------
A gateway that merges on a caller's say-so is not a gate, it is a formality.
Every one of the checks below is independently re-derived from the GitHub
API, under this module's own credential, immediately before the merge
attempt:

* the pull request is OPEN;
* its head is EXACTLY the sha the caller expects to merge (a caller passes
  the sha it believes carries an accepted verdict; a PR that moved since
  then is refused, never merged at a different commit than was reviewed);
* an independent, non-author reviewer's ACCEPT verdict stands on that exact
  head with no REJECT on the same head anywhere outstanding -- delegated to
  `scripts.assert_independent_acceptance.evaluate`, the exact function the
  required CI acceptance gate itself runs, so this gateway can never enforce
  a looser independence contract than the gate already requires in CI;
* every required check on that head is terminal-success.

Why reviews are paginated by hand instead of `gh pr view --json reviews`
------------------------------------------------------------------------
Independent review of an earlier version of this gateway (PR #523,
e0b05bbd) found that `gh pr view --json reviews` resolves through GitHub's
GraphQL API with a single bounded page of review nodes. A pull request with
enough reviews can have an active REJECT fall outside that bounded page
while a stale or unrelated ACCEPT remains visible within it -- the gate
would then see only the ACCEPT and merge over a live rejection it never
fetched. `scripts/assert_independent_acceptance.py` (the CI acceptance
gate) already gets this right by paginating the REST reviews endpoint to
its end before evaluating anything; `_paginated_reviews` below is the same
approach, and its output is fed to that same script's `evaluate()` so the
independence and no-active-reject logic is identical, not merely similar,
to what CI already enforces. Pagination that does not terminate within the
page budget is refused, not silently truncated -- an unbounded or
malicious review history must never look like "no reviews" instead of
"can't tell."

Fail-closed
-----------
Every check is one `try` block ending in a single `except GatewayError`
that returns a refusal `MergeResult` -- there is no code path between "a
check failed, or could not be completed" and "no merge is attempted."
Missing credentials, a network failure, a malformed API response, a
pagination bound exceeded, an ambiguous check-run rerun ordering: every one
of these raises `GatewayError` and is caught by that same handler. Nothing
here has an implicit "assume yes and continue" branch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from command_center.orchestrator import github_app_auth
from command_center.orchestrator.github_shared import (
    check_is_green as _check_is_green,
    latest_checks_by_name as _latest_checks_by_name,
    owner_repo_number_from_pr_url as _owner_repo_number_from_pr_url,
)
from scripts.assert_independent_acceptance import AcceptanceError, evaluate

__all__ = ["GatewayError", "MergeResult", "evaluate_and_merge"]

#: Bumped whenever this module's OWN verification contract changes (which
#: checks it performs, or what counts as satisfying one) -- the same
#: rollout discipline as `review_merge._REVIEW_POLICY_VERSION` and
#: `_VERIFICATION_POLICY_VERSION`. Nothing on the GitHub side carries this
#: value (the ACCEPTANCE marker's own format is a separate, CI-gate-shared
#: contract this module deliberately does not renegotiate -- see the module
#: docstring), so it is not compared against anything fetched from the API;
#: its purpose is a stable, greppable identifier for which contract version
#: produced a given `MergeResult`, exactly the way the review policy version
#: identifies which prompt contract produced a given verdict.
GATEWAY_POLICY_VERSION = "gateway-v1"

_PER_PAGE = 100
_MAX_REVIEW_PAGES = 50
_MAX_CHECK_PAGES = 50
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_API_VERSION = "2022-11-28"

_SHA = re.compile(r"[0-9a-fA-F]{40}")


class GatewayError(RuntimeError):
    """Any reason the gateway will not merge. Always caught by
    `evaluate_and_merge`'s own top-level handler and turned into a refusal
    `MergeResult` -- never raised out of this module."""


@dataclass(frozen=True, slots=True)
class MergeResult:
    """The gateway's verdict on one merge attempt.

    ``attempted`` is the load-bearing field: it is True iff every gateway
    check passed AND the privileged `gh pr merge` command was actually
    dispatched under the gateway's identity. It is NOT proof the pull
    request is now merged -- a merge-queue-protected repository may only
    have enqueued it, exactly as the un-gated `gh pr merge` call always
    could; the caller re-reads the target branch to find out, as it always
    has (`review_merge._merged_target_sha`). ``attempted is False`` is the
    property every pre-merge refusal test exists to check: it means the
    privileged command was never run at all, not merely that its result
    was discarded.
    """

    attempted: bool
    returncode: int | None
    stdout: str
    stderr: str
    #: Populated only when `attempted` is False.
    refusal_reason: str
    #: The independent reviewer's login that supplied the ACCEPT, when one
    #: was found -- for audit logging alongside the merge, not itself a gate.
    reviewer: str | None = None


def _gateway_credentials() -> github_app_auth.GitHubAppCredentials | None:
    """The merge gateway's own GitHub App installation -- gated on env the
    same way `review_merge._acceptance_app_credentials` gates the
    acceptance identity, but a DIFFERENT set of variables naming a THIRD
    installation: this identity must never be the acceptance bot's, and
    must never be the ambient credential the PR's author/opener runs
    under. A host with none of the three set has no gateway identity at
    all and every merge attempt refuses closed -- there is nothing safe to
    fall back to, exactly as `_acceptance_app_credentials` has nothing
    safe to fall back to either."""
    app_id = os.environ.get("VOYN_MERGE_GATEWAY_APP_ID", "")
    installation_id = os.environ.get("VOYN_MERGE_GATEWAY_INSTALLATION_ID", "")
    key_path = os.environ.get("VOYN_MERGE_GATEWAY_PRIVATE_KEY_PATH", "")
    if not (app_id and installation_id and key_path):
        return None
    return github_app_auth.GitHubAppCredentials(app_id, installation_id, Path(key_path))


def _request(method: str, path: str, token: str, *, body: dict[str, Any] | None = None) -> Any:
    """The gateway's one HTTP transport -- every read and the merge
    dispatch's precondition reads all funnel through here, always
    authenticated with the gateway's own freshly-minted token, never the
    ambient `gh` credential. `evaluate_and_merge` accepts this as an
    injectable parameter precisely so a test can record every call made
    through it and assert none of them was ever the merge write."""
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - fixed GitHub API host
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise GatewayError(f"api_http_error:{method}:{path}:{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GatewayError(f"api_unreachable:{method}:{path}") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise GatewayError(f"api_response_too_large:{path}")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GatewayError(f"api_response_not_json:{path}") from exc


def _paginated_reviews(
    owner: str, repo: str, number: str, token: str, transport: Any
) -> list[Any]:
    """Every submitted review on the pull request, following pagination to
    its end -- the same approach `scripts/assert_independent_acceptance.
    py`'s `_reviews` takes, and for the same reason: `gh pr view --json
    reviews` resolves through a single bounded GraphQL page, and a PR with
    enough reviews can have an active REJECT sit outside that page while an
    ACCEPT remains visible within it (independent review of e0b05bbd on
    PR #523). Refuses rather than truncates if the review history does not
    end within the page budget -- an unbounded history must read as "can't
    tell," never as "no more reviews.\""""
    collected: list[Any] = []
    for page in range(1, _MAX_REVIEW_PAGES + 1):
        batch = transport(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews"
            f"?per_page={_PER_PAGE}&page={page}",
            token,
        )
        if not isinstance(batch, list):
            raise GatewayError("reviews_response_malformed")
        collected.extend(batch)
        if len(batch) < _PER_PAGE:
            return collected
    raise GatewayError("reviews_pagination_did_not_terminate")


def _paginated_check_runs(
    owner: str, repo: str, sha: str, token: str, transport: Any
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for page in range(1, _MAX_CHECK_PAGES + 1):
        batch = transport(
            "GET",
            f"/repos/{owner}/{repo}/commits/{sha}/check-runs"
            f"?per_page={_PER_PAGE}&page={page}",
            token,
        )
        runs = batch.get("check_runs") if isinstance(batch, dict) else None
        if not isinstance(runs, list):
            raise GatewayError("check_runs_response_malformed")
        collected.extend(runs)
        total = batch.get("total_count") if isinstance(batch, dict) else None
        if len(runs) < _PER_PAGE or (isinstance(total, int) and len(collected) >= total):
            return collected
    raise GatewayError("check_runs_pagination_did_not_terminate")


def _paginated_statuses(
    owner: str, repo: str, sha: str, token: str, transport: Any
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for page in range(1, _MAX_CHECK_PAGES + 1):
        batch = transport(
            "GET",
            f"/repos/{owner}/{repo}/commits/{sha}/statuses"
            f"?per_page={_PER_PAGE}&page={page}",
            token,
        )
        if not isinstance(batch, list):
            raise GatewayError("statuses_response_malformed")
        collected.extend(batch)
        if len(batch) < _PER_PAGE:
            return collected
    raise GatewayError("statuses_pagination_did_not_terminate")


def _normalized_checks(
    check_runs: list[dict[str, Any]], statuses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Reshape the two REST check sources into the same dict shape
    `review_merge._latest_checks_by_name` / `_check_is_green` already
    expect from GraphQL's `statusCheckRollup` (`name`/`conclusion` or
    `name`/`state`, `startedAt`/`completedAt`) -- reusing that logic keeps
    "what counts as green," "the latest run wins," and "an ambiguous rerun
    order fails closed" identical to the rest of the merge pipeline instead
    of a second, divergent implementation of the same rule."""
    normalized: list[dict[str, Any]] = []
    for run in check_runs:
        if not isinstance(run, dict):
            raise GatewayError("check_run_entry_malformed")
        conclusion = run.get("conclusion")
        normalized.append({
            "name": run.get("name"),
            "conclusion": conclusion.upper() if isinstance(conclusion, str) else None,
            "startedAt": run.get("started_at"),
            "completedAt": run.get("completed_at"),
        })
    for status in statuses:
        if not isinstance(status, dict):
            raise GatewayError("status_entry_malformed")
        state = status.get("state")
        normalized.append({
            "name": status.get("context"),
            "state": state.upper() if isinstance(state, str) else None,
            "startedAt": status.get("created_at"),
            "completedAt": status.get("updated_at"),
        })
    return normalized


def _merge_via_gh(
    pr_url: str, token: str, *, runner: Any
) -> subprocess.CompletedProcess[str]:
    """The ONE call in this codebase that lands a pull request -- `gh pr
    merge` invoked with the gateway's own freshly-minted installation token
    via `GH_TOKEN`, which `gh` prefers over any stored `gh auth login`
    state. No `repo_path`/cwd is needed: a full PR URL argument lets `gh`
    resolve the repository on its own, so this call has no dependency on a
    local checkout existing at all -- deliberately, since the gateway is
    meant to be deployable as a standalone privileged component."""
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return runner(
        ["gh", "pr", "merge", pr_url, "--squash"],
        capture_output=True, text=True, check=False, timeout=120, env=env,
    )


def evaluate_and_merge(
    pr_url: str,
    expected_head_sha: str,
    *,
    creds: github_app_auth.GitHubAppCredentials | None = None,
    request: Any = None,
    runner: Any = None,
) -> MergeResult:
    """Verify every gateway precondition against a fresh, independently
    fetched read of the pull request, and merge it iff all of them hold.

    ``expected_head_sha`` is the caller's claim about which commit carries
    an accepted verdict (typically the head `review_merge._pr_is_mergeable`
    just observed) -- it is never trusted on its own, only used to demand
    that THIS module's own independent read of the PR agrees exactly. A PR
    that moved between the caller's check and this call is refused, not
    merged at whatever its new head happens to be.

    ``creds``/``request``/``runner`` are injectable for tests: production
    callers pass none of them and get the real GitHub App credential gate,
    the real HTTPS transport, and the real `gh` subprocess. Every code path
    that does not end in `attempted=True` is required, by the single
    top-level `except GatewayError` below, to have made zero calls through
    ``runner`` -- there is no other way to reach the return statement that
    dispatches the merge.
    """
    transport = request if request is not None else _request
    dispatch = runner if runner is not None else subprocess.run
    try:
        if not isinstance(expected_head_sha, str) or _SHA.fullmatch(expected_head_sha) is None:
            raise GatewayError(f"invalid_expected_head_sha:{expected_head_sha!r}")
        parsed = _owner_repo_number_from_pr_url(pr_url)
        if parsed is None:
            raise GatewayError(f"invalid_pr_url:{pr_url!r}")
        owner, repo, number = parsed

        active_creds = creds if creds is not None else _gateway_credentials()
        if active_creds is None:
            raise GatewayError("gateway_credentials_not_configured")
        try:
            token = github_app_auth.installation_token(active_creds)
        except github_app_auth.AppAuthError as exc:
            raise GatewayError(f"gateway_auth_failed:{exc}") from exc

        pull = transport("GET", f"/repos/{owner}/{repo}/pulls/{number}", token)
        if not isinstance(pull, dict):
            raise GatewayError("pull_request_response_malformed")
        if pull.get("state") != "open":
            raise GatewayError(f"pr_not_open:{pull.get('state')!r}")
        head = pull.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or head_sha.casefold() != expected_head_sha.casefold():
            raise GatewayError(f"head_sha_mismatch:{head_sha!r}")
        user = pull.get("user")
        author_login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(author_login, str) or not author_login:
            raise GatewayError("pr_author_unresolvable")

        reviews = _paginated_reviews(owner, repo, number, token, transport)
        try:
            reviewer_login = evaluate(reviews, expected_head_sha, author_login)
        except AcceptanceError as exc:
            raise GatewayError(f"acceptance_refused:{exc}") from exc

        check_runs = _paginated_check_runs(owner, repo, expected_head_sha, token, transport)
        statuses = _paginated_statuses(owner, repo, expected_head_sha, token, transport)
        rollup = _latest_checks_by_name(_normalized_checks(check_runs, statuses))
        if not rollup:
            raise GatewayError("no_required_checks_reported")
        bad = [c.get("name", "?") for c in rollup if not _check_is_green(c)]
        if bad:
            raise GatewayError(f"checks_not_green:{bad[:5]}")

        completed = _merge_via_gh(pr_url, token, runner=dispatch)
        return MergeResult(
            attempted=True,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            refusal_reason="",
            reviewer=reviewer_login,
        )
    except GatewayError as exc:
        return MergeResult(
            attempted=False,
            returncode=None,
            stdout="",
            stderr="",
            refusal_reason=str(exc),
            reviewer=None,
        )
