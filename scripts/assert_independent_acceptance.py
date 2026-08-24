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
``(#<number>)`` to each subject. Commit subjects identify the represented pull
requests but do not prove their exact source heads. The gate therefore binds
that chain to GitHub's authoritative merge-queue entries. Every entry's
``baseCommit`` and synthetic ``headCommit`` must match its segment of that
chain, while the entry's current pull-request head must match both a second
API read and the accepted SHA. It never treats the final pull request as a
proxy for the rest of a batch.

The subject suffix is an explicit repository contract: changing GitHub's
squash-message template so queue commits no longer end in ``(#<number>)``
will stop the gate closed until the resolver and its tests are updated.

Everything it cannot establish is a refusal: no review, no marker, a marker for
a different commit, an unparseable sha, a missing token, an API error, or an
ambiguous synthetic history. A gate that guesses accepts nothing in particular.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ``python3 scripts/assert_independent_acceptance.py`` makes ``scripts/`` the
# import root. Add the checked-out repository root explicitly so the workflow
# and server import the same versioned policy loader instead of duplicating it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from command_center.orchestrator import acceptance_policy  # noqa: E402

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
_MAX_QUEUE_ENTRIES = _PER_PAGE * _MAX_PAGES

_SHA = re.compile(r"[0-9a-fA-F]{40}")
_QUEUE_REF = re.compile(
    r"^refs/heads/gh-readonly-queue/(?P<base>.+)/pr-(?P<number>[1-9][0-9]*)-"
    r"(?P<stamp>[0-9a-fA-F]{40})$"
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


def evaluate(
    reviews: object,
    head_sha: object,
    pull_request_author: object,
    policy: acceptance_policy.AcceptancePolicy | None = None,
) -> str:
    """Return the accepting reviewer's login, or raise with cause and remedy."""
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise AcceptanceError(f"head sha is not a 40-character commit id: {head_sha!r}")
    head = head_sha.lower()
    if not isinstance(pull_request_author, str) or not pull_request_author:
        raise AcceptanceError(
            "the pull request has no resolvable author, so independence is unprovable"
        )
    author = pull_request_author.casefold()
    policy = policy or acceptance_policy.load()

    verdicts = verdicts_from(reviews)
    current = [
        verdict
        for verdict in verdicts
        if verdict.sha == head and verdict.state != PENDING
    ]
    authorized = [
        verdict
        for verdict in current
        if verdict.state != DISMISSED
        and verdict.author
        and verdict.author.casefold() != author
        and verdict.author.casefold() in policy.trusted_reviewer_logins
    ]
    rejections = [verdict for verdict in authorized if verdict.decision == "REJECT"]
    if rejections:
        who = ", ".join(sorted({verdict.author for verdict in rejections}))
        raise AcceptanceError(
            f"acceptance was REJECTED for {head} by {who}. "
            "Address the rejection, push the fix, and obtain a new verdict on the new head commit"
        )
    accepting = [verdict for verdict in authorized if verdict.decision == "ACCEPT"]
    if accepting:
        return accepting[-1].author

    # Nothing accepted this commit. Say which of the ways it failed, because a
    # gate whose refusal is unreadable gets routed around instead of fixed.
    self_issued = [
        verdict
        for verdict in current
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
        for verdict in current
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
        f"no acceptance verdict for {head}. A policy-authorized reviewer must publish a review "
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
    request = Request(  # noqa: S310 - fixed GitHub API host from runner env
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


def _graphql(query: str, variables: dict[str, object], env: dict[str, str]) -> object:
    """Run a read-only GraphQL query, with the same fail-closed limits as REST."""
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        raise AcceptanceError("GITHUB_TOKEN is required to read the merge queue")
    url = env.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(  # noqa: S310 - fixed GitHub GraphQL host from runner env
        url,
        method="POST",
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aicc-acceptance-gate",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - see above
            response_body = response.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise AcceptanceError(
            "cannot read the merge queue from the GitHub API"
        ) from error
    if len(response_body) > _MAX_BYTES:
        raise AcceptanceError("the merge-queue response is implausibly large")
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("the merge-queue response is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("errors"):
        raise AcceptanceError("the merge-queue GraphQL query returned errors")
    return decoded


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


_MERGE_QUEUE_QUERY = """
query($owner: String!, $name: String!, $branch: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) {
      entries(first: 100, after: $cursor) {
        nodes {
          position
          state
          baseCommit { oid }
          headCommit { oid }
          pullRequest { number headRefOid baseRefName state }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _merge_queue_entries(
    repository: str, branch: str, env: dict[str, str]
) -> list[dict]:
    """Return the complete live queue snapshot for ``branch``."""
    try:
        owner, name = repository.split("/", 1)
    except ValueError as error:
        raise AcceptanceError(f"invalid GITHUB_REPOSITORY: {repository!r}") from error
    if not owner or not name:
        raise AcceptanceError(f"invalid GITHUB_REPOSITORY: {repository!r}")

    entries: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _page in range(_MAX_PAGES):
        result = _graphql(
            _MERGE_QUEUE_QUERY,
            {"owner": owner, "name": name, "branch": branch, "cursor": cursor},
            env,
        )
        data = result.get("data") if isinstance(result, dict) else None
        repo = data.get("repository") if isinstance(data, dict) else None
        queue = repo.get("mergeQueue") if isinstance(repo, dict) else None
        connection = queue.get("entries") if isinstance(queue, dict) else None
        nodes = connection.get("nodes") if isinstance(connection, dict) else None
        page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise AcceptanceError("merge queue or its entries are unavailable")
        if any(not isinstance(node, dict) for node in nodes):
            raise AcceptanceError("merge queue contains a malformed entry")
        entries.extend(nodes)
        if len(entries) > _MAX_QUEUE_ENTRIES:
            raise AcceptanceError("merge queue exceeds the supported entry limit")
        if page_info.get("hasNextPage") is not True:
            return entries
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise AcceptanceError("merge queue pagination has no cursor")
        if next_cursor in seen_cursors:
            raise AcceptanceError("merge queue pagination repeated a cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise AcceptanceError("merge queue did not end within the page limit")


def _bind_queue_heads(
    numbers: list[int],
    expected_bases: list[str],
    expected_heads: list[str],
    branch: str,
    repository: str,
    env: dict[str, str],
) -> list[tuple[int, str]]:
    """Bind the synthetic PR sequence to exact heads in the live queue."""
    if not (len(numbers) == len(expected_bases) == len(expected_heads)):
        raise AcceptanceError("merge_group queue-binding inputs are inconsistent")
    queue_entries = _merge_queue_entries(repository, branch, env)
    by_number: dict[int, dict] = {}
    for entry in queue_entries:
        pull_request = entry.get("pullRequest")
        number = pull_request.get("number") if isinstance(pull_request, dict) else None
        if not isinstance(number, int):
            raise AcceptanceError("merge queue entry has no pull request number")
        if number in by_number:
            raise AcceptanceError(
                f"merge queue contains duplicate pull request #{number}"
            )
        by_number[number] = entry

    selected: list[tuple[int, int, str]] = []
    for number, expected_base, expected_head in zip(
        numbers, expected_bases, expected_heads, strict=True
    ):
        entry = by_number.get(number)
        if entry is None:
            raise AcceptanceError(
                f"pull request #{number} is no longer in the live merge queue"
            )
        pull_request = entry["pullRequest"]
        if pull_request.get("baseRefName") != branch:
            raise AcceptanceError(
                f"merge queue pull request #{number} targets the wrong branch"
            )
        if pull_request.get("state") != "OPEN":
            raise AcceptanceError(f"merge queue pull request #{number} is not open")
        position = entry.get("position")
        if not isinstance(position, int):
            raise AcceptanceError(f"merge queue pull request #{number} has no position")
        queued_head = entry.get("headCommit")
        queued_head_oid = (
            queued_head.get("oid") if isinstance(queued_head, dict) else None
        )
        exact_pull_head = _commit_sha(
            pull_request.get("headRefOid"), f"merge queue pull request #{number} head"
        )
        if (
            _commit_sha(queued_head_oid, f"merge queue entry #{number} synthetic head")
            != expected_head
        ):
            raise AcceptanceError(
                f"merge queue entry #{number} head disagrees with the synthetic chain"
            )
        queued_base = entry.get("baseCommit")
        queued_base_oid = (
            queued_base.get("oid") if isinstance(queued_base, dict) else None
        )
        if (
            _commit_sha(queued_base_oid, f"merge queue entry #{number} base")
            != expected_base
        ):
            raise AcceptanceError(
                f"merge queue entry #{number} base disagrees with the synthetic chain"
            )
        selected.append((position, number, exact_pull_head))

    selected.sort()
    positions = [position for position, _number, _head in selected]
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise AcceptanceError(
            "merge_group entries are not contiguous in the live queue"
        )
    ordered_numbers = [number for _position, number, _head in selected]
    if ordered_numbers != numbers:
        raise AcceptanceError("merge_group order disagrees with the live merge queue")

    return [(number, head) for _position, number, head in selected]


def _merge_group_numbers(
    payload: dict, repository: str, env: dict[str, str]
) -> tuple[list[tuple[int, str]], str]:
    """Resolve every PR represented by a validated synthetic queue chain.

    Returns ``(number_and_exact_head, base_branch)``. Commit subjects identify
    group members; GraphQL merge-queue entries authoritatively bind every one
    of those numbers to its exact queued head.
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
    expected_queue_bases: list[str] = []
    expected_queue_heads: list[str] = []
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
        expected_queue_bases.append(previous)
        expected_queue_heads.append(commit_sha)
        previous = commit_sha

    if previous != head:
        raise AcceptanceError("merge_group comparison does not end at head_sha")
    if len(set(numbers)) != len(numbers):
        raise AcceptanceError("merge_group contains a duplicate pull request number")
    if numbers[-1] != int(match.group("number")):
        raise AcceptanceError("merge_group history disagrees with its queue ref")
    return (
        _bind_queue_heads(
            numbers,
            expected_queue_bases,
            expected_queue_heads,
            base_branch,
            repository,
            env,
        ),
        base_branch,
    )


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
    if event == "merge_group":
        queued_pulls, expected_base = _merge_group_numbers(payload, repository, env)
    elif event in {"pull_request", "pull_request_review"}:
        queued_pulls = [(_pull_request_number(payload, env), None)]
    else:
        raise AcceptanceError(f"unsupported event for acceptance: {event!r}")

    evidence = []
    for number, queued_exact_head in queued_pulls:
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
        if queued_exact_head is not None and exact_head != queued_exact_head:
            raise AcceptanceError(
                f"merge_group pull request #{number} moved after the group was built"
            )
        user = pull_request.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        reviewer = evaluate(_reviews(repository, number, env), exact_head, author)
        evidence.append(f"#{number}:{reviewer}")

    if event != "merge_group":
        # Preserve the original public return contract for existing callers.
        return evidence[0].split(":", 1)[1]
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
