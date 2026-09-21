"""Live read projection of the VOYN master backlog into ACC
(`command_center.backlog_client`).

The Backlog Engine is the single owner of the `Task` entity; ACC is only a
*reader* of its `VOYN_RECOMMENDATION` master store (engine plan invariant #5:
"Локальные tasks.json, очереди и UI-модели являются только проекциями"). These
tests pin that contract: the client parses the machine lines, never writes, and
degrades to an empty-but-usable projection when the master store is absent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from command_center import backlog_client as bc

# One real record straight from work/VOYN_TASKS_BACKLOG.md (14 fields).
_REC = (
    "- VOYN_RECOMMENDATION | ts=2026-08-12T17:00:00Z | status=PO-Approved | "
    "issue_id=VOYN-W1-UI | current_wave=W1 | proposed_wave=W1 | priority=P0 | "
    "owner=frontend | effect=high | effort=L | "
    "acceptance=accept:no_duplicate_screens | "
    "task=build_dashboard_desktop_on_api_and_tokens | "
    "evidence=file:command_center/api | file_scope=command_center/ui | "
    "parallel_domain=ux"
)

# The canonical *spec template* line from section 0B — placeholder `<...>` tokens,
# introduced with a backtick, never a list dash. Must never be parsed as a record.
_TEMPLATE = (
    "`VOYN_RECOMMENDATION | ts=<ISO8601> | status=<AI-Reco|PO-Review|PO-Approved> "
    "| issue_id=<ID|NEW-####> | ...`"
)


def test_parses_a_real_recommendation_line():
    result = bc.parse_recommendations(_REC)
    assert result.errors == []
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.issue_id == "VOYN-W1-UI"
    assert rec.status == "PO-Approved"
    assert rec.priority == "P0"
    assert rec.proposed_wave == "W1"
    assert rec.parallel_domain == "ux"
    assert rec.task == "build_dashboard_desktop_on_api_and_tokens"


def test_task_slug_is_humanized_into_a_title():
    rec = bc.parse_recommendations(_REC).records[0]
    assert rec.title == "Build dashboard desktop on api and tokens"


def test_template_and_prose_lines_are_ignored_not_parsed_as_records():
    text = "\n".join(["# heading", "- some prose", _TEMPLATE, _REC])
    result = bc.parse_recommendations(text)
    assert len(result.records) == 1
    assert result.errors == []  # the template line is skipped, not an error


def test_malformed_record_is_reported_not_crashed():
    bad = "- VOYN_RECOMMENDATION | ts=2026 | status=PO-Approved"  # too few fields
    result = bc.parse_recommendations("\n".join([_REC, bad]))
    assert len(result.records) == 1  # the good one still lands
    assert len(result.errors) == 1
    assert result.errors[0].line_no == 2


def test_unknown_field_key_is_an_error():
    bad = _REC.replace("parallel_domain=ux", "bogus_key=ux")
    result = bc.parse_recommendations(bad)
    assert result.records == []
    assert len(result.errors) == 1


def test_is_approved_reflects_status():
    approved = bc.parse_recommendations(_REC).records[0]
    assert approved.is_approved is True
    draft = bc.parse_recommendations(_REC.replace("PO-Approved", "AI-Reco")).records[0]
    assert draft.is_approved is False


def test_resolve_backlog_path_prefers_arg_then_env(monkeypatch, tmp_path):
    monkeypatch.delenv(bc.MASTER_BACKLOG_ENV, raising=False)
    assert bc.resolve_backlog_path() is None
    monkeypatch.setenv(bc.MASTER_BACKLOG_ENV, str(tmp_path / "env.md"))
    assert bc.resolve_backlog_path() == tmp_path / "env.md"
    explicit = tmp_path / "explicit.md"
    assert bc.resolve_backlog_path(explicit) == explicit  # arg wins over env


def test_load_projection_reads_and_parses_the_master_store(tmp_path):
    f = tmp_path / "VOYN_TASKS_BACKLOG.md"
    f.write_text("\n".join([_TEMPLATE, _REC]), encoding="utf-8")
    proj = bc.load_projection(f)
    assert proj.exists is True
    assert proj.source_path == f
    assert proj.source_mtime == pytest.approx(f.stat().st_mtime)
    assert [r.issue_id for r in proj.records] == ["VOYN-W1-UI"]


def test_missing_master_store_is_empty_projection_not_an_error(tmp_path):
    proj = bc.load_projection(tmp_path / "nope.md")
    assert proj.exists is False
    assert proj.records == []
    assert proj.errors == []  # absence is not a parse error


def test_unconfigured_backlog_is_empty_projection(monkeypatch):
    monkeypatch.delenv(bc.MASTER_BACKLOG_ENV, raising=False)
    proj = bc.load_projection()
    assert proj.exists is False
    assert proj.records == []


def test_approved_recommendations_filters_to_executable(tmp_path):
    draft = _REC.replace("PO-Approved", "AI-Reco").replace("VOYN-W1-UI", "DRAFT-1")
    f = tmp_path / "b.md"
    f.write_text("\n".join([_REC, draft]), encoding="utf-8")
    proj = bc.load_projection(f)
    approved = bc.approved_recommendations(proj)
    assert [r.issue_id for r in approved] == ["VOYN-W1-UI"]


def test_read_model_is_read_only_and_sourced_from_master(tmp_path):
    rec = bc.parse_recommendations(_REC).records[0]
    model = bc.to_read_model(rec)
    assert model["id"] == "VOYN-W1-UI"
    assert model["title"] == "Build dashboard desktop on api and tokens"
    assert model["status"] == "PO-Approved"
    assert model["priority"] == "P0"
    assert model["wave"] == "W1"
    assert model["domain"] == "ux"
    assert model["read_only"] is True
    assert model["source"] == "master_backlog"


def test_client_exposes_no_write_surface():
    # A read projection must never grow a writer (engine invariant #5).
    for banned in ("save", "write", "mutate", "apply", "delete", "create_task"):
        assert not any(
            banned in name for name in dir(bc)
        ), f"backlog_client must stay read-only; found {banned!r}-like attribute"


def test_summarize_counts_by_facet():
    draft = _REC.replace("PO-Approved", "AI-Reco").replace("priority=P0", "priority=P1")
    summary = bc.summarize(bc.parse_recommendations("\n".join([_REC, draft])))
    assert summary.total == 2
    assert summary.approved == 1
    assert summary.by_priority == {"P0": 1, "P1": 1}
    assert summary.by_status == {"AI-Reco": 1, "PO-Approved": 1}
    assert summary.by_wave == {"W1": 2}


def test_execution_queue_is_approved_only_and_priority_ordered():
    p1 = _REC.replace("VOYN-W1-UI", "B").replace("priority=P0", "priority=P1")
    draft = _REC.replace("VOYN-W1-UI", "C").replace("PO-Approved", "AI-Reco")
    proj = bc.parse_recommendations("\n".join([p1, _REC, draft]))
    queue = bc.execution_queue(bc.Projection(records=proj.records))
    assert [r.issue_id for r in queue] == ["VOYN-W1-UI", "B"]  # P0 before P1, no draft


def test_filter_records_search_and_facets():
    other = (
        _REC.replace("VOYN-W1-UI", "OTHER")
        .replace("task=build_dashboard_desktop_on_api_and_tokens", "task=fix_login_bug")
        .replace("parallel_domain=ux", "parallel_domain=api")
    )
    records = bc.parse_recommendations("\n".join([_REC, other])).records
    assert [r.issue_id for r in bc.filter_records(records, query="login")] == ["OTHER"]
    assert [r.issue_id for r in bc.filter_records(records, domain="ux")] == ["VOYN-W1-UI"]
    assert bc.filter_records(records, query="dashboard", domain="api") == []


# --- The generated projection's own render stamp -----------------------------
#
# Since BO-S4 this file is normally rendered by `backlog-export`, and the
# rendering stamps its own age into its header. These pin the reading half of
# that contract; `tests/db/test_backlog_export.py` pins that the exporter's
# header really carries a line this side can read.


def _stamped(rendered_at: str, rows: int, *, body: str = _REC) -> str:
    """A file shaped like a real export header: title, marker prose, stamp."""
    return "\n".join(
        [
            "# VOYN master backlog — generated projection",
            "",
            "This file is RENDERED from the canonical PostgreSQL backlog store",
            "(`backlog_task`); it is regenerated whole and never read back.",
            "",
            f"Rendered {rendered_at} from {rows} task row(s)",
            "",
            "## 0B. Machine records",
            "",
            body,
        ]
    )


def test_the_render_stamp_round_trips_through_its_own_renderer():
    """`render_generated_stamp` and `parse_generated_stamp` are two halves of
    one line format; if either drifts the stamp goes quietly unreadable and the
    console silently falls back to mtime — the exact signal BO-S4 replaced."""
    moment = datetime(2026, 9, 9, 6, 30, tzinfo=timezone.utc)
    line = bc.render_generated_stamp(moment, 394)
    assert line == "Rendered 2026-09-09T06:30:00Z from 394 task row(s)"
    parsed = bc.parse_generated_stamp(line)
    assert parsed == bc.GeneratedStamp(rendered_at=moment, row_count=394)


def test_the_stamp_is_rendered_in_utc_whatever_the_writers_zone():
    """Two hosts' stamps have to be comparable, and a reader must never have to
    guess which clock a stamp means."""
    moment = datetime(2026, 9, 9, 6, 30, tzinfo=timezone.utc)
    elsewhere = moment.astimezone(timezone(timedelta(hours=5)))
    line = bc.render_generated_stamp(elsewhere, 1)
    assert line == bc.render_generated_stamp(moment, 1)
    assert bc.parse_generated_stamp(line).rendered_at == moment


def test_an_authored_file_carries_no_stamp():
    """`None`, not an exception and not a fabricated age: the owner's own
    backlog has no export tick behind it, so it has no cadence to be late
    against and the caller falls back to mtime for it."""
    assert bc.parse_generated_stamp(_REC) is None
    assert bc.parse_generated_stamp("") is None


def test_a_stamp_quoted_deep_in_a_body_is_not_the_files_own_claim():
    """Bounded to the header for the same reason the import-side marker check
    is: the authored backlog legitimately *describes* this machinery inside a
    task body, and prose that quotes a stamp must not make the file claim to
    be a rendering."""
    quoted = "\n".join(
        ["# VOYN master backlog"]
        + ["filler"] * bc.HEADER_SCAN_LINES
        + ["Rendered 2026-09-09T06:30:00Z from 394 task row(s)"]
    )
    assert bc.parse_generated_stamp(quoted) is None


def test_a_stamp_shaped_sentence_is_not_matched_as_a_substring():
    """Machine-fields rule: whole line or nothing. A header sentence that
    merely mentions a render time is prose, not a claim."""
    prose = "See: Rendered 2026-09-09T06:30:00Z from 394 task row(s) is the shape."
    assert bc.parse_generated_stamp(prose) is None


def test_a_stamp_shaped_line_that_is_not_a_real_moment_degrades_the_read(tmp_path):
    """The regex pins digit counts, not calendars, so `2026-13-45T99:99:99Z` is
    stamp-shaped and impossible. This module's whole contract is that a
    malformed file is *reported or ignored*, never fatal — and `load_projection`
    runs this on every file it opens, so one mistyped character in a header must
    not take the Master Backlog page down with an unhandled ValueError."""
    impossible = "Rendered 2026-13-45T99:99:99Z from 1 task row(s)"
    assert bc.parse_generated_stamp(impossible) is None

    f = tmp_path / "VOYN_TASKS_BACKLOG.md"
    f.write_text(_stamped("2026-02-30T00:00:00Z", 1), encoding="utf-8")
    proj = bc.load_projection(f)  # must not raise
    assert proj.stamp is None
    assert [r.issue_id for r in proj.records] == ["VOYN-W1-UI"]  # records still land


def test_a_valid_stamp_after_an_impossible_one_still_wins(tmp_path):
    """Skipping, not aborting: a broken line is not evidence that the rest of
    the header is unreadable."""
    text = "\n".join(
        [
            "# VOYN master backlog",
            "Rendered 2026-13-45T00:00:00Z from 9 task row(s)",
            "Rendered 2026-09-09T06:30:00Z from 1 task row(s)",
        ]
    )
    assert bc.parse_generated_stamp(text) == bc.GeneratedStamp(
        rendered_at=datetime(2026, 9, 9, 6, 30, tzinfo=timezone.utc), row_count=1
    )


def test_load_projection_reads_the_stamp_out_of_a_generated_file(tmp_path):
    f = tmp_path / "VOYN_TASKS_BACKLOG.md"
    f.write_text(_stamped("2026-09-09T06:30:00Z", 1), encoding="utf-8")
    proj = bc.load_projection(f)
    assert proj.stamp == bc.GeneratedStamp(
        rendered_at=datetime(2026, 9, 9, 6, 30, tzinfo=timezone.utc), row_count=1
    )
    assert [r.issue_id for r in proj.records] == ["VOYN-W1-UI"]


def test_a_hand_authored_projection_has_no_stamp_and_still_reports_mtime(tmp_path):
    f = tmp_path / "VOYN_TASKS_BACKLOG.md"
    f.write_text(_REC, encoding="utf-8")
    proj = bc.load_projection(f)
    assert proj.stamp is None
    assert proj.source_mtime is not None  # the fallback the panel still uses


def test_staleness_is_measured_against_the_ticks_own_cadence():
    """One missed 5-minute tick is jitter (AccuracySec, a slow query, a
    restart); three in a row is a dead timer. The threshold has to sit between
    those two, or the alarm is either useless or ignored."""
    rendered = datetime(2026, 9, 9, 6, 30, tzinfo=timezone.utc)
    stamp = bc.GeneratedStamp(rendered_at=rendered, row_count=1)
    assert stamp.is_stale(rendered + timedelta(minutes=6)) is False
    assert stamp.is_stale(rendered + timedelta(minutes=16)) is True
    assert stamp.age(rendered + timedelta(minutes=16)) == timedelta(minutes=16)


def test_a_stamp_from_the_future_is_reported_not_clamped():
    """A projection stamped ahead of the reader's clock means two hosts
    disagree about the time — a real problem. Clamping the age to zero would
    render it as permanently, perfectly fresh instead."""
    rendered = datetime(2026, 9, 9, 6, 30, tzinfo=timezone.utc)
    stamp = bc.GeneratedStamp(rendered_at=rendered, row_count=1)
    assert stamp.age(rendered - timedelta(hours=2)) == timedelta(hours=-2)
    assert stamp.is_stale(rendered - timedelta(hours=2)) is False


def test_a_stamp_disagreeing_with_the_record_count_is_reported(tmp_path):
    """A generated file holds exactly as many record lines as its header says,
    so a mismatch means lines were added or removed after the render — the one
    trace ADR-0011's "do not edit the generated file" convention can leave.
    Detects inserted/deleted records; an edit that changes a field in place
    changes no count and is still invisible, which is why the ADR keeps calling
    it a convention."""
    f = tmp_path / "VOYN_TASKS_BACKLOG.md"
    f.write_text(_stamped("2026-09-09T06:30:00Z", 1), encoding="utf-8")
    assert bc.stamp_matches_content(bc.load_projection(f)) is True

    deleted = tmp_path / "deleted.md"
    deleted.write_text(_stamped("2026-09-09T06:30:00Z", 2), encoding="utf-8")
    assert bc.stamp_matches_content(bc.load_projection(deleted)) is False

    f.write_text(_stamped("2026-09-09T06:30:00Z", 1, body=_REC), encoding="utf-8")
    assert bc.stamp_matches_content(bc.load_projection(f)) is True


def test_an_unreadable_record_line_counts_as_present_not_missing():
    """A line the parser rejects is still a line that was rendered. Counting
    only good records would report every parse error a second time as a
    phantom deleted row and blur two different problems together."""
    broken = "- VOYN_RECOMMENDATION | ts=2026 | status=PO-Approved"
    projection = bc.Projection(
        records=bc.parse_recommendations(_REC).records,
        errors=bc.parse_recommendations(broken).errors,
        stamp=bc.GeneratedStamp(
            rendered_at=datetime(2026, 9, 9, 6, 30, tzinfo=timezone.utc), row_count=2
        ),
    )
    assert len(projection.errors) == 1
    assert bc.stamp_matches_content(projection) is True


def test_stamp_checks_are_inert_without_a_stamp():
    """No stamp is not "mismatch": an authored file never had a row count to
    disagree with, and reporting one would fire the panel's edited-file warning
    on every hand-authored backlog."""
    assert bc.stamp_matches_content(bc.Projection()) is None


# --- UI page (Streamlit AppTest) -------------------------------------------


def _backlog_fixture(tmp_path):
    f = tmp_path / "VOYN_TASKS_BACKLOG.md"
    draft = _REC.replace("VOYN-W1-UI", "DRAFT-1").replace("PO-Approved", "AI-Reco")
    f.write_text("\n".join([_TEMPLATE, _REC, draft]), encoding="utf-8")
    return f


def _page_script() -> None:
    # Re-exec'd standalone by AppTest: path comes from the env var, not a closure.
    import os

    from command_center.ui import master_backlog_panel

    master_backlog_panel.render_master_backlog_page(os.environ.get("AICC_MASTER_BACKLOG"))


def _run_page(monkeypatch, path):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("AICC_MASTER_BACKLOG", str(path))
    return AppTest.from_function(_page_script, default_timeout=30).run()


def test_page_renders_connected_projection_with_counts(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, _backlog_fixture(tmp_path))
    assert not at.exception
    body = " ".join(str(m.value) for m in at.markdown) + " ".join(
        str(c.value) for c in at.caption
    )
    assert "Master Backlog" in " ".join(str(t.value) for t in at.title)
    # Read-only / master authority labelling is present.
    assert any("read-only" in str(c.value).lower() for c in at.caption)
    assert any("master" in str(i.value).lower() for i in at.info)
    # Totals: 2 records, 1 approved.
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Всего записей"] == "2"
    assert metric_values["Approved"] == "1"
    # Freshness/source surfaced. This fixture is hand-authored -- no render
    # stamp -- so the metric falls back to mtime and says so, rather than
    # implying a tick stands behind a file that has none.
    assert "master store" in [m.value for m in at.metric]
    assert "Актуальность (mtime)" in [m.label for m in at.metric]
    assert body  # smoke


def _rendered_fixture(tmp_path, *, rendered_at, rows=1, name="VOYN_TASKS_BACKLOG.md"):
    """A real `backlog-export` rendering, produced by the real exporter.

    Handwriting the header here would test the panel against a fixture rather
    than against the file production actually writes -- the drift the stamp's
    single-definition format exists to prevent.
    """
    from command_center.db import backlog_export

    store_rows = [
        {
            "task_id": f"VOYN-W0-AICC-EXPORTED-{index}",
            "wave": "0",
            "priority": "P0",
            "status": "OPEN",
            "title": f"exported row {index}",
            "repo": "ai-command-center",
            "updated_at": rendered_at,
        }
        for index in range(rows)
    ]
    f = tmp_path / name
    f.write_text(
        backlog_export.render_projection(store_rows, generated_at=rendered_at),
        encoding="utf-8",
    )
    return f


def test_page_shows_a_generated_projections_own_render_stamp(monkeypatch, tmp_path):
    """For a rendered file the freshness metric must read the header, not the
    filesystem: this fixture is written *now*, so mtime cannot distinguish it
    from a stale one, and only the stamp carries the truth."""
    fresh = datetime.now(timezone.utc)
    at = _run_page(monkeypatch, _rendered_fixture(tmp_path, rendered_at=fresh))
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Актуальность (рендер)" in labels
    assert "Актуальность (mtime)" not in labels
    assert not at.error  # a fresh tick raises nothing


def test_page_says_the_export_tick_is_dead_for_an_old_stamp(monkeypatch, tmp_path):
    """The failure BO-S4 exists to end, reproduced exactly: a file written to
    disk seconds ago (fresh mtime) whose content stopped being true long ago.
    mtime called this fresh; the stamp has to call it dead."""
    ancient = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    at = _run_page(monkeypatch, _rendered_fixture(tmp_path, rendered_at=ancient))
    assert not at.exception
    assert any("устарела" in str(e.value) for e in at.error)


def test_page_flags_a_generated_file_edited_after_its_render(monkeypatch, tmp_path):
    """Deleting a record line from a rendering leaves the header promising more
    rows than the body holds -- the one trace ADR-0011's "do not edit the
    generated file" convention can leave behind."""
    path = _rendered_fixture(tmp_path, rendered_at=datetime.now(timezone.utc), rows=2)
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if "VOYN-W0-AICC-EXPORTED-1" not in line
    ]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    at = _run_page(monkeypatch, path)
    assert not at.exception
    assert any("не совпадает со штампом" in str(w.value) for w in at.warning)


