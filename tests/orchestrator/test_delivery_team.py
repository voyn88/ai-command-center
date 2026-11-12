import json
from unittest.mock import MagicMock

import pytest
from command_center.orchestrator import delivery_team as team

META = dict(task_id="VOYN-TEAM-1", revision=2, stage="ba", input_digest="a" * 64)


def result(**changes):
    value = dict(schema=team.SCHEMA, **META, verdict="pass", criteria=[dict(id="C1", description="A real criterion")], evidence=[dict(criterion_id="C1", reference="owner-approved-spec#C1", sha=None, verified=True)], blockers=[], summary="Reviewed")
    value.update(changes)
    return value


def test_strict_provenance_and_json():
    assert team.parse_role_result(json.dumps(result()), META)["verdict"] == "pass"
    for value in [result(revision=True), result(revision=3), result(input_digest="b"*64), result(extra=True), result(stage="implementation"), '```json\n{}\n```', '{"schema":1,"schema":2}', result(verdict=[]), result(stage=[])]:
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(value, META)


def test_blocked_never_pass_and_criteria_not_fabricated():
    with pytest.raises(team.TeamResultError):
        team.parse_role_result(result(evidence=[]), META)
    for value in [result(blockers=["Missing owner"]), result(verdict="blocked"), result(criteria=[]), result(criteria=[dict(id="C1", description="x")]*2)]:
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(value, META)
    assert team.parse_role_result(result(verdict="blocked", blockers=["Owner required"]), META)


def test_postproduction_requires_sha_criterion_and_environment():
    expected = dict(META, stage="production_qa")
    value = result(stage="production_qa")
    with pytest.raises(team.TeamResultError):
        team.parse_role_result(value, expected)
    value["evidence"] = [dict(criterion_id="C1", reference="receipt:123", sha="b"*40, verified=True, environment="production")]
    assert team.parse_role_result(value, expected)
    for key, invalid in [("sha",None),("verified",False),("environment","preprod"),("criterion_id","foreign")]:
        evidence = dict(value["evidence"][0])
        evidence[key] = invalid
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(dict(value,evidence=[evidence]), expected)


def test_role_payload_never_implementation_or_paid_fallback():
    for stage in team.STAGES:
        p = team.build_payload(dict(META,stage=stage,title="x",body="x",context={"source_sha": "b" * 40, "release_receipt": {"sha": "b" * 40}}), ("AIOS","/repo"))
        assert p["task_type"] == "review"
        assert "backlog_task_id" not in p
        assert {x["executor"] for x in p["cascade"]} == {"codex","claude"}
        assert all(x["task_type"]=="review" for x in p["cascade"])
    with pytest.raises(team.TeamResultError):
        team.build_payload(dict(META,stage="unknown"),("AIOS","/repo"))


def test_disabled_has_zero_database_effects(monkeypatch):
    monkeypatch.delenv("AICC_DELIVERY_TEAM_ENABLED", raising=False)
    factory = MagicMock()
    assert not team.tick(factory).enabled
    assert team.implementation_gate(factory,"T",1)==(True,"disabled")
    factory.assert_not_called()


def test_stop_holds_gate_and_ingests_without_dispatch(monkeypatch):
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED","1")
    monkeypatch.setenv("AICC_DELIVERY_TEAM_STOP","1")
    factory=MagicMock()
    cur=factory.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [[], [("VOYN-TEAM-1", "ba", "pass")]]
    cur.fetchone.return_value = (True, False)
    report=team.tick(factory)
    assert report.stopped and not report.dispatched
    calls=[x.args[0] for x in cur.execute.call_args_list]
    assert calls == ["SELECT * FROM delivery_team_sync(%s,%s::jsonb)", "SELECT * FROM delivery_team_ingest(%s)"]
    assert report.ingested == [("VOYN-TEAM-1", "ba", "pass")]
    assert team.implementation_gate(factory,"T",1)==(False,"delivery_team_stopped")


def test_incident_identity_exact_not_normalized():
    assert team.incident_key("p","run","a"*40,"a-b") == team.incident_key("p","run","a"*40,"a-b")
    assert team.incident_key("p","run","a"*40,"a-b") != team.incident_key("p","run","a"*40,"a/b")


def test_size_and_nonfinite_json_refused():
    for value in [result(summary="x"*team.MAX_RESULT_BYTES), result(summary=float("nan"))]:
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(value,META)


def test_pilot_scope_cannot_expand_from_invalid_input(monkeypatch):
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    for value in ("", "[]", "null", '["VOYN-A","VOYN-A"]', '["foreign"]', "broken"):
        monkeypatch.setenv("AICC_DELIVERY_TEAM_TASK_IDS", value)
        factory = MagicMock()
        with pytest.raises(team.TeamResultError):
            team.tick(factory)
        factory.assert_not_called()
    monkeypatch.setenv("AICC_DELIVERY_TEAM_TASK_IDS", '["VOYN-A"]')
    assert team.scoped_task_ids() == ["VOYN-A"]


