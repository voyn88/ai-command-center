"""Live *read* projection of the VOYN master backlog into ACC.

The Backlog Engine (`work/VOYN_BACKLOG_ENGINE_PLAN.md`) is the single owner of the
`Task` entity. ACC, ITC and the other centers are interfaces over it, never a
second source of truth — engine plan invariant #5: "Локальные ``tasks.json``,
очереди и UI-модели являются только проекциями." This module is exactly that
projection for ACC's *read* side (the W0 line "Read/write интеграция ACC без
второго task store"): it reads the master store's machine records and hands ACC a
stable, read-only view. It has **no write surface** — nothing here creates,
mutates, saves or deletes a task, so wiring ACC through it can never make ACC a
divergent store. Writes go the other way, through the Backlog API, and land back
here only as a re-read of the master file.

The master store is authored as `work/VOYN_TASKS_BACKLOG.md`, whose section 0B
carries the *only* machine source of tasks: one `VOYN_RECOMMENDATION` record per
line, 14 ``key=value`` fields separated by exactly ``" | "``:

    - VOYN_RECOMMENDATION | ts=... | status=... | issue_id=... | current_wave=...
      | proposed_wave=... | priority=... | owner=... | effect=... | effort=...
      | acceptance=... | task=... | evidence=... | file_scope=...
      | parallel_domain=...

Only *record* lines (a Markdown list item, ``- VOYN_RECOMMENDATION | ``) are
parsed. The section's spec **template** — the same marker introduced with a
backtick and carrying ``<...>`` placeholders — is prose describing the format, not
a record, so it is skipped rather than mis-parsed. A record that is present but
malformed is reported (never silently dropped and never allowed to crash the read)
so the UI can surface "N lines could not be read" instead of quietly under-showing
the backlog.

As of VOYN-W0-BACKLOG-ORCHESTRATOR (BO-S1..S4), PostgreSQL's ``backlog_task``
table is the canonical store for this data, not this file: ``backlog-export``
(``command_center/db/backlog_export.py``) renders ``backlog_task`` back into
exactly the record format above on a five-minute tick
(``deploy/systemd/aicc-backlog-export.timer``), so this module keeps reading
the same file format while what fills it changes underneath. ``backlog-import``
(``ops/aicc_backlog_publish.py``) still feeds hand edits from this file into
the store in the other direction — both directions are temporary by design;
see ``docs/adr/0011-backlog-projection-bidirectional-bridge.md`` for the
condition and 2026-11-01 date under which the import side retires and this
file becomes purely generated output.

A generated file also carries a header stamping when it was rendered and from
how many store rows; ``load_projection`` reads it into ``Projection.stamp``, so
a reader can tell a live projection from one whose export tick died without
trusting ``mtime`` — which any copy of the file resets to now. See the
"Generated-projection header" section below.

The master file lives outside this repo (it belongs to the Backlog Engine
project), so its path is *configuration*, resolved exactly like every other
runtime location — an explicit argument, else the ``AICC_MASTER_BACKLOG``
environment variable, else nothing. An unconfigured or missing file is not an
error: it yields an empty-but-usable projection (``exists=False``), so ACC renders
"backlog not connected" rather than throwing.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

# --- Wire format ------------------------------------------------------------

#: Environment variable naming the master backlog markdown file. Mirrors
#: ``AICC_DATA_DIR`` (see ``command_center.storage.resolve_data_dir``): one
#: variable points ACC at the Backlog Engine's store, and tests point it at a
#: throwaway fixture.
MASTER_BACKLOG_ENV = "AICC_MASTER_BACKLOG"

#: Leading token of every machine record.
RECOMMENDATION_MARKER = "VOYN_RECOMMENDATION"

#: The 14 ``key=value`` fields, in order, that follow the marker. The count and
#: order are the format contract; a line with any other key set is not a record
#: we understand and is reported as an error rather than partially accepted.
RECOMMENDATION_FIELDS: tuple[str, ...] = (
    "ts",
    "status",
    "issue_id",
    "current_wave",
    "proposed_wave",
    "priority",
    "owner",
    "effect",
    "effort",
    "acceptance",
    "task",
    "evidence",
    "file_scope",
    "parallel_domain",
)

#: Field separator — exactly this, so a value can never itself contain " | ".
FIELD_SEP = " | "

#: Only records at this status are handed to executors (backlog rule: "Clod
#: исполняет только записи со статусом ``PO-Approved``").
STATUS_APPROVED = "PO-Approved"


# --- Generated-projection header ---------------------------------------------
#
# Since BO-S4 this file is normally *rendered* rather than authored, and the
# rendering stamps its own age and size into its header
# (``command_center/db/backlog_export.py``). That stamp is a format contract
# between the writer and every reader, so — exactly like ``RECOMMENDATION_FIELDS``
# above — it is defined here, on the reading side, and the exporter renders
# through it; the two cannot drift into a stamp nobody can read.
#
# The stamp exists because the freshness signal we had *lies*. ``mtime`` is a
# property of the filesystem, not of the text: ``cp``, ``scp``, a checkout, a
# container build, an editor's save-in-place all reset it to now, so a
# projection rendered two weeks ago reads as seconds old the moment it moves
# host. That is precisely the failure BO-S4 was opened for — a console booted
# 2026-09-03 rendering a file that stopped being true on 2026-08-20, with
# nothing on screen to say so — and mtime is structurally unable to close it,
# because on the reader's host the file really was written seconds ago. An age
# written *into* the text travels with the text.

#: How far into a file a header claim still counts as that file's own header.
#: Bounded on purpose, and shared by both readers of this header (this module,
#: and ``backlog_export.is_generated_projection`` on the import side): the
#: owner's hand-authored backlog legitimately *describes* the exporter inside a
#: task body — BO-S4 is a task in that very file — and a body quoting one of
#: these lines must never be mistaken for the file's own provenance claim.
HEADER_SCAN_LINES = 20

#: UTC, second precision, explicit ``Z``: one unambiguous zone, so two ticks'
#: stamps are directly comparable and neither is ambiguous about which clock it
#: means.
_STAMP_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The whole-line shape of the stamp. Anchored at both ends and never matched as
#: a substring (machine-fields rule), so prose that merely mentions a render time
#: is not read as one.
_GENERATED_STAMP = re.compile(
    r"^Rendered (?P<at>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) "
    r"from (?P<rows>\d+) task row\(s\)$"
)

#: Three consecutive missed ticks of ``aicc-backlog-export.timer`` (5 min).
#: One missed tick is normal jitter — the timer's own ``AccuracySec``, a slow
#: query, a restart — and alarming on it would train the owner to ignore the
#: alarm. Three in a row is not jitter.
PROJECTION_STALE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class GeneratedStamp:
    """A generated projection's own claim about when it was rendered and from
    how many store rows.

    Read straight out of the file's header, so it survives every copy that
    destroys ``mtime`` — which is the entire point of it existing.
    """

    rendered_at: datetime
    row_count: int

    def age(self, now: datetime) -> timedelta:
        """How far behind ``now`` this rendering is. Negative if the writer's
        clock runs ahead of the reader's, which is reported as-is rather than
        clamped: a projection stamped in the future is a real problem (two hosts
        disagreeing about the time) and hiding it behind ``max(0, ...)`` would
        make it look perfectly fresh forever."""
        return now - self.rendered_at

    def is_stale(
        self, now: datetime, limit: timedelta = PROJECTION_STALE_AFTER
    ) -> bool:
        """Whether the export tick that writes this file has stopped."""
        return self.age(now) > limit


def render_generated_stamp(rendered_at: datetime, row_count: int) -> str:
    """The header's one machine-readable line, rendered.

    ``backlog_export`` interpolates the result into its header rather than
    formatting the line itself, so the exporter cannot reword the one line this
    module parses without this function changing too.
    """
    stamped = rendered_at.astimezone(UTC).strftime(_STAMP_TIME_FORMAT)
    return f"Rendered {stamped} from {row_count} task row(s)"


def parse_generated_stamp(text: str) -> GeneratedStamp | None:
    """The stamp a ``backlog-export`` rendering carries, or ``None`` for a file
    that makes no such claim.

    ``None`` is the normal answer for the owner's hand-authored backlog, which
    has no tick behind it and therefore no cadence to be late against; a caller
    showing freshness falls back to ``mtime`` for that file. Only the header is
    scanned, for the reason on ``HEADER_SCAN_LINES``.

    A line that is stamp-shaped but not a real moment (``2026-13-45T99:99:99Z``
    — the regex constrains digit counts, not calendars) is skipped rather than
    raised. This module's contract is that a malformed file degrades the read,
    never crashes it; ``load_projection`` calls this on every file it opens,
    including the owner's own, so a single mistyped character in a header must
    not take the whole Master Backlog page down.
    """
    for line in text.splitlines()[:HEADER_SCAN_LINES]:
        match = _GENERATED_STAMP.match(line.strip())
        if match is None:
            continue
        try:
            rendered_at = datetime.strptime(match["at"], _STAMP_TIME_FORMAT)
        except ValueError:
            continue
        return GeneratedStamp(
            rendered_at=rendered_at.replace(tzinfo=UTC),
            row_count=int(match["rows"]),
        )
    return None


@dataclass(frozen=True)
class BacklogRecommendation:
    """One parsed ``VOYN_RECOMMENDATION`` record from the master store.

    A pure value object mirroring the wire fields one-to-one, plus the source
    ``line_no`` for provenance. Read-only by construction (frozen).
    """

    ts: str
    status: str
    issue_id: str
    current_wave: str
    proposed_wave: str
    priority: str
    owner: str
    effect: str
    effort: str
    acceptance: str
    task: str
    evidence: str
    file_scope: str
    parallel_domain: str
    line_no: int = 0

    @property
    def is_approved(self) -> bool:
        """Whether this record is executable (approved by the product owner)."""
        return self.status == STATUS_APPROVED

    @property
    def title(self) -> str:
        """Human-readable title from the ``task`` slug (``a_b_c`` -> ``A b c``)."""
        text = self.task.replace("_", " ").strip()
        return text[:1].upper() + text[1:] if text else text


@dataclass(frozen=True)
class ParseError:
    """A record line that could not be read, with why — surfaced, never hidden."""

    line_no: int
    line: str
    reason: str


@dataclass(frozen=True)
class ParseResult:
    """Outcome of parsing backlog text: the good records and the bad lines."""

    records: list[BacklogRecommendation] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)


@dataclass(frozen=True)
class Projection:
    """A point-in-time read projection of the master backlog for ACC.

    ``source_mtime`` is the file's modification time at read; the caller re-reads
    to get a fresh projection, which is what makes the view "live" without ACC
    ever holding its own copy of the truth.

    ``stamp`` is the *file's own* claim about its age (``GeneratedStamp``), set
    when the file is a ``backlog-export`` rendering and ``None`` when it is
    hand-authored. Prefer it over ``source_mtime`` wherever both exist: mtime
    describes when this host last touched these bytes, ``stamp`` describes when
    the store they mirror was actually read, and only the second survives the
    copy that moved the file here.
    """

    records: list[BacklogRecommendation] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    source_path: Path | None = None
    source_mtime: float | None = None
    exists: bool = False
    stamp: GeneratedStamp | None = None


def _is_record_line(stripped: str) -> bool:
    """True for a Markdown list item carrying a record (``- VOYN_RECOMMENDATION |``).

    The spec template in section 0B uses the same marker but is introduced with a
    backtick and holds ``<...>`` placeholders; it is not a list item, so it never
    matches here and is left as prose.
    """
    if not stripped.startswith("-"):
        return False
    body = stripped[1:].lstrip()
    return body.startswith(RECOMMENDATION_MARKER + FIELD_SEP) or body == RECOMMENDATION_MARKER


def _parse_line(raw: str, line_no: int) -> BacklogRecommendation | ParseError:
    body = raw.strip()[1:].lstrip()  # drop the leading "- "
    tokens = body.split(FIELD_SEP)
    if not tokens or tokens[0] != RECOMMENDATION_MARKER:
        return ParseError(line_no, raw, "not a VOYN_RECOMMENDATION record")
    fields = tokens[1:]
    if len(fields) != len(RECOMMENDATION_FIELDS):
        return ParseError(
            line_no,
            raw,
            f"expected {len(RECOMMENDATION_FIELDS)} fields, found {len(fields)}",
        )
    values: dict[str, str] = {}
    for expected_key, token in zip(RECOMMENDATION_FIELDS, fields, strict=True):
        key, sep, value = token.partition("=")
        if not sep:
            return ParseError(line_no, raw, f"field {token!r} is not key=value")
        if key != expected_key:
            return ParseError(
                line_no, raw, f"expected field {expected_key!r}, found {key!r}"
            )
        values[key] = value
    return BacklogRecommendation(line_no=line_no, **values)


def parse_recommendations(text: str) -> ParseResult:
    """Parse master-backlog ``text`` into records and per-line errors (pure)."""
    records: list[BacklogRecommendation] = []
    errors: list[ParseError] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        if not _is_record_line(raw.strip()):
            continue
        parsed = _parse_line(raw, index)
        if isinstance(parsed, ParseError):
            errors.append(parsed)
        else:
            records.append(parsed)
    return ParseResult(records=records, errors=errors)


def resolve_backlog_path(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Resolve the master backlog file: explicit ``path`` > env var > ``None``."""
    if path is not None:
        return Path(path)
    override = os.environ.get(MASTER_BACKLOG_ENV)
    return Path(override) if override else None


