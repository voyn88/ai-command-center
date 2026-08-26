"""Repository-tier tests for the Wave-3 networking table family
(``command_center.runtime.db.networking``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v23 migration on a fresh db.

Fixtures use only generic project codes (``AICC``, ``BANK``) and invented ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import db
from command_center.runtime.db.networking import InvalidInvitationTransitionError


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


# --- migration ------------------------------------------------------------


def test_migration_brings_fresh_db_to_current_version(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    assert db.current_schema_version(db_path) == SCHEMA_VERSION >= 23


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)  # second run must be a no-op, not an error
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- contacts -------------------------------------------------------------


def test_create_and_get_contact(db_path: Path) -> None:
    row = db.create_contact(
        db_path, display_name="Ada", handle="@ada", org="Analytical", project_ref="AICC"
    )
    assert row["version"] == 0 and row["created_at"]
    got = db.get_contact(db_path, row["id"])
    assert got["display_name"] == "Ada" and got["handle"] == "@ada"


def test_create_contact_requires_name(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_contact(db_path, display_name="   ")


def test_list_contacts_redacts_and_pages(db_path: Path) -> None:
    db.create_contact(db_path, display_name="Visible", project_ref="AICC")
    db.create_contact(db_path, display_name="Unattributed")
    db.create_contact(db_path, display_name="Secret", project_ref="BANK")

    visible = db.list_contacts(db_path, exclude_projects=["BANK", "LEGAL"])
    names = {c["display_name"] for c in visible}
    assert names == {"Visible", "Unattributed"}
    # Paging counts only visible rows.
    page = db.list_contacts(db_path, exclude_projects=["BANK", "LEGAL"], limit=1, offset=0)
    assert len(page) == 1


def test_update_contact_fields_is_compare_and_set(db_path: Path) -> None:
    row = db.create_contact(db_path, display_name="Ada")
    updated = db.update_contact_fields(
        db_path, row["id"], expected_version=0, fields={"org": "New Org"}
    )
    assert updated["org"] == "New Org" and updated["version"] == 1
    with pytest.raises(db.LostUpdateError):
        db.update_contact_fields(
            db_path, row["id"], expected_version=0, fields={"org": "stale"}
        )


def test_update_contact_rejects_unknown_field(db_path: Path) -> None:
    row = db.create_contact(db_path, display_name="Ada")
    with pytest.raises(ValueError):
        db.update_contact_fields(
            db_path, row["id"], expected_version=0, fields={"project_ref": "BANK"}
        )


# --- messages -------------------------------------------------------------


def test_create_message_validates_direction_and_kind(db_path: Path) -> None:
    contact = db.create_contact(db_path, display_name="Ada", project_ref="AICC")
    msg = db.create_message(
        db_path, contact_id=contact["id"], direction="inbound", kind="feedback",
        body="please add X", project_ref="AICC",
    )
    assert msg["kind"] == "feedback" and msg["direction"] == "inbound"
    with pytest.raises(ValueError):
        db.create_message(db_path, contact_id=contact["id"], direction="sideways")
    with pytest.raises(ValueError):
        db.create_message(db_path, contact_id=contact["id"], kind="rumor")


def test_list_messages_filters_by_contact_and_kind(db_path: Path) -> None:
    a = db.create_contact(db_path, display_name="A")
    b = db.create_contact(db_path, display_name="B")
    db.create_message(db_path, contact_id=a["id"], kind="note", body="hi")
    db.create_message(db_path, contact_id=a["id"], kind="feedback", body="fix")
    db.create_message(db_path, contact_id=b["id"], kind="note", body="yo")

    assert len(db.list_messages(db_path, contact_id=a["id"])) == 2
    assert len(db.list_messages(db_path, kind="feedback")) == 1


def test_messages_redacted_by_project(db_path: Path) -> None:
    c = db.create_contact(db_path, display_name="C", project_ref="BANK")
    db.create_message(db_path, contact_id=c["id"], body="secret", project_ref="BANK")
    visible = db.list_messages(db_path, exclude_projects=["BANK", "LEGAL"])
    assert visible == []


# --- invitations (Council seam) -------------------------------------------


def test_create_and_get_invitation(db_path: Path) -> None:
    c = db.create_contact(db_path, display_name="Ada", project_ref="AICC")
    inv = db.create_invitation(
        db_path, contact_id=c["id"], council_ref="council-invite:x", project_ref="AICC"
    )
    assert inv["status"] == "pending" and inv["invited_at"]
    assert inv["responded_at"] is None and inv["version"] == 0
    got = db.get_invitation(db_path, inv["id"])
    assert got["council_ref"] == "council-invite:x"


def test_create_invitation_requires_council_ref(db_path: Path) -> None:
    c = db.create_contact(db_path, display_name="Ada")
    with pytest.raises(ValueError):
        db.create_invitation(db_path, contact_id=c["id"], council_ref="  ")


def test_invitation_transition_pending_to_accepted(db_path: Path) -> None:
    c = db.create_contact(db_path, display_name="Ada")
    inv = db.create_invitation(db_path, contact_id=c["id"], council_ref="r1")
    accepted = db.set_invitation_status(
        db_path, inv["id"], expected_version=0, status="accepted"
    )
    assert accepted["status"] == "accepted" and accepted["responded_at"] is not None
    # accepted is terminal — no further transition.
    with pytest.raises(InvalidInvitationTransitionError):
        db.set_invitation_status(
            db_path, inv["id"], expected_version=accepted["version"], status="declined"
        )


def test_invitation_transition_is_compare_and_set(db_path: Path) -> None:
    c = db.create_contact(db_path, display_name="Ada")
    inv = db.create_invitation(db_path, contact_id=c["id"], council_ref="r1")
    with pytest.raises(db.LostUpdateError):
        db.set_invitation_status(
            db_path, inv["id"], expected_version=99, status="accepted"
        )


def test_list_invitations_filters_and_redacts(db_path: Path) -> None:
    a = db.create_contact(db_path, display_name="A", project_ref="AICC")
    b = db.create_contact(db_path, display_name="B", project_ref="BANK")
    db.create_invitation(db_path, contact_id=a["id"], council_ref="ra", project_ref="AICC")
    db.create_invitation(db_path, contact_id=b["id"], council_ref="rb", project_ref="BANK")

    visible = db.list_invitations(db_path, exclude_projects=["BANK", "LEGAL"])
    assert len(visible) == 1 and visible[0]["council_ref"] == "ra"
    pending = db.list_invitations(db_path, status="pending", exclude_projects=["BANK"])
    assert len(pending) == 1