def rca_result(stage="auditor", **changes):
    value = result(
        stage=stage,
        criteria=[dict(id="E1", description="Correction is effective")],
        evidence=[dict(criterion_id="E1", reference="https://github.com/org/repo/actions/runs/1", sha="b" * 40, verified=True)],
        rca=[dict(incident_key="c" * 64, facts=["The exact-head CI run failed"],
                  hypotheses=[], root_cause="Fixture omitted required confirmation",
                  missed_control="No contract migration check",
                  actions=[dict(task_id="VOYN-FIX-1", owner="qa", action="Add contract migration coverage",
                                effectiveness_criterion_id="E1")])],
    )
    value.update(changes)
    return value


@pytest.mark.parametrize("stage", ["auditor", "cto_rca"])
def test_rca_requires_structured_actions_and_cause(stage):
    expected = dict(META, stage=stage)
    value = rca_result(stage)
    assert team.parse_role_result(value, expected)
    for field in ("incident_key", "facts", "hypotheses", "root_cause", "missed_control", "actions"):
        broken = json.loads(json.dumps(value))
        del broken["rca"][0][field]
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(broken, expected)
    for records in ([], value["rca"] * 2):
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(dict(value, rca=records), expected)
    assert team.parse_role_result(dict(value, rca=[], verdict="blocked", blockers=["No corrective task assigned"]), expected)


def test_rca_cannot_invent_criterion_or_omit_action_owner():
    expected = dict(META, stage="auditor")
    for field, invalid in (("effectiveness_criterion_id", "foreign"), ("owner", ""), ("task_id", "invented")):
        value = rca_result()
        value["rca"][0]["actions"][0][field] = invalid
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(value, expected)
    value = rca_result()
    value["rca"][0]["actions"] *= 2
    with pytest.raises(team.TeamResultError):
        team.parse_role_result(value, expected)


def test_cto_rca_requires_sha_bound_effectiveness():
    expected = dict(META, stage="cto_rca")
    for field, invalid in (("sha", None), ("verified", False)):
        value = rca_result("cto_rca")
        value["evidence"][0][field] = invalid
        with pytest.raises(team.TeamResultError):
            team.parse_role_result(value, expected)


def test_non_rca_roles_cannot_smuggle_corrective_closure():
    with pytest.raises(team.TeamResultError):
        team.parse_role_result(result(rca=[]), META)


def test_incident_identity_uses_utf8_length_framing():
    import hashlib

    parts = ["орг/репо", "run:1", "a" * 40, "сбой"]
    framed = b"".join(str(len(p.encode())).encode() + b":" + p.encode() for p in parts)
    assert team.incident_key(*parts) == hashlib.sha256(framed).hexdigest()
    with pytest.raises(team.TeamResultError):
        team.incident_key("repo", "run", None, "failure")


@pytest.mark.parametrize("stop", [False, True])
def test_unenrolled_legacy_gate_is_not_held(monkeypatch, stop):
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    monkeypatch.setenv("AICC_DELIVERY_TEAM_STOP", "1" if stop else "0")
    factory = MagicMock()
    cur = factory.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (False, False)
    assert team.implementation_gate(factory, "VOYN-LEGACY", 1) == (True, "unenrolled")


@pytest.mark.parametrize("context", [[1], "invalid", {"source_sha": "b" * 40, "release_receipt": [1]}])
def test_malformed_context_isolated_before_next_candidate(monkeypatch, context):
    from command_center.orchestrator import planner
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    monkeypatch.delenv("AICC_DELIVERY_TEAM_STOP", raising=False)
    monkeypatch.delenv("AICC_DELIVERY_TEAM_TASK_IDS", raising=False)
    monkeypatch.setattr(planner, "repo_route", lambda _: ("AIOS", "/repo"))
    factory = MagicMock()
    cur = factory.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    bad = dict(META, stage="production_qa", repo="repo", context=context)
    good = dict(META, task_id="VOYN-GOOD", repo="repo")
    cur.fetchall.side_effect = [[], [], [(bad,), (good,)], [(True, "queued", "work-good")]]
    report = team.tick(factory)
    assert report.refused[0][:2] == (META["task_id"], "invalid_candidate")
    assert report.dispatched == [("VOYN-GOOD", "ba", "work-good")]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_raw_json_nonfinite_rejected_during_decode(constant):
    raw = json.dumps(result()).replace('"revision": 2', '"revision": ' + constant)
    with pytest.raises(team.TeamResultError, match="invalid role JSON") as error:
        team.parse_role_result(raw, META)
    assert str(error.value.__cause__) == "nonfinite JSON"
    assert team.parse_role_result(json.dumps(result()), META)["revision"] == 2


def test_empty_criteria_rejected_without_foreign_evidence():
    with pytest.raises(team.TeamResultError, match="criteria required"):
        team.parse_role_result(result(criteria=[], evidence=[], verdict="blocked", blockers=["No agreed criteria"]), META)


def test_auditor_can_cite_incident_evidence_without_code_sha():
    value = rca_result("auditor")
    value["evidence"][0]["sha"] = None
    assert team.parse_role_result(value, dict(META, stage="auditor"))["verdict"] == "pass"
