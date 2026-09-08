"""Persistence for per-agent `AttestationRecord`s (VOYN-AGT-ATTEST).

Single writer of `data/agent_attestation.json` (see `docs/AUTHORITY_MAP.md`).
Mirrors `dispatch.policy_config`'s primitives exactly — atomic-replace writes
guarded by a cross-process advisory file lock via `command_center.storage` —
so two sessions recording certification evidence for different agents at the
same time cannot tear the file or clobber each other's agent.

No business logic here: `attestation.evaluate_attestation` is the pure
decision, this module only loads/saves the evidence it decides on.
"""

from __future__ import annotations

import contextlib
import dataclasses

from pathlib import Path

from command_center import models, storage
from command_center.dispatch.attestation import AttestationRecord

RECORD_FILE_NAME = "agent_attestation.json"
RECORD_LOCK_FILE_NAME = "agent_attestation.lock"

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05


def record_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / RECORD_FILE_NAME


def record_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / RECORD_LOCK_FILE_NAME


@contextlib.contextmanager
def record_lock(root: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Cross-process mutual exclusion for the store's read-modify-write cycle
    — the same OS advisory-lock primitive as `policy_config.policy_lock`."""
    with storage.file_lock(
        record_lock_path(root), timeout=timeout, poll_seconds=_LOCK_POLL_SECONDS
    ):
        yield


def load_records(root: Path) -> dict[str, AttestationRecord]:
    """Read every persisted record, keyed by agent id, or an empty map if
    nothing is saved yet. An agent absent from the returned map has no
    evidence on file — `attestation.evaluate_attestation(None)` (uncertified)
    is what a caller gets for it, so a never-recorded agent fails closed by
    construction rather than by a caller remembering to check. Unlocked by
    design (a plain read of an atomically-written file); use `save_record` for
    anything that writes."""
    raw = storage.read_json(record_file_path(root), {})
    if not isinstance(raw, dict):
        return {}
    return {
        agent_id: AttestationRecord.from_dict(agent_id, data)
        for agent_id, data in raw.items()
        if isinstance(agent_id, str) and agent_id.strip()
    }


def save_record(
    root: Path, record: AttestationRecord, *, actor: str | None = None
) -> AttestationRecord:
    """Upsert `record` under its `agent_id`, re-reading the store under the
    lock so a concurrent write recording a *different* agent's evidence is
    never lost (lost-update-safe partial update, like
    `policy_config.update_policy`)."""
    stamped = dataclasses.replace(
        record, recorded_at=models.iso_now(), recorded_by=actor or record.recorded_by
    )
    with record_lock(root):
        raw = storage.read_json(record_file_path(root), {})
        current = dict(raw) if isinstance(raw, dict) else {}
        current[stamped.agent_id] = stamped.as_dict()
        storage.atomic_write_json(record_file_path(root), current)
    return stamped
