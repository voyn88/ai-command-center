"""Pure read-model / bookkeeping helpers for rendering a task card.

Kept separate from `app.py` — and with zero `st.*` calls — so a future
non-Streamlit UI (the desktop shell documented under `docs/desktop/`) can
reuse the exact same task-card data logic through its own
`command_center.application` adapter layer, per `docs/desktop/
ARCHITECTURE.md` §5/§7 ("adapters call existing functions verbatim... no
forked copies"). `app.py`'s `render_task_card` calls into this module and
only handles widget rendering.
"""

from __future__ import annotations

from pathlib import Path

from command_center import git_info, models, project_config


DEFAULT_TASK_PRIORITY = "Medium"


def kanban_status_options(current_status: str | None) -> list[str]:
    """Stored Kanban statuses plus the task's current legacy/unknown value.

    A Streamlit selectbox immediately exposes its default value. If an unknown
    current status is omitted from the options, the widget defaults to
    ``Backlog`` and the renderer's ordinary ``new != current`` handler writes
    that value back merely because the page was opened. Keeping the current
    value as the first option makes rendering read-only while still letting the
    operator explicitly migrate the task to a canonical lane."""
    canonical = [status for status in models.KANBAN_STATUSES if status != "Done"]
    if current_status == "Done":
        return ["Done", *canonical]
    if current_status and current_status not in canonical:
        return [current_status, *canonical]
    return canonical


def kanban_priority_options(tasks: list[dict]) -> list[str]:
    """The priority values the Kanban priority filter must offer as options.

    Returns the canonical `models.TASK_PRIORITIES` in their canonical order,
    followed by any *other* priority value actually present on a task (e.g. a
    task imported with a `P0`/`P1` scheme that isn't in the canonical set),
    de-duplicated, order-preserved.

    Why this exists: the filter used to offer only `models.TASK_PRIORITIES`
    and keep a task iff `task["priority"] in selection`. A task whose priority
    was outside that set (`AICC-CI-001`, priority `"P0"`) was therefore never
    an option *and* never matched the default all-selected filter, so it was
    silently dropped from every lane — present in `tasks.json`, invisible in
    the UI. Sourcing the options from the tasks themselves means an unknown
    priority is always shown and stays user-toggleable, instead of vanishing.
    """
    options = list(models.TASK_PRIORITIES)
    seen = set(options)
    for task in tasks:
        priority = task.get("priority", DEFAULT_TASK_PRIORITY)
        if priority not in seen:
            seen.add(priority)
            options.append(priority)
    return options


def filter_kanban_tasks(
    tasks: list[dict],
    *,
    project: str | None,
    priorities: list[str],
) -> list[dict]:
    """Tasks visible on the Kanban board for the given project and selected
    priorities. `project=None` means "all projects". Pure — the exact
    predicate `app.py` applies, extracted so it is regression-testable
    without Streamlit.

    The project comparison is done on canonical project ids, not raw strings:
    the Kanban project selector emits canonical `models.PROJECT_IDS` values
    (e.g. `"AICC"`), but a task's stored `project` may be a display name or
    alias (`AICC-CI-001` carries `"AI Command Center"`). Comparing the raw
    strings dropped every such task the moment a project was selected —
    present in `tasks.json`, rendered on the "all projects" board, yet
    invisible under its own project lane (AICC-UI-001). Normalizing both
    sides via `project_config.normalize_project_id` makes id, display name,
    and alias all resolve to the same lane. The canonicalization lives in the
    single shared `project_config.project_matches` helper every project-scoped
    filter/counter/ranking in the app now routes through, so they can never
    drift apart again."""
    return [
        task
        for task in tasks
        if project_config.project_matches(task.get("project"), project)
        and task.get("priority", DEFAULT_TASK_PRIORITY) in priorities
    ]


def cached_git_status(workspace_path: str | None, cache: dict[str, dict]) -> dict:
    """Memoized git status lookup — one subprocess call per unique
    workspace path per render pass, however many tasks share that repo."""
    if not workspace_path:
        return {"is_repo": False}
    if workspace_path not in cache:
        cache[workspace_path] = git_info.get_status(Path(workspace_path))
    return cache[workspace_path]


def set_manual_launch_status(task: dict, status: str, note: str) -> None:
    """Pause/Resume/Restart bookkeeping — advisory status only. See
    `command_center.launch`'s module docstring for why this can't be real
    process control for the synchronous v1.1 runner."""
    task["launch_status"] = status
    task["updated_at"] = models.iso_now()
    event_type = "launch_requires_attention" if status == "Failed" else "executor_started"
    models.append_timeline_event(task, event_type, note)