def load_projection(path: str | os.PathLike[str] | None = None) -> Projection:
    """Read and project the master backlog. Absence is empty, never an error."""
    resolved = resolve_backlog_path(path)
    if resolved is None or not resolved.is_file():
        return Projection(source_path=resolved, exists=False)
    text = resolved.read_text(encoding="utf-8")
    result = parse_recommendations(text)
    return Projection(
        records=result.records,
        errors=result.errors,
        source_path=resolved,
        source_mtime=resolved.stat().st_mtime,
        exists=True,
        stamp=parse_generated_stamp(text),
    )


def stamp_matches_content(projection: Projection) -> bool | None:
    """Whether a generated projection still holds as many record lines as its
    header says it was rendered with. ``None`` when there is no stamp to check
    against.

    For a file straight off an export tick this is true by construction — the
    header's count is ``len(rows)`` and the body is one line per row — so a
    mismatch means record lines were added or removed after rendering. That is
    a partial detector for ADR-0011's one convention that is otherwise
    unenforceable ("the owner must not edit the generated file directly"):
    it catches inserted and deleted records, and does *not* catch a field
    edited in place, which changes no count. Partial, and worth having: the
    edit it catches is the one that silently changes what the panel totals.

    Errors count toward the total on purpose. A line the parser rejects is
    still a line that was rendered; excluding them would report every
    unreadable record as a missing one and confuse two different problems.
    """
    if projection.stamp is None:
        return None
    present = len(projection.records) + len(projection.errors)
    return projection.stamp.row_count == present


