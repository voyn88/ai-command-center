"""Shared logic: does a set of PR reviews establish independent acceptance?

Extracted out of ``scripts/assert_independent_acceptance.py`` (the CI
"Acceptance gate" check) so that script and
``command_center.orchestrator.merge_gateway`` — which must make exactly the
same judgement before it will spend its own merge credential — read the
identical policy instead of two implementations that could quietly drift
apart. The CI script keeps the parts that are genuinely its own (talking to
the Actions event payload, calling the GitHub API with ``GITHUB_TOKEN``, its
``main()``); this module keeps the part that has to be identical everywhere
it runs: given a PR's reviews, its head sha, and its author, was it
independently accepted?

The delivery rules require every pull request to be accepted by a reviewer
who is not its author, on the exact commit that will be merged. GitHub
cannot enforce that natively here, and both built-in routes were tried and
closed:

* ``required_approving_review_count`` with ``reviewDecision == APPROVED``.
  The acceptance identity is a GitHub App installation with ``contents:
  read``; its approval carries ``authorAssociation: NONE`` and GitHub only
  counts approvals from accounts with write access. Granting write access to
  the reviewer would dissolve the separation the rule exists to create.
* Self-approval. Every agent runs under the account that opens the pull
  request, and GitHub refuses an approval from the author at the API level.

So the verdict is published as a review whose **first line** is exactly::

    ACCEPTANCE: ACCEPT <40-character head sha>
    ACCEPTANCE: REJECT <40-character head sha>

and ``evaluate`` is what makes it binding. Independence is checkable here
even though it is invisible to branch protection: the app reviews under its
own ``login``, so the comparison is ``login`` against the pull request's
author ``login`` — not ``authorAssociation``, which is exactly the field
that made the built-in route unusable.

``evaluate``'s optional ``merger`` parameter closes a second, narrower gap:
publishing the verdict and executing the merge are meant to be two different
identities, not merely two different checks run by whichever identity holds
both credentials. ``command_center.orchestrator.merge_gateway`` — the only
caller that actually merges anything — always resolves its own identity
(via ``gh api user`` under its own credential) and passes it as ``merger``;
a verdict that identity published itself is refused on the same footing as
a self-issued one, because an account that can publish a verdict and then
act on it is a single identity closing the loop on itself, independent of
whether it also happens to be the pull request's author
(VOYN-W0-AICC-MARKER-REVIEWER-INDEPENDENCE). ``scripts.
assert_independent_acceptance`` never merges anything itself, so it always
calls with ``merger=None`` and only checks independence from the author.

Everything it cannot establish is a refusal: no review, no marker, a marker
for a different commit, an unparseable sha, a self-issued verdict, a
merger-issued verdict, a dismissed verdict, an active rejection. A gate that
guesses accepts nothing in particular.
"""

from __future__ import annotations

import re

__all__ = [
    "POLICY_VERSION",
    "MARKER",
    "DISMISSED",
    "PENDING",
    "SUBMITTED",
    "AcceptanceError",
    "Verdict",
    "parse_marker",
    "verdicts_from",
    "evaluate",
]

# Bumped whenever `evaluate`'s semantics change (a new required check, a
# changed independence rule, a changed marker grammar) — never for a
# refactor that leaves the judgement itself identical. `merge_gateway`
# records this string as evidence on every merge, so a later policy change
# is distinguishable, after the fact, from what actually authorized a past
# merge: two merges recorded under different versions were not necessarily
# judged by the same rules even though both read "ACCEPT".
POLICY_VERSION = "acceptance_policy_v1"

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

