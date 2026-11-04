"""Board model for the Live Execution Center: how sessions are bucketed into
the operator's reading order, and whether a task may be launched right now.

Pure functions over already-built session views (`runtime.session_view`) and
plain task records. No Streamlit, no database, no side effects — the renderer
in `app.py` decides *how* to draw a bucket, this module decides *what goes in
it* and *why a launch button is disabled*. That split is what makes the board's
behaviour testable without an `AppTest` harness, and it is why the launch gate
lives here rather than inline next to the button it disables.

The gate deliberately reuses the reason-code vocabulary the autopilot planner
already reports (`waiting_dependency`, `workspace_busy`,
`duplicate_task_assignment`, `workspace_not_configured`) — see
`docs/desktop/AUTOPILOT_OPERATOR_GUIDE.md` §3. An operator who has learned what
`workspace_busy` means on a planned wave must not have to learn a second word
for the identical condition on a launch button.

This gate is an *affordance*, not a safety boundary: it stops an operator from
pressing a button whose launch would be refused anyway. The real, fail-closed
enforcement stays where it already is — `launch_service.find_active_run_conflict`
and the isolation check in `Supervisor.start_raw`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from command_center import models, tasks_repository
from command_center.runtime import session_view

# The board's four buckets, in the order an operator reads them. "What is
# happening now" first, "what needs me" second; everything terminal is
# history and collapses.
#
# This ordering is the whole point of the board: the previous layout led with
# the planner's wave and a project grid, so the three runs actually executing
# sat below a screenful of context and were reached by scrolling.
BUCKET_LIVE = "live"
BUCKET_ATTENTION = "attention"
BUCKET_WAITING = "waiting"
BUCKET_DONE = "done"

BUCKET_ORDER: tuple[str, ...] = (BUCKET_LIVE, BUCKET_ATTENTION, BUCKET_WAITING, BUCKET_DONE)

BUCKET_TITLES: dict[str, str] = {
    BUCKET_LIVE: "Выполняется",
    BUCKET_ATTENTION: "Требуют внимания",
    BUCKET_WAITING: "Ожидают запуска",
    BUCKET_DONE: "Завершено",
}

# `Cancelled` sits in `done` rather than `attention`: a run the operator
# themselves stopped is a closed matter, not an unread problem.
_BUCKET_BY_STATUS: dict[str, str] = {
    **{status: BUCKET_LIVE for status in session_view.LIVE_PROCESS_DISPLAY_STATUSES},
    session_view.STATUS_WAITING: BUCKET_WAITING,
    session_view.STATUS_LAUNCHING: BUCKET_WAITING,
    session_view.STATUS_REQUIRES_ATTENTION: BUCKET_ATTENTION,
    session_view.STATUS_BLOCKED: BUCKET_ATTENTION,
    session_view.STATUS_INCOMPLETE: BUCKET_ATTENTION,
    session_view.STATUS_FAILED: BUCKET_ATTENTION,
    session_view.STATUS_COMPLETED: BUCKET_DONE,
    session_view.STATUS_CANCELLED: BUCKET_DONE,
}


def bucket_for_status(status: str) -> str:
    """Which board bucket a display status belongs to. An unknown status is
    treated as needing attention rather than being hidden — a status this
    module has not been taught about is exactly the kind of thing an operator
    should see, not the kind that should silently vanish from the board."""
    return _BUCKET_BY_STATUS.get(status, BUCKET_ATTENTION)


def superseded_run_ids(sessions: list[dict]) -> frozenset[str]:
    """Run ids that are *not* the latest run of their task — an earlier attempt
    that a newer run for the same task has moved past.

    A task that failed once and then succeeded (or is running again) has two
    runs: the old failed one should not keep sitting in "Requires Attention" as
    if the task still needs a human, because a newer attempt already superseded
    it. This returns exactly those stale run ids so the board can drop them from
    the attention bucket. Ad-hoc runs (no `task_id`) are never superseded — each
    stands alone.
    """
    latest_by_task: dict[str, dict] = {}
    for session in sessions:
        task_id = session.get("task_id")
        if not task_id:
            continue
        current = latest_by_task.get(task_id)
        if current is None or (session.get("started_at") or "") > (current.get("started_at") or ""):
            latest_by_task[task_id] = session
    latest_ids = {s.get("run_id") for s in latest_by_task.values()}
    return frozenset(
        s["run_id"]
        for s in sessions
        if s.get("task_id") and s.get("run_id") not in latest_ids
    )


def completed_task_run_ids(
    sessions: list[dict], tasks_by_id: dict[str, dict]
) -> frozenset[str]:
    """Run ids whose linked task is already resolved as ``Done``.

    A failed historical attempt remains valuable in the run journal, but it is
    no longer actionable work once the canonical task record is ``Done``.
    Keeping such a run in the attention bucket invites an operator to launch a
    needless remediation against already-delivered code.
    """
    return frozenset(
        session["run_id"]
        for session in sessions
        if session.get("task_id")
        and tasks_by_id.get(session["task_id"], {}).get("status") == "Done"
    )


def split_board(sessions: list[dict], *, display_status: str = "status") -> dict[str, list[dict]]:
    """Bucket `sessions` for the board, newest-started first inside each
    bucket. Every bucket key in `BUCKET_ORDER` is always present, so a caller
    can render "0" without a membership check.

    `display_status` names the key holding the *display* status — `app.py`
    passes its own guarded value (`Completed` is never shown below 100 %),
    which is why this is a parameter rather than a hard-coded `"status"`.
    """
    board: dict[str, list[dict]] = {bucket: [] for bucket in BUCKET_ORDER}
    for session in sessions:
        board[bucket_for_status(session.get(display_status) or "")].append(session)
    for bucket_sessions in board.values():
        bucket_sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    return board


# --------------------------------------------------------------------------
# Launch gate
# --------------------------------------------------------------------------

GATE_OK = "ok"
GATE_READ_ONLY = "read_only_master_task"
GATE_ALREADY_DONE = "already_done"
GATE_WAITING_DEPENDENCY = "waiting_dependency"
GATE_DUPLICATE = "duplicate_task_assignment"
GATE_WORKSPACE_BUSY = "workspace_busy"
GATE_WORKSPACE_NOT_CONFIGURED = "workspace_not_configured"

# What the operator should *do*, per code. Kept beside the codes so the
# button's tooltip and the planner's wave give the same advice.
GATE_ACTIONS: dict[str, str] = {
    GATE_READ_ONLY: "Задача центрального бэклога — управление через конвейер, не через консоль.",
    GATE_ALREADY_DONE: "Задача уже завершена — запускать нечего.",
    GATE_WAITING_DEPENDENCY: "Сначала завершите зависимости: «Done» требует подтверждённого merge.",
    GATE_DUPLICATE: "У задачи уже есть активная попытка — дождитесь её или отмените.",
    GATE_WORKSPACE_BUSY: "Этот workspace занят другим прогоном — дождитесь его или отмените.",
    GATE_WORKSPACE_NOT_CONFIGURED: "Укажите `workspace_path` задачи или `repository_path` проекта.",
}


@dataclass(frozen=True)
class LaunchGate:
    """Whether this task may be launched from the board right now.

    `blocking_run_id` is set only for the two conflict codes, so the renderer
    can link straight to the run that is in the way instead of making the
    operator hunt for it.
    """

    allowed: bool
    code: str
    reason: str
    action: str = ""
    blocking_run_id: str | None = None
    unmet: tuple[str, ...] = ()

    @property
    def is_conflict(self) -> bool:
        """A conflict clears on its own once the blocking run ends; the other
        blocked codes need the operator to change something."""
        return self.code in (GATE_DUPLICATE, GATE_WORKSPACE_BUSY)


def _resolve(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return None


def launch_gate(
    task: dict,
    *,
    tasks_by_id: dict[str, dict],
    active_runs: list[dict],
    project_repository_path: str | None = None,
) -> LaunchGate:
    """Decide whether `task` can start now.

    `active_runs` must already be filtered to `EXECUTION_CENTER_ACTIVE_STATES`
    — this function performs no queries, so the caller reads the run table once
    for the whole board instead of once per rendered button.

    Checks run cheapest-and-most-final first: a `Done` task and an unmet
    dependency are properties of the task itself, while the two conflict checks
    depend on what happens to be running this second.

    A master-projection record (the read-only backlog fallback view, stamped
    `source: "master"` by `backlog_client.projection_board_tasks`) is refused
    before any other check: `save_tasks` already drops such records
    structurally on write, so allowing this gate to say "launchable" would be
    a promise the write path cannot keep — the operator would see a run start
    and then watch its task-state update vanish into a warning log instead of
    landing on the task it was meant to update.
    """
    if tasks_repository.is_master_projection_task(task):
        return LaunchGate(
            False,
            GATE_READ_ONLY,
            "Задача центрального бэклога (read-only) — запуск с консоли недоступен.",
            GATE_ACTIONS[GATE_READ_ONLY],
        )

    if (task.get("status") or "") == "Done":
        return LaunchGate(False, GATE_ALREADY_DONE, "Задача уже в статусе Done.", GATE_ACTIONS[GATE_ALREADY_DONE])

    unmet = tuple(models.unmet_dependencies(task, tasks_by_id))
    if unmet:
        titles = ", ".join((tasks_by_id.get(dep, {}).get("title") or dep)[:40] for dep in unmet[:3])
        more = f" (+{len(unmet) - 3})" if len(unmet) > 3 else ""
        return LaunchGate(
            False,
            GATE_WAITING_DEPENDENCY,
            f"Не завершены зависимости: {titles}{more}.",
            GATE_ACTIONS[GATE_WAITING_DEPENDENCY],
            unmet=unmet,
        )

    task_id = task.get("id")
    for run in active_runs:
        if task_id and run.get("task_id") == task_id:
            return LaunchGate(
                False,
                GATE_DUPLICATE,
                "У задачи уже есть активная попытка.",
                GATE_ACTIONS[GATE_DUPLICATE],
                blocking_run_id=run.get("id"),
            )

    workspace = _resolve(task.get("workspace_path") or task.get("repository_path") or project_repository_path)
    if not workspace:
        return LaunchGate(
            False,
            GATE_WORKSPACE_NOT_CONFIGURED,
            "Не удалось определить workspace задачи.",
            GATE_ACTIONS[GATE_WORKSPACE_NOT_CONFIGURED],
        )

    for run in active_runs:
        if _resolve(run.get("repository_path")) == workspace:
            return LaunchGate(
                False,
                GATE_WORKSPACE_BUSY,
                "Workspace занят другим активным прогоном.",
                GATE_ACTIONS[GATE_WORKSPACE_BUSY],
                blocking_run_id=run.get("id"),
            )

    return LaunchGate(True, GATE_OK, "Готова к запуску.")


# --------------------------------------------------------------------------
# Capacity — how loaded the machine is and how many free slots remain
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentLoad:
    """One agent's live load: how many runs it holds against its own cap."""

    agent_id: str
    used: int
    max_concurrency: int
    available: bool

    @property
    def free(self) -> int:
        """Free slots on this agent right now — zero if it is unavailable, so a
        down agent never counts as spare capacity."""
        if not self.available:
            return 0
        return max(0, self.max_concurrency - self.used)


