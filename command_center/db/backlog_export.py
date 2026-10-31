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
sense: read-only over ``backlog_task``, deterministic (the render clock is
an argument, not a ``now()`` read inside), regenerated whole on every run,
never merged with the previous file, and carrying a header that says so —
editing the output is editing a rendering, not the backlog. That header
also stamps when the file was rendered and from how many rows, so a reader
holding only the text can tell a live projection from one whose tick died —
machine-readably, not just to a human: the stamp's format belongs to
``backlog_client`` (``render_generated_stamp``/``parse_generated_stamp``),
which renders it here and reads it back into ``Projection.stamp``, so the
console's freshness metric shows the file's own age instead of an ``mtime``
that any copy of the file resets to now. The header also carries
``GENERATED_MARKER``, the line ``backlog-import`` uses to refuse importing a
rendering back into the store (``is_generated_projection``) instead of
accepting it as an authored file and reporting a successful zero-task
import.

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

Execution-status granularity is not lost, though: it is carried on the
master file's *other* record surface, the one
``backlog_client.parse_rich_records``/``load_rich_records`` reads (consumed
by ``native_gateway/projection_producer.py`` for its Kanban lanes and
wave-goal card). Section 0C below renders it, one
``- VOYN_TASK_STATUS | id=... | wave=... | status=... | ...`` line per row,
through ``backlog_client.render_task_status`` — same discipline as the 0B
records and the header stamp: the reader owns the format, the exporter
renders through it, and the two cannot drift into a line nobody parses.

That marker shape is not cosmetic, it is the safety property. The surface's
hand-authored form — a bold ``- **VOYN-<id>** | <wave> | <status> |
<priority> |`` task line — cannot be machine-written while the import
direction is alive: ``backlog_client._RICH_LINE`` is a strict SUBSET of
``backlog_parser``'s ``_TASK_LINE``/``_RECORD_SHAPED`` match (bold
``**VOYN-...**`` id followed by a ``| ``), so every line the rich reader
accepts the importer would accept too, and parse fully as a real authored
task — not even landing in ``unparsed``. No variant of the bold-id/pipe
shape satisfies one parser and not the other; the two were built to share
that convention on purpose. So this section takes the move
``VOYN_RECOMMENDATION`` already made for 0B records instead: a distinct
leading marker on a plain, *unbolded* list item, which matches neither
importer pattern — invisible to ``backlog-import``, not merely rejected by
it (proved end to end through the real importer in
``tests/db/test_backlog_export.py``). ADR-0011's central argument, that the
two directions share no line shape, holds unchanged.

Two fields are translated rather than copied, for the same reason
``status`` is on the 0B record — the reader's vocabulary is the contract,
not the column's:

* ``wave`` renders as the authored *text* (``"0"`` -> ``"Wave 0"``; a named
  lane like ``COM`` stands alone), because that is what reads this field:
  ``projection_producer._wave_goal`` matches ``Wave <n>`` exactly, and a
  bare ``0`` would silently empty the goal card. The inverse mapping is
  ``backlog_parser.normalize_wave``, shared so the round trip is pinned
  against the real normalizer rather than a copy of its regex;
* ``slug`` carries ``backlog_task.title``, which IS the authored backtick
  slug (``backlog_parser`` fills ``title`` from it), so
  ``RichRecord.title`` humanizes an exported record exactly as it does an
  authored one.

Rendering exists here but a tick has to actually call it: production runs
this through ``backlog-export`` on ``deploy/systemd/aicc-backlog-export.timer``
(five-minute cadence, matching the import side's own publisher). Both
directions running together is a deliberately temporary bridge — see
``docs/adr/0011-backlog-projection-bidirectional-bridge.md`` for the
condition and the 2026-11-01 target date under which the import side
(``backlog-import`` / ``ops/aicc_backlog_publish.py``) retires, leaving this
module as the only crossing. Until then both record surfaces above are
written for readers, never read back: section 0C's precedence rule (a
machine record outranks a hand-authored line for the same id) is what
governs a file where the two ever meet.
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

#: The one line by which a file can be recognised as this module's own
#: output. ``backlog-import`` keys off it to refuse importing a rendering
#: back into the store (``is_generated_projection``), so it is defined here —
#: beside the code that emits it — and interpolated into the header rather
#: than written twice, where the two copies could drift apart and quietly
#: disarm that refusal.
GENERATED_MARKER = "This file is RENDERED from the canonical PostgreSQL backlog store"


