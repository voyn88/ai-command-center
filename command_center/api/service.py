"""Read-only aggregation layer for the API.

This is the Service tier: the routes in ``app.py`` contain no business logic;
they call one function here per endpoint, and each function assembles a typed
response out of the *existing* read paths. Nothing in this module writes: it
loads the Integration Center registry and its health collectors, the tasks
repository, the runtime run read model, and the git readers, then maps their
output onto the :mod:`command_center.api.schemas` contract.

Testability seam: every backing call is referenced through a module-level name
(imported at the top and used unqualified), so a test can
``monkeypatch.setattr(service, "load_entries", fake)`` and drive the whole
service off fakes without a real registry, database, or git checkout — the same
pattern ``command_center/webapi/app.py`` uses. That also means no import of
this module, and no call into it, ever touches the developer's real data unless
a real backing function actually runs.

Redaction: BANK/LEGAL projects (``is_sensitive``) never get repository paths,
health detail, run rows, task rows, or activity entries on this surface.
Sensitive rows are dropped entirely rather than field-allowlisted, mirroring the
strict policy already applied by ``command_center/webapi/serializers.py``.
"""

from __future__ import annotations

from pathlib import Path

from command_center import __version__, models, read_model, task_ordering
from command_center.integration.collectors import collect_health
from command_center.integration.registry import get_entry, load_entries
from command_center.project_config import is_sensitive
from command_center.runtime.db.core import resolve_db_path
from command_center.runtime.runs_read import list_unified_runs
from command_center.tasks_repository import load_tasks
from command_center.ui.git_readers import get_git_log

from command_center.api import schemas

# Repo root is three levels up: <root>/command_center/api/service.py
ROOT = Path(__file__).resolve().parents[2]

# How many runs/commits the dashboard feed shows before truncation.
_ACTIVITY_LIMIT = 20
_ATTENTION_LIMIT = 20

# Task launch_status values that mean "needs a human decision" (mirrors
# read_model's task-level attention definition).
_ATTENTION_LAUNCH_STATUS = "Requires Attention"


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def get_health() -> schemas.HealthResponse:
    return schemas.HealthResponse(version=__version__)


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


def _progress_by_project_ref(tasks: list[dict]) -> dict[str, int]:
    """done/total percentage per project namespace, computed from the task
    list. A proxy for progress (there is no stored progress on this surface),
    honest about being derived. Projects with no tasks are absent (progress is
    then ``None`` on the schema)."""
    totals: dict[str, int] = {}
    done: dict[str, int] = {}
    for task in tasks:
        ref = task.get("project")
        if not ref:
            continue
        totals[ref] = totals.get(ref, 0) + 1
        if (task.get("status") or "") == "Done":
            done[ref] = done.get(ref, 0) + 1
    return {
        ref: round(done.get(ref, 0) * 100 / total)
        for ref, total in totals.items()
        if total
    }


def _health_from_record(record: dict) -> schemas.ProjectHealth:
    git = record.get("git") or {}
    github = record.get("github") or {}
    return schemas.ProjectHealth(
        worktree_state=record.get("worktree_state", "unconfigured"),
        git_available=bool(git.get("available")),
        branch=git.get("branch"),
        dirty=bool(git.get("dirty")),
        last_activity=git.get("last_activity"),
        github_available=bool(github.get("available")),
        open_pr_count=github.get("open_pr_count"),
        ci_state=github.get("ci_state"),
    )


def _serialize_project(
    entry: dict, *, progress: dict[str, int], with_health: bool
) -> schemas.Project:
    """Map one registry entry onto a :class:`schemas.Project`, redacting
    sensitive projects. ``with_health`` gates the (potentially slow) collector
    call so the list endpoint can opt out per entry."""
    project_ref = entry.get("project") or ""
    sensitive = bool(project_ref) and is_sensitive(project_ref)

    if sensitive:
        # Drop the operator-supplied name, repo path and every health signal:
        # nothing about a BANK/LEGAL project is exposed on this surface beyond
        # the fact that a redacted entry with this id exists.
        return schemas.Project(
            id=entry.get("id", ""),
            name=project_ref,
            kind=entry.get("kind", "other"),
            project_ref=project_ref,
            healthy=True,
            redacted=True,
        )

    health = None
    healthy = True
    if with_health:
        record = collect_health(entry)
        health = _health_from_record(record)
        healthy = health.worktree_state in ("unconfigured", "ok")

    return schemas.Project(
        id=entry.get("id", ""),
        name=entry.get("name") or entry.get("id", ""),
        kind=entry.get("kind", "other"),
        project_ref=project_ref,
        healthy=healthy,
        redacted=False,
        progress=progress.get(project_ref),
        health=health,
    )


def list_projects(*, with_health: bool = True) -> list[schemas.Project]:
    entries = load_entries()
    progress = _progress_by_project_ref(_safe_load_tasks())
    return [
        _serialize_project(e, progress=progress, with_health=with_health)
        for e in entries
    ]


