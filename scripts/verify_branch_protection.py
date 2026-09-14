"""Report what GitHub branch protection on a branch *actually* enforces.

Why this exists
---------------
`DR-GITHUB-TIER-ENFORCEMENT-001` (`docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md`)
records that on 2026-08-26 `gh api repos/…/branches/main/protection` returned
`required_approving_review_count` = 0, no `required_status_checks` and
`enforce_admins` = false: branch protection on `main` enforced *nothing*, and
the application-level `merge_once` gate was the only thing standing between an
ACCEPT-marked pull request and `main`.

A finding like that goes stale the moment somebody changes a repository setting
by hand — in either direction. So the record's standing rule is that no document
or audit may describe branch protection as a working control without re-checking
the API first, and this module is the re-check: it turns the protection endpoint
into a verdict with an exit code, so "is it enforced?" is answered by the API
rather than by memory, by a settings screenshot, or by a script that printed a
checkmark after issuing a write it never read back.

Three outcomes, deliberately distinct — an unreadable answer is not the same as
"nothing is enforced", which is not the same as "the requirement is met":

* exit 0 — the API was read and every stated requirement holds;
* exit 1 — the API was read and it does not: the requirement is unmet, or (with
  no requirement stated) protection enforces nothing at all;
* exit 2 — the API could not be read, so nothing was established either way.

Note the asymmetry between the two ways protection can be missing. A repository
whose plan does not expose branch protection answers 403/404 exactly like a
branch nobody has protected yet; both are reported as "not enforced" with the
API's own message quoted, because for a merge to `main` the two are the same
fact. Which of them it is, is what the decision record's Option A/Option B
choice is about, and that choice is the founder's, not this script's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__all__ = [
    "Protection",
    "ProtectionUnreadable",
    "parse_protection",
    "unmet_requirements",
    "describe",
]

_MAX_BYTES = 4 * 1024 * 1024

#: Keys that only a real protection object carries. Used to tell a protection
#: payload apart from an API error body (`{"message": "Branch not protected"}`),
#: which is JSON, parses fine, and would otherwise read as a protection object
#: with every gate switched off — the one misreading this script must not make.
_PROTECTION_KEYS = frozenset(
    {
        "required_status_checks",
        "enforce_admins",
        "required_pull_request_reviews",
        "allow_force_pushes",
        "allow_deletions",
        "restrictions",
        "required_linear_history",
        "required_conversation_resolution",
        "lock_branch",
        "block_creations",
        "url",
    }
)


class ProtectionUnreadable(RuntimeError):
    """The protection state could not be established (exit 2, never a verdict)."""


@dataclass(frozen=True, slots=True)
class Protection:
    """What the API says is enforced on one branch, normalized.

    ``protected`` is false when the endpoint answered 403/404 — the plan does
    not expose branch protection, or the branch simply has none. ``reason``
    then carries the API's own message, so a reader can tell the two apart
    without re-running the call.
    """

    protected: bool
    reason: str = ""
    required_checks: tuple[str, ...] = ()
    strict_checks: bool = False
    required_reviews: int = 0
    enforce_admins: bool = False
    allow_force_pushes: bool = True
    allow_deletions: bool = True

    @property
    def enforces_nothing(self) -> bool:
        """True when no merge into this branch is blocked by GitHub itself.

        Force-push and deletion rules are deliberately excluded: they protect
        history, not the merge decision, and a branch that only denies deletion
        still lets any writer merge anything. The gap the decision record is
        about is the *merge* gate.
        """
        return not self.required_checks and self.required_reviews < 1


def _enabled(value: object) -> bool:
    """`{"enabled": true}` and a bare `true` both mean enabled.

    The REST endpoint wraps these flags in an object, but `gh api -q` output,
    hand-written fixtures and the rulesets projection all flatten them to a
    bare boolean. Accepting both keeps a flattened copy from silently reading
    as "off" — which would report a real gate as missing.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return value.get("enabled") is True
    return False


