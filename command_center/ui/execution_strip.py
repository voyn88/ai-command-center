"""Execution Strip (UX-2a): a slim, always-visible status bar that surfaces
what is running right now regardless of which page is open.

It is a small, isolated polling fragment, so it polls the runtime database on its
own cadence and repaints *only itself* — the page behind it never blanks just
because the strip ticked. This is the direct answer to the operator's reported
"every few seconds the whole page visibly refreshes" pain: execution state
becomes one glance away on every page without a per-page auto-refresh.

The strip deliberately mirrors the Live Execution Center's bucket vocabulary
(``live_board``: live / waiting / attention) so an operator does not learn a
second word for the same thing here. Counts come from cheap raw-state filtering
of ``api.list_runs`` — the strip is a status glyph, not a second board, so it
does not build the full session views the Execution Center page does.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import execution_queue, read_model, tasks_repository
from command_center.runtime.api import ExecutionCenterAPI
from command_center.runtime.db import RUN_STATES
from command_center.ui import execution_metrics, tokens

# The strip mirrors the Live Execution Center's three buckets (live / waiting /
# attention) but counts them from the **persisted** `run.state` vocabulary
# (`runtime/db.RUN_STATES`: PREPARED, QUEUED, RUNNING, COMPLETED, FAILED,
# CANCELLED, INTERRUPTED, UNKNOWN) — *not* from the *display* statuses that
# `session_view.derive_status` produces (Launching / Starting / Running / Stale
# / Waiting / Blocked / Incomplete / …). Those display strings are derived at
# render time and are never written to `run.state`, so filtering raw runs by
# them (the original code) matched nothing: "в очереди" was always 0, "требуют
# внимания" only ever counted FAILED.
#
# Mapping (aligned with `live_board._BUCKET_BY_STATUS` via
# `session_view.derive_status`):
#   PREPARED, QUEUED  → display Launching → board "Ожидают запуска" (waiting)
#   RUNNING           → Starting/Running  → board "Выполняется"      (live)
#   FAILED            → Failed/Blocked    → board "Требуют внимания" (attention)
#   INTERRUPTED, UNKNOWN → Requires Attention                          (attention)
#   COMPLETED, CANCELLED → done / operator-stopped — not surfaced in the strip.
# Single source of truth for the run buckets: shared with the AI-Supervisor
# caption and the top-bar glyph via `read_model` (audit D5) so the three surfaces
# can never again show three different "attention" numbers on one screen.
_LIVE_STATES = read_model.RUN_LIVE_STATES
_WAITING_STATES = read_model.RUN_WAITING_STATES
_ATTENTION_STATES = read_model.RUN_ATTENTION_STATES


def strip_counts(runs: list[dict]) -> tuple[int, int, int]:
    """Pure bucketing of raw run rows into ``(live, waiting, attention)``.

    Kept free of Streamlit so it can be unit-tested directly. Any state outside
    the three buckets (e.g. COMPLETED/CANCELLED, or a future state added to
    ``RUN_STATES``) is intentionally ignored by the strip — the strip is a
    "what is happening *now*" glyph, not a full board. A guard below asserts the
    buckets stay consistent with ``RUN_STATES`` so a new persisted state can't
    silently drop off the strip.

    Superseded earlier attempts (a task with a newer run) are dropped by the
    caller (see ``_nonsuperseded_runs``) so the strip's attention count agrees
    with the Live Board, which filters ``live_board.superseded_run_ids`` out of
    its attention bucket — without that, a task that failed once and was retried
    would keep its old FAILED run pinned in "требуют внимания" forever even
    though a newer attempt has moved past it.
    """
    live = sum(1 for r in runs if r.get("state") in _LIVE_STATES)
    waiting = sum(1 for r in runs if r.get("state") in _WAITING_STATES)
    attention = sum(1 for r in runs if r.get("state") in _ATTENTION_STATES)
    return live, waiting, attention


def _superseded_run_ids(runs: list[dict]) -> frozenset[str]:
    """Run ids that are *not* the latest run of their task. Delegates to
    `read_model.superseded_run_ids` so the strip, the AI-Supervisor caption and
    the top-bar glyph share one definition (audit D5)."""
    return read_model.superseded_run_ids(runs)


def _nonsuperseded_runs(runs: list[dict]) -> list[dict]:
    """Drop earlier attempts a task has moved past, so the strip reports what
    is happening *now* — consistent with the Live Board's attention filter."""
    superseded = _superseded_run_ids(runs)
    if not superseded:
        return runs
    return [r for r in runs if r.get("id") not in superseded]