def get_project(project_id: str) -> schemas.Project | None:
    entry = get_entry(project_id)
    if entry is None:
        return None
    progress = _progress_by_project_ref(_safe_load_tasks())
    return _serialize_project(entry, progress=progress, with_health=True)


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


def _safe_load_tasks() -> list[dict]:
    """The task list, or ``[]`` if the store cannot be read, with BANK/LEGAL
    tasks dropped entirely.

    Redaction happens at this single load seam so *every* task consumer on the
    surface — the list endpoint, its counts, task detail, the dashboard
    attention feed and the per-project progress proxy — inherits it and none can
    leak a sensitive task's title. Sensitive rows are dropped rather than
    field-allowlisted, matching the policy already applied to projects, runs and
    activity. ``load_tasks`` already returns ``[]`` for a missing/torn file in
    its read-only default, so the guard only covers an unexpected raise."""
    try:
        tasks = load_tasks(ROOT)
    except Exception:  # pragma: no cover - defensive; load_tasks degrades itself
        return []
    return [t for t in tasks if not is_sensitive(t.get("project") or "")]


def _serialize_task(
    task: dict, tasks_by_id: dict[str, dict] | None = None
) -> schemas.Task:
    depends_on = list(task.get("depends_on") or [])
    unmet = (
        models.unmet_dependencies(task, tasks_by_id) if tasks_by_id is not None else []
    )
    return schemas.Task(
        id=str(task.get("id", "")),
        project=task.get("project", ""),
        title=task.get("title", ""),
        task_type=task.get("task_type"),
        status=task.get("status"),
        priority=task.get("priority"),
        owner=task.get("owner"),
        launch_status=task.get("launch_status"),
        created_at=task.get("created_at"),
        updated_at=task.get("updated_at"),
        depends_on=depends_on,
        blocked=bool(unmet),
        blocked_by=list(unmet),
    )


def _task_counts(tasks: list[dict]) -> schemas.TaskCounts:
    snap = read_model.task_snapshot(tasks)
    return schemas.TaskCounts(
        total=snap.total,
        by_lane=dict(snap.by_lane),
        other=snap.other,
        done=snap.done,
        blocked=snap.blocked,
        active=snap.active,
        attention=snap.attention,
    )


def list_tasks(
    *, project: str | None = None, status: str | None = None
) -> schemas.TaskList:
    all_tasks = _safe_load_tasks()
    # Blocked state is judged against the *whole* visible set, not the filtered
    # view: a task whose dependency lives in another project is still blocked.
    tasks_by_id = {str(t.get("id", "")): t for t in all_tasks}
    tasks = all_tasks
    if project:
        tasks = [t for t in tasks if t.get("project") == project]
    if status:
        tasks = [t for t in tasks if (t.get("status") or "") == status]
    return schemas.TaskList(
        tasks=[_serialize_task(t, tasks_by_id) for t in tasks],
        counts=_task_counts(tasks),
    )


def get_task(task_id: str) -> schemas.Task | None:
    all_tasks = _safe_load_tasks()
    tasks_by_id = {str(t.get("id", "")): t for t in all_tasks}
    for task in all_tasks:
        if str(task.get("id", "")) == task_id:
            return _serialize_task(task, tasks_by_id)
    return None


def task_graph(project: str | None = None) -> schemas.TaskGraph:
    """One project's tasks as a dependency-levelled graph in priority order.

    The read the task-dependency UI consumes: dependency levels/state come from
    the same ``ui.live_board.project_tree`` the board uses (so "blocked" and
    "ready" mean exactly what they mean elsewhere), the priority order comes from
    the persisted ``priority_rank`` healed to a legal sequence, and every
    ``depends_on``/``blocks`` edge is surfaced so the client can draw the graph
    without a second round trip. Sensitive projects are already dropped by
    ``_safe_load_tasks``.
    """
    from command_center.ui import live_board

    all_tasks = _safe_load_tasks()
    tasks_by_id = {str(t.get("id", "")): t for t in all_tasks}
    scoped = (
        [t for t in all_tasks if t.get("project") == project]
        if project
        else all_tasks
    )

    order = task_ordering.default_order(scoped)
    scoped_by_id = {str(t.get("id", "")): t for t in scoped}
    ordered_tasks = [scoped_by_id[tid] for tid in order]

    tree = live_board.project_tree(scoped, tasks_by_id)
    node_by_id = {n.task_id: n for n in tree}
    has_cycle = task_ordering.find_cycle(scoped) is not None

    nodes: list[schemas.TaskGraphNode] = []
    for rank, task in enumerate(ordered_tasks):
        tid = str(task.get("id", ""))
        tree_node = node_by_id.get(tid)
        edges = models.derive_dependency_edges(task, scoped)
        unmet = models.unmet_dependencies(task, tasks_by_id)
        nodes.append(
            schemas.TaskGraphNode(
                id=tid,
                project=task.get("project", ""),
                title=task.get("title", ""),
                status=task.get("status"),
                priority=task.get("priority"),
                level=tree_node.level if tree_node else 0,
                state=tree_node.state if tree_node else live_board.NODE_WAITING,
                rank=rank,
                depends_on=list(task.get("depends_on") or []),
                blocked=bool(unmet),
                blocked_by=list(unmet),
                blocks=list(edges.get("blocks") or []),
            )
        )
    return schemas.TaskGraph(
        project=project, nodes=nodes, order=order, has_cycle=has_cycle
    )


