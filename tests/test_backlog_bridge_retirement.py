"""The migration bridge's retirement date is enforced, not just written down.

VOYN-W0-BACKLOG-ORCHESTRATOR BO-S4 admits a *bidirectional* backlog bridge
"only for the migration window, with an explicit removal date": the store
renders markdown (`backlog-export`) while the owner's hand-edited file is
still imported back (`backlog-import` / `ops/aicc_backlog_publish.py`).
ADR-0011 names both the condition and the date that ends the import half.

That ADR rejects "no explicit date, condition only" in its own words: a
bridge with no calendar backstop "has no forcing function if the condition
is simply never checked". Until now the date it chose instead had the same
problem one level up -- it was prose in four files, and nothing checked it
either, so 2026-11-02 would arrive with both directions live and no signal.
This module is that check. It turns the deadline into the one thing this
repository cannot ignore: a red test.

The failure is deliberately not silenceable by waiting. Options when it
fires are exactly the ADR's own: delete the import side (the outcome the
condition is written for), or make a new explicit owner decision by moving
the date in the ADR -- a one-line edit, which this test then re-pins.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "adr" / "0011-backlog-projection-bidirectional-bridge.md"

#: The machine-readable form of the ADR's deadline. The ADR is the single
#: source of truth for the date -- restating it here as a literal would
#: recreate exactly the drift this file exists to catch.
_TARGET_DATE = re.compile(
    r"^\*\*Target date:\*\*\s*(\d{4}-\d{2}-\d{2})\b", re.MULTILINE
)

#: What "the import side" concretely is, from ADR-0011's revisit condition:
#: "`backlog-import`, `ops/aicc_backlog_publish.py` and the launchd job that
#: drives it are deleted outright". Retirement means these are gone, so their
#: continued existence is what the deadline is measured against. Each entry is
#: (path, marker) -- a marker of `None` means the file's mere existence counts,
#: while `cli.py` survives retirement and only loses one subcommand.
_IMPORT_SIDE: tuple[tuple[str, str | None], ...] = (
    ("ops/aicc_backlog_publish.py", None),
    ("deploy/com.ai-command-center.backlog-publish.plist", None),
    ("command_center/db/cli.py", '"backlog-import"'),
)

#: Every file that repeats the ADR's date in prose. They explain the bridge
#: to whoever is reading that file, which is worth the duplication -- but a
#: date extended in the ADR and left stale here would tell a reader the
#: bridge closes sooner than it does.
_CITATIONS: tuple[str, ...] = (
    "deploy/com.ai-command-center.backlog-publish.plist",
    "command_center/backlog_client.py",
    "command_center/db/backlog_export.py",
)


def _target_date() -> date:
    matches = _TARGET_DATE.findall(ADR.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"ADR-0011 must state exactly one machine-readable '**Target date:** "
        f"YYYY-MM-DD' line; found {len(matches)}: {matches}. The bridge's "
        "deadline is only enforceable while it stays parseable."
    )
    return date.fromisoformat(matches[0])


def _surviving_import_side() -> list[str]:
    surviving = []
    for relative, marker in _IMPORT_SIDE:
        path = ROOT / relative
        if not path.is_file():
            continue
        if marker is None or marker in path.read_text(encoding="utf-8"):
            surviving.append(relative)
    return surviving


def _is_overdue(today: date, target: date, surviving: list[str]) -> bool:
    """The deadline is a decision point, not a countdown: it only fires while
    the import side is actually still here. Once it is deleted the bridge is
    single-direction and there is nothing left for a date to force, so this
    stops firing on its own rather than needing the guard itself removed."""
    return bool(surviving) and today >= target


def test_the_adr_states_exactly_one_machine_readable_target_date() -> None:
    """The deadline can only be enforced while a machine can read it. Reword
    the line and this fails here -- loudly and immediately -- instead of the
    enforcement below quietly passing forever on a date it can no longer
    find."""
    target = _target_date()
    assert target > date(2026, 1, 1)


def test_the_guard_fires_only_after_the_date_and_only_while_import_survives() -> None:
    """The verdict itself, at simulated dates, because the live assertion
    below can only ever exercise one point on the calendar.

    Fires *on* the target date, not the day after: the ADR asks for the
    decision "by then"."""
    target = date(2026, 11, 1)
    surviving = ["ops/aicc_backlog_publish.py"]

    assert _is_overdue(date(2026, 10, 31), target, surviving) is False
    assert _is_overdue(date(2026, 11, 1), target, surviving) is True
    assert _is_overdue(date(2027, 1, 1), target, surviving) is True
    # Retired: the bridge is single-direction, so no date forces anything.
    assert _is_overdue(date(2027, 1, 1), target, []) is False


def test_the_import_side_is_detected_while_it_is_still_here() -> None:
    """Guards the guard. If a rename made `_surviving_import_side` blind, the
    deadline check above would pass forever by looking at nothing -- the
    failure mode of every 'assert the thing is gone' test. Today the import
    side is very much present, so an empty result means broken detection, not
    a finished migration; when it really is deleted, this test is deleted with
    it, in the same commit that removes the files it names."""
    assert _surviving_import_side() == [relative for relative, _ in _IMPORT_SIDE]


def test_every_file_quoting_the_deadline_quotes_the_current_one() -> None:
    """The date is repeated in prose across the code that implements each
    half of the bridge. Moving it in the ADR alone would leave those readers
    with a date that has already passed, or one that never applied.

    One-directional by construction: this proves the current date is present,
    not that an older one was removed. A stale date left *beside* the new one
    is a wart this cannot see; a stale date left *instead* of it is caught."""
    target = _target_date().isoformat()
    stale = [
        relative
        for relative in _CITATIONS
        if target not in (ROOT / relative).read_text(encoding="utf-8")
    ]
    assert stale == [], (
        f"ADR-0011's target date is now {target}, but these still cite a "
        f"different one: {stale}"
    )


def test_the_bidirectional_bridge_has_not_outlived_its_window() -> None:
    """The forcing function itself, against the real calendar.

    Deliberately clock-dependent: a deadline that only fires when someone
    remembers to look is the thing ADR-0011 rejected. Green until the date,
    red on it, and red until the owner decides -- which is the entire point.
    """
    target = _target_date()
    surviving = _surviving_import_side()
    # UTC, like every other stamp this system writes: the deadline is a
    # calendar date on the control plane's clock, not on whichever one the
    # runner happens to hold.
    today = datetime.now(UTC).date()
    assert not _is_overdue(today, target, surviving), (
        f"The backlog markdown bridge is past its window: ADR-0011 set "
        f"{target.isoformat()} as the date by which the import half retires, "
        f"and it is still here ({', '.join(surviving)}). This is an owner "
        "decision, not a test to silence. Either (a) the revisit condition "
        "has been met -- delete backlog-import, ops/aicc_backlog_publish.py "
        "and the launchd job outright, supersede ADR-0011 with export-only "
        "projection as final, and delete this module; or (b) it has not -- "
        "move '**Target date:**' in ADR-0011 to a new explicit date, "
        "recording why the bridge still needs to exist."
    )