def test_page_explains_when_backlog_not_connected(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, tmp_path / "missing.md")
    assert not at.exception
    assert any(bc.MASTER_BACKLOG_ENV in str(w.value) for w in at.warning)
    # An unconnected page must not have rendered the records table metrics.
    assert "Всего записей" not in [m.label for m in at.metric]


def test_rich_records_parse_exact_statuses_and_slug():
    from command_center.backlog_client import parse_rich_records

    text = (
        "- **VOYN-W0-EXAMPLE** | Wave 0 | OPEN (сверено 2026-08-20) | **P0** | "
        "Security | `example-slug` | Текст.\n"
        "  - **VOYN-W4-DONE-ONE** | Wave 4 | DONE | P1 | Team | `done_thing` | x\n"
        "- **VOYN-W1-WEIRD** | Wave 1 | TRIAGE_LATER | P2 | Team | `weird` | x\n"
        "- не запись | OPEN | мимо\n"
    )
    records = {r.record_id: r for r in parse_rich_records(text)}
    assert records["VOYN-W0-EXAMPLE"].status == "OPEN"
    assert records["VOYN-W0-EXAMPLE"].priority == "P0"
    assert records["VOYN-W0-EXAMPLE"].slug == "example-slug"
    assert records["VOYN-W0-EXAMPLE"].title == "Example slug"
    # Indented sub-list records are body lines too — the regex is anchored to
    # line starts; the nested DONE line above is deliberately NOT matched
    # (it belongs to a parent record's evidence trail).
    assert "VOYN-W4-DONE-ONE" not in records
    # Outside the exact vocabulary -> UNKNOWN, surfaced, never guessed.
    assert records["VOYN-W1-WEIRD"].status == "UNKNOWN"


