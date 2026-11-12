"""Stage 3 contract tests: voyn-task as a read-only source of ``backlog_task``.

Hermetic: no PostgreSQL, no psql, no network. The database side is a fake
``fetch_states`` callable or a patched ``subprocess.run``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from command_center.db import cli, voyn_task_source as src
from command_center.db.backlog_parser import parse_backlog

ROOT = Path(__file__).parent / "fixtures" / "voyn_task_source"
FIX = ROOT / "equivalence"
REAL = ROOT / "real" / "tasks"
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


# ---- stage 3b: dependencies, evidence, reviewer, no-wave, real-derived fixture


def _real():
    return src.build_report(src.load_definitions(REAL), {})


def test_real_fixture_is_closed_and_loses_nothing():
    defs = src.load_definitions(REAL)
    report = _real()
    assert len(defs) == 21
    assert len(report.tasks) + len(report.unparsed) == len(defs)
    mapped = {t.task_id for t in report.tasks}
    for task_id, deps in report.dependencies.items():
        assert set(deps) <= set(defs), f"{task_id} depends outside the fixture"
        assert set(deps) <= mapped | set(report.no_wave)


def test_real_fixture_gates_and_dependencies():
    report = _real()
    by_id = {t.task_id: t for t in report.tasks}
    gate = by_id["VOYN-W0-AICC-SRV-G0"]
    assert gate.kind == "gate"
    assert report.dependencies["VOYN-W0-AICC-SRV-G0"] == tuple(
        f"VOYN-W0-AICC-SRV-0{n}" for n in range(1, 10)
    )
    assert all(t.kind == "task" for t in report.tasks if not t.task_id.endswith(("-G0", "-G1")))


def test_real_fixture_blocked_tasks_map_to_needs_refinement():
    defs = src.load_definitions(REAL)
    native_blocked = {i for i, d in defs.items() if d.get("status") == "BLOCKED"}
    assert native_blocked
    by_id = {t.task_id: t for t in _real().tasks}
    for task_id in native_blocked & set(by_id):
        assert by_id[task_id].status == "NEEDS_REFINEMENT"


def test_reviewer_unassigned_is_not_a_reviewer():
    assert src.effective_reviewer("reviewer-unassigned") is None
    assert src.effective_reviewer("Reviewer-Unassigned") is None
    assert src.effective_reviewer("") is None
    assert src.effective_reviewer(None) is None
    assert src.effective_reviewer("qa_agent") == "qa_agent"
    report = _real()
    assert report.reviewers and all(v is None for v in report.reviewers.values())


def test_tasks_without_wave_are_reported_explicitly():
    report = _real()
    assert "VOYN-OPS-PYTEST-IMPACT-01" in report.no_wave
    assert "VOYN-OPS-PYTEST-IMPACT-01" not in {t.task_id for t in report.tasks}
    assert any(r.startswith("no wave") for _l, r, _e in report.unparsed)


def test_cli_prints_no_wave(monkeypatch, capsys):
    monkeypatch.setenv(src.FLAG_ENV, "1")
    monkeypatch.setenv(src.TASKS_DIR_ENV, str(REAL))
    monkeypatch.setattr(src, "fetch_states_psql", lambda env=None: {})
    assert cli.main(["backlog-import-voyn-task", "--parse-only"]) == 0
    out = capsys.readouterr().out
    assert "NO-WAVE VOYN-OPS-PYTEST-IMPACT-01" in out


def test_evidence_mapping_and_db_evidence_wins():
    defs = {
        "VOYN-X-1": {
            "id": "VOYN-X-1", "wave": "0", "status": "OPEN", "priority": "P1", "title": "t",
            "evidence": {"pr": "#1", "sha": "abcdef1", "test": "yaml proof"},
        },
        "VOYN-X-2": {
            "id": "VOYN-X-2", "wave": "0", "status": "OPEN", "priority": "P1", "title": "t",
            "evidence": {"pr": "#2"},
        },
    }
    states = {"VOYN-X-2": {"task_id": "VOYN-X-2", "status": "DONE", "revision": 1,
                           "evidence": {"pr": "#99", "test": "db proof"}}}
    report = src.build_report(defs, states)
    assert report.evidence["VOYN-X-1"] == (("acceptance", "pr: #1"), ("sha", "abcdef1"), ("acceptance", "yaml proof"))
    assert report.evidence["VOYN-X-2"] == (("acceptance", "pr: #99"), ("acceptance", "db proof"))


class _FakeStore:
    def __init__(self, existing=None):
        self.calls = []
        self.existing = existing or {}

    def get_task(self, task_id):
        return self.existing.get(task_id)

    def import_parsed(self, parsed):
        from command_center.db.backlog_store import ImportReport

        self.calls.append(("import", len(parsed.tasks)))
        self.imported = [t.task_id for t in parsed.tasks]
        return ImportReport(inserted=len(parsed.tasks))

    def add_dependency(self, task_id, dep):
        self.calls.append(("dep", task_id, dep))
        return (dep != "VOYN-BAD-DEP", "cycle", None)

    def record_evidence(self, task_id, kind, value):
        self.calls.append(("ev", task_id, kind, value))
        return True, ""


def test_apply_report_writes_tasks_then_dependencies_then_evidence():
    report = _real()
    store = _FakeStore()
    result = src.apply_report(store, report)
    kinds = [c[0] for c in store.calls]
    assert kinds[0] == "import"
    assert kinds.index("dep") > 0 and "ev" in kinds
    assert max(i for i, k in enumerate(kinds) if k == "dep") < min(i for i, k in enumerate(kinds) if k == "ev")
    assert ("dep", "VOYN-W0-AICC-SRV-G0", "VOYN-W0-AICC-SRV-01") in store.calls
    assert ("ev", "VOYN-OPS-CI-MANDATORY-GATES", "acceptance", "pr: #229") in store.calls
    assert result.refused == [] or all("not in the mapped set" in r for _t, r in result.refused)


def test_apply_report_reports_refused_dependency():
    defs = {
        "VOYN-X-1": {"id": "VOYN-X-1", "wave": "0", "status": "OPEN", "priority": "P1", "title": "t",
                     "depends_on": ["VOYN-BAD-DEP", "VOYN-MISSING-1"]},
        "VOYN-BAD-DEP": {"id": "VOYN-BAD-DEP", "wave": "0", "status": "OPEN", "priority": "P1", "title": "t"},
    }
    result = src.apply_report(_FakeStore(), src.build_report(defs, {}))
    reasons = " ".join(r for _t, r in result.refused)
    assert "VOYN-BAD-DEP: cycle" in reasons and "VOYN-MISSING-1: not in the mapped set" in reasons


def test_flag_off_cli_opens_neither_config_nor_pool(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("config/pool touched while the flag is off")

    monkeypatch.delenv(src.FLAG_ENV, raising=False)
    monkeypatch.setattr(cli, "load_config", boom)
    monkeypatch.setattr(cli.pool, "open_pool", boom)
    monkeypatch.setattr(cli.pool, "connection", boom)
    assert cli.main(["backlog-import-voyn-task"]) == 0
    assert cli.main(["backlog-import-voyn-task", "--parse-only"]) == 0


@pytest.mark.skipif(
    not os.environ.get("VOYN_BACKLOG_TASKS_DIR"),
    reason="VOYN_BACKLOG_TASKS_DIR (the real voyn-backlog tasks/ checkout) is not set",
)
def test_real_backlog_all_tasks_accounted_for():
    tasks_dir = Path(os.environ["VOYN_BACKLOG_TASKS_DIR"])
    files = [p for p in tasks_dir.glob("*.yaml") if p.name != "EXAMPLE.yaml"]
    defs = src.load_definitions(tasks_dir)
    assert len(defs) == len(files), "duplicate or id-less YAML files"
    report = src.build_report(defs, {})
    imported = {t.task_id for t in report.tasks}
    reported = {e for _l, _r, e in report.unparsed}
    assert imported | reported == set(defs)
    assert not imported & reported
    assert set(report.no_wave) <= reported
    for task_id, deps in report.dependencies.items():
        assert set(deps) <= set(defs)


# ---- review blockers: repo routing, existing rows untouched, psql hardening

SLICE = ROOT / "real_slice.json"


def _slice_defs():
    rows = json.loads(SLICE.read_text(encoding="utf-8"))
    return {
        r["id"]: {
            "id": r["id"], "project": r["project"], "status": r["status"], "wave": r["wave"],
            "priority": "P1", "title": r["id"], "depends_on": r["depends_on"], "gate": r["gate"],
        }
        for r in rows
    }


def test_slice_covers_all_real_tasks_without_loss():
    defs = _slice_defs()
    assert len(defs) == 647
    report = src.build_report(defs, {})
    assert len(report.tasks) == 640 and len(report.no_wave) == 7
    assert {t.task_id for t in report.tasks} | set(report.no_wave) == set(defs)
    mapped = {t.task_id for t in report.tasks}
    for task_id, deps in report.dependencies.items():
        assert set(deps) <= set(defs)
        assert task_id in mapped


def test_repo_follows_the_markdown_family_rule_not_yaml_project():
    from command_center.db.backlog_parser import infer_repo

    defs = _slice_defs()
    report = src.build_report(defs, {})
    for t in report.tasks:
        assert t.repo == infer_repo(t.task_id)
    # the coarse YAML ``project`` would route differently for most tasks:
    # that is exactly what the adapter must not use
    coarse = {"aicc": "ai-command-center", "crm": "voyn-logistics-crm", "aios": "aios"}
    assert sum(1 for t in report.tasks if coarse.get(defs[t.task_id]["project"]) != t.repo) > 100


def test_target_repo_hint_in_text_wins():
    defs = {"VOYN-OPS-X-1": {"id": "VOYN-OPS-X-1", "wave": "0", "status": "OPEN", "title": "t",
                             "priority": "P1", "project": "aios",
                             "why": "Target repo: `some-repo`."}}
    assert src.build_report(defs, {}).tasks[0].repo == "some-repo"


def test_existing_rows_are_not_rewritten_and_differences_are_reported():
    report = _real()
    by_id = {t.task_id: t for t in report.tasks}
    t = by_id["VOYN-W0-AICC-SRV-01"]
    existing = {
        t.task_id: {"task_id": t.task_id, "wave": t.wave, "priority": t.priority,
                    "status": "DEFER_TO_USER", "kind": t.kind, "title": "slug", "repo": "other-repo",
                    "revision": 5},
    }
    store = _FakeStore(existing)
    result = src.apply_report(store, report)
    assert t.task_id not in store.imported  # never upserted
    assert len(store.imported) == len(report.tasks) - 1
    assert result.existing == 1 and result.inserted == len(report.tasks) - 1
    fields = {(i, f) for i, f, *_ in result.differences}
    assert (t.task_id, "status") in fields and (t.task_id, "repo") in fields
    assert (t.task_id, "wave") not in fields
    assert not any(f == "title" for _i, f, *_ in result.differences)


def test_cli_prints_differences(monkeypatch, capsys):
    import contextlib

    monkeypatch.setenv(src.FLAG_ENV, "1")
    monkeypatch.setenv(src.TASKS_DIR_ENV, str(REAL))
    monkeypatch.setattr(src, "fetch_states_psql", lambda env=None: {})
    t = _real().tasks[0]
    store = _FakeStore({t.task_id: {"status": "UNTRIAGED", "repo": t.repo, "wave": t.wave,
                                     "priority": t.priority, "kind": t.kind}})
    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli.pool, "open_pool", lambda c: None)
    monkeypatch.setattr(cli.pool, "close_pool", lambda: None, raising=False)
    monkeypatch.setattr(cli.pool, "connection", lambda: contextlib.nullcontext(object()))
    import command_center.db.backlog_store as bs

    monkeypatch.setattr(bs, "BacklogStore", lambda factory: store)
    cli.main(["backlog-import-voyn-task"])
    out = capsys.readouterr().out
    assert f"DIFF {t.task_id} status: aicc=UNTRIAGED" in out and "(not written)" in out


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:pw%21@h:5432/d",
        "postgresql://u@h/d?password=pw!&sslmode=require",
        "host=h port=5432 dbname=d user=u password=pw!",
    ],
)
def test_password_never_in_argv(dsn):
    target, env = src._psql_target(dsn)
    assert "pw" not in target
    assert env == {"PGPASSWORD": "pw!"}


def test_child_env_drops_inherited_pg_and_is_read_only(monkeypatch):
    for k in ("PGPASSWORD", "PGHOST", "PGSERVICE", "VOYN_TASK_DSN"):
        monkeypatch.setenv(k, "leak")
    env = src._child_env({}, {"PGPASSWORD": "mine"})
    assert env["PGPASSWORD"] == "mine"
    assert "PGHOST" not in env and "PGSERVICE" not in env and "VOYN_TASK_DSN" not in env
    assert "default_transaction_read_only=on" in env["PGOPTIONS"]


def test_pr_evidence_only_when_full_github_url():
    defs = {"VOYN-X-1": {"id": "VOYN-X-1", "wave": "0", "status": "OPEN", "title": "t", "priority": "P1",
                         "evidence": {"pr": "#229", "sha": "10fc48f"}},
            "VOYN-X-2": {"id": "VOYN-X-2", "wave": "0", "status": "OPEN", "title": "t", "priority": "P1",
                         "evidence": {"pr": "https://github.com/o/r/pull/7"}}}
    report = src.build_report(defs, {})
    assert report.evidence["VOYN-X-1"] == (("acceptance", "pr: #229"), ("sha", "10fc48f"))
    assert report.evidence["VOYN-X-2"] == (("pr", "https://github.com/o/r/pull/7"),)
