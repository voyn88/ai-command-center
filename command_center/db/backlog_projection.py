"""Atomic filesystem write for the Markdown backlog projection (BO-S4).

Kept out of `backlog_parser.py` (pure, no I/O by design — see its module
docstring) and out of `backlog_store.py`'s database-only write surface: this
is the one filesystem act BO-S4 needs, isolated so it is testable without a
database and reusable by anything that writes the projection (the CLI, a
future scheduled export tick).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["atomic_write_text"]


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Replace the file at `path` with `text` without any concurrent reader
    (the importer, an operator's `cat`) ever observing a truncated or empty
    document. `Path.write_text` truncates the target in place first, which a
    reader can catch mid-write; this instead writes a sibling temp file,
    flushes and fsyncs it, then `os.replace`s it over the target — a single
    atomic rename, so every reader sees either the old file or the new one,
    never a partial one."""
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