def test_rich_records_on_the_real_master_shape(tmp_path):
    from command_center.backlog_client import load_rich_records

    master = tmp_path / "master.md"
    master.write_text(
        "- **VOYN-W0-A** | Wave 0 | IN_PROGRESS | P1 | X | `a` | t\n"
        "- **VOYN-W0-B** | Wave 0 | READY_TO_REVIEW | P1 | X | `b` | t\n",
        encoding="utf-8",
    )
    statuses = {r.record_id: r.status for r in load_rich_records(master)}
    assert statuses == {"VOYN-W0-A": "IN_PROGRESS", "VOYN-W0-B": "READY_TO_REVIEW"}


# --- The machine-rendered execution-status surface (BO-S4) --------------------


def test_machine_status_records_round_trip_through_their_own_renderer():
    """`backlog_export` writes this surface THROUGH `render_task_status`, so
    the renderer and the reader are the one contract that must not drift.
    Pins the field set, the order, and that a rendered record reads back
    identical — a field renamed on either side breaks here, not in the
    console."""
    record = bc.RichRecord(
        record_id="VOYN-W0-EXAMPLE",
        wave="Wave 0",
        status="IN_PROGRESS",
        priority="P0",
        slug="example-slug",
    )
    line = bc.render_task_status(record)

    tokens = line[2:].split(bc.FIELD_SEP)
    assert tokens[0] == bc.TASK_STATUS_MARKER
    assert [t.partition("=")[0] for t in tokens[1:]] == list(bc.TASK_STATUS_FIELDS)
    assert bc.parse_rich_records(line) == [record]


