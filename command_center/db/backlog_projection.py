"""Renders the structured backlog store back to Markdown (BO-S4).

`backlog_parser.py` reads the Markdown record shape into the store;
this is the other direction — the store is authority, Markdown is a
generated read projection for the file's owner. Round trip is the whole
point: `parse_backlog(render_backlog(export_tasks()))` must reproduce every
task's `wave`/`priority`/`status`/`title`/`repo` exactly, because a
projection that drifts from what it was rendered from is worse than no
projection — it looks authoritative and lies.

Pure module, the parser's own rule: no database, no I/O beyond the data it is
given, so its tests are hermetic and the store's tests need only prove the
seam (`BacklogStore.export_tasks`).

Bidirectional period, signed: `backlog-import` (Markdown -> store) stays live
only while the store is still absorbing tasks the Markdown file authored by
hand. After IMPORT_SUNSET_DATE the store is sole write authority and the
importer is removed — a dual-write path with no removal date is how a
migration becomes permanent. Tracked under the owning task, not a bare date
nobody remembers the reason for.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["IMPORT_SUNSET_DATE", "IMPORT_SUNSET_TASK", "render_backlog"]

#: The bidirectional (import + export) period ends here. Remove
#: `backlog-import` / `BacklogStore.import_markdown` and this note once the
#: store has been the sole writer for the whole window with no regressions.
IMPORT_SUNSET_DATE = "2026-10-31"
IMPORT_SUNSET_TASK = "VOYN-W0-BACKLOG-ORCHESTRATOR"

_NUMERIC_WAVE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _wave_field(wave: str) -> str:
    """`Wave 0` for a numeric wave, the bare token for a named lane — the
    exact inverse of `backlog_parser._WAVE`, which accepts only these two
    shapes."""
    return f"Wave {wave}" if _NUMERIC_WAVE.match(wave) else wave


def _wave_sort_key(wave: str) -> tuple[int, float, str]:
    """Numeric waves execute in order, so they sort first and numerically;
    named lanes (`COM`, `W7`, ...) have no execution order, so they sort
    after, alphabetically, rather than colliding at 0."""
    if _NUMERIC_WAVE.match(wave):
        return (0, float(wave), "")
    return (1, 0.0, wave)


def _priority_sort_key(priority: str | None) -> int:
    if priority is None:
        return 99
    return int(priority[1:])


def _sort_key(task: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _wave_sort_key(task["wave"]),
        _priority_sort_key(task["priority"]),
        task["task_id"],
    )


def _render_task_line(task: dict[str, Any]) -> str:
    fields = [_wave_field(task["wave"]), task["status"]]
    if task["priority"]:
        fields.append(task["priority"])
    fields.append(f"`{task['title']}`")
    # One logical line per record, the parser's dominant shape: a body that
    # carried continuation bullets (newlines) collapses to single-spaced
    # prose rather than emitting unindented lines a re-parse could misread
    # as a new top-level record.
    body = " ".join(task["body"].split()) if task.get("body") else ""
    if body:
        fields.append(body)
    return f"- **{task['task_id']}** | " + " | ".join(fields)


def render_backlog(tasks: list[dict[str, Any]], *, generated_at: str) -> str:
    """The store, rendered as the Markdown record shape `backlog_parser`
    reads — grouped by wave, sorted deterministically so two renders of the
    same store state are byte-identical (a diff then shows real change, not
    reshuffled output)."""
    lines = [
        "# VOYN_TASKS_BACKLOG.md — generated projection",
        "",
        f"Generated {generated_at} from the structured backlog store "
        "(VOYN-W0-BACKLOG-ORCHESTRATOR BO-S1..S4). This file is a READ "
        "projection: status transitions, dependencies, leases and dispatch "
        "live in PostgreSQL, not here.",
        "",
        f"Bidirectional period (import and export both live) ends "
        f"{IMPORT_SUNSET_DATE}, tracked under {IMPORT_SUNSET_TASK}. After "
        "that date hand edits to this file are no longer reconciled into "
        "the store.",
        "",
    ]

    ordered = sorted(tasks, key=_sort_key)
    current_wave: str | None = None
    for task in ordered:
        if task["wave"] != current_wave:
            current_wave = task["wave"]
            lines.append(f"## {_wave_field(current_wave)}")
        lines.append(_render_task_line(task))

    lines.append("")
    return "\n".join(lines)
