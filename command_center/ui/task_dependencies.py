"""Task dependencies + priority-order surface (VOYN-W2-TASKS).

A read-through-the-API surface: it shows every task in a project as a dependency
graph — each task's ``depends_on`` edges, its dependency *level*, and whether it
is *blocked* (an unmet dependency) — and lets the operator set the explicit
**priority order** with up/down controls.

Why up/down and not HTML5 drag-drop: Streamlit renders server-side and has no
native drag-drop primitive, so a drag surface would mean shipping a bespoke
component (opaque, untestable from ``pytest``). Up/down buttons are the robust,
accessible control that survives a rerun and is trivially exercised by a test —
each press asks the API to move one task one slot and repaints from whatever the
server committed.

The invariant is not enforced here. Every reorder is a call to
``POST /api/v1/tasks/reorder``; the server validates it against the dependency
rule (``command_center.task_ordering``) under the tasks lock and either persists
it or returns ``409``. This module only *renders* the outcome — the ✅ toast on
success, the error banner naming the offending dependency on rejection — so the
UI can never persist an order the domain would refuse.

Styled only through the canonical design-token package
(``command_center/design``): no raw hex, every colour a ``var(--token)`` so both
themes stay in step. Every async region carries the full idle / loading / error
/ empty / success contract.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol

import streamlit as st

from command_center.ui import operator_dashboard as _od

if TYPE_CHECKING:
    from command_center.api import schemas


# --------------------------------------------------------------------------
# API client seam — the same "consume the API, never the store" boundary the
# operator dashboard uses (see ``dashboard_client``).
# --------------------------------------------------------------------------


class ReorderRejected(Exception):
    """A reorder the server refused. ``message`` is operator-facing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TaskGraphClient(Protocol):
    """The narrow contract this surface renders against — a :class:`Protocol` so
    a UI test injects a fake (or a raising stub) without a live backend."""

    def task_graph(self, project: str | None) -> schemas.TaskGraph: ...

    def reorder(self, project: str, order: list[str]) -> schemas.TaskGraph: ...


class InProcessTaskGraphClient:
    """Default client: the API service layer, in the desktop shell's process.

    The seam is the API's own aggregation/redaction/validation layer, never the
    task store underneath it — so redaction (BANK/LEGAL dropped) and the reorder
    invariant both stay in force exactly as an over-the-wire client would see."""

    def task_graph(self, project: str | None) -> schemas.TaskGraph:
        from command_center.api import service as read_service

        return read_service.task_graph(project=project)

    def reorder(self, project: str, order: list[str]) -> schemas.TaskGraph:
        from command_center.api import wave1_service as write_service

        try:
            return write_service.reorder_tasks(project, order)
        except write_service.TaskReorderRejected as exc:
            raise ReorderRejected(exc.code, exc.message) from exc


# --------------------------------------------------------------------------
# State contract + small helpers (reused from the operator dashboard so colour,
# spacing and the five-state vocabulary stay single-sourced).
# --------------------------------------------------------------------------

IDLE, LOADING, ERROR, EMPTY, SUCCESS = (
    _od.IDLE,
    _od.LOADING,
    _od.ERROR,
    _od.EMPTY,
    _od.SUCCESS,
)

# Board state -> chip variant (same green/orange/red/blue/gray tokens the board
# uses; one colour means one thing across the whole app).
_STATE_CHIP: dict[str, str] = {
    "done": "ok",
    "running": "warn",
    "blocked": "crit",
    "ready": "info",
    "waiting": "",
}
_STATE_LABEL: dict[str, str] = {
    "done": "смёржено",
    "running": "выполняется",
    "blocked": "отказ",
    "ready": "готова",
    "waiting": "ждёт зависимости",
}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


@dataclass(frozen=True)
class SectionState:
    status: str
    data: Any = None
    error: str | None = None


def load_graph(client: TaskGraphClient, project: str | None) -> SectionState:
    """Fetch the graph once and classify it into the five-state contract.

    Any client exception becomes the ``error`` state rather than propagating, so
    a backend hiccup shows a retry affordance instead of a stack trace."""
    try:
        graph = client.task_graph(project)
    except Exception as exc:  # noqa: BLE001 - surfaced as the error state
        return SectionState(status=ERROR, error=str(exc) or exc.__class__.__name__)
    if not graph.nodes:
        return SectionState(status=EMPTY, data=graph)
    return SectionState(status=SUCCESS, data=graph)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_ERROR_KEY = "task_deps_reorder_error"


def _titles(nodes: list[Any]) -> dict[str, str]:
    return {n.id: n.title for n in nodes}


