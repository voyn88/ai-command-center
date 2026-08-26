"""Priority-ordering domain rules for tasks (VOYN-W2-TASKS).

A task's *priority order* is the explicit sequence an operator wants their work
picked up in — distinct from the coarse ``priority`` label (Low/Medium/High/
Critical) and from the dependency *levels* ``ui.live_board.project_tree``
derives. This module owns the single hard rule that ordering must obey:

    **A task may never be prioritised above a task it depends on.**

If ``A`` declares ``depends_on=[B]`` then ``B`` must be delivered first, so ``B``
has to sit *earlier* (higher priority) in any valid order. An order that places
``A`` above ``B`` is rejected — that is the acceptance invariant of this task
("reordering never breaks dependency invariants"). The same validation also
rejects any order over a set that contains a dependency **cycle**, so a reorder
can neither introduce nor mask one.

Namespace-aware, exactly like ``project_tree``: the ordering set handed in is a
single project's tasks, and only ``depends_on`` edges whose target is *inside*
that set constrain the order. A cross-project dependency still makes a task
*blocked* (``command_center.models.unmet_dependencies``), but it does not
participate in this project's private sequence — it cannot be reordered here.

Pure functions over plain task dicts: no Streamlit, no I/O, no persistence. The
API service layer (``command_center.api.service.reorder_tasks``) is what loads
the store, calls :func:`validated_order` here, and commits the result through
the single writer (``tasks_repository``). Keeping the rule here — not inline
next to the drag control — is what lets a plain ``pytest`` prove the invariant
without an ``AppTest`` harness.
"""

from __future__ import annotations

from command_center import models

PRIORITY_RANK_FIELD = "priority_rank"


