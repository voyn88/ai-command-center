"""The Python surface over the ``work_item`` claim protocol.

Migration ``0002_queue_claim`` shipped the whole protocol as PL/pgSQL —
``queue_claim``, ``queue_heartbeat``, ``queue_complete``, ``queue_fail`` — and
until this module, nothing in Python called any of them: the contract existed
only for tests. This wrapper is deliberately thin, because the database owns
the semantics and duplicating them here would create a second authority:

* **The claimant is not a parameter.** ``session_user`` — the PostgreSQL role
  authenticated at connect time — is written into ``claimed_by_role`` by a
  trigger. This module therefore never sends an identity; whoever the
  connection is, is who claimed.
* **The claim token never travels in the clear on claim.** The worker
  generates it locally, sends its SHA-256 hex, and keeps the plaintext in
  process memory for the ownership calls (heartbeat/complete/fail).
* **A heartbeat cannot lengthen the lease.** ``visible_until`` is extended by
  the row's own ``visibility_seconds``; the value is chosen once, at claim.

Import purity: ``command_center.db`` promises that importing it pulls in
neither ``aios_db`` nor ``psycopg``. The pool is resolved on use, exactly as
``queue_store.py`` does, and the connection factory is injectable for tests.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ClaimedWork",
    "QueueRefusal",
    "WorkQueueStore",
]


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    """One claimed execution attempt, plus the secret that proves ownership.

    ``claim_token`` is the plaintext the ownership functions require. It lives
    only in process memory; persisting it anywhere would turn a crash into a
    credential leak, and the protocol is designed so nothing ever needs it
    after the attempt ends.
    """

    work_item_id: str
    attempt_id: str
    attempt_no: int
    visible_until: str
    payload: dict[str, Any]
    claim_token: str


@dataclass(frozen=True, slots=True)
class QueueRefusal:
    """A refusal is data, not an exception: ``no_work`` is the ordinary idle
    case of a polling worker, and treating it as an error would make every
    quiet minute look like a failure in the logs."""

    reason: str


class WorkQueueStore:
    """Claim/heartbeat/complete/fail over the shipped SQL protocol."""

    def __init__(self, connection_factory: Any = None) -> None:
        self._factory = connection_factory

    def _connection(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from command_center.db import pool

        return pool.connection()

    # -- protocol -------------------------------------------------------------

    def claim(
        self, queue: str, *, visibility_seconds: int = 300
    ) -> ClaimedWork | QueueRefusal:
        """Claim the next ready item, or report why not.

        The token is generated here, per attempt, and only its hash travels.
        ``visibility_seconds`` is the one lease-length decision the caller
        gets to make — the database clamps it to [1, 3600] and heartbeats can
        only renew, never extend.
        """
        token = secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        row = self._call(
            "SELECT * FROM queue_claim(%s, %s, %s)",
            (queue, token_hash, visibility_seconds),
        )
        if not row["ok"]:
            return QueueRefusal(reason=row["reason"])
        payload = row["payload"]
        if isinstance(payload, str):  # psycopg may hand jsonb back as text
            payload = json.loads(payload)
        return ClaimedWork(
            work_item_id=row["work_item_id"],
            attempt_id=row["attempt_id"],
            attempt_no=row["attempt_no"],
            visible_until=str(row["visible_until"]),
            payload=payload or {},
            claim_token=token,
        )

    def heartbeat(self, work: ClaimedWork) -> bool:
        """Renew the lease. ``False`` means the attempt is no longer ours —
        superseded after a lapse — and the only correct response is to stop
        the work, not to retry the heartbeat."""
        row = self._call(
            "SELECT * FROM queue_heartbeat(%s, %s)",
            (work.attempt_id, work.claim_token),
        )
        return bool(row["ok"])

    def complete(self, work: ClaimedWork, result: dict[str, Any]) -> bool:
        row = self._call(
            "SELECT * FROM queue_complete(%s, %s, %s::jsonb)",
            (work.attempt_id, work.claim_token, json.dumps(result)),
        )
        return bool(row["ok"])

    def fail(self, work: ClaimedWork, *, reason: str, retryable: bool = True) -> bool:
        row = self._call(
            "SELECT * FROM queue_fail(%s, %s, %s, %s)",
            (work.attempt_id, work.claim_token, reason, retryable),
        )
        return bool(row["ok"])

    # -- enqueue (control plane, app role) ------------------------------------

    def enqueue(
        self,
        queue: str,
        *,
        idempotency_key: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        repository_id: str | None = None,
        max_attempts: int = 3,
        priority: int = 0,
        delay_seconds: int = 0,
        backoff_seconds: int = 2,
    ) -> str:
        """Enqueue one item; returns its ``work_item_id``.

        Exactly-once by key: ``queue_enqueue`` is an upsert on
        ``(queue, idempotency_key)``, and a duplicate returns the EXISTING
        item id (auditing the refusal server-side) — so a retried HTTP
        request or dispatcher crash-loop cannot create a second run. This is
        an ``aicc_app`` privilege: the worker role is deliberately not
        granted enqueue, and this method on a worker connection fails at the
        database, not here.
        """
        row = self._call(
            "SELECT queue_enqueue(%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s) AS work_item_id",
            (
                queue,
                idempotency_key,
                json.dumps(payload),
                task_id,
                repository_id,
                max_attempts,
                priority,
                delay_seconds,
                backoff_seconds,
            ),
        )
        return str(row["work_item_id"])

    # -- plumbing -------------------------------------------------------------

    def _call(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [d[0] for d in cur.description]
                values = cur.fetchone()
        return dict(zip(columns, values, strict=True))