def test_the_machine_surface_shares_no_line_shape_with_the_authored_one():
    """ADR-0011's safety property, from this side: the rendered line must be
    invisible to the importer's patterns, which is only true while it stays
    an UNBOLDED list item with its own marker. A reader that started emitting
    `- **VOYN-...** |` would be re-importable as an authored task."""
    from command_center.db.backlog_parser import _RECORD_SHAPED, _TASK_LINE

    line = bc.render_task_status(
        bc.RichRecord("VOYN-W0-EXAMPLE", "Wave 0", "DONE", "P0", "slug")
    )
    assert _TASK_LINE.match(line) is None
    assert _RECORD_SHAPED.match(line) is None
    # ...and it is not a 0B record either: the two machine surfaces carry
    # different vocabularies and neither reader may claim the other's lines.
    assert bc.parse_recommendations(line).records == []
    assert bc.parse_recommendations(line).errors == []


def test_the_rich_vocabulary_matches_the_store_exactly():
    """`RICH_STATUSES` and the store's `backlog_parser.STATUSES` have to be
    the same set now that a rendered record carries a column value straight
    from `backlog_task`.

    While this surface was only hand-authored, a divergence was harmless — a
    human typing a status outside the set got UNKNOWN and noticed. Once
    `backlog-export` renders it, a status the store holds but this set omits
    is silently relabelled UNKNOWN and dropped into the Backlog lane, with
    nothing anywhere saying a known status was thrown away."""
    from command_center.db.backlog_parser import STATUSES

    assert bc.RICH_STATUSES == STATUSES


