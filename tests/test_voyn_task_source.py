"""Stage 3 contract tests: voyn-task as a read-only source of ``backlog_task``.

Hermetic: no PostgreSQL, no psql, no network. The database side is a fake
``fetch_states`` callable or a patched ``subprocess.run``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center.db import cli, voyn_task_source as src
from command_center.db.backlog_parser import parse_backlog

FIX = Path(__file__).parent / "fixtures" / "voyn_task_source"
FIELDS = ("task_id", "wave", "priority", "status", "kind", "title", "repo")


def _states():
    return {r["task_id"]: r for r in json.loads((FIX / "states.json").read_text())}


def _key(task):
    return tuple(getattr(task, f) for f in FIELDS)


def test_same_tasks_give_same_backlog_task_records_as_markdown():
    md = parse_backlog((FIX / "backlog.md").read_text(encoding="utf-8"))
    report = src.build_report(src.load_definitions(FIX / "tasks"), _states())
    assert md.unparsed == [] and report.unparsed == []
    assert sorted(map(_key, report.tasks)) == sorted(map(_key, md.tasks))


@pytest.mark.parametrize(
    ("native", "mapped"),
    [
        ("OPEN", "OPEN"),
        ("IN_PROGRESS", "IN_PROGRESS"),
        ("READY_TO_REVIEW", "READY_TO_REVIEW"),
        ("DONE", "DONE"),
        ("BLOCKED", "NEEDS_REFINEMENT"),
    ],
)
def test_status_table(native, mapped):
    defs = {"VOYN-X-1": {"id": "VOYN-X-1", "wave": "0", "priority": "P1", "title": "t", "project": "aicc"}}
    out = src.build_report(defs, {"VOYN-X-1": {"task_id": "VOYN-X-1", "status": native, "revision": 7}})
    assert out.tasks[0].status == mapped
    assert out.revisions == {"VOYN-X-1": 7}
    # every mapped value is in the backlog_task vocabulary (no invented status)
    from command_center.db.backlog_parser import STATUSES

    assert mapped in STATUSES


def test_triage_statuses_are_never_produced():
    produced = set(src.STATUS_MAP.values())
    assert not produced & {"UNTRIAGED", "DEFER_TO_USER", "SPLIT", "DECIDED"}


def test_blocked_keeps_original_status_in_body():
    report = src.build_report(src.load_definitions(FIX / "tasks"), _states())
    blocked = next(t for t in report.tasks if t.task_id == "VOYN-W0-A6")
    assert "[voyn-task status: BLOCKED]" in blocked.body


def test_db_status_wins_and_unregistered_falls_back_to_yaml():
    defs = src.load_definitions(FIX / "tasks")
    states = _states()
    del states["VOYN-W0-A1"]
    states["VOYN-GHOST-1"] = {"task_id": "VOYN-GHOST-1", "status": "OPEN", "revision": 0}
    report = src.build_report(defs, states)
    assert report.unregistered == ["VOYN-W0-A1"]
    assert report.orphan_states == ["VOYN-GHOST-1"]
    assert next(t for t in report.tasks if t.task_id == "VOYN-W0-A1").status == "OPEN"


def test_bad_definitions_are_reported_not_guessed():
    defs = {
        "VOYN-X-1": {"id": "VOYN-X-1", "wave": "Q", "priority": "P1", "title": "t"},
        "VOYN-X-2": {"id": "VOYN-X-2", "wave": "0", "priority": "high", "title": "t"},
        "VOYN-X-3": {"id": "VOYN-X-3", "wave": "0", "priority": "P1", "title": "t", "status": "WAT"},
    }
    report = src.build_report(defs, {})
    assert report.tasks == []
    assert len(report.unparsed) == 3


@pytest.mark.parametrize("value", [None, "", "0", "false", "off", "no"])
def test_flag_defaults_off(value):
    env = {} if value is None else {src.FLAG_ENV: value}
    assert src.is_enabled(env) is False


def test_flag_on_values():
    assert src.is_enabled({src.FLAG_ENV: "1"}) and src.is_enabled({src.FLAG_ENV: "true"})


def test_flag_off_reads_nothing(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):  # any DB / process / directory access fails the test
        raise AssertionError("voyn-task was read while the flag is off")

    monkeypatch.delenv(src.FLAG_ENV, raising=False)
    monkeypatch.setattr(src.subprocess, "run", boom)
    monkeypatch.setattr(src, "load_definitions", boom)
    monkeypatch.setattr(src, "fetch_states_psql", boom)
    with pytest.raises(src.SourceError):
        src.load_from_voyn_task({}, fetch_states=boom)
    # CLI path: returns 0, prints that nothing was read, never loads DB config
    monkeypatch.setattr(cli, "load_config", boom)
    assert cli.main(["backlog-import-voyn-task"]) == 0
    assert "nothing read" in capsys.readouterr().out


def test_flag_on_parse_only_uses_fake_db(monkeypatch, capsys):
    monkeypatch.setenv(src.FLAG_ENV, "1")
    monkeypatch.setenv(src.TASKS_DIR_ENV, str(FIX / "tasks"))
    monkeypatch.setattr(src, "fetch_states_psql", lambda env=None: _states())
    monkeypatch.setattr(cli, "load_config", lambda: (_ for _ in ()).throw(AssertionError("AICC DB opened")))
    assert cli.main(["backlog-import-voyn-task", "--parse-only"]) == 0
    assert "mapped: 6 tasks" in capsys.readouterr().out


def test_psql_password_goes_to_env_not_argv(monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "[]", ""

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"], input=kw["input"])
        return R()

    monkeypatch.setattr(src.subprocess, "run", fake_run)
    src.fetch_states_psql({"VOYN_TASK_READER_DSN": "postgresql://voyn_task_reader:s3cr%40t@db:5432/x"})
    assert "s3cr" not in " ".join(seen["cmd"])
    assert seen["env"]["PGPASSWORD"] == "s3cr@t"
    assert seen["input"].lower().lstrip().startswith("select")
    for word in ("insert", "update", "delete", "transition", "claim", "register"):
        assert word not in seen["input"].lower()


def test_db_error_does_not_leak_dsn(monkeypatch):
    class R:
        returncode, stdout, stderr = 2, "", "psql: error: connection to server failed"

    monkeypatch.setattr(src.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(src.SourceError) as err:
        src.fetch_states_psql({"VOYN_TASK_DSN": "postgresql://u:topsecret@h/db"})
    assert "topsecret" not in str(err.value)


def test_missing_dsn_names_variables_only():
    with pytest.raises(src.SourceError) as err:
        src.fetch_states_psql({})
    assert "VOYN_TASK_READER_DSN" in str(err.value)
