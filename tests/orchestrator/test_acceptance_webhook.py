"""Webhook intake: authenticity, idempotency, ordering safety.

GitHub delivers at-least-once and unordered. These tests treat both as
adversarial input rather than as rare weather, because both are ordinary.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from command_center.orchestrator.acceptance_controller import AcceptanceController
from command_center.orchestrator.acceptance_webhook import (
    AcceptanceWebhook,
    WebhookRefused,
    parse_delivery,
    verify_signature,
)
from tests.orchestrator.test_acceptance_controller import (
    REPO,
    REVIEWER,
    SHA_A,
    SHA_B,
    FakeChecks,
    FakeReviews,
    _accept,
)

SECRET = "a-webhook-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _body(event_payload: dict) -> bytes:
    payload = dict(event_payload)
    payload.setdefault("repository", {"full_name": REPO})
    return json.dumps(payload).encode()


def _headers(body: bytes, event: str, delivery: str = "d-1") -> dict[str, str]:
    return {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


def _webhook(reviews: FakeReviews) -> tuple[AcceptanceWebhook, FakeChecks]:
    checks = FakeChecks()
    controller = AcceptanceController(checks, reviews)
    return AcceptanceWebhook(controller, SECRET), checks


def test_an_unsigned_delivery_is_refused_before_it_is_parsed():
    body = _body(
        {"action": "opened", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    hook, checks = _webhook(FakeReviews())

    with pytest.raises(WebhookRefused, match="no sha256 signature"):
        hook.handle({"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d"}, body)

    assert checks.created == [], "an unverified delivery must not reach the API"


def test_a_wrongly_signed_delivery_is_refused():
    body = _body(
        {"action": "opened", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    headers = _headers(body, "pull_request")
    headers["X-Hub-Signature-256"] = _sign(body, "the-wrong-secret")
    hook, checks = _webhook(FakeReviews())

    with pytest.raises(WebhookRefused, match="does not match"):
        hook.handle(headers, body)
    assert checks.created == []


def test_an_absent_secret_refuses_everything():
    """A controller deployed without its secret must fail closed, not open."""
    body = _body(
        {"action": "opened", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    with pytest.raises(WebhookRefused, match="no webhook secret"):
        verify_signature("", body, _sign(body))


def test_a_valid_delivery_creates_exactly_one_in_progress_check():
    body = _body(
        {"action": "opened", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    hook, checks = _webhook(FakeReviews())

    decision = hook.handle(_headers(body, "pull_request"), body)

    assert decision is not None and decision.is_pending
    assert len(checks.created) == 1
    assert checks.created[0]["status"] == "in_progress"


def test_a_replayed_delivery_reaches_the_same_conclusion_without_a_second_check():
    reviews = FakeReviews()
    reviews.review_list = [_accept(SHA_A)]
    hook, checks = _webhook(reviews)
    body = _body(
        {"action": "submitted", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    headers = _headers(body, "pull_request_review", delivery="same-delivery")

    first = hook.handle(headers, body)
    second = hook.handle(headers, body)

    assert first.conclusion == second.conclusion == "success"
    assert len(checks.created) == 1
    assert hook.duplicates == 1, "a replay must be visible, not silent"


def test_a_late_delivery_for_an_older_head_cannot_conclude_the_newer_check():
    """Routing is by the SHA the event names, never by 'the current head'."""
    reviews = FakeReviews()
    hook, checks = _webhook(reviews)

    reviews.review_list = [_accept(SHA_A)]
    old = _body(
        {"action": "opened", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    hook.handle(_headers(old, "pull_request", "d-old"), old)

    reviews.head = SHA_B
    reviews.review_list = []
    new = _body(
        {"action": "synchronize", "pull_request": {"number": 1, "head": {"sha": SHA_B}}}
    )
    hook.handle(_headers(new, "pull_request", "d-new"), new)

    # The review for the OLD commit arrives after the push. Note what the fake
    # reports: the pull request's CURRENT head is SHA_B, because the branch has
    # already moved. Only the event still names SHA_A. A controller that asked
    # the API "what is this PR's head?" would answer for SHA_B and conclude the
    # new check from a verdict that names the old commit -- so the fake is left
    # telling the truth about the present, and the event is the only source of
    # the commit under decision.
    reviews.review_list = [_accept(SHA_A)]
    late = _body(
        {"action": "submitted", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    hook.handle(_headers(late, "pull_request_review", "d-late"), late)

    by_sha = {run["head_sha"]: run for run in checks.runs.values()}
    assert by_sha[SHA_B]["status"] == "in_progress"
    assert "conclusion" not in by_sha[SHA_B]
    assert by_sha[SHA_A]["conclusion"] == "success"


def test_unrelated_actions_and_events_are_ignored_without_touching_checks():
    hook, checks = _webhook(FakeReviews())
    for event, action in (
        ("pull_request", "labeled"),
        ("pull_request_review", "requested"),
        ("merge_group", "destroyed"),
        ("push", None),
    ):
        body = _body(
            {"action": action, "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
        )
        assert hook.handle(_headers(body, event, f"d-{event}-{action}"), body) is None
    assert checks.created == []
    assert hook.ignored == 4


def test_a_merge_group_delivery_is_routed_to_the_synthetic_sha():
    reviews = FakeReviews()
    reviews.members = [{"number": 1, "head_sha": SHA_A}]
    reviews.review_list = [_accept(SHA_A)]
    hook, checks = _webhook(reviews)
    synthetic = "c" * 40
    body = _body({"action": "checks_requested", "merge_group": {"head_sha": synthetic}})

    decision = hook.handle(_headers(body, "merge_group", "d-mg"), body)

    assert decision.conclusion == "success"
    assert checks.created[0]["head_sha"] == synthetic


def test_a_delivery_without_a_repository_is_refused():
    body = json.dumps({"action": "opened"}).encode()
    hook, _checks = _webhook(FakeReviews())
    with pytest.raises(WebhookRefused, match="no repository"):
        hook.handle(_headers(body, "pull_request"), body)


def test_a_delivery_without_an_id_is_refused():
    """The delivery id is how a replay is recognised at all."""
    body = _body(
        {"action": "opened", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    with pytest.raises(WebhookRefused, match="no delivery id"):
        parse_delivery({"x-github-event": "pull_request"}, body)


def test_a_malformed_body_is_refused_rather_than_guessed():
    body = b"{not json"
    hook, checks = _webhook(FakeReviews())
    with pytest.raises(WebhookRefused, match="not JSON"):
        hook.handle(_headers(body, "pull_request"), body)
    assert checks.created == []


def test_the_reviewer_identity_is_not_taken_from_the_payload():
    """The payload says who reviewed; the decision must not trust it blindly.

    The controller re-reads the reviews from the API rather than believing the
    event body, so a forged (but correctly signed, e.g. replayed) payload
    cannot assert an acceptance that the API does not show.
    """
    reviews = FakeReviews()
    reviews.review_list = []  # the API shows no acceptance
    hook, checks = _webhook(reviews)
    body = _body(
        {
            "action": "submitted",
            "pull_request": {"number": 1, "head": {"sha": SHA_A}},
            "review": {
                "body": f"ACCEPTANCE: ACCEPT {SHA_A}",
                "user": {"login": REVIEWER},
            },
        }
    )

    decision = hook.handle(_headers(body, "pull_request_review", "d-forged"), body)

    assert decision.is_pending, "the event body must not be evidence"
    assert checks.updated == []


def test_a_verdict_for_the_new_head_never_concludes_the_old_head_s_check():
    """The decisive ordering case, built so the two implementations differ.

    Setup: the branch has moved to SHA_B and an acceptance exists for SHA_B.
    A delivery then arrives naming SHA_A -- the old commit.

    A controller that decides for "the pull request's current head" reads
    SHA_B, finds the acceptance, and writes success onto the check attached to
    SHA_A: a commit nobody accepted would carry a green acceptance check. A
    controller that decides for the commit the event names finds no verdict for
    SHA_A and leaves that check in progress.

    An earlier version of this test set the fake's head back to SHA_A before
    the late delivery, which made both implementations agree -- it passed
    against the defect. Mutation testing caught that; the fake now keeps
    telling the truth about the present.
    """
    reviews = FakeReviews()
    hook, checks = _webhook(reviews)

    old = _body(
        {"action": "opened", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    hook.handle(_headers(old, "pull_request", "d-open-a"), old)
    old_check = next(r for r in checks.runs.values() if r["head_sha"] == SHA_A)

    # The branch moves, and SHA_B is the commit that gets accepted.
    reviews.head = SHA_B
    reviews.review_list = [_accept(SHA_B)]
    new = _body(
        {"action": "synchronize", "pull_request": {"number": 1, "head": {"sha": SHA_B}}}
    )
    hook.handle(_headers(new, "pull_request", "d-sync-b"), new)

    # A delivery naming the OLD commit arrives late.
    late = _body(
        {"action": "submitted", "pull_request": {"number": 1, "head": {"sha": SHA_A}}}
    )
    hook.handle(_headers(late, "pull_request_review", "d-late-a"), late)

    assert old_check["status"] == "in_progress", (
        "SHA_A has no acceptance of its own; the verdict for SHA_B must not "
        "conclude SHA_A's check"
    )
    assert "conclusion" not in old_check
    new_check = next(r for r in checks.runs.values() if r["head_sha"] == SHA_B)
    assert new_check["conclusion"] == "success"
