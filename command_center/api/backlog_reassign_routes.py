"""Priority/wave reassignment for the Postgres-backed backlog (``VOYN-W0-APP-CONTROL-S6d``).

The one write ``backlog_intake_routes.confirm`` explicitly declined to be:
that route only ever inserts a brand-new task and refuses outright if the id
already exists (409), precisely so that changing an EXISTING task's fields
stays on its own, narrower surface. This is that surface for two fields:
``wave`` and ``priority``. It moves through :func:`backlog_reassign` (0010),
the same optimistic-revision shape as :func:`backlog_transition` (0005) — the
caller must supply the revision it read, and a stale one is refused rather
than silently overwritten, whether the caller is a human editing the Tasks
screen or the chat/voice parser (S6a/S6b) re-issuing a task line with a
changed wave or priority token.

Status, title, body and repo are untouched here on purpose: reprioritizing a
task is not a claim about its progress, and the status machine has its own
function for a reason.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from command_center.db.backlog_store import BacklogStore
from command_center.db.pool import PoolNotOpenError

router = APIRouter(prefix="/api/v1/backlog", tags=["backlog"])

_UNCONFIGURED = (
    "the Postgres-backed backlog is not configured on this server "
    "(AICC_PG_HOST unset) — this deployment has no autonomous delivery "
    "backlog to reassign"
)


def _write_store() -> BacklogStore:
    return BacklogStore()


@router.post("/tasks/{task_id}/reassign")
def reassign(task_id: str, payload: dict = Body(...)) -> dict:
    wave = payload.get("wave")
    if not (isinstance(wave, str) and wave.strip()):
        raise HTTPException(status_code=422, detail="wave is required")
    priority = payload.get("priority")
    if priority is not None and not isinstance(priority, str):
        raise HTTPException(status_code=422, detail="priority must be a string or null")
    expected_revision = payload.get("expected_revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise HTTPException(status_code=422, detail="expected_revision is required")

    store = _write_store()
    try:
        ok, reason, revision = store.reassign(task_id, wave, priority, expected_revision)
    except PoolNotOpenError as exc:
        raise HTTPException(status_code=503, detail=_UNCONFIGURED) from exc

    if not ok:
        if reason == "unknown_task":
            raise HTTPException(status_code=404, detail=reason)
        if reason == "revision_conflict":
            # The caller read a stale revision; hand back the current one so a
            # UI or voice-correction flow can re-read and retry rather than
            # re-deriving it with a second request.
            raise HTTPException(
                status_code=409, detail={"reason": reason, "revision": revision}
            )
        raise HTTPException(status_code=422, detail=reason)
    return {"task_id": task_id, "reason": reason, "revision": revision}
