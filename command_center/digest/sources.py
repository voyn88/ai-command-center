"""Read-only source adapters for the morning digest.

The digest engine never invents data: every section it emits is derived from an
*existing* in-repo read path — the runtime run read model, the git log, the
advisor-proposal repository, the task store and the run/task snapshots the rest
of the app already computes. This module is the thin seam that reads those
sources and normalises each into a small, uniform ``dict`` the assembler
(:mod:`command_center.digest.service`) turns into ordered ``digest_item`` rows.

Redaction: BANK/LEGAL projects (:func:`command_center.project_config.is_sensitive`)
are dropped from every source here, exactly as the read-only API service does —
a sensitive project never contributes a run, proposal or task to the digest.

Testability seam: every backing call is referenced through a module-level name
so a test can monkeypatch it and drive the whole assembler off fakes, without a
real runtime db, git checkout or task store.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from command_center import read_model
from command_center.project_config import is_sensitive
from command_center.runtime.db.core import resolve_db_path
from command_center.runtime.runs_read import list_unified_runs
from command_center.tasks_repository import load_tasks
from command_center.ui.git_readers import get_git_log
from command_center.runtime import db

# Repo root is three levels up: <root>/command_center/digest/sources.py
ROOT = Path(__file__).resolve().parents[2]

#: How far back "overnight" reaches when the caller does not pass an explicit
#: cutoff — the window a morning digest summarises.
OVERNIGHT_WINDOW_HOURS = 18

#: Per-section caps so one noisy source can never flood the digest.
_MAX_OVERNIGHT = 12
_MAX_COMMITS = 8
_MAX_PROPOSALS = 12
_MAX_ATTENTION = 12

# The task launch_status that means "needs a human decision" (mirrors the
# read-only API service's attention definition).
_ATTENTION_LAUNCH_STATUS = "Requires Attention"


def _not_sensitive(project: str | None) -> bool:
    return not (project and is_sensitive(project))


def overnight_cutoff(*, now: datetime | None = None) -> str:
    """The ISO cutoff below which activity is *not* "overnight" — naive-local
    to match every other timestamp in the app (:func:`models.iso_now`)."""
    now = now or datetime.now()
    return (now - timedelta(hours=OVERNIGHT_WINDOW_HOURS)).isoformat(timespec="seconds")


def overnight_runs(*, since: str | None = None, root: Path = ROOT) -> list[dict]:
    """Runs created since ``since`` (default: the overnight window), newest
    first, sensitive projects dropped. Each item: ``ref`` (``run:<id>``),
    ``title``, ``detail``, ``project``, ``ts``. Degrades to ``[]`` if the
    runtime store is not initialised yet."""
    since = since or overnight_cutoff()
    try:
        runs = list_unified_runs(resolve_db_path(root), root=root, limit=_MAX_OVERNIGHT * 4)
    except Exception:
        return []
    out: list[dict] = []
    for run in runs:
        if not _not_sensitive(run.get("project")):
            continue
        ts = run.get("created_at") or ""
        if ts < since:
            continue
        rid = str(run.get("id", ""))
        out.append(
            {
                "ref": f"run:{rid}" if rid else "",
                "title": str(run.get("task_type") or run.get("state") or "run"),
                "detail": str(run.get("state") or ""),
                "project": run.get("project"),
                "ts": ts,
            }
        )
        if len(out) >= _MAX_OVERNIGHT:
            break
    return out


def recent_commits(*, limit: int = _MAX_COMMITS) -> list[dict]:
    """Recent commits from the repo's own git log, normalised to the uniform
    source shape (``ref`` = ``commit:<hash>``)."""
    items: list[dict] = []
    for commit in get_git_log(limit=limit):
        h = commit.get("hash") or ""
        items.append(
            {
                "ref": f"commit:{h}" if h else "",
                "title": commit.get("subject", ""),
                "detail": h[:8],
                "project": None,
                "ts": commit.get("date"),
            }
        )
    return items


def open_proposals(*, root: Path = ROOT) -> list[dict]:
    """Advisor proposals still awaiting the owner (status ``new``/``accepted``),
    newest first, sensitive projects dropped. Each item carries a
    ``proposal:<id>`` ref so the digest entry links straight to the Советник
    detail route."""
    try:
        rows = db.list_advisor_proposals(
            resolve_db_path(root),
            statuses=["new", "accepted"],
            limit=_MAX_PROPOSALS * 2,
        )
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        if not _not_sensitive(row.get("project_ref")):
            continue
        out.append(
            {
                "ref": f"proposal:{row['id']}",
                "title": row.get("title") or "",
                "detail": f"{row.get('kind') or ''} · {row.get('status') or ''}".strip(" ·"),
                "project": row.get("project_ref"),
                "ts": row.get("created_at"),
            }
        )
        if len(out) >= _MAX_PROPOSALS:
            break
    return out


def attention_items(*, root: Path = ROOT) -> list[dict]:
    """Things that need a human decision *now*: tasks flagged
    ``Requires Attention`` (or regressed after Done) and runs in an attention
    state (:data:`read_model.RUN_ATTENTION_STATES`). Sensitive projects dropped.
    Each item carries a ``task:<id>`` / ``run:<id>`` ref."""
    out: list[dict] = []
    try:
        tasks = load_tasks(root)
    except Exception:
        tasks = []
    for task in tasks:
        project = task.get("project")
        if not _not_sensitive(project):
            continue
        needs = (task.get("launch_status") or "") == _ATTENTION_LAUNCH_STATUS or bool(
            task.get("regressed_after_done")
        )
        if not needs:
            continue
        out.append(
            {
                "ref": f"task:{task.get('id', '')}",
                "title": task.get("title", ""),
                "detail": task.get("launch_status") or "regressed after done",
                "project": project,
                "ts": task.get("updated_at") or task.get("created_at"),
            }
        )
    try:
        runs = list_unified_runs(resolve_db_path(root), root=root, limit=_MAX_ATTENTION * 4)
    except Exception:
        runs = []
    for run in runs:
        if not _not_sensitive(run.get("project")):
            continue
        if run.get("state") in read_model.RUN_ATTENTION_STATES:
            out.append(
                {
                    "ref": f"run:{run.get('id', '')}",
                    "title": str(run.get("task_type") or "run"),
                    "detail": str(run.get("state") or ""),
                    "project": run.get("project"),
                    "ts": run.get("created_at"),
                }
            )
    return out[:_MAX_ATTENTION]


def agent_status(*, root: Path = ROOT) -> dict:
    """A single, honest status line: how many agent runs are running / queued /
    need attention right now (:func:`read_model.run_snapshot` over the visible,
    redacted run set). No cost or budget figure is invented — there is no spend
    source in this repo, and the digest never fabricates one."""
    try:
        runs = list_unified_runs(resolve_db_path(root), root=root, limit=200)
        runs = [r for r in runs if _not_sensitive(r.get("project"))]
        available = True
    except Exception:
        runs, available = [], False
    snap = read_model.run_snapshot(runs)
    return {
        "running": snap.running,
        "queued": snap.queued,
        "attention": snap.attention,
        "total": snap.total,
        "available": available,
    }
