"""The control-plane tick journal.

VOYN-W0-AICC-PR-WINDOW-TIMER-NOT-DEPLOYED-ON-CONTROL. control-01 ran no
PR-window timer for over a day: 67 open PRs (752-822) carried no queue label
and operators labelled them by hand. Nothing on the host recorded that the
tick had or had not run, so "never installed" and "installed, nothing to do"
were the same observation. These tests pin the record that tells them apart.
"""

from __future__ import annotations

import json

import pytest

from command_center.ops import tick_journal


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Point the journal at a temporary file, the way a unit's env does."""
    path = tmp_path / "ticks.jsonl"
    monkeypatch.setenv(tick_journal.JOURNAL_PATH_ENV, str(path))
    return path


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_run_is_one_line_carrying_its_own_outcome(journal) -> None:
    assert tick_journal.record_tick("pr-window", "ok", detail={"active": 5}) is True

    (row,) = _rows(journal)
    assert row["tick"] == "pr-window"
    assert row["outcome"] == "ok"
    assert row["active"] == 5
    assert row["at"].endswith("+00:00"), "the timestamp must be unambiguous UTC"


def test_a_failed_tick_is_recorded_too(journal) -> None:
    """The case the journal exists for. A tick that only journalled its
    successes would look exactly like an uninstalled timer on the days it
    failed -- which is the whole confusion this record removes."""
    tick_journal.record_tick("pr-window", "failed", detail={"error": "pr_list_failed"})

    (row,) = _rows(journal)
    assert row["outcome"] == "failed"
    assert row["error"] == "pr_list_failed"


def test_runs_append_rather_than_replace(journal) -> None:
    for index in range(3):
        tick_journal.record_tick("pr-window", "ok", detail={"active": index})

    assert [row["active"] for row in _rows(journal)] == [0, 1, 2]


def test_the_journal_owns_its_identity_fields(journal) -> None:
    """A tick cannot relabel its own row: a detail key that collides with
    `at`/`tick`/`outcome` is dropped, not merged, so no caller can make a
    failed run read as a successful one."""
    tick_journal.record_tick(
        "pr-window", "failed", detail={"outcome": "ok", "tick": "other", "at": "never"}
    )

    (row,) = _rows(journal)
    assert (row["outcome"], row["tick"]) == ("failed", "pr-window")
    assert row["at"] != "never"


def test_a_huge_detail_value_cannot_make_an_unreadable_row(journal) -> None:
    """A GitHub error body is unbounded; a journal line must not be."""
    tick_journal.record_tick("pr-window", "failed", detail={"error": "x" * 10_000})

    (row,) = _rows(journal)
    assert len(row["error"]) == tick_journal._DETAIL_VALUE_CAP


def test_an_unwritable_journal_is_reported_and_never_raised(
    tmp_path, monkeypatch
) -> None:
    """Evidence, not a gate: a tick that cannot write its row still did its
    work, and journald still carries its stdout. Raising here would turn a
    full disk into a labelling outage -- the exact failure being fixed."""
    monkeypatch.setenv(
        tick_journal.JOURNAL_PATH_ENV, str(tmp_path / "ticks.jsonl" / "nested.jsonl")
    )
    (tmp_path / "ticks.jsonl").write_text("not a directory")

    assert tick_journal.record_tick("pr-window", "ok") is False


def test_an_unserialisable_detail_degrades_instead_of_raising(journal) -> None:
    tick_journal.record_tick("pr-window", "ok", detail={"quota": object()})

    assert _rows(journal)[0]["quota"].startswith("<object")


def test_the_last_run_is_the_last_run_of_that_tick(journal) -> None:
    tick_journal.record_tick("pr-window", "ok", detail={"active": 1})
    tick_journal.record_tick("self-deploy", "ok", detail={"active": 99})
    tick_journal.record_tick("pr-window", "failed", detail={"active": 2})

    last = tick_journal.read_last_run("pr-window")
    assert last is not None and last["outcome"] == "failed" and last["active"] == 2


def test_a_torn_line_invalidates_that_row_and_not_the_journal(journal) -> None:
    """A tick killed mid-write leaves a partial line. The rows before it are
    still evidence and must survive it."""
    tick_journal.record_tick("pr-window", "ok", detail={"active": 7})
    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"tick": "pr-window", "outc')

    last = tick_journal.read_last_run("pr-window")
    assert last is not None and last["active"] == 7


def test_no_recorded_run_is_an_answer_and_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(tick_journal.JOURNAL_PATH_ENV, str(tmp_path / "absent.jsonl"))

    assert tick_journal.read_last_run("pr-window") is None


def test_the_default_path_needs_no_host_layout(monkeypatch) -> None:
    """The reconciler was undeployable because its unit named a host layout
    nothing on the control plane had. The journal must not reintroduce one:
    the default is under the running user's own home, and the env var is an
    override rather than a requirement."""
    monkeypatch.delenv(tick_journal.JOURNAL_PATH_ENV, raising=False)

    assert tick_journal.journal_path().is_absolute()
    assert not tick_journal.DEFAULT_JOURNAL_PATH.startswith("/")
    assert tick_journal.journal_path("/tmp/explicit.jsonl").name == "explicit.jsonl"
