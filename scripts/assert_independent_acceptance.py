"""Fail closed unless an independent verdict accepted *this exact* head commit.

Why this exists
---------------
The delivery rules require every pull request to be accepted by a reviewer who
is not its author, on the exact commit that will be merged. GitHub cannot
enforce that here, and both built-in routes were tried and closed:

* `required_approving_review_count` with `reviewDecision == APPROVED`. The
  acceptance identity is a GitHub App installation with `contents: read`; its
  approval carries `authorAssociation: NONE` and GitHub only counts approvals
  from accounts with write access. Granting write access to the reviewer would
  dissolve the separation the rule exists to create.
* Self-approval. Every agent runs under the account that opens the pull
  request, and GitHub refuses an approval from the author at the API level.

So the verdict is published as a review whose **first line** is exactly::

    ACCEPTANCE: ACCEPT <40-character head sha>
    ACCEPTANCE: REJECT <40-character head sha>

and this gate is what makes it binding. Independence is checkable here even
though it is invisible to branch protection: the app reviews under its own
`login` (`voyn-acceptance[bot]`), so the comparison is `login` against the pull
request's author `login` — not `authorAssociation`, which is exactly the field
that made the built-in route unusable.

Everything it cannot establish is a refusal: no review, no marker, a marker for
a different commit, an unparseable sha, a missing token, an API error. A gate
that guesses accepts nothing in particular.

The judgement itself (``evaluate`` and its helpers) lives in
``command_center.orchestrator.acceptance_policy``, imported below rather than
redefined: ``command_center.orchestrator.merge_gateway`` — the only component
allowed to actually merge a pull request — has to make this exact same
judgement before it will spend its own merge credential, and two
implementations of "was this independently accepted" is exactly the kind of
drift that turns into a bypass. This script stays the CI-specific shell: read
the Actions event payload, call the GitHub API with ``GITHUB_TOKEN``, print a
verdict.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# This step deliberately runs the checked-out file directly
# (`python3 scripts/assert_independent_acceptance.py`, no install step — see
# acceptance-gate.yml), which puts only this file's own directory on
# `sys.path`, not the repository root where `command_center` lives.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from command_center.orchestrator.acceptance_policy import (  # noqa: E402
    DISMISSED,
    MARKER,
    PENDING,
    AcceptanceError,
    Verdict,
    evaluate,
    parse_marker,
    verdicts_from,
)

__all__ = [
    "MARKER",
    "DISMISSED",
    "PENDING",
    "AcceptanceError",
    "Verdict",
    "parse_marker",
    "verdicts_from",
    "evaluate",
    "assert_accepted",
    "main",
]

_PER_PAGE = 100
_MAX_PAGES = 50
_MAX_BYTES = 8 * 1024 * 1024


def _api(path: str, env: dict[str, str]) -> object:
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        # Without a token the verdict is unreadable, which is not the same as
        # absent, which is not the same as favourable.
        raise AcceptanceError("GITHUB_TOKEN is required to read the pull request's reviews")
    base = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    request = Request(  # noqa: S310 - fixed https API host from the runner environment
        f"{base}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aicc-acceptance-gate",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - see above
            body = response.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise AcceptanceError(f"cannot read {path} from the GitHub API") from error
    if len(body) > _MAX_BYTES:
        raise AcceptanceError(f"the response for {path} is implausibly large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"the response for {path} is not JSON") from error


def _pull_request_number(env: dict[str, str]) -> int:
    """The pull request under judgement, from the event payload."""
    path = env.get("GITHUB_EVENT_PATH")
    if not path:
        raise AcceptanceError("GITHUB_EVENT_PATH is not set")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("event payload is unreadable") from error
    pull_request = payload.get("pull_request") if isinstance(payload, dict) else None
    number = pull_request.get("number") if isinstance(pull_request, dict) else None
    if not isinstance(number, int):
        raise AcceptanceError(
            f"event {env.get('GITHUB_EVENT_NAME')!r} carries no pull request; this gate only "
            "judges pull requests"
        )
    return number


def _reviews(repository: str, number: int, env: dict[str, str]) -> list:
    """Every submitted review, following pagination to the end."""
    collected: list = []
    for page in range(1, _MAX_PAGES + 1):
        batch = _api(
            f"/repos/{repository}/pulls/{number}/reviews?per_page={_PER_PAGE}&page={page}",
            env,
        )
        if not isinstance(batch, list):
            raise AcceptanceError("the reviews response is not an array")
        collected.extend(batch)
        if len(batch) < _PER_PAGE:
            return collected
    raise AcceptanceError("the review list did not end within the page limit")


def assert_accepted(env: dict[str, str]) -> str:
    """Return the accepting reviewer's login, or raise `AcceptanceError`."""
    repository = env.get("GITHUB_REPOSITORY")
    if not repository:
        raise AcceptanceError("GITHUB_REPOSITORY is not set")
    number = _pull_request_number(env)

    # Re-read the pull request rather than trusting the event payload: on a
    # `pull_request_review` event the payload was assembled for the review, and
    # a head that moved between the push and this run must not be judged by a
    # verdict for the commit it replaced.
    pull_request = _api(f"/repos/{repository}/pulls/{number}", env)
    if not isinstance(pull_request, dict):
        raise AcceptanceError("the pull request response is not an object")
    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    user = pull_request.get("user")
    pull_request_author = user.get("login") if isinstance(user, dict) else None

    return evaluate(_reviews(repository, number, env), head_sha, pull_request_author)


def main(argv: list[str] | None = None) -> int:
    try:
        reviewer = assert_accepted(dict(os.environ))
    except AcceptanceError as error:
        print(f"acceptance gate refused: {error}", file=sys.stderr)
        return 1
    print(f"independent acceptance confirmed, published by {reviewer}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
