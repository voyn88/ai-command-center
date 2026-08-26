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

Pure module: no database, no I/O beyond the text it is given — so its tests
are hermetic and the store's tests need only prove the seam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["ParsedTask", "ParseReport", "parse_backlog"]

EXECUTABLE_STATUSES = ("OPEN", "IN_PROGRESS", "READY_TO_REVIEW", "DONE")
NON_EXECUTABLE_STATUSES = (
    "UNTRIAGED",
    "DEFER_TO_USER",
    "SPLIT",
    "NEEDS_REFINEMENT",
    "DECIDED",
)
STATUSES = frozenset(EXECUTABLE_STATUSES + NON_EXECUTABLE_STATUSES)

_TASK_LINE = re.compile(r"^(\s*)- \*\*(VOYN-[A-Za-z0-9._-]+)\*\* \| (.+)$")
#: A line SHAPED like a record whose id is outside the VOYN namespace. Not a
#: task — but not silently droppable either: it is either a typo in a real
#: record or a foreign record, and both belong in the report.
_RECORD_SHAPED = re.compile(r"^\s*- \*\*([^*]+)\*\* \| ")
#: Numeric waves ("Wave N") plus the file's closed set of named lanes and
#: idea pools, exactly as observed: W1/W7 are FUTURE-wave idea pools and
#: deliberately distinct from waves 1/7 (the W0-vs-W00 distinctness rule);
#: P1/P0.5 are lane names of the idea sections, not priorities.
_WAVE = re.compile(r"^Wave ([0-9]+(?:\.[0-9]+)?)$|^(COM|WOW|AICOS|W1|W7|P1|P0\.5)$")
_PRIORITY = re.compile(r"^P([0-9])(?:\s*\(.*\))?$", re.S)
_SLUG = re.compile(r"^`([^`]+)`$")
_GATE_ID = re.compile(r"-G[0-9]+$")
_ID_SHAPE = re.compile(r"^VOYN-[A-Za-z0-9][A-Za-z0-9._-]*$")


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

    def flush() -> None:
        nonlocal current, body_extra
        if current is None:
            return
        body = current.body
        if body_extra:
            body = (body + "\n" if body else "") + "\n".join(body_extra)
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
                kind=current.kind,
                title=current.title,
                body=body,
                repo=repo,
                line_no=current.line_no,
            )
        )
        current = None
        body_extra = []

    for line_no, line in enumerate(lines, start=1):
        match = _TASK_LINE.match(line)
        if match is None:
            shaped = _RECORD_SHAPED.match(line)
            if shaped is not None:
                flush()
                report.unparsed.append(
                    (
                        line_no,
                        f"id outside the VOYN namespace: {shaped.group(1)!r}",
                        line.strip()[:160],
                    )
                )
                continue
            # Continuation prose under the current record keeps its evidence
            # (acceptance bullets, target repo, notes) in the body.
            if (
                current is not None
                and line.strip()
                and (
                    len(line) - len(line.lstrip()) > current_indent
                    or not line.lstrip().startswith("- **")
                )
            ):
                if len(line) - len(line.lstrip()) > current_indent:
                    body_extra.append(line.strip())
                elif not line.strip().startswith("#") and not line.strip().startswith(
                    "- "
                ):
                    body_extra.append(line.strip())
                else:
                    flush()
            elif current is not None and line.strip().startswith("#"):
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
