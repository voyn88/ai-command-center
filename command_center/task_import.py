"""Task package import: parse -> validate -> preview -> apply, for task
packages produced outside the app (e.g. by an AI-run Founder audit) — see
`command_center.tasks_repository`'s own module docstring for why this must
never become a second task store. Every write in this module goes through
`tasks_repository.load_tasks`/`save_tasks`, the same store the Kanban board,
Create Task page, and every other task-mutating code path already use.

Supported input formats (all four converge on the *same* `ParsedPackage`, so
validation/preview/apply are format-agnostic — see `parse_task_package`):

- **JSON** (`.json`): a bare list of task objects, or an envelope
  `{"schema_version", "package_id", "tasks": [...]}`.
- **YAML** (`.yaml`/`.yml`): the exact same two shapes, parsed with
  `yaml.safe_load` (never `yaml.load` — untrusted input must never construct
  arbitrary Python objects). Requires PyYAML (declared in `requirements.txt`);
  a missing dependency surfaces as an ordinary `TaskImportError`, never an
  `ImportError` traceback.
- **Markdown** (`.md`/`.markdown`): a human-authored document that *carries*
  the structured task list, either as a leading YAML front-matter block
  (`---` … `---`) or as the first fenced code block (```json / ```yaml /
  ```yml / untagged). The extracted block is parsed as JSON or YAML and must
  itself be one of the two shapes above.
- **Plain text** (`.txt`, or any unknown/absent extension): sniffed — parsed
  as JSON first, then YAML, so a `.txt` holding either structured form Just
  Works. This is also the path a caller that passes no `filename` takes, which
  is why a bare JSON string still parses exactly as it did before multi-format
  support existed (full backward compatibility).

Zero Streamlit coupling: `app.py`'s Create Task page and `scripts/import_tasks.py`
are both thin callers of the four functions below.

Pipeline
--------
`parse_task_package`  — bytes/str (+ optional `filename`/`fmt` hint) ->
    `ParsedPackage` (raises `TaskImportError` on malformed input in any format
    or an unrecognized root shape). Never touches disk.
`validate_task_package` — `ParsedPackage` -> `ValidationResult`: per-task field
    and vocabulary checks, project-name normalization. Never touches disk.
`build_import_preview` — `(root, parsed, validation)` -> `ImportPreview`: reads
    the current store once (read-only) to classify each valid task as new or
    an id-duplicate. Never writes.
`apply_task_package` — `(root, parsed, validation)` -> `ImportResult`:
    re-derives duplicates and dependencies against its own fresh read of the
    store, then persists one `get_repository(root).create()` call per new
    task. Each `create()` holds `tasks_repository.tasks_lock` — the *same*
    shared lock (never a second, import-specific one) every other write path
    holds for its own load-mutate-save cycle (task creation, status change,
    deletion, manual launch-status toggles — see `tasks_repository`'s module
    docstring) — so no *individual* write can lose an update against a
    concurrent Kanban edit.

    **The import as a whole is not one transaction.** N new tasks means N
    separate lock cycles, and the up-front duplicate/dependency read is taken
    *without* the lock. Another writer can therefore interleave between two
    tasks of the same package, and a failure part-way through leaves the
    tasks already created in the store — `TaskImportError.imported_ids` says
    which. This is the deliberate trade-off that lets the AIOS backend
    (which has no batch-write primitive) share this code path; it is a
    narrowing of the original single-`mutate_tasks` behavior, and
    `docs/audits/TASK_IMPORT_LOCK_REVIEW.md` records the reasoning and what
    it costs.

    Never trusts a caller-held `ImportPreview` — a store mutated between
    preview and apply, by another process or a concurrent writer of any
    kind, is still seen fresh. Refuses to run at all (raises
    `TaskImportError`) if `validation.errors` is non-empty — a package with
    any structural/field error is rejected in full, before any write.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from command_center import artifacts, models, project_config, storage, tasks_repository

SCHEMA_VERSION = "1"

# Hard upper bound on the raw bytes `parse_task_package` will even attempt to
# decode/parse. A task package is a hand/AI-authored manifest of a handful to a
# few hundred tasks; anything past this ceiling is a mistake (a wrong file, a
# runaway generator, a hostile upload) and is rejected as a plain
# `TaskImportError` *before* any JSON/YAML parser is handed the blob — never an
# unbounded parse of an arbitrarily large input. 5 MiB is many times the
# largest real Founder-audit package and still trivially cheap to parse.
# Callers that genuinely need a different ceiling pass `max_bytes` explicitly.
MAX_PACKAGE_BYTES = 5 * 1024 * 1024

# Extension -> canonical format name. Anything not listed (including no
# extension at all) falls through to the "text" sniffer, which tries JSON then
# YAML — so a bare JSON string with no filename parses exactly as it always
# has (backward compatibility for every pre-multi-format caller).
_FORMAT_BY_SUFFIX: dict[str, str] = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
}

# File extensions the UI/CLI advertise as accepted, derived from the mapping
# above so the two never drift apart.
SUPPORTED_IMPORT_SUFFIXES: tuple[str, ...] = tuple(sorted(s.lstrip(".") for s in _FORMAT_BY_SUFFIX))

# `apply_task_package`'s lock timeout — the *lock itself* is
# `tasks_repository.tasks_lock` (shared with every other task-store write
# path), not a dedicated one for imports. Kept as a distinct name here only
# because import batches may legitimately want a longer timeout than a
# single interactive edit; forwarded as the `timeout` of each per-task
# `create()` call, so it bounds every individual lock wait, not the import
# as a whole (an N-task package can wait up to N * this in the worst case).
IMPORT_LOCK_TIMEOUT_SECONDS = tasks_repository.TASKS_LOCK_TIMEOUT_SECONDS

# Literal field list from the import spec: every task in a package must carry
# all of these (an empty string/None counts as missing). `depends_on` is
# special-cased below since `[]` is a valid, non-missing value that is also
# falsy.
REQUIRED_TASK_FIELDS: tuple[str, ...] = (
    "id",
    "status",
    "priority",
    "task_type",
    "depends_on",
    "repository_path",
    "workspace_path",
    "branch",
    "project",
)

# Fields consumed explicitly by `_build_task_record` below; everything else on
# an imported task dict is preserved verbatim under `record["metadata"]`
# rather than silently dropped (see module docstring / point 9 of the import
# spec: package-specific provenance like `source`/`target_version`/
# `parallel_group` must survive the import, even though the task model has no
# dedicated field for it).
_KNOWN_TASK_FIELDS: frozenset[str] = frozenset(
    {"id", "title", "goal", "prompt", "status", "priority", "task_type", "project",
     "depends_on", "repository_path", "workspace_path", "branch"}
)


class TaskImportError(Exception):
    """Raised for input the caller cannot recover from: malformed JSON, an
    unrecognized package root shape, or calling `apply_task_package` on a
    `ValidationResult` that still has blocking errors.

    `imported_ids` carries the ids `apply_task_package` had already
    committed when it failed part-way through its per-task create loop. It
    is empty for every failure raised before the first write — which is
    every other raise site in this module. Because the store is *not*
    rolled back (see `apply_task_package`'s "Partial import" note), a
    caller that needs to report or clean up the real post-failure state can
    read this instead of diffing the store itself.
    """

    def __init__(self, *args: object, imported_ids: tuple[str, ...] = ()) -> None:
        super().__init__(*args)
        self.imported_ids = imported_ids


def _partial_import_note(imported: list[dict]) -> str:
    """Human-readable tail for an import that failed part-way through the
    create loop. Tasks already committed are *not* rolled back, so the
    message says so explicitly rather than leaving the operator to assume a
    clean abort and re-run the package blind."""
    if not imported:
        return "Ни одна задача не была записана."
    ids = ", ".join(str(task.get("id")) for task in imported)
    return f"Уже записано задач: {len(imported)} ({ids}) — они остаются в хранилище."


@dataclass(frozen=True)
class ImportIssue:
    task_ref: str | None
    message: str


@dataclass(frozen=True)
class ParsedPackage:
    package_id: str
    schema_version: str
    tasks: list[dict]
    package_hash: str


@dataclass(frozen=True)
class ValidationResult:
    # Each item is the raw task dict with `project` replaced by its
    # canonical `models.PROJECT_IDS` form — every other field is untouched
    # package input, not yet a task record.
    items: list[dict]
    errors: list[ImportIssue] = field(default_factory=list)
    warnings: list[ImportIssue] = field(default_factory=list)


@dataclass(frozen=True)
class PackageTaskRow:
    id: str
    title: str
    project: str
    status: str
    priority: str
    task_type: str
    outcome: str  # "new" | "duplicate" | "invalid"


@dataclass(frozen=True)
class ImportPreview:
    package_id: str
    schema_version: str
    package_hash: str
    total_tasks: int
    new_items: list[dict]
    duplicate_ids: list[str]
    errors: list[ImportIssue]
    warnings: list[ImportIssue]
    rows: list[PackageTaskRow]

    @property
    def has_blocking_errors(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True)
class ImportResult:
    package_id: str
    schema_version: str
    imported_ids: list[str]
    skipped_duplicate_ids: list[str]
    warnings: list[ImportIssue]


def _resolve_format(filename: str | None, fmt: str | None) -> str:
    """Pick the parser: an explicit `fmt` wins; otherwise the file extension;
    otherwise `"text"` (the JSON-then-YAML sniffer). Case-insensitive on both
    the format name and the extension."""
    if fmt is not None:
        normalized = fmt.strip().lower()
        if normalized not in {"json", "yaml", "markdown", "text"}:
            raise TaskImportError(f"Неизвестный формат импорта: {fmt!r}")
        return normalized
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in _FORMAT_BY_SUFFIX:
            return _FORMAT_BY_SUFFIX[suffix]
    return "text"


def _load_yaml(text: str):
    """`yaml.safe_load` with PyYAML imported lazily. A missing dependency or a
    YAML syntax error is reported as a `TaskImportError`, never an
    `ImportError`/`yaml.YAMLError` traceback — the caller (UI/CLI) treats every
    parse failure uniformly. `safe_load` (not `load`) is mandatory: an imported
    package is untrusted input and must never be able to construct arbitrary
    Python objects."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise TaskImportError(
            "Для импорта YAML требуется пакет PyYAML (pip install pyyaml); установите зависимости из requirements.txt."
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaskImportError(f"Файл не является корректным YAML: {exc}") from exc


def _load_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskImportError(f"Файл не является корректным JSON: {exc}") from exc


# A leading YAML front-matter block: `---\n ... \n---` at the very start of the
# document (a trailing `\n` after the closing fence is optional). Group 1 is
# the block body.
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# The first fenced code block; group 1 is the info string (e.g. "json"), group
# 2 the body.
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)


