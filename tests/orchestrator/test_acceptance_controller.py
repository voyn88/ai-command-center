"""The acceptance controller's contract, one test per promise.

The gate this replaces was fail-first: a pull request waiting for its reviewer
carried a RED required check, and the marker's arrival did not clear it without
a manual re-run. Every test here exists because that behaviour must not come
back in a new shape.

The GitHub side is a fake that records calls and enforces the parts of the
Checks API this controller depends on (a check is created once, updated by id,
and carries an external_id). Nothing here talks to GitHub: the proof is about
the controller's decisions, and a fake makes the adversarial cases -- duplicate
and reordered deliveries, a restart, a merge group that does not match --
reachable at all.
"""

from __future__ import annotations

import pytest

from command_center.orchestrator.acceptance_controller import (
    CHECK_NAME,
    AcceptanceController,
    CheckKey,
    ControllerError,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
REPO = "voyn88/ai-command-center"
AUTHOR = "the-author"
REVIEWER = "voyn88-acceptance-gate[bot]"


class FakeChecks:
    """Enough of the Checks API to hold the controller to its contract."""

    def __init__(self) -> None:
        self.runs: dict[int, dict] = {}
        self._next_id = 1
        self.created: list[dict] = []
        self.updated: list[tuple[int, dict]] = []

    def list_check_runs(self, repository: str, head_sha: str) -> list[dict]:
        return [r for r in self.runs.values() if r["head_sha"] == head_sha]

    def create_check_run(self, repository: str, payload: dict) -> dict:
        # GitHub would happily create a second check with the same name; the
        # controller must not ask it to, so the fake records every creation and
        # the tests assert on the count.
        run = dict(payload)
        run["id"] = self._next_id
        self._next_id += 1
        self.runs[run["id"]] = run
        self.created.append(run)
        return run

    def update_check_run(
        self, repository: str, check_run_id: int, payload: dict
    ) -> dict:
        if check_run_id not in self.runs:
            raise AssertionError(f"update of unknown check run {check_run_id}")
        self.runs[check_run_id].update(payload)
        self.updated.append((check_run_id, payload))
        return self.runs[check_run_id]


class FakeReviews:
    def __init__(self, head: str = SHA_A, author: str = AUTHOR) -> None:
        self.head = head
        self.author = author
        self.review_list: list[dict] = []
        self.members: list[dict] = []
        self.fail_with: Exception | None = None

    def pull_request(self, repository: str, number: int) -> dict:
        if self.fail_with:
            raise self.fail_with
        return {
            "number": number,
            "head": {"sha": self.head},
            "user": {"login": self.author},
        }

    def reviews(self, repository: str, number: int) -> list[dict]:
        if self.fail_with:
            raise self.fail_with
        return self.review_list

    def merge_group_pull_requests(self, repository: str, head_sha: str) -> list[dict]:
        if self.fail_with:
            raise self.fail_with
        return self.members


def _accept(sha: str, author: str = REVIEWER, state: str = "COMMENTED") -> dict:
    return {
        "body": f"ACCEPTANCE: ACCEPT {sha}",
        "user": {"login": author},
        "state": state,
        "commit_id": sha,
        "submitted_at": "2026-08-30T00:00:00Z",
    }


def _pr_event(sha: str = SHA_A, number: int = 1) -> dict:
    return {"pull_request": {"number": number, "head": {"sha": sha}}}


def _controller(reviews: FakeReviews) -> tuple[AcceptanceController, FakeChecks]:
    checks = FakeChecks()
    return AcceptanceController(checks, reviews), checks


def test_a_new_head_gets_exactly_one_in_progress_check_and_no_failure():
    """The whole point. A pull request awaiting its reviewer is not failing."""
    reviews = FakeReviews()
    controller, checks = _controller(reviews)

    decision = controller.handle_pull_request_event(REPO, _pr_event())

    assert len(checks.created) == 1
    created = checks.created[0]
    assert created["name"] == CHECK_NAME
    assert created["status"] == "in_progress"
    assert created["head_sha"] == SHA_A
    assert checks.updated == [], "waiting must not write a conclusion"
    assert decision.is_pending
    assert decision.cause == "no_verdict_yet"


def test_an_independent_accept_updates_the_same_check_to_success():
    reviews = FakeReviews()
    controller, checks = _controller(reviews)
    controller.handle_pull_request_event(REPO, _pr_event())
    created_id = checks.created[0]["id"]

    reviews.review_list = [_accept(SHA_A)]
    decision = controller.handle_review_event(REPO, _pr_event())

    assert len(checks.created) == 1, "the verdict must not create a second check"
    assert [cid for cid, _ in checks.updated] == [created_id]
    assert checks.runs[created_id]["status"] == "completed"
    assert checks.runs[created_id]["conclusion"] == "success"
    assert decision.reviewer == REVIEWER


def test_a_new_sha_never_inherits_the_previous_success():
    """Acceptance is per commit. A push after an ACCEPT starts over."""
    reviews = FakeReviews()
    controller, checks = _controller(reviews)
    reviews.review_list = [_accept(SHA_A)]
    controller.handle_pull_request_event(REPO, _pr_event(SHA_A))
    first_id = checks.created[0]["id"]
    assert checks.runs[first_id]["conclusion"] == "success"

    # The push: new head, and the old ACCEPT names the old commit.
    reviews.head = SHA_B
    decision = controller.handle_pull_request_event(REPO, _pr_event(SHA_B))

    assert len(checks.created) == 2
    second = checks.created[1]
    assert second["head_sha"] == SHA_B
    assert second["status"] == "in_progress"
    assert second["id"] != first_id
    assert "conclusion" not in second
    assert decision.is_pending and decision.cause == "verdict_is_stale"
    assert checks.runs[first_id]["conclusion"] == "success", (
        "the old check stays true of the old commit"
    )


@pytest.mark.parametrize(
    "reviews_payload, expected_cause",
    [
        ([_accept(SHA_A, author=AUTHOR)], "self_issued"),
        ([_accept(SHA_A, state="DISMISSED")], "dismissed"),
        (
            [
                {
                    "body": f"ACCEPTANCE: REJECT {SHA_A}",
                    "user": {"login": REVIEWER},
                    "state": "COMMENTED",
                    "commit_id": SHA_A,
                }
            ],
            "rejected",
        ),
    ],
)
def test_disqualified_verdicts_fail_the_check(reviews_payload, expected_cause):
    """Self-issued, dismissed and rejected are decisions, not waiting."""
    reviews = FakeReviews()
    reviews.review_list = reviews_payload
    controller, checks = _controller(reviews)

    decision = controller.handle_review_event(REPO, _pr_event())

    check_id = checks.created[0]["id"]
    assert checks.runs[check_id]["conclusion"] == "failure"
    assert decision.cause == expected_cause


def test_a_malformed_marker_is_not_an_acceptance():
    """Prose that mentions a verdict is not a verdict."""
    reviews = FakeReviews()
    reviews.review_list = [
        {
            "body": f"I would say ACCEPTANCE: ACCEPT {SHA_A} if it were ready",
            "user": {"login": REVIEWER},
            "state": "COMMENTED",
            "commit_id": SHA_A,
        }
    ]
    controller, checks = _controller(reviews)

    decision = controller.handle_review_event(REPO, _pr_event())

    assert decision.conclusion != "success"
    assert decision.is_pending, "an unparsed body means no verdict has arrived"
    assert checks.updated == []


def test_duplicate_deliveries_are_idempotent():
    """Webhook delivery is at-least-once."""
    reviews = FakeReviews()
    controller, checks = _controller(reviews)

    for _ in range(3):
        controller.handle_pull_request_event(REPO, _pr_event())
    reviews.review_list = [_accept(SHA_A)]
    for _ in range(3):
        controller.handle_review_event(REPO, _pr_event())

    assert len(checks.created) == 1
    assert {cid for cid, _ in checks.updated} == {checks.created[0]["id"]}
    assert checks.runs[checks.created[0]["id"]]["conclusion"] == "success"


def test_a_reordered_older_delivery_does_not_touch_the_newer_head():
    """Deliveries can arrive out of order; each SHA owns its own check."""
    reviews = FakeReviews()
    controller, checks = _controller(reviews)
    reviews.review_list = [_accept(SHA_A)]
    controller.handle_pull_request_event(REPO, _pr_event(SHA_A))
    old_id = checks.created[0]["id"]

    reviews.head = SHA_B
    reviews.review_list = []
    controller.handle_pull_request_event(REPO, _pr_event(SHA_B))
    new_id = checks.created[1]["id"]

    # The stale delivery for the OLD head arrives late.
    reviews.head = SHA_A
    reviews.review_list = [_accept(SHA_A)]
    controller.handle_review_event(REPO, _pr_event(SHA_A))

    assert checks.runs[new_id]["status"] == "in_progress"
    assert "conclusion" not in checks.runs[new_id], (
        "a late delivery for an older commit must not conclude the newer check"
    )
    assert checks.runs[old_id]["conclusion"] == "success"


def test_an_unreadable_api_fails_closed():
    """Ambiguity is not evidence of acceptance."""
    reviews = FakeReviews()
    reviews.fail_with = RuntimeError("502 from GitHub")
    controller, checks = _controller(reviews)

    decision = controller.handle_pull_request_event(REPO, _pr_event())

    assert decision.conclusion == "failure"
    assert "could not read" in decision.summary
    assert checks.runs[checks.created[0]["id"]]["conclusion"] == "failure"


def test_reconciliation_rebuilds_state_without_local_memory():
    """After a restart the API is the state."""
    reviews = FakeReviews()
    checks = FakeChecks()
    first = AcceptanceController(checks, reviews)
    first.handle_pull_request_event(REPO, _pr_event())
    created_id = checks.created[0]["id"]

    # A brand-new controller instance: no memory of the check it must finish.
    reviews.review_list = [_accept(SHA_A)]
    second = AcceptanceController(checks, reviews)
    decision = second.reconcile(REPO, SHA_A, 1)

    assert len(checks.created) == 1, "reconciliation must adopt, not duplicate"
    assert checks.runs[created_id]["conclusion"] == "success"
    assert decision.conclusion == "success"


def test_a_timeout_ends_the_check_with_a_stated_reason():
    reviews = FakeReviews()
    controller, checks = _controller(reviews)
    key = CheckKey(repository=REPO, head_sha=SHA_A)
    controller.ensure_in_progress(key)

    decision = controller.fail_timed_out(key, 3600)

    assert decision.conclusion == "failure"
    assert "3600s" in decision.summary
    assert checks.runs[checks.created[0]["id"]]["conclusion"] == "failure"


def test_the_merge_group_check_requires_every_member_to_be_accepted():
    reviews = FakeReviews()
    controller, checks = _controller(reviews)
    reviews.members = [{"number": 1, "head_sha": SHA_A}]
    reviews.review_list = [_accept(SHA_A)]

    decision = controller.handle_merge_group_event(
        REPO, {"merge_group": {"head_sha": "c" * 40}}
    )

    assert decision.conclusion == "success"
    assert checks.created[0]["head_sha"] == "c" * 40


def test_a_merge_group_member_without_acceptance_blocks_the_group():
    reviews = FakeReviews()
    controller, checks = _controller(reviews)
    reviews.members = [{"number": 1, "head_sha": SHA_A}]
    reviews.review_list = []  # nobody accepted it

    decision = controller.handle_merge_group_event(
        REPO, {"merge_group": {"head_sha": "c" * 40}}
    )

    assert decision.conclusion == "failure"
    assert "#1" in decision.summary


def test_an_unresolvable_merge_group_never_succeeds():
    """A synthetic commit whose membership is unknown approves an unknown set."""
    reviews = FakeReviews()
    controller, _checks = _controller(reviews)
    reviews.members = []

    decision = controller.handle_merge_group_event(
        REPO, {"merge_group": {"head_sha": "c" * 40}}
    )

    assert decision.conclusion == "failure"
    assert "unknown set" in decision.summary


def test_a_pr_head_success_is_never_transplanted_onto_the_synthetic_sha():
    """The synthetic commit is not any pull request's head."""
    reviews = FakeReviews()
    controller, checks = _controller(reviews)
    reviews.review_list = [_accept(SHA_A)]
    controller.handle_pull_request_event(REPO, _pr_event(SHA_A))

    # The queue asks about a synthetic commit, and the membership cannot be
    # established. The PR's own success must not answer for it.
    reviews.members = []
    decision = controller.handle_merge_group_event(
        REPO, {"merge_group": {"head_sha": "c" * 40}}
    )

    assert decision.conclusion == "failure"
    synthetic = [c for c in checks.created if c["head_sha"] == "c" * 40]
    assert len(synthetic) == 1


def test_the_check_key_refuses_an_unusable_identity():
    with pytest.raises(ControllerError):
        CheckKey(repository=REPO, head_sha="not-a-sha")
    with pytest.raises(ControllerError):
        CheckKey(repository="no-slash", head_sha=SHA_A)


def test_the_policy_version_is_part_of_the_identity():
    """A replay after a policy change is a new question, not an old answer."""
    one = CheckKey(repository=REPO, head_sha=SHA_A, policy_version="1")
    two = CheckKey(repository=REPO, head_sha=SHA_A, policy_version="2")
    assert one.external_id != two.external_id


def test_metrics_record_latency_and_the_machine_readable_cause():
    reviews = FakeReviews()
    reviews.review_list = [_accept(SHA_A, author=AUTHOR)]
    controller, _checks = _controller(reviews)

    controller.handle_review_event(REPO, _pr_event())

    assert controller.metrics.failures == 1
    assert controller.metrics.failure_reasons == {"self_issued": 1}
    assert len(controller.metrics.latency_seconds) == 1


def test_the_controller_never_adopts_a_check_it_does_not_own():
    """A commit carries many checks; only one of them is this controller's.

    Written after a mutation test caught the gap: dropping the `external_id`
    comparison in `_find_check` left every existing test green, because each
    SHA happened to have exactly one check. Without that comparison the
    controller would adopt whatever check GitHub listed first for the commit --
    `Final merge gate`, a shard, another App's -- and drive it to a conclusion
    it has no business writing.
    """
    reviews = FakeReviews()
    checks = FakeChecks()
    # Somebody else's check, already completed, on the very same commit.
    foreign = checks.create_check_run(
        REPO,
        {
            "name": "Final merge gate",
            "head_sha": SHA_A,
            "status": "completed",
            "conclusion": "success",
            "external_id": "some-other-app/1",
        },
    )
    controller = AcceptanceController(checks, reviews)

    controller.handle_pull_request_event(REPO, _pr_event(SHA_A))

    assert len(checks.created) == 2, "the controller must create its own check"
    mine = checks.created[1]
    assert mine["name"] == CHECK_NAME
    assert mine["external_id"].startswith("acceptance/")
    assert checks.runs[foreign["id"]]["name"] == "Final merge gate"
    assert checks.updated == [], "the foreign check must be untouched"


def test_a_check_from_an_older_policy_version_is_not_adopted():
    """A policy change makes the same commit a new question.

    The old check keeps its conclusion, because it is a true statement about
    what the old policy decided; the new policy gets its own check rather than
    silently inheriting that answer.
    """
    reviews = FakeReviews()
    checks = FakeChecks()
    stale_key = CheckKey(repository=REPO, head_sha=SHA_A, policy_version="0")
    checks.create_check_run(
        REPO,
        {
            "name": CHECK_NAME,
            "head_sha": SHA_A,
            "status": "completed",
            "conclusion": "success",
            "external_id": stale_key.external_id,
        },
    )
    controller = AcceptanceController(checks, reviews)

    controller.handle_pull_request_event(REPO, _pr_event(SHA_A))

    assert len(checks.created) == 2
    assert checks.created[1]["status"] == "in_progress"
    assert checks.updated == []


# -- findings from the independent review of 708afbb ----------------------


def test_a_foreign_check_with_a_guessed_external_id_is_not_adopted():
    """`external_id` is derived from public facts, so it is guessable.

    Anyone able to create a check on the commit can produce the same value.
    Ownership therefore also requires our published name -- otherwise another
    producer could plant a check and have this controller drive it, deciding
    what our required context reports.
    """
    reviews = FakeReviews()
    checks = FakeChecks()
    stolen = CheckKey(repository=REPO, head_sha=SHA_A).external_id
    checks.create_check_run(
        REPO,
        {
            "name": "Some other producer's check",
            "head_sha": SHA_A,
            "status": "completed",
            "conclusion": "success",
            "external_id": stolen,
        },
    )
    controller = AcceptanceController(checks, reviews)

    controller.handle_pull_request_event(REPO, _pr_event(SHA_A))

    assert len(checks.created) == 2
    assert checks.created[1]["name"] == CHECK_NAME
    assert checks.updated == [], "the planted check must not be driven"


def test_a_check_created_by_another_app_is_not_adopted():
    reviews = FakeReviews()
    checks = FakeChecks()
    key = CheckKey(repository=REPO, head_sha=SHA_A)
    checks.create_check_run(
        REPO,
        {
            "name": CHECK_NAME,
            "head_sha": SHA_A,
            "status": "in_progress",
            "external_id": key.external_id,
            "app": {"id": 999},
        },
    )
    controller = AcceptanceController(checks, reviews, app_id=4685414)

    controller.handle_pull_request_event(REPO, _pr_event(SHA_A))

    assert len(checks.created) == 2, "a check from another App is not ours"


def test_two_checks_for_one_question_fail_closed():
    """Two owned checks means two controllers, or a lost race.

    Picking one arbitrarily would let whichever the API listed first decide a
    required context. Refusing blocks the merge until a human looks, which is
    the safe direction.
    """
    reviews = FakeReviews()
    checks = FakeChecks()
    key = CheckKey(repository=REPO, head_sha=SHA_A)
    for _ in range(2):
        checks.create_check_run(
            REPO,
            {
                "name": CHECK_NAME,
                "head_sha": SHA_A,
                "status": "in_progress",
                "external_id": key.external_id,
            },
        )
    controller = AcceptanceController(checks, reviews)

    with pytest.raises(ControllerError, match="ambiguous"):
        controller.ensure_in_progress(key)


def test_a_malformed_pull_request_shape_fails_closed_rather_than_crashing():
    """A 200 response is not a promise about shape."""

    class Malformed(FakeReviews):
        def pull_request(self, repository: str, number: int):
            return "not an object"

    controller, checks = _controller(Malformed())

    decision = controller.handle_pull_request_event(REPO, _pr_event())

    assert decision.conclusion == "failure"
    assert checks.runs[checks.created[0]["id"]]["conclusion"] == "failure"


def test_a_merge_group_member_that_has_moved_since_queueing_blocks_the_group():
    """The queue entry names a commit; the pull request must still be at it."""
    reviews = FakeReviews()
    reviews.head = SHA_B  # the PR has moved on
    reviews.members = [{"number": 1, "head_sha": SHA_A}]
    reviews.review_list = [_accept(SHA_A)]
    controller, _checks = _controller(reviews)

    decision = controller.handle_merge_group_event(
        REPO, {"merge_group": {"head_sha": "c" * 40}}
    )

    assert decision.conclusion == "failure"
    assert "no longer matches" in decision.title


def test_a_malformed_merge_group_membership_fails_closed():
    class Malformed(FakeReviews):
        def merge_group_pull_requests(self, repository: str, head_sha: str):
            return {"not": "a list"}

    controller, _checks = _controller(Malformed())

    decision = controller.handle_merge_group_event(
        REPO, {"merge_group": {"head_sha": "c" * 40}}
    )

    assert decision.conclusion == "failure"
    assert "not a list" in decision.summary


def test_a_merge_group_entry_that_is_not_an_object_fails_closed():
    reviews = FakeReviews()
    reviews.members = ["#1"]
    controller, _checks = _controller(reviews)

    decision = controller.handle_merge_group_event(
        REPO, {"merge_group": {"head_sha": "c" * 40}}
    )

    assert decision.conclusion == "failure"
    assert "not an object" in decision.summary