def test_a_machine_record_overrides_a_stale_authored_line_for_the_same_id():
    """The migration-window precedence rule. The store is canonical and a
    `VOYN_TASK_STATUS` line is a direct reading of it; a bold line is
    owner-typed input the store may have moved past. Preferring the authored
    line would let a stale hand edit mask live execution state — the exact
    silent staleness BO-S4 exists to end."""
    text = (
        "- **VOYN-W0-A** | Wave 0 | OPEN | P0 | X | `a` | t\n"
        "- **VOYN-W0-B** | Wave 0 | OPEN | P1 | X | `b` | t\n"
        "\n"
        "- VOYN_TASK_STATUS | id=VOYN-W0-A | wave=Wave 0 | status=DONE"
        " | priority=P0 | slug=a\n"
        "- VOYN_TASK_STATUS | id=VOYN-W0-C | wave=Wave 0 | status=IN_PROGRESS"
        " | priority=P2 | slug=c\n"
    )
    records = bc.parse_rich_records(text)

    # The overridden record keeps A's original position, so a reader
    # rendering in document order does not watch tasks jump lanes-and-places.
    assert [r.record_id for r in records] == ["VOYN-W0-A", "VOYN-W0-B", "VOYN-W0-C"]
    by_id = {r.record_id: r for r in records}
    assert by_id["VOYN-W0-A"].status == "DONE"
    # An id only the authored surface knows is untouched...
    assert by_id["VOYN-W0-B"].status == "OPEN"
    # ...and one only the machine surface knows is still present.
    assert by_id["VOYN-W0-C"].status == "IN_PROGRESS"