def _render_node_row(
    node: Any,
    titles: dict[str, str],
    *,
    is_first: bool,
    is_last: bool,
    on_move: Callable[[str, int], None],
) -> None:
    """One task line: priority rank, title, state chip, dependency edges, and
    the up/down controls. Controls that would run off the end are disabled."""
    chip_variant = _STATE_CHIP.get(node.state, "")
    state_chip = (
        f"<span class='chip chip--{chip_variant}'>{_esc(_STATE_LABEL.get(node.state, node.state))}</span>"
        if chip_variant
        else f"<span class='chip'>{_esc(_STATE_LABEL.get(node.state, node.state))}</span>"
    )
    dep_names = [titles.get(d, d) for d in node.depends_on]
    deps_html = ""
    if dep_names:
        chips = "".join(f"<span class='chip'>⬅ {_esc(name)}</span>" for name in dep_names[:4])
        more = f"<span class='chip'>+{len(dep_names) - 4}</span>" if len(dep_names) > 4 else ""
        deps_html = f"<div class='tdp-edges'>{chips}{more}</div>"
    blocked_html = ""
    if node.blocked:
        blk = ", ".join(_esc(titles.get(b, b)) for b in node.blocked_by[:3])
        blocked_html = f"<div class='tdp-blocked'>🔴 заблокирована: {blk}</div>"

    controls, body = st.columns([1, 11], gap="small")
    with body:
        st.markdown(
            f"<div class='card tdp-row'>"
            f"<div class='tdp-row__head'>"
            f"<span class='tdp-rank'>{node.rank + 1}</span>"
            f"<span class='tdp-title'>{_esc(node.title)}</span>"
            f"<span class='tdp-lvl'>уровень {node.level}</span>"
            f"{state_chip}</div>"
            f"{deps_html}{blocked_html}</div>",
            unsafe_allow_html=True,
        )
    with controls:
        if st.button("▲", key=f"tdp_up_{node.id}", disabled=is_first,
                     help="Поднять приоритет"):
            on_move(node.id, -1)
        if st.button("▼", key=f"tdp_down_{node.id}", disabled=is_last,
                     help="Понизить приоритет"):
            on_move(node.id, +1)


def _move_handler(client: TaskGraphClient, project: str, order: list[str]) -> Callable[[str, int], None]:
    def _move(task_id: str, delta: int) -> None:
        idx = order.index(task_id)
        target = idx + delta
        if target < 0 or target >= len(order):
            return
        new_order = list(order)
        new_order.insert(target, new_order.pop(idx))
        try:
            client.reorder(project, new_order)
        except ReorderRejected as exc:
            # Keep the old order; surface exactly why the move was refused.
            st.session_state[_ERROR_KEY] = exc.message
            st.rerun()
            return
        st.session_state.pop(_ERROR_KEY, None)
        st.toast("Порядок обновлён", icon="✅")
        st.rerun()

    return _move


def render(
    project: str | None,
    *,
    client: TaskGraphClient | None = None,
) -> None:
    """Render the dependency graph + priority-order surface for ``project``.

    ``client`` defaults to the in-process API client; UI tests inject a fake (or
    a raising stub) to drive the empty/error/rejected states from mocked data.
    """
    if client is None:
        client = InProcessTaskGraphClient()

    _od.inject_css()
    st.markdown(_EXTRA_CSS, unsafe_allow_html=True)

    st.markdown(
        "<div class='ocd-head'><div class='ocd-title'>Зависимости и приоритет</div>"
        "<div class='ocd-sub'>Все данные — из /api/v1.</div></div>",
        unsafe_allow_html=True,
    )

    state = load_graph(client, project)

    if state.status == LOADING:
        _od._skeleton()
        return
    if state.status == IDLE:
        _od._idle_note()
        return
    if state.status == ERROR:
        _od._note(f"Не удалось загрузить граф задач: {state.error}", error=True)
        _od._retry("task_deps")
        return
    if state.status == EMPTY:
        _od._note("В этом проекте пока нет задач для упорядочивания.")
        return

    graph = state.data
    if graph.has_cycle:
        _od._note(
            "В зависимостях обнаружен цикл — приоритет заблокирован, пока цикл не разорван.",
            error=True,
        )

    err = st.session_state.get(_ERROR_KEY)
    if err:
        _od._note(err, error=True)

    titles = _titles(graph.nodes)
    on_move = _move_handler(client, project or graph.project or "", graph.order)
    last = len(graph.nodes) - 1
    for index, node in enumerate(graph.nodes):
        _render_node_row(
            node,
            titles,
            is_first=index == 0 or graph.has_cycle,
            is_last=index == last or graph.has_cycle,
            on_move=on_move,
        )


# Layout-only scaffolding for this surface. Colour, spacing, radius, type — all
# via var(--token); no literal below is a colour.
_EXTRA_CSS = """<style>
.tdp-row { margin-bottom:var(--space-2); }
.tdp-row__head { display:flex; align-items:center; gap:var(--space-3); flex-wrap:wrap; }
.tdp-rank { font-family:var(--font-mono); font-size:var(--fs-sm); color:var(--text-3);
  min-width:22px; text-align:right; }
.tdp-title { font-family:var(--font-sans); font-size:var(--fs-md);
  font-weight:var(--fw-semibold); color:var(--text); }
.tdp-lvl { font-family:var(--font-mono); font-size:var(--fs-xs); color:var(--text-3); }
.tdp-edges { display:flex; flex-wrap:wrap; gap:var(--space-2); margin-top:var(--space-2); }
.tdp-blocked { font-family:var(--font-sans); font-size:var(--fs-sm); color:var(--crit);
  margin-top:var(--space-2); }
</style>"""
