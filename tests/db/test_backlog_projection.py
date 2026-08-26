"""The store rendered back to Markdown (BO-S4) — and proved by round trip.

Hermetic, the projection module's own rule: no database, synthetic task
dicts in exactly the shape `BacklogStore.export_tasks` returns. The property
that matters is round trip through the parser that already exists and is
already trusted: `parse_backlog(render_backlog(tasks))` must reproduce every
task's machine fields, because a generated file that a re-import of itself
cannot reconstruct is a generator with a bug, not a projection.
"""

from __future__ import annotations

from command_center.db.backlog_parser import parse_backlog
from command_center.db.backlog_projection import (
    IMPORT_SUNSET_DATE,
    IMPORT_SUNSET_TASK,
    render_backlog,
)

GENERATED_AT = "2026-08-26T12:00:00+00:00"

TASKS = [
    dict(
        task_id="VOYN-W0-S1",
        wave="0",
        priority="P0",
        status="OPEN",
        kind="task",
        title="first-task",
        body="Acceptance: sub-bullets travel into the body.\n"
        "Target repo (owner decision): `~/somewhere/repo-a`.",
        repo="~/somewhere/repo-a",
    ),
    dict(
        task_id="VOYN-W0-S5",
        wave="0",
        priority=None,
        status="DONE",
        kind="task",
        title="no-priority",
        body="",
        repo=None,
    ),
    dict(
        task_id="VOYN-W0-G1",
        wave="0",
        priority=None,
        status="OPEN",
        kind="gate",
        title="gate",
        body="",
        repo=None,
    ),
    dict(
        task_id="VOYN-W0.5-S1",
        wave="0.5",
        priority="P1",
        status="READY_TO_REVIEW",
        kind="task",
        title="wave-half",
        body="Wave 0.5 is distinct from wave 0.",
        repo=None,
    ),
    dict(
        task_id="VOYN-COM-S1",
        wave="COM",
        priority=None,
        status="OPEN",
        kind="task",
        title="lane-com",
        body="A named parallel lane.",
        repo=None,
    ),
    dict(
        task_id="VOYN-POOL-S1",
        wave="W7",
        priority=None,
        status="OPEN",
        kind="task",
        title="idea-pool",
        body="",
        repo=None,
    ),
]


def test_round_trips_through_the_parser():
    rendered = render_backlog(TASKS, generated_at=GENERATED_AT)
    report = parse_backlog(rendered)

    assert report.unparsed == []
    by_id = {t.task_id: t for t in report.tasks}
    assert set(by_id) == {t["task_id"] for t in TASKS}

    for task in TASKS:
        parsed = by_id[task["task_id"]]
        assert parsed.wave == task["wave"]
        assert parsed.priority == task["priority"]
        assert parsed.status == task["status"]
        assert parsed.kind == task["kind"]
        assert parsed.title == task["title"]
        assert parsed.repo == task["repo"]


def test_numeric_waves_get_the_wave_prefix_named_lanes_do_not():
    rendered = render_backlog(TASKS, generated_at=GENERATED_AT)
    assert "## Wave 0\n" in rendered
    assert "## Wave 0.5\n" in rendered
    assert "## COM\n" in rendered
    assert "## W7\n" in rendered
    # A named lane is never prefixed, so "## Wave COM" must not appear.
    assert "## Wave COM" not in rendered


def test_output_is_grouped_by_wave_not_interleaved():
    rendered = render_backlog(TASKS, generated_at=GENERATED_AT)
    lines = rendered.splitlines()
    wave_0_heading = lines.index("## Wave 0")
    wave_half_heading = lines.index("## Wave 0.5")
    assert wave_0_heading < wave_half_heading
    # Every task line between the two headings belongs to wave 0.
    for line in lines[wave_0_heading + 1 : wave_half_heading]:
        assert "**VOYN-W0-S1**" in line or "**VOYN-W0-S5**" in line or "**VOYN-W0-G1**" in line


def test_render_is_deterministic():
    first = render_backlog(TASKS, generated_at=GENERATED_AT)
    second = render_backlog(list(reversed(TASKS)), generated_at=GENERATED_AT)
    assert first == second


def test_a_body_with_continuation_lines_collapses_to_one_record_line():
    rendered = render_backlog(TASKS, generated_at=GENERATED_AT)
    lines = [ln for ln in rendered.splitlines() if "**VOYN-W0-S1**" in ln]
    assert len(lines) == 1, "a multi-line body must not fragment into new records"
    assert "\n" not in lines[0]


def test_a_task_with_no_priority_omits_the_priority_field():
    rendered = render_backlog(TASKS, generated_at=GENERATED_AT)
    (line,) = [ln for ln in rendered.splitlines() if "**VOYN-W0-S5**" in ln]
    assert line == "- **VOYN-W0-S5** | Wave 0 | DONE | `no-priority`"


def test_the_sunset_banner_names_the_date_and_the_owning_task():
    rendered = render_backlog(TASKS, generated_at=GENERATED_AT)
    assert IMPORT_SUNSET_DATE in rendered
    assert IMPORT_SUNSET_TASK in rendered
    assert GENERATED_AT in rendered
