"""Renders the immutable report artifact for a run, from its `RunEvent` stream.

One report file per run (`report_path_for` embeds the run id), written once,
at a terminal state, with the full text of every assistant/result event this
run produced — never truncated (mirrors `agent_runner.save_report`'s
guarantee for v1.2). `Report` rows are 0..1 per run and are never rewritten
once created: `save_report` is only ever called once per run by the
supervisor, and re-running it against the same run id would produce a new
file (the timestamp is fixed at first call, but nothing here overwrites an
existing path — `db.create_report` has a primary key on `run_id` and will
raise on a second insert for the same run).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from command_center.models import iso_now, utc_now
from command_center.storage import atomic_write_text

ROOT = Path(__file__).resolve().parent.parent.parent
# `AICC_REPORTS_ROOT` mirrors `storage.resolve_data_dir`'s `AICC_DATA_DIR` override,
# but for `reports/` specifically: `tests/conftest.py`'s `isolated_reports_dir`
# monkeypatches this module's `REPORTS_ROOT` attribute for tests that run in-process,
# but `tests/test_execution_center_debug_cli.py` runs `scripts/execution_center_debug.py`
# as a genuinely separate OS subprocess (see that script's module docstring) — a
# monkeypatch in the parent test process cannot reach a child process's own fresh
# import of this module. Without an env-var-driven override, every subprocess-based
# CLI test wrote a real (small, `fake_claude`-produced) report into the developer's
# actual `reports/<PROJECT>/` directory on every single test run — confirmed as the
# source of ~150 leaked report files accumulated over two days of this session's
# `pytest` runs (see the runtime-integrity incident this comment responds to). The CLI
# test's subprocess env now sets `AICC_REPORTS_ROOT` so the child process resolves the
# same isolated directory the parent test's `AICC_DATA_DIR` already points at.
REPORTS_ROOT = Path(os.environ["AICC_REPORTS_ROOT"]) if os.environ.get("AICC_REPORTS_ROOT") else ROOT / "reports"


# Sanitize each user-influenced path component so an imported/hand-authored
# `project` or run `id` like "../../root" cannot walk the report *write* out of
# REPORTS_ROOT (audit MAJOR-9). Mirrors `agent_runner._safe_path_component`,
# which contained the sibling report-path builder under SEC-2; `.` and path
# separators are excluded so no component can become `.` or `..`. (A future
# refactor could centralize the two duplicated copies.)
_SAFE_PATH_COMPONENT = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _safe_path_component(value: str, fallback: str) -> str:
    cleaned = "".join(c if c in _SAFE_PATH_COMPONENT else "_" for c in value)
    return cleaned or fallback


def report_path_for(run: dict) -> Path:
    project = _safe_path_component(run.get("project") or "UNKNOWN", "UNKNOWN")
    started = run.get("started_at") or run.get("created_at") or iso_now()
    try:
        started_dt = datetime.fromisoformat(started)
    except (ValueError, TypeError):
        # Same clock as the `iso_now` string this normally parses, so a
        # fallback filename sorts alongside the real ones.
        started_dt = utc_now()
    timestamp = started_dt.strftime("%Y%m%d-%H%M%S")
    run_part = _safe_path_component(run.get("id") or "unknown", "unknown")[:12]
    return REPORTS_ROOT / project / f"{timestamp}_{run_part}.md"


def stored_report_path(path: Path) -> str:
    """Return the stable persisted reference for a report under REPORTS_ROOT.

    The historical schema stores ``reports/<project>/<file>``.  Keep that
    representation for compatibility, but derive it from the configured
    reports root instead of the application source root.
    """
    resolved = path.resolve()
    root = REPORTS_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("report path is outside the configured reports root")
    return str(Path("reports") / resolved.relative_to(root))


def resolve_report_path(report_path: str | None) -> Path | None:
    """Resolve a persisted report reference without allowing root escape.

    Canonical ``reports/...`` references are resolved from the configured
    REPORTS_ROOT, so code, data and report roots may live independently.
    Older root-relative references and absolute paths are accepted only when
    they resolve inside REPORTS_ROOT.  ``resolve()`` also closes symlink and
    traversal escapes.  Missing/invalid references return ``None``.
    """
    if not report_path or not isinstance(report_path, str):
        return None

    raw = Path(report_path)
    if raw.is_absolute():
        candidate = raw.resolve()
    elif raw.parts and raw.parts[0] == "reports":
        candidate = (REPORTS_ROOT / Path(*raw.parts[1:])).resolve()
    else:
        # Legacy rows occasionally stored a path relative to REPORTS_ROOT.
        candidate = (REPORTS_ROOT / raw).resolve()

    root = REPORTS_ROOT.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def _assistant_text_blocks(events: list[dict]) -> list[str]:
    blocks: list[str] = []
    for event in events:
        if event["event_type"] != "assistant_message":
            continue
        message = (event["payload"].get("message") or {})
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                blocks.append(block["text"])
    return blocks


def _tool_activity_lines(events: list[dict]) -> list[str]:
    lines: list[str] = []
    for event in events:
        if event["event_type"] != "assistant_message":
            continue
        message = event["payload"].get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                lines.append(f"- `{block.get('name', '?')}` {block.get('input', {})}")
    return lines


def result_text(events: list[dict]) -> str:
    """The run's final `result` event text, or a placeholder if none exists
    yet. Public — also used by `task_sync.py` to feed `report_parser.
    parse_report` the same text v1.2 parses (the agent's final message, not
    the whole rendered report markdown)."""
    for event in reversed(events):
        if event["event_type"] == "result":
            text = event["payload"].get("result")
            if text:
                return text
    return "_Не найдено в событиях запуска._"


def _stderr_text(events: list[dict]) -> str:
    lines = [event["payload"].get("line", "") for event in events if event["event_type"] == "stderr_line"]
    return "\n".join(lines)


def _malformed_count(events: list[dict]) -> int:
    return sum(1 for event in events if event["event_type"] == "malformed")


def render_report_markdown(run: dict, events: list[dict]) -> str:
    duration = None
    if run.get("started_at") and run.get("completed_at"):
        try:
            started = datetime.fromisoformat(run["started_at"])
            completed = datetime.fromisoformat(run["completed_at"])
            duration = (completed - started).total_seconds()
        except (ValueError, TypeError):
            duration = None
    duration_str = f"{duration:.1f} с" if isinstance(duration, (int, float)) else "—"

    assistant_text = "\n\n".join(_assistant_text_blocks(events)) or "_Нет текстового вывода ассистента._"
    tool_lines = _tool_activity_lines(events)
    tool_text = "\n".join(tool_lines) if tool_lines else "_Инструменты не вызывались._"
    result_text_value = result_text(events)
    stderr_text = _stderr_text(events)
    malformed = _malformed_count(events)

    return f"""# Отчёт агента (v2 Session Supervisor)