def _utc_stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header(generated_at: datetime, row_count: int) -> str:
    """The prose preamble: what this file is, and how old it is.

    The age line is not decoration. The failure that created this exporter
    was a *silently* stale projection — a console rendering a two-week-old
    file with nothing in the content to say so. The console itself now reads
    freshness from the file's mtime (``master_backlog_panel`` shows it), but
    mtime is a property of the filesystem, not of the text: it does not
    survive a copy, an scp or a checkout, and it is invisible to the owner
    reading the rendered markdown in an editor, which is exactly the audience
    BO-S4 renders this file for. A stamp inside the file travels with it.

    The stamp line is rendered by ``backlog_client.render_generated_stamp``
    rather than formatted here, because it is not prose: it is the one line of
    this header a machine reads back (``backlog_client.parse_generated_stamp``,
    into ``Projection.stamp``, which is what the console's freshness metric now
    shows). Writing it on both sides would let a reworded header quietly stop
    parsing while still looking right to a human.
    """
    return (
        "# VOYN master backlog — generated projection\n"
        "\n"
        f"{GENERATED_MARKER}\n"
        "(`backlog_task`); it is regenerated whole and never read back. Do not\n"
        "edit: changes here change a rendering, not the backlog.\n"
        "\n"
        f"{backlog_client.render_generated_stamp(generated_at, row_count)}\n"
        "\n"
        "Written by `backlog-export` (aicc-backlog-export.timer, every 5\n"
        "minutes). If the stamp above is far behind the current time, the export\n"
        "tick has stopped and every record below is stale — check the timer on\n"
        "the control host rather than trusting what follows.\n"
        "\n"
        "## 0B. Machine records\n"
        "\n"
    )


def is_generated_projection(text: str) -> bool:
    """True when ``text`` is this module's own output rather than an authored
    backlog.

    Exists for the import direction: ``backlog-import`` refuses a file this
    returns True for. Matches the marker as a whole line (never a substring)
    and only within the header — ``backlog_client.HEADER_SCAN_LINES``, the same
    bound the read side applies to the stamp line, so the two readers of this
    header agree on where it ends; the constant carries the reasoning.
    """
    head = text.splitlines()[: backlog_client.HEADER_SCAN_LINES]
    return any(line.strip() == GENERATED_MARKER for line in head)


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


#: A wave whose store value is a number ("0", "0.5") rather than a named lane
#: ("COM", "W1"). Only these take the "Wave " prefix on the authored surface —
#: the exact split ``backlog_parser._WAVE`` makes in the other direction, and
#: pinned against ``normalize_wave`` itself in the tests rather than trusted.
_NUMERIC_WAVE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _wave_text(wave: object) -> str:
    """Render ``backlog_task.wave`` as the wave TEXT readers of the rich
    surface expect — see the module docstring's field-translation note."""
    value = _clean(wave)
    return f"Wave {value}" if _NUMERIC_WAVE.match(value) else value


def render_record(row: dict[str, Any]) -> str:
    """One ``- VOYN_RECOMMENDATION | ...`` line from one ``backlog_task`` row."""
    updated = row.get("updated_at")
    ts = _utc_stamp(updated) if isinstance(updated, datetime) else _clean(updated)
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


def render_status_record(row: dict[str, Any]) -> str:
    """One ``- VOYN_TASK_STATUS | ...`` line from one ``backlog_task`` row.

    Renders through ``backlog_client.render_task_status`` rather than
    formatting the line here, so the surface's one reader also owns its
    shape — the same arrangement as ``render_record`` and the header stamp.
    Every value goes through ``_clean`` first: the store's ``title`` is free
    text and a raw ``|`` or newline in it would shift or break the record,
    exactly as on the 0B line.
    """
    return backlog_client.render_task_status(
        backlog_client.RichRecord(
            record_id=_clean(row.get("task_id")),
            wave=_wave_text(row.get("wave")),
            status=_clean(row.get("status")),
            priority=_clean(row.get("priority")),
            slug=_clean(row.get("title")),
        )
    )


#: Preamble of the execution-status section. Prose, so it must not parse as a
#: record on either surface — it carries no leading ``- `` list marker.
_STATUS_SECTION = (
    "\n"
    "## 0C. Execution status\n"
    "\n"
    "One record per task carrying the store's execution lifecycle exactly\n"
    "(`OPEN`/`IN_PROGRESS`/`READY_TO_REVIEW`/`DONE`/...), which section 0B\n"
    "above necessarily coarsens to approved/not-approved. Read by\n"
    "`backlog_client.parse_rich_records`; invisible to `backlog-import`.\n"
    "\n"
)


def render_projection(rows: list[dict[str, Any]], *, generated_at: datetime) -> str:
    """The whole file: header (stamped ``generated_at``), one 0B record line
    per row, then one 0C execution-status line per row.

    ``generated_at`` is a required argument rather than a ``datetime.now()``
    read inside this function, so the renderer stays a pure function of its
    inputs — the tick supplies the clock, and a test can render a byte-exact
    expected file. Required rather than defaulted, because a stamp silently
    omitted would leave the projection claiming nothing about its own age,
    which is the state this header exists to end.

    Both sections cover the same ``rows`` in the same order: the two record
    surfaces describe one store, and a task present in one but not the other
    would be a task whose lane and whose approval state came from different
    reads. The header's row count therefore still describes each section,
    and ``backlog_client.stamp_matches_content`` — which counts 0B records
    only — keeps agreeing with it.
    """
    return (
        _header(generated_at, len(rows))
        + "\n".join(render_record(row) for row in rows)
        + "\n"
        + _STATUS_SECTION
        + "\n".join(render_status_record(row) for row in rows)
        + "\n"
    )


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
