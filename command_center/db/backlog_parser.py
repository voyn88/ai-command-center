"""Parser for the canonical Markdown backlog (BO-S1).

The machine invariant this implements, verbatim from the delivery rules:
machine fields are **exact normalized schema values** — substring matching is
forbidden; `W0`, `W0.5`, `W00` and `W01` are distinct values. So every field
here is matched against a closed shape, and a line that does not normalize is
not guessed at: it lands in the report's ``unparsed`` list with its line
number and reason. The importer never loses input silently.

Vocabulary facts measured on the real file (2026-08-19), not assumed:

* task lines: ``- **VOYN-…** | Wave <w> | <STATUS> | [<priority>] |
  <owner…> | `slug` | description`` at two indent levels;
* statuses observed: the executable four plus UNTRIAGED, DEFER_TO_USER,
  SPLIT — all closed vocabulary here; gates (`…-G<n>`) are control records
  and get ``kind='gate'`` (they are refused by the transition function);
* priority may be ``P0``, ``**P0**`` or ``**P0 (annotation)**`` — the machine
  value is exactly ``P<digit>``, the annotation belongs to prose;
* two duplicate ids exist in the file: the FIRST occurrence wins, later ones
  are reported (an importer that silently overwrote would let the last stray
  copy of a record rewrite the canonical one).

Since BO-S4 this module owns BOTH directions of the format. The projection
(``backlog_projection.render_backlog``) renders records that this parser
reads back; the two halves share the constants and the escaping helpers
below so they cannot drift into "renders one dialect, parses another".

Two constructs exist only for that round trip, and both are conservative —
they are emitted only for a value the authored dialect cannot carry, so a
hand-written file never sees them:

* the MACHINE DIRECTIVE ``<!-- voyn:machine {...} -->`` — a comment line
  under a record carrying the fields the human line has no slot for
  (``kind``, ``repo``) or cannot spell (a ``title`` holding a backtick or a
  field separator). It is the authority: ``kind`` is a stored column that
  is NOT a function of the id (the store accepts ``kind='gate'`` on an id
  without a ``-G<n>`` suffix and the converse), and ``repo`` reconstructed
  from the body hint or the id family is a GUESS — the rule against
  substring inference applies to a round trip too;
* the ESCAPED BODY LINE ``\\"…"`` (a backslash and a JSON string) — for a
  body line that would not survive reparse verbatim: one that is blank,
  carries leading/trailing whitespace (continuations are read with
  ``str.strip``), holds a character ``str.splitlines`` would break on, or
  is shaped like a record or a directive. ``body_line_is_literal`` is the
  single predicate deciding this, and the renderer consults exactly it.

Pure module: no database, no I/O beyond the text it is given — so its tests
are hermetic and the store's tests need only prove the seam.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ParsedTask",
    "ParseReport",
    "parse_backlog",
    "EXECUTABLE_STATUSES",
    "NON_EXECUTABLE_STATUSES",
    "STATUSES",
    "KINDS",
    "NAMED_WAVES",
    "NUMERIC_WAVE",
    "PRIORITY_SHAPE",
    "TASK_ID_SHAPE",
    "FIELD_SEP",
    "VERTICAL_WHITESPACE",
    "MACHINE_FIELDS",
    "encode_machine_directive",
    "decode_machine_directive",
    "body_line_is_literal",
    "encode_body_line",
    "decode_body_line",
]

EXECUTABLE_STATUSES = ("OPEN", "IN_PROGRESS", "READY_TO_REVIEW", "DONE")
NON_EXECUTABLE_STATUSES = (
    "UNTRIAGED",
    "DEFER_TO_USER",
    "SPLIT",
    "NEEDS_REFINEMENT",
    "DECIDED",
)
STATUSES = frozenset(EXECUTABLE_STATUSES + NON_EXECUTABLE_STATUSES)
#: The stored `kind` vocabulary (migration 0005's CHECK), and a column in its
#: own right: the `-G<n>` id suffix only SEEDS it when a record is first read
#: out of an authored file.
KINDS = ("task", "gate")
#: The file's closed set of named lanes and idea pools, exactly as observed:
#: W1/W7 are FUTURE-wave idea pools and deliberately distinct from waves 1/7
#: (the W0-vs-W00 distinctness rule); P1/P0.5 are lane names of the idea
#: sections, not priorities.
NAMED_WAVES = ("COM", "WOW", "AICOS", "W1", "W7", "P1", "P0.5")
#: The separator between the fields of a record line. One definition: the
#: renderer joins with it and every "can this value be spelled inline"
#: predicate tests against it.
FIELD_SEP = " | "

_TASK_LINE = re.compile(r"^(\s*)- \*\*(VOYN-[A-Za-z0-9._-]+)\*\* \| (.+)$")
#: A line SHAPED like a record whose id is outside the VOYN namespace. Not a
#: task — but not silently droppable either: it is either a typo in a real
#: record or a foreign record, and both belong in the report.
_RECORD_SHAPED = re.compile(r"^\s*- \*\*([^*]+)\*\* \| ")
#: Numeric waves ("Wave N") plus the named lanes above.
NUMERIC_WAVE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_WAVE = re.compile(
    r"^Wave ([0-9]+(?:\.[0-9]+)?)$"
    + r"|^("
    + "|".join(re.escape(w) for w in NAMED_WAVES)
    + r")$"
)
_PRIORITY = re.compile(r"^P([0-9])(?:\s*\(.*\))?$", re.S)
#: The exact stored priority shape (0005's CHECK), with no annotation slack.
PRIORITY_SHAPE = re.compile(r"^P[0-9]$")
_SLUG = re.compile(r"^`([^`]+)`$")
_GATE_ID = re.compile(r"-G[0-9]+$")
TASK_ID_SHAPE = re.compile(r"^VOYN-[A-Za-z0-9][A-Za-z0-9._-]*$")
_ID_SHAPE = TASK_ID_SHAPE

#: Every boundary `str.splitlines` recognises — far wider than \r\n (\v, \f,
#: FS/GS/RS, \x85, U+2028/U+2029). A value carrying one of these cannot be
#: spelled on a line at all: the parser would read it as two lines. The same
#: class is why `backlog_export` scrubs them from its record projection; here
#: the round trip needs them PRESERVED, so they force the escaped form.
VERTICAL_WHITESPACE = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")

#: The machine directive: an HTML comment (invisible in rendered Markdown)
#: carrying a JSON object of stored fields the human line cannot hold.
#: `(.*)` is GREEDY on purpose — a value may itself contain `-->`, and only
#: the LAST one, anchored at end-of-line, is the terminator.
_MACHINE_DIRECTIVE = re.compile(r"^<!--\s*voyn:machine\s+(.*)\s*-->$")
MACHINE_DIRECTIVE_PREFIX = "<!-- voyn:machine "
MACHINE_DIRECTIVE_SUFFIX = " -->"
#: Closed vocabulary — an unknown key is reported, never applied. A directive
#: may not carry `body` (per-line escaping keeps the body readable) nor any
#: field the record line already spells exactly (wave/status/priority/id),
#: so the file can never disagree with itself about those.
MACHINE_FIELDS = ("kind", "repo", "title")
#: The escaped-body-line marker.
BODY_ESCAPE_PREFIX = "\\"


@dataclass(frozen=True, slots=True)
class ParsedTask:
    task_id: str
    wave: str
    priority: str | None
    status: str
    kind: str
    title: str
    body: str
    repo: str | None
    line_no: int


@dataclass(slots=True)
class ParseReport:
    tasks: list[ParsedTask] = field(default_factory=list)
    #: (line_no, reason, line excerpt) — nothing is dropped silently.
    unparsed: list[tuple[int, str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The round-trip format: the two constructs the projection renders and this
# parser reads back. Both live here, next to the shapes they must dodge, so
# "what the renderer escapes" and "what the parser unescapes" are one fact.
# ---------------------------------------------------------------------------


def body_line_is_literal(line: str) -> bool:
    """True when ``line`` is one the projection may write verbatim.

    Verbatim means: indented by the renderer, then read back by the
    continuation branch below — which applies ``str.strip`` — and compared
    equal. Each clause is one way that identity fails, and none of them is
    hypothetical:

    * empty: a blank line is not a continuation at all, it is skipped;
    * ``line != line.strip()``: leading or trailing whitespace (spaces, tabs)
      is removed on the way back in;
    * a leading backslash: it would be read as the escape marker;
    * vertical whitespace: ``str.splitlines`` would break the line in two;
    * record-shaped or directive-shaped: the line would be read as a NEW
      record (``_TASK_LINE`` accepts any indent), as an out-of-namespace
      record report, or as a machine directive — a stored body line may look
      like any of these, and it must not become one.
    """
    return bool(
        line
        and line == line.strip()
        and not line.startswith(BODY_ESCAPE_PREFIX)
        and VERTICAL_WHITESPACE.search(line) is None
        and _TASK_LINE.match(line) is None
        and _RECORD_SHAPED.match(line) is None
        and _MACHINE_DIRECTIVE.match(line) is None
    )


def encode_body_line(line: str) -> str:
    """The escaped form of a body line: a backslash and a JSON string.

    ``ensure_ascii`` stays on: it is what makes the result single-line and
    whitespace-fenced — every control character, every ``str.splitlines``
    boundary and every non-ASCII character becomes a ``\\uXXXX`` escape, and
    the surrounding quotes hold the value's own leading/trailing spaces.
    """
    return BODY_ESCAPE_PREFIX + json.dumps(line)


def decode_body_line(line: str) -> str:
    """The inverse, on an ALREADY-stripped continuation line.

    A line that merely starts with a backslash is not necessarily ours — an
    authored file may hold one — so a payload that is not a JSON string comes
    back as itself, which is exactly the behaviour that predates the escape.
    """
    if not line.startswith(BODY_ESCAPE_PREFIX):
        return line
    try:
        decoded = json.loads(line[len(BODY_ESCAPE_PREFIX) :])
    except ValueError:
        return line
    return decoded if isinstance(decoded, str) else line


def encode_machine_directive(values: dict[str, Any]) -> str:
    """The machine directive line for ``values`` (keys sorted, so the whole
    projection is byte-deterministic for a given store state)."""
    unknown = sorted(set(values) - set(MACHINE_FIELDS))
    if unknown:
        raise ValueError(f"unknown machine field(s): {unknown}")
    return (
        MACHINE_DIRECTIVE_PREFIX
        + json.dumps(values, sort_keys=True)
        + MACHINE_DIRECTIVE_SUFFIX
    )


def decode_machine_directive(payload: str) -> tuple[dict[str, Any], str | None]:
    """``(values, error)`` for a directive payload — refusals are data here
    too. Every field is checked against its stored vocabulary before it is
    allowed to override anything: a directive is machine input, and machine
    input that does not normalize is reported, never guessed at."""
    try:
        values = json.loads(payload)
    except ValueError as exc:
        return {}, f"machine directive is not JSON: {exc.args[0] if exc.args else exc}"
    if not isinstance(values, dict):
        return {}, f"machine directive is not an object: {type(values).__name__}"
    for key, value in values.items():
        if key not in MACHINE_FIELDS:
            return {}, f"unknown machine field: {key!r}"
        if key == "kind" and value not in KINDS:
            return {}, f"kind outside vocabulary: {value!r}"
        if key == "repo" and not (value is None or isinstance(value, str)):
            return {}, f"repo does not normalize: {value!r}"
        if key == "title" and not isinstance(value, str):
            return {}, f"title does not normalize: {value!r}"
    return values, None


def _strip_bold(text: str) -> str:
    text = text.strip()
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        return text[2:-2].strip()
    return text


_REPO_HINT = re.compile(r"Target repo[^`]*`([^`]+)`")

# Repo inference from the task-id family, so the whole backlog is machine-
# routable without a per-record hint. An explicit `Target repo` hint always
# wins (below); this is the fallback. A family whose work is NOT a code change
# in one of our repos (ops/infra/planning/commercial/product-spec) maps to
# None on purpose — mis-routing a non-code task into a repo is worse than
# leaving it unrouted and visible in the planner report.
_REPO_BY_FAMILY: dict[str, str | None] = {
    # AIOS core and the platform/dispatch work that lives there
    "PLAT": "aios", "AIOS": "aios", "SEC": "aios", "ARCH": "aios",
    # AI Command Center product + server lane
    "AICC": "ai-command-center", "BE": "ai-command-center",
    "MIN": "ai-command-center", "UX": "ai-command-center",
    "AGT": "ai-command-center", "IOS": "ai-command-center",
    "BACKLOG": "ai-command-center", "APP": "ai-command-center",
    # VOYN Logistics CRM delivery lane
    "CRM": "voyn-logistics-crm",
    # Non-code families: no repo, reported not dispatched
    "OPS": None, "CI": None, "INFRA": None, "COMMON": None, "PLAN": None,
    "COM": None, "STAGE": None, "G": None, "EXT": None, "AI": None,
    "AML": None, "AICOS": None,
}

# The F* wave-0 foundation tasks were split across repos by owner decision;
# encoded explicitly rather than by prefix.
_REPO_BY_ID: dict[str, str] = {
    "VOYN-W0-F2": "aios", "VOYN-W0-F3": "ai-command-center",
    "VOYN-W0-F4": "aios", "VOYN-W0-F5": "ai-command-center",
}


def _infer_repo(task_id: str) -> str | None:
    if task_id in _REPO_BY_ID:
        return _REPO_BY_ID[task_id]
    # VOYN-<wave?>-<FAMILY>-... — take the family token after the optional wave.
    parts = task_id.split("-")
    for token in parts[1:]:
        if token in _REPO_BY_FAMILY:
            return _REPO_BY_FAMILY[token]
    return None


def parse_backlog(text: str) -> ParseReport:
    report = ParseReport()
    seen: dict[str, int] = {}
    lines = text.splitlines()
    current: ParsedTask | None = None
    current_indent = 0
    body_extra: list[str] = []
    machine: dict[str, Any] = {}

    def flush() -> None:
        nonlocal current, body_extra, machine
        if current is None:
            machine = {}
            return
        body = current.body
        if body_extra:
            body = (body + "\n" if body else "") + "\n".join(body_extra)
        # An explicit machine directive is the authority; the hint and the
        # family inference below are the FALLBACK for an authored file that
        # has no directive. Note the membership test rather than a truthiness
        # or None test: `repo: null` in a directive is a stored value (this
        # task routes nowhere), and it has to beat inference, or a projected
        # record would acquire a repo on the way back in.
        if "repo" in machine:
            repo = machine["repo"]
        else:
            repo = current.repo
            if repo is None:
                hint = _REPO_HINT.search(body)
                if hint:
                    repo = hint.group(1).strip()
            if repo is None:
                repo = _infer_repo(current.task_id)
        report.tasks.append(
            ParsedTask(
                task_id=current.task_id,
                wave=current.wave,
                priority=current.priority,
                status=current.status,
                kind=machine.get("kind", current.kind),
                title=machine.get("title", current.title),
                body=body,
                repo=repo,
                line_no=current.line_no,
            )
        )
        current = None
        body_extra = []
        machine = {}

    for line_no, line in enumerate(lines, start=1):
        match = _TASK_LINE.match(line)
        if match is None:
            stripped = line.strip()
            shaped = _RECORD_SHAPED.match(line)
            if shaped is not None:
                flush()
                report.unparsed.append(
                    (
                        line_no,
                        f"id outside the VOYN namespace: {shaped.group(1)!r}",
                        stripped[:160],
                    )
                )
                continue
            directive = _MACHINE_DIRECTIVE.match(stripped)
            if directive is not None:
                # Machine metadata for the record above: applied, never
                # carried into the body. A directive that does not normalize
                # — or one with no record to belong to — is reported like any
                # other unreadable line instead of being silently skipped.
                if current is None:
                    report.unparsed.append(
                        (line_no, "machine directive outside a record", stripped[:160])
                    )
                    continue
                values, error = decode_machine_directive(directive.group(1))
                if error is not None:
                    report.unparsed.append((line_no, error, stripped[:160]))
                else:
                    machine.update(values)
                continue
            # Continuation prose under the current record keeps its evidence
            # (acceptance bullets, target repo, notes) in the body.
            if (
                current is not None
                and stripped
                and (
                    len(line) - len(line.lstrip()) > current_indent
                    or not line.lstrip().startswith("- **")
                )
            ):
                if len(line) - len(line.lstrip()) > current_indent:
                    body_extra.append(decode_body_line(stripped))
                elif not stripped.startswith("#") and not stripped.startswith("- "):
                    body_extra.append(decode_body_line(stripped))
                else:
                    flush()
            elif current is not None and stripped.startswith("#"):
                flush()
            continue

        flush()
        indent, task_id, rest = match.group(1), match.group(2), match.group(3)
        excerpt = line.strip()[:160]

        if not _ID_SHAPE.match(task_id):
            report.unparsed.append(
                (line_no, f"id does not normalize: {task_id!r}", excerpt)
            )
            continue
        if task_id in seen:
            report.unparsed.append(
                (line_no, f"duplicate id (first at line {seen[task_id]})", excerpt)
            )
            continue

        fields = [part.strip() for part in rest.split(" | ")]
        if len(fields) < 2:
            report.unparsed.append((line_no, "fewer than two fields after id", excerpt))
            continue

        wave_match = _WAVE.match(_strip_bold(fields[0]))
        if wave_match is None:
            report.unparsed.append(
                (line_no, f"wave does not normalize: {fields[0]!r}", excerpt)
            )
            continue
        wave = wave_match.group(1) or wave_match.group(2)

        status_field = _strip_bold(fields[1])
        # An annotated status — "IN_PROGRESS (slice 1 DONE)" — normalizes to
        # its exact leading token; the annotation is prose and goes to body.
        annotation_match = re.match(r"^([A-Z_]+)\s*(\(.*\))$", status_field)
        status_note = None
        if annotation_match is not None and annotation_match.group(1) in STATUSES:
            status_field, status_note = (
                annotation_match.group(1),
                annotation_match.group(2),
            )
        status = status_field
        if status not in STATUSES:
            report.unparsed.append(
                (line_no, f"status outside vocabulary: {fields[1]!r}", excerpt)
            )
            continue

        remainder = fields[2:]
        priority: str | None = None
        if remainder:
            priority_match = _PRIORITY.match(_strip_bold(remainder[0]))
            if priority_match is not None:
                priority = f"P{priority_match.group(1)}"
                remainder = remainder[1:]

        title: str | None = None
        prose: list[str] = []
        for part in remainder:
            slug = _SLUG.match(part)
            if slug is not None and title is None:
                title = slug.group(1)
            else:
                prose.append(part)
        if title is None:
            title = task_id  # a record without a slug is still a record

        seen[task_id] = line_no
        body_head = " | ".join(prose)
        if status_note:
            body_head = f"[status note: {status_note}]" + (
                " " + body_head if body_head else ""
            )
        current = ParsedTask(
            task_id=task_id,
            wave=wave,
            priority=priority,
            status=status,
            kind="gate" if _GATE_ID.search(task_id) else "task",
            title=title,
            body=body_head,
            repo=None,
            line_no=line_no,
        )
        current_indent = len(indent)

    flush()
    return report
