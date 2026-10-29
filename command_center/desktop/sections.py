"""Canonical navigation sections for the desktop shell.

The nine top-level sections and their D1 activation state, transcribed from
`INFORMATION_ARCHITECTURE.md` §1 (order) and §2 (which three are active in
Desktop Increment 1). This is the single place the sidebar, the page stack, and
the tests agree on — no second, drifting copy.
"""

from __future__ import annotations

from dataclasses import dataclass

from .i18n import SECTION_LABELS


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    enabled: bool  # False → rendered disabled ("Available in a future release")


# Order remains binding. P2 activates the six operational sections on top of
# the completed native foundation.
SECTIONS: tuple[Section, ...] = (
    Section("home", SECTION_LABELS["home"], enabled=True),
    Section("projects", SECTION_LABELS["projects"], enabled=True),
    Section("sessions", SECTION_LABELS["sessions"], enabled=True),
    Section("execution", SECTION_LABELS["execution"], enabled=True),
    Section("git", SECTION_LABELS["git"], enabled=True),
    Section("artifacts", SECTION_LABELS["artifacts"], enabled=True),
    Section("reports", SECTION_LABELS["reports"], enabled=True),
    Section("agents", SECTION_LABELS["agents"], enabled=True),
    # Remote mode: read-only view of the pipeline via the native gateway.
    Section("pipeline", SECTION_LABELS["pipeline"], enabled=True),
    Section("settings", SECTION_LABELS["settings"], enabled=True),
)

ACTIVE_SECTION_KEYS: tuple[str, ...] = tuple(s.key for s in SECTIONS if s.enabled)
DEFAULT_SECTION_KEY = "home"
