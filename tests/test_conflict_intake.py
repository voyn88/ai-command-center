"""Unit tests for the incident→conflict intake
(``command_center.conflicts.intake``).

Hermetic: the per-test ``AICC_DATA_DIR`` sandbox backs the runtime db. Each test
drives a :class:`ConflictIntake` bound to an *isolated* :class:`EventBus`, so the
``IncidentOpened`` → conflict mapping, the source-ref dedup and the BANK/LEGAL
redaction are tested in isolation from the process-wide bus.

Fixtures use only invented ids and generic project codes — no real names/paths.
"""

from __future__ import annotations

import pytest

from command_center.conflicts import install_default_intake
from command_center.conflicts.intake import ConflictIntake, ConflictIntakeConfig
from command_center.conflicts.service import ROOT
from command_center.events import EventBus, IncidentOpened
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


def _conflicts():
    return db.list_conflicts(resolve_db_path(ROOT))


def _source_refs():
    return {r["source_ref"] for r in _conflicts()}


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def test_incident_opens_a_conflict(bus) -> None:
    ConflictIntake().register(bus)
    bus.publish(IncidentOpened(incident_id="i1", severity="sev2", project_ref="AICC"))
    (row,) = [r for r in _conflicts() if r["source_ref"] == "incident:i1"]
    assert row["kind"] == "perf"  # default operational bucket
    assert row["severity"] == "sev2"
    assert row["status"] == "open"
    assert row["project_ref"] == "AICC"


def test_incident_intake_dedups_by_source_ref(bus) -> None:
    ConflictIntake().register(bus)
    bus.publish(IncidentOpened(incident_id="dup", severity="sev1"))
    bus.publish(IncidentOpened(incident_id="dup", severity="sev1"))  # redelivery
    opened = [r for r in _conflicts() if r["source_ref"] == "incident:dup"]
    assert len(opened) == 1


def test_sensitive_incident_opens_no_conflict(bus) -> None:
    ConflictIntake().register(bus)
    bus.publish(IncidentOpened(incident_id="secret", severity="sev1", project_ref="BANK"))
    assert "incident:secret" not in _source_refs()


def test_intake_severity_gate(bus) -> None:
    ConflictIntake(
        config=ConflictIntakeConfig(incident_severities=frozenset({"sev1"}))
    ).register(bus)
    bus.publish(IncidentOpened(incident_id="low", severity="sev3"))  # below gate
    bus.publish(IncidentOpened(incident_id="high", severity="sev1"))  # at gate
    refs = _source_refs()
    assert "incident:high" in refs
    assert "incident:low" not in refs


def test_unknown_severity_falls_back_to_sev3(bus) -> None:
    ConflictIntake().register(bus)
    bus.publish(IncidentOpened(incident_id="odd", severity="weird"))
    (row,) = [r for r in _conflicts() if r["source_ref"] == "incident:odd"]
    assert row["severity"] == "sev3"


def test_configurable_incident_kind(bus) -> None:
    ConflictIntake(config=ConflictIntakeConfig(incident_kind="security")).register(bus)
    bus.publish(IncidentOpened(incident_id="i9", severity="sev2"))
    (row,) = [r for r in _conflicts() if r["source_ref"] == "incident:i9"]
    assert row["kind"] == "security"


def test_install_default_intake_is_idempotent() -> None:
    first = install_default_intake()
    second = install_default_intake()
    assert first is second


def test_unregister_stops_intake(bus) -> None:
    intake = ConflictIntake().register(bus)
    intake.unregister()
    bus.publish(IncidentOpened(incident_id="after", severity="sev1"))
    assert "incident:after" not in _source_refs()
