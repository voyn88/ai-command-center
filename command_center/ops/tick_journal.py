"""Host-local, append-only record of control-plane tick runs.

VOYN-W0-AICC-PR-WINDOW-TIMER-NOT-DEPLOYED-ON-CONTROL. A oneshot tick that was
never installed and a oneshot tick that ran and found nothing to do look
identical from outside: both leave nothing behind. That is how the PR
review-window reconciler stayed unnoticed for over a day while 67 open PRs
(752-822) carried no queue label and operators labelled them by hand --
nothing on control-01 could answer "when did this tick last run, and what did
it do?", so "the timer is not installed" was indistinguishable from "the
timer is installed and the fleet is quiet".

This is that answer: one JSON object per line per run, written by the tick
itself at the tick boundary, for failures exactly as much as for successes. A
tick that failed and recorded nothing is the case the record exists for, so
the outcome travels *in* the row rather than deciding whether there is one.

Deliberately best-effort, for the same reason `self_deploy` writes its
provenance that way: the journal is evidence, not a gate. A tick that cannot
write its row still did its work, and journald still carries its stdout, so
`record_tick` reports whether it wrote and never raises.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_JOURNAL_PATH",
    "JOURNAL_PATH_ENV",
    "journal_path",
    "read_last_run",
    "record_tick",
]

#: Env var a unit sets to move the journal off the running user's home. The
#: control units run as `voynadmin` with a writable home (no `ProtectHome`),
#: so the default needs no host layout -- which is the property that kept the
#: reconciler undeployable in the first place and must not come back here.
JOURNAL_PATH_ENV = "AICC_TICK_JOURNAL"
DEFAULT_JOURNAL_PATH = "~/.aicc-tick-journal.jsonl"

#: Per-value bound on the caller's detail. A tick's `error` can carry a whole
#: GitHub response body; a journal row that big is a journal nobody reads.
_DETAIL_VALUE_CAP = 500

#: Fields the journal owns. A caller's detail may not shadow them: the row's
#: own identity must not be something a tick can overwrite by accident.
_RESERVED = ("at", "tick", "outcome")


def journal_path(override: str | None = None) -> Path:
    """Where this host's tick journal lives: argument, env, then default."""
    raw = override or os.environ.get(JOURNAL_PATH_ENV) or DEFAULT_JOURNAL_PATH
    return Path(raw).expanduser()


def record_tick(
    tick: str,
    outcome: str,
    *,
    detail: dict[str, Any] | None = None,
    path: str | None = None,
) -> bool:
    """Append one run to the journal; return whether the row was written.

    `outcome` is the tick's own verdict (`ok`/`failed`), never inferred here:
    a reconciler that listed nothing because GitHub refused it has run, and
    the difference between that and a clean tick is the whole point of the
    row.
    """
    row: dict[str, Any] = {
        key: (value[:_DETAIL_VALUE_CAP] if isinstance(value, str) else value)
        for key, value in (detail or {}).items()
        if key not in _RESERVED
    }
    row["at"] = datetime.datetime.now(datetime.UTC).isoformat()
    row["tick"] = tick
    row["outcome"] = outcome
    try:
        target = journal_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            # `default=str` so an unexpected object in the detail degrades to
            # its repr instead of raising out of a best-effort writer.
            line = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            handle.write(line + "\n")
    except OSError:
        return False
    return True


def read_last_run(tick: str, *, path: str | None = None) -> dict[str, Any] | None:
    """The most recent recorded run of `tick`, or None if there is none.

    The read side of the acceptance evidence: "no manual labelling for 48h"
    is only checkable against a record of what the tick did in those 48
    hours. Never raises -- an unreadable or malformed journal answers "no
    recorded run", which is what an operator needs to act on anyway.
    """
    last: dict[str, Any] | None = None
    try:
        with journal_path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # A torn final line (a tick killed mid-write) invalidates
                    # that row, not the journal: keep the last good one.
                    continue
                if isinstance(row, dict) and row.get("tick") == tick:
                    last = row
    except OSError:
        return None
    return last
