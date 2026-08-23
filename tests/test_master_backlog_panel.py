"""UI coverage for the Master Backlog page
(`command_center.ui.master_backlog_panel`).

The pure projection is covered in `test_backlog_client.py`; here we drive the page
itself through Streamlit `AppTest`. The page's whole job is to report the master
store faithfully, so these tests are mostly about the ways it could report *less*
than the store holds: an unreadable store must not render as an empty backlog,
unparsable lines must be visible, a truncated queue must say so, and the
search/facet widgets must actually be wired to the filter they claim to drive —
none of which the two render-smoke tests this file inherited could see.

The read-only contract gets its own test: the page has no widget that writes, and
a regression that added one would otherwise pass every count assertion here.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from command_center import backlog_client as bc


def _rec(
    issue_id: str,
    *,
    status: str = "PO-Approved",
    priority: str = "P0",
    wave: str = "W1",
    domain: str = "ux",
    task: str = "build_dashboard_desktop_on_api_and_tokens",
) -> str:
    """One machine record in the section-0B wire format (14 fields)."""
    return (
        f"- VOYN_RECOMMENDATION | ts=2026-08-12T17:00:00Z | status={status} | "
        f"issue_id={issue_id} | current_wave=W1 | proposed_wave={wave} | "
        f"priority={priority} | owner=frontend | effect=high | effort=L | "
        f"acceptance=accept:no_duplicate_screens | task={task} | "
        f"evidence=file:command_center/api | file_scope=command_center/ui | "
        f"parallel_domain={domain}"
    )


# The canonical *spec template* from section 0B — prose, never a record.
_TEMPLATE = (
    "`VOYN_RECOMMENDATION | ts=<ISO8601> | status=<AI-Reco|PO-Review|PO-Approved> "
    "| issue_id=<ID|NEW-####> | ...`"
)

#: Three records that differ in every facet, so a filter that is wired to the
#: wrong field cannot accidentally return the right rows.
_RECORDS = [
    _rec("VOYN-W1-UI"),
    _rec("VOYN-W2-API", priority="P1", wave="W2", domain="api", task="fix_login_bug"),
    _rec("DRAFT-1", status="AI-Reco", priority="P2", wave="W3", domain="ops"),
]


def _store(tmp_path, lines=None, name="VOYN_TASKS_BACKLOG.md"):
    f = tmp_path / name
    f.write_text("\n".join(lines if lines is not None else [_TEMPLATE, *_RECORDS]), "utf-8")
    return f


def _page_script() -> None:
    # Re-exec'd standalone by AppTest: path comes from the env var, not a closure.
    import os

    from command_center.ui import master_backlog_panel

    master_backlog_panel.render_master_backlog_page(os.environ.get("AICC_MASTER_BACKLOG"))


def _run_page(monkeypatch, path) -> AppTest:
    monkeypatch.setenv("AICC_MASTER_BACKLOG", str(path))
    return AppTest.from_function(_page_script, default_timeout=30).run()


def _metrics(at) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def _text(at) -> str:
    """Every rendered string, for asserting on prose without pinning a widget."""
    parts = []
    for block in (at.markdown, at.caption, at.warning, at.error, at.info, at.title):
        parts.extend(str(el.value) for el in block)
    return " ".join(parts)


# --- Connected, good read ---------------------------------------------------


def test_page_renders_connected_projection_with_counts(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, _store(tmp_path))
    assert not at.exception
    assert "Master Backlog" in " ".join(str(t.value) for t in at.title)
    # Read-only / master authority labelling is present.
    assert any("read-only" in str(c.value).lower() for c in at.caption)
    assert any("master" in str(i.value).lower() for i in at.info)
    metrics = _metrics(at)
    assert metrics["Всего записей"] == "3", "the template line is prose, not a record"
    assert metrics["Approved"] == "2"
    assert metrics["В очереди исполнения"] == "2"
    assert metrics["Ошибок парсинга"] == "0"
    assert "master store" in [m.value for m in at.metric]


def test_page_execution_queue_is_approved_only_and_priority_ordered(
    monkeypatch, tmp_path
):
    at = _run_page(monkeypatch, _store(tmp_path))
    queue = at.dataframe[0].value
    assert list(queue["id"]) == ["VOYN-W1-UI", "VOYN-W2-API"], "P0 first, no AI-Reco"


def test_page_rows_table_is_read_only_and_shows_every_record(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, _store(tmp_path))
    rows = at.dataframe[1].value
    assert len(rows) == 3, "the rows table shows drafts too, not just the queue"
    assert set(rows["read_only"]) == {True}
    assert set(rows["source"]) == {"master_backlog"}


def test_page_has_no_write_widget(monkeypatch, tmp_path):
    # ACC is a reader of the master store (engine invariant #5). Search and the
    # four facets are the only inputs this page may ever grow.
    at = _run_page(monkeypatch, _store(tmp_path))
    assert at.button.values == []
    assert len(at.text_input) == 1 and at.text_input[0].label == "Поиск"
    assert [s.label for s in at.selectbox] == ["Волна", "Приоритет", "Статус", "Домен"]


# --- The filters are actually wired ----------------------------------------


def test_page_search_narrows_the_rows_table(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, _store(tmp_path))
    at.text_input(key="mb_query").set_value("login").run()
    assert not at.exception
    assert list(at.dataframe[1].value["id"]) == ["VOYN-W2-API"]
    assert "Показано 1 из 3" in _text(at)


def test_page_facets_filter_by_their_own_field(monkeypatch, tmp_path):
    # Each record differs in every facet, so selecting "W2" can only produce the
    # right row if the wave facet really filters on wave.
    at = _run_page(monkeypatch, _store(tmp_path))
    at.selectbox(key="mb_wave").set_value("W2").run()
    assert list(at.dataframe[1].value["id"]) == ["VOYN-W2-API"]

    at.selectbox(key="mb_wave").set_value("Все").run()
    at.selectbox(key="mb_status").set_value("AI-Reco").run()
    assert list(at.dataframe[1].value["id"]) == ["DRAFT-1"]


def test_page_facets_and_search_are_combined_not_alternatives(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, _store(tmp_path))
    at.selectbox(key="mb_domain").set_value("ux").run()
    at.text_input(key="mb_query").set_value("login").run()  # only exists in domain=api
    assert not at.exception
    assert len(at.dataframe[1].value) == 0
    assert "Показано 0 из 3" in _text(at)


# --- Under-showing the store is always stated ------------------------------


def test_page_reports_an_unreadable_store_instead_of_crashing(monkeypatch, tmp_path):
    # A read that lands on a half-written store: the page must survive it. Before
    # this, `read_text` raised straight through and the page died on a traceback.
    torn = tmp_path / "VOYN_TASKS_BACKLOG.md"
    torn.write_bytes("- VOYN_RECOMMENDATION | задача".encode("cp1251"))
    at = _run_page(monkeypatch, torn)
    assert not at.exception
    assert at.error, "an unreadable store is an error, not a quiet empty page"
    assert "UTF-8" in " ".join(str(e.value) for e in at.error)
    assert str(torn) in _text(at), "the operator needs the path to go look"


def test_page_does_not_render_zero_counts_for_an_unreadable_store(
    monkeypatch, tmp_path
):
    # The failure this whole state exists to prevent: "Всего записей 0" for a
    # store that could not be read is a lie about the master backlog.
    torn = tmp_path / "VOYN_TASKS_BACKLOG.md"
    torn.write_bytes(b"\xff\xfe not utf-8")
    at = _run_page(monkeypatch, torn)
    assert "Всего записей" not in _metrics(at)
    assert at.dataframe.values == []


def test_page_distinguishes_unreadable_from_not_connected(monkeypatch, tmp_path):
    torn = tmp_path / "VOYN_TASKS_BACKLOG.md"
    torn.write_bytes(b"\xff\xfe")
    at = _run_page(monkeypatch, torn)
    # Not the "connect the store" guide: the store *is* connected.
    assert not any(bc.MASTER_BACKLOG_ENV in str(w.value) for w in at.warning)


def test_page_explains_when_backlog_not_connected(monkeypatch, tmp_path):
    at = _run_page(monkeypatch, tmp_path / "missing.md")
    assert not at.exception
    assert any(bc.MASTER_BACKLOG_ENV in str(w.value) for w in at.warning)
    # An unconnected page must not have rendered the records table metrics.
    assert "Всего записей" not in _metrics(at)


def test_page_surfaces_lines_it_could_not_parse(monkeypatch, tmp_path):
    bad = "- VOYN_RECOMMENDATION | ts=2026 | status=PO-Approved"  # too few fields
    at = _run_page(monkeypatch, _store(tmp_path, [*_RECORDS, bad]))
    assert _metrics(at)["Ошибок парсинга"] == "1"
    assert len(at.expander) == 1
    assert "стр. 4" in " ".join(str(t.value) for t in at.expander[0].text)


def test_page_says_when_the_execution_queue_is_truncated(monkeypatch, tmp_path):
    from command_center.ui import master_backlog_panel

    cap = master_backlog_panel.QUEUE_PREVIEW_ROWS
    approved = [_rec(f"VOYN-Q{n:03d}") for n in range(cap + 3)]
    at = _run_page(monkeypatch, _store(tmp_path, approved))
    assert _metrics(at)["В очереди исполнения"] == str(cap + 3)
    assert len(at.dataframe[0].value) == cap
    # The count and the table disagree on purpose — the page has to admit it.
    assert f"Показаны первые {cap} из {cap + 3}" in _text(at)


@pytest.mark.parametrize("mode", ["directory", "empty"])
def test_page_survives_degenerate_stores(monkeypatch, tmp_path, mode):
    target = tmp_path if mode == "directory" else _store(tmp_path, [])
    at = _run_page(monkeypatch, target)
    assert not at.exception
    if mode == "directory":
        assert at.error
    else:
        assert _metrics(at)["Всего записей"] == "0"