def approved_recommendations(
    projection: Projection,
) -> list[BacklogRecommendation]:
    """The subset of a projection's records that executors may act on."""
    return [rec for rec in projection.records if rec.is_approved]


#: Priority tokens, most-urgent first — the order the execution queue is sorted by.
PRIORITY_ORDER: tuple[str, ...] = ("P0", "P1", "P2")


@dataclass(frozen=True)
class BacklogSummary:
    """Aggregate counts over a projection, for the read-only overview header.

    Pure data (no Streamlit): the panel turns this into metrics and tables. Every
    count is derived from the same ``records`` the rows render, so header totals and
    the filtered table can never disagree about what the master store contains.
    """

    total: int
    approved: int
    errors: int
    by_wave: dict[str, int]
    by_priority: dict[str, int]
    by_status: dict[str, int]
    by_domain: dict[str, int]


def _ordered_counts(values: list[str], order: tuple[str, ...] = ()) -> dict[str, int]:
    """Count ``values``, listing ``order`` first (even at zero), then any extras."""
    counts = Counter(values)
    result = {key: counts.get(key, 0) for key in order if counts.get(key, 0)}
    for key in sorted(counts):
        if key not in result:
            result[key] = counts[key]
    return result


def summarize(projection: Projection) -> BacklogSummary:
    """Aggregate a projection into the counts the overview header shows."""
    records = projection.records
    return BacklogSummary(
        total=len(records),
        approved=sum(1 for r in records if r.is_approved),
        errors=len(projection.errors),
        by_wave=_ordered_counts([r.proposed_wave for r in records]),
        by_priority=_ordered_counts([r.priority for r in records], PRIORITY_ORDER),
        by_status=_ordered_counts([r.status for r in records]),
        by_domain=_ordered_counts([r.parallel_domain for r in records]),
    )


