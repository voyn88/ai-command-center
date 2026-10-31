"""Unit tests for the pure certification gate (`dispatch.attestation`).

Every assertion is against `evaluate_attestation` directly — no database, no
filesystem, no HTTP — mirroring `test_policy.py`'s hermetic style.
"""

from __future__ import annotations

from command_center.dispatch.attestation import (
    AttestationRecord,
    CertificationCase,
    evaluate_attestation,
)


def _record(**kwargs) -> AttestationRecord:
    kwargs.setdefault("agent_id", "claude_code")
    return AttestationRecord(**kwargs)


def test_no_record_is_uncertified():
    decision = evaluate_attestation(None)

    assert decision.certified is False
    assert decision.reasons == ("no_attestation_record",)


def test_record_with_no_test_cases_is_uncertified():
    decision = evaluate_attestation(_record(test_results=()))

    assert decision.certified is False
    assert decision.reasons == ("no_test_cases_recorded",)


def test_all_passing_test_cases_and_no_incidents_certifies():
    decision = evaluate_attestation(
        _record(
            test_results=(
                CertificationCase(name="handles_permission_denial", passed=True),
                CertificationCase(name="respects_kill_switch", passed=True),
            ),
            critical_incidents=0,
        )
    )

    assert decision.certified is True
    assert decision.reasons == ()


def test_any_failing_test_case_withholds_certification():
    decision = evaluate_attestation(
        _record(
            test_results=(
                CertificationCase(name="ok_case", passed=True),
                CertificationCase(name="broken_case", passed=False),
            )
        )
    )

    assert decision.certified is False
    assert decision.reasons == ("failing_test_cases:broken_case",)


def test_multiple_failing_cases_are_named_and_sorted():
    decision = evaluate_attestation(
        _record(
            test_results=(
                CertificationCase(name="zeta", passed=False),
                CertificationCase(name="alpha", passed=False),
                CertificationCase(name="beta", passed=True),
            )
        )
    )

    assert decision.reasons == ("failing_test_cases:alpha,zeta",)


def test_any_historical_critical_incident_withholds_certification():
    decision = evaluate_attestation(
        _record(
            test_results=(CertificationCase(name="ok_case", passed=True),),
            critical_incidents=1,
        )
    )

    assert decision.certified is False
    assert decision.reasons == ("historical_critical_incident",)


def test_failing_cases_and_incidents_both_surface_as_reasons():
    decision = evaluate_attestation(
        _record(
            test_results=(CertificationCase(name="broken_case", passed=False),),
            critical_incidents=2,
        )
    )

    assert decision.certified is False
    assert decision.reasons == (
        "failing_test_cases:broken_case",
        "historical_critical_incident",
    )


def test_test_case_result_from_dict_drops_malformed_entries():
    assert CertificationCase.from_dict({"name": "ok", "passed": True}) == CertificationCase(
        name="ok", passed=True
    )
    assert CertificationCase.from_dict({"passed": True}) is None
    assert CertificationCase.from_dict({"name": "", "passed": True}) is None
    assert CertificationCase.from_dict("not-a-dict") is None
    assert CertificationCase.from_dict(None) is None


def test_test_case_result_from_dict_treats_missing_passed_as_failed():
    result = CertificationCase.from_dict({"name": "ok"})

    assert result == CertificationCase(name="ok", passed=False)


def test_attestation_record_from_dict_is_fail_closed_on_garbage():
    for garbage in (None, [], "nope", 42):
        record = AttestationRecord.from_dict("claude_code", garbage)
        assert record.agent_id == "claude_code"
        assert record.test_results == ()
        assert record.critical_incidents == 0
        assert evaluate_attestation(record).certified is False


def test_attestation_record_from_dict_clamps_negative_and_bool_incident_counts():
    for bad_value in (-1, True, "3", None):
        record = AttestationRecord.from_dict(
            "claude_code", {"critical_incidents": bad_value}
        )
        assert record.critical_incidents == 0


def test_attestation_record_roundtrips_through_dict():
    record = AttestationRecord(
        agent_id="claude_code",
        test_results=(CertificationCase(name="ok_case", passed=True),),
        critical_incidents=0,
        recorded_at="2026-09-01T00:00:00+00:00",
        recorded_by="operator:test",
    )

    restored = AttestationRecord.from_dict("claude_code", record.as_dict())

    assert restored == record
