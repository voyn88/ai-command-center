"""The Markdown projection of the structured backlog store (BO-S4).

Hermetic, like the parser it round-trips against: no database. A
``ParsedTask`` (post-``flush()``: body already carries any continuation
bullets and repo hint, repo already resolved) is exactly the row shape
``BacklogStore.export_all`` returns, so these tests build the "stored" side
by parsing the fixture rather than standing up Postgres.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from command_center.db.backlog_parser import _infer_repo, parse_backlog
from command_center.db.backlog_projection import export_tasks, render_task_line, write_projection

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "backlog_sample.md"


def _row(task) -> dict:
    d = dataclasses.asdict(task)
    d.pop("line_no")
    return d


@pytest.fixture(scope="module")
def stored_tasks():
    report = parse_backlog(FIXTURE.read_text(encoding="utf-8"))
    return [_row(t) for t in report.tasks]


def test_export_then_reimport_is_a_fixed_point(stored_tasks) -> None:
    """The invariant BO-S4 exists to hold: reconciling the exported text
    back into the store must change nothing already there."""
    exported = export_tasks(stored_tasks)
    reparsed = {t.task_id: _row(t) for t in parse_backlog(exported).tasks}

    for original in stored_tasks:
        again = reparsed[original["task_id"]]
        assert again == original, original["task_id"]


def test_continuation_bullets_survive_as_separate_lines(stored_tasks) -> None:
    """Regression for the collapsed-body defect: a body with continuation
    bullets must still be newline-separated (not joined into one line) after
    a render, or the fixed point above breaks silently for this record."""
    task = next(t for t in stored_tasks if t["task_id"] == "VOYN-W0-S1")
    assert "\n" in task["body"], "fixture precondition: a multi-line body"

    rendered = render_task_line(task)
    body_lines = rendered.splitlines()[1:]
    assert len(body_lines) == task["body"].count("\n")
    for line in body_lines:
        assert line.startswith("  "), "continuation lines must out-indent the record"


def test_repo_set_outside_hint_or_inference_still_round_trips() -> None:
    """A repo that came from neither an explicit hint nor family inference
    (e.g. set directly through the API) must not be silently overwritten on
    the next import — export has to render an explicit hint for it."""
    task_id = "VOYN-W0-AICC-CUSTOM"
    assert _infer_repo(task_id) == "ai-command-center", "must diverge from override below"
    task = dict(
        task_id=task_id,
        wave="0",
        priority="P0",
        status="OPEN",
        kind="task",
        title="custom-repo",
        body="plain prose, no repo mention.",
        repo="a-completely-different-repo",
    )

    rendered = render_task_line(task)
    reparsed = parse_backlog(rendered).tasks[0]
    assert reparsed.repo == "a-completely-different-repo"


def test_repo_matching_inference_adds_no_redundant_hint() -> None:
    """When the body/inference already reconstructs the stored repo, export
    should not manufacture a hint the store never had — keeps the fixed
    point exact instead of merely eventually-stable."""
    task = dict(
        task_id="VOYN-W0-AICC-PLAIN",
        wave="0",
        priority="P0",
        status="OPEN",
        kind="task",
        title="plain",
        body="no repo mention here.",
        repo="ai-command-center",  # exactly what _infer_repo already gives
    )
    rendered = render_task_line(task)
    assert "Target repo" not in rendered


def test_export_orders_by_task_id_and_ends_with_newline(stored_tasks) -> None:
    exported = export_tasks(stored_tasks)
    assert exported.endswith("\n")
    ids = [line.split("**")[1] for line in exported.splitlines() if line.startswith("- **")]
    assert ids == sorted(ids)


def test_export_of_no_tasks_is_the_empty_string() -> None:
    assert export_tasks([]) == ""


def test_write_projection_is_atomic_and_leaves_no_temp_file(tmp_path) -> None:
    target = tmp_path / "VOYN_TASKS_BACKLOG.md"
    target.write_text("stale content\n", encoding="utf-8")

    write_projection(target, "fresh content\n")

    assert target.read_text(encoding="utf-8") == "fresh content\n"
    assert list(tmp_path.iterdir()) == [target], "no stray temp file left behind"
