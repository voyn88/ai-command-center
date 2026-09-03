"""Render the canonical PostgreSQL backlog as the master-file projection.

VOYN-W0-AICC-BACKLOG-EXPORT-PROJECTION. The machine invariants already say
it plainly: "Markdown and dashboards are projections; the structured
transactional backlog store is canonical." The import direction exists
(``backlog-import`` feeds the store from the authored file), but nothing
ever wrote the projection BACK — so every markdown reader, including the
console's Master Backlog panel (``ui/master_backlog_panel.py`` via
``backlog_client.load_projection``), was frozen at whatever snapshot last
predated the store (live: the console booted 2026-09-03 rendered a file
from 2026-08-20 — two weeks of a working fleet invisible to its owner).

This module is the missing half, and it is a PROJECTION in the strict
sense: read-only over ``backlog_task``, deterministic, regenerated whole on
every run, never merged with the previous file, and carrying a header that
says so — editing the output is editing a rendering, not the backlog.

The format is not ours to choose: ``backlog_client.parse_recommendations``
is the one consumer contract (exactly ``RECOMMENDATION_FIELDS`` in exactly
that order, ``" | "``-separated ``key=value`` tokens on ``- ``-prefixed
lines). Rendering through the parser's own constants — and round-tripping
in the test through the parser itself — keeps the two sides from drifting:
a field added to the parser breaks the exporter's test, not the console.

Field mapping is honest about what the store holds: ``issue_id``/``task``/
waves/priority/status/``ts`` come from columns; ``owner`` carries the repo
route (the store's writer identity); the remaining narrative fields
(``effect``/``effort``/``acceptance``/``evidence``/``file_scope``/
``parallel_domain``) live inside free-text bodies, and inventing summaries
here would put a second author's words into a record that claims to be a
projection — they render as ``-`` until the store grows those columns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from command_center import backlog_client

_HEADER = (
    "# VOYN master backlog — generated projection\n"
    "\n"
    "This file is RENDERED from the canonical PostgreSQL backlog store\n"
    "(`backlog_task`); it is regenerated whole and never read back. Do not\n"
    "edit: changes here change a rendering, not the backlog.\n"
    "\n"
    "## 0B. Machine records\n"
    "\n"
)

#: The one character sequence a value may never contain is the field
#: separator; newlines would end the record early. Both are flattened
#: rather than escaped — the projection is for reading, and a value that
#: needed escaping would round-trip as a different string anyway.
_FLATTEN = (("\r", " "), ("\n", " "), (backlog_client.FIELD_SEP, " / "))


def _clean(value: object) -> str:
    text = "-" if value is None else str(value).strip()
    for needle, replacement in _FLATTEN:
        text = text.replace(needle, replacement)
    return text or "-"


def render_record(row: dict[str, Any]) -> str:
    """One ``- VOYN_RECOMMENDATION | ...`` line from one ``backlog_task`` row."""
    updated = row.get("updated_at")
    if isinstance(updated, datetime):
        ts = updated.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts = _clean(updated)
    values = {
        "ts": ts,
        "status": _clean(row.get("status")),
        "issue_id": _clean(row.get("task_id")),
        "current_wave": _clean(row.get("wave")),
        "proposed_wave": _clean(row.get("wave")),
        "priority": _clean(row.get("priority")),
        "owner": _clean(row.get("repo")),
        "effect": "-",
        "effort": "-",
        "acceptance": "-",
        "task": _clean(row.get("title")),
        "evidence": "-",
        "file_scope": "-",
        "parallel_domain": "-",
    }
    tokens = [backlog_client.RECOMMENDATION_MARKER] + [
        f"{key}={values[key]}" for key in backlog_client.RECOMMENDATION_FIELDS
    ]
    return "- " + backlog_client.FIELD_SEP.join(tokens)


def render_projection(rows: list[dict[str, Any]]) -> str:
    return _HEADER + "\n".join(render_record(row) for row in rows) + "\n"


def fetch_rows(conn: Any) -> list[dict[str, Any]]:
    """Every task, terminal ones included: the projection is the store's
    whole truth, and the panel filters by status itself."""
    with conn.cursor() as cur:
        cur.execute(
            "select task_id, wave, priority, status, title, repo, updated_at "
            "from backlog_task order by wave, priority nulls last, task_id"
        )
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