def _permitted(value: object) -> bool:
    """Whether a permissive flag (force push, deletion) is *not* known to be off.

    Absent or unrecognized reads as permitted, which is the fail-closed
    direction for these two: they are only ever checked as "prove this is
    denied", and an unconfirmed denial must not pass as a denial.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return value["enabled"]
    return True


def _checks(value: object) -> tuple[tuple[str, ...], bool]:
    """Required check contexts and the `strict` flag, from either shape.

    GitHub returns both a legacy `contexts` list of names and a newer `checks`
    list of `{context, app_id}` objects. They normally agree; when they do not,
    their union is taken, since a check named in either field is one GitHub can
    require. Names come from those two fields only and are never inferred from
    anything else — inventing a context here would let this script bless a gate
    that does not exist, which is the failure it was written to prevent.
    """
    if not isinstance(value, dict):
        return (), False
    names: list[str] = []
    raw_contexts = value.get("contexts")
    if isinstance(raw_contexts, list):
        names.extend(item for item in raw_contexts if isinstance(item, str) and item)
    raw_checks = value.get("checks")
    if isinstance(raw_checks, list):
        for check in raw_checks:
            if isinstance(check, dict):
                context = check.get("context")
                if isinstance(context, str) and context:
                    names.append(context)
    ordered = tuple(dict.fromkeys(names))
    return ordered, value.get("strict") is True


def parse_protection(payload: object) -> Protection:
    """Normalize a protection API response into `Protection`.

    An error body (`{"message": …}`) with no protection fields is *not* a
    protection object with everything off: it is reported as unprotected with
    the message attached. A payload that is neither raises, because guessing
    at an unrecognized shape is how a gate gets described as working when it
    is not.
    """
    if not isinstance(payload, dict):
        raise ProtectionUnreadable("the protection response is not a JSON object")
    if not _PROTECTION_KEYS & payload.keys():
        message = payload.get("message")
        if isinstance(message, str) and message:
            # "Branch not protected" (404) and "Upgrade to GitHub Pro …" (403)
            # both land here: no protection, and the API says why.
            return Protection(protected=False, reason=message)
        raise ProtectionUnreadable(
            "the response carries no branch-protection fields and no API message"
        )

    reviews = payload.get("required_pull_request_reviews")
    count = reviews.get("required_approving_review_count") if isinstance(reviews, dict) else None
    required_checks, strict = _checks(payload.get("required_status_checks"))
    return Protection(
        protected=True,
        required_checks=required_checks,
        strict_checks=strict,
        # A non-int (or absent) count is 0: an unreadable review requirement
        # cannot be counted as a review requirement.
        required_reviews=count if isinstance(count, int) and not isinstance(count, bool) else 0,
        enforce_admins=_enabled(payload.get("enforce_admins")),
        # Silence about force-pushes is not a denial of them.
        allow_force_pushes=_permitted(payload.get("allow_force_pushes")),
        allow_deletions=_permitted(payload.get("allow_deletions")),
    )


@dataclass(frozen=True, slots=True)
class Requirements:
    """What the caller claims the branch enforces, to be checked against the API."""

    checks: tuple[str, ...] = ()
    reviews: int = 0
    admins: bool = False
    no_force_push: bool = False
    no_deletions: bool = False
    stated: bool = False


def unmet_requirements(protection: Protection, requirements: Requirements) -> list[str]:
    """Every stated requirement the API does not support, in reading order.

    With no requirement stated, the only question asked is the decision
    record's: does this branch block a merge at all?
    """
    if not protection.protected:
        detail = f": {protection.reason}" if protection.reason else ""
        return [f"the branch has no protection GitHub will enforce{detail}"]

    failures: list[str] = []
    for context in requirements.checks:
        if context not in protection.required_checks:
            have = ", ".join(protection.required_checks) or "none"
            failures.append(
                f"required status check {context!r} is not required (required checks: {have})"
            )
    if requirements.reviews and protection.required_reviews < requirements.reviews:
        failures.append(
            f"required_approving_review_count is {protection.required_reviews}, "
            f"expected at least {requirements.reviews}"
        )
    if requirements.admins and not protection.enforce_admins:
        failures.append("enforce_admins is false, so an admin can merge past every rule above")
    if requirements.no_force_push and protection.allow_force_pushes:
        failures.append("force pushes are allowed")
    if requirements.no_deletions and protection.allow_deletions:
        failures.append("branch deletion is allowed")

    if not requirements.stated and protection.enforces_nothing:
        failures.append(
            "branch protection exists but enforces no merge gate: no required status "
            "checks and required_approving_review_count is 0"
        )
    return failures


def describe(protection: Protection) -> str:
    """The observed state, as facts a reader can quote in an audit."""
    if not protection.protected:
        reason = protection.reason or "no protection is configured"
        return f"  protected:                        no ({reason})"
    checks = ", ".join(protection.required_checks) or "none"
    return "\n".join(
        (
            "  protected:                        yes",
            f"  required status checks:           {checks}",
            f"  strict (branch up to date):       {str(protection.strict_checks).lower()}",
            f"  required_approving_review_count:  {protection.required_reviews}",
            f"  enforce_admins:                   {str(protection.enforce_admins).lower()}",
            f"  allow_force_pushes:               {str(protection.allow_force_pushes).lower()}",
            f"  allow_deletions:                  {str(protection.allow_deletions).lower()}",
        )
    )


def _read_json_argument(source: str) -> object:
    """Protection JSON from a file, or from stdin for `-`."""
    try:
        if source == "-":
            body = sys.stdin.read(_MAX_BYTES + 1)
        else:
            with open(source, encoding="utf-8") as handle:
                body = handle.read(_MAX_BYTES + 1)
    except (OSError, UnicodeError) as error:
        raise ProtectionUnreadable(f"cannot read protection JSON from {source}") from error
    if len(body) > _MAX_BYTES:
        raise ProtectionUnreadable("the protection JSON is implausibly large")
    if not body.strip():
        raise ProtectionUnreadable(f"{source} is empty")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise ProtectionUnreadable(f"the content of {source} is not JSON") from error


def fetch_protection(repository: str, branch: str, env: dict[str, str]) -> object:
    """Read the protection endpoint directly, mirroring the acceptance gate's client."""
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        raise ProtectionUnreadable(
            "GITHUB_TOKEN (or GH_TOKEN) is required to read branch protection; "
            "without it the protection state is unknown, not absent"
        )
    base = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    request = Request(  # noqa: S310 - fixed https API host from the environment
        f"{base}/repos/{repository}/branches/{branch}/protection",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aicc-branch-protection-verifier",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - see above
            body = response.read(_MAX_BYTES + 1)
    except HTTPError as error:
        if error.code in (403, 404):
            # Not an outage: this is the API stating there is nothing to enforce
            # (or nothing this plan can enforce). It is a readable answer.
            try:
                return json.loads(error.read(_MAX_BYTES + 1).decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return {"message": f"HTTP {error.code} from the protection endpoint"}
        raise ProtectionUnreadable(
            f"the protection endpoint answered HTTP {error.code}"
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise ProtectionUnreadable("cannot reach the GitHub API") from error
    if len(body) > _MAX_BYTES:
        raise ProtectionUnreadable("the protection response is implausibly large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProtectionUnreadable("the protection response is not JSON") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify what GitHub branch protection actually enforces.",
        epilog=(
            "exit 0 = every stated requirement holds; exit 1 = it does not "
            "(see docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md); exit 2 = the "
            "protection state could not be read at all."
        ),
    )
    parser.add_argument("--repo", help="OWNER/NAME (default: $GITHUB_REPOSITORY)")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--json",
        dest="json_source",
        metavar="PATH",
        help="read the protection payload from PATH ('-' for stdin) instead of the API",
    )
    parser.add_argument(
        "--require-check",
        action="append",
        default=[],
        metavar="CONTEXT",
        help="fail unless this status check is required (repeatable)",
    )
    parser.add_argument("--require-reviews", type=int, default=0, metavar="N")
    parser.add_argument("--require-admins", action="store_true")
    parser.add_argument("--require-no-force-push", action="store_true")
    parser.add_argument("--require-no-deletions", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = dict(os.environ)

    requirements = Requirements(
        checks=tuple(args.require_check),
        reviews=args.require_reviews,
        admins=args.require_admins,
        no_force_push=args.require_no_force_push,
        no_deletions=args.require_no_deletions,
        stated=bool(
            args.require_check
            or args.require_reviews
            or args.require_admins
            or args.require_no_force_push
            or args.require_no_deletions
        ),
    )

    try:
        if args.json_source:
            payload = _read_json_argument(args.json_source)
            source = args.json_source
        else:
            repository = args.repo or env.get("GITHUB_REPOSITORY")
            if not repository:
                raise ProtectionUnreadable("--repo or GITHUB_REPOSITORY is required")
            payload = fetch_protection(repository, args.branch, env)
            source = f"{repository}@{args.branch}"
        protection = parse_protection(payload)
    except ProtectionUnreadable as error:
        print(f"branch protection is UNVERIFIED: {error}", file=sys.stderr)
        return 2

    print(f"branch protection for {source}, as the API reports it:")
    print(describe(protection))

    failures = unmet_requirements(protection, requirements)
    if failures:
        # The observed facts belong above the verdict even when the two streams
        # are read together in a terminal.
        sys.stdout.flush()
        print("NOT ENFORCED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nDo not describe branch protection as a working control on this evidence. "
            "See docs/GITHUB_TIER_ENFORCEMENT_GAP_DECISION.md (DR-GITHUB-TIER-ENFORCEMENT-001).",
            file=sys.stderr,
        )
        return 1
    print("\nENFORCED: every stated requirement is backed by the API.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