def _markdown_payload(text: str):
    """Extract the structured task list a Markdown document carries. Two
    conventions, tried in order:

    1. A leading YAML **front-matter** block (`---` … `---`) — parsed as YAML.
    2. The first **fenced code block**: a ```json fence is parsed as JSON; a
       ```yaml/```yml or untagged fence is parsed as YAML.

    Prose outside these carriers is ignored (it's the human-readable report the
    block is embedded in). A document with neither carrier is a hard error —
    Markdown import is deliberately structured, never a guess at free text."""
    front = _FRONT_MATTER_RE.search(text)
    if front:
        return _load_yaml(front.group(1))

    fence = _FENCE_RE.search(text)
    if fence:
        info = fence.group(1).lower()
        body = fence.group(2)
        return _load_json(body) if info == "json" else _load_yaml(body)

    raise TaskImportError(
        "Markdown-файл не содержит задач: добавьте YAML front-matter (--- … ---) "
        "или блок кода ```json/```yaml со списком задач."
    )


def _package_from_data(data, package_hash: str) -> ParsedPackage:
    """The single place the two accepted root shapes — a bare list of tasks or
    an `{..., "tasks": [...]}` envelope — become a `ParsedPackage`, shared by
    every format so JSON/YAML/Markdown/text behave identically once parsed."""
    if isinstance(data, list):
        return ParsedPackage(
            package_id=f"legacy-{package_hash}",
            schema_version="legacy",
            tasks=data,
            package_hash=package_hash,
        )

    if isinstance(data, dict):
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise TaskImportError('Пакет-конверт должен содержать поле "tasks" со списком задач.')
        return ParsedPackage(
            package_id=str(data.get("package_id") or f"package-{package_hash}"),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            tasks=tasks,
            package_hash=package_hash,
        )

    raise TaskImportError("Корневой элемент пакета должен быть списком задач либо объектом-конвертом.")