@dataclass(frozen=True)
class CapacitySummary:
    """The whole machine's execution capacity in one value: global load, free
    global slots, and per-agent breakdown. Pure — built from a scheduler load
    snapshot plus the configured limits, no I/O — so the side panel can render
    it and a test can assert it without a live `runtime.db`."""

    global_running: int
    global_limit: int
    agents: tuple[AgentLoad, ...]

    @property
    def global_free(self) -> int:
        """Free global slots — bounded below by 0 and never more than the sum of
        the agents' own free slots (an idle global limit is not usable capacity
        if every agent that could take work is full or down)."""
        by_limit = max(0, self.global_limit - self.global_running)
        return min(by_limit, self.agents_free)

    @property
    def agents_free(self) -> int:
        return sum(a.free for a in self.agents)

    @property
    def free_agent_count(self) -> int:
        """How many distinct agents have at least one free slot right now."""
        return sum(1 for a in self.agents if a.free > 0)

    @property
    def saturated(self) -> bool:
        return self.global_free == 0


def capacity_summary(
    *,
    running_by_agent: dict[str, int],
    global_running: int,
    global_limit: int,
    agents: list[tuple[str, int, bool]],
) -> CapacitySummary:
    """Build a `CapacitySummary` from a scheduler load snapshot and the agent
    registry. `agents` is `(agent_id, max_concurrency, available)` per registered
    agent — exactly what `scheduler.default_registry` knows."""
    loads = tuple(
        AgentLoad(
            agent_id=agent_id,
            used=int(running_by_agent.get(agent_id, 0)),
            max_concurrency=int(max_concurrency),
            available=bool(available),
        )
        for agent_id, max_concurrency, available in agents
    )
    return CapacitySummary(
        global_running=int(global_running),
        global_limit=int(global_limit),
        agents=loads,
    )


