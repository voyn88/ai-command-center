from datetime import UTC, datetime, timedelta

import pytest

from command_center import decision_time_machine as dtm


def _create(root, **overrides):
    kwargs = {
        "event_ref": "incident:1",
        "title": "Database failover flapped for 40 minutes",
        "severity": "sev1",
        "hypothesis": "Forcing failover to the standby region cuts recovery time in half",
        "alternatives": [
            {"option": "Wait for primary to self-heal", "why_not": "no ETA, breaches SLA"},
            "Roll back last deploy",
        ],
        "decision": "Force failover to the standby region",
        "rationale": "Fastest path to a known-good state under an active SLA breach",
        "owner": "sre-lead",
        "project_ref": "AICC",
        "decided_at": "2026-01-01T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return dtm.create_decision_package(root, **kwargs)


# --------------------------------------------------------------------------
# create_decision_package
# --------------------------------------------------------------------------


def test_create_decision_package_schedules_all_four_horizons(tmp_path):
    package = _create(tmp_path)
    horizons = [c["horizon_days"] for c in package["checkpoints"]]
    assert horizons == [1, 7, 30, 90]
    assert all(c["status"] == dtm.CHECKPOINT_PENDING for c in package["checkpoints"])


def test_create_decision_package_normalizes_string_and_dict_alternatives(tmp_path):
    package = _create(tmp_path)
    assert package["alternatives"][0] == {
        "option": "Wait for primary to self-heal",
        "why_not": "no ETA, breaches SLA",
    }
    assert package["alternatives"][1] == {"option": "Roll back last deploy", "why_not": None}


def test_create_decision_package_persists_across_calls(tmp_path):
    created = _create(tmp_path)
    fetched = dtm.get_decision_package(tmp_path, created["id"])
    assert fetched == created


@pytest.mark.parametrize(
    "field",
    ["event_ref", "hypothesis", "decision"],
)
def test_create_decision_package_requires_core_fields(tmp_path, field):
    with pytest.raises(ValueError):
        _create(tmp_path, **{field: ""})


def test_checkpoint_due_dates_follow_decided_at(tmp_path):
    package = _create(tmp_path, decided_at="2026-01-01T00:00:00+00:00")
    due_at_by_horizon = {c["horizon_days"]: c["due_at"] for c in package["checkpoints"]}
    assert due_at_by_horizon[1] == "2026-01-02T00:00:00+00:00"
    assert due_at_by_horizon[7] == "2026-01-08T00:00:00+00:00"
    assert due_at_by_horizon[30] == "2026-01-31T00:00:00+00:00"
    assert due_at_by_horizon[90] == "2026-04-01T00:00:00+00:00"


# --------------------------------------------------------------------------
# record_effect
# --------------------------------------------------------------------------


def test_record_effect_marks_checkpoint_recorded(tmp_path):
    package = _create(tmp_path)
    updated = dtm.record_effect(
        tmp_path,
        package["id"],
        1,
        outcome="as_expected",
        effect_summary="Recovery time dropped from 40m to 18m",
        metric_deltas={"mttr_minutes": -22},
    )
    checkpoint = next(c for c in updated["checkpoints"] if c["horizon_days"] == 1)
    assert checkpoint["status"] == dtm.CHECKPOINT_RECORDED
    assert checkpoint["outcome"] == "as_expected"
    assert checkpoint["metric_deltas"] == {"mttr_minutes": -22}
    assert checkpoint["recorded_at"] is not None


def test_record_effect_out_of_order_is_allowed(tmp_path):
    package = _create(tmp_path)
    dtm.record_effect(
        tmp_path, package["id"], 30, outcome="as_expected", effect_summary="Still holding"
    )
    updated = dtm.get_decision_package(tmp_path, package["id"])
    statuses = {c["horizon_days"]: c["status"] for c in updated["checkpoints"]}
    assert statuses[30] == dtm.CHECKPOINT_RECORDED
    assert statuses[1] == dtm.CHECKPOINT_PENDING


def test_record_effect_rejects_unknown_outcome(tmp_path):
    package = _create(tmp_path)
    with pytest.raises(ValueError):
        dtm.record_effect(
            tmp_path, package["id"], 1, outcome="great!", effect_summary="x"
        )


def test_record_effect_unknown_package_raises(tmp_path):
    with pytest.raises(dtm.DecisionPackageNotFoundError):
        dtm.record_effect(tmp_path, "no-such-id", 1, outcome="as_expected", effect_summary="x")


def test_record_effect_unknown_horizon_raises(tmp_path):
    package = _create(tmp_path)
    with pytest.raises(dtm.CheckpointNotFoundError):
        dtm.record_effect(
            tmp_path, package["id"], 14, outcome="as_expected", effect_summary="x"
        )


# --------------------------------------------------------------------------
# list_decision_packages
# --------------------------------------------------------------------------


def test_list_decision_packages_filters_by_status(tmp_path):
    open_pkg = _create(tmp_path, event_ref="incident:1")
    complete_pkg = _create(tmp_path, event_ref="incident:2")
    for horizon in dtm.EFFECT_HORIZONS_DAYS:
        dtm.record_effect(
            tmp_path, complete_pkg["id"], horizon, outcome="as_expected", effect_summary="ok"
        )

    open_only = dtm.list_decision_packages(tmp_path, status="open")
    complete_only = dtm.list_decision_packages(tmp_path, status="complete")

    assert [p["id"] for p in open_only] == [open_pkg["id"]]
    assert [p["id"] for p in complete_only] == [complete_pkg["id"]]


def test_list_decision_packages_filters_by_event_ref_and_project(tmp_path):
    _create(tmp_path, event_ref="incident:1", project_ref="AICC")
    other = _create(tmp_path, event_ref="incident:2", project_ref="BANK")

    assert [p["id"] for p in dtm.list_decision_packages(tmp_path, event_ref="incident:2")] == [
        other["id"]
    ]
    assert [p["id"] for p in dtm.list_decision_packages(tmp_path, project_ref="BANK")] == [
        other["id"]
    ]


# --------------------------------------------------------------------------
# due_checkpoints
# --------------------------------------------------------------------------


def test_due_checkpoints_returns_only_past_due_pending_checkpoints(tmp_path):
    _create(tmp_path, decided_at="2026-01-01T00:00:00+00:00")
    due = dtm.due_checkpoints(tmp_path, as_of="2026-01-10T00:00:00+00:00")
    assert [d["horizon_days"] for d in due] == [1, 7]


def test_due_checkpoints_excludes_already_recorded(tmp_path):
    package = _create(tmp_path, decided_at="2026-01-01T00:00:00+00:00")
    dtm.record_effect(
        tmp_path, package["id"], 1, outcome="as_expected", effect_summary="fine"
    )
    due = dtm.due_checkpoints(tmp_path, as_of="2026-01-10T00:00:00+00:00")
    assert [d["horizon_days"] for d in due] == [7]


def test_due_checkpoints_accepts_naive_as_of(tmp_path):
    decided = (datetime.now(UTC) - timedelta(days=2)).replace(tzinfo=None).isoformat()
    _create(tmp_path, decided_at=decided)
    due = dtm.due_checkpoints(tmp_path)
    assert [d["horizon_days"] for d in due] == [1]


# --------------------------------------------------------------------------
# build_post_mortem
# --------------------------------------------------------------------------


def test_post_mortem_is_pending_data_before_any_checkpoint(tmp_path):
    package = _create(tmp_path)
    post_mortem = dtm.build_post_mortem(package)
    assert post_mortem["verdict"] == "pending_data"
    assert post_mortem["effect_timeline"] == []
    assert post_mortem["outstanding_checkpoints"] == [1, 7, 30, 90]


def test_post_mortem_validated_when_outcomes_lean_validating(tmp_path):
    package = _create(tmp_path)
    dtm.record_effect(tmp_path, package["id"], 1, outcome="as_expected", effect_summary="a")
    dtm.record_effect(
        tmp_path, package["id"], 7, outcome="better_than_expected", effect_summary="b"
    )
    dtm.record_effect(tmp_path, package["id"], 30, outcome="worse_than_expected", effect_summary="c")
    updated = dtm.get_decision_package(tmp_path, package["id"])
    post_mortem = dtm.build_post_mortem(updated)
    assert post_mortem["verdict"] == "validated"
    assert [c["horizon_days"] for c in post_mortem["effect_timeline"]] == [1, 7, 30]


def test_post_mortem_invalidated_when_outcomes_lean_invalidating(tmp_path):
    package = _create(tmp_path)
    dtm.record_effect(tmp_path, package["id"], 1, outcome="worse_than_expected", effect_summary="a")
    dtm.record_effect(tmp_path, package["id"], 7, outcome="worse_than_expected", effect_summary="b")
    dtm.record_effect(tmp_path, package["id"], 30, outcome="as_expected", effect_summary="c")
    updated = dtm.get_decision_package(tmp_path, package["id"])
    assert dtm.build_post_mortem(updated)["verdict"] == "invalidated"


def test_post_mortem_mixed_on_tie(tmp_path):
    package = _create(tmp_path)
    dtm.record_effect(tmp_path, package["id"], 1, outcome="as_expected", effect_summary="a")
    dtm.record_effect(tmp_path, package["id"], 7, outcome="worse_than_expected", effect_summary="b")
    updated = dtm.get_decision_package(tmp_path, package["id"])
    assert dtm.build_post_mortem(updated)["verdict"] == "mixed"


def test_post_mortem_mixed_when_only_neutral_outcomes_recorded(tmp_path):
    package = _create(tmp_path)
    dtm.record_effect(tmp_path, package["id"], 1, outcome="mixed", effect_summary="a")
    dtm.record_effect(tmp_path, package["id"], 7, outcome="inconclusive", effect_summary="b")
    updated = dtm.get_decision_package(tmp_path, package["id"])
    assert dtm.build_post_mortem(updated)["verdict"] == "mixed"


# --------------------------------------------------------------------------
# find_similar_packages
# --------------------------------------------------------------------------


def test_find_similar_packages_matches_on_word_overlap(tmp_path):
    package = _create(
        tmp_path,
        event_ref="incident:1",
        title="Database failover flapped for 40 minutes",
        hypothesis="Forcing failover to the standby region cuts recovery time in half",
        decision="Force failover to the standby region",
    )
    dtm.record_effect(tmp_path, package["id"], 1, outcome="as_expected", effect_summary="worked")

    matches = dtm.find_similar_packages(
        tmp_path,
        title="Database failover stuck again",
        hypothesis="Forcing failover to the standby region will resolve it quickly",
        decision="",
    )

    assert len(matches) == 1
    assert matches[0]["package"]["id"] == package["id"]
    assert matches[0]["post_mortem"]["verdict"] == "validated"


def test_find_similar_packages_ignores_packages_with_no_recorded_checkpoints(tmp_path):
    _create(
        tmp_path,
        title="Database failover flapped for 40 minutes",
        hypothesis="Forcing failover to the standby region cuts recovery time in half",
        decision="Force failover to the standby region",
    )
    matches = dtm.find_similar_packages(
        tmp_path,
        title="Database failover stuck again",
        hypothesis="Forcing failover to the standby region will resolve it quickly",
        decision="",
    )
    assert matches == []


def test_find_similar_packages_prefers_same_project_on_equal_similarity(tmp_path):
    same_project = _create(
        tmp_path,
        event_ref="incident:1",
        title="Checkout latency spike",
        hypothesis="Scaling the checkout pool fixes latency",
        decision="Scale the checkout pool",
        project_ref="AICC",
    )
    dtm.record_effect(
        tmp_path, same_project["id"], 1, outcome="as_expected", effect_summary="worked"
    )
    other_project = _create(
        tmp_path,
        event_ref="incident:2",
        title="Checkout latency spike",
        hypothesis="Scaling the checkout pool fixes latency",
        decision="Scale the checkout pool",
        project_ref="BANK",
    )
    dtm.record_effect(
        tmp_path, other_project["id"], 1, outcome="as_expected", effect_summary="worked"
    )

    matches = dtm.find_similar_packages(
        tmp_path,
        title="Checkout latency spike",
        hypothesis="Scaling the checkout pool fixes latency",
        decision="Scale the checkout pool",
        project_ref="AICC",
        limit=1,
    )

    assert matches[0]["package"]["id"] == same_project["id"]


def test_find_similar_packages_returns_empty_with_no_query_text(tmp_path):
    assert dtm.find_similar_packages(tmp_path, title="", hypothesis="", decision="") == []


# --------------------------------------------------------------------------
# missing_decision_packages / is_critical
# --------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["sev1", "critical", "SEV1"])
def test_is_critical_recognizes_both_vocabularies(severity):
    assert dtm.is_critical(severity)


@pytest.mark.parametrize("severity", ["sev2", "sev3", "sev4", "high", None, ""])
def test_is_critical_rejects_non_critical_severities(severity):
    assert not dtm.is_critical(severity)


def test_missing_decision_packages_flags_uncovered_critical_events(tmp_path):
    _create(tmp_path, event_ref="incident:covered", severity="sev1")

    events = [
        {"ref": "incident:covered", "severity": "sev1"},
        {"ref": "incident:uncovered", "severity": "sev1", "title": "Payment outage"},
        {"ref": "incident:minor", "severity": "sev3"},
    ]

    missing = dtm.missing_decision_packages(tmp_path, events)

    assert [e["ref"] for e in missing] == ["incident:uncovered"]


def test_missing_decision_packages_empty_when_all_covered(tmp_path):
    _create(tmp_path, event_ref="incident:1", severity="sev1")
    events = [{"ref": "incident:1", "severity": "sev1"}]
    assert dtm.missing_decision_packages(tmp_path, events) == []
