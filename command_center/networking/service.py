"""Service tier for the Wave-3 networking engine (routes → **service** →
repository → db).

The routes in :mod:`command_center.api.networking_routes` hold no logic; they
call one function here per endpoint. This module is the only place that:

* resolves and lazily migrates the runtime db (the repository functions take an
  explicit ``db_path``);
* maps stored rows onto the :mod:`command_center.api.models` contract;
* applies the BANK/LEGAL redaction policy — a contact/message/invitation whose
  ``project_ref`` is sensitive is dropped from every list and reads as *not
  found* on detail, and a manual write naming a sensitive project is rejected, so
  a sensitive handle never leaves this surface (the drop-don't-mask policy the
  Wave-1/2 services already apply);
* **owns the feedback→task invariant**: submitting feedback always yields an
  actionable board task, created through the single tasks writer, plus the
  captured intake message and the ``feedback`` signal event;
* **owns the Council seam**: an invite records an invitation carrying a stable
  ``council_ref`` and publishes the seam event — it wires no external identity.
* publishes domain events onto the in-process bus after a write commits.

Testability seam: every backing call (repository functions, ``create_task``,
``is_sensitive``, ``resolve_db_path``) is referenced through a module-level name
so a test can monkeypatch it, and the runtime db path resolves under the per-test
``AICC_DATA_DIR`` sandbox (see ``tests/conftest.py``).

Layering note: the tasks path is reached **only** through
``tasks_repository.create_task`` — the documented single writer of
``data/tasks.json`` — never by touching the store directly here.
"""

from __future__ import annotations

from pathlib import Path

from command_center.api import models
from command_center.api import networking_schemas as s
from command_center.events import (
    NetworkingContactInvited,
    NetworkingFeedbackReceived,
    default_bus,
)
from command_center.models import PROJECT_IDS, SENSITIVE_PROJECT_IDS
from command_center.project_config import is_sensitive
from command_center.runtime import db
from command_center.runtime.db.core import current_schema_version, resolve_db_path
from command_center.runtime.db.schema import SCHEMA_VERSION
from command_center.tasks_repository import create_task

# Repo root is three levels up: <root>/command_center/networking/service.py
ROOT = Path(__file__).resolve().parents[2]


class SensitiveProjectRefError(Exception):
    """Raised when a manual write names a BANK/LEGAL project. A sensitive row is
    redacted on every read anyway, so the write is *rejected* (HTTP 400) rather
    than persisted — its handle/body never lands in the store."""


class UnknownProjectRefError(Exception):
    """Raised when feedback names a ``project_ref`` that is not a real
    ``models.PROJECT_IDS`` namespace. A task in an unknown project is otherwise
    silently dropped from every project-scoped view (see
    ``tasks_repository.validate_tasks``), so the intake refuses it up front
    (HTTP 422) instead of creating an orphan task."""


class ContactNotFoundError(Exception):
    """Raised when feedback/invite references a contact that does not exist or is
    sensitive (treated as not found) — surfaced as HTTP 404 so the intake never
    captures a message or task against a phantom contact."""


def _sensitive_projects() -> list[str]:
    """The redaction exclusion list handed to the repository, in a stable order —
    the same policy :func:`is_sensitive` enforces per-row, expressed as a set so
    it can be applied inside the SQL query."""
    return sorted(SENSITIVE_PROJECT_IDS)


def _db_path() -> Path:
    """The runtime db path, migrated to the current schema if it lags. ``migrate``
    is idempotent; the version pre-check keeps the hot path a single cheap read on
    an already-current db while a brand-new sandbox db (each test) migrates once."""
    path = resolve_db_path(ROOT)
    if current_schema_version(path) < SCHEMA_VERSION:
        db.migrate(path)
    return path


# --------------------------------------------------------------------------
# Row -> contract-model mapping
# --------------------------------------------------------------------------


def _contact_from_row(row: dict) -> models.Contact:
    return models.Contact(
        id=row["id"],
        display_name=row.get("display_name") or "",
        handle=row.get("handle") or "",
        org=row.get("org"),
        note=row.get("note"),
        project_ref=row.get("project_ref"),
        created_at=row.get("created_at"),
    )


def _message_from_row(row: dict) -> models.Message:
    return models.Message(
        id=row["id"],
        contact_id=row["contact_id"],
        direction=row["direction"],
        kind=row["kind"],
        body=row.get("body") or "",
        project_ref=row.get("project_ref"),
        created_at=row.get("created_at"),
    )


def _invitation_from_row(row: dict) -> models.Invitation:
    return models.Invitation(
        id=row["id"],
        contact_id=row["contact_id"],
        council_ref=row["council_ref"],
        status=row["status"],
        note=row.get("note"),
        project_ref=row.get("project_ref"),
        invited_at=row.get("invited_at"),
        responded_at=row.get("responded_at"),
    )


def _require_visible_contact(path: Path, contact_id: str) -> dict:
    """Load a contact, treating a missing or sensitive one as not found."""
    row = db.get_contact(path, contact_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        raise ContactNotFoundError(f"contact {contact_id!r} not found")
    return row


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------


def create_contact(payload: s.ContactCreate) -> models.Contact:
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"contact for sensitive project {payload.project_ref!r} is rejected"
        )
    row = db.create_contact(
        _db_path(),
        display_name=payload.display_name,
        handle=payload.handle,
        org=payload.org,
        note=payload.note,
        project_ref=payload.project_ref,
    )
    return _contact_from_row(row)


