"""Read-only project wave planner over the canonical AICC backlog."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from graphlib import CycleError, TopologicalSorter
from dataclasses import dataclass


SNAPSHOT_SQL = """SELECT json_build_object('as_of', now(), 'tasks', (
    SELECT coalesce(json_agg(json_build_object(
        'id', t.task_id, 'title', t.title, 'repo', t.repo, 'status', t.status,
        'wave', t.wave, 'priority', t.priority, 'class', t.task_class,
        'kind', t.kind, 'revision', t.revision,
        'acceptance', EXISTS (
            SELECT 1 FROM backlog_evidence e WHERE e.task_id = t.task_id
            AND e.kind = 'acceptance')
    ) ORDER BY t.task_id), '[]') FROM backlog_task t
), 'edges', (
    SELECT coalesce(json_agg(json_build_array(task_id, depends_on_task_id)), '[]')
    FROM backlog_dependency
), 'claims', (
    SELECT coalesce(json_agg(DISTINCT task_id), '[]') FROM work_item_public
    WHERE queue = 'execution' AND state IN ('ready', 'claimed')
))"""

def canonical_wave(value):
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal("Infinity")


@dataclass(frozen=True)
class Decision:
    task: dict
    project: str
    wave: int | None
    stage: str
    blockers: tuple
    impact: tuple[int, int, int]


def build_plan(snapshot, route_for):
    """Recompute project waves and stages from one consistent snapshot."""
    def project(repo):
        route = route_for(repo) if repo else None
        return route[0] if route else None

    tasks = {task["id"]: task for task in snapshot["tasks"]}
    dependencies = defaultdict(set)
    successors = defaultdict(set)
    for child, parent in snapshot["edges"]:
        dependencies[child].add(parent)
        successors[parent].add(child)
    claimed = set(snapshot["claims"])
    accepted = {key for key, task in tasks.items()
                if task["status"] == "DONE" and task["acceptance"]}
    completed = {key for key, task in tasks.items() if task["status"] == "DONE"}
    active = {"OPEN", "IN_PROGRESS", "READY_TO_REVIEW"}
    candidates = {key for key, task in tasks.items()
                  if task["status"] in active and task["kind"] == "task"
                  and project(task["repo"])}
    unresolved = {key: dependencies[key] - completed for key in candidates}

    # Only traverse feasible dependency paths. Nodes with external blockers
    # and dependency cycles stay unplanned, as do their descendants.
    graph = TopologicalSorter()
    external = {key for parents in unresolved.values() for key in parents
                if key not in candidates}
    for key, parents in unresolved.items():
        graph.add(key, *parents)
    try:
        graph.prepare()
    except CycleError:
        pass  # The acyclic portion remains available through get_ready().
    waves = {}
    while graph.is_active():
        ready = graph.get_ready()
        for key in ready:
            if key not in external:
                waves[key] = 1 + max((waves[parent] for parent in unresolved[key]), default=0)
                graph.done(key)
        # Unfinished external roots deliberately block their dependents.
        if not ready:
            break

    decisions = []
    for key, task in tasks.items():
        repo_project = project(task["repo"])
        pending = dependencies[key] - completed
        if task["status"] == "DONE":
            stage = "Принято" if key in accepted else "Сверить приёмку"
        elif task["status"] == "READY_TO_REVIEW":
            stage = "Ревью и приёмка"
        elif task["status"] == "IN_PROGRESS":
            stage = "В работе" if key in claimed else "Сверить выполнение"
        elif task["status"] != "OPEN" or task["kind"] != "task":
            stage = "Уточнение / вне плана"
        elif not repo_project:
            stage = "Уточнить проект"
        elif pending:
            stage = "Ждёт зависимостей" if key in waves else "Заблокировано"
        elif key in claimed:
            stage = "Уже в очереди"
        else:
            stage = "Проверить допуск"
        downstream = {child for child in successors[key]
                      if child in tasks and tasks[child]["status"] in active}
        # Indicators, not measured minutes, quality or spend. A pipeline
        # improvement is reusable only as a hypothesis until accepted.
        impact = (len(downstream),
                  sum(tasks[child]["kind"] == "gate" for child in downstream),
                  len(downstream) if task["class"] == "pipeline" else 0)
        decisions.append(Decision(task, repo_project or "Без маршрута", waves.get(key),
                                  stage, tuple(sorted(pending)), impact))
    return sorted(decisions, key=lambda d: (
        d.project, d.wave is None, d.wave or 0, canonical_wave(d.task["wave"]),
        d.task["priority"] or "P9", -min(d.impact), -sum(d.impact), d.task["id"],
    ))