# --------------------------------------------------------------------------
# Dependency tree
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyNode:
    """One line of the rendered dependency tree."""

    task_id: str
    title: str
    status: str
    depth: int
    relation: str  # "depends_on" | "blocks" | "parent" | "child"
    done: bool


_RELATION_MARKS: dict[str, str] = {
    "depends_on": "⬅",
    "blocks": "➡",
    "parent": "⬆",
    "child": "⬇",
}


def relation_mark(relation: str) -> str:
    return _RELATION_MARKS.get(relation, "•")


def dependency_tree(
    task: dict, tasks_by_id: dict[str, dict], *, max_depth: int = 3
) -> list[DependencyNode]:
    """The task's upstream chain, deepest-first, plus its immediate downstream
    and family links.

    Upstream (`depends_on`) is walked transitively up to `max_depth`, because
    "what is actually holding this task" is frequently two hops away — a direct
    dependency that is itself waiting. Downstream and parent/child links are
    shown one hop only: they are context, not the blocking chain.

    Cycles are tolerated: a task already emitted is never expanded twice, so a
    mutually-dependent pair renders as two lines instead of recursing forever.
    """
    nodes: list[DependencyNode] = []
    seen: set[str] = {task.get("id") or ""}

    def walk_upstream(current: dict, depth: int) -> None:
        if depth > max_depth:
            return
        for dep_id in current.get("depends_on") or []:
            if dep_id in seen:
                continue
            seen.add(dep_id)
            dep = tasks_by_id.get(dep_id)
            status = (dep or {}).get("status") or "неизвестна"
            nodes.append(
                DependencyNode(
                    task_id=dep_id,
                    title=(dep or {}).get("title") or dep_id,
                    status=status,
                    depth=depth,
                    relation="depends_on",
                    done=status == "Done",
                )
            )
            if dep is not None:
                walk_upstream(dep, depth + 1)

    walk_upstream(task, 1)

    edges = models.derive_dependency_edges(task, list(tasks_by_id.values()))
    for relation, ids in (("parent", [task.get("parent_task_id")] if task.get("parent_task_id") else []),
                          ("blocks", edges.get("blocks") or []),
                          ("child", edges.get("children") or [])):
        for other_id in ids:
            if not other_id or other_id in seen:
                continue
            seen.add(other_id)
            other = tasks_by_id.get(other_id)
            status = (other or {}).get("status") or "неизвестна"
            nodes.append(
                DependencyNode(
                    task_id=other_id,
                    title=(other or {}).get("title") or other_id,
                    status=status,
                    depth=1,
                    relation=relation,
                    done=status == "Done",
                )
            )
    return nodes