def execution_queue(projection: Projection) -> list[BacklogRecommendation]:
    """The executable queue derived from the master store, most-urgent first.

    ACC does not own a second queue: the "queue" is simply the approved records
    the Backlog Engine would hand executors, ordered by priority (``P0`` before
    ``P1`` …) then wave. This is a read view — claiming/leasing happens in the
    Backlog API, not here.
    """

    def sort_key(rec: BacklogRecommendation) -> tuple[int, str, str]:
        try:
            priority_rank = PRIORITY_ORDER.index(rec.priority)
        except ValueError:
            priority_rank = len(PRIORITY_ORDER)
        return (priority_rank, rec.proposed_wave, rec.issue_id)

    return sorted(approved_recommendations(projection), key=sort_key)


def filter_records(
    records: list[BacklogRecommendation],
    *,
    query: str = "",
    wave: str | None = None,
    priority: str | None = None,
    status: str | None = None,
    domain: str | None = None,
) -> list[BacklogRecommendation]:
    """Search + facet filter for the rows table (pure; the panel binds widgets).

    ``query`` is a case-insensitive substring over the fields an operator scans
    for — id, task/title, owner, scope, acceptance. Each facet, when given,
    restricts to an exact match. All conditions are AND-ed.
    """
    needle = query.strip().lower()

    def matches(rec: BacklogRecommendation) -> bool:
        if wave and rec.proposed_wave != wave:
            return False
        if priority and rec.priority != priority:
            return False
        if status and rec.status != status:
            return False
        if domain and rec.parallel_domain != domain:
            return False
        if needle:
            haystack = " ".join(
                (
                    rec.issue_id,
                    rec.task,
                    rec.title,
                    rec.owner,
                    rec.file_scope,
                    rec.acceptance,
                )
            ).lower()
            if needle not in haystack:
                return False
        return True

    return [rec for rec in records if matches(rec)]


