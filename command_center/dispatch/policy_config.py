"""Persistence for the config-driven `DispatchPolicy`.

This module is the **single writer** of `data/dispatch_policy.json` (see
`docs/AUTHORITY_MAP.md`). It mirrors `pipeline_settings.py`'s primitives
exactly — atomic-replace writes guarded by a cross-process advisory file lock
via `command_center.storage` — so a read-modify-write from two sessions cannot
tear the file. No SQL, no business logic: just load/save of the policy value
object, which keeps the layering honest (Service calls the config store, the
store owns the file). Named like `pipeline_settings` — a config store, not an
engine — so it does not read as new persistence-engine growth.
"""

from __future__ import annotations

import contextlib
import json

from pathlib import Path
from typing import TYPE_CHECKING

from command_center import models, storage
from command_center.dispatch.models import DispatchPolicy

if TYPE_CHECKING:  # a type-only import: the config store stays free of FastAPI
    from command_center.http_auth.identity import Principal

POLICY_FILE_NAME = "dispatch_policy.json"
POLICY_LOCK_FILE_NAME = "dispatch_policy.lock"

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05


def policy_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / POLICY_FILE_NAME


def policy_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / POLICY_LOCK_FILE_NAME


@contextlib.contextmanager
def policy_lock(root: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Cross-process mutual exclusion for the policy read-modify-write cycle —
    the same OS advisory-lock primitive as `pipeline_settings.settings_lock`."""
    with storage.file_lock(
        policy_lock_path(root), timeout=timeout, poll_seconds=_LOCK_POLL_SECONDS
    ):
        yield


class UnreadablePolicy(RuntimeError):
    """The policy file exists but could not be turned into a policy — an OS
    error, malformed JSON, or a document that is not a JSON object.

    Raised instead of falling back to `DispatchPolicy()`, because that fallback
    was the last fail-open input to `plan()`. The defaults are only *safe* as
    the answer to "nothing has been configured yet"; as the answer to "the
    configuration could not be read" they are the opposite, because every
    guardrail in this file is expressed by its *presence*:
    `per_agent_limits` and `per_project_limits` default to empty, and empty
    means "no per-agent concurrency limit, no per-agent spend limit, no project
    ceiling" — precisely the limits an operator edits this file to impose. A
    truncated write therefore does not degrade the policy, it deletes it, and
    reports the deletion as a healthy plan.

    Measured on the real `plan()`: a policy pinning `claude_code` to
    `max_concurrent=1` assigns 1 of 3 queued tasks; corrupt that same file mid
    write and the identical call assigns **3 of 3**, with
    `kill_switch_engaged`, `budget_unknown` and `capacity_unknown` all still
    reading `False`. Nothing in the response says the policy was lost.

    `load_policy` therefore separates the two cases by reading the file itself
    rather than through `storage.read_json`, whose swallow-and-default is right
    for a display surface and wrong for a guardrail: it collapses "absent",
    "malformed" and "unreadable" into one value before `from_dict` can tell
    them apart. A *missing* file is still the defaults — that is a fresh
    install, not a failure.
    """


def _read_policy_document(root: Path) -> dict:
    """The policy file's JSON object, `{}` when no policy has been saved yet.

    Fails closed on everything in between; see `UnreadablePolicy`. An existing
    but empty file counts as unreadable rather than unconfigured: writes go
    through `storage.atomic_write_json`, which never produces a zero-byte
    policy, so emptiness is a torn write rather than a state an operator can
    legitimately have asked for.
    """
    path = policy_file_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        # A decode failure is a `ValueError`, not an `OSError`, so it needs its
        # own arm or it escapes untyped past every `except UnreadablePolicy`.
        # Mirrors `pipeline_settings.read_settings_document`.
        raise UnreadablePolicy(
            f"dispatch policy at {path} is not UTF-8 text: {exc}"
        ) from exc
    except OSError as exc:
        raise UnreadablePolicy(
            f"dispatch policy at {path} could not be read: {exc}"
        ) from exc
    if not raw.strip():
        raise UnreadablePolicy(
            f"dispatch policy at {path} is empty; an atomically-written policy "
            "is never zero bytes, so this is a torn write, not an unset policy"
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise UnreadablePolicy(
            f"dispatch policy at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise UnreadablePolicy(
            f"dispatch policy at {path} is a {type(document).__name__}, not a "
            "JSON object; it cannot express a policy"
        )
    return document


def load_policy(root: Path) -> DispatchPolicy:
    """Read the persisted policy, or the safe defaults if nothing is saved yet.
    Unlocked by design (a plain read of an atomically-written file); use
    `save_policy` / `update_policy` for anything that writes.

    Raises `UnreadablePolicy` when the file is present but unusable, rather
    than silently returning the defaults — see that class. `service.plan`
    turns the exception into the `policy_unknown` gate, the same way it turns
    an unreadable runtime store into `budget_unknown` / `capacity_unknown`.
    """
    return DispatchPolicy.from_dict(_read_policy_document(root))


def save_policy(
    root: Path, policy: DispatchPolicy, *, actor: str | None = None
) -> DispatchPolicy:
    """Persist `policy` wholesale under `policy_lock`, stamping provenance."""
    stamped = _stamp(policy, actor)
    with policy_lock(root):
        storage.atomic_write_json(policy_file_path(root), stamped.as_dict())
    return stamped


def update_policy(
    root: Path, changes: dict, *, principal: "Principal"
) -> DispatchPolicy:
    """Lost-update-safe partial update: re-read the current policy under the
    lock, overlay `changes` (validated through `DispatchPolicy.from_dict`) and
    write back — so two concurrent edits of different fields don't clobber.

    This is the HTTP-reachable mutator, so it takes a `Principal` and has no
    `actor` parameter to forge: `updated_by` is the authenticated caller or the
    call does not typecheck (VOYN-W0-AICC-AUTH-HTTP-01). `save_policy` below
    keeps its `actor` string — it is the persistence primitive, reachable only
    from in-process callers that already have their own provenance, and giving
    it a `Principal` would force every local caller to fabricate one.

    Propagates `UnreadablePolicy` rather than merging onto the defaults, and
    that matters more here than on the read path: a partial update layered over
    a policy that failed to load would *persist* the empty limit maps, turning
    a recoverable corrupt file into a permanently guardrail-free one, with an
    authenticated operator's name stamped on it as if they had asked for it.
    Editing one field is not consent to drop every limit the file used to hold.
    The remedy for a corrupt policy is `save_policy`, which states the whole
    policy explicitly instead of inheriting the unreadable part."""
    with policy_lock(root):
        current = DispatchPolicy.from_dict(_read_policy_document(root))
        merged = dict(current.as_dict())
        merged.update(changes or {})
        stamped = _stamp(DispatchPolicy.from_dict(merged), principal.principal_id)
        storage.atomic_write_json(policy_file_path(root), stamped.as_dict())
    return stamped


def _stamp(policy: DispatchPolicy, actor: str | None) -> DispatchPolicy:
    import dataclasses

    return dataclasses.replace(
        policy, updated_at=models.iso_now(), updated_by=actor or policy.updated_by
    )