def sorted_timeline(task: dict) -> list[dict]:
    """Newest-first timeline events — plain data, no formatting/markup."""
    return sorted(task.get("timeline") or [], key=lambda event: event.get("ts", ""), reverse=True)


def dependency_narrative(task: dict, tasks_by_id: dict[str, dict]) -> list[str]:
    """Plain-language sentences explaining why this task's dependency chain
    matters, for a reader who has never seen `depends_on`/`blocks`/
    `parent_task_id` and shouldn't need to. Each sentence names the concrete
    other task and the concrete consequence ("X waits on Y" / "finishing this
    unblocks Z"), never the graph jargon.

    This is the accessible counterpart to `dependency_graph_dot`: a
    Graphviz chart is an image with no text alternative, so a screen-reader
    user (or anyone who just doesn't read node-and-arrow diagrams) gets
    nothing from it today. Returns `[]` under the same "nothing to explain"
    condition `dependency_graph_dot` uses to return `None`, so callers can
    share one empty-state check.
    """
    lines: list[str] = []

    def label(other_id: str, other: dict | None) -> str:
        title = other.get("title") if other else None
        return title or f"(задача {other_id[:8]} удалена)"

    depends_on = task.get("depends_on") or []
    unmet = set(models.unmet_dependencies(task, tasks_by_id))
    for dep_id in depends_on:
        title = label(dep_id, tasks_by_id.get(dep_id))
        if dep_id in unmet:
            lines.append(f"Эта задача ждёт «{title}»: пока та не будет готова, эта не начнётся.")
        else:
            lines.append(f"«{title}» уже готово — эта задача может продолжаться.")

    edges = models.derive_dependency_edges(task, list(tasks_by_id.values()))
    for blocked_id in edges["blocks"]:
        title = label(blocked_id, tasks_by_id.get(blocked_id))
        lines.append(f"Пока эта задача не будет готова, не сможет начаться «{title}».")

    parent_id = task.get("parent_task_id")
    if parent_id:
        title = label(parent_id, tasks_by_id.get(parent_id))
        lines.append(f"Это часть более крупной задачи «{title}» — от этого шага зависит, когда та будет готова.")

    for child_id in edges["children"]:
        title = label(child_id, tasks_by_id.get(child_id))
        lines.append(f"У этой задачи есть подзадача «{title}» — её ход тоже влияет на общий результат.")

    return lines


def dependency_graph_dot(task: dict, tasks_by_id: dict[str, dict]) -> str | None:
    """Builds Graphviz DOT source for a task's dependency neighborhood
    (depends_on / blocks / parent / children). Returns `None` when there is
    nothing to draw. Rendering (`st.graphviz_chart`) stays in `app.py` —
    this function returns plain text, not a UI element, so it's reusable
    from a non-Streamlit renderer too."""
    edges = models.derive_dependency_edges(task, list(tasks_by_id.values()))
    depends_on = task.get("depends_on") or []
    parent_id = task.get("parent_task_id")
    if not (depends_on or edges["blocks"] or edges["children"] or parent_id):
        return None

    def node_label(task_id: str) -> str:
        other = tasks_by_id.get(task_id)
        label = (other.get("title") if other else None) or task_id[:8]
        return label.replace('"', "'")[:40]

    lines = ["digraph {", "rankdir=LR;", 'node [shape=box, style="rounded,filled", fillcolor="#eef2ff"];']
    lines.append(f'"{task["id"]}" [label="{node_label(task["id"])}", fillcolor="#c7d2fe"];')
    for dep_id in depends_on:
        lines.append(f'"{dep_id}" [label="{node_label(dep_id)}"];')
        lines.append(f'"{dep_id}" -> "{task["id"]}";')
    for blocked_id in edges["blocks"]:
        lines.append(f'"{blocked_id}" [label="{node_label(blocked_id)}"];')
        lines.append(f'"{task["id"]}" -> "{blocked_id}";')
    if parent_id:
        lines.append(f'"{parent_id}" [label="{node_label(parent_id)}", fillcolor="#fef3c7"];')
        lines.append(f'"{parent_id}" -> "{task["id"]}" [style=dashed];')
    for child_id in edges["children"]:
        lines.append(f'"{child_id}" [label="{node_label(child_id)}", fillcolor="#fef3c7"];')
        lines.append(f'"{task["id"]}" -> "{child_id}" [style=dashed];')
    lines.append("}")
    return "\n".join(lines)
