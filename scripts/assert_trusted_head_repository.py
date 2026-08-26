"""Fail closed unless the checked-out code was authored in this repository.

Why this exists
---------------
`ci.yml` hands `AIOS_ARTIFACT_READONLY_TOKEN` / `AIOS_ARTIFACT_READ_TOKEN` — a
read credential for the *private* `dimastov-lab/aios` repository — to a step
that runs `scripts/fetch_aios_sdk_artifact.py`, a script taken from the
checked-out tree. On the `pull_request` path that is safe by construction:
GitHub withholds secrets from fork-originated runs.

`merge_group` is different. The queue ref (`gh-readonly-queue/<base>/pr-N-<sha>`)
is created *in this repository*, so the event is a base-repository event and
**secrets are passed** — but the ref carries the pull request's own commits, a
fork's included. Enabling the merge queue therefore makes that credential
reachable from fork-authored code unless something stops it.

The obstacle is that the `merge_group` payload describes the queue ref only: it
carries `head_ref`, `head_sha`, `base_ref` and `base_sha`, and says nothing
about the head *repository*. The pull request number is, however, part of the
queue ref, and that ref is minted by GitHub rather than by the contributor, so
it is a trustworthy index: resolve the number, ask the API which repository the
pull request's head lives in, and require it to be this one.

Every unclear case is a refusal: an unknown event, a missing payload, a ref that
does not parse, an absent token, an API error, a field of the wrong shape. A
credential guard that guesses is not a guard.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Events whose ref cannot carry code from outside this repository: `push` is
# gated by branch protection, and `workflow_dispatch`/`schedule` run a ref that
# already exists here. They have no pull-request head to check.
TRUSTED_BY_CONSTRUCTION = frozenset({"push", "workflow_dispatch", "schedule"})

# `refs/heads/gh-readonly-queue/main/pr-311-f9bb889...` -> 311. Anchored on the
# queue prefix so an ordinary branch that merely contains "pr-1-" cannot pose
# as a queue entry.
_QUEUE_REF_PATTERN = re.compile(r"^refs/heads/gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]{40}$")

_MAX_METADATA_BYTES = 1024 * 1024


class UntrustedContextError(RuntimeError):
    """The run may not be given a repository secret."""


def _read_event(env: dict[str, str]) -> dict:
    path = env.get("GITHUB_EVENT_PATH")
    if not path:
        raise UntrustedContextError("GITHUB_EVENT_PATH is not set")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise UntrustedContextError("event payload is unreadable") from error
    if not isinstance(payload, dict):
        raise UntrustedContextError("event payload is not an object")
    return payload


def parse_queue_ref(head_ref: object) -> int:
    """Return the pull request number encoded in a merge-queue ref."""
    if not isinstance(head_ref, str):
        raise UntrustedContextError("merge_group head_ref is missing")
    match = _QUEUE_REF_PATTERN.fullmatch(head_ref)
    if match is None:
        raise UntrustedContextError(f"merge_group head_ref is not a queue ref: {head_ref!r}")
    return int(match.group(1))


def head_repository_of(payload: object) -> str:
    """Return `head.repo.full_name` from a pull-request object, or refuse."""
    if not isinstance(payload, dict):
        raise UntrustedContextError("pull request payload is not an object")
    head = payload.get("head")
    if not isinstance(head, dict):
        raise UntrustedContextError("pull request payload has no head")
    repo = head.get("repo")
    if not isinstance(repo, dict):
        # A deleted fork leaves `head.repo` null. Unknown provenance is untrusted.
        raise UntrustedContextError("pull request head repository is unknown")
    full_name = repo.get("full_name")
    if not isinstance(full_name, str) or not full_name:
        raise UntrustedContextError("pull request head repository has no name")
    return full_name


def _fetch_pull_request(number: int, env: dict[str, str]) -> dict:
    repository = env.get("GITHUB_REPOSITORY")
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        # Without a token the answer would be unverifiable, not "probably fine".
        raise UntrustedContextError("GITHUB_TOKEN is required to resolve the queued pull request")
    api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    request = Request(  # noqa: S310 - fixed https API host from the runner environment
        f"{api}/repos/{repository}/pulls/{number}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aicc-trusted-head-repository-guard",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - see above
            body = response.read(_MAX_METADATA_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise UntrustedContextError(f"cannot resolve pull request #{number}") from error
    if len(body) > _MAX_METADATA_BYTES:
        raise UntrustedContextError("pull request metadata is implausibly large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UntrustedContextError("pull request metadata is not JSON") from error


def resolve_head_repository(env: dict[str, str], payload: dict) -> str:
    """Return the repository that authored the code this run checked out."""
    event = env.get("GITHUB_EVENT_NAME")
    if event == "pull_request":
        return head_repository_of(payload.get("pull_request"))
    if event == "merge_group":
        merge_group = payload.get("merge_group")
        if not isinstance(merge_group, dict):
            raise UntrustedContextError("merge_group payload is missing")
        number = parse_queue_ref(merge_group.get("head_ref"))
        return head_repository_of(_fetch_pull_request(number, env))
    raise UntrustedContextError(f"unsupported event for secret access: {event!r}")


def assert_trusted(env: dict[str, str]) -> str:
    """Return the trusted head repository, or raise `UntrustedContextError`."""
    repository = env.get("GITHUB_REPOSITORY")
    if not repository:
        raise UntrustedContextError("GITHUB_REPOSITORY is not set")
    event = env.get("GITHUB_EVENT_NAME")
    if event in TRUSTED_BY_CONSTRUCTION:
        return repository
    head_repository = resolve_head_repository(env, _read_event(env))
    if head_repository != repository:
        raise UntrustedContextError(
            f"refusing to expose repository secrets to code from {head_repository!r}; "
            f"this run must be authored in {repository!r}"
        )
    return head_repository


def main(argv: list[str] | None = None) -> int:
    try:
        assert_trusted(dict(os.environ))
    except UntrustedContextError as error:
        # The refusal path names the offending repository, which comes from the
        # API rather than from the environment, and is the whole diagnostic
        # value of this guard. The success path deliberately echoes nothing: it
        # is handed the process environment, and printing anything derived from
        # that is how a log line becomes a credential leak.
        print(f"untrusted context: {error}", file=sys.stderr)
        return 1
    print("head repository confirmed: the checked-out code was authored here")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