def parse_task_package(
    raw: str | bytes,
    *,
    filename: str | None = None,
    fmt: str | None = None,
    max_bytes: int = MAX_PACKAGE_BYTES,
) -> ParsedPackage:
    """Parse a task package in any supported format into a `ParsedPackage`.

    The format is chosen by (in priority order) an explicit `fmt`, the
    `filename`'s extension, or — with neither — content sniffing (JSON first,
    then YAML). Both accepted root shapes — a bare list of tasks (legacy: what
    every Founder audit package produced so far looks like) and an envelope
    `{"schema_version", "package_id", "tasks": [...]}` — are valid in every
    format; a legacy list gets an auto-generated `package_id` derived from the
    package's content hash, so it never requires manual conversion.

    `package_hash` is the SHA-256 of the *raw bytes as received*, independent
    of format, so two uploads of byte-identical content always dedupe to the
    same hash/`package_id` regardless of how they were parsed.

    Passing neither `filename` nor `fmt` (the pre-multi-format call signature)
    still parses a bare JSON string exactly as before — full backward
    compatibility.

    An input larger than `max_bytes` (default `MAX_PACKAGE_BYTES`) is rejected
    up front with a `TaskImportError`, before any parser sees it — an oversized
    or hostile blob can never trigger an unbounded parse.
    """
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(raw_bytes) > max_bytes:
        raise TaskImportError(
            f"Файл слишком большой для импорта: {len(raw_bytes)} байт "
            f"(максимум {max_bytes} байт). Разбейте пакет на несколько файлов."
        )

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    package_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    resolved = _resolve_format(filename, fmt)
    if resolved == "json":
        data = _load_json(text)
    elif resolved == "yaml":
        data = _load_yaml(text)
    elif resolved == "markdown":
        data = _markdown_payload(text)
    else:  # "text": sniff JSON, then YAML
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = _load_yaml(text)

    return _package_from_data(data, package_hash)


