"""Run the debug CLI with the finalization race widened to a certainty.

The defect this guards against — a CLI that returns the moment a run row turns
terminal, truncating the `process_exited` event, the auto-commit of the agent's
work and the run report — reproduces about once in a hundred runs. A test that
merely launches the CLI therefore passes with the fix reverted, which is
exactly what independent review demonstrated about the test that shipped
alongside the fix.

So the window is widened rather than raced for. This wrapper holds the
supervisor's daemon thread for `AICC_TEST_WIDEN_FINALIZATION_SECONDS`
immediately after the *terminal* run row is committed and before that thread
appends `process_exited`. It stretches an interval that already exists —
measured at ~2.5 ms median, 41 ms max, against the CLI's 200 ms poll interval —
until no poll tick can land outside it. Ordering and product logic are
untouched; only the gap between two steps that were already in this order.

It is a wrapper script rather than a `sitecustomize` module because the first
attempt was one, and it **failed silently**: at `sitecustomize` time
`command_center` is not importable yet, so the patch never applied, Python
printed a line nobody reads, and the guard went green while measuring nothing.
That is the same class of false gate this whole series exists to remove, so
this version refuses to run unless it can prove it patched what it meant to.
"""

from __future__ import annotations

import atexit
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "execution_center_debug.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_WIDEN = float(os.environ.get("AICC_TEST_WIDEN_FINALIZATION_SECONDS", "0") or 0)
if _WIDEN <= 0:
    raise SystemExit(
        "AICC_TEST_WIDEN_FINALIZATION_SECONDS must be set to a positive number; "
        "this wrapper exists only to widen the finalization window, and running "
        "it without one would quietly measure nothing"
    )

# The widening itself now lives in `finalization_window`, because the kill probe
# needs the same mechanism around a bare `Supervisor` rather than around this
# CLI, and two copies of it would drift apart exactly where it matters. What
# this file keeps is what is specific to wrapping the CLI: the argv rewrite, the
# marker file, and the refusal below.
from tests.fixtures import finalization_window  # noqa: E402  (after sys.path setup)

finalization_window.widen(_WIDEN)

# Proof that it *fired*, not that it was installed.
#
# The previous version checked `_db.update_run_state is not _update_run_state`
# on the line after the assignment — a tautology on a plain module object, and
# `if not _TERMINAL` only asked whether a constant was non-empty. Review made
# the widener inert by renaming the keyword it reads, then also deleted the
# guard being tested, and the test passed three times out of three. Both the
# fixture and the fix could be removed together with the suite green: the
# `sitecustomize` failure this file was written to design out, one layer over.
#
# So the sleep records itself, and the wrapper refuses to exit successfully if
# it never happened. A test asserting `returncode == 0` then holds the fixture
# as well as the product.
_MARKER = os.environ.get("AICC_TEST_WIDEN_MARKER")
_cli_code: object = 0


@atexit.register
def _refuse_to_report_an_unwidened_run() -> None:
    slept = finalization_window.fired()
    if slept:
        if _MARKER:
            Path(_MARKER).write_text(str(slept), encoding="utf-8")
        return
    sys.stderr.write(
        "widen_finalization: the terminal-state write never went through this "
        "wrapper, so nothing was widened and the run proves nothing about the "
        "race it was meant to expose.\n"
    )
    if _cli_code not in (0, None):
        # The CLI already failed and already said why. Overriding its code with
        # this one would replace a real diagnosis with a fixture's complaint.
        return
    os._exit(97)

sys.argv = [str(CLI), *sys.argv[1:]]
try:
    runpy.run_path(str(CLI), run_name="__main__")
except BaseException as exit_request:  # noqa: BLE001 — see below
    # Remember what the CLI itself decided, so the check above can override
    # only a *success*. Review pointed out that a CLI failing before the first
    # terminal write reported 97 instead of its own code — the diagnostics
    # survive, since CPython flushes the std files before the hook, but a
    # non-zero code replaced by a different non-zero code is a worse report
    # than the one it replaced.
    # `BaseException`, not `SystemExit`. Review showed the narrower catch
    # covered only the orderly exit: an uncaught exception — the commonest
    # pre-terminal-write failure — still had its code replaced by the
    # fixture's 97, so a crash was reported as "nothing was widened".
    _cli_code = exit_request.code if isinstance(exit_request, SystemExit) else 1
    raise