- Run ID: `{run.get('id', '—')}`
- Session ID: `{run.get('session_id', '—')}`
- Task ID: `{run.get('task_id') or '—'}`
- Project: {run.get('project', '—')}
- Task type: {run.get('task_type', '—')}
- Repository: `{run.get('repository_path', '—')}`
- Resume: {'да' if run.get('is_resume') else 'нет'} (sequence {run.get('sequence', '—')})
- Started: {run.get('started_at') or '—'}
- Completed: {run.get('completed_at') or '—'}
- Duration: {duration_str}
- Exit code: {run.get('exit_code')}
- State: {run.get('state', '—')}
- Malformed stream lines encountered: {malformed}

## Executor capabilities

- Capability profile: {run.get('capability_profile') or '—'}
- Capability override: {run.get('capability_override') or '—'}
- Required capabilities: {run.get('required_capabilities') or '—'}
- Granted capabilities: {run.get('granted_capabilities') or '—'}
- Preflight: {run.get('capability_preflight') or '—'}
- Command policy: {run.get('command_policy') or '—'}
- Failure reason: {run.get('failure_reason') or '—'}

## Prompt

```
{run.get('prompt', '')}
```

## Assistant output (полный, без сокращений)

{assistant_text}

## Инструменты (вызовы)

{tool_text}

## Итоговый результат (result)

```
{result_text_value}
```

## Stderr (полный, без сокращений)

```
{stderr_text}
```

## Git status до запуска

```
{run.get('pre_run_git_status') or '—'}
```

## Git status после запуска

```
{run.get('post_run_git_status') or '—'}
```
"""


def save_report(run: dict, events: list[dict]) -> Path:
    path = report_path_for(run)
    content = render_report_markdown(run, events)
    atomic_write_text(path, content)
    return path
