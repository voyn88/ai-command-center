"""What counts as an independent acceptance of an exact commit.

This is the policy alone: which review bodies carry a verdict, whose verdict
counts, and what disqualifies one. It performs no I/O and knows nothing about
GitHub's API, so both callers can share one definition of independence instead
of drifting apart:

- ``scripts/assert_independent_acceptance.py`` -- the workflow gate, which
  fetches reviews and applies this policy to them;
- ``command_center/orchestrator/acceptance_controller.py`` -- the GitHub App
  controller, which applies the same policy to webhook payloads and reports the
  result through one Check Run per head SHA
  (VOYN-W0-AICC-ACCEPTANCE-CONTROLLER-APP).

Extracted rather than reimplemented deliberately. Two independent definitions
of "independent acceptance" would be the worst outcome of adding a controller:
the gate and the controller could disagree about the same pull request, and
whichever ran last would decide.
"""

from __future__ import annotations

import re

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


#: Machine-readable refusal causes. The prose in each refusal is written for
#: the reviewer who has to act on it; these are for the callers that have to
#: BRANCH on it. The acceptance controller must distinguish "no verdict yet"
#: (a check that stays in progress) from "this verdict is disqualified" (a
#: check that fails), and deciding that by matching substrings of the prose is
#: how a reworded message silently turns waiting into a premature failure.
NO_VERDICT_YET = "no_verdict_yet"
VERDICT_IS_STALE = "verdict_is_stale"
REJECTED = "rejected"
SELF_ISSUED = "self_issued"
DISMISSED_VERDICT = "dismissed"
MALFORMED_INPUT = "malformed_input"

#: Causes that mean the verdict has not arrived yet, as opposed to a verdict
#: that arrived and does not count. Only these leave a check in progress.
PENDING_CAUSES = frozenset({NO_VERDICT_YET, VERDICT_IS_STALE})


class AcceptanceError(RuntimeError):
    """The pull request may not merge on the evidence available.

    `cause` is the machine-readable reason; `str(...)` stays the human one, so
    existing callers and the gate's output are unchanged.
    """

    def __init__(self, message: str, cause: str = MALFORMED_INPUT) -> None:
        super().__init__(message)
        self.cause = cause

    @property
    def is_pending(self) -> bool:
        """True when the verdict simply has not been published yet."""
        return self.cause in PENDING_CAUSES


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
        raise AcceptanceError("review list is not an array", MALFORMED_INPUT)
    found = []
    for review in reviews:
        if not isinstance(review, dict):
            raise AcceptanceError("review entry is not an object", MALFORMED_INPUT)
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
        raise AcceptanceError(
            f"head sha is not a 40-character commit id: {head_sha!r}", MALFORMED_INPUT
        )
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
            "Address the rejection, push the fix, and obtain a new verdict on the new head commit",
            REJECTED,
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
            "acceptance reviewer publish the verdict",
            SELF_ISSUED,
        )
    dismissed = [
        verdict
        for verdict in on_head
        if verdict.decision == "ACCEPT" and verdict.state == DISMISSED
    ]
    if dismissed:
        raise AcceptanceError(
            f"the ACCEPT for {head} was dismissed and no longer stands. Obtain a fresh verdict",
            DISMISSED_VERDICT,
        )
    stale = sorted({verdict.sha for verdict in verdicts if verdict.sha != head})
    if stale:
        raise AcceptanceError(
            f"no verdict names the current head {head}; the newest verdict on record is for "
            f"{', '.join(stale)}. Acceptance is per commit — re-run acceptance against the current "
            "head and publish a verdict naming it",
            VERDICT_IS_STALE,
        )
    raise AcceptanceError(
        f"no acceptance verdict for {head}. A reviewer other than the author must publish a review "
        f"whose first line is exactly `ACCEPTANCE: ACCEPT {head}`",
        NO_VERDICT_YET,
    )
