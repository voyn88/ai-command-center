"""Read-only adapter: the voyn-backlog (``voyn-task``) as a source of ``backlog_task``.

Stage 3 of the voyn-backlog integration (PLAN.md section 13). Task
*definitions* are YAML files (``tasks/*.yaml``, one task per file); task
*state* is the PostgreSQL schema ``voyn_coordination`` (``task_state``). This
module reads both and produces the same ``ParsedTask`` records the Markdown
parser produces, so the existing ``BacklogStore`` upsert path is the only
writer of ``backlog_task`` -- there is no second write path.

Boundaries, all enforced here and by tests:

* **Off by default.** ``AICC_VOYN_TASK_SOURCE`` must be exactly ``1``, ``true``,
  ``yes`` or ``on``. Anything else, including unset, means off. While off,
  nothing in this module opens a connection, reads a directory or spawns a
  process.
* **Read only.** Only ``SELECT`` on ``task_state`` under the reader role
  (``voyn_task_reader``). No ``claim`` / ``transition`` / ``register`` call
  exists in this module. ``ops/aicc_backlog_publish.py`` is untouched; the
  Markdown path stays authoritative until the reconciliation week is over.
* **No secrets in argv or output.** The DSN comes from the environment only
  (``VOYN_TASK_READER_DSN``, else ``VOYN_TASK_DSN``, the same names the
  ``voyn-task`` CLI uses). A password inside a URL DSN is moved to
  ``PGPASSWORD`` for the child process; errors never echo the DSN.

Status mapping (voyn-task -> ``backlog_task.status``)::

    OPEN             -> OPEN
    IN_PROGRESS      -> IN_PROGRESS
    READY_TO_REVIEW  -> READY_TO_REVIEW
    DONE             -> DONE
    BLOCKED          -> NEEDS_REFINEMENT

``BLOCKED`` does not exist in the ``backlog_task`` vocabulary (contract
divergence 1). ``NEEDS_REFINEMENT`` is chosen because it is non-executable,
has no outgoing transitions and, unlike ``DEFER_TO_USER`` (auto-resume,
migration 0014), cannot be resumed behind the owner's back; the original
status is kept in the body as ``[voyn-task status: BLOCKED]``. This is a
proposal pending the owner's decision.

``UNTRIAGED``, ``DEFER_TO_USER``, ``SPLIT`` and ``DECIDED`` are AICC-side
triage states that voyn-task has no notion of; this adapter never produces
them. A task registered in AICC with one of those statuses and present in
voyn-task is reported by the reconciliation as a difference, not rewritten
silently.

A task with a YAML definition but no row in ``task_state`` (stage 2, the state
transfer, not done yet) takes its status from the YAML ``status`` field and is
listed in ``SourceReport.unregistered``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from command_center.db.backlog_parser import ParsedTask, ParseReport

__all__ = [
    "FLAG_ENV",
    "STATUS_MAP",
    "SourceError",
    "SourceReport",
    "build_report",
    "fetch_states_psql",
    "is_enabled",
    "load_definitions",
    "load_from_voyn_task",
]

FLAG_ENV = "AICC_VOYN_TASK_SOURCE"
TASKS_DIR_ENV = "VOYN_TASKS_DIR"
_TRUE = frozenset({"1", "true", "yes", "on"})

STATUS_MAP: dict[str, str] = {
    "OPEN": "OPEN",
    "IN_PROGRESS": "IN_PROGRESS",
    "READY_TO_REVIEW": "READY_TO_REVIEW",
    "DONE": "DONE",
    "BLOCKED": "NEEDS_REFINEMENT",
}

#: voyn-task ``project`` -> AICC ``repo`` (the value the Markdown path infers
#: from the id family or a ``Target repo`` hint). Unknown projects map to None:
#: unrouted and visible, never guessed.
REPO_BY_PROJECT: dict[str, str | None] = {
    "aicc": "ai-command-center",
    "crm": "voyn-logistics-crm",
    "aios": "aios",
}

_WAVE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$|^(?:COM|WOW|AICOS|W1|W7|P1|P0\.5)$")
_PRIORITY = re.compile(r"^P[0-9]$")
_ID_SHAPE = re.compile(r"^VOYN-[A-Za-z0-9][A-Za-z0-9._-]*$")
_GATE_ID = re.compile(r"-G[0-9]+$")
_EXAMPLE_FILE = "EXAMPLE.yaml"


class SourceError(RuntimeError):
    """The source could not be read. Messages never contain a DSN or password."""


@dataclass(slots=True)
class SourceReport(ParseReport):
    #: Task ids with a definition but no ``task_state`` row (status from YAML).
    unregistered: list[str] = field(default_factory=list)
    #: Ids that have a ``task_state`` row but no YAML definition (ignored).
    orphan_states: list[str] = field(default_factory=list)
    #: task_id -> revision from the database, for reconciliation output.
    revisions: dict[str, int] = field(default_factory=dict)


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(FLAG_ENV, "").strip().lower() in _TRUE


# ------------------------------------------------------------------ definitions

def load_definitions(tasks_dir: Path) -> dict[str, dict[str, Any]]:
    """``tasks/*.yaml`` -> id -> definition. ``EXAMPLE.yaml`` is skipped."""
    import yaml  # PyYAML is a base requirement

    if not tasks_dir.is_dir():
        raise SourceError(f"tasks directory not found: {tasks_dir.name}")
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(tasks_dir.glob("*.yaml")):
        if path.name == _EXAMPLE_FILE:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from exc
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            out.setdefault(data["id"], data)
    return out


# ------------------------------------------------------------------------ state

def _psql_target(dsn: str) -> tuple[str, dict[str, str]]:
    """Return (argv-safe DSN, extra env). A URL password goes to PGPASSWORD."""
    env: dict[str, str] = {}
    target = dsn
    if dsn.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(dsn)
        if parts.password:
            env["PGPASSWORD"] = unquote(parts.password)
            host = parts.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            user = f"{parts.username}@" if parts.username else ""
            port = f":{parts.port}" if parts.port else ""
            target = parts._replace(netloc=f"{user}{host}{port}").geturl()
    return target, env


def fetch_states_psql(env: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """SELECT task_state under the reader role through ``psql``. Read only."""
    source = os.environ if env is None else env
    dsn = source.get("VOYN_TASK_READER_DSN") or source.get("VOYN_TASK_DSN")
    if not dsn:
        raise SourceError("neither VOYN_TASK_READER_DSN nor VOYN_TASK_DSN is set")
    target, extra = _psql_target(dsn)
    custom = source.get("VOYN_PSQL")
    import shlex

    cmd = (shlex.split(custom) if custom else ["psql"]) + [
        "-X", "-qAt", "-v", "ON_ERROR_STOP=1", target,
    ]
    sql = (
        "select coalesce(jsonb_agg(jsonb_build_object('task_id',task_id,'status',status,"
        "'revision',revision) order by task_id),'[]'::jsonb)::text "
        "from voyn_coordination.task_state;"
    )
    try:
        result = subprocess.run(
            cmd, input=sql, text=True, capture_output=True, env={**os.environ, **extra},
            timeout=60, check=False,
        )
    except FileNotFoundError as exc:
        raise SourceError("psql not found (install the PostgreSQL client or set VOYN_PSQL)") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError("psql timed out") from exc
    if result.returncode != 0:
        first = next((ln for ln in (result.stderr or "").splitlines() if ln.strip()), "error")
        raise SourceError(f"database read failed: {first[:200]}")
    try:
        rows = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise SourceError("database returned malformed JSON") from exc
    return {row["task_id"]: row for row in rows}


# ---------------------------------------------------------------------- mapping

def _body(definition: Mapping[str, Any], native_status: str) -> str:
    parts: list[str] = []
    if native_status == "BLOCKED":
        parts.append("[voyn-task status: BLOCKED]")
    why = definition.get("why")
    if isinstance(why, str) and why.strip():
        parts.append(why.strip())
    acceptance = definition.get("acceptance")
    if isinstance(acceptance, str) and acceptance.strip():
        parts.append("Acceptance: " + acceptance.strip())
    return "\n".join(parts)


def build_report(
    definitions: Mapping[str, Mapping[str, Any]],
    states: Mapping[str, Mapping[str, Any]],
) -> SourceReport:
    """Pure mapping. A definition that does not normalize is reported in
    ``unparsed`` (line_no 0), never guessed -- the Markdown parser's rule."""
    report = SourceReport()
    for task_id in sorted(definitions):
        d = definitions[task_id]

        def refuse(reason: str, _id: str = task_id) -> None:
            report.unparsed.append((0, reason, _id))

        if not _ID_SHAPE.match(task_id):
            refuse("id does not normalize")
            continue
        wave = d.get("wave")
        wave = str(wave) if isinstance(wave, (int, float)) else wave
        if not isinstance(wave, str) or not _WAVE.match(wave):
            refuse(f"wave does not normalize: {wave!r}")
            continue
        state = states.get(task_id)
        native = state["status"] if state else d.get("status")
        if native not in STATUS_MAP:
            refuse(f"status outside voyn-task vocabulary: {native!r}")
            continue
        priority = d.get("priority")
        if priority is not None and not (isinstance(priority, str) and _PRIORITY.match(priority)):
            refuse(f"priority does not normalize: {priority!r}")
            continue
        title = d.get("title") if isinstance(d.get("title"), str) and d["title"].strip() else task_id
        project = d.get("project")
        gate = d.get("gate") is True or bool(_GATE_ID.search(task_id))
        if state is None:
            report.unregistered.append(task_id)
        else:
            report.revisions[task_id] = int(state.get("revision", 0))
        report.tasks.append(
            ParsedTask(
                task_id=task_id,
                wave=wave,
                priority=priority,
                status=STATUS_MAP[native],
                kind="gate" if gate else "task",
                title=title,
                body=_body(d, native),
                repo=REPO_BY_PROJECT.get(project) if isinstance(project, str) else None,
                line_no=0,
            )
        )
    report.orphan_states = sorted(set(states) - set(definitions))
    return report


def load_from_voyn_task(
    env: Mapping[str, str] | None = None,
    *,
    fetch_states: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
) -> SourceReport:
    """Entry point. Raises ``SourceError`` when the flag is off, *before* any
    I/O: callers must check ``is_enabled`` and keep the Markdown path."""
    source = os.environ if env is None else env
    if not is_enabled(source):
        raise SourceError(f"{FLAG_ENV} is off; voyn-task is not read")
    tasks_dir = Path(source.get(TASKS_DIR_ENV) or "tasks")
    definitions = load_definitions(tasks_dir)
    states = fetch_states() if fetch_states else fetch_states_psql(source)
    return build_report(definitions, states)