# --------------------------------------------------------------------------
# Project tree — a project's tasks as dependency levels
# --------------------------------------------------------------------------

# What a task is *doing*, as opposed to which Kanban column it sits in. The
# board colours by this, so "merged and verified" reads differently from
# "sitting in Done's column", and "ready to start" reads differently from
# "waiting on something".
NODE_DONE = "done"
NODE_RUNNING = "running"
NODE_BLOCKED = "blocked"
NODE_READY = "ready"
NODE_WAITING = "waiting"

# Colour per state, as Streamlit semantic tones (see .streamlit/config.toml —
# these are the same green/orange/red/blue/gray the status badges use, so one
# colour means one thing everywhere in the app).
NODE_COLORS: dict[str, str] = {
    NODE_DONE: "green",
    NODE_RUNNING: "orange",
    NODE_BLOCKED: "red",
    NODE_READY: "blue",
    NODE_WAITING: "gray",
}

NODE_MARKS: dict[str, str] = {
    NODE_DONE: "✅",
    NODE_RUNNING: "🟡",
    NODE_BLOCKED: "🔴",
    NODE_READY: "🔵",
    NODE_WAITING: "⚪️",
}

NODE_LABELS: dict[str, str] = {
    NODE_DONE: "смёржено",
    NODE_RUNNING: "выполняется",
    NODE_BLOCKED: "отказ",
    NODE_READY: "готова к запуску",
    NODE_WAITING: "ждёт зависимости",
}