def to_read_model(rec: BacklogRecommendation) -> dict:
    """A stable, read-only dict view of a record for ACC's UI/task surfaces.

    ``read_only`` and ``source`` are explicit so any consumer that mistakes this
    for a mutable task record fails loudly rather than trying to persist it. Wave
    is the *proposed* wave — the planning engine's current placement.
    """
    return {
        "id": rec.issue_id,
        "title": rec.title,
        "task": rec.task,
        "status": rec.status,
        "priority": rec.priority,
        "wave": rec.proposed_wave,
        "current_wave": rec.current_wave,
        "owner": rec.owner,
        "effect": rec.effect,
        "effort": rec.effort,
        "acceptance": rec.acceptance,
        "evidence": rec.evidence,
        "file_scope": rec.file_scope,
        "domain": rec.parallel_domain,
        "ts": rec.ts,
        "source": "master_backlog",
        "read_only": True,
    }


# --- Rich execution records (execution status) --------------------------------
#
# Beyond section 0B's machine `VOYN_RECOMMENDATION` records, execution STATUS
# (`OPEN`, `IN_PROGRESS`, `READY_TO_REVIEW`, `DONE`, ...) lives on its own
# record surface — the 0B records carry only the coarse planning status
# (`PO-Approved`/`PO-Review`). This parser is the shared, exact-token reader
# for that surface (machine-fields rule: no substring matching; an
# unrecognized status is surfaced as `UNKNOWN`, never guessed and never
# silently dropped). Read-only, like everything in this module.
#
# TWO shapes carry it, for the length of ADR-0011's migration window:
#
#   hand-authored   - **VOYN-<ID>** | <wave> | <status>[ annotations] | <priority> | ...
#   machine-rendered - VOYN_TASK_STATUS | id=... | wave=... | status=... | priority=... | slug=...
#
# The second exists because the first cannot be machine-written while the
# import direction is alive. `backlog_parser._TASK_LINE` (the importer) matches
# a bold `**VOYN-...**` id followed by `| ` — of which the hand-authored rich
# shape is a strict SUBSET, so anything `backlog-export` rendered in that shape
# would be read straight back in as an authored task. There is no variant of
# the bold-id/pipe shape that satisfies one reader and not the other; the two
# were built to share that convention on purpose.
#
# So the machine surface takes the move `VOYN_RECOMMENDATION` already made for
# 0B records: a distinct leading marker on a plain (unbolded) list item, which
# neither `backlog_parser._TASK_LINE` nor `_RECORD_SHAPED` matches at all — it
# is invisible to the importer, not merely rejected by it (proved end-to-end in
# `tests/db/test_backlog_export.py`, through the real importer). That keeps
# ADR-0011's central safety property intact — the two directions still share no
# line shape — while letting the exporter state execution status exactly
# instead of collapsing it to approved/not-approved.

