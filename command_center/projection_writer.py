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

One function, two guarantees, both about what a reader can ever see:

- **No partial file.** The bytes land in a same-directory temp file first
  and take the destination's name atomically, with the temp unlinked on any
  failure. A concurrent reader observes either the whole previous
  projection or the whole new one.
- **No partial file after a crash either.** The temp file is ``fsync``-ed
  before it is renamed. ``os.replace`` is atomic with respect to concurrent
  readers, but that says nothing about durability: without the flush, a
  power loss or host crash can persist the rename while the data blocks are
  still only in page cache, and the destination comes back truncated or
  zero-length. That failure is worse here than a missing tick, because a
  truncated projection loses its header — and a projection with no
  ``render_generated_stamp`` line is indistinguishable from a hand-authored
  backlog, so the console falls back to ``mtime``, which the crashed write
  just set to now, and reports an empty backlog as freshly rendered. Silent
  staleness through the freshness indicator is the exact failure BO-S4
  exists to end, so the write pays one fsync per tick to keep it shut.

The destination directory is deliberately *not* fsync-ed. That would make
the rename itself durable; skipping it means a crash can leave the previous
projection in place instead of the new one — which is a whole, correct,
self-describing file carrying its own older stamp, exactly what a reader
should see when the last tick did not survive, and the next tick five
minutes later overwrites it anyway. Only the truncation case above is worth
paying for.
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
            # Both halves are required and neither implies the other:
            # `flush` moves the bytes out of Python's buffer into the
            # kernel, `fsync` moves them out of the kernel onto the disk.
            # Inside the `with`, so a failure of either is still an
            # exception that reaches the cleanup below with the
            # destination untouched.
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        os.unlink(tmp_name)
        raise