@dataclass(frozen=True)
class ProjectNode:
    """One task in a project's dependency-levelled tree."""

    task_id: str
    title: str
    status: str
    level: int
    state: str
    priority: str | None = None
    is_next: bool = False

    @property
    def color(self) -> str:
        return NODE_COLORS.get(self.state, "gray")

    @property
    def mark(self) -> str:
        return NODE_MARKS.get(self.state, "•")

    @property
    def state_label(self) -> str:
        return NODE_LABELS.get(self.state, self.state)


def _node_state(task: dict, tasks_by_id: dict[str, dict], running_task_ids: frozenset[str]) -> str:
    if (task.get("status") or "") == "Done":
        return NODE_DONE
    if task.get("id") in running_task_ids:
        return NODE_RUNNING
    if (task.get("status") or "") == "Blocked" or (task.get("launch_status") or "") == "Failed":
        return NODE_BLOCKED
    return NODE_WAITING if models.unmet_dependencies(task, tasks_by_id) else NODE_READY


def project_tree(
    project_tasks: list[dict],
    tasks_by_id: dict[str, dict],
    *,
    running_task_ids: frozenset[str] = frozenset(),
) -> list[ProjectNode]:
    """A project's tasks ordered into dependency *levels*.

    Level 0 is everything that depends on nothing still open; level N depends
    on something at level N-1. That is the order the work can actually be
    done in, and it is the question a project view has to answer — "what is
    finished, what is running, and what unlocks next" — which a flat list of
    titles sorted by status cannot.

    Levels are computed by longest-path relaxation over `depends_on`, bounded
    by the number of tasks: a dependency cycle therefore stops rather than
    recursing, and its members settle at the highest level reached. Dependencies
    on tasks outside `project_tasks` (another project's work) are honoured for
    *state* — an unmet cross-project dependency still makes a task `waiting` —
    but do not contribute a level, since the level axis is this project's own
    sequence.

    The first `ready` task at the lowest open level is flagged `is_next`: with
    levels executed a level at a time, that is the task to start now.
    """
    ids = [t.get("id") for t in project_tasks if t.get("id")]
    id_set = set(ids)
    by_id = {t["id"]: t for t in project_tasks if t.get("id")}

    level: dict[str, int] = {task_id: 0 for task_id in ids}
    for _ in range(len(ids)):
        changed = False
        for task_id in ids:
            deps = [d for d in (by_id[task_id].get("depends_on") or []) if d in id_set]
            if not deps:
                continue
            candidate = max(level[d] for d in deps) + 1
            if candidate > level[task_id]:
                level[task_id] = candidate
                changed = True
        if not changed:
            break

    nodes: list[ProjectNode] = []
    for task_id in ids:
        task = by_id[task_id]
        nodes.append(
            ProjectNode(
                task_id=task_id,
                title=task.get("title") or task_id,
                status=task.get("status") or "—",
                level=level[task_id],
                state=_node_state(task, tasks_by_id, running_task_ids),
                priority=task.get("priority"),
            )
        )

    nodes.sort(key=lambda n: (n.level, n.title))

    # "What to start now": the first ready task on the lowest level that is
    # not already fully done. Flagged rather than reordered — the tree's value
    # is that its order is the dependency order, not a priority ranking.
    open_levels = sorted({n.level for n in nodes if n.state != NODE_DONE})
    for open_level in open_levels:
        candidates = [n for n in nodes if n.level == open_level and n.state == NODE_READY]
        if candidates:
            chosen = candidates[0]
            nodes = [
                ProjectNode(
                    task_id=n.task_id, title=n.title, status=n.status, level=n.level,
                    state=n.state, priority=n.priority, is_next=n.task_id == chosen.task_id,
                )
                for n in nodes
            ]
            break

    return nodes


def project_progress(nodes: list[ProjectNode]) -> tuple[int, int]:
    """`(done, total)` for a project tree — the numerator a progress bar needs
    without the renderer re-counting states it does not own."""
    return sum(1 for n in nodes if n.state == NODE_DONE), len(nodes)