def validate_task_package(parsed: ParsedPackage) -> ValidationResult:
    """Read-only structural/vocabulary validation plus project-name
    normalization (via `project_config.normalize_project_id`). Never reads or
    writes the task store — dedup against existing tasks happens later, in
    `build_import_preview`/`apply_task_package`, each against its own fresh
    read.
    """
    errors: list[ImportIssue] = []
    warnings: list[ImportIssue] = []
    items: list[dict] = []

    if not parsed.tasks:
        return ValidationResult(items=[], errors=[ImportIssue(None, "Пакет не содержит задач.")])

    seen_ids: set[str] = set()

    for index, raw_item in enumerate(parsed.tasks):
        label = raw_item.get("id") if isinstance(raw_item, dict) else None
        ref = label or f"#{index + 1}"

        if not isinstance(raw_item, dict):
            errors.append(ImportIssue(ref, "Элемент пакета не является объектом задачи."))
            continue

        missing = [
            f for f in REQUIRED_TASK_FIELDS
            if not (f == "depends_on" and isinstance(raw_item.get("depends_on"), list))
            and raw_item.get(f) in (None, "")
        ]
        if missing:
            errors.append(ImportIssue(ref, f"Отсутствуют обязательные поля: {', '.join(missing)}"))
            continue

        task_id = raw_item["id"]
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append(ImportIssue(ref, "Поле id должно быть непустой строкой."))
            continue

        if task_id in seen_ids:
            errors.append(ImportIssue(task_id, f"Дублирующийся id внутри пакета: {task_id}"))
            continue
        seen_ids.add(task_id)

        if not (raw_item.get("title") or raw_item.get("goal")):
            errors.append(ImportIssue(task_id, "Необходимо указать хотя бы одно из полей: title, goal."))
            continue

        status = raw_item["status"]
        if status not in models.KANBAN_STATUSES:
            errors.append(ImportIssue(task_id, f"Недопустимый статус: {status!r}"))
            continue

        priority = raw_item["priority"]
        if priority not in models.TASK_PRIORITIES:
            errors.append(ImportIssue(task_id, f"Недопустимый приоритет: {priority!r}"))
            continue

        task_type = raw_item["task_type"]
        if task_type not in artifacts.TASK_TYPES:
            errors.append(ImportIssue(task_id, f"Недопустимый тип задачи: {task_type!r}"))
            continue

        depends_on = raw_item["depends_on"]
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            errors.append(ImportIssue(task_id, "Поле depends_on должно быть списком строковых id."))
            continue

        canonical_project = project_config.normalize_project_id(raw_item["project"])
        if canonical_project is None:
            errors.append(ImportIssue(task_id, f"Неизвестный проект: {raw_item['project']!r}"))
            continue

        # Dependency *existence* is deliberately not checked here: this
        # function never reads the store, so a dependency absent from the
        # package alone is not yet known to be genuinely unmet — it may
        # already exist in `tasks.json` from a prior import. That check (the
        # only one that can give an accurate answer) happens once, in
        # `build_import_preview`/`apply_task_package`, each against its own
        # fresh combined view of package ids ∪ store ids.
        item = dict(raw_item)
        item["project"] = canonical_project
        items.append(item)

    return ValidationResult(items=items, errors=errors, warnings=warnings)


