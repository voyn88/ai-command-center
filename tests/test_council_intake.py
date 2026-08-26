"""Unit tests for the proposal/incident→motion intake
(``command_center.council.intake``).

Hermetic: the per-test ``AICC_DATA_DIR`` sandbox backs the runtime db. Each test
drives a :class:`CouncilIntake` bound to an *isolated* :class:`EventBus`, so the
event → motion mapping, the source-ref dedup and the BANK/LEGAL redaction are
tested in isolation from the process-wide bus.

Fixtures use only invented ids and generic project codes — no real names/paths.
"""

from __future__ import annotations

import pytest

from command_center.council import install_default_intake
from command_center.council.intake import CouncilIntake, CouncilIntakeConfig
from command_center.council.service import ROOT
from command_center.events import EventBus, IncidentOpened, ProposalCreated
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


def _motions():
    return db.list_motions(resolve_db_path(ROOT))


def _source_refs():
    return {m["source_ref"] for m in _motions()}


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def test_proposal_raises_a_motion(bus) -> None:
    CouncilIntake().register(bus)
    bus.publish(ProposalCreated(proposal_id="p1", kind="trend", project_ref="AICC"))
    (row,) = [m for m in _motions() if m["source_ref"] == "proposal:p1"]
    assert row["status"] == "open"
    assert row["proposal_ref"] == "p1"
    assert row["project_ref"] == "AICC"
    assert row["proposed_by"] == "council-intake"


def test_incident_raises_a_motion(bus) -> None:
    CouncilIntake().register(bus)
    bus.publish(IncidentOpened(incident_id="i1", severity="sev2", project_ref="AICC"))
    assert "incident:i1" in _source_refs()


def test_intake_dedups_by_source_ref(bus) -> None:
    CouncilIntake().register(bus)
    bus.publish(ProposalCreated(proposal_id="dup", kind="ux"))
    bus.publish(ProposalCreated(proposal_id="dup", kind="ux"))  # redelivery
    opened = [m for m in _motions() if m["source_ref"] == "proposal:dup"]
    assert len(opened) == 1


def test_sensitive_source_raises_no_motion(bus) -> None:
    CouncilIntake().register(bus)
    bus.publish(ProposalCreated(proposal_id="secret", kind="ux", project_ref="BANK"))
    bus.publish(IncidentOpened(incident_id="secret2", severity="sev1", project_ref="LEGAL"))
    refs = _source_refs()
    assert "proposal:secret" not in refs and "incident:secret2" not in refs


def test_config_can_gate_sources_and_quorum(bus) -> None:
    CouncilIntake(
        config=CouncilIntakeConfig(raise_from_incidents=False, quorum=3)
    ).register(bus)
    bus.publish(IncidentOpened(incident_id="i", severity="sev1"))  # gated off
    bus.publish(ProposalCreated(proposal_id="p", kind="trend"))
    assert "incident:i" not in _source_refs()
    (row,) = [m for m in _motions() if m["source_ref"] == "proposal:p"]
    assert row["quorum"] == 3


def test_unregister_stops_delivery(bus) -> None:
    intake = CouncilIntake().register(bus)
    intake.unregister()
    bus.publish(ProposalCreated(proposal_id="after", kind="trend"))
    assert "proposal:after" not in _source_refs()


def test_install_default_intake_is_idempotent() -> None:
    a = install_default_intake()
    b = install_default_intake()
    assert a is b
