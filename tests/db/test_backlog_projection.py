"""The master-backlog projection (BO-S4) and its round-trip guarantee.

Hermetic: renderer and parser are both pure, so the property under test —
"a rendered store re-imports as itself" — needs no database. The store test
proves the same property through real PostgreSQL once (``tests/db/
test_backlog_store.py::test_export_then_reimport_is_a_fixed_point``); here
it is proved against the values a database fixture cannot conveniently
hold: bodies carrying record-shaped lines, blank lines, edge whitespace and
``str.splitlines`` boundaries, titles carrying backticks and separators, and
the two fields whose stored value may CONTRADICT what the authored-file
parser would infer (``kind`` from the id's ``-G<n>`` suffix, ``repo`` from
the id's family or a body hint).

Every case here is a defect that reached review, not an imagined one.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from command_center.db.backlog_parser import ParsedTask, parse_backlog
from command_center.db.backlog_projection import (
    HEADER,
    TWO_WAY_WINDOW_ENDS,
    UnrenderableTask,
    render_backlog,
    two_way_window_notice,
    verify_round_trip,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "backlog_sample.md"


def _task(task_id: str, **overrides) -> ParsedTask:
    values = dict(
        task_id=task_id,
        wave="0",
        priority="P0",
        status="OPEN",
        kind="task",
        title=task_id.lower(),
        body="",
        repo=None,
        line_no=0,
    )
    values.update(overrides)
    return ParsedTask(**values)


def _reimport(tasks: list[ParsedTask]) -> list[ParsedTask]:
    """Render, re-parse, and assert the file reported nothing unreadable.

    The comparison the callers make is whole-record equality with ``line_no``
    normalized away — deliberately not a hand-listed field set, so a field
    added to ``ParsedTask`` and forgotten by the renderer fails these tests
    instead of slipping through an enumeration nobody updated.
    """
    report = parse_backlog(render_backlog(tasks))
    assert report.unparsed == [], "a rendered projection must re-read cleanly"
    return [dataclasses.replace(t, line_no=0) for t in report.tasks]


def test_the_round_trip_is_exact_for_values_the_dialect_cannot_spell() -> None:
    body = "\n".join(
        [
            "- **VOYN-W0-INJECTED** | Wave 0 | DONE | P0 | `x` | a record in a body",
            "- **Bold note** | not even a VOYN id",
            '<!-- voyn:machine {"kind": "gate", "repo": "elsewhere"} -->',
            "  two leading spaces",
            "two trailing spaces  ",
            "\ta leading tab",
            "",
            "\\a lone backslash line",
            '\\"looks like our own escape"',
            # Real ``str.splitlines`` boundaries, written as escapes so a
            # reader can SEE them: each would end the line and start a
            # bogus one if it were emitted raw.
            "a line separator \u2028 inside",
            "a next line \x85 inside",
            "a form feed \x0c inside",
            "a carriage return\r",
            "plain prose that must stay readable",
        ]
    )
    tasks = [
        _task("VOYN-W0-BODY", body=body, repo="ai-command-center"),
        _task("VOYN-W0-TRAILER", body="\n"),  # two empty lines, nothing else
    ]
    read_back = _reimport(tasks)
    assert read_back == [dataclasses.replace(t, line_no=0) for t in tasks]
    # And specifically: the injected record line did NOT become a record.
    assert [t.task_id for t in read_back] == ["VOYN-W0-BODY", "VOYN-W0-TRAILER"]


def test_kind_is_a_stored_column_not_a_function_of_the_id() -> None:
    """The store accepts either ``kind`` on either id shape (0005's CHECK
    constrains the vocabulary, not the pairing), while the parser SEEDS
    ``kind`` from the ``-G<n>`` suffix. So both disagreements have to be
    carried explicitly — a fixture whose id shape and stored kind agree
    proves nothing, because the inference would produce the right answer by
    accident."""
    gate_without_the_suffix = _task("VOYN-W0-CONTROL", kind="gate")
    task_with_the_suffix = _task("VOYN-W0-PLAIN-G9", kind="task")
    read_back = _reimport([gate_without_the_suffix, task_with_the_suffix])
    assert [t.kind for t in read_back] == ["gate", "task"]
    # The inference these fixtures contradict is real, and still the default
    # for an authored file with no directive:
    authored = parse_backlog(
        "- **VOYN-W0-CONTROL** | Wave 0 | OPEN | P0 | `x`\n"
        "- **VOYN-W0-PLAIN-G9** | Wave 0 | OPEN | P0 | `y`\n"
    )
    assert [t.kind for t in authored.tasks] == ["task", "gate"]


def test_repo_is_written_not_re_derived_from_hint_or_family() -> None:
    """Three stored values, each contradicting one of the import-side
    heuristics: family inference in both directions, and a ``Target repo``
    hint sitting in the body of a record that routes nowhere."""
    tasks = [
        # family AICC would infer "ai-command-center"; stored value is NULL.
        _task("VOYN-W0-AICC-UNROUTED", repo=None),
        # family OPS would infer None; stored value is a repo.
        _task("VOYN-OPS-ROUTED", repo="ai-command-center"),
        # the body hint would win over inference; stored value is still NULL.
        _task(
            "VOYN-W0-HINTED",
            repo=None,
            body="- Target repo (owner decision): `aios`.",
        ),
    ]
    read_back = _reimport(tasks)
    assert [t.repo for t in read_back] == [None, "ai-command-center", None]
    # Same three ids WITHOUT a directive still take the heuristics: the
    # projection overrides them, it does not remove them.
    authored = parse_backlog(
        "- **VOYN-W0-AICC-UNROUTED** | Wave 0 | OPEN | P0 | `x`\n"
        "- **VOYN-OPS-ROUTED** | Wave 0 | OPEN | P0 | `y`\n"
        "- **VOYN-W0-HINTED** | Wave 0 | OPEN | P0 | `z`\n"
        "  - Target repo (owner decision): `aios`.\n"
    )
    assert [t.repo for t in authored.tasks] == ["ai-command-center", None, "aios"]


def test_a_title_the_record_line_cannot_hold_travels_intact() -> None:
    tasks = [
        _task("VOYN-W0-TITLE", title="a `backticked` | separated\ntitle"),
        _task("VOYN-W0-SPACED", title="  padded  "),
    ]
    read_back = _reimport(tasks)
    assert [t.title for t in read_back] == [
        "a `backticked` | separated\ntitle",
        "  padded  ",
    ]
    # The unspellable one still shows the reader a name: its own id.
    assert "`VOYN-W0-TITLE`" in render_backlog(tasks[:1])


def test_plain_bodies_stay_plain() -> None:
    """The escape is for values the dialect cannot carry, not for every
    value: a projection nobody can read is not a projection. A body of
    ordinary prose renders verbatim, one indent under its record."""
    rendered = render_backlog(
        [_task("VOYN-W0-READABLE", body="Acceptance: it reads.\n- a sub-bullet.")]
    )
    records = rendered[len(HEADER) :]  # the header documents the escape
    assert "  Acceptance: it reads.\n  - a sub-bullet.\n" in records
    assert "\\\"" not in records, "an ordinary body line is never escaped"


def test_every_stored_field_appears_in_the_rendered_record() -> None:
    rendered = render_backlog(
        [
            _task(
                "VOYN-W0.5-FULL",
                wave="0.5",
                priority="P3",
                status="READY_TO_REVIEW",
                kind="gate",
                title="full-record",
                body="body line",
                repo="aios",
            )
        ]
    )
    record = [line for line in rendered.splitlines() if line.startswith("- **")]
    assert record == [
        "- **VOYN-W0.5-FULL** | Wave 0.5 | READY_TO_REVIEW | P3 | `full-record`"
    ]
    assert '  <!-- voyn:machine {"kind": "gate", "repo": "aios"} -->' in rendered
    assert "  body line" in rendered


def test_a_priorityless_record_keeps_no_priority_slot() -> None:
    read_back = _reimport([_task("VOYN-COM-LANE", wave="COM", priority=None)])
    assert read_back[0].priority is None and read_back[0].wave == "COM"


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"wave": "Wave 0"}, "wave"),  # the RENDERED form, not a stored value
        ({"wave": "W00"}, "wave"),  # W0 and W00 are distinct; W00 is not a wave
        ({"status": "MERGED"}, "status"),
        ({"kind": "control"}, "kind"),
        ({"priority": "P0 (annotated)"}, "priority"),
        ({"task_id": "TASK-1"}, "task_id"),
    ],
)
def test_render_refuses_what_it_cannot_spell(overrides, field_name) -> None:
    """A value outside 0005's CHECK vocabularies cannot reach the store, so
    this is only reachable through a hand-built record — and there the
    refusal is the point: rendering it would produce a line that reads back
    as something else, which is the failure the projection exists to make
    impossible."""
    task_id = overrides.pop("task_id", "VOYN-W0-REFUSED")
    with pytest.raises(UnrenderableTask, match=field_name):
        render_backlog([_task(task_id, **overrides)])


def test_render_refuses_a_duplicate_id() -> None:
    with pytest.raises(UnrenderableTask, match="duplicate"):
        render_backlog([_task("VOYN-W0-TWICE"), _task("VOYN-W0-TWICE")])


def test_the_projection_is_deterministic_and_a_text_fixed_point() -> None:
    tasks = [
        _task("VOYN-W0-B", body="second"),
        _task("VOYN-W0-A", body="first", repo="aios"),
    ]
    once = render_backlog(tasks)
    assert render_backlog(tasks) == once, "same records, same bytes"
    assert render_backlog(parse_backlog(once).tasks) == once, "render∘parse is stable"


def test_the_sample_fixture_survives_a_full_cycle() -> None:
    """The authored dialect in, the projected dialect out, and back: every
    record the incumbent file shape produces must be rendered without loss,
    including the ones whose repo came from a hint and whose kind came from
    the id."""
    authored = parse_backlog(FIXTURE.read_text(encoding="utf-8"))
    assert authored.tasks, "the fixture must yield records"
    read_back = _reimport(authored.tasks)
    assert read_back == [dataclasses.replace(t, line_no=0) for t in authored.tasks]


def test_verify_round_trip_actually_detects_a_difference() -> None:
    """The checker the CLI gates its write on must not be vacuous: given a
    text that does NOT match the records, it has to say so."""
    tasks = [_task("VOYN-W0-CHECKED", kind="gate", repo="aios", body="  padded  ")]
    text = render_backlog(tasks)
    assert verify_round_trip(tasks, text) == []

    mangled = text.replace('"kind": "gate"', '"kind": "task"')
    assert any("kind" in detail for _, detail in verify_round_trip(tasks, mangled))
    dropped = text.replace('\\"  padded  "', "padded")
    assert any("body" in detail for _, detail in verify_round_trip(tasks, dropped))


@pytest.mark.parametrize(
    ("directive", "reason"),
    [
        ('<!-- voyn:machine {"kind": "control"} -->', "kind outside vocabulary"),
        ('<!-- voyn:machine {"wave": "1"} -->', "unknown machine field"),
        ('<!-- voyn:machine {"repo": 7} -->', "repo does not normalize"),
        ('<!-- voyn:machine {"title": null} -->', "title does not normalize"),
        ("<!-- voyn:machine not json -->", "machine directive is not JSON"),
        ('<!-- voyn:machine ["kind"] -->', "machine directive is not an object"),
    ],
)
def test_a_machine_directive_that_does_not_normalize_is_reported(
    directive, reason
) -> None:
    """Machine input is held to the machine rule: a directive that does not
    normalize is reported with its line, never partially applied and never
    silently dropped into the body."""
    report = parse_backlog(
        "- **VOYN-W0-BAD-DIRECTIVE** | Wave 0 | OPEN | P0 | `x`\n  " + directive + "\n"
    )
    assert len(report.unparsed) == 1, report.unparsed
    assert report.unparsed[0][1].startswith(reason)
    task = report.tasks[0]
    assert task.kind == "task" and task.repo is None and task.title == "x"
    assert directive not in task.body


def test_a_machine_directive_with_no_record_is_reported() -> None:
    report = parse_backlog('<!-- voyn:machine {"kind": "gate"} -->\n')
    assert report.tasks == []
    assert [r for _, r, _ in report.unparsed] == ["machine directive outside a record"]


def test_a_value_containing_the_comment_terminator_round_trips() -> None:
    """The directive is closed by the LAST ``-->`` on the line, so a stored
    value may contain one."""
    read_back = _reimport([_task("VOYN-W0-ARROW", repo="repo-->x", title="a --> b")])
    assert read_back[0].repo == "repo-->x" and read_back[0].title == "a --> b"


def test_the_two_way_window_has_an_executed_sunset() -> None:
    """The migration's write-back direction is dated, and the date is
    exercised on both sides rather than waiting for a wall clock to reach
    it. It notifies; it does not refuse — stopping the only command that
    feeds the store would be an outage, not a migration."""
    assert two_way_window_notice(TWO_WAY_WINDOW_ENDS - timedelta(days=1)) is None
    notice = two_way_window_notice(TWO_WAY_WINDOW_ENDS)
    assert notice is not None
    assert TWO_WAY_WINDOW_ENDS.isoformat() in notice
    # The publisher reads the import report by prefix; the notice must not
    # be mistaken for it (ops/aicc_backlog_publish.py looks for "inserted ").
    assert not notice.startswith("inserted ")


def test_the_console_section_is_rendered_and_is_inert_to_the_round_trip() -> None:
    """The master file has two readers and the projection serves both: the
    console (`backlog_client.parse_recommendations`, pointed at this same
    file by `AICC_MASTER_BACKLOG`) reads section 0B, the importer reads the
    records. A projection carrying only the records would blind the panel
    the moment it was rendered over the path the panel reads — and one whose
    0B lines leaked into a record's body would corrupt the store on the way
    back."""
    from command_center.backlog_client import parse_recommendations
    from command_center.db.backlog_export import render_record

    tasks = [
        _task("VOYN-W0-CONSOLE1", body="a body line", repo="aios"),
        _task("VOYN-W0-CONSOLE2", kind="gate"),
    ]
    rows = [
        {
            "task_id": t.task_id,
            "wave": t.wave,
            "priority": t.priority,
            "status": t.status,
            "title": t.title,
            "repo": t.repo,
            "updated_at": None,
        }
        for t in tasks
    ]
    text = render_backlog(tasks, [render_record(row) for row in rows])

    console = parse_recommendations(text)
    assert console.errors == []
    assert [r.issue_id for r in console.records] == [t.task_id for t in tasks]

    assert verify_round_trip(tasks, text) == []
    read_back = parse_backlog(text)
    assert read_back.unparsed == []
    assert [t.body for t in read_back.tasks] == ["a body line", ""]


def test_a_line_that_is_not_a_console_record_is_refused_from_that_section() -> None:
    """Section 0B is not a free-text slot: a line that is not one of
    `backlog_export`'s records — or one carrying a break that would silently
    become two lines — is refused rather than written into the file the
    console parses."""
    tasks = [_task("VOYN-W0-STRICT")]
    with pytest.raises(UnrenderableTask, match="section-0B"):
        render_backlog(tasks, ["- **VOYN-W0-SNEAK** | Wave 0 | DONE | P0 | `x`"])
    with pytest.raises(UnrenderableTask, match="section-0B"):
        render_backlog(tasks, ["- VOYN_RECOMMENDATION | ts=1\n- **VOYN-W0-SNEAK** |"])