# --------------------------------------------------------------------------
# Agents / runs
# --------------------------------------------------------------------------


def _load_runs(limit: int | None = None) -> tuple[list[dict], bool]:
    """Recent runs from the runtime read model, newest first, with sensitive
    projects dropped. Returns ``(runs, available)``; ``available`` is ``False``
    when the runtime store has not been initialised yet (a fresh install with
    no ``runtime.db``), in which case the agents surface degrades to an empty,
    documented stub rather than raising."""
    try:
        runs = list_unified_runs(resolve_db_path(ROOT), root=ROOT, limit=limit)
    except Exception:
        return [], False
    visible = [r for r in runs if not _run_is_sensitive(r)]
    return visible, True


def _run_is_sensitive(run: dict) -> bool:
    ref = run.get("project")
    return bool(ref) and is_sensitive(ref)


def _run_summary(runs: list[dict]) -> schemas.AgentSummary:
    snap = read_model.run_snapshot(runs)
    return schemas.AgentSummary(
        running=snap.running,
        queued=snap.queued,
        attention=snap.attention,
        total=snap.total,
    )


def _serialize_agent(run: dict) -> schemas.Agent:
    return schemas.Agent(
        run_id=str(run.get("id", "")),
        source=run.get("source"),
        project=run.get("project"),
        task_id=run.get("task_id"),
        task_type=run.get("task_type"),
        state=run.get("state"),
        status=run.get("status"),
        agent=run.get("agent"),
        created_at=run.get("created_at"),
        started_at=run.get("started_at"),
    )


def list_agents() -> schemas.AgentsResponse:
    runs, available = _load_runs()
    return schemas.AgentsResponse(
        summary=_run_summary(runs),
        agents=[_serialize_agent(r) for r in runs],
        available=available,
    )


# --------------------------------------------------------------------------
# Dashboard aggregate
# --------------------------------------------------------------------------


def _recent_activity(runs: list[dict]) -> list[schemas.ActivityItem]:
    """Merge run events (runs_read) and recent commits (git_readers) into one
    newest-first feed. Sensitive runs are already excluded upstream in
    ``_load_runs``; commits come from the API repo's own log."""
    items: list[schemas.ActivityItem] = []
    for run in runs[:_ACTIVITY_LIMIT]:
        title = run.get("task_type") or run.get("state") or "run"
        items.append(
            schemas.ActivityItem(
                kind="run",
                project=run.get("project"),
                title=str(title),
                ref=str(run.get("id", "")) or None,
                ts=run.get("created_at"),
            )
        )
    for commit in get_git_log(limit=_ACTIVITY_LIMIT):
        items.append(
            schemas.ActivityItem(
                kind="commit",
                project=None,
                title=commit.get("subject", ""),
                ref=commit.get("hash"),
                ts=commit.get("date"),
            )
        )
    return items


def _attention_items(
    tasks: list[dict], runs: list[dict]
) -> list[schemas.AttentionItem]:
    items: list[schemas.AttentionItem] = []
    for task in tasks:
        needs = (task.get("launch_status") or "") == _ATTENTION_LAUNCH_STATUS or bool(
            task.get("regressed_after_done")
        )
        if needs:
            items.append(
                schemas.AttentionItem(
                    kind="task",
                    project=task.get("project"),
                    title=task.get("title", ""),
                    detail=task.get("launch_status") or "regressed after done",
                    ref=str(task.get("id", "")) or None,
                )
            )
    for run in runs:
        if run.get("state") in read_model.RUN_ATTENTION_STATES:
            items.append(
                schemas.AttentionItem(
                    kind="run",
                    project=run.get("project"),
                    title=run.get("task_type") or "run",
                    detail=run.get("state"),
                    ref=str(run.get("id", "")) or None,
                )
            )
    return items[:_ATTENTION_LIMIT]


def build_dashboard() -> schemas.DashboardResponse:
    tasks = _safe_load_tasks()
    runs, _available = _load_runs(limit=_ACTIVITY_LIMIT * 2)
    return schemas.DashboardResponse(
        agents=_run_summary(runs),
        task_counts=_task_counts(tasks),
        projects=list_projects(with_health=True),
        activity=_recent_activity(runs),
        attention=_attention_items(tasks, runs),
    )
