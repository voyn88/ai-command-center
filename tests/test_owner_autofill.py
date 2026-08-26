"""Unit tests for «Мой день» auto-fill (``command_center.digest.owner_autofill``).

Hermetic: the per-test ``AICC_DATA_DIR`` sandbox backs the runtime db. Each test
drives an :class:`OwnerAutofill` bound to an *isolated* :class:`EventBus`, so the
event→item mapping, the owner-gate config and the source-ref dedupe are tested
in isolation from the process-wide bus and from each other.

Fixtures use only invented ids and generic project codes — no real names/paths.
"""

from __future__ import annotations

import pytest

from command_center.digest import complete_owner_item, install_default_autofill
from command_center.digest.owner_autofill import OwnerAutofill
from command_center.digest.owner_gates import OwnerGateConfig
from command_center.events import (
    EventBus,
    IncidentOpened,
    OwnerItemCreated,
    ProposalCreated,
    ProposalPromotedToTask,
)
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path
from command_center.api.wave1_service import ROOT


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    """Migrate the per-test sandbox db up front so helpers that read the store
    directly (bypassing the service's lazy migrate) see the tables."""
    db.migrate(resolve_db_path(ROOT))


def _items(*, done=None):
    return db.list_owner_items(resolve_db_path(ROOT), done=done)


def _source_refs():
    return {r["source_ref"] for r in _items()}


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def test_promotion_event_creates_follow_up_item(bus) -> None:
    OwnerAutofill().register(bus)
    bus.publish(ProposalPromotedToTask(proposal_id="p1", task_id="t9", project_ref="AICC"))
    assert "promotion:p1" in _source_refs()
    (item,) = [r for r in _items() if r["source_ref"] == "promotion:p1"]
    assert item["done"] == 0 and "t9" in (item["detail"] or "")


def test_incident_event_gated_by_severity(bus) -> None:
    OwnerAutofill(gates=OwnerGateConfig(incident_severities=frozenset({"sev1"}))).register(bus)
    bus.publish(IncidentOpened(incident_id="i1", severity="sev3"))  # below gate
    bus.publish(IncidentOpened(incident_id="i2", severity="sev1"))  # at gate
    refs = _source_refs()
    assert "incident:i2" in refs
    assert "incident:i1" not in refs


def test_proposal_created_only_for_gated_project(bus) -> None:
    OwnerAutofill(gates=OwnerGateConfig(gate_projects=frozenset({"AICC"}))).register(bus)
    bus.publish(ProposalCreated(proposal_id="p1", kind="trend", project_ref="AICC"))
    bus.publish(ProposalCreated(proposal_id="p2", kind="trend", project_ref="OTHER"))
    refs = _source_refs()
    assert "proposal:p1" in refs
    assert "proposal:p2" not in refs


def test_event_to_item_is_idempotent_on_replay(bus) -> None:
    OwnerAutofill().register(bus)
    event = ProposalPromotedToTask(proposal_id="p1", task_id="t1", project_ref="AICC")
    bus.publish(event)
    bus.publish(event)  # redelivery / replay
    assert len([r for r in _items() if r["source_ref"] == "promotion:p1"]) == 1


def test_creation_publishes_owner_item_created(bus) -> None:
    seen: list[OwnerItemCreated] = []
    bus.subscribe(OwnerItemCreated, seen.append)
    OwnerAutofill().register(bus)
    bus.publish(ProposalPromotedToTask(proposal_id="p1", task_id="t1", project_ref="AICC"))
    assert len(seen) == 1 and seen[0].title


def test_board_and_networking_direct_seams() -> None:
    af = OwnerAutofill()
    m = af.board_motion_awaiting_vote(motion_id="m1", title="Ship v2")
    r = af.networking_reply_nondelegable(reply_ref="c9", title="Reply to X")
    assert m is not None and r is not None
    refs = _source_refs()
    assert {"motion:m1", "reply:c9"} <= refs
    # Idempotent per source.
    assert af.board_motion_awaiting_vote(motion_id="m1", title="Ship v2") is None


def test_unregister_stops_delivery(bus) -> None:
    af = OwnerAutofill().register(bus)
    af.unregister()
    bus.publish(ProposalPromotedToTask(proposal_id="p1", task_id="t1", project_ref="AICC"))
    assert "promotion:p1" not in _source_refs()


def test_complete_owner_item_idempotent_and_missing() -> None:
    row = db.create_owner_item(resolve_db_path(ROOT), title="Do X")
    done = complete_owner_item(row["id"])
    assert done is not None and done["done"] == 1
    # Second complete is a no-op that still returns the done row.
    again = complete_owner_item(row["id"])
    assert again["done"] == 1
    assert complete_owner_item("nope") is None


def test_install_default_autofill_is_idempotent() -> None:
    assert install_default_autofill() is install_default_autofill()


# --- redaction: sensitive projects never reach «Мой день» (audit MED-1b) ---


def test_gated_sensitive_proposal_creates_no_owner_item(bus) -> None:
    # Even when an operator gates a sensitive project, its proposal must not
    # land on «Мой день» — the subject would leak through the owner list.
    OwnerAutofill(gates=OwnerGateConfig(gate_projects=frozenset({"BANK"}))).register(bus)
    bus.publish(ProposalCreated(proposal_id="p1", kind="trend", project_ref="BANK"))
    assert "proposal:p1" not in _source_refs()
    assert _items() == []


def test_sensitive_incident_creates_no_owner_item(bus) -> None:
    OwnerAutofill().register(bus)
    bus.publish(IncidentOpened(incident_id="i1", severity="sev1", project_ref="LEGAL"))
    assert "incident:i1" not in _source_refs()


def test_sensitive_promotion_creates_no_owner_item(bus) -> None:
    OwnerAutofill().register(bus)
    bus.publish(ProposalPromotedToTask(proposal_id="p1", task_id="t1", project_ref="BANK"))
    assert "promotion:p1" not in _source_refs()