class ReorderError(ValueError):
    """A requested order is rejected because it would break a dependency rule.

    Carries a human-readable, operator-facing ``message`` (Russian, matching the
    rest of the surface) plus the machine-readable ``code`` the UI switches on to
    decide *which* error banner to paint.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


CODE_UNKNOWN_TASK = "unknown_task"
CODE_DUPLICATE = "duplicate_task"
CODE_CYCLE = "dependency_cycle"
CODE_DEPENDENCY_ORDER = "dependency_order"


def _intra_set_dependencies(task: dict, id_set: set[str]) -> list[str]:
    """The task's ``depends_on`` restricted to ids present in the ordering set.

    This is the namespace filter: a dependency on another project's task (absent
    from ``id_set``) is honoured elsewhere for *blocked* state, but it never
    constrains this set's private priority order, so it is dropped here.
    """
    self_id = task.get("id")
    return [
        dep
        for dep in (task.get("depends_on") or [])
        if dep in id_set and dep != self_id
    ]


def find_cycle(tasks: list[dict]) -> list[str] | None:
    """Return one dependency cycle among ``tasks`` (as an id path), or ``None``.

    Edges are ``depends_on`` restricted to the given set — a cross-project
    dependency cannot form a cycle *here*. Iterative DFS with a colour marking
    (white/grey/black) so a large graph cannot blow the recursion stack; the
    grey → grey back-edge is the cycle, returned as the id path that closes it.
    """
    by_id = {t["id"]: t for t in tasks if t.get("id")}
    id_set = set(by_id)
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {node: WHITE for node in by_id}

    for root in by_id:
        if colour[root] != WHITE:
            continue
        # Stack frames carry the node plus its not-yet-visited dependency index,
        # and ``path`` mirrors the current grey chain for cycle reconstruction.
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = [root]
        colour[root] = GREY
        while stack:
            node, idx = stack[-1]
            deps = _intra_set_dependencies(by_id[node], id_set)
            if idx < len(deps):
                stack[-1] = (node, idx + 1)
                dep = deps[idx]
                if colour[dep] == GREY:
                    return path[path.index(dep):] + [dep]
                if colour[dep] == WHITE:
                    colour[dep] = GREY
                    stack.append((dep, 0))
                    path.append(dep)
            else:
                colour[node] = BLACK
                stack.pop()
                path.pop()
    return None


def validated_order(order: list[str], tasks_by_id: dict[str, dict]) -> list[str]:
    """Return ``order`` unchanged if it is a legal priority order, else raise.

    Legal means, over the set of tasks named in ``order``:

    * every id is known and appears exactly once;
    * the ``depends_on`` graph restricted to the set is acyclic; and
    * no task precedes (out-ranks) any of its in-set dependencies.

    Raises :class:`ReorderError` with a specific ``code``/``message`` on the
    first violation found, so the caller never persists a half-checked order.
    """
    id_set = set(order)
    if len(id_set) != len(order):
        raise ReorderError(CODE_DUPLICATE, "Порядок содержит задачу дважды.")

    tasks: list[dict] = []
    for task_id in order:
        task = tasks_by_id.get(task_id)
        if task is None:
            raise ReorderError(
                CODE_UNKNOWN_TASK, f"Задача {task_id} не найдена в наборе."
            )
        tasks.append(task)

    cycle = find_cycle(tasks)
    if cycle is not None:
        chain = " → ".join(cycle)
        raise ReorderError(
            CODE_CYCLE,
            f"Обнаружен цикл зависимостей: {chain}. Порядок отклонён.",
        )

    position = {task_id: index for index, task_id in enumerate(order)}
    for index, task in enumerate(tasks):
        for dep in _intra_set_dependencies(task, id_set):
            if position[dep] > index:
                dep_title = (tasks_by_id.get(dep, {}).get("title") or dep)[:40]
                task_title = (task.get("title") or task.get("id") or "")[:40]
                raise ReorderError(
                    CODE_DEPENDENCY_ORDER,
                    f"«{task_title}» нельзя поставить выше зависимости "
                    f"«{dep_title}»: сначала должна идти зависимость.",
                )
    return list(order)


def reorder(
    order: list[str], task_id: str, target_index: int, tasks_by_id: dict[str, dict]
) -> list[str]:
    """Move ``task_id`` to ``target_index`` and return the validated new order.

    ``target_index`` is clamped into range, so a control that asks to move the
    top task "up" is a no-op rather than an error. The resulting order is run
    through :func:`validated_order`; an illegal move raises and the caller keeps
    the old order intact.
    """
    if task_id not in order:
        raise ReorderError(
            CODE_UNKNOWN_TASK, f"Задача {task_id} не входит в переставляемый набор."
        )
    remaining = [t for t in order if t != task_id]
    target_index = max(0, min(target_index, len(remaining)))
    remaining.insert(target_index, task_id)
    return validated_order(remaining, tasks_by_id)


def move(
    order: list[str], task_id: str, delta: int, tasks_by_id: dict[str, dict]
) -> list[str]:
    """Nudge ``task_id`` by ``delta`` positions (``-1`` up, ``+1`` down).

    The robust up/down-control primitive the Streamlit surface uses instead of
    HTML5 drag-drop (which Streamlit cannot deliver natively). Rejects the move
    with a clear :class:`ReorderError` when it would out-rank a dependency.
    """
    current = order.index(task_id)
    return reorder(order, task_id, current + delta, tasks_by_id)


def default_order(tasks: list[dict]) -> list[str]:
    """The persisted priority order for ``tasks``, healed to a legal sequence.

    Tasks are sorted by their stored ``priority_rank`` (unranked rows sink to the
    end in stable id order). If the persisted order happens to violate the
    dependency rule — e.g. a dependency was added *after* the ranks were set — a
    dependency-respecting order is derived instead (stable topological sort keyed
    on the stored rank), so a graph that has drifted still opens in a legal state
    the operator can then adjust.
    """
    ids = [t["id"] for t in tasks if t.get("id")]
    ranked = sorted(
        ids,
        key=lambda tid: (
            _rank_key(tasks_by_id_lookup(tasks, tid)),
            ids.index(tid),
        ),
    )
    tasks_by_id = {t["id"]: t for t in tasks if t.get("id")}
    try:
        return validated_order(ranked, tasks_by_id)
    except ReorderError:
        return _topological_order(ranked, tasks_by_id)


def _rank_key(task: dict | None) -> float:
    rank = (task or {}).get(PRIORITY_RANK_FIELD)
    return float(rank) if isinstance(rank, (int, float)) else float("inf")


def tasks_by_id_lookup(tasks: list[dict], task_id: str) -> dict | None:
    for task in tasks:
        if task.get("id") == task_id:
            return task
    return None


def _topological_order(
    preferred: list[str], tasks_by_id: dict[str, dict]
) -> list[str]:
    """Dependency-respecting order that follows ``preferred`` as a tie-break.

    Kahn's algorithm over the in-set ``depends_on`` edges, always emitting the
    earliest ``preferred`` task among those whose dependencies are all placed. A
    cyclic set has no topological order, so the cycle is surfaced rather than
    silently linearised.
    """
    id_set = set(preferred)
    tasks = [tasks_by_id[t] for t in preferred]
    cycle = find_cycle(tasks)
    if cycle is not None:
        raise ReorderError(
            CODE_CYCLE, "Набор содержит цикл зависимостей — порядок не определён."
        )
    placed: list[str] = []
    remaining = list(preferred)
    while remaining:
        for candidate in remaining:
            deps = _intra_set_dependencies(tasks_by_id[candidate], id_set)
            if all(dep in placed for dep in deps):
                placed.append(candidate)
                remaining.remove(candidate)
                break
    return placed


def blocked_by(task: dict, tasks_by_id: dict[str, dict]) -> list[str]:
    """The task's unmet dependencies — the namespace-aware blocked reason.

    Thin re-export of ``models.unmet_dependencies`` so ordering callers do not
    reach across into ``models`` directly and the "blocked" definition stays
    single-sourced (a dependency is met only when its task is ``Done``)."""
    return models.unmet_dependencies(task, tasks_by_id)
