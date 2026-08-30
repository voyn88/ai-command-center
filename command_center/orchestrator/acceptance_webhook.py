"""Webhook intake for the acceptance controller.

GitHub delivers at-least-once and in no guaranteed order. Both properties are
adversarial here, and neither is hypothetical: a redelivery after a timeout and
a `synchronize` overtaking the review that preceded it are ordinary events.

So this module holds exactly three responsibilities, and hands the decision
itself to `acceptance_controller`:

1. **Authenticity.** A payload is processed only if it carries a valid
   `X-Hub-Signature-256` for the configured secret. An unsigned or wrongly
   signed delivery is refused before it is parsed, because parsing attacker
   input is already giving it a turn.
2. **Idempotency.** A delivery is identified by GitHub's own delivery id, and
   the *decision* is identified by `(repository, head SHA, policy_version)`.
   Replaying a delivery re-runs the same decision against the same check,
   which is safe by construction, but the seen-set makes it cheap and makes
   double-processing visible in metrics rather than silent.
3. **Ordering safety.** Each event is routed by the head SHA *it names*, never
   by "the pull request's current head". A late delivery for an older commit
   therefore lands on that commit's own check and cannot conclude a newer one.

Deliberately not here: retries with backoff, a queue, or persistence. The
controller re-derives its answer from the API on every event and on
reconciliation, so a dropped delivery costs latency, not correctness — and a
queue that could hold a stale decision would be a second source of truth.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from command_center.orchestrator.acceptance_controller import (
    POLICY_VERSION,
    AcceptanceController,
    ControllerError,
    Decision,
)

_SIGNATURE_HEADER = "x-hub-signature-256"
_EVENT_HEADER = "x-github-event"
_DELIVERY_HEADER = "x-github-delivery"
_PREFIX = "sha256="

#: How many delivery ids to remember. Bounded on purpose: this is a
#: duplicate-suppression hint, not the correctness mechanism -- correctness
#: comes from the decision being idempotent -- so an unbounded set would be a
#: memory leak defending something already defended.
_SEEN_LIMIT = 4096


class WebhookRefused(RuntimeError):
    """The delivery was not processed, and why."""


@dataclass(frozen=True, slots=True)
class Delivery:
    event: str
    delivery_id: str
    repository: str
    payload: dict


def verify_signature(secret: str, body: bytes, signature: str | None) -> None:
    """Refuse anything not signed with the configured secret.

    Compared with `compare_digest`: a plain `==` on a hex digest leaks how much
    of a forged signature is correct through timing, which is enough to forge
    one given enough attempts.
    """
    if not secret:
        raise WebhookRefused("no webhook secret is configured; refusing all deliveries")
    if not signature or not signature.startswith(_PREFIX):
        raise WebhookRefused("delivery carries no sha256 signature")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature[len(_PREFIX) :]):
        raise WebhookRefused("delivery signature does not match the configured secret")


def parse_delivery(headers: dict[str, str], body: bytes) -> Delivery:
    """Read the delivery, refusing anything whose identity is unclear."""
    lowered = {key.lower(): value for key, value in headers.items()}
    event = lowered.get(_EVENT_HEADER, "")
    delivery_id = lowered.get(_DELIVERY_HEADER, "")
    if not event:
        raise WebhookRefused("delivery names no event type")
    if not delivery_id:
        raise WebhookRefused("delivery carries no delivery id")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookRefused(f"delivery body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookRefused("delivery body is not a JSON object")
    repository = ((payload.get("repository") or {}).get("full_name")) or ""
    if not repository:
        raise WebhookRefused("delivery names no repository")
    return Delivery(
        event=event, delivery_id=delivery_id, repository=repository, payload=payload
    )


class AcceptanceWebhook:
    """Routes verified deliveries into the controller."""

    def __init__(
        self,
        controller: AcceptanceController,
        secret: str,
        *,
        audit: Callable[[dict], None] | None = None,
    ) -> None:
        self._controller = controller
        self._secret = secret
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._audit = audit or (lambda _record: None)
        self.duplicates = 0
        self.ignored = 0

    def _remember(self, delivery: Delivery, outcome: str) -> None:
        self._seen[delivery.delivery_id] = outcome
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)

    def handle(self, headers: dict[str, str], body: bytes) -> Decision | None:
        """Verify, deduplicate and dispatch one delivery.

        Returns the decision, or `None` for an event this controller does not
        act on. A duplicate is re-processed rather than skipped: the decision
        is idempotent, and re-deriving it repairs any check that a partially
        applied earlier attempt left behind.
        """
        verify_signature(
            self._secret,
            body,
            headers.get("X-Hub-Signature-256") or headers.get("x-hub-signature-256"),
        )
        delivery = parse_delivery(headers, body)
        if delivery.delivery_id in self._seen:
            self.duplicates += 1
            self._audit(
                {
                    "event": "duplicate_delivery",
                    "delivery_id": delivery.delivery_id,
                    "github_event": delivery.event,
                    "repository": delivery.repository,
                }
            )
        action = delivery.payload.get("action")
        try:
            decision = self._dispatch(delivery, action)
        except ControllerError as exc:
            self._remember(delivery, "error")
            raise WebhookRefused(
                f"{delivery.event}/{action} could not be handled: {exc}"
            ) from exc
        self._remember(delivery, "handled" if decision else "ignored")
        return decision

    def _dispatch(self, delivery: Delivery, action: object) -> Decision | None:
        if delivery.event == "pull_request":
            if action in {"opened", "reopened", "synchronize"}:
                return self._controller.handle_pull_request_event(
                    delivery.repository, delivery.payload
                )
            self.ignored += 1
            return None
        if delivery.event == "pull_request_review":
            if action in {"submitted", "edited", "dismissed"}:
                return self._controller.handle_review_event(
                    delivery.repository, delivery.payload
                )
            self.ignored += 1
            return None
        if delivery.event == "merge_group":
            if action == "checks_requested":
                return self._controller.handle_merge_group_event(
                    delivery.repository, delivery.payload
                )
            self.ignored += 1
            return None
        # An event this controller has no opinion about. Ignoring it is not a
        # refusal: GitHub sends what the App is subscribed to, and reacting to
        # an unrecognised event would be acting on an unread contract.
        self.ignored += 1
        return None

    @property
    def policy_version(self) -> str:
        return POLICY_VERSION