def _row_for(item: dict, outcome: str) -> PackageTaskRow:
    return PackageTaskRow(
        id=item["id"],
        title=item.get("title") or item.get("goal") or "",
        project=item["project"],
        status=item["status"],
        priority=item["priority"],
        task_type=item["task_type"],
        outcome=outcome,
    )


def _find_unresolved_dependencies(new_items: list[dict], resolvable_ids: set[str]) -> list[ImportIssue]:
    """Dependency resolution is checked only for `new_items` — the tasks an
    import call is actually about to write — never for a task that is
    already a duplicate (already in the store): that task's own dependency
    correctness was already accepted whenever *it* was first imported, and
    re-litigating it now would let a stale/broken dependency on one
    already-imported task block an unrelated new task in the same package.
    `resolvable_ids` is the caller's full universe a dependency may point
    into — package ids ∪ store ids ("dependency внутри пакета; dependency в
    актуальном store" — both allowed, nothing else)."""
    issues: list[ImportIssue] = []
    for item in new_items:
        for dep_id in item.get("depends_on") or []:
            if dep_id not in resolvable_ids:
                issues.append(ImportIssue(item["id"], f"Зависимость не найдена ни в пакете, ни в хранилище: {dep_id}"))
    return issues


def build_import_preview(
    root: Path,
    parsed: ParsedPackage,
    validation: ValidationResult,
    *,
    allow_unresolved_dependencies: bool = False,
) -> ImportPreview:
    """Read-only. Cross-references `validation.items` against a fresh read of
    the store to classify each valid task as new or an id-duplicate, and
    checks every new task's `depends_on` against the combined universe of
    package ids ∪ store ids.

    By default (`allow_unresolved_dependencies=False`, matching
    `apply_task_package`) an unresolved dependency is a **blocking error**:
    it is added to `errors` (so `has_blocking_errors` is `True` and the
    caller must refuse to offer "Import"), not merely a warning — an
    unknown/typo'd dependency id must never be allowed to import silently.
    Passing `allow_unresolved_dependencies=True` is the explicit opt-in that
    downgrades the same finding to a `warnings` entry instead, for the rare
    case an operator knowingly wants to import a task ahead of a dependency
    that will arrive in a later package.

    Nothing here is written to `tasks.json` — the caller (UI/CLI) must call
    `apply_task_package` separately, after explicit user confirmation.
    """
    existing_ids = {t["id"] for t in tasks_repository.get_repository(root).load_all() if t.get("id")}
    package_ids = {item["id"] for item in validation.items}

    new_items: list[dict] = []
    duplicate_ids: list[str] = []
    rows: list[PackageTaskRow] = []

    for item in validation.items:
        is_duplicate = item["id"] in existing_ids
        if is_duplicate:
            duplicate_ids.append(item["id"])
        else:
            new_items.append(item)
        rows.append(_row_for(item, "duplicate" if is_duplicate else "new"))

    for issue in validation.errors:
        rows.append(PackageTaskRow(
            id=issue.task_ref or "—", title="", project="", status="", priority="", task_type="", outcome="invalid",
        ))

    dependency_issues = _find_unresolved_dependencies(new_items, existing_ids | package_ids)
    errors = list(validation.errors)
    warnings = list(validation.warnings)
    if allow_unresolved_dependencies:
        warnings.extend(dependency_issues)
    else:
        errors.extend(dependency_issues)

    return ImportPreview(
        package_id=parsed.package_id,
        schema_version=parsed.schema_version,
        package_hash=parsed.package_hash,
        total_tasks=len(parsed.tasks),
        new_items=new_items,
        duplicate_ids=duplicate_ids,
        errors=errors,
        warnings=warnings,
        rows=rows,
    )


