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
waves/priority/``ts`` come from columns; ``owner`` carries the repo route
(the store's writer identity); the remaining narrative fields
(``effect``/``effort``/``acceptance``/``evidence``/``file_scope``/
``parallel_domain``) live inside free-text bodies, and inventing summaries
here would put a second author's words into a record that claims to be a
projection — they render as ``-`` until the store grows those columns.

``status`` is the one field that is NOT a straight column copy, and for a
reason worth spelling out: ``backlog_client``'s own module docstring draws a
hard line between two vocabularies that happen to share a field name — the
0B ``VOYN_RECOMMENDATION`` record's ``status`` is *planning* status
(``AI-Reco``/``PO-Review``/``PO-Approved``; see ``backlog_client.
STATUS_APPROVED``), while ``backlog_task.status`` is the store's *execution*
lifecycle (``backlog_parser.STATUSES``: ``OPEN``/``IN_PROGRESS``/etc). Every
planning-status reader keys off the exact literal ``"PO-Approved"`` —
``BacklogRecommendation.is_approved``, and everything built on it
(``approved_recommendations``, ``execution_queue``, the panel's "Approved"
metric, ``native_gateway``'s Next/Backlog lane) — so writing an execution
value straight into this field would make every one of those readers see a
permanently empty approved set for an export-generated file, without ever
raising an error. ``_planning_status`` translates instead: a row's mere
presence in ``backlog_task`` already means the owner authored it into the
machine-managed pipeline (``backlog-import`` is the only writer for owner
content), so ``backlog_parser.EXECUTABLE_STATUSES`` — the store's own line
between "admitted to the execution machine" and "still needs triage" — is
reused as the approval boundary rather than inventing a second one here.
This necessarily loses the execution vocabulary's granularity in this field
(``OPEN``/``IN_PROGRESS``/``DONE`` all read as ``PO-Approved``); the ``by_status``
breakdown coarsens to two buckets for an export-generated file, the same
kind of accepted, documented lossiness as the narrative fields above rather
than the silent wrongness it replaces.

This does NOT restore execution-status granularity for consumers that read
it from the master file's *other* record surface — the body's bold
``- **VOYN-<ID>** | <wave> | <status> | ...`` task lines
(``backlog_client.parse_rich_records``/``load_rich_records``, consumed by
``native_gateway/projection_producer.py`` for its Kanban lanes and wave-goal
card). This module renders 0B records only; it does not emit rich lines, so
those consumers still see only the coarse approved/not-approved distinction
for an export-generated file, never the finer IN_PROGRESS/READY_TO_REVIEW/
DONE detail. Emitting rich lines is not a safe drop-in fix: that line shape
is exactly what ``backlog_parser``'s importer (`_TASK_LINE`/`_RECORD_SHAPED`)
recognizes, so doing so without a fresh safety analysis would break
ADR-0011's "the two directions never share a line shape" argument. Left as
an open, tracked gap rather than a silent one.

Rendering exists here but a tick has to actually call it: production runs
this through ``backlog-export`` on ``deploy/systemd/aicc-backlog-export.timer``
(five-minute cadence, matching the import side's own publisher). Both
directions running together is a deliberately temporary bridge — see
``docs/adr/0011-backlog-projection-bidirectional-bridge.md`` for the
condition and date the import side (``backlog-import`` /
``ops/aicc_backlog_publish.py``) retires under, leaving this module as the
only crossing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from command_center import backlog_client
from command_center.db.backlog_parser import EXECUTABLE_STATUSES

#: A `backlog_task` row not yet admitted to the execution machine
#: (`backlog_parser.NON_EXECUTABLE_STATUSES`) reads as still under review —
#: "AI-Reco" would claim no one has looked at it yet, which is false for,
#: say, a DEFER_TO_USER task.
_STATUS_NOT_YET_APPROVED = "PO-Review"

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

#: Two character classes can break a record and both are removed outright
#: rather than escaped (the projection is for reading; an escaped value
#: would round-trip as a different string anyway):
#: - every `|` becomes `/`: replacing only the exact `" | "` sequence was
#:   proven insufficient — a value ENDING in " |" met the joining
#:   FIELD_SEP as " | | " and shifted every later field (independent
#:   review of 7bfda54, confirmed by execution);
#: - every boundary `str.splitlines` recognises becomes a space — the
#:   parser splits with splitlines, whose set is far wider than \r\n
#:   (\v, \f, FS/GS/RS, \x85, U+2028/U+2029; same review).
_VERTICAL_WS = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")


def _clean(value: object) -> str:
    text = "-" if value is None else str(value)
    text = _VERTICAL_WS.sub(" ", text).replace("|", "/").strip()
    return text or "-"


def _planning_status(execution_status: object) -> str:
    """Translate `backlog_task.status` (execution) into the 0B record's
    planning vocabulary — see the module docstring for why the two must not
    be confused."""
    return (
        backlog_client.STATUS_APPROVED
        if execution_status in EXECUTABLE_STATUSES
        else _STATUS_NOT_YET_APPROVED
    )


def render_record(row: dict[str, Any]) -> str:
    """One ``- VOYN_RECOMMENDATION | ...`` line from one ``backlog_task`` row."""
    updated = row.get("updated_at")
    if isinstance(updated, datetime):
        ts = updated.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ts = _clean(updated)
    values = {
        "ts": ts,
        "status": _planning_status(row.get("status")),
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
    whole truth, and the panel filters by status itself.

    Orders numeric waves (``'0'``, ``'0.5'``, ``'1'``, ...) by their NUMERIC
    value, not their text value -- ``ORDER BY wave`` alone sorts lexically,
    where ``'10'`` comes before ``'2'``; this matches the numeric cast
    ``backlog_eligible`` (0006_backlog_planner) already applies for the same
    reason, so wave order does not disagree between what the planner
    dispatches and what this projection renders once a wave reaches two
    digits. Named lanes (``COM``/``W1``/...) have no numeric value to sort
    by, so they group after every numeric wave and fall back to their own
    text order.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select task_id, wave, priority, status, title, repo, updated_at "
            "from backlog_task "
            "order by (wave ~ '^[0-9]+(\\.[0-9]+)?$') desc, "
            "case when wave ~ '^[0-9]+(\\.[0-9]+)?$' then wave::numeric end asc, "
            "wave asc, priority nulls last, task_id"
        )
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
