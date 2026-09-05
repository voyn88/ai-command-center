"""Regression for the tasks.json data-loss amplification (audit P0/M2).

A transient/torn read of an existing tasks.json used to surface as an empty list
from `load_tasks`; inside `mutate_tasks` that empty list was then mutated and
saved back — overwriting the real store with just the one new record. The
read-modify-write path must instead RAISE on a bad read and persist nothing, so
the on-disk list is never destroyed by a single failed read.
"""
from __future__ import annotations

import json

import pytest

from command_center import tasks_repository as tr


def _seed(root, *titles):
    tr.save_tasks(root, [
        {"id": t.lower(), "title": t, "project": "AICC", "status": "Backlog"} for t in titles
    ])


def test_mutate_tasks_raises_and_preserves_a_corrupt_file(isolated_data_dir):
    root = isolated_data_dir
    _seed(root, "Alpha", "Beta")
    # An existing file that cannot be decoded (torn write / transient error).
    tr.tasks_file_path(root).write_text("{ this is not valid json", encoding="utf-8")
    corrupt = tr.tasks_file_path(root).read_text(encoding="utf-8")

    def _append(tasks):
        tasks.append(tr.new_task_record("AICC", "New", "implementation", "Backlog"))
        return tasks

    with pytest.raises((json.JSONDecodeError, ValueError, OSError)):
        tr.mutate_tasks(root, _append)

    # The file was NOT overwritten with just the new record — nothing persisted.
    assert tr.tasks_file_path(root).read_text(encoding="utf-8") == corrupt


def test_load_tasks_is_lenient_by_default_strict_raises(isolated_data_dir):
    root = isolated_data_dir
    _seed(root, "Alpha")
    tr.tasks_file_path(root).write_text("not json at all", encoding="utf-8")

    assert tr.load_tasks(root) == []  # read-only default stays lenient
    with pytest.raises((json.JSONDecodeError, ValueError)):
        tr.load_tasks(root, strict=True)


def test_mutate_tasks_happy_path_preserves_every_task(isolated_data_dir):
    root = isolated_data_dir
    _seed(root, "Alpha", "Beta")

    def _append(tasks):
        tasks.append(tr.new_task_record("AICC", "Gamma", "implementation", "Backlog"))
        return tasks

    tr.mutate_tasks(root, _append)
    ids = {t["id"] for t in tr.load_tasks(root)}
    assert {"alpha", "beta"} <= ids
    assert len(ids) == 3


def test_missing_file_is_still_a_legitimate_empty_store(isolated_data_dir):
    # A brand-new install has no file yet — that must remain [] (not an error),
    # even on the strict mutation path.
    root = isolated_data_dir
    tr.tasks_file_path(root).unlink(missing_ok=True)

    def _noop(tasks):
        return tasks

    assert tr.mutate_tasks(root, _noop) == []


def test_a_malformed_example_does_not_raise_from_a_lenient_read(tmp_path, monkeypatch):
    """`strict=False` promises a read that never raises. Seeding from an
    example must not break that promise.

    Found by independent review of `4b058ff`: seeding used to copy the example
    verbatim, and the exclusive-create fix briefly parsed it instead. Parsing
    moved the decode error outside the `_decode_tasks` handler below, so a
    malformed example escaped from a call whose whole contract is that it
    returns `[]` rather than raising.
    """
    monkeypatch.setenv("AICC_DATA_DIR", str(tmp_path / "data"))
    example = tmp_path / "tasks.example.json"
    example.write_text("{not valid json", encoding="utf-8")
    tr.tasks_file_path(tmp_path).unlink(missing_ok=True)

    assert tr.load_tasks(tmp_path, example_file=example) == []


def test_a_malformed_example_still_raises_on_the_strict_path(tmp_path, monkeypatch):
    """The other half of the same contract: `strict=True` must still refuse,
    so a write cycle never proceeds against a wrongly-empty list."""
    monkeypatch.setenv("AICC_DATA_DIR", str(tmp_path / "data"))
    example = tmp_path / "tasks.example.json"
    example.write_text("{not valid json", encoding="utf-8")
    tr.tasks_file_path(tmp_path).unlink(missing_ok=True)

    with pytest.raises((json.JSONDecodeError, ValueError)):
        tr.load_tasks(tmp_path, example_file=example, strict=True)
