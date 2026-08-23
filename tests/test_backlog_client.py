"""Live read projection of the VOYN master backlog into ACC
(`command_center.backlog_client`).

The Backlog Engine is the single owner of the `Task` entity; ACC is only a
*reader* of its `VOYN_RECOMMENDATION` master store (engine plan invariant #5:
"Локальные tasks.json, очереди и UI-модели являются только проекциями"). These
tests pin that contract: the client parses the machine lines, never writes, and
degrades to a reported projection — empty-but-usable when the master store is
absent, `read_error` when it is there and unreadable — instead of raising.

The page built on it is covered in `test_master_backlog_panel.py`.
"""

from __future__ import annotations

import errno
import os

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


# --- The store is there and cannot be read ---------------------------------
#
# The projection is re-read live while the Backlog Engine rewrites the same file,
# so these are operating states, not corrupt-input trivia. Each one used to raise
# out of `load_projection` and take the page down with a traceback.


def test_non_utf8_store_is_reported_not_raised(tmp_path):
    # A torn read of a half-written store, or one saved as cp1251 — the master
    # store is authored in Russian, so a bad byte is a byte, not a hypothetical.
    f = tmp_path / "torn.md"
    f.write_bytes("- VOYN_RECOMMENDATION | задача".encode("cp1251"))
    proj = bc.load_projection(f)
    assert proj.read_error is not None
    assert "UTF-8" in proj.read_error
    assert proj.exists is True, "the store is present; it is the read that failed"
    assert proj.records == []
    # Freshness survives the failure: it is the evidence for "read mid-write".
    assert proj.source_mtime == pytest.approx(f.stat().st_mtime)


def test_unreadable_store_is_never_a_silently_empty_backlog(tmp_path):
    f = tmp_path / "torn.md"
    f.write_bytes(b"\xff\xfe not utf-8")
    proj = bc.load_projection(f)
    # The one confusion this state must not allow: zero records reading as
    # "the master backlog is empty".
    assert bc.summarize(proj).total == 0 and proj.read_error is not None
    assert bc.execution_queue(proj) == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads through mode 000, so the failure cannot be provoked",
)
def test_permission_denied_store_is_reported_not_raised(tmp_path):
    f = tmp_path / "locked.md"
    f.write_text(_REC, encoding="utf-8")
    f.chmod(0o000)
    try:
        proj = bc.load_projection(f)
    finally:
        f.chmod(0o600)  # so tmp_path teardown can remove it
    assert proj.exists is True
    # `strerror` comes from libc and is localised under a non-C LC_MESSAGES, so
    # pin the errno rather than the English sentence.
    assert proj.read_error and proj.read_error == os.strerror(errno.EACCES)


def test_a_directory_is_reported_unreadable_rather_than_missing(tmp_path):
    # Misconfiguration pointing at the containing folder: "not found" would send
    # the operator looking for a file that is right there.
    proj = bc.load_projection(tmp_path)
    assert proj.exists is True
    assert proj.read_error is not None
    assert proj.records == []


def test_a_broken_symlink_is_missing_not_unreadable(tmp_path):
    link = tmp_path / "backlog.md"
    link.symlink_to(tmp_path / "gone.md")
    proj = bc.load_projection(link)
    assert proj.exists is False, "nothing is there — this is 'not connected'"
    assert proj.read_error is None


def test_a_good_read_carries_no_read_error(tmp_path):
    f = tmp_path / "b.md"
    f.write_text(_REC, encoding="utf-8")
    assert bc.load_projection(f).read_error is None


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