def _build_task_record(item: dict, parsed: ParsedPackage, now: str) -> dict:
    title = item.get("title") or models.derive_short_title(item.get("goal") or "") or item["id"]
    record = tasks_repository.new_task_record(
        item["project"],
        title,
        item["task_type"],
        item["status"],
        goal=item.get("goal"),
        priority=item["priority"],
        depends_on=list(item.get("depends_on") or []),
        workspace_path=item.get("workspace_path"),
        branch=item.get("branch"),
        prompt=item.get("prompt"),
    )
    # `new_task_record` always mints its own id; an imported task keeps the
    # id assigned by the package (e.g. "AICC-AUDIT-001") instead — that id is
    # this task's identity for dedup on every future import of the same or an
    # overlapping package.
    record["id"] = item["id"]
    # `repository_path` isn't one of `new_task_record`'s kwargs (it's a
    # workflow field defaulted to `None`); set it explicitly from the package.
    record["repository_path"] = item.get("repository_path")

    extras = {k: v for k, v in item.items() if k not in _KNOWN_TASK_FIELDS}
    extras["package_id"] = parsed.package_id
    extras["schema_version"] = parsed.schema_version
    extras["imported_at"] = now
    record["metadata"] = extras
    # App-controlled provenance marker: this task entered from an external task
    # package (untrusted input), so it must launch with reduced capabilities by
    # default unless an operator explicitly elevates it (audit D7 / SEC-1). Set
    # by the app, never read from package content — an attacker cannot clear it.
    record["untrusted_import"] = True
    return record