def list_contacts(
    *, project: str | None = None, limit: int = 100, offset: int = 0
) -> s.ContactList:
    rows = db.list_contacts(
        _db_path(),
        project=project,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    # Redaction happens in the SQL query (``exclude_projects``), so ``limit``/
    # ``offset`` page over visible rows only.
    return s.ContactList(
        contacts=[_contact_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_contact(contact_id: str) -> models.Contact | None:
    row = db.get_contact(_db_path(), contact_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        # A sensitive contact reads as absent — its handle must never leak.
        return None
    return _contact_from_row(row)


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


def create_message(payload: s.MessageCreate) -> models.Message:
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"message for sensitive project {payload.project_ref!r} is rejected"
        )
    path = _db_path()
    contact = _require_visible_contact(path, payload.contact_id)
    # A message inherits its project from an explicit payload ref, else the
    # contact's — so redaction and routing stay consistent with the contact.
    project_ref = payload.project_ref or contact.get("project_ref")
    row = db.create_message(
        path,
        contact_id=payload.contact_id,
        direction=payload.direction,
        kind="note",
        body=payload.body,
        project_ref=project_ref,
    )
    return _message_from_row(row)


def list_messages(
    *, contact_id: str | None = None, limit: int = 100, offset: int = 0
) -> s.MessageList:
    rows = db.list_messages(
        _db_path(),
        contact_id=contact_id,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    return s.MessageList(
        messages=[_message_from_row(r) for r in rows], limit=limit, offset=offset
    )


# --------------------------------------------------------------------------
# Feedback intake — capture + actionable task (the acceptance path)
# --------------------------------------------------------------------------


def submit_feedback(payload: s.FeedbackCreate) -> s.FeedbackResponse:
    """Capture inbound feedback and turn it into an **actionable board task**.

    The acceptance invariant of this wave: a submitted piece of feedback always
    yields a task on the board. The feedback is captured as an inbound
    ``feedback``-kind message, then a task is created through the single tasks
    writer (``tasks_repository.create_task``), and a
    :class:`NetworkingFeedbackReceived` event is published — which doubles as the
    ``feedback`` signal the advisor can consume without this layer touching
    advisor internals.

    ``project_ref`` must be a real, non-sensitive ``models.PROJECT_IDS`` project:
    a sensitive one is rejected (redaction) and an unknown one is refused (an
    orphan task would be dropped from every project-scoped view).
    """
    if is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"feedback for sensitive project {payload.project_ref!r} is rejected"
        )
    if payload.project_ref not in PROJECT_IDS:
        raise UnknownProjectRefError(
            f"feedback names unknown project {payload.project_ref!r}"
        )
    path = _db_path()
    # Validate the contact exists and is visible before capturing anything.
    _require_visible_contact(path, payload.contact_id)

    # 1) Capture the inbound feedback as a message (the audit trail of what was
    #    said), then 2) create the actionable task through the single writer.
    message = db.create_message(
        path,
        contact_id=payload.contact_id,
        direction="inbound",
        kind="feedback",
        body=payload.body or payload.title,
        project_ref=payload.project_ref,
    )
    task = create_task(
        ROOT,
        project=payload.project_ref,
        title=payload.title,
        task_type="implementation",
        status="Backlog",
        goal=payload.body or payload.title,
        notes=f"Actionable feedback from networking contact {payload.contact_id}",
    )
    default_bus().publish(
        NetworkingFeedbackReceived(
            message_id=message["id"],
            task_id=task["id"],
            contact_id=payload.contact_id,
            project_ref=payload.project_ref,
        ),
        raise_errors=False,
    )
    return s.FeedbackResponse(message=_message_from_row(message), task_id=task["id"])


# --------------------------------------------------------------------------
# Invitations — the Council seam
# --------------------------------------------------------------------------


def _council_ref_for(contact_id: str) -> str:
    """Mint a stable, opaque Council reference for an invitation. The Council
    engine consumes this seam; it carries no external identity."""
    return f"council-invite:{contact_id}"


def invite_contact(payload: s.InviteCreate) -> models.Invitation:
    """Invite a networking contact to the Council: record an invitation carrying a
    stable ``council_ref`` and publish :class:`NetworkingContactInvited`. This is
    the boundary the Council engine consumes — no external identity/auth is wired.

    ``project_ref`` inherits from the contact when omitted; a sensitive project is
    rejected on the manual override."""
    if payload.project_ref and is_sensitive(payload.project_ref):
        raise SensitiveProjectRefError(
            f"invitation for sensitive project {payload.project_ref!r} is rejected"
        )
    path = _db_path()
    contact = _require_visible_contact(path, payload.contact_id)
    project_ref = payload.project_ref or contact.get("project_ref")
    council_ref = payload.council_ref or _council_ref_for(payload.contact_id)
    row = db.create_invitation(
        path,
        contact_id=payload.contact_id,
        council_ref=council_ref,
        note=payload.note,
        project_ref=project_ref,
    )
    default_bus().publish(
        NetworkingContactInvited(
            invitation_id=row["id"],
            contact_id=row["contact_id"],
            council_ref=row["council_ref"],
            project_ref=row.get("project_ref"),
        ),
        raise_errors=False,
    )
    return _invitation_from_row(row)


def list_invitations(
    *, status: str | None = None, contact_id: str | None = None,
    limit: int = 100, offset: int = 0,
) -> s.InvitationList:
    rows = db.list_invitations(
        _db_path(),
        status=status,
        contact_id=contact_id,
        exclude_projects=_sensitive_projects(),
        limit=limit,
        offset=offset,
    )
    return s.InvitationList(
        invitations=[_invitation_from_row(r) for r in rows], limit=limit, offset=offset
    )


def get_invitation(invitation_id: str) -> models.Invitation | None:
    row = db.get_invitation(_db_path(), invitation_id)
    if row is None or is_sensitive(row.get("project_ref") or ""):
        return None
    return _invitation_from_row(row)
