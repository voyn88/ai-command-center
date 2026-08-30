"""Acceptance as a Check Run's state, not as a workflow run's exit code.

The gate this replaces is fail-first by construction. On every new head SHA a
workflow starts, finds no verdict yet, and exits non-zero -- so a red required
check is the *normal* state of a pull request that is simply waiting for its
reviewer. Three deliveries this session (#474, #478, #502) each needed a manual
re-run after the marker went up, because GitHub reports the latest run for a
required context and the latest run was the one that failed before the verdict
existed.

Here the same policy drives a Check Run instead:

    new head SHA          -> create ONE check, status=in_progress
    independent ACCEPT    -> update THAT SAME check -> completed/success
    reject / stale / self -> update THAT SAME check -> completed/failure
    still waiting         -> leave it in_progress; waiting is not failure

What is deliberately NOT here: the definition of an independent acceptance.
That lives in `acceptance_policy`, shared with the workflow gate, so the two
cannot disagree about the same pull request.

Identity and safety properties this module is responsible for:

- One check per (repository, head SHA, policy version). Webhook delivery is
  at-least-once and unordered, so every handler is keyed on that triple and is
  idempotent: a duplicate delivery updates the same check to the same
  conclusion, and an out-of-order delivery for an older SHA never touches a
  newer SHA's check.
- A new SHA never inherits an older SHA's success. The check is created fresh
  and in_progress; there is no path that copies a conclusion forward.
- Merge queue is separate. A synthetic merge_group SHA gets its own check, and
  it succeeds only when every pull request in the group is independently
  accepted at its own exact head AND the group's composition matches what was
  accepted. A PR-head success is never transplanted onto the synthetic SHA.
- Fail closed. Ambiguity, an unreadable API answer, a dismissed or self-issued
  verdict, or an explicit timeout end the check as a failure with a stated
  reason. Only "no verdict yet" leaves it in_progress.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from command_center.orchestrator.acceptance_policy import (
    AcceptanceError,
    evaluate,
)

#: Bumped when the policy's meaning changes. It is part of the idempotency key
#: because a replayed webhook must not be answered from a decision made under
#: different rules -- the same SHA under a new policy is a new question.
POLICY_VERSION = "1"

#: The check's name is the required-status context. It must stay stable: branch
#: protection refers to it by this exact string, and renaming it silently makes
#: the requirement unenforced rather than failing loudly.
CHECK_NAME = "Acceptance (independent verdict on exact SHA)"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_IN_PROGRESS = "in_progress"
_COMPLETED = "completed"
_SUCCESS = "success"
_FAILURE = "failure"


class ControllerError(RuntimeError):
    """The controller could not establish what it needed to decide."""


@dataclass(frozen=True, slots=True)
class CheckKey:
    """What makes two deliveries the same question.

    `policy_version` is part of the identity on purpose: replaying an old
    webhook after the rules changed must re-decide under the new rules rather
    than resurface the old answer.
    """

    repository: str
    head_sha: str
    policy_version: str = POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.repository or "/" not in self.repository:
            raise ControllerError(f"repository must be owner/name: {self.repository!r}")
        if SHA_RE.fullmatch(self.head_sha) is None:
            raise ControllerError(
                f"head sha must be 40 lowercase hex characters: {self.head_sha!r}"
            )

    @property
    def external_id(self) -> str:
        """A stable id GitHub stores with the check.

        Recovering after a restart means finding the check that belongs to this
        exact question without trusting local state, so the identity travels
        with the check itself.
        """
        digest = hashlib.sha256(
            f"{self.repository}\0{self.head_sha}\0{self.policy_version}".encode()
        ).hexdigest()
        return f"acceptance/{self.policy_version}/{digest[:32]}"


class ChecksApi(Protocol):
    """The four Checks API calls this controller makes, and nothing more.

    Narrow on purpose: the App holds `checks: write` and read-only everything
    else, so a protocol that cannot express a write to code is a protocol that
    cannot be widened by accident.
    """

    def list_check_runs(self, repository: str, head_sha: str) -> list[dict]: ...

    def create_check_run(self, repository: str, payload: dict) -> dict: ...

    def update_check_run(
        self, repository: str, check_run_id: int, payload: dict
    ) -> dict: ...


class ReviewsApi(Protocol):
    """Read-only access to what a decision is made from."""

    def pull_request(self, repository: str, number: int) -> dict: ...

    def reviews(self, repository: str, number: int) -> list[dict]: ...

    def merge_group_pull_requests(
        self, repository: str, head_sha: str
    ) -> list[dict]: ...


@dataclass
class Decision:
    """What the controller concluded, and why.

    `summary` is written for the reviewer who must act; `cause` is what the
    controller and its metrics branch on.
    """

    conclusion: str | None
    title: str
    summary: str
    reviewer: str | None = None
    cause: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.conclusion is None


@dataclass
class ControllerMetrics:
    """Latency and failure reasons, recorded per decision.

    Kept as plain counters rather than a metrics client so the controller has
    no transport dependency; the caller exports them.
    """

    decisions: int = 0
    pending: int = 0
    successes: int = 0
    failures: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)
    latency_seconds: list[float] = field(default_factory=list)

    def record(self, decision: Decision, elapsed: float) -> None:
        self.decisions += 1
        self.latency_seconds.append(elapsed)
        if decision.is_pending:
            self.pending += 1
        elif decision.conclusion == _SUCCESS:
            self.successes += 1
        else:
            self.failures += 1
            reason = decision.cause or decision.title
            self.failure_reasons[reason] = self.failure_reasons.get(reason, 0) + 1


class AcceptanceController:
    """Drives one check run per (repository, head SHA, policy version)."""

    def __init__(
        self,
        checks: ChecksApi,
        reviews: ReviewsApi,
        *,
        clock: Callable[[], float] = time.monotonic,
        metrics: ControllerMetrics | None = None,
        audit: Callable[[dict], None] | None = None,
    ) -> None:
        self._checks = checks
        self._reviews = reviews
        self._clock = clock
        self.metrics = metrics or ControllerMetrics()
        self._audit = audit or (lambda _record: None)

    # -- check lifecycle -------------------------------------------------

    def _find_check(self, key: CheckKey) -> dict | None:
        """The check this controller already owns for this exact question.

        Looked up by `external_id` among the checks GitHub reports for the SHA,
        rather than remembered locally: after a restart the API is the state,
        and a controller that trusted its own memory would create a second
        check for a question already answered.
        """
        for run in self._checks.list_check_runs(key.repository, key.head_sha):
            if not isinstance(run, dict):
                continue
            if run.get("external_id") == key.external_id:
                return run
        return None

    def ensure_in_progress(self, key: CheckKey) -> dict:
        """Create the check for a head SHA, or return the existing one.

        Idempotent by construction: a duplicated `synchronize` delivery finds
        the check it already created. A check that has already completed is
        NOT reopened here -- re-deciding is `resolve`'s job, and resetting a
        conclusion to in_progress would make a merge-blocking failure vanish on
        a replayed webhook.
        """
        existing = self._find_check(key)
        if existing is not None:
            return existing
        created = self._checks.create_check_run(
            key.repository,
            {
                "name": CHECK_NAME,
                "head_sha": key.head_sha,
                "status": _IN_PROGRESS,
                "external_id": key.external_id,
                "output": {
                    "title": "Awaiting an independent verdict",
                    "summary": (
                        "This check stays in progress until an independent "
                        "reviewer publishes `ACCEPTANCE: ACCEPT "
                        f"{key.head_sha}` as a pull request review. Waiting is "
                        "not a failure; only a rejected, dismissed, "
                        "self-issued or stale verdict is."
                    ),
                },
            },
        )
        self._audit(
            {
                "event": "check_created",
                "external_id": key.external_id,
                "repository": key.repository,
                "head_sha": key.head_sha,
                "check_run_id": created.get("id"),
            }
        )
        return created

    def _complete(self, key: CheckKey, check: dict, decision: Decision) -> dict:
        check_id = check.get("id")
        if not isinstance(check_id, int):
            raise ControllerError(
                f"check run for {key.head_sha} has no usable id: {check_id!r}"
            )
        updated = self._checks.update_check_run(
            key.repository,
            check_id,
            {
                "status": _COMPLETED,
                "conclusion": decision.conclusion,
                "output": {"title": decision.title, "summary": decision.summary},
            },
        )
        self._audit(
            {
                "event": "check_completed",
                "external_id": key.external_id,
                "repository": key.repository,
                "head_sha": key.head_sha,
                "check_run_id": check_id,
                "conclusion": decision.conclusion,
                "title": decision.title,
                "reviewer": decision.reviewer,
            }
        )
        return updated

    # -- decisions -------------------------------------------------------

    def decide_pull_request(self, repository: str, number: int) -> Decision:
        """Apply the shared policy to a pull request's current reviews."""
        try:
            pull = self._reviews.pull_request(repository, number)
            reviews = self._reviews.reviews(repository, number)
        except Exception as exc:  # noqa: BLE001 - any API failure is ambiguity
            # Fail closed: an unreadable answer is not evidence of acceptance,
            # and leaving it pending would let a merge wait on a check that
            # will never resolve.
            return Decision(
                conclusion=_FAILURE,
                title="Acceptance could not be established",
                summary=(
                    "The controller could not read the pull request or its "
                    f"reviews, so independence is unproven: {exc}"
                ),
            )
        head = ((pull.get("head") or {}).get("sha") or "").lower()
        author = ((pull.get("user") or {}).get("login")) or ""
        try:
            reviewer = evaluate(reviews, head, author)
        except AcceptanceError as refusal:
            # `cause` is the machine-readable classification the policy
            # attaches; the prose is for the reviewer. Branching on the prose
            # would mean a reworded message silently turns waiting into a
            # premature failure -- which is exactly the defect this controller
            # exists to remove.
            if refusal.is_pending:
                return Decision(
                    conclusion=None,
                    title="Awaiting an independent verdict",
                    summary=str(refusal),
                    cause=refusal.cause,
                )
            return Decision(
                conclusion=_FAILURE,
                title="Independent acceptance refused",
                summary=str(refusal),
                cause=refusal.cause,
            )
        return Decision(
            conclusion=_SUCCESS,
            title="Independent acceptance confirmed",
            summary=(
                f"`ACCEPTANCE: ACCEPT {head}` was published by {reviewer}, who "
                "did not author this change."
            ),
            reviewer=reviewer,
        )

    def handle_pull_request_event(self, repository: str, payload: dict) -> Decision:
        """`opened` / `reopened` / `synchronize`.

        A `synchronize` carries a new head SHA, and a new SHA is a new
        question: it gets its own check, created in progress. Nothing copies
        the previous SHA's conclusion forward -- the old check stays attached
        to the old commit, where it is true.
        """
        pull = payload.get("pull_request") or {}
        number = pull.get("number")
        head = ((pull.get("head") or {}).get("sha") or "").lower()
        if not isinstance(number, int):
            raise ControllerError("pull_request payload has no usable number")
        key = CheckKey(repository=repository, head_sha=head)
        started = self._clock()
        check = self.ensure_in_progress(key)
        decision = self.decide_pull_request(repository, number)
        if not decision.is_pending:
            self._complete(key, check, decision)
        self.metrics.record(decision, self._clock() - started)
        return decision

    def handle_review_event(self, repository: str, payload: dict) -> Decision:
        """`submitted` / `edited` / `dismissed`.

        Dismissal and edits are handled by re-deciding from the pull request's
        full review set rather than by reacting to the single event: a
        dismissed ACCEPT may be replaced by an older one that still stands, and
        only the whole set can say so.
        """
        pull = payload.get("pull_request") or {}
        number = pull.get("number")
        head = ((pull.get("head") or {}).get("sha") or "").lower()
        if not isinstance(number, int):
            raise ControllerError("pull_request_review payload has no usable number")
        key = CheckKey(repository=repository, head_sha=head)
        started = self._clock()
        check = self.ensure_in_progress(key)
        decision = self.decide_pull_request(repository, number)
        if not decision.is_pending:
            self._complete(key, check, decision)
        self.metrics.record(decision, self._clock() - started)
        return decision

    def handle_merge_group_event(self, repository: str, payload: dict) -> Decision:
        """`merge_group:checks_requested` on the synthetic queue commit.

        The synthetic SHA is not any pull request's head, so no PR-head
        acceptance applies to it directly. It succeeds only if every pull
        request the group contains is independently accepted at its own exact
        head, and the group's membership is exactly what those acceptances
        cover.
        """
        group = payload.get("merge_group") or {}
        head = (group.get("head_sha") or "").lower()
        key = CheckKey(repository=repository, head_sha=head)
        started = self._clock()
        check = self.ensure_in_progress(key)
        decision = self._decide_merge_group(repository, head)
        if not decision.is_pending:
            self._complete(key, check, decision)
        self.metrics.record(decision, self._clock() - started)
        return decision

    def _decide_merge_group(self, repository: str, head_sha: str) -> Decision:
        try:
            members = self._reviews.merge_group_pull_requests(repository, head_sha)
        except Exception as exc:  # noqa: BLE001 - ambiguity fails closed
            return Decision(
                conclusion=_FAILURE,
                title="Merge group composition could not be established",
                summary=(
                    "The controller could not determine which pull requests "
                    f"this merge group contains: {exc}"
                ),
            )
        if not members:
            return Decision(
                conclusion=_FAILURE,
                title="Merge group composition could not be established",
                summary=(
                    "No pull request could be bound to this synthetic commit. "
                    "Accepting it would approve an unknown set of changes."
                ),
            )
        accepted: list[str] = []
        for member in members:
            number = member.get("number")
            member_head = (member.get("head_sha") or "").lower()
            if not isinstance(number, int) or SHA_RE.fullmatch(member_head) is None:
                return Decision(
                    conclusion=_FAILURE,
                    title="Merge group member is not identifiable",
                    summary=(
                        "A merge group entry did not name a pull request and an "
                        f"exact head commit: {member!r}"
                    ),
                )
            decision = self.decide_pull_request(repository, number)
            if decision.conclusion != _SUCCESS:
                detail = (
                    decision.summary
                    if not decision.is_pending
                    else ("it has no independent verdict yet")
                )
                return Decision(
                    conclusion=_FAILURE,
                    title="A queued pull request is not independently accepted",
                    summary=(
                        f"#{number} at {member_head} cannot enter the queue: {detail}"
                    ),
                )
            accepted.append(f"#{number}@{member_head[:12]}")
        return Decision(
            conclusion=_SUCCESS,
            title="Every queued pull request is independently accepted",
            summary=(
                "Each pull request in this merge group carries an independent "
                "acceptance for its own exact head: " + ", ".join(accepted)
            ),
        )

    # -- reconciliation --------------------------------------------------

    def reconcile(self, repository: str, head_sha: str, number: int) -> Decision:
        """Re-establish the check for one head after a restart or a missed event.

        Webhook delivery is at-least-once, which also means it is
        at-most-never: a delivery can be dropped entirely. Reconciliation makes
        the controller's correctness independent of any single delivery by
        re-deriving the answer from the API.
        """
        key = CheckKey(repository=repository, head_sha=head_sha.lower())
        started = self._clock()
        check = self.ensure_in_progress(key)
        decision = self.decide_pull_request(repository, number)
        if not decision.is_pending:
            self._complete(key, check, decision)
        self.metrics.record(decision, self._clock() - started)
        return decision

    def fail_timed_out(self, key: CheckKey, waited_seconds: float) -> Decision:
        """End a check that waited past its policy deadline.

        An explicit timeout is a decision, not an accident: it says the verdict
        did not arrive in the window the policy allows, and it says so on the
        check rather than leaving a pull request in_progress forever.
        """
        check = self._find_check(key)
        if check is None:
            raise ControllerError(
                f"no controller-owned check to time out for {key.head_sha}"
            )
        decision = Decision(
            conclusion=_FAILURE,
            title="Timed out awaiting an independent verdict",
            summary=(
                f"No independent verdict for {key.head_sha} arrived within "
                f"{waited_seconds:.0f}s. Obtain a fresh review; the check "
                "re-opens on the next head commit."
            ),
        )
        self._complete(key, check, decision)
        self.metrics.record(decision, 0.0)
        return decision


def audit_line(record: dict) -> str:
    """One durable audit record per decision, as a JSON line."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))
