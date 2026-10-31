"""``command_center.projection_writer.write_atomically`` (VOYN-W0-BACKLOG-ORCHESTRATOR
BO-S4) had zero test coverage anywhere in the suite even though it is the one
function every projection writer (today: ``backlog-export``) relies on for the
"a reader never observes a partial file" guarantee described in its own
docstring and in ``aicc-backlog-export.timer``'s comments. These tests pin
that guarantee directly, independent of the backlog domain — both halves of
it: no partial file for a concurrent reader (same-directory temp plus
``os.replace``, and no debris when either fails), and no partial file for a
reader after a crash (``fsync`` before the rename, which the atomicity of
``os.replace`` alone does not give)."""

from __future__ import annotations

import os

import pytest

from command_center import projection_writer


def test_writes_the_destination_and_leaves_no_temp_file_behind(tmp_path):
    destination = tmp_path / "out.md"
    projection_writer.write_atomically(destination, "hello\n")
    assert destination.read_text(encoding="utf-8") == "hello\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_overwrites_existing_content_wholesale(tmp_path):
    destination = tmp_path / "out.md"
    destination.write_text("stale projection\nwith stale lines\n", encoding="utf-8")
    projection_writer.write_atomically(destination, "fresh\n")
    assert destination.read_text(encoding="utf-8") == "fresh\n"


def test_creates_missing_parent_directories(tmp_path):
    destination = tmp_path / "nested" / "deeper" / "out.md"
    projection_writer.write_atomically(destination, "hello\n")
    assert destination.read_text(encoding="utf-8") == "hello\n"


def test_failure_leaves_destination_untouched_and_cleans_up_the_temp_file(
    tmp_path, monkeypatch
):
    destination = tmp_path / "out.md"
    destination.write_text("original\n", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(projection_writer.os, "replace", _boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        projection_writer.write_atomically(destination, "new\n")

    assert destination.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_temp_file_shares_the_destination_directory(tmp_path, monkeypatch):
    """The atomicity guarantee (``os.replace``) only holds when the temp file
    and the destination are on the same filesystem — pin that ``mkstemp`` is
    given the destination's own parent, not the platform default temp dir."""
    destination = tmp_path / "out.md"
    seen_dirs = []
    real_mkstemp = projection_writer.tempfile.mkstemp

    def _spy(*args, **kwargs):
        seen_dirs.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(projection_writer.tempfile, "mkstemp", _spy)
    projection_writer.write_atomically(destination, "hello\n")
    assert seen_dirs == [destination.parent]


def test_the_temp_file_is_fsynced_before_it_takes_the_destinations_name(
    tmp_path, monkeypatch
):
    """`os.replace` is atomic for a concurrent reader but says nothing about
    durability: if the rename reaches disk while the bytes are still only in
    page cache, a crash leaves the destination truncated or empty.

    That is not merely a lost tick here. A truncated projection loses its
    header, and a file with no `render_generated_stamp` line is
    indistinguishable from a hand-authored backlog — so the console drops
    back to `mtime`, which the crashed write just set to now, and presents an
    empty backlog as freshly rendered. The ordering below is what keeps that
    shut, and ordering is the whole property: an fsync *after* the rename
    would prove nothing, so this pins the sequence, not just the call."""
    destination = tmp_path / "out.md"
    calls = []
    real_fsync, real_replace = projection_writer.os.fsync, projection_writer.os.replace

    def _fsync(fd):
        # Recorded by size so the assertion below proves the *content* was
        # flushed, not merely that some descriptor was synced.
        calls.append(("fsync", os.fstat(fd).st_size))
        return real_fsync(fd)

    def _replace(src, dst):
        calls.append(("replace", os.stat(src).st_size))
        return real_replace(src, dst)

    monkeypatch.setattr(projection_writer.os, "fsync", _fsync)
    monkeypatch.setattr(projection_writer.os, "replace", _replace)

    payload = "projection line\n" * 100
    projection_writer.write_atomically(destination, payload)

    size = len(payload.encode("utf-8"))
    assert calls == [("fsync", size), ("replace", size)]
    assert destination.read_text(encoding="utf-8") == payload
