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
    """

    records: list[BacklogRecommendation] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    source_path: Path | None = None
    source_mtime: float | None = None
    exists: bool = False


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
    )


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


# --- Rich execution records (section-body task lines) -------------------------
#
# Beyond section 0B's machine `VOYN_RECOMMENDATION` records, the master file's
# body carries one structured list line per executable task:
#
#     - **VOYN-<ID>** | <wave> | <status>[ annotations] | <priority> | ...
#
# These lines are where execution STATUS actually lives (`OPEN`,
# `IN_PROGRESS`, `READY_TO_REVIEW`, `DONE`, ...) — the 0B records carry only
# the planning status (`PO-Approved`). This parser is the shared, exact-token
# reader for that surface (machine-fields rule: no substring matching; an
# unrecognized status is surfaced as `UNKNOWN`, never guessed and never
# silently dropped). Read-only, like everything in this module.

#: The exact execution-status vocabulary. Annotations after the token (e.g.
#: "OPEN (сверено ...)") are permitted and ignored; the token itself must
#: match exactly.
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
    }
)

_RICH_LINE = re.compile(
    r"^- \*\*(?P<id>VOYN-[A-Z0-9-]+)\*\*\s*\|\s*(?P<wave>[^|]+?)\s*\|\s*"
    r"(?P<status>[^|]+?)\s*\|\s*(?P<priority>[^|]+?)\s*\|",
    re.MULTILINE,
)


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


def parse_rich_records(text: str) -> list[RichRecord]:
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


# --- read-only board view over the projection (VOYN-W0-AICC-WIRE-BACKLOG-
# API-TO-DEPLOYED-WEBAPI, first increment) ---------------------------------
#
# The console's Overview/Kanban read the local task repository; on a host
# whose store is empty (the deployed console) they rendered zeros while the
# fleet's real state sat in the same projection file the Master Backlog
# panel already reads. This maps that projection into the read model's task
# shape — a VIEW, never a store: every record is stamped `source: "master"`
# and `read_only: True`, and the write path refuses them (see
# `legacy_task_helpers.upsert_tasks`), so no second task store can emerge.

#: Master-store execution statuses → the read model's canonical lanes.
#: DECIDED maps to Closed (record closed by decision, not by work);
#: REJECTED and DEFER_TO_USER surface as Blocked — both wait on something
#: other than an executor.
_MASTER_STATUS_TO_LANE: dict[str, str] = {
    "OPEN": "Backlog",
    "UNTRIAGED": "Backlog",
    "NEEDS_REFINEMENT": "Backlog",
    "IN_PROGRESS": "In Progress",
    "READY_TO_REVIEW": "Review",
    "REJECTED": "Blocked",
    "DEFER_TO_USER": "Blocked",
    "SPLIT": "Blocked",
    "DONE": "Done",
    "DECIDED": "Closed",
}


def projection_board_tasks(path: str | os.PathLike[str] | None = None) -> list[dict]:
    """The master projection as read-only board tasks (empty when absent)."""
    projection = load_projection(path)
    tasks: list[dict] = []
    for record in projection.records:
        tasks.append(
            {
                "id": record.issue_id,
                "title": record.task,
                # An unmapped status keeps its raw name and lands in the
                # read model's visible `other` bucket instead of silently
                # inflating Backlog (independent review of 92a501f).
                "status": _MASTER_STATUS_TO_LANE.get(record.status, record.status),
                "project": record.owner if record.owner != "-" else "VOYN",
                "priority": record.priority if record.priority != "-" else "",
                "task_type": "backlog",
                "source": "master",
                "read_only": True,
            }
        )
    return tasks
