"""Chat-text intake for the Postgres-backed backlog (``VOYN-W0-APP-CONTROL-S6a``).

Two-step, stateless by design:

* ``POST .../draft`` sends the owner's free text to a model, runs the reply
  through :mod:`command_center.db.backlog_intake` (which itself just calls
  the existing Markdown-backlog grammar,
  :func:`command_center.db.backlog_parser.parse_backlog`) and returns the
  proposal for a human to read. It never writes.
* ``POST .../confirm`` takes the (possibly hand-edited) line back from the
  client and re-parses it from scratch — the draft's parse is never trusted
  across a request boundary, so there is no server-held "pending draft" state
  to race or expire — then inserts through ``BacklogStore.upsert_task``, the
  same ``SECURITY DEFINER`` path the Markdown importer already uses.

Chat intake creates NEW backlog records only. ``backlog_upsert_task`` is also
the Markdown-reconciliation path and CAN overwrite an existing row's status,
wave, priority, title and body directly, bypassing ``backlog_transition``'s
status machine — correct for reconciling the incumbent Markdown file, wrong
for a chat message. So ``confirm`` checks the task id first and refuses with
409 if a record already exists under it, rather than silently rewriting it.
Changing an existing task belongs to the transition/dependency surface
(S6d), not here.

The model call rides ``agent_runner.run_claude_code`` in the read-only
``review`` profile — the same sandbox class ``chat_service.ClaudeCodeChatProvider``
uses for a chat turn. It is not reused directly: that module's prompt builds
a general conversational turn, not this route's fixed structured-line
grammar, and reusing its ``send()`` would tie this route to conversation
history and repository context it does not have.

Mounted alongside ``backlog_routes`` in ``app.py`` under the same ``enforce``
guard; both writes are covered by ``http_auth.routing.ROUTE_OPERATIONS``.
``_call_model`` is a module global resolved at call time so tests
monkeypatch it — no real subprocess in the test suite (see
``tests/api/test_backlog_intake_routes.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from command_center import agent_runner
from command_center.db import backlog_intake
from command_center.db.backlog_parser import ParsedTask
from command_center.db.backlog_store import BacklogStore
from command_center.db.pool import PoolNotOpenError

router = APIRouter(prefix="/api/v1/backlog/intake", tags=["backlog"])

_MODEL_TIMEOUT_SECONDS = 60

_UNCONFIGURED = (
    "the Postgres-backed backlog is not configured on this server "
    "(AICC_PG_HOST unset) — this deployment has no autonomous delivery "
    "backlog to write to"
)


def _call_model(prompt: str) -> str:
    result = agent_runner.run_claude_code(
        repository_path=agent_runner.ROOT,
        prompt=prompt,
        task_type="review",  # read-only sandbox: a draft turn must never touch a repo
        timeout_seconds=_MODEL_TIMEOUT_SECONDS,
    )
    if result.status != "completed":
        raise HTTPException(
            status_code=502,
            detail=result.stderr.strip() or f"model call did not complete: {result.status}",
        )
    return agent_runner.extract_result_text(result.stdout)


def _write_store() -> BacklogStore:
    return BacklogStore()


def _task_payload(task: ParsedTask) -> dict:
    return {
        "task_id": task.task_id,
        "wave": task.wave,
        "priority": task.priority,
        "status": task.status,
        "kind": task.kind,
        "title": task.title,
        "body": task.body,
        "repo": task.repo,
    }


@router.post("/draft")
def draft(payload: dict = Body(...)) -> dict:
    text = payload.get("text")
    if not (isinstance(text, str) and text.strip()):
        raise HTTPException(status_code=422, detail="text is required")

    raw_output = _call_model(backlog_intake.build_intake_prompt(text))
    result = backlog_intake.draft_from_model_output(raw_output)
    if not result.ok or result.task is None:
        return {"ok": False, "reason": result.reason, "raw_output": result.raw_output}
    return {
        "ok": True,
        "line": result.raw_output,
        "task": _task_payload(result.task),
    }


@router.post("/confirm")
def confirm(payload: dict = Body(...)) -> dict:
    line = payload.get("line")
    if not (isinstance(line, str) and line.strip()):
        raise HTTPException(status_code=422, detail="line is required")

    result = backlog_intake.draft_from_model_output(line)
    if not result.ok or result.task is None:
        raise HTTPException(
            status_code=422,
            detail=f"line does not match the backlog task grammar: {result.reason}",
        )

    store = _write_store()
    try:
        if store.get_task(result.task.task_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{result.task.task_id} already exists — chat intake creates new "
                    "tasks only; edit the task id in the line, or use the "
                    "transition/dependency surface to change an existing task"
                ),
            )
        ok, reason, changed = store.upsert_task(result.task)
    except PoolNotOpenError as exc:
        raise HTTPException(status_code=503, detail=_UNCONFIGURED) from exc

    if not ok:
        raise HTTPException(status_code=422, detail=reason)
    return {"task_id": result.task.task_id, "reason": reason, "changed": changed}
