"""Projection producer: AICC state → the gateway's projection artifact.

This is the AIOS/AICC-owned side of the Gateway v1 boundary
(VOYN-W4-AICC-GATEWAY-PROJECTION-PRODUCER): it reads the repository's
existing read surfaces — `tasks_repository.load_tasks` (canonical task list),
`read_model` (the one reconciled counting authority), and
`runtime.runs_read.list_unified_runs` (normalized run journal) — maps them
through an explicit allowlist into the projection format documented in
`docs/aicc_native_gateway/GATEWAY_V1.md` (sample:
`native_gateway/fixtures/projection.sample.json`), and writes the artifact
atomically via `storage.atomic_write_json`.

It deliberately owns no aggregation logic of its own: every number is a
projection of what `read_model` already computes, so this module cannot
become a second source of truth. Unavailable sources degrade, never fail:
a missing/broken run journal yields `degraded: true` with the task data that
is available, and the gateway then reports that state honestly to clients.

Run once (cron/launchd/systemd own the cadence)::

    python -m native_gateway.projection_producer --root . \
        --out /var/lib/aicc/gateway-projection.json

or loop with ``--interval N`` where no scheduler is available yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from command_center import backlog_client, read_model, storage, tasks_repository
from command_center.runtime import runs_read

from .task_titles import load_cache, title_for

PROJECTION_VERSION = "1"
_EVENT_LIMIT = 50

# Optional delivery-evidence keys copied from a task record when present.
# Fleet-managed tasks may carry them; local tasks simply project "unknown".
_EVIDENCE_KEYS = {
    "head_sha": "head_sha",
    "pr": "pr",
    "ci": "ci",
    "acceptance": "acceptance",
    "merged_sha": "merged_sha",
    "deployed_sha": "deployed_sha",
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clean_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _map_task(task: dict) -> dict:
    evidence_raw = (
        task.get("evidence") if isinstance(task.get("evidence"), dict) else task
    )
    evidence = {}
    for src_key, dst_key in _EVIDENCE_KEYS.items():
        evidence[dst_key] = _clean_str(evidence_raw.get(src_key))
    evidence["ci"] = evidence["ci"] or "unknown"
    evidence["acceptance"] = evidence["acceptance"] or "unknown"
    blocker = _clean_str(task.get("blocker"))
    if blocker is None and task.get("status") == "Blocked":
        blocker = "Задача заблокирована"
    return {
        "id": _clean_str(task.get("id")) or "unknown",
        "title": _clean_str(task.get("title")) or "Untitled",
        "blocker": blocker,
        "state": _clean_str(task.get("state")),
        "evidence": evidence,
    }


def _map_projects(tasks: list[dict]) -> list[dict]:
    """Per-project rollup; counting semantics delegated to read_model."""
    by_project: dict[str, list[dict]] = {}
    for task in tasks:
        project = _clean_str(task.get("project")) or "workspace"
        by_project.setdefault(project, []).append(task)
    projects = []
    for name in sorted(by_project):
        snapshot = read_model.task_snapshot(by_project[name])
        # For the calm overview a blocked task IS something the owner should
        # see: needs_attention = explicit decision requests + blocked lane.
        attention = snapshot.attention + snapshot.blocked
        projects.append(
            {
                "id": name,
                "name": name,
                "state": "active" if (snapshot.active or snapshot.blocked) else "idle",
                "active_tasks": snapshot.active,
                "needs_attention": attention,
            }
        )
    return projects


def _map_events(runs: list[dict]) -> list[dict]:
    events = []
    for run in runs[:_EVENT_LIMIT]:
        occurred = _clean_str(run.get("started_at")) or _clean_str(
            run.get("created_at")
        )
        if occurred is None:
            continue
        agent = _clean_str(run.get("agent")) or "run"
        state = _clean_str(run.get("state")) or "UNKNOWN"
        events.append(
            {
                "id": f"{run.get('source', 'run')}:{run.get('id', '?')}",
                "occurred_at": occurred,
                "summary": f"{agent}: {state.lower()}",
                "correlation_id": _clean_str(run.get("task_id")) or "",
            }
        )
    return events


def _map_lanes(runs: list[dict], now: datetime) -> list[dict]:
    lanes = []
    for run in runs:
        if run.get("state") != "RUNNING":
            continue
        started = _clean_str(run.get("started_at"))
        age = -1
        if started is not None:
            try:
                stamp = datetime.fromisoformat(started)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                age = max(0, int((now - stamp).total_seconds()))
            except ValueError:
                pass
        lanes.append(
            {
                "id": f"{run.get('source', 'run')}:{run.get('id', '?')}",
                "state": "active",
                "heartbeat_age_seconds": age,
            }
        )
    return lanes


# Exact rich-status vocabulary -> the projection's task state and the AICC
# read-model lane it corresponds to. DEFER_TO_USER additionally carries a
# blocker so the client's attention surface picks it up.
_RICH_STATE = {
    "UNTRIAGED": ("backlog", "Backlog"),
    "OPEN": ("next", "Next"),
    "NEEDS_REFINEMENT": ("backlog", "Backlog"),
    "SPLIT": ("backlog", "Backlog"),
    "IN_PROGRESS": ("in_progress", "In Progress"),
    "READY_TO_REVIEW": ("review", "Review"),
    "DONE": ("done", "Done"),
    "DEFER_TO_USER": ("deferred", "Blocked"),
    "UNKNOWN": ("backlog", "Backlog"),
}


def _backlog_tasks(
    backlog_path: Path | None, titles_path: Path | None = None
) -> list[dict]:
    """Read-only projection of the VOYN master backlog into task records.

    Reuses `backlog_client` (the sanctioned read side of the Backlog Engine —
    it has no write surface, so this cannot create a second task store).
    Two record surfaces merge here: machine `VOYN_RECOMMENDATION` lines
    (planning) and the body's rich task lines (execution status, parsed by
    the same shared module); on an id collision the rich EXECUTION status
    wins, because that is the state the owner actually asks about.
    An unconfigured or missing file yields an empty list, never an error.
    """
    projection = backlog_client.load_projection(backlog_path)
    rich = backlog_client.load_rich_records(backlog_path)
    # Russian executive titles, produced offline by `localize_titles` on a
    # local model; a record absent from the cache keeps its humanized slug.
    titles = load_cache(titles_path)

    tasks: dict[str, dict] = {}
    for rec in projection.records:
        tasks[rec.issue_id] = {
            "id": rec.issue_id,
            "project": rec.parallel_domain or rec.owner or "backlog",
            "title": title_for(rec.issue_id, rec.title, titles),
            "status": "Next" if rec.is_approved else "Backlog",
            "state": "next" if rec.is_approved else "backlog",
            "type": "backlog",
        }
    for record in rich:
        state, lane = _RICH_STATE[record.status]
        entry = tasks.setdefault(
            record.record_id,
            {
                "id": record.record_id,
                "project": "backlog",
                "title": title_for(record.record_id, record.title, titles),
                "type": "backlog",
            },
        )
        entry["status"] = lane
        entry["state"] = state
        if record.status == "DEFER_TO_USER":
            entry["blocker"] = "Ждёт вашего решения"
    return list(tasks.values())


def _map_dialogs(root: Path) -> list[dict]:
    """Dialog summaries from the AICC chat store (`data/chats.json`).

    Summary-level strictly: id, title, message count and the last-activity
    timestamp. Message CONTENT never enters the projection — raw dialog text
    is exactly the class of data the gateway boundary exists to keep in.
    """
    conversations = storage.read_json(
        storage.resolve_data_dir(root) / "chats.json", []
    )
    if not isinstance(conversations, list):
        return []
    dialogs = []
    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        messages = conversation.get("messages")
        dialogs.append(
            {
                "id": _clean_str(conversation.get("id")) or "unknown",
                "title": _clean_str(conversation.get("title")) or "Разговор",
                "state": "open",
                "last_activity_at": _clean_str(conversation.get("updated_at")),
                "message_count": len(messages) if isinstance(messages, list) else 0,
                "last_summary": None,
            }
        )
    dialogs.sort(key=lambda d: d["last_activity_at"] or "", reverse=True)
    return dialogs


def build_projection(
    root: Path,
    *,
    db_path: Path | None = None,
    backlog_path: Path | None = None,
    titles_path: Path | None = None,
) -> dict:
    """Assemble the projection dict from the repository's read surfaces."""
    now = datetime.now(UTC)
    degraded = False

    tasks = tasks_repository.load_tasks(root) + _backlog_tasks(
        backlog_path, titles_path
    )

    runs: list[dict] = []
    resolved_db = db_path or (storage.resolve_data_dir(root) / "runtime.db")
    try:
        runs = runs_read.list_unified_runs(resolved_db, root=root, limit=_EVENT_LIMIT)
    except Exception:  # noqa: BLE001 -- any journal failure degrades, never breaks the artifact
        # The run journal is optional context for the calm overview; its
        # unavailability degrades freshness, it must not hide the task list.
        degraded = True

    payload = {
        "projection_version": PROJECTION_VERSION,
        "degraded": degraded,
        "projects": _map_projects(tasks),
        "tasks": [_map_task(t) for t in tasks],
        "lanes": _map_lanes(runs, now),
        "events": _map_events(runs),
        "dialogs": _map_dialogs(root),
        "decisions": [],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    payload["revision"] = f"r-{digest[:12]}"
    payload["generated_at"] = _iso_now()
    return payload


def write_projection(
    root: Path,
    out_path: Path,
    *,
    db_path: Path | None = None,
    backlog_path: Path | None = None,
    titles_path: Path | None = None,
) -> dict:
    projection = build_projection(
        root, db_path=db_path, backlog_path=backlog_path, titles_path=titles_path
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    storage.atomic_write_json(out_path, projection)
    return projection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the Gateway v1 projection artifact from AICC state"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--backlog",
        type=Path,
        default=None,
        help="VOYN master backlog path (else AICC_MASTER_BACKLOG env, else none)",
    )
    parser.add_argument(
        "--titles",
        type=Path,
        default=None,
        help="Russian title cache from native_gateway.localize_titles",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="seconds between refreshes; 0 (default) writes once and exits",
    )
    args = parser.parse_args(argv)

    while True:
        projection = write_projection(
            args.root,
            args.out,
            db_path=args.db,
            backlog_path=args.backlog,
            titles_path=args.titles,
        )
        print(
            f"projection {projection['revision']} → {args.out} "
            f"(tasks={len(projection['tasks'])}, degraded={projection['degraded']})"
        )
        if args.interval <= 0:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
