"""Artifact/report discovery — filesystem helpers with no Streamlit dependency.

Extracted verbatim (zero behavior change) from `app.py`'s original module-level
functions (`app.py:201-223`), which lived there only because nothing outside
`app.py` needed them until Workspace Home did — see `WORKSPACE_HOME_ARCHITECTURE.md`
§9/§9.1/§9.2 for the extraction rationale. This module is a leaf: it imports nothing
from `command_center.workspace_home`, `command_center.runtime.*`, or `app.py`, and it
never imports Streamlit. `app.py` and `command_center.workspace_home` both import
from here; the reverse must never happen.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def format_mtime(path: Path) -> str:
    """Human mtime for artifact listings; '—' when the file cannot be stat'd.

    Moved here from `app.py` (NIGHT-W9 slice 5) beside `read_text`/
    `list_markdown_files` — same Streamlit-free file-helper family (see
    WORKSPACE_HOME_ARCHITECTURE.md §9)."""
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")


def read_text(path: Path) -> str:
    if not path.exists():
        return "Файл пока не создан."
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Ошибка чтения файла: {exc}"
    return content if content.strip() else "Файл пока пуст."


def list_markdown_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        (path for path in directory.rglob("*.md") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def project_from_path(path: Path, base: Path) -> str:
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return "—"
    return parts[0] if len(parts) > 1 else "—"


# Canonical recognized task types, in display/selection order. This is the single
# source of truth for the app's task-type set — `app.py` imports this instead of
# defining its own duplicate list, so the two can never drift apart.
TASK_TYPES: tuple[str, ...] = (
    "implementation",
    "review",
    "remediation",
    "final_gate",
    "architecture_review",
)


def infer_task_type_from_filename(path: Path) -> str | None:
    parts = path.stem.split("_", 1)
    if len(parts) == 2 and parts[1] in TASK_TYPES:
        return parts[1]
    return None
