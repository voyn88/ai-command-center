"""HTTP status/dispatch surface over the server work queue
(VOYN-W0-APP-CONTROL-S1, plus the audit-enqueue start of S4).

Thin by the same rule as the dispatch controller: the database owns queue
semantics, `work_queue_read`/`work_queue_store` own the SQL, and this module
owns only HTTP shape. The stores are module globals resolved at call time so
tests monkeypatch them (`tests/webapi/test_queue_routes.py`).

Auth rides `command_center.http_auth` — the accepted boundary (#316,
VOYN-W0-AICC-AUTH-HTTP-01) — not a second mechanism:

* ``POST /audit`` is a mutating route, so it is covered by the routing
  table (``queue:audit:enqueue``) and the router-level ``enforce``
  dependency mounted in ``app.py``: platform-verified principal, then the
  local deny-by-default grant.
* The three ``GET`` routes carry ``Depends(authenticate)`` explicitly. The
  platform doctrine leaves reads unauthenticated (AUTH-HTTP-02), but these
  reads return run transcripts and repository identifiers, so they demand a
  verified principal while *authorization* for reads stays with
  AUTH-HTTP-02/AUTHZ-BOUNDARY-01: any authenticated platform principal may
  read queue status; only granted principals may enqueue. That asymmetry is
  deliberate and recorded here rather than inventing a read-grant vocabulary
  the accepted inventory does not have.
* ``GET /metrics`` (VOYN-W0-AICC-SRV-08, worker-telemetry-contract) is the
  same read authority as ``/items``, aggregated: per-queue backlog depth,
  age and stale-claim counts for a monitoring consumer — see
  `command_center/db/work_queue_read.py`'s `queue_metrics` for the query and
  `QUEUE_METRICS_SCHEMA_VERSION` for the versioning discipline.

The audit payload pins the safe profile by construction: ``task_type`` is
hardwired to ``review`` (a `READ_ONLY_TASK_TYPES` member — the runner's
sandbox decision, not ours) and ``untrusted`` to ``False`` on the authority
of the authenticated, authorized principal. The idempotency key defaults to
a digest of what the audit would do, so a retried request (or a
double-submitted form) lands on the SAME work item instead of a second run —
the caller may override when it genuinely wants a fresh run of identical
parameters.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from command_center.db.work_queue_read import (
    QUEUE_METRICS_SCHEMA_VERSION,
    WorkQueueReadStore,
)
from command_center.db.work_queue_store import WorkQueueStore
from command_center.http_auth import routing
from command_center.worker.payloads import AGENT_RUN_SCHEMA_VERSION

__all__ = ["create_queue_router"]

_AUDIT_QUEUE = "execution"
_AUDIT_TIMEOUT_SECONDS = 900


def _read_store() -> WorkQueueReadStore:
    return WorkQueueReadStore()


def _write_store() -> WorkQueueStore:
    return WorkQueueStore()


def _authenticated_read(request: Request) -> None:
    """Authenticate a read without authorizing it (the recorded asymmetry in
    the module docstring). Resolved through the module attribute — not a
    captured function object — so the platform seam the http_auth test
    fixture patches (`routing.authenticate`) governs these routes too."""
    routing.authenticate(request)


def create_queue_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/queue", tags=["queue"])

    @router.get("/items", dependencies=[Depends(_authenticated_read)])
    def list_items(
        state: str | None = None, queue: str | None = None, limit: int = 100
    ) -> dict:  # read-only, no mutation
        items = _read_store().list_items(queue=queue, state=state, limit=limit)
        return {"items": items}

    @router.get("/items/{work_item_id}", dependencies=[Depends(_authenticated_read)])
    def get_item(work_item_id: str) -> dict:  # read-only, no mutation
        item = _read_store().get_item(work_item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="unknown work item")
        return item

    @router.get("/metrics", dependencies=[Depends(_authenticated_read)])
    def queue_metrics(queue: str | None = None) -> dict:  # read-only, no mutation
        return {
            "schema_version": QUEUE_METRICS_SCHEMA_VERSION,
            "queues": _read_store().queue_metrics(queue=queue),
        }

    @router.post("/audit")
    def enqueue_audit(payload: dict = Body(default=None)) -> dict:
        body: dict[str, Any] = payload if isinstance(payload, dict) else {}
        project_id = body.get("project_id")
        repository_path = body.get("repository_path")
        prompt = body.get("prompt")
        missing = [
            key
            for key, value in (
                ("project_id", project_id),
                ("repository_path", repository_path),
                ("prompt", prompt),
            )
            if not (isinstance(value, str) and value.strip())
        ]
        if missing:
            raise HTTPException(
                status_code=422, detail=f"missing required fields: {missing}"
            )

        key = body.get("idempotency_key")
        if key is not None and not (isinstance(key, str) and key.strip()):
            raise HTTPException(
                status_code=422, detail="idempotency_key must be a non-empty string"
            )
        if key is None:
            digest = hashlib.sha256(
                "\x1f".join((project_id, repository_path, prompt)).encode("utf-8")
            ).hexdigest()
            key = f"audit-{digest[:24]}"

        work_item_id = _write_store().enqueue(
            _AUDIT_QUEUE,
            idempotency_key=key,
            payload={
                "kind": "agent_run",
                "v": AGENT_RUN_SCHEMA_VERSION,
                "project_id": project_id,
                "repository_path": repository_path,
                "prompt": prompt,
                "task_type": "review",  # READ_ONLY_TASK_TYPES: sandbox stays read-only
                "untrusted": False,  # authenticated + authorized principal
                "timeout_seconds": _AUDIT_TIMEOUT_SECONDS,
            },
            repository_id=body.get("repository_id")
            if isinstance(body.get("repository_id"), str)
            else None,
        )
        return {"work_item_id": work_item_id, "idempotency_key": key}

    return router
