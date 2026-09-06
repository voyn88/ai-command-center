"""Live read projection of the VOYN master backlog into ACC
(`command_center.backlog_client`).

The Backlog Engine is the single owner of the `Task` entity; ACC is only a
*reader* of its `VOYN_RECOMMENDATION` master store (engine plan invariant #5:
"Локальные tasks.json, очереди и UI-модели являются только проекциями"). These
tests pin that contract: the client parses the machine lines, never writes, and
degrades to an empty-but-usable projection when the master store is absent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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


def test_resolve_backlog_path_expands_tilde(monkeypatch):
    # A "~"-relative path is a natural thing to put in the env var; a literal
    # "~" path can never exist, which would otherwise silently read as
    # "not connected" instead of the misconfiguration it actually is.
    monkeypatch.delenv(bc.MASTER_BACKLOG_ENV, raising=False)
    home = Path.home()
    assert bc.resolve_backlog_path("~/VOYN_TASKS_BACKLOG.md") == home / "VOYN_TASKS_BACKLOG.md"
    monkeypatch.setenv(bc.MASTER_BACKLOG_ENV, "~/env-backlog.md")
    assert bc.resolve_backlog_path() == home / "env-backlog.md"


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


def test_broken_utf8_master_store_is_reported_not_raised(tmp_path):
    f = tmp_path / "broken.md"
    f.write_bytes(b"- VOYN_RECOMMENDATION | ts=\xff\xfe broken bytes")
    proj = bc.load_projection(f)  # must not raise UnicodeDecodeError
    assert proj.exists is False
    assert proj.records == []
    assert proj.read_error is not None


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permission bits",
)
@pytest.mark.skipif(sys.platform == "win32", reason="posix permission bits only")
def test_permission_denied_master_store_is_reported_not_raised(tmp_path):
    f = tmp_path / "secret.md"
    f.write_text(_REC, encoding="utf-8")
    f.chmod(0o000)
    try:
        proj = bc.load_projection(f)  # must not raise PermissionError
        assert proj.exists is False
        assert proj.records == []
        assert proj.read_error is not None
    finally:
        f.chmod(0o644)


def test_load_projection_has_no_toctou_gap_between_check_and_read(tmp_path):
    # Absence must be reported as "not connected", not surfaced as a crash,
    # even though the file existed a moment before the read (the pattern a
    # preceding `is_file()` check cannot protect against).
    f = tmp_path / "vanishes.md"
    f.write_text(_REC, encoding="utf-8")
    f.unlink()
    proj = bc.load_projection(f)
    assert proj.exists is False
    assert proj.read_error is None


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


def test_client_module_never_calls_a_filesystem_write_api():
    # A name-substring scan over `dir(bc)` only catches a *symbol* that sounds
    # like a writer; it would happily miss, say, a helper named `_persist`
    # that calls `Path.write_text` under the hood, or a bare `open(..., "w")`
    # inline in an existing function. Scan the actual source for the write
    # APIs a Path/file-based module could use instead.
    import inspect

    source = inspect.getsource(bc)
    write_calls = (
        "write_text(",
        "write_bytes(",
        ".mkdir(",
        ".rmdir(",
        ".unlink(",
        ".touch(",
        ".chmod(",
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "os.mkdir",
        "os.makedirs",
        "shutil.",
    )
    found = [call for call in write_calls if call in source]
    assert not found, f"backlog_client must stay read-only; found {found!r} in source"
    # Every current caller reads via `Path.read_text`; a bare `open(path, "w")`
    # would not trip any of the calls above, so flag `open(` outright too.
    assert "open(" not in source, "backlog_client must stay read-only; found open("


def test_summarize_counts_by_facet():
    draft = _REC.replace("PO-Approved", "AI-Reco").replace("priority=P0", "priority=P1")
    summary = bc.summarize(bc.parse_recommendations("\n".join([_REC, draft])))
    assert summary.total == 2
    assert summary.approved == 1
    # Every declared priority is listed, even at zero — a blank P2 column is
    # itself information (nothing urgent-but-not-critical is pending).
    assert summary.by_priority == {"P0": 1, "P1": 1, "P2": 0}
    assert summary.by_status == {"AI-Reco": 1, "PO-Approved": 1}
    assert summary.by_wave == {"W1": 2}


def test_wave_breakdown_and_queue_sort_naturally_not_lexicographically():
    # W10 must not land between W1 and W2 (plain string sort would do that).
    w1 = _REC.replace("VOYN-W1-UI", "A")
    w2 = _REC.replace("VOYN-W1-UI", "B").replace("proposed_wave=W1", "proposed_wave=W2")
    w10 = _REC.replace("VOYN-W1-UI", "C").replace("proposed_wave=W1", "proposed_wave=W10")
    records = bc.parse_recommendations("\n".join([w10, w1, w2])).records
    summary = bc.summarize(bc.Projection(records=records))
    assert list(summary.by_wave.keys()) == ["W1", "W2", "W10"]

    queue = bc.execution_queue(bc.Projection(records=records))
    assert [r.issue_id for r in queue] == ["A", "B", "C"]


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
    # Freshness/source surfaced.
    assert "master store" in [m.value for m in at.metric]
    assert body  # smoke


def test_page_explains_when_backlog_not_connected(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, tmp_path / "missing.md")
    assert not at.exception
    assert any(bc.MASTER_BACKLOG_ENV in str(w.value) for w in at.warning)
    # An unconnected page must not have rendered the records table metrics.
    assert "Всего записей" not in [m.label for m in at.metric]


def test_page_reports_unreadable_source_instead_of_crashing(monkeypatch, tmp_path):
    f = tmp_path / "broken.md"
    f.write_bytes(b"- VOYN_RECOMMENDATION | ts=\xff\xfe broken")
    at = _run_page(monkeypatch, f)
    assert not at.exception  # the old bug let UnicodeDecodeError reach the page
    assert at.error  # surfaced as an explicit error, not silence or a crash
    assert any(str(f) in str(e.value) for e in at.error)
    # Must not be conflated with the plain "not connected" warning copy.
    assert not any(bc.MASTER_BACKLOG_ENV in str(w.value) for w in at.warning)


def test_page_queue_table_matches_the_execution_queue(monkeypatch, tmp_path):
    p1 = _REC.replace("VOYN-W1-UI", "SECOND").replace("priority=P0", "priority=P1")
    f = tmp_path / "b.md"
    f.write_text("\n".join([_REC, p1]), encoding="utf-8")
    at = _run_page(monkeypatch, f)
    assert not at.exception
    queue_rows = at.dataframe[0].value
    assert list(queue_rows["id"]) == ["VOYN-W1-UI", "SECOND"]  # P0 before P1
    assert list(queue_rows["приоритет"]) == ["P0", "P1"]


def test_page_truncates_queue_table_with_a_visible_notice(monkeypatch, tmp_path):
    many = "\n".join(
        _REC.replace("VOYN-W1-UI", f"VOYN-W1-{i:03d}") for i in range(60)
    )
    f = tmp_path / "b.md"
    f.write_text(many, encoding="utf-8")
    at = _run_page(monkeypatch, f)
    assert not at.exception
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["В очереди исполнения"] == "60"  # the full count
    assert len(at.dataframe[0].value) == 50  # display cap
    assert any("50" in str(c.value) and "60" in str(c.value) for c in at.caption)


def test_page_breakdown_tables_match_summary_counts(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, _backlog_fixture(tmp_path))
    assert not at.exception
    # _backlog_fixture writes 2 records (both wave=W1, priority=P0, domain=ux),
    # one PO-Approved and one AI-Reco draft.
    # Column order: wave, priority, status, domain (see render_master_backlog_page).
    wave_table, priority_table, status_table, domain_table = at.table
    assert dict(zip(wave_table.value[""], wave_table.value["n"])) == {"W1": 2}
    assert list(priority_table.value[""]) == ["P0", "P1", "P2"]  # declared order, incl. zero
    assert list(priority_table.value["n"]) == [2, 0, 0]
    assert dict(zip(status_table.value[""], status_table.value["n"])) == {
        "AI-Reco": 1,
        "PO-Approved": 1,
    }
    assert dict(zip(domain_table.value[""], domain_table.value["n"])) == {"ux": 2}


def test_page_search_and_facet_filters_narrow_the_records_table(monkeypatch, tmp_path):
    other = (
        _REC.replace("VOYN-W1-UI", "OTHER")
        .replace("task=build_dashboard_desktop_on_api_and_tokens", "task=fix_login_bug")
        .replace("parallel_domain=ux", "parallel_domain=api")
    )
    f = tmp_path / "b.md"
    f.write_text("\n".join([_REC, other]), encoding="utf-8")
    at = _run_page(monkeypatch, f)
    assert not at.exception
    assert len(at.dataframe[1].value) == 2  # unfiltered: both records

    at.text_input(key="mb_query").set_value("login").run()
    assert len(at.dataframe[1].value) == 1
    assert at.dataframe[1].value["id"].iloc[0] == "OTHER"
    assert "Показано 1 из 2" in at.caption[-1].value

    at.text_input(key="mb_query").set_value("").run()
    at.selectbox(key="mb_domain").select("ux").run()  # select() takes the raw value
    assert list(at.dataframe[1].value["id"]) == ["VOYN-W1-UI"]


def test_page_shows_parse_error_disclosure_expander(monkeypatch, tmp_path):
    bad = "- VOYN_RECOMMENDATION | ts=x | garbage-not-enough-fields"
    f = tmp_path / "b.md"
    f.write_text("\n".join([_REC, bad]), encoding="utf-8")
    at = _run_page(monkeypatch, f)
    assert not at.exception
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Ошибок парсинга"] == "1"
    assert len(at.expander) == 1
    expander_text = " ".join(str(t.value) for t in at.expander[0].text)
    assert "стр. 2" in expander_text


def test_page_hides_error_expander_when_nothing_failed_to_parse(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, _backlog_fixture(tmp_path))
    assert not at.exception
    assert at.expander == []


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