# The states in which a review currently represents its author's live
# position. An allowlist rather than excluding the two known non-live states
# (PENDING, DISMISSED) individually: a state this gate has never seen —
# something GitHub adds later, or a malformed API response — fails closed
# here instead of silently falling through whatever bare `!=` checks happened
# to exclude.
SUBMITTED = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED"})


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
    """Every parseable verdict in an API review list.

    Two callers feed this two differently-shaped review lists for the same
    underlying data: the REST API (`scripts.assert_independent_acceptance`,
    reading `/pulls/{number}/reviews` directly) nests the login under
    `user`; `gh pr view --json reviews` (`merge_gateway`) nests it under
    `author` instead. Accepting either keeps this the one place that decides
    "was this accepted" for both, rather than one caller silently getting a
    login of `None` because it fetched its data a different way.
    """
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
        if not isinstance(user, dict):
            user = review.get("author")
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
    merger: object | None = None,
) -> str:
    """Return the accepting reviewer's login, or raise with cause and remedy.

    `merger`, when not `None`, is the identity that would execute the merge;
    a verdict it published is refused on the same footing as a self-issued
    one. `None` means the caller itself never merges anything and
    independence from the author alone is what is being asked for; any other
    non-string or empty value means the caller *does* merge but cannot say as
    whom, which is refused rather than silently skipping the check.
    """
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise AcceptanceError(f"head sha is not a 40-character commit id: {head_sha!r}")
    head = head_sha.lower()
    if not isinstance(pull_request_author, str) or not pull_request_author:
        raise AcceptanceError("the pull request has no resolvable author, so independence is unprovable")
    author = pull_request_author.casefold()
    if merger is not None and (not isinstance(merger, str) or not merger):
        raise AcceptanceError("the merger has no resolvable identity, so independence is unprovable")
    merger_login = merger.casefold() if isinstance(merger, str) else None

    verdicts = verdicts_from(reviews)
    on_head = [verdict for verdict in verdicts if verdict.sha == head and verdict.state in SUBMITTED]

    rejections = [verdict for verdict in on_head if verdict.decision == "REJECT"]
    if rejections:
        # Checked before any acceptance: a rejection standing on the current
        # head is the reviewer's live position, and an earlier ACCEPT on the
        # same commit does not overturn it.
        who = ", ".join(sorted({verdict.author or "<unknown>" for verdict in rejections}))
        raise AcceptanceError(
            f"acceptance was REJECTED for {head} by {who}. "
            "Address the rejection, push the fix, and obtain a new verdict on the new head commit"
        )

    accepting = [
        verdict
        for verdict in on_head
        if verdict.decision == "ACCEPT"
        and verdict.author
        and verdict.author.casefold() != author
        and (merger_login is None or verdict.author.casefold() != merger_login)
    ]
    if accepting:
        return accepting[0].author

    # Nothing accepted this commit. Say which of the ways it failed, because a
    # gate whose refusal is unreadable gets routed around instead of fixed.
    self_issued = [
        verdict
        for verdict in on_head
        if verdict.decision == "ACCEPT" and verdict.author and verdict.author.casefold() == author
    ]
    if self_issued:
        raise AcceptanceError(
            f"the only ACCEPT for {head} was published by {self_issued[0].author}, who authored this "
            "pull request. Acceptance must come from an identity that is not the author; have the "
            "acceptance reviewer publish the verdict"
        )
    merger_issued = [
        verdict
        for verdict in on_head
        if verdict.decision == "ACCEPT"
        and merger_login is not None
        and verdict.author
        and verdict.author.casefold() == merger_login
    ]
    if merger_issued:
        raise AcceptanceError(
            f"the only ACCEPT for {head} was published by {merger_issued[0].author}, who would merge "
            "this pull request. Acceptance must come from an identity that is neither the author nor "
            "the merger"
        )
    # DISMISSED is excluded from `on_head` by the SUBMITTED allowlist above,
    # so this scans `verdicts` directly rather than `on_head` — otherwise a
    # dismissed ACCEPT could never be reported as dismissed, only as "no
    # acceptance verdict", which is true but hides the remedy (obtain a fresh
    # verdict, not merely publish any verdict).
    dismissed = [
        verdict
        for verdict in verdicts
        if verdict.sha == head and verdict.decision == "ACCEPT" and verdict.state == DISMISSED
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
