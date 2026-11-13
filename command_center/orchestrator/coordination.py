"""Durable economical Grok↔Cursor coordination (v2).

Grok 4.6 supplies a bounded, structured advisory ordering of *already
eligible* backlog task ids. Cursor (via the existing planner dispatch
cascade) remains the implementation path. The planner is still the only
authority that may dispatch: this module never widens permissions, never
invents task ids, never bypasses leases/WIP/wave gates, and never marks
DONE.

Economical dedup (survives restart):
  * unchanged candidate/evidence snapshot → no model call
  * unchanged blocker after a reject/cooldown → no model call
  * at most one coordinator action in flight (CAS + queue idempotency)

Model calls are asynchronous work_items. The planner lease is never held
across a synchronous provider round-trip: ``coordinate_once`` is meant to
run *after* ``plan_once`` returns (see ``command_center.db.cli``).

Opt-in: ``AICC_COORDINATOR_V2=1``. Stop switch: ``AICC_COORDINATOR_V2_STOP=1``.
Neither is claimed active by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from command_center.worker.payloads import AGENT_RUN_SCHEMA_VERSION

__all__ = [
    "COORDINATOR_NAME",
    "CoordinationLimits",
    "CoordinationReport",
    "apply_advisory_order",
    "build_candidate_snapshot",
    "coordinate_once",
    "coordination_enabled",
    "coordination_stopped",
    "load_advisory_order",
    "parse_coordination_result",
]

COORDINATOR_NAME = "grok_cursor_v2"
_COORD_TASK_ID = "VOYN-AICC-COORD-V2"
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_DEFAULT_COOLDOWN_SECONDS = 300
_DEFAULT_TIMEOUT_SECONDS = 180
_DEFAULT_MAX_CANDIDATES = 40
_PROMPT_TITLE_CHARS = 80


@dataclass(frozen=True, slots=True)
class CoordinationLimits:
    name: str = COORDINATOR_NAME
    max_candidates: int = _DEFAULT_MAX_CANDIDATES
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS
    queue: str = "execution"
    #: project_id / repository_path for the model-only agent_run payload.
    project_id: str = "AICC"
    repository_path: str = ""


@dataclass(slots=True)
class CoordinationReport:
    enabled: bool = False
    stopped: bool = False
    skipped: str | None = None
    enqueued: str | None = None
    applied: list[str] = field(default_factory=list)
    rejected: str | None = None
    snapshot_hash: str | None = None
    blocker_hash: str | None = None


def coordination_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get("AICC_COORDINATOR_V2") or "").strip() == "1"


def coordination_stopped(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get("AICC_COORDINATOR_V2_STOP") or "").strip() == "1"


def build_candidate_snapshot(
    candidates: list[dict[str, Any]],
    *,
    max_candidates: int = _DEFAULT_MAX_CANDIDATES,
) -> tuple[str, list[dict[str, Any]], str]:
    """Compact, secret-free snapshot of eligible work.

    Returns ``(snapshot_hash, compact_rows, blocker_hash)``. Bodies, prompts,
    credentials and full backlog text never enter the hash or the model
    prompt — only ids, wave, priority, class and evidence *kinds*.
    """
    rows: list[dict[str, Any]] = []
    for raw in candidates[: max(max_candidates, 0)]:
        task_id = str(raw.get("task_id") or "").strip()
        if not task_id:
            continue
        kinds = raw.get("evidence_kinds") or ()
        if isinstance(kinds, str):
            kinds = (kinds,)
        compact = {
            "task_id": task_id,
            "wave": str(raw.get("wave") or ""),
            "priority": str(raw.get("priority") or ""),
            "task_class": str(raw.get("task_class") or "functional"),
            "evidence_kinds": sorted({str(k) for k in kinds if k}),
            "title": str(raw.get("title") or "")[:_PROMPT_TITLE_CHARS],
        }
        rows.append(compact)
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    snapshot_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # Blocker fingerprint: absence of dispatchable repos / empty set / class mix.
    blocker_bits = {
        "count": len(rows),
        "classes": sorted({r["task_class"] for r in rows}),
        "waves": sorted({r["wave"] for r in rows}),
        "evidence": sorted({k for r in rows for k in r["evidence_kinds"]}),
    }
    blocker_hash = hashlib.sha256(
        json.dumps(blocker_bits, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return snapshot_hash, rows, blocker_hash


def parse_coordination_result(
    text: str, eligible_ids: set[str]
) -> tuple[list[str] | None, str | None]:
    """Fail closed: any invented id, bad shape, or partial JSON → reject."""
    if not isinstance(text, str) or not text.strip():
        return None, "empty_result"
    match = _JSON_OBJECT.search(text)
    if match is None:
        return None, "no_json_object"
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None, "malformed_json"
    if not isinstance(parsed, dict):
        return None, "not_object"
    # Forbidden keys that would widen authority if honored.
    for banned in ("create_tasks", "mark_done", "permissions", "bypass", "done_ids"):
        if banned in parsed:
            return None, f"forbidden_key:{banned}"
    ordered = parsed.get("ordered_task_ids")
    if not isinstance(ordered, list):
        return None, "missing_ordered_task_ids"
    if any(not isinstance(item, str) or not item.strip() for item in ordered):
        return None, "non_string_task_id"
    cleaned = [item.strip() for item in ordered]
    if len(cleaned) != len(set(cleaned)):
        return None, "duplicate_task_id"
    unknown = [item for item in cleaned if item not in eligible_ids]
    if unknown:
        return None, f"invented_ids:{unknown[0]}"
    # Partial: model returned a proper subset *claiming* completeness via
    # an explicit cover flag is fine; a partial without listing only known
    # ids already failed above. Empty order is a valid "no preference".
    return cleaned, None


def apply_advisory_order(
    candidates: list[tuple[Any, ...]] | list[dict[str, Any]],
    advisory_order: list[str] | None,
    *,
    id_index: int = 0,
) -> list[Any]:
    """Stable reorder: known advisory ids first (live ∩ order), then the rest.

    Unknown / stale ids in ``advisory_order`` are ignored. Invented ids never
    appear because they were rejected at apply time; ignoring here is defence
    in depth when state was written by an older revision.
    """
    if not advisory_order:
        return list(candidates)

    def _task_id(row: Any) -> str:
        if isinstance(row, dict):
            return str(row.get("task_id") or "")
        return str(row[id_index])

    by_id = {_task_id(row): row for row in candidates}
    seen: set[str] = set()
    ordered: list[Any] = []
    for task_id in advisory_order:
        row = by_id.get(task_id)
        if row is not None and task_id not in seen:
            ordered.append(row)
            seen.add(task_id)
    for row in candidates:
        task_id = _task_id(row)
        if task_id not in seen:
            ordered.append(row)
            seen.add(task_id)
    return ordered


def _coordination_cascade() -> list[dict[str, Any]]:
    """Grok 4.6 model-only — independent of the implementation cascade."""
    return [
        {
            "executor": "grok",
            "task_type": "independent_review",
            "model": "grok-4.6",
            "capability": "model_only",
        }
    ]


def _render_prompt(compact_rows: list[dict[str, Any]]) -> str:
    catalog = json.dumps(compact_rows, sort_keys=True, indent=2)
    return (
        "You are the AICC coordination advisor (Grok 4.6). Cursor implements;\n"
        "you only propose an ordering of ALREADY ELIGIBLE task ids.\n"
        "Rules (fail closed — violate any and your output is discarded):\n"
        "- Return ONE JSON object, no markdown fences, no prose outside JSON.\n"
        "- Shape: {\"ordered_task_ids\": [\"ID\", ...], \"notes\": \"short\"}.\n"
        "- ordered_task_ids may only contain ids from the catalog below.\n"
        "- Do NOT invent ids, create tasks, mark DONE, widen permissions,\n"
        "  or claim to bypass leases, WIP, protection, or acceptance gates.\n"
        "- Prefer work that reduces downstream model calls, retries, CI and errors.\n"
        "- Reconcile around already-shipped / evidence-bearing work before new work.\n"
        "- UNKNOWN costs stay unknown; do not invent cost numbers.\n"
        "- Prefer a permutation/subsequence of the catalog over new work.\n\n"
        f"Eligible catalog (compact, no bodies/secrets):\n{catalog}\n"
    )


def _rows(factory: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return list(cur.fetchall())


def _row(factory: Any, sql: str, params: tuple[Any, ...] = ()) -> tuple | None:
    rows = _rows(factory, sql, params)
    return rows[0] if rows else None


def _load_candidates(factory: Any, limits: CoordinationLimits) -> list[dict[str, Any]]:
    rows = _rows(
        factory,
        "SELECT e.task_id, e.wave, e.priority, e.title, e.task_class, "
        "       coalesce(("
        "         SELECT array_agg(DISTINCT ev.kind ORDER BY ev.kind)"
        "           FROM backlog_evidence ev WHERE ev.task_id = e.task_id"
        "       ), ARRAY[]::text[]) AS evidence_kinds "
        "FROM backlog_eligible e "
        "ORDER BY e.numeric_wave DESC, e.wave, e.priority NULLS LAST, e.task_id "
        "LIMIT %s",
        (limits.max_candidates,),
    )
    out: list[dict[str, Any]] = []
    for task_id, wave, priority, title, task_class, evidence_kinds in rows:
        out.append(
            {
                "task_id": task_id,
                "wave": wave,
                "priority": priority,
                "title": title,
                "task_class": task_class,
                "evidence_kinds": evidence_kinds or (),
            }
        )
    return out


def _read_state(factory: Any, name: str) -> dict[str, Any] | None:
    row = _row(
        factory,
        "SELECT snapshot_hash, blocker_hash, advisory_order, in_flight_key, "
        "       in_flight_at, last_result_hash, last_status, cooldown_until, detail "
        "FROM coordinator_state_read(%s)",
        (name,),
    )
    if row is None:
        return None
    (
        snapshot_hash,
        blocker_hash,
        advisory_order,
        in_flight_key,
        in_flight_at,
        last_result_hash,
        last_status,
        cooldown_until,
        detail,
    ) = row
    if isinstance(advisory_order, str):
        advisory_order = json.loads(advisory_order)
    if isinstance(detail, str):
        detail = json.loads(detail)
    return {
        "snapshot_hash": snapshot_hash or "",
        "blocker_hash": blocker_hash or "",
        "advisory_order": list(advisory_order or []),
        "in_flight_key": in_flight_key,
        "in_flight_at": in_flight_at,
        "last_result_hash": last_result_hash or "",
        "last_status": last_status or "idle",
        "cooldown_until": cooldown_until,
        "detail": detail or {},
    }


def _work_item_for_key(
    factory: Any, queue: str, key: str
) -> tuple[str, Any] | None:
    row = _row(
        factory,
        "SELECT i.state, wr.payload FROM work_item i "
        "LEFT JOIN work_result wr ON wr.result_id = i.result_id "
        "WHERE i.queue = %s AND i.idempotency_key = %s",
        (queue, key),
    )
    if row is None:
        return None
    state, payload = row
    if isinstance(payload, str):
        payload = json.loads(payload)
    return str(state), payload


def _resolve_inflight(
    factory: Any,
    *,
    name: str,
    state: dict[str, Any],
    limits: CoordinationLimits,
    eligible_ids: set[str],
    report: CoordinationReport,
) -> None:
    key = state.get("in_flight_key")
    if not key:
        return
    item = _work_item_for_key(factory, limits.queue, key)
    if item is None:
        _row(
            factory,
            "SELECT coordinator_cas_clear_inflight(%s, %s, %s)",
            (name, key, "missing_work_item"),
        )
        report.skipped = "cleared_stale_inflight"
        return
    item_state, payload = item
    if item_state in ("ready", "claimed"):
        report.skipped = "inflight_pending"
        return
    if item_state == "succeeded":
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("result_text") or "")
        ordered, err = parse_coordination_result(text, eligible_ids)
        result_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
        if ordered is None:
            _row(
                factory,
                "SELECT coordinator_cas_finish(%s, %s, %s, NULL, %s, %s::jsonb, %s)",
                (
                    name,
                    key,
                    "rejected",
                    result_hash,
                    json.dumps({"reason": err or "invalid"}),
                    limits.cooldown_seconds,
                ),
            )
            report.rejected = err or "invalid"
            return
        ok_row = _row(
            factory,
            "SELECT coordinator_cas_finish(%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)",
            (
                name,
                key,
                "applied",
                json.dumps(ordered),
                result_hash,
                json.dumps({"count": len(ordered)}),
                0,
            ),
        )
        if ok_row and ok_row[0]:
            report.applied = ordered
        else:
            report.rejected = "finish_cas_failed"
        return
    # dead / other terminal — fail closed with cooldown
    _row(
        factory,
        "SELECT coordinator_cas_finish(%s, %s, %s, NULL, %s, %s::jsonb, %s)",
        (
            name,
            key,
            "rejected",
            "",
            json.dumps({"reason": f"work_item_{item_state}"}),
            limits.cooldown_seconds,
        ),
    )
    report.rejected = f"work_item_{item_state}"


def coordinate_once(
    factory: Any,
    limits: CoordinationLimits | None = None,
    *,
    environ: dict[str, str] | None = None,
    load_candidates: Callable[[], list[dict[str, Any]]] | None = None,
) -> CoordinationReport:
    """One bounded coordination step: apply prior result and maybe enqueue.

    Never blocks on a provider call. Safe to run after ``plan_once`` without
    holding ``planner:global``.
    """
    limits = limits or CoordinationLimits()
    report = CoordinationReport()
    if coordination_stopped(environ):
        report.stopped = True
        report.skipped = "stop_switch"
        return report
    if not coordination_enabled(environ):
        report.skipped = "disabled"
        return report
    report.enabled = True
    if not isinstance(limits.repository_path, str) or not limits.repository_path.strip():
        report.rejected = "repository_path_required"
        return report

    candidates = (
        load_candidates() if load_candidates is not None else _load_candidates(factory, limits)
    )
    local_simple = (os.environ if environ is None else environ).get("AICC_LOCAL_SIMPLE_ENABLED") == "1"
    snapshot_hash, compact_rows, blocker_hash = build_candidate_snapshot(
        candidates, max_candidates=min(limits.max_candidates, 8) if local_simple else limits.max_candidates
    )
    if local_simple:
        # A route change must not inherit a previous provider's permanent
        # unchanged-blocker suppression. Existing CAS/idempotency still owns it.
        snapshot_hash = hashlib.sha256(f"local-simple-v1:{snapshot_hash}".encode()).hexdigest()
        blocker_hash = hashlib.sha256(f"local-simple-v1:{blocker_hash}".encode()).hexdigest()
    report.snapshot_hash = snapshot_hash
    report.blocker_hash = blocker_hash
    eligible_ids = {row["task_id"] for row in compact_rows}

    state = _read_state(factory, limits.name) or {
        "snapshot_hash": "",
        "blocker_hash": "",
        "advisory_order": [],
        "in_flight_key": None,
        "last_status": "idle",
        "cooldown_until": None,
    }

    if state.get("in_flight_key"):
        _resolve_inflight(
            factory,
            name=limits.name,
            state=state,
            limits=limits,
            eligible_ids=eligible_ids,
            report=report,
        )
        # Re-read after possible finish/clear before considering a new enqueue.
        state = _read_state(factory, limits.name) or state
        if state.get("in_flight_key"):
            if report.skipped is None:
                report.skipped = "inflight_pending"
            return report

    cooldown_until = state.get("cooldown_until")
    if cooldown_until is not None:
        now = datetime.now(UTC)
        aware = cooldown_until
        if getattr(aware, "tzinfo", None) is None:
            aware = aware.replace(tzinfo=UTC)
        if aware > now:
            report.skipped = "cooldown"
            return report

    if (
        state.get("snapshot_hash") == snapshot_hash
        and state.get("last_status") != "enqueue_failed"
    ):
        report.skipped = "unchanged_snapshot"
        return report
    if (
        blocker_hash
        and state.get("blocker_hash") == blocker_hash
        and state.get("last_status") in ("rejected", "cooldown")
    ):
        report.skipped = "unchanged_blocker"
        return report

    if not compact_rows:
        report.skipped = "no_candidates"
        return report

    key = f"coord:v2:{snapshot_hash}"
    cascade = _coordination_cascade()
    payload = {
        "kind": "agent_run",
        "v": AGENT_RUN_SCHEMA_VERSION,
        "project_id": limits.project_id,
        "repository_path": limits.repository_path,
        "prompt": _render_prompt(compact_rows),
        "task_type": "independent_review",
        "timeout_seconds": limits.timeout_seconds,
        "untrusted": True,
        "cascade": cascade,
        "mode": "coordination_v2",
    }
    # Advisory ordering is simple, schema-validated work, not acceptance.
    # The worker keeps the existing queue lease and falls back read-only.
    if local_simple:
        from command_center.worker.simple_tasks import MAX_INPUT_BYTES
        if len(compact_rows) <= 8 and len(json.dumps(compact_rows, ensure_ascii=False).encode()) <= MAX_INPUT_BYTES:
            payload = {
                "kind": "simple_task", "v": 1, "operation": "order_tasks",
                "input": compact_rows, "project_id": limits.project_id,
                "repository_path": limits.repository_path,
                "mode": "coordination_v2",
            }
    # SQL owns both the claim and queue write in one transaction. Do not
    # split these operations into callbacks using unrelated connections.
    queued = _row(
        factory,
        "SELECT * FROM coordinator_enqueue(%s, %s, %s, %s, %s, %s::jsonb)",
        (limits.name, snapshot_hash, blocker_hash, key, limits.queue, json.dumps(payload)),
    )
    if queued and queued[0]:
        report.enqueued = str(queued[0])
    elif queued and str(queued[1]).startswith("enqueue_failed:"):
        report.rejected = str(queued[1])
    else:
        report.skipped = str(queued[1]) if queued else "cas_begin_refused"
    return report


def load_advisory_order(factory: Any, name: str = COORDINATOR_NAME) -> list[str]:
    state = _read_state(factory, name)
    if not state:
        return []
    order = state.get("advisory_order") or []
    return [str(item) for item in order if isinstance(item, str)]
