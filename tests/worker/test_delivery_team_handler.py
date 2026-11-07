"""Native role work must stay read-only and retain complete validated output."""
import json
import threading

import pytest

from command_center import agent_runner
from command_center.orchestrator.delivery_team import build_payload
from command_center.worker import handlers
from command_center.worker.payloads import PayloadError, parse_agent_run
pytest_plugins = ("tests.worker.test_handlers",)

META = {"task_id": "VOYN-TEAM-1", "revision": 1, "stage": "ba", "input_digest": "a" * 64}


def payload():
    return build_payload({**META, "title": "Outcome", "body": "Criteria", "context": {}}, ("proj", "/tmp/repo"), 120)


@pytest.mark.parametrize("change", [
    {"task_type": "implementation"},
    {"backlog_task_id": "VOYN-TEAM-1"},
    {"cascade": []},
    {"cascade": [{"executor": "grok", "task_type": "review"}]},
    {"cascade": [{"executor": "codex", "task_type": "implementation"}]},
    {"delivery_team": {**META, "stage": "invented"}},
    {"delivery_team": {**META, "revision": True}},
])
def test_bad_capability_or_metadata_never_runs(change):
    assert isinstance(parse_agent_run({**payload(), **change}), PayloadError)


def run_with_output(handler, monkeypatch, text):
    invoke, _ = handler
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        return agent_runner.RunResult(status="completed", exit_code=0,
            stdout=json.dumps({"result": text}), stderr="", duration_seconds=1,
            started_at="2026-11-04T00:00:00Z", completed_at="2026-11-04T00:00:01Z")

    monkeypatch.setattr(agent_runner, "run_claude_code", runner)
    monkeypatch.setattr(handlers, "_executor_preflight", lambda *a, **k: (True, "ok", ""))
    return invoke(payload(), threading.Event()), calls


def test_complete_structured_result_survives_tail_and_operator_override(handler, monkeypatch):
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-alternatives")
    value = {"schema": "delivery-team-review/v1", **META, "verdict": "blocked",
             "criteria": [{"id": "C1", "description": "Expected outcome"}],
             "evidence": [], "blockers": ["Owner decision missing"], "summary": "x" * 20000}
    outcome, calls = run_with_output(handler, monkeypatch, json.dumps(value))
    assert outcome.ok
    assert outcome.result["delivery_team_result"] == value
    assert len(outcome.result["result_text"]) < len(json.dumps(value))
    assert calls and calls[0]["task_type"] == "review"
    assert outcome.result["executor"] in {"codex", "claude"}


def test_malformed_success_is_nonretryable_not_stage_pass(handler, monkeypatch):
    outcome, _ = run_with_output(handler, monkeypatch, "All good")
    assert not outcome.ok and not outcome.retryable
    assert "invalid delivery-team result" in outcome.reason


def post_code_payload():
    return build_payload({**META, "stage":"independent_reviewer", "title":"Review", "body":"Verify", "context":{"source_sha":"b"*40}}, ("proj","/tmp/repo"),120)


def test_post_code_requires_explicit_commit_pin():
    p=post_code_payload()
    assert parse_agent_run(p).delivery_team_source_sha == "b"*40
    for pin in [None,"main","https://example.com/repo", "b"*39]:
        assert isinstance(parse_agent_run(dict(p,delivery_team_source_sha=pin)),PayloadError)
    assert isinstance(parse_agent_run(dict(p,review_head={"pr_number":"1","head_sha":"b"*40})),PayloadError)


def test_unisolated_post_code_fails_before_model(handler,monkeypatch):
    invoke,calls=handler
    monkeypatch.setattr(agent_runner,"principal_isolation_required",lambda:False)
    monkeypatch.setattr(handlers,"_executor_preflight",lambda *a,**k:(True,"ok",""))
    outcome=invoke(post_code_payload(),threading.Event())
    assert not outcome.ok and not outcome.retryable and not calls
    assert "requires principal isolation" in outcome.reason


@pytest.mark.parametrize("actual",["a"*40,"b"*40])
def test_commit_pin_observed_not_agent_claim(handler,monkeypatch,tmp_path,actual):
    from subprocess import CompletedProcess
    invoke,calls=handler
    checkout=tmp_path/'pinned'
    monkeypatch.setattr(agent_runner,"principal_isolation_required",lambda:True)
    monkeypatch.setattr(handlers,"_executor_preflight",lambda *a,**k:(True,"ok",""))
    monkeypatch.setattr(handlers,"_refresh_read_only_source",lambda *a,**k:(True,None))
    pins=[]
    def pinned(repository,*,pin_sha=None):
        pins.append(pin_sha)
        return checkout,None,False
    monkeypatch.setattr(handlers,"_read_only_isolated_checkout",pinned)
    monkeypatch.setattr(handlers,"_remove_read_only_isolated_checkout",lambda *a:None)
    monkeypatch.setattr(agent_runner,"_run_git",lambda *a,**k:CompletedProcess([],0,stdout=actual+'\n',stderr=''))
    meta=dict(META,stage="independent_reviewer")
    value={"schema":"delivery-team-review/v1",**meta,"verdict":"blocked","criteria":[{"id":"C1","description":"Need decision"}],"evidence":[],"blockers":["Decision required"],"summary":"Inspected pinned source"}
    def runner(**kwargs):
        calls.append(kwargs)
        return agent_runner.RunResult(status="completed",exit_code=0,stdout=json.dumps({"result":json.dumps(value)}),stderr="",duration_seconds=1,started_at="x",completed_at="y")
    monkeypatch.setattr(agent_runner,"run_claude_code",runner)
    outcome=invoke(post_code_payload(),threading.Event())
    assert pins==["b"*40]
    if actual != "b"*40:
        assert not outcome.ok and not calls
        assert "SHA mismatch" in outcome.reason
    else:
        assert outcome.ok
        assert outcome.result['observed_source_sha']==actual
        assert calls[0]['repository_path']==checkout
