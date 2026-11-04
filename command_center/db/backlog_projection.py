"""Render the canonical store as the master Markdown backlog (BO-S4).

The machine invariant is old: "Markdown is a projection; the structured
transactional store is canonical." Until BO-S4 only the import direction
existed — the authored ``VOYN_TASKS_BACKLOG.md`` fed ``backlog_task`` and
nothing ever wrote back — so every status the machine moved (planner
dispatch, review verdicts, merges) was invisible in the file its owner
reads. This module is the return path.

It is distinct from ``backlog_export``, deliberately: that module renders
the console's RECOMMENDATION format, a lossy read-only view that scrubs
separators out of values because nothing ever reads it back. This one
renders the MASTER format, and its contract is stronger by exactly one
property:

    ``parse_backlog(render_backlog(tasks)).tasks`` reproduces every task in
    order and reports nothing unparsed, with ``task_id``, ``wave``,
    ``priority``, ``status``, ``kind``, ``title``, ``body`` and ``repo``
    equal to the input, character for character. (``line_no`` is a property
    of a file, not of a record, and is the one field that legitimately
    differs.)

That property is what makes the two-way migration period safe: the file can
be regenerated from the store and re-imported without the import being seen
as a change (``report.changed == 0``), so a projection write can never
silently rewrite the store it came from.

Its bound is stated as precisely as the guarantee, because a guarantee that
reads wider than it acts is the defect it exists to prevent:

* the round trip holds for ANY ``title``, ``body`` and ``repo`` — arbitrary
  text, including record-shaped lines, blank lines, leading/trailing
  whitespace, ``str.splitlines`` boundaries and lone backslashes, which the
  parser's escape helpers carry (see ``backlog_parser``);
* it requires ``task_id``, ``wave``, ``priority``, ``status`` and ``kind``
  to be inside migration 0005's CHECK vocabularies, which every STORED row
  satisfies by construction. A record that is not — only reachable by
  calling this module with a hand-built ``ParsedTask`` — raises
  ``UnrenderableTask`` rather than emitting a line that would come back as
  something else. Refusing loudly is the point: a wave the record line
  cannot spell has no honest rendering.

The master file has TWO readers, and the projection serves both or it is
not the master file. ``backlog_parser`` reads the ``- **VOYN-…** |`` record
lines; ``backlog_client.parse_recommendations`` — the console's Master
Backlog panel, pointed at this same file by ``AICC_MASTER_BACKLOG`` — reads
section 0B's ``- VOYN_RECOMMENDATION | …`` lines, which ``backlog_export``
renders. So ``render_backlog`` takes those already-rendered lines and emits
them in their own section ahead of the records: a projection that dropped
them would blind the panel the moment it was rendered over the path the
panel reads. They are inert to the round trip — the parser sees a list item
that is not record-shaped, outside any record — and that is asserted, not
assumed.

Read-only over ``backlog_task``, regenerated whole, never merged with the
previous file. It renders and returns text; the durable write lives in
``command_center.projection_writer`` (this module carries the ``db`` path
token the AIOS boundary gate reads as a persistence-engine signature, and
that gate's judgement stands — see that module's docstring).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from command_center import backlog_client
from command_center.db import backlog_export
from command_center.db.backlog_parser import (
    FIELD_SEP,
    KINDS,
    NAMED_WAVES,
    NUMERIC_WAVE,
    PRIORITY_SHAPE,
    STATUSES,
    TASK_ID_SHAPE,
    VERTICAL_WHITESPACE,
    ParsedTask,
    body_line_is_literal,
    encode_body_line,
    encode_machine_directive,
    parse_backlog,
)

__all__ = [
    "UnrenderableTask",
    "render_task",
    "render_backlog",
    "verify_round_trip",
    "fetch_tasks",
    "fetch_projection",
    "render_master_file",
    "two_way_window_notice",
    "HEADER",
    "RECOMMENDATION_SECTION",
    "RECORD_SECTION",
    "ROUND_TRIP_FIELDS",
    "TWO_WAY_WINDOW_ENDS",
]

#: The recorded end of the two-way period. BO-S4 grants the Markdown file a
#: write direction "only for the duration of the migration, with an explicit
#: removal date" — and a temporary window with no date is how a temporary
#: window becomes permanent. So the date is here, in the code, rather than
#: in a plan: after it, `backlog-import` says on every run that it is the
#: direction scheduled for removal. It only SAYS so — a hard refusal on a
#: calendar date would stop the store being fed by the one command that
#: currently feeds it, which is an outage, not a migration. Moving the date
#: is the owner's call and is made by editing this line.
TWO_WAY_WINDOW_ENDS = date(2026, 12, 1)

#: Every field the round trip carries. ``line_no`` is absent on purpose: it
#: is a property of a file, not of a record. One definition, so the checker
#: below, the CLI's pre-write guard and the tests cannot drift into checking
#: different subsets — a field added to ``ParsedTask`` and forgotten here
#: would be a silently unverified field.
ROUND_TRIP_FIELDS = (
    "task_id",
    "wave",
    "priority",
    "status",
    "kind",
    "title",
    "body",
    "repo",
)

#: Continuation lines sit one level under the record. Two spaces, not four:
#: four would make Markdown read the block as a code fence, and the parser
#: only requires "deeper than the record's own indent".
_INDENT = "  "

HEADER = (
    "# VOYN master backlog — generated projection\n"
    "\n"
    "RENDERED from the canonical PostgreSQL backlog store (`backlog_task`)\n"
    "by `aicc-db backlog-project`. Regenerated whole on every run. Editing\n"
    "this file edits a rendering, not the backlog — during the migration\n"
    "period an edit here survives only until the next render, and after it\n"
    "the file is read-only for good.\n"
    "\n"
    "The `<!-- voyn:machine ... -->` comment under a record carries the\n"
    "stored fields the record line has no slot for (`kind`, `repo`); a body\n"
    "line written as `\\\"...\"` is a JSON-escaped line whose exact text the\n"
    "record line's own syntax could not hold. Both are read back by\n"
    "`aicc-db backlog-import`.\n"
    "\n"
)
#: The console's section (``backlog_client.parse_recommendations``), kept
#: under its incumbent 0B heading and title so a reader who knows the master
#: file finds it where it has always been.
RECOMMENDATION_SECTION = "## 0B. Machine records\n\n"
#: The importer's section.
RECORD_SECTION = "## Machine task records\n\n"


class UnrenderableTask(ValueError):
    """A record the master format cannot spell without changing it."""


def _refuse(task_id: str, what: str, value: object) -> UnrenderableTask:
    return UnrenderableTask(f"{task_id}: {what} does not normalize: {value!r}")


def _wave_field(task: ParsedTask) -> str:
    if NUMERIC_WAVE.match(task.wave):
        return f"Wave {task.wave}"
    if task.wave in NAMED_WAVES:
        return task.wave
    raise _refuse(task.task_id, "wave", task.wave)


def _title_is_literal(title: str) -> bool:
    """True when ``title`` can be spelled as a ``\\`slug\\``` field and read
    back unchanged: non-empty (an empty slug is not a slug and the field
    would be swallowed as prose), no backtick (it would close the span
    early), no field separator (it would split the line into two fields) and
    no ``str.splitlines`` boundary (it would split the line into two lines).
    Whitespace inside the backticks is safe — the span fences it."""
    return bool(
        title
        and "`" not in title
        and FIELD_SEP not in title
        and VERTICAL_WHITESPACE.search(title) is None
    )


def render_task(task: ParsedTask) -> list[str]:
    """The lines of one record: the record line, its machine directive, then
    one line per body line."""
    if not TASK_ID_SHAPE.match(task.task_id):
        raise _refuse(task.task_id, "task_id", task.task_id)
    if task.status not in STATUSES:
        raise _refuse(task.task_id, "status", task.status)
    if task.kind not in KINDS:
        raise _refuse(task.task_id, "kind", task.kind)
    if task.priority is not None and not PRIORITY_SHAPE.match(task.priority):
        raise _refuse(task.task_id, "priority", task.priority)

    fields = [_wave_field(task), task.status]
    if task.priority is not None:
        fields.append(task.priority)
    # `kind` and `repo` are ALWAYS written, never left to be re-derived.
    # `kind` is not a function of the id (a stored gate need not carry a
    # `-G<n>` suffix, and an id that does need not be a gate), and `repo` is
    # reconstructed on import from a body hint or the id's family — both are
    # heuristics over the authored dialect, and a projection that leaned on
    # them would silently overwrite any stored value that disagreed.
    machine: dict[str, Any] = {"kind": task.kind, "repo": task.repo}
    if _title_is_literal(task.title):
        fields.append(f"`{task.title}`")
    else:
        # The reader still gets a name for the record (the id, the one value
        # guaranteed spellable); the exact stored title travels in the
        # directive, which overrides the slug on the way back.
        fields.append(f"`{task.task_id}`")
        machine["title"] = task.title

    lines = [f"- **{task.task_id}**{FIELD_SEP}" + FIELD_SEP.join(fields)]
    lines.append(_INDENT + encode_machine_directive(machine))
    if task.body:
        for body_line in task.body.split("\n"):
            lines.append(
                _INDENT
                + (
                    body_line
                    if body_line_is_literal(body_line)
                    else encode_body_line(body_line)
                )
            )
    return lines


#: What a section-0B line must look like to be placed in this file. The
#: check is on SHAPE only — the recommendation format belongs to
#: `backlog_client`, whose own constants build this prefix so a rename there
#: breaks the check loudly instead of silently — but a line that is not one
#: of its records has no business in that section, and one carrying a line
#: break would silently become two.
_RECOMMENDATION_PREFIX = (
    "- " + backlog_client.RECOMMENDATION_MARKER + backlog_client.FIELD_SEP
)


def render_backlog(
    tasks: list[ParsedTask], recommendation_lines: Sequence[str] = ()
) -> str:
    """The whole projection: section 0B for the console, then the machine
    records for the importer.

    Duplicate ids are refused rather than rendered: the parser keeps the
    FIRST occurrence of an id and reports the rest, so a file with two rows
    for one id does not round-trip — and a store that produced one would
    have a broken primary key.
    """
    for line in recommendation_lines:
        if not line.startswith(_RECOMMENDATION_PREFIX) or VERTICAL_WHITESPACE.search(
            line
        ):
            raise UnrenderableTask(f"not a section-0B record line: {line!r}")
    seen: set[str] = set()
    blocks = []
    for task in tasks:
        if task.task_id in seen:
            raise UnrenderableTask(f"{task.task_id}: duplicate task_id")
        seen.add(task.task_id)
        blocks.append("\n".join(render_task(task)))
    text = HEADER
    if recommendation_lines:
        text += RECOMMENDATION_SECTION + "\n".join(recommendation_lines) + "\n\n"
    # A blank line between records: the parser ignores blank lines inside a
    # record block (a body's own blank lines are escaped, so a literal blank
    # line in the file is unambiguously a separator).
    return text + RECORD_SECTION + "\n\n".join(blocks) + "\n"


def two_way_window_notice(today: date) -> str | None:
    """The import direction's sunset notice, or ``None`` while the window is
    open. Pure and date-injected: a rule that only fires on a future wall
    clock is a rule nobody has ever executed."""
    if today < TWO_WAY_WINDOW_ENDS:
        return None
    return (
        f"NOTICE: the Markdown -> store import window closed on "
        f"{TWO_WAY_WINDOW_ENDS.isoformat()}. The store is canonical and "
        f"`backlog-project` renders the file from it; this direction is the "
        f"one scheduled for removal (BO-S4)."
    )


def verify_round_trip(
    tasks: list[ParsedTask], text: str | None = None
) -> list[tuple[str, str]]:
    """Re-parse a rendered projection and return every way it differs from
    the records it was rendered from — ``[]`` when the property holds.

    Executable rather than documentary: the guarantee in this module's
    docstring is checked against the ACTUAL text about to be written, so a
    stored value outside the format's bound is caught before it replaces the
    file instead of after, when the difference would already be the only
    copy.
    """
    text = render_backlog(tasks) if text is None else text
    report = parse_backlog(text)
    problems = [
        ("-", f"unparsed line {line_no}: {reason} :: {excerpt}")
        for line_no, reason, excerpt in report.unparsed
    ]
    if len(report.tasks) != len(tasks):
        problems.append(
            ("-", f"read back {len(report.tasks)} records, rendered {len(tasks)}")
        )
    for original, reparsed in zip(tasks, report.tasks, strict=False):
        if original.task_id != reparsed.task_id:
            problems.append(
                (original.task_id, f"out of order: read back as {reparsed.task_id}")
            )
            continue
        for name in ROUND_TRIP_FIELDS:
            stored, read_back = getattr(original, name), getattr(reparsed, name)
            if stored != read_back:
                problems.append(
                    (original.task_id, f"{name}: {stored!r} came back as {read_back!r}")
                )
    return problems


_SELECT = (
    "SELECT task_id, wave, priority, status, kind, title, body, repo, updated_at "
    "FROM backlog_task "
    # Deterministic and readable: numeric waves in numeric order (a text
    # sort puts '10' before '2'), named lanes after them, then priority then
    # id — so two renders of one store state are byte-identical. The CASE
    # guards the cast: `wave` also holds named lanes ('COM', 'W7'), and
    # `'COM'::numeric` is an error, not a null.
    "ORDER BY (wave ~ '^[0-9]+(\\.[0-9]+)?$') DESC, "
    "         CASE WHEN wave ~ '^[0-9]+(\\.[0-9]+)?$' THEN wave::numeric END, "
    "         wave, priority NULLS LAST, task_id"
)


def fetch_projection(conn: Any) -> tuple[list[ParsedTask], list[dict[str, Any]]]:
    """ONE query, both views of every stored row: the records the importer
    reads back, and the row dicts section 0B is rendered from.

    One query rather than two on purpose — two would each take their own
    snapshot under READ COMMITTED, and a commit landing between them would
    put a task in one section of the file and not the other.

    Terminal rows included: the projection is the store's whole truth, not a
    worklist. ``line_no`` is 0 — these records did not come from a file, and
    0 says so instead of inventing a line number that would read like
    provenance.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT)
        columns = [description[0] for description in cur.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    tasks = [
        ParsedTask(
            task_id=row["task_id"],
            wave=row["wave"],
            priority=row["priority"],
            status=row["status"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            repo=row["repo"],
            line_no=0,
        )
        for row in rows
    ]
    return tasks, rows


def fetch_tasks(conn: Any) -> list[ParsedTask]:
    """Every stored row as a ``ParsedTask``, in the projection's order."""
    return fetch_projection(conn)[0]


def render_master_file(conn: Any) -> tuple[str, list[ParsedTask]]:
    """The master file's whole text, plus the records it was rendered from.

    The one definition of "what the master file is", so the CLI command and
    ``BacklogStore.export_markdown`` cannot render two different files and
    both call themselves the projection. The records come back with the text
    because the caller that writes the file is the caller that should be
    able to VERIFY it (``verify_round_trip``) before it replaces anything.
    """
    tasks, rows = fetch_projection(conn)
    return (
        render_backlog(tasks, [backlog_export.render_record(row) for row in rows]),
        tasks,
    )