def apply_task_package(
    root: Path,
    parsed: ParsedPackage,
    validation: ValidationResult,
    *,
    lock_timeout: float = IMPORT_LOCK_TIMEOUT_SECONDS,
    allow_unresolved_dependencies: bool = False,
) -> ImportResult:
    """Load the store, re-derive duplicates and unmet dependencies against
    that fresh read, build every new task's full record, then persist each
    new task with a separate `get_repository(root).create()` call. Each
    `create()` acquires and releases the tasks-store lock independently, so
    concurrent writers may interleave between individual task creates — this
    is the accepted trade-off that enables the AIOS backend (which has no
    batch-write primitive). The dependency and duplicate checks are still
    computed against a single up-front `load_all()` snapshot, so the
    pre-flight validation is consistent within one import run.

    Never trusts any `ImportPreview` the caller may be holding — duplicates
    and dependency constraints are recomputed from the fresh `load_all()` read.

    By default (`allow_unresolved_dependencies=False`) a new task whose
    `depends_on` points outside both the package and the current store —
    the fresh read, not whatever `build_import_preview` last saw —
    is a **blocking error**: the whole package is rejected and nothing is
    written, same as a package with blocking `validation.errors`. Pass
    `allow_unresolved_dependencies=True` to explicitly opt into the old,
    permissive behavior (unresolved dependency downgraded to a warning,
    import proceeds) — never the default, so a typo'd dependency id can
    never silently import a task that will stay blocked forever.

    Every new task's full record is built via `tasks_repository.
    new_task_record` (so it gets the same `created_at`/`updated_at`/
    timeline/workflow/execution defaults every other task gets). One
    `create()` call is made per new task; N tasks → N separate lock
    cycles and N writes. A package that already has blocking
    `validation.errors` is refused outright before any write is attempted.
    Re-importing already-imported ids is a no-op for those ids
    (`skipped_duplicate_ids`), not a second copy.

    **Partial import:** the create loop is not rolled back. If it fails
    after at least one successful `create()` but before all tasks are
    written — either `storage.LockTimeoutError`, or the store refusing a
    record because a concurrent writer claimed that id in the window since
    the `load_all()` above — the tasks already persisted stay in the store.
    Both cases are raised as `TaskImportError` (never a raw `ValueError`,
    which would escape the CLI/UI handlers as an uncaught traceback), and
    the exception's `imported_ids` lists exactly what was committed, so the
    caller can report real state without diffing the store.

    Raises `TaskImportError` if a lock cannot be acquired within
    `lock_timeout` seconds (default 30s) — surfaced to the UI/CLI as an
    ordinary import failure, never an indefinite hang.
    """
    if validation.errors:
        raise TaskImportError("Невозможно применить пакет: обнаружены ошибки валидации.")

    skipped: list[str] = []
    warnings = list(validation.warnings)
    imported: list[dict] = []

    try:
        _repo = tasks_repository.get_repository(root)
        existing_ids = {t["id"] for t in _repo.load_all() if t.get("id")}
        package_ids = {item["id"] for item in validation.items}

        new_items = [item for item in validation.items if item["id"] not in existing_ids]
        skipped[:] = [item["id"] for item in validation.items if item["id"] in existing_ids]

        dependency_issues = _find_unresolved_dependencies(new_items, existing_ids | package_ids)
        if dependency_issues:
            if not allow_unresolved_dependencies:
                details = "; ".join(f"{issue.task_ref}: {issue.message}" for issue in dependency_issues)
                raise TaskImportError(f"Невозможно применить пакет: не разрешены зависимости — {details}")
            warnings.extend(dependency_issues)

        now = models.iso_now()
        new_records = [_build_task_record(item, parsed, now) for item in new_items]
        for task_dict in new_records:
            try:
                imported.append(_repo.create(task_dict, timeout=lock_timeout))
            except ValueError as exc:
                # The store refused the record under its own lock — in
                # practice a concurrent writer that claimed this id in the
                # window between our unlocked `load_all()` above and this
                # `create()`. That is an ordinary, recoverable import
                # failure, so it must reach the CLI/UI as `TaskImportError`
                # like every other one; letting the raw `ValueError` escape
                # would surface as an uncaught traceback instead.
                raise TaskImportError(
                    f"импорт прерван на задаче {task_dict.get('id')!r}: хранилище отклонило запись "
                    f"({exc}). {_partial_import_note(imported)}",
                    imported_ids=tuple(str(t.get("id")) for t in imported),
                ) from exc
    except storage.LockTimeoutError as exc:
        raise TaskImportError(
            f"не удалось получить блокировку импорта задач за {lock_timeout:.0f}с: "
            f"{tasks_repository.tasks_lock_path(root)}. {_partial_import_note(imported)}",
            imported_ids=tuple(str(t.get("id")) for t in imported),
        ) from exc

    return ImportResult(
        package_id=parsed.package_id,
        schema_version=parsed.schema_version,
        imported_ids=[t["id"] for t in imported],
        skipped_duplicate_ids=skipped,
        warnings=warnings,
    )
