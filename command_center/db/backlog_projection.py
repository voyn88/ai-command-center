"""The Markdown projection of the structured backlog store (BO-S4).

Read direction of the two-way bridge ``backlog_store.import_markdown``
already opened: the store is canonical, the file is a read projection for
the human owner during the migration period. The fixed point this module
must hold is not "the exported text equals the original file" (formatting is
allowed to normalize) but "reconciling the exported text back into the store
changes nothing" — ``import_markdown(export_tasks(store.export_all())).changed
== 0``. Two ways to break that fixed point, both concrete:

* Collapsing a task's ``body`` onto one line. ``backlog_parser`` stores a
  continuation bullet's original newline structure (each ``\\n``-separated
  segment came from its own line under the record); rendering all of it back
  onto the task's own line changes what a reparse sees as the record's body,
  so the reconciled row differs from the one already in the store.
* Dropping ``repo``. It is a real column, but the parser only ever
  *reconstructs* it from an explicit ``Target repo`` hint in the body or by
  inference from the task-id family (see ``_infer_repo``). A stored repo
  that came from neither — set directly through the API, say — is invisible
  to the parser unless this module renders an explicit hint for it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from command_center.db.backlog_parser import _REPO_HINT, _infer_repo
from command_center.storage import atomic_write_text

__all__ = ["render_task_line", "export_tasks", "write_projection"]

_NUMERIC_WAVE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _wave_field(wave: str) -> str:
    # The parser's two wave shapes, in reverse: a numeric token gets the
    # "Wave " prefix back; a named lane (COM, W7, P1, ...) is bare.
    return f"Wave {wave}" if _NUMERIC_WAVE.match(wave) else wave


def _repo_without_a_hint(body: str, task_id: str) -> str | None:
    """What ``parse_backlog`` would resolve ``repo`` to from this exact body,
    without us adding anything — the same two-step lookup ``flush()`` does."""
    hint = _REPO_HINT.search(body)
    if hint is not None:
        return hint.group(1).strip()
    return _infer_repo(task_id)


def render_task_line(task: dict[str, Any]) -> str:
    """One task, rendered back into the record shape ``parse_backlog``
    consumes. Always at the top indent level: the parser does not store
    indentation as a task field, so there is nothing to reconstruct it from,
    and a continuation line's own indent only has to exceed the task line's
    (checked below) for the parser to fold it back into the body.
    """
    body = task["body"] or ""
    segments = body.split("\n") if body else []
    head, continuations = (segments[0], segments[1:]) if segments else ("", [])

    fields = [_wave_field(task["wave"]), task["status"]]
    if task["priority"]:
        fields.append(task["priority"])
    fields.append(f"`{task['title']}`")
    if head:
        fields.append(head)

    repo = task.get("repo")
    if repo is not None and _repo_without_a_hint(body, task["task_id"]) != repo:
        # The body alone would reparse to a different (or no) repo; add the
        # explicit hint the parser already recognizes so the reparsed value
        # matches what is actually stored, instead of being silently
        # overwritten by inference.
        continuations = [*continuations, f"Target repo: `{repo}`"]

    lines = [f"- **{task['task_id']}** | " + " | ".join(fields)]
    # Indented past the task line's own (zero) indent so `parse_backlog`
    # folds each one into the body as a continuation, verbatim content and
    # all -- it strips the line, not the newline between them.
    lines.extend(f"  {line}" for line in continuations)
    return "\n".join(lines)


def export_tasks(tasks: list[dict[str, Any]]) -> str:
    """The full Markdown projection, one task per ``render_task_line``,
    ordered by task_id for a deterministic diff across export runs."""
    ordered = sorted(tasks, key=lambda t: t["task_id"])
    rendered = "\n".join(render_task_line(t) for t in ordered)
    return f"{rendered}\n" if rendered else ""


def write_projection(path: Path, text: str) -> None:
    """Replace the projection file atomically (temp file + ``os.replace``),
    so a concurrently scheduled importer or any other reader never observes
    a truncated or half-written document -- the failure mode a plain
    ``Path.write_text`` (truncate-then-write in place) leaves open."""
    atomic_write_text(Path(path), text)