#: The exact execution-status vocabulary. Annotations after the token (e.g.
#: "OPEN (сверено ...)") are permitted and ignored; the token itself must
#: match exactly.
#:
#: Deliberately the SAME set as the store's own ``backlog_parser.STATUSES``.
#: It used to be that set minus ``DECIDED``, which was harmless while this
#: surface was only ever hand-authored — a human writing ``DECIDED`` on a body
#: line got ``UNKNOWN`` and nobody noticed. Once ``backlog-export`` renders
#: this surface FROM ``backlog_task``, a divergence here is a status the store
#: holds and every rich-record reader silently mislabels: a ``DECIDED`` row
#: would arrive as ``UNKNOWN`` and land in the Backlog lane with no trace that
#: a real, known status was thrown away. The two vocabularies now have to
#: agree, and ``tests/test_backlog_client.py`` pins that they do.
RICH_STATUSES: frozenset[str] = frozenset(
    {
        "UNTRIAGED",
        "OPEN",
        "IN_PROGRESS",
        "READY_TO_REVIEW",
        "DONE",
        "DEFER_TO_USER",
        "NEEDS_REFINEMENT",
        "SPLIT",
        "DECIDED",
    }
)

_RICH_LINE = re.compile(
    r"^- \*\*(?P<id>VOYN-[A-Z0-9-]+)\*\*\s*\|\s*(?P<wave>[^|]+?)\s*\|\s*"
    r"(?P<status>[^|]+?)\s*\|\s*(?P<priority>[^|]+?)\s*\|",
    re.MULTILINE,
)

#: Leading token of a machine-rendered execution-status record. Distinct from
#: ``RECOMMENDATION_MARKER`` because the two carry different vocabularies on
#: purpose (execution vs planning status) and a reader must never take one for
#: the other; distinct from the bold ``**VOYN-...**`` shape because that one
#: belongs to the importer (see the section comment above).
TASK_STATUS_MARKER = "VOYN_TASK_STATUS"

#: The fields of a ``VOYN_TASK_STATUS`` record, in order. Same contract style
#: as ``RECOMMENDATION_FIELDS``: exactly these keys, in exactly this order,
#: ``FIELD_SEP``-separated — anything else is not a record this module
#: understands, and is skipped rather than partially accepted.
TASK_STATUS_FIELDS: tuple[str, ...] = ("id", "wave", "status", "priority", "slug")


@dataclass(frozen=True)
class RichRecord:
    """One structured body task line: id, wave text, exact status, priority.

    ``status`` is a member of :data:`RICH_STATUSES` or the literal
    ``"UNKNOWN"`` when the line's token is outside the vocabulary. ``slug``
    is the backticked short name when the line carries one, else ``""``.
    """

    record_id: str
    wave: str
    status: str
    priority: str
    slug: str = ""

    @property
    def title(self) -> str:
        text = self.slug.replace("-", " ").replace("_", " ").strip()
        return text[:1].upper() + text[1:] if text else self.record_id


_RICH_SLUG = re.compile(r"`([^`]+)`")


def render_task_status(record: RichRecord) -> str:
    """One machine-rendered execution-status line.

    Lives here, beside the parser that reads it, for the same reason
    ``render_generated_stamp`` does: ``backlog_export`` renders THROUGH this
    function rather than formatting the line itself, so the writer cannot drift
    into a shape this module no longer reads.

    Values are not escaped here. The exporter is the only caller and it cleans
    every value first (``backlog_export._clean``: no ``|``, no line breaks) —
    which is where cleaning belongs, since it is the store's free text that
    needs it, not this format.
    """
    values = {
        "id": record.record_id,
        "wave": record.wave,
        "status": record.status,
        "priority": record.priority,
        "slug": record.slug,
    }
    tokens = [TASK_STATUS_MARKER] + [f"{key}={values[key]}" for key in TASK_STATUS_FIELDS]
    return "- " + FIELD_SEP.join(tokens)


def _parse_task_status_line(stripped: str) -> RichRecord | None:
    """One ``- VOYN_TASK_STATUS | ...`` line, or ``None`` if it is not one.

    Malformed lines return ``None`` (skipped) rather than raising: like every
    other reader in this module, a damaged file degrades the read instead of
    taking the caller's page down. Unlike the 0B records there is no error
    channel to report them on — ``parse_rich_records`` returns a bare list —
    so a record that cannot be read is simply absent, exactly as it was before
    this shape existed.
    """
    body = stripped[1:].lstrip()
    tokens = body.split(FIELD_SEP)
    if tokens[0] != TASK_STATUS_MARKER:
        return None
    fields = tokens[1:]
    if len(fields) != len(TASK_STATUS_FIELDS):
        return None
    values: dict[str, str] = {}
    for expected_key, token in zip(TASK_STATUS_FIELDS, fields, strict=True):
        key, sep, value = token.partition("=")
        if not sep or key != expected_key:
            return None
        values[key] = value
    status = values["status"]
    return RichRecord(
        record_id=values["id"],
        wave=values["wave"],
        status=status if status in RICH_STATUSES else "UNKNOWN",
        priority=values["priority"],
        slug=values["slug"],
    )


