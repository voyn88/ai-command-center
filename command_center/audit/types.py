"""Value objects shared across the Audit engine.

The engine is a pipeline: **checks** turn local, in-repo signals (ruff output,
coverage data, requirement pins) into :class:`Finding` value objects; the
:mod:`command_center.api.audit_service` write service dedups them and persists
the survivors as ``audit_finding`` rows through the Wave-2 repository, always
stamping a ``status`` and an ``owner``.

Nothing here touches storage or the network — these are plain, immutable
dataclasses so a check and the runner can be exercised in isolation with
hand-built inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

#: Default owner (a *role*, never a person) for each check category. Every
#: finding carries an owner; a check that does not name a more specific assignee
#: falls back to its category's role here, so the acceptance invariant — a
#: finding always has an owner — holds by construction before the row is even
#: written. The persistence boundary is the hard backstop (it refuses an empty
#: owner); this map is what makes the common path always satisfy it.
DEFAULT_OWNERS: dict[str, str] = {
    "security": "security",
    "coverage": "qa",
    "code-quality": "engineering",
    "deps": "platform",
    "lint": "engineering",
}


def default_owner_for(category: str) -> str:
    """The role that owns findings of ``category`` by default. Falls back to a
    generic ``"engineering"`` owner for an unmapped category so this function
    can never return an empty owner."""
    return DEFAULT_OWNERS.get(category, "engineering")


def normalize_summary(summary: str) -> str:
    """Case- and whitespace-insensitive normalization used to build a finding's
    dedup signature, so two checks reporting the same issue with cosmetically
    different spacing collapse onto one key."""
    return " ".join(str(summary).strip().lower().split())


@dataclass(frozen=True, slots=True)
class Finding:
    """A single issue a check wants to raise, before persistence.

    ``owner`` is the role (or assignee) accountable for the finding — a check
    always sets one (defaulting to its category's role via
    :func:`default_owner_for`), so the value object can never carry an empty
    owner. ``dedup_key`` is an optional stable signature; when empty the runner
    derives one from ``category``/``file_path``/``loc``/normalized ``summary`` so
    two checks (or two passes) never raise the same finding twice. ``source``
    records the originating check for provenance and is **never** persisted."""

    category: str
    summary: str
    owner: str
    severity: str = "info"
    file_path: str | None = None
    loc: str | None = None
    dedup_key: str = ""
    source: str = ""

    def signature(self) -> str:
        """The dedup signature: ``dedup_key`` if the check set one, else a
        deterministic key over category, location and normalized summary."""
        if self.dedup_key:
            return self.dedup_key
        return (
            f"{self.category}|{self.file_path or ''}|{self.loc or ''}"
            f"|{normalize_summary(self.summary)}"
        )


@dataclass(frozen=True, slots=True)
class CheckContext:
    """Read-only inputs handed to every check on a pass.

    ``target`` is the directory a check scans (a project's repository path, or
    the app root when the project has none configured). ``db_path`` is an
    already-migrated runtime db path for checks that read local signals through
    the ``command_center.runtime.db`` facade. ``options`` is a free-form bag the
    caller can use to tune a check (thresholds, an injected data path) without
    touching code."""

    root: Path
    target: Path
    project: str
    db_path: Path
    options: Mapping[str, Any] = field(default_factory=dict)
