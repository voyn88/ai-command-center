"""The projection's atomic write (BO-S4).

Hermetic — no database. `Path.write_text` truncates the target in place
before writing the new content, so a reader racing the write (the scheduled
importer, an operator's `cat`) can observe an empty or partial document
(review finding on PR #431). `atomic_write_text` must instead never mutate
the target path except via a single `os.replace`.
"""

from __future__ import annotations

import os

from command_center.db.backlog_projection import atomic_write_text


def test_write_replaces_the_target_via_rename_not_in_place_truncation(tmp_path):
    target = tmp_path / "VOYN_TASKS_BACKLOG.md"
    target.write_text("stale content", encoding="utf-8")
    before_inode = target.stat().st_ino

    atomic_write_text(target, "fresh content")

    assert target.read_text(encoding="utf-8") == "fresh content"
    # A different inode proves the file was replaced by a rename, not
    # truncated and rewritten through the original file handle in place —
    # the property that keeps a concurrent reader from ever observing a
    # half-written document.
    assert target.stat().st_ino != before_inode


def test_write_creates_a_new_file_and_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "new_projection.md"

    atomic_write_text(target, "content")

    assert target.read_text(encoding="utf-8") == "content"
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


def test_a_failed_write_leaves_the_original_file_untouched(tmp_path, monkeypatch):
    target = tmp_path / "VOYN_TASKS_BACKLOG.md"
    target.write_text("original content", encoding="utf-8")

    real_fsync = os.fsync

    def _boom(fd):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "fsync", _boom)
    try:
        try:
            atomic_write_text(target, "new content")
        except OSError:
            pass
        else:
            raise AssertionError("expected the simulated fsync failure to propagate")
    finally:
        monkeypatch.setattr(os, "fsync", real_fsync)

    assert target.read_text(encoding="utf-8") == "original content"
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == [], f"temp file(s) left behind after failure: {leftovers}"