# Safety net: if a state is added to RUN_STATES but not to any strip bucket, we
# want a loud failure at import rather than a silently-missing count.
_unbucketed = set(RUN_STATES) - _LIVE_STATES - _WAITING_STATES - _ATTENTION_STATES
assert _unbucketed <= {"COMPLETED", "CANCELLED"}, (
    f"runtime/db.RUN_STATES gained states with no Execution Strip bucket: {_unbucketed}"
)


def current_counts(
    runs: list[dict],
    tasks: list[dict],
    queue_entries: list[dict],
    *,
    dismissed_attention_run_ids: set[str] | frozenset[str] = frozenset(),
) -> execution_metrics.ExecutionCounts:
    """Build the same four counts the Live Center headline uses."""
    tasks_by_id = {task["id"]: task for task in tasks if task.get("id")}
    sessions = execution_metrics.lightweight_sessions_from_runs(runs, tasks_by_id)
    board = execution_metrics.actionable_board(
        sessions,
        tasks_by_id,
        dismissed_attention_run_ids=dismissed_attention_run_ids,
    )
    return execution_metrics.counts_for_snapshot(board, queue_entries)


@st.fragment(run_every=15.0)
def render_execution_strip(api: ExecutionCenterAPI, root: Path) -> None:
    """Render the cross-page execution status strip.

    Polls a lightweight snapshot every 15 s and shows the exact same live /
    waiting / attention totals as the Live Center headline. The slower cadence
    keeps the status current without repainting the page every five seconds.
    counts with a single button to jump to the Live Execution Center. When
    nothing is running and nothing needs attention the strip renders a quiet
    idle line so the bar never looks broken-empty.
    """
    runs = api.list_runs(limit=200)
    tasks = tasks_repository.load_tasks(root)
    queue_entries = execution_queue.load_queue(root)
    counts = current_counts(
        runs,
        tasks,
        queue_entries,
        dismissed_attention_run_ids=st.session_state.get(
            "exec_attention_dismissed", set()
        ),
    )
    live, waiting, attention = counts.live, counts.waiting, counts.attention

    with st.container(border=True, key="exec_strip"):
        left, right = st.columns([5, 2], vertical_alignment="center")
        with left:
            parts: list[str] = []
            if live:
                parts.append(f"▲ {live} выполняется")
            if waiting:
                parts.append(f"◷ {waiting} в очереди")
            if attention:
                parts.append(f"● {attention} требуют внимания")
            label = "  ·  ".join(parts) if parts else "Система простаивает — активных прогонов нет"
            tone = tokens.TONE_DANGER if attention else tokens.TONE_ACTIVE if live else tokens.TONE_NEUTRAL
            st.markdown(f"**{label}**")
            st.caption("Live Execution Center · актуальный снимок каждые 15 с")
            # The tone is exposed as a colored badge so a glance catches the
            # worst state (attention) without reading the label.
            st.badge(
                "внимание" if attention else "в работе" if live else "ожидание",
                color=tone,
            )
        with right:
            if st.button(
                "Execution Center",
                icon=":material/bolt:",
                key="exec_strip_open",
                width="stretch",
                type="primary" if (live or attention) else "tertiary",
            ):
                st.session_state.pending_nav = "execution_center"
                st.rerun()
