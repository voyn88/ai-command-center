"""``command_center.projection_writer.write_atomically`` (VOYN-W0-BACKLOG-ORCHESTRATOR
BO-S4) had zero test coverage anywhere in the suite even though it is the one
function every projection writer (today: ``backlog-export``) relies on for the
"a reader never observes a partial file" guarantee described in its own
docstring and in ``aicc-backlog-export.timer``'s comments. These tests pin
that guarantee directly, independent of the backlog domain."""

from __future__ import annotations

from pathlib import Path

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
