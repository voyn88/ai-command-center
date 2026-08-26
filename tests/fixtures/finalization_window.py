"""Widening the finalization window, for any process that supervises a run.

Extracted from `widen_finalization.py`, which held the only implementation and
could only widen the *debug CLI*: it patches, then `runpy`s
`scripts/execution_center_debug.py`. `finalization_kill_probe.py` needs the same
widening around a bare `Supervisor`, and a second copy of this logic is the kind
of duplicate authority that drifts — one of them learns that `new_state` moved
to a positional argument and the other silently stops widening anything.

So the mechanism lives here and both callers use it. What it does, and does not
do, is worth keeping stated: it holds the supervisor's thread **after**
`update_run_state` has already returned, so every write still happens in the
same order and no product logic changes. Only the gap between two steps that
were already in this sequence gets larger.

The self-proof is the part that must not be lost in the move. The original
version of the CLI wrapper was a `sitecustomize` module, and it failed silently
— at `sitecustomize` time `command_center` is not importable, so the patch never
applied and the guard went green while measuring nothing. Its replacement then
checked `db.update_run_state is not _patched` on the line after the assignment,
which is a tautology. Review made the widener inert by renaming the keyword it
reads, deleted the guard under test, and the suite passed three times out of
three. `fired()` therefore reports whether the sleep actually *happened*, not
whether the patch was installed, and callers are expected to refuse to report
success without it.
"""

from __future__ import annotations

import time

__all__ = ["fired", "widen"]

_slept = 0


def fired() -> int:
    """How many terminal-state writes actually went through the widener.

    Zero means nothing was widened, whatever the patch looks like — which is the
    only question worth asking, and the one three previous versions of this
    fixture answered wrongly.
    """
    return _slept


def widen(seconds: float) -> None:
    """Hold the supervising thread for `seconds` after each terminal-state write.

    Patches `command_center.runtime.db.update_run_state` in place, so it must be
    called after `command_center` is importable and before the run is launched.
    """
    from command_center.runtime import db

    terminal = set(db.TERMINAL_STATES)
    original = db.update_run_state

    def update_run_state_then_hold(*args, **kwargs):
        global _slept
        result = original(*args, **kwargs)
        # `new_state` is keyword-only in `db.execution.update_run_state`, so a
        # positional call cannot occur without a signature change — but a rename
        # or a relocation of the terminal write would make this condition stop
        # matching, which is exactly what `fired()` exists to expose.
        if kwargs.get("new_state") in terminal:
            _slept += 1
            time.sleep(seconds)
        return result

    db.update_run_state = update_run_state_then_hold
