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

On a ``merge_group`` event the checked commit is synthetic. GitHub's queue
creates one linear squash commit per pull request, in queue order, and appends
``(#<number>)`` to each subject. The gate validates that complete base-to-head
chain, cross-checks its final number against the GitHub-minted queue ref, then
re-reads and judges every represented pull request. It never treats the final
pull request as a proxy for the rest of a batch.

Everything it cannot establish is a refusal: no review, no marker, a marker for
a different commit, an unparseable sha, a missing token, an API error, or an
ambiguous synthetic history. A gate that guesses accepts nothing in particular.
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# The verdict line. Matched with `fullmatch` against the body's *first line*
# only: a marker in the middle of a review does not count, because the
# reviewer's prose may quote, discuss or recommend a verdict, and only a body
# that opens with one is the reviewer rendering it.
MARKER = re.compile(r"ACCEPTANCE: (ACCEPT|REJECT) ([0-9a-fA-F]{40})[ \t]*")

# A review that was submitted but later dismissed no longer represents its
# author's position, so it cannot supply an acceptance. A rejection still
# blocks: withdrawing an objection is not the same as accepting.
DISMISSED = "DISMISSED"
# Not yet submitted; it is a draft visible only to its author.
PENDING = "PENDING"

_PER_PAGE = 100
_MAX_PAGES = 50
_MAX_BYTES = 8 * 1024 * 1024
_MAX_GROUP_PULL_REQUESTS = 100

_SHA = re.compile(r"[0-9a-fA-F]{40}")
_QUEUE_REF = re.compile(
    r"^refs/heads/gh-readonly-queue/(?P<base>.+)/pr-(?P<number>[1-9][0-9]*)-"
    r"(?P<head>[0-9a-fA-F]{40})$"
)
_SQUASH_SUBJECT = re.compile(r"^.+ \(#(?P<number>[1-9][0-9]*)\)$")


class AcceptanceError(RuntimeError):
    """The pull request may not merge on the evidence available."""


class Verdict:
    """One parsed `ACCEPTANCE:` line together with who published it."""

    def __init__(self, decision: str, sha: str, author: str, state: str) -> None:
        self.decision = decision
        self.sha = sha
        self.author = author
        self.state = state


def parse_marker(body: object) -> tuple[str, str] | None:
    """Return `(decision, sha)` if the body's first line is a verdict."""
    if not isinstance(body, str) or not body:
        return None
    first_line = body.replace("\r\n", "\n").split("\n", 1)[0]
    match = MARKER.fullmatch(first_line)
    if match is None:
        return None
    return match.group(1), match.group(2).lower()


def verdicts_from(reviews: object) -> list[Verdict]:
    """Every parseable verdict in an API review list."""
    if not isinstance(reviews, list):
        raise AcceptanceError("review list is not an array")
    found = []
    for review in reviews:
        if not isinstance(review, dict):
            raise AcceptanceError("review entry is not an object")
        parsed = parse_marker(review.get("body"))
        if parsed is None:
            continue
        user = review.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        state = review.get("state")
        found.append(
            Verdict(
                decision=parsed[0],
                sha=parsed[1],
                # An unattributable review can never establish independence.
                author=author if isinstance(author, str) and author else "",
                state=state if isinstance(state, str) else "",
            )
        )
    return found


def evaluate(reviews: object, head_sha: object, pull_request_author: object) -> str:
    """Return the accepting reviewer's login, or raise with cause and remedy."""
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise AcceptanceError(f"head sha is not a 40-character commit id: {head_sha!r}")
    head = head_sha.lower()
    if not isinstance(pull_request_author, str) or not pull_request_author:
        raise AcceptanceError(
            "the pull request has no resolvable author, so independence is unprovable"
        )
    author = pull_request_author.casefold()

    verdicts = verdicts_from(reviews)
    on_head = [
        verdict
        for verdict in verdicts
        if verdict.sha == head and verdict.state != PENDING
    ]

    rejections = [verdict for verdict in on_head if verdict.decision == "REJECT"]
    if rejections:
        # Checked before any acceptance: a rejection standing on the current
        # head is the reviewer's live position, and an earlier ACCEPT on the
        # same commit does not overturn it.
        who = ", ".join(
            sorted({verdict.author or "<unknown>" for verdict in rejections})
        )
        raise AcceptanceError(
            f"acceptance was REJECTED for {head} by {who}. "
            "Address the rejection, push the fix, and obtain a new verdict on the new head commit"
        )

    accepting = [
        verdict
        for verdict in on_head
        if verdict.decision == "ACCEPT"
        and verdict.state != DISMISSED
        and verdict.author
        and verdict.author.casefold() != author
    ]
    if accepting:
        return accepting[0].author

    # Nothing accepted this commit. Say which of the ways it failed, because a
    # gate whose refusal is unreadable gets routed around instead of fixed.
    self_issued = [
        verdict
        for verdict in on_head
        if verdict.decision == "ACCEPT" and verdict.author.casefold() == author
    ]
    if self_issued:
        raise AcceptanceError(
            f"the only ACCEPT for {head} was published by {self_issued[0].author}, who authored this "
            "pull request. Acceptance must come from an identity that is not the author; have the "
            "acceptance reviewer publish the verdict"
        )
    dismissed = [
        verdict
        for verdict in on_head
        if verdict.decision == "ACCEPT" and verdict.state == DISMISSED
    ]
    if dismissed:
        raise AcceptanceError(
            f"the ACCEPT for {head} was dismissed and no longer stands. Obtain a fresh verdict"
        )
    stale = sorted({verdict.sha for verdict in verdicts if verdict.sha != head})
    if stale:
        raise AcceptanceError(
            f"no verdict names the current head {head}; the newest verdict on record is for "
            f"{', '.join(stale)}. Acceptance is per commit — re-run acceptance against the current "
            "head and publish a verdict naming it"
        )
    raise AcceptanceError(
        f"no acceptance verdict for {head}. A reviewer other than the author must publish a review "
        f"whose first line is exactly `ACCEPTANCE: ACCEPT {head}`"
    )


def _api(path: str, env: dict[str, str]) -> object:
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        # Without a token the verdict is unreadable, which is not the same as
        # absent, which is not the same as favourable.
        raise AcceptanceError(
            "GITHUB_TOKEN is required to read the pull request's reviews"
        )
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


def _event_payload(env: dict[str, str]) -> dict:
    """Read the event payload as an object, or refuse."""
    path = env.get("GITHUB_EVENT_PATH")
    if not path:
        raise AcceptanceError("GITHUB_EVENT_PATH is not set")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("event payload is unreadable") from error
    if not isinstance(payload, dict):
        raise AcceptanceError("event payload is not an object")
    return payload


def _pull_request_number(payload: dict, env: dict[str, str]) -> int:
    """The single pull request under judgement."""
    pull_request = payload.get("pull_request")
    number = pull_request.get("number") if isinstance(pull_request, dict) else None
    if not isinstance(number, int):
        raise AcceptanceError(
            f"event {env.get('GITHUB_EVENT_NAME')!r} carries no pull request; this gate only "
            "judges pull requests"
        )
    return number


def _commit_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise AcceptanceError(f"{field} is not a 40-character commit id: {value!r}")
    return value.lower()


def _merge_group_numbers(
    payload: dict, repository: str, env: dict[str, str]
) -> tuple[list[int], str, str]:
    """Resolve every PR represented by a validated synthetic queue chain.

    Returns ``(numbers, base_branch, final_pr_head)``. The final head is encoded
    in the GitHub-minted queue ref and provides an independent cross-check of
    the last pull request fetched below.
    """
    merge_group = payload.get("merge_group")
    if not isinstance(merge_group, dict):
        raise AcceptanceError("merge_group payload is missing")
    base = _commit_sha(merge_group.get("base_sha"), "merge_group base_sha")
    head = _commit_sha(merge_group.get("head_sha"), "merge_group head_sha")
    head_ref = merge_group.get("head_ref")
    match = _QUEUE_REF.fullmatch(head_ref) if isinstance(head_ref, str) else None
    if match is None:
        raise AcceptanceError(f"merge_group head_ref is not a queue ref: {head_ref!r}")

    base_ref = merge_group.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref:
        raise AcceptanceError("merge_group base_ref is missing")
    base_branch = base_ref.removeprefix("refs/heads/")
    if match.group("base") != base_branch:
        raise AcceptanceError(
            f"merge_group queue ref targets {match.group('base')!r}, not {base_branch!r}"
        )

    comparison = _api(f"/repos/{repository}/compare/{base}...{head}", env)
    if not isinstance(comparison, dict):
        raise AcceptanceError("merge_group comparison is not an object")
    commits = comparison.get("commits")
    if not isinstance(commits, list) or not commits:
        raise AcceptanceError("merge_group comparison contains no commits")
    total = comparison.get("total_commits")
    ahead_by = comparison.get("ahead_by")
    if total != len(commits) or ahead_by != len(commits):
        raise AcceptanceError(
            "merge_group comparison is incomplete or contains non-linear history"
        )
    if len(commits) > _MAX_GROUP_PULL_REQUESTS:
        raise AcceptanceError("merge_group exceeds the supported pull request limit")
    merge_base = comparison.get("merge_base_commit")
    merge_base_sha = merge_base.get("sha") if isinstance(merge_base, dict) else None
    if _commit_sha(merge_base_sha, "comparison merge base") != base:
        raise AcceptanceError("merge_group base is not the comparison merge base")

    numbers: list[int] = []
    previous = base
    for commit in commits:
        if not isinstance(commit, dict):
            raise AcceptanceError("merge_group commit entry is not an object")
        commit_sha = _commit_sha(commit.get("sha"), "merge_group commit sha")
        parents = commit.get("parents")
        if not isinstance(parents, list) or len(parents) != 1:
            raise AcceptanceError("merge_group history is not a linear squash chain")
        parent = parents[0]
        parent_sha = parent.get("sha") if isinstance(parent, dict) else None
        if _commit_sha(parent_sha, "merge_group commit parent") != previous:
            raise AcceptanceError("merge_group commit chain is discontinuous")
        metadata = commit.get("commit")
        message = metadata.get("message") if isinstance(metadata, dict) else None
        subject = (
            message.splitlines()[0] if isinstance(message, str) and message else ""
        )
        subject_match = _SQUASH_SUBJECT.fullmatch(subject)
        if subject_match is None:
            raise AcceptanceError(
                f"merge_group commit {commit_sha} has no unambiguous pull request number"
            )
        numbers.append(int(subject_match.group("number")))
        previous = commit_sha

    if previous != head:
        raise AcceptanceError("merge_group comparison does not end at head_sha")
    if len(set(numbers)) != len(numbers):
        raise AcceptanceError("merge_group contains a duplicate pull request number")
    if numbers[-1] != int(match.group("number")):
        raise AcceptanceError("merge_group history disagrees with its queue ref")
    return numbers, base_branch, match.group("head").lower()


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
    """Return acceptance evidence, or raise `AcceptanceError`."""
    repository = env.get("GITHUB_REPOSITORY")
    if not repository:
        raise AcceptanceError("GITHUB_REPOSITORY is not set")
    payload = _event_payload(env)
    event = env.get("GITHUB_EVENT_NAME")
    expected_base: str | None = None
    expected_final_head: str | None = None
    if event == "merge_group":
        numbers, expected_base, expected_final_head = _merge_group_numbers(
            payload, repository, env
        )
    elif event in {"pull_request", "pull_request_review"}:
        numbers = [_pull_request_number(payload, env)]
    else:
        raise AcceptanceError(f"unsupported event for acceptance: {event!r}")

    evidence = []
    for index, number in enumerate(numbers):
        # Re-read each pull request rather than trusting the event payload. A
        # pushed head invalidates the old verdict even if this run was queued
        # from an earlier review event.
        pull_request = _api(f"/repos/{repository}/pulls/{number}", env)
        if not isinstance(pull_request, dict):
            raise AcceptanceError(f"pull request #{number} response is not an object")
        if pull_request.get("number") != number:
            raise AcceptanceError(
                f"pull request #{number} response has the wrong number"
            )
        if event == "merge_group" and pull_request.get("state") != "open":
            raise AcceptanceError(f"merge_group pull request #{number} is not open")
        base = pull_request.get("base")
        base_ref = base.get("ref") if isinstance(base, dict) else None
        if expected_base is not None and base_ref != expected_base:
            raise AcceptanceError(
                f"merge_group pull request #{number} targets {base_ref!r}, "
                f"not {expected_base!r}"
            )
        head = pull_request.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        exact_head = _commit_sha(head_sha, f"pull request #{number} head sha")
        if index == len(numbers) - 1 and expected_final_head is not None:
            if exact_head != expected_final_head:
                raise AcceptanceError(
                    f"merge_group queue ref is stale for pull request #{number}"
                )
        user = pull_request.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        reviewer = evaluate(_reviews(repository, number, env), exact_head, author)
        evidence.append(f"#{number}:{reviewer}")

    return ", ".join(evidence)


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
