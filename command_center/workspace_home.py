"""Workspace Home read model — cross-project rollup with sensitivity redaction.

See `WORKSPACE_HOME_ARCHITECTURE.md` for the full design. Plain Python, no Streamlit
import anywhere in this module — `app.py`'s Workspace Home page is a thin renderer
over `build_workspace_home_snapshot`'s returned dict; no business logic beyond
`st.*` calls lives there.

**Deviation from §4's data-source map, recorded here rather than silently applied:**
the architecture document's Projects row lists `load_tasks()` (the v1.2 Kanban
board, `data/tasks.json`) as a data source for the per-project task count. That
function lives only in `app.py` and is out of scope for the Condition-4 extraction
this document itself defines (§9.1 names exactly three functions, not `load_tasks`).
Since §6 *unconditionally* prohibits this module from importing `app.py` — a hard
constraint repeated three times in the document (§1 item 4, §6, §9.2) — this module
uses `ExecutionCenterAPI.list_tasks(project=...)` (v2 SQLite task table, an
explicitly allowed read method per §6) for the Projects section's task count
instead. This underscores v2 orchestration tasks, not v1.2 Kanban cards; the
Kanban board itself is unaffected and remains reachable via its own existing page.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from command_center import (
    activity_log,
    agent_runner,
    git_info,
    models,
    project_config,
    report_parser,
)
from command_center import artifacts as artifacts_module
from command_center.runtime import db as runtime_db
from command_center.runtime.api import ExecutionCenterAPI

ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = ROOT / "generated"
REPORTS_DIR = ROOT / "reports"


def _runtime_sibling_dir(*, explicit_env: str, name: str, fallback: Path) -> Path:
    """Resolve an operational workspace directory for source and frozen apps.

    A packaged desktop bundle has its own immutable resource root, while the
    live runtime database, generated tasks, and reports remain together in the
    user's operational workspace. ``AICC_DATA_DIR`` already identifies that
    workspace's ``data/`` directory; its siblings are therefore the safest
    default unless a dedicated override is supplied.
    """
    explicit = os.environ.get(explicit_env)
    if explicit:
        return Path(explicit).expanduser()
    data_dir = os.environ.get("AICC_DATA_DIR")
    if data_dir:
        data_path = Path(data_dir).expanduser()
        # Production uses the conventional ``<workspace>/data`` shape, while
        # isolated callers may point AICC_DATA_DIR at a standalone directory
        # that directly owns all test/runtime material.
        workspace_root = data_path.parent if data_path.name == "data" else data_path
        return workspace_root / name
    return fallback


def resolve_generated_dir() -> Path:
    return _runtime_sibling_dir(
        explicit_env="AICC_GENERATED_ROOT",
        name="generated",
        fallback=GENERATED_DIR,
    )


def resolve_reports_dir() -> Path:
    return _runtime_sibling_dir(
        explicit_env="AICC_REPORTS_ROOT",
        name="reports",
        fallback=REPORTS_DIR,
    )

# --------------------------------------------------------------------------
# §5.1 — field allowlists for sensitive (BANK/LEGAL) projects. Every field not
# listed here is dropped for a sensitive project's entries, never merely hidden
# by the renderer — see the module docstring and WORKSPACE_HOME_ARCHITECTURE.md §13.
# --------------------------------------------------------------------------

_RUN_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "source",
        "project",
        "task_type",
        "state",
        "status",
        "created_at",
        "started_at",
        "completed_at",
        "exit_code",
        "duration_seconds",
        # `failure_reason` is allowed because the Supervisor constrains it, at the
        # single call site that ever sets it (`runtime/supervisor.py:_supervise`),
        # to exactly `None` or the literal string `"timeout"` — never raw exception
        # text or any other free-form detail. If that constraint ever changes, this
        # field must be re-reviewed before staying in the allowlist.
        "failure_reason",
    }
)

_REPORT_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"run_id", "source", "project", "verdict", "severity_counts", "created_at"}
)

_ARTIFACT_ALLOWED_FIELDS: frozenset[str] = frozenset({"project", "task_type", "created_at", "nav_target"})

_ACTIVITY_ALLOWED_FIELDS: frozenset[str] = frozenset({"project", "event_type", "ts", "run_id", "task_id"})


def _allow(entry: dict, allowed: frozenset[str]) -> dict:
    return {key: value for key, value in entry.items() if key in allowed}


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _artifact_entry(path: Path, project_id: str, generated_dir: Path) -> dict:
    return {
        "project": artifacts_module.project_from_path(path, generated_dir) or project_id,
        "task_type": artifacts_module.infer_task_type_from_filename(path),
        "created_at": _mtime(path),
        "nav_target": {"project": project_id, "section": "generated"},
        "path": path,
    }


def sanitize_workspace_project_entry(
    project_id: str,
    *,
    runs: list[dict],
    reports: list[dict],
    artifacts: list[Path],
    activity: list[dict],
    generated_dir: Path | None = None,
) -> dict:
    """Returns `{"runs": [...], "reports": [...], "artifacts": [...], "activity": [...]}`
    with every entry passed through the field allowlist for this project. For a
    non-sensitive project, every field of every entry passes through unchanged —
    this is the *single* place Workspace Home's redaction policy lives, applied
    uniformly rather than as an if-sensitive branch scattered per section.
    """
    artifact_root = generated_dir or resolve_generated_dir()
    artifact_entries = [
        _artifact_entry(path, project_id, artifact_root) for path in artifacts
    ]

    if not project_config.is_sensitive(project_id):
        return {
            "runs": [dict(entry) for entry in runs],
            "reports": [dict(entry) for entry in reports],
            "artifacts": artifact_entries,
            "activity": [dict(entry) for entry in activity],
        }

    return {
        "runs": [_allow(entry, _RUN_ALLOWED_FIELDS) for entry in runs],
        "reports": [_allow(entry, _REPORT_ALLOWED_FIELDS) for entry in reports],
        "artifacts": [_allow(entry, _ARTIFACT_ALLOWED_FIELDS) for entry in artifact_entries],
        "activity": [_allow(entry, _ACTIVITY_ALLOWED_FIELDS) for entry in activity],
    }


# --------------------------------------------------------------------------
# §7 — per-project git/worktree discovery
# --------------------------------------------------------------------------


def _discover_worktrees(repository_path: str | None) -> tuple[str, list[dict]]:
    if not repository_path:
        return "unconfigured", []
    valid, _ = project_config.validate_repository_path(repository_path)
    if not valid:
        return "invalid_path", []
    cwd = Path(repository_path).expanduser()
    status = git_info.get_status(cwd)
    if not status.get("is_repo"):
        return "not_git_repo", []
    return "ok", git_info.get_worktrees(cwd)


# --------------------------------------------------------------------------
# §8 — run identity/source tagging. Both v1.2 and v2 generate ids via the
# identical `models.new_id()`, so `(source, run_id)` — never bare `run_id` — is
# the only safe merged-run identity (§8/F5).
# --------------------------------------------------------------------------


def _duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
    except (ValueError, TypeError):
        return None
    # Old runtime rows are naive UTC while newer rows carry an explicit offset.
    # Normalise both so a mixed operational database remains renderable.
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=UTC)
    return (completed - started).total_seconds()


def _tag_v1_run(run: dict) -> dict:
    tagged = dict(run)
    tagged["run_id"] = run.get("id")
    tagged["source"] = "v1.2"
    return tagged


def _tag_v2_run(run: dict) -> dict:
    tagged = dict(run)
    tagged["run_id"] = run.get("id")
    tagged["source"] = "v2"
    tagged.setdefault("duration_seconds", _duration_seconds(run.get("started_at"), run.get("completed_at")))
    return tagged


def _normalize_activity_entry(entry: dict) -> dict:
    normalized = dict(entry)
    normalized["event_type"] = entry.get("type")
    return normalized


# --------------------------------------------------------------------------
# §9 — Reports: file-primary, joined against the v1.2/v2 run that produced it,
# for a verdict/severity badge. A report file with no matching run still
# appears, just without a verdict.
# --------------------------------------------------------------------------


def _v1_report_entry(run: dict, path: Path) -> dict:
    corrected = report_parser.apply_manual_corrections(run.get("parsed") or {})
    return {
        "run_id": run.get("id"),
        "source": "v1.2",
        "project": run.get("project"),
        "verdict": corrected.get("verdict"),
        "severity_counts": report_parser.severity_counts(run.get("parsed")),
        "created_at": _mtime(path),
        "report_path": path,
    }


def _v2_report_entry(run: dict, path: Path, report_text: str) -> dict:
    parsed = report_parser.parse_report(report_text)
    return {
        "run_id": run.get("id"),
        "source": "v2",
        "project": run.get("project"),
        "verdict": parsed.get("verdict"),
        "severity_counts": report_parser.severity_counts(parsed),
        "created_at": _mtime(path),
        "report_path": path,
    }


def _unmatched_report_entry(path: Path, project_id: str) -> dict:
    return {
        "run_id": None,
        "source": None,
        "project": project_id,
        "verdict": None,
        "severity_counts": {severity: 0 for severity in models.SEVERITIES},
        "created_at": _mtime(path),
        "report_path": path,
    }


def _build_reports_for_project(
    *,
    project_id: str,
    report_files: list[Path],
    v1_report_index: dict[str, dict],
    v2_terminal_runs_for_project: list[dict],
    execution_center_api: ExecutionCenterAPI,
    reports_dir: Path | None = None,
) -> list[dict]:
    report_root = reports_dir or resolve_reports_dir()
    v2_report_index: dict[str, dict] = {}
    for run in v2_terminal_runs_for_project:
        report_row = execution_center_api.get_report(run["id"])
        if report_row:
            v2_report_index[report_row["path"]] = run

    entries: list[dict] = []
    for path in report_files:
        rel_str = f"reports/{path.relative_to(report_root)}"
        v1_run = v1_report_index.get(rel_str)
        if v1_run is not None:
            entries.append(_v1_report_entry(v1_run, path))
            continue
        v2_run = v2_report_index.get(rel_str)
        if v2_run is not None:
            entries.append(_v2_report_entry(v2_run, path, artifacts_module.read_text(path)))
            continue
        entries.append(_unmatched_report_entry(path, project_id))
    return entries


# --------------------------------------------------------------------------
# §10 — Recent Activity: real activity_log events folded with display-only
# rows derived from the already-fetched, already-redacted Recent Runs list
# (v2 lifecycle events are never written to activity.jsonl — see §10's gap).
# --------------------------------------------------------------------------


def _derive_activity_from_v2_runs(runs: list[dict]) -> list[dict]:
    derived: list[dict] = []
    for run in runs:
        if run.get("source") != "v2":
            continue
        state = run.get("state")
        if state and run.get("completed_at"):
            event_type = "run_completed" if state == "COMPLETED" else "run_failed"
            ts = run["completed_at"]
        elif run.get("started_at"):
            event_type = "run_started"
            ts = run["started_at"]
        else:
            continue
        derived.append(
            {
                "project": run.get("project"),
                "event_type": event_type,
                "ts": ts,
                "run_id": run.get("run_id"),
                "task_id": run.get("task_id"),
            }
        )
    return derived


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def build_workspace_home_snapshot(
    *,
    execution_center_api: ExecutionCenterAPI,
    active_runs_limit: int = 20,
    recent_runs_limit: int = 20,
    activity_limit: int = 20,
    artifacts_limit: int = 20,
    reports_limit: int = 20,
) -> dict:
    configs = project_config.load_project_configs()
    generated_dir = resolve_generated_dir()
    reports_dir = resolve_reports_dir()

    v2_active_raw = execution_center_api.list_runs(
        states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES, limit=active_runs_limit
    )
    v2_recent_raw = execution_center_api.list_runs(states=runtime_db.TERMINAL_STATES, limit=recent_runs_limit)
    v1_runs_raw = agent_runner.load_runs()
    activity_raw = activity_log.load_activity(limit=max(activity_limit * len(models.PROJECT_IDS), activity_limit))
    all_generated_files = artifacts_module.list_markdown_files(generated_dir)
    all_report_files = artifacts_module.list_markdown_files(reports_dir)
    v1_report_index = {run["report_path"]: run for run in v1_runs_raw if run.get("report_path")}

    projects_out: list[dict] = []
    worktrees_by_project: dict[str, dict] = {}
    active_runs_all: list[dict] = []
    recent_runs_all: list[dict] = []
    reports_all: list[dict] = []
    artifacts_all: list[dict] = []
    activity_all: list[dict] = []

    for project_id in models.PROJECT_IDS:
        cfg = configs[project_id]
        repository_path = cfg.get("repository_path")

        repo_state, worktrees = _discover_worktrees(repository_path)
        worktrees_by_project[project_id] = {"state": repo_state, "worktrees": worktrees}

        proj_v2_active = [_tag_v2_run(r) for r in v2_active_raw if r.get("project") == project_id]
        proj_v2_recent = [_tag_v2_run(r) for r in v2_recent_raw if r.get("project") == project_id]
        proj_v1_recent = [_tag_v1_run(r) for r in v1_runs_raw if r.get("project") == project_id]

        proj_report_files = [
            path
            for path in all_report_files
            if artifacts_module.project_from_path(path, reports_dir) == project_id
        ]
        proj_reports = _build_reports_for_project(
            project_id=project_id,
            report_files=proj_report_files,
            v1_report_index=v1_report_index,
            v2_terminal_runs_for_project=[r for r in v2_recent_raw if r.get("project") == project_id],
            execution_center_api=execution_center_api,
            reports_dir=reports_dir,
        )

        proj_artifact_paths = [
            path
            for path in all_generated_files
            if artifacts_module.project_from_path(path, generated_dir) == project_id
        ]

        proj_activity = [_normalize_activity_entry(e) for e in activity_raw if e.get("project") == project_id]

        sanitized = sanitize_workspace_project_entry(
            project_id,
            runs=proj_v2_active + proj_v2_recent + proj_v1_recent,
            reports=proj_reports,
            artifacts=proj_artifact_paths,
            activity=proj_activity,
            generated_dir=generated_dir,
        )

        sanitized_active = [r for r in sanitized["runs"] if r.get("state") in runtime_db.EXECUTION_CENTER_ACTIVE_STATES]
        sanitized_recent = [r for r in sanitized["runs"] if r.get("state") not in runtime_db.EXECUTION_CENTER_ACTIVE_STATES]

        active_runs_all.extend(sanitized_active)
        recent_runs_all.extend(sanitized_recent)
        reports_all.extend(sanitized["reports"])
        artifacts_all.extend(sanitized["artifacts"])
        activity_all.extend(sanitized["activity"])

        task_count = len(execution_center_api.list_tasks(project=project_id))
        projects_out.append(
            {
                "id": project_id,
                "display_name": cfg.get("display_name", project_id),
                "sensitive": project_config.is_sensitive(project_id),
                "repository_path": repository_path,
                "repository_state": repo_state,
                "task_count": task_count,
                "active_run_count": len(proj_v2_active),
            }
        )

    active_runs_all.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    recent_runs_all.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    reports_all.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    artifacts_all.sort(key=lambda a: a.get("created_at") or 0, reverse=True)

    derived_activity = _derive_activity_from_v2_runs(recent_runs_all[:recent_runs_limit])
    merged_activity = activity_all + derived_activity
    merged_activity.sort(key=lambda a: a.get("ts") or "", reverse=True)

    return {
        "projects": projects_out,
        "worktrees_by_project": worktrees_by_project,
        "active_runs": active_runs_all[:active_runs_limit],
        "recent_runs": recent_runs_all[:recent_runs_limit],
        "recent_activity": merged_activity[:activity_limit],
        "artifacts": artifacts_all[:artifacts_limit],
        "reports": reports_all[:reports_limit],
    }
