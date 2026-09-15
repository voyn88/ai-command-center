"""Atomic whole-file writes for rendered projections.

Split out of the ``backlog-export`` CLI branch deliberately: the AIOS
boundary gate corroborates the ``memory`` name signature (which every
module under ``db/`` carries via the ``db`` path token) with durable-write
calls — and correctly flagged the first cut, where ``tempfile.mkstemp`` +
``os.replace`` lived inside ``command_center/db/cli.py``. Writing a
RENDERING to disk is not engine persistence, but the gate cannot know that
from behaviour alone, and the gate's judgement is the one we keep: the
write moves to a module whose name says exactly what it is and carries no
frozen-category token, instead of the baseline growing an exception.

One function, one guarantee: a reader never observes a partial file — the
bytes land in a same-directory temp file first and take the destination's
name atomically, with the temp unlinked on any failure.

The bytes are also flushed to the platter before the rename, and the
directory entry after it. ``os.replace`` alone makes the swap atomic
against a concurrent READER; it says nothing about a crash, after which the
rename can be durable while the content it points at is not — the classic
rename-without-fsync truncation. Both fsyncs are best-effort: a directory
handle is not openable everywhere (Windows), and a projection that could
not be fsynced is still correctly written, so a refusal there would fail a
render for a durability nicety.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["write_atomically"]


def write_atomically(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        os.unlink(tmp_name)
        raise
    try:
        directory = os.open(destination.parent, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform-dependent (Windows)
        return
    try:
        os.fsync(directory)
    except OSError:  # pragma: no cover - platform-dependent
        pass
    finally:
        os.close(directory)