def _parse_authored_rich_records(text: str) -> list[RichRecord]:
    """The hand-authored bold-line surface: ``- **VOYN-<ID>** | ...``."""
    records: list[RichRecord] = []
    for match in _RICH_LINE.finditer(text):
        status_raw = match.group("status").strip().strip("*")
        token = status_raw.split()[0].strip("*") if status_raw else ""
        status = token if token in RICH_STATUSES else "UNKNOWN"
        line_end = text.find("\n", match.end())
        line = text[match.start() : line_end if line_end != -1 else len(text)]
        slug_match = _RICH_SLUG.search(line)
        records.append(
            RichRecord(
                record_id=match.group("id"),
                wave=match.group("wave").strip(),
                status=status,
                priority=match.group("priority").strip().strip("*"),
                slug=slug_match.group(1) if slug_match else "",
            )
        )
    return records


def _parse_machine_rich_records(text: str) -> dict[str, RichRecord]:
    """The machine-rendered surface, keyed by id, first occurrence winning.

    First-wins matches ``backlog_parser``'s own duplicate-id rule, so the two
    sides of the bridge resolve a repeated id the same way. A tick's own output
    never repeats one (the store's ``task_id`` is unique), so this only governs
    a file that was edited after rendering.

    Indentation is ignored, as it is for the 0B records whose marker
    convention this shape copies — and unlike the bold surface, where an
    indented line is deliberately skipped because it belongs to a parent
    record's evidence trail. The exporter never indents, so the difference
    only shows in a hand-mixed file, and finding the record there beats
    dropping it silently.
    """
    records: dict[str, RichRecord] = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("- " + TASK_STATUS_MARKER):
            continue
        parsed = _parse_task_status_line(stripped)
        if parsed is not None:
            records.setdefault(parsed.record_id, parsed)
    return records


def parse_rich_records(text: str) -> list[RichRecord]:
    """Every execution-status record in ``text``, from both surfaces.

    **Precedence: a machine-rendered record wins over a hand-authored one for
    the same id.** In production the two never meet — ``backlog-import`` reads
    the owner's authored file and ``backlog-export`` writes the rendering, and
    ADR-0011 keeps those two paths pointed at different files — so this rule
    governs a file that mixes them, which is possible only during the migration
    window (an owner pasting rendered lines into their own file, a
    part-converted document, a test). It resolves the way the whole task's
    invariant does: ``backlog_task`` is canonical and a ``VOYN_TASK_STATUS``
    line is a direct reading of it, while a bold line is owner-typed *input*
    that the store may already have moved past. Preferring the authored line
    would let a stale hand edit override live execution state — the exact
    silent staleness BO-S4 exists to end.

    A file with no machine lines — every hand-authored backlog, which is still
    most of them — is returned exactly as the bold-line parser read it,
    duplicate ids and all. The override path is the only behaviour this shape
    added; it does not otherwise re-interpret the surface that predates it.

    An overriding record keeps the position where its id first appeared, so a
    reader rendering this list in document order does not watch tasks jump
    around depending on which surface described them. Machine records for ids
    with no bold line at all follow, in file order.
    """
    authored = _parse_authored_rich_records(text)
    machine = _parse_machine_rich_records(text)
    if not machine:
        return authored

    records: list[RichRecord] = []
    overridden: set[str] = set()
    for record in authored:
        override = machine.get(record.record_id)
        if override is None:
            records.append(record)
        elif record.record_id not in overridden:
            records.append(override)
            overridden.add(record.record_id)
    for record_id, record in machine.items():
        if record_id not in overridden:
            records.append(record)
            overridden.add(record_id)
    return records


def load_rich_records(path: str | os.PathLike[str] | None = None) -> list[RichRecord]:
    """Read-only rich execution records from the master file (or [])."""
    resolved = resolve_backlog_path(path)
    if resolved is None or not resolved.exists():
        return []
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_rich_records(text)