def test_a_file_with_no_machine_records_reads_exactly_as_before():
    """The override path is the only behaviour this shape added. Every
    hand-authored backlog — still most of them — must come back from the
    bold-line parser untouched, duplicate ids and all."""
    text = (
        "- **VOYN-W0-A** | Wave 0 | OPEN | P0 | X | `a` | t\n"
        "- **VOYN-W0-A** | Wave 0 | DONE | P0 | X | `a-again` | t\n"
    )
    records = bc.parse_rich_records(text)
    assert [(r.record_id, r.status) for r in records] == [
        ("VOYN-W0-A", "OPEN"),
        ("VOYN-W0-A", "DONE"),
    ]


def test_damaged_machine_records_are_skipped_not_half_read():
    """Same contract as the 0B records: exactly these keys in exactly this
    order, or it is not a record this module understands. A partially
    accepted line would put a wave in a status field and mislabel a card.

    Skipped rather than raised, like every read in this module — a damaged
    file degrades the read instead of taking the owner's page down."""
    good = (
        "- VOYN_TASK_STATUS | id=VOYN-W0-OK | wave=Wave 0 | status=DONE"
        " | priority=P0 | slug=ok\n"
    )
    damaged = (
        # a field short
        "- VOYN_TASK_STATUS | id=VOYN-W0-SHORT | wave=Wave 0 | status=DONE"
        " | priority=P0\n"
        # keys out of order
        "- VOYN_TASK_STATUS | wave=Wave 0 | id=VOYN-W0-SWAP | status=DONE"
        " | priority=P0 | slug=s\n"
        # not key=value
        "- VOYN_TASK_STATUS | VOYN-W0-BARE | Wave 0 | DONE | P0 | s\n"
        # the marker as prose, not a list item
        "VOYN_TASK_STATUS | id=VOYN-W0-PROSE | wave=Wave 0 | status=DONE"
        " | priority=P0 | slug=s\n"
    )
    records = bc.parse_rich_records(good + damaged)
    assert [r.record_id for r in records] == ["VOYN-W0-OK"]


def test_an_unknown_machine_status_is_surfaced_never_guessed():
    """A status outside the vocabulary reads as UNKNOWN on this surface for
    the same reason it does on the authored one: the machine-fields rule
    forbids guessing, and UNKNOWN is a value consumers already handle."""
    line = (
        "- VOYN_TASK_STATUS | id=VOYN-W0-X | wave=Wave 0 | status=TRIAGE_LATER"
        " | priority=P0 | slug=x\n"
    )
    assert bc.parse_rich_records(line)[0].status == "UNKNOWN"
