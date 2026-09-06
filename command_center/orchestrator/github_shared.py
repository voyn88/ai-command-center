"""GitHub read-shape helpers shared by `review_merge.py` and
`merge_gateway.py`.

Both modules need to parse a PR URL into `(owner, repo, number)` and decide
whether a check-rollup entry is definitively green -- and both used to
define their own copies. `merge_gateway.py` is deliberately independent of
`review_merge.py`'s own ambient-`gh`-credential reads (see its module
docstring), which means it cannot import from `review_merge` without
creating an import cycle the other way (`review_merge.merge_once` calls
into `merge_gateway.evaluate_and_merge`). Pulling the shared, credential-
and transport-agnostic parsing logic out here breaks that cycle and, as a
second-order benefit, guarantees the two verification paths agree on what
"green" and "which PR" mean -- a divergence between them is exactly the
kind of gap a privileged gateway exists to not have.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["PR_URL", "check_is_green", "latest_checks_by_name", "owner_repo_number_from_pr_url"]

PR_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")


def owner_repo_number_from_pr_url(pr_url: str) -> tuple[str, str, str] | None:
    """(owner, repo, pr_number) for a GitHub REST API path, or None if
    `pr_url` is not a well-formed `https://github.com/<owner>/<repo>/pull/
    <number>` URL."""
    match = PR_URL.match(pr_url)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def check_is_green(check: dict[str, Any]) -> bool:
    """A single check-rollup entry is green iff it is DEFINITIVELY
    successful -- never on absence of information. GitHub mixes two shapes:
    a CheckRun (`status`: QUEUED/IN_PROGRESS/COMPLETED, `conclusion` set
    only once `status == COMPLETED`) and a legacy StatusContext (`state`:
    PENDING/SUCCESS/FAILURE/ERROR, no `status`/`conclusion` keys at all).
    Treating a missing `conclusion` as passing would silently wave through
    a still-queued or still-running required check on either shape. Fail
    closed: anything not explicitly SUCCESS (via `conclusion`) or SUCCESS
    (via legacy `state`) is not green."""
    conclusion = check.get("conclusion")
    if conclusion is not None:
        return conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED")
    state = check.get("state")
    if state is not None:
        return state == "SUCCESS"
    return False


def latest_checks_by_name(rollup: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the latest run for every check name, failing closed on
    ambiguity.

    GitHub retains reruns in a check rollup. A prior failure must not block
    the latest successful run, and a prior success must not mask the latest
    pending or failed run. Duplicate runs without timestamps cannot be
    ordered safely, so they remain non-green.
    """
    latest: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for check in rollup:
        name = str(check.get("name") or "?")
        previous = latest.get(name)
        if previous is None:
            latest[name] = check
            continue
        previous_at = previous.get("startedAt") or previous.get("completedAt")
        current_at = check.get("startedAt") or check.get("completedAt")
        if not previous_at or not current_at:
            ambiguous.add(name)
            continue
        if str(current_at) == str(previous_at):
            ambiguous.add(name)
        elif str(current_at) > str(previous_at):
            latest[name] = check
    for name in ambiguous:
        latest[name] = {"name": name, "conclusion": "AMBIGUOUS"}
    return list(latest.values())
