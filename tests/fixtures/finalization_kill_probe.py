"""Launch one real run and SIGKILL this process inside the finalization window.

Used by `tests/test_run_finalized_at.py`. It has to be a separate process
because the property under test is what a *dead* supervisor leaves behind, and a
supervisor that dies inside its own test process takes the test with it.

The kill is self-inflicted from a watcher thread rather than delivered by the
parent, so the only latency between "the run's row is terminal" and "this
process is gone" is one SQLite read — no pipe, no scheduler round trip. That
keeps the probe measuring the width of the window rather than the parent's
reaction time.

Argv: <repo path> <poll seconds>. Prints the run id, then `SURVIVED` if
finalization completed before the watcher could fire. Any other exit is the
kill, which the parent recognises by the -9 status.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

REPO = Path(sys.argv[1])
POLL_SECONDS = float(sys.argv[2])

FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"

from command_center import agent_runner, project_config  # noqa: E402
from command_center.runtime import db, supervisor as supervisor_module  # noqa: E402

from tests.fixtures import finalization_window  # noqa: E402

# The same two substitutions the `fake_claude` fixture makes, done by plain
# assignment because there is no monkeypatch fixture in a bare process: run the
# stub under this interpreter instead of the real `claude` binary, so the
# supervisor still does a genuine `Popen` with a real process group.
supervisor_module.CLAUDE_BINARY = sys.executable
_original_build = supervisor_module.build_claude_command


def _build_against_the_stub(**kwargs):
    command = _original_build(**kwargs)
    return [command[0], str(FAKE_CLAUDE)] + command[1:]


supervisor_module.build_claude_command = _build_against_the_stub

# An implementation run is only classified COMPLETED when it changed the working
# tree, so the stub touches a file — matching the fixture's default.
os.environ["FAKE_CLAUDE_TOUCH_FILE"] = "finalization_kill_probe_touch.txt"

_original_config = project_config.get_project_config


def _config_pointing_at_the_throwaway_repo(project_id):
    config = _original_config(project_id)
    if project_id == "AIOS":
        config["repository_path"] = str(REPO)
    return config


project_config.get_project_config = _config_pointing_at_the_throwaway_repo
agent_runner.project_config.get_project_config = _config_pointing_at_the_throwaway_repo


# --------------------------------------------------------------------------
# Widening the window that already exists
# --------------------------------------------------------------------------
#
# The window between "the terminal row is committed" and "finalization is
# durable" measures a 6.1 ms median with a clean working tree and 139 ms once
# the auto-commit has a real `git commit` to make. The second is wide enough to
# hit unaided — measured 20 of 20 at a 200 ms poll — but "wide enough most of
# the time" is a coin flip dressed as an assertion, and it depends on how fast
# `git` is on the runner.
#
# The mechanism is `finalization_window`'s, shared with the CLI wrapper rather
# than reimplemented here, and it reads the same environment variable so there
# is one name for one concept. Set it to 0 and this probe measures the real
# window, which is what the control does.
_WIDEN_SECONDS = float(os.environ.get("AICC_TEST_WIDEN_FINALIZATION_SECONDS", "0") or 0)
_window = {}

_original_update_run_state = db.update_run_state


def _update_run_state_timed(*args, **kwargs):
    result = _original_update_run_state(*args, **kwargs)
    if kwargs.get("new_state") in set(db.TERMINAL_STATES):
        _window["opened"] = time.perf_counter()
    return result


# Timing first, then the widening on top, so `opened` is stamped at the moment
# the terminal row became visible rather than after the hold.
db.update_run_state = _update_run_state_timed
if _WIDEN_SECONDS > 0:
    finalization_window.widen(_WIDEN_SECONDS)

_original_mark_run_finalized = db.mark_run_finalized


def _mark_run_finalized_timed(db_path, run_id, **kwargs):
    result = _original_mark_run_finalized(db_path, run_id, **kwargs)
    _window["closed"] = time.perf_counter()
    return result


db.mark_run_finalized = _mark_run_finalized_timed


def _window_ms() -> float:
    """How wide the window actually was, measured inside the process.

    Reported so the control can check the widening is absent without timing the
    whole subprocess from outside — an outside measurement is dominated by
    interpreter start-up and by whatever else the runner is doing, which is
    exactly the noise that makes a timing assertion flaky.
    """
    return (_window["closed"] - _window["opened"]) * 1000


def main() -> None:
    supervisor = supervisor_module.Supervisor()
    run = supervisor.start_raw(
        project="AIOS",
        repository_path=str(REPO),
        task_type="implementation",
        prompt="finalization window probe",
        confirmed=True,
    )
    run_id = run["id"]
    print(run_id, flush=True)

    def watch_for_the_window() -> None:
        """Kill this process only while it is *inside* the window.

        The condition is "terminal and not yet finalized", which is the window
        stated exactly: the state is committed and visible to every observer,
        and the `process_exited` event, the auto-commit and the report have not
        been written. Killing on the terminal state alone would fire again after
        a clean finalization and make every run look caught, which is how a
        control stops being a control.
        """
        while True:
            row = db.get_run(supervisor.db_path, run_id)
            if row is not None and row["state"] in db.TERMINAL_STATES and not row["finalized_at"]:
                os.kill(os.getpid(), signal.SIGKILL)
            time.sleep(POLL_SECONDS)

    threading.Thread(target=watch_for_the_window, daemon=True).start()

    # Deliberately not `wait_for_run`: that reads the in-memory registry this
    # whole task exists because other processes cannot see. Poll the durable
    # marker instead, which is what any observer would have to do.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        row = db.get_run(supervisor.db_path, run_id)
        if row is not None and row["finalized_at"]:
            # Surviving a *widened* run means the widening never happened —
            # the same silent-no-op failure `finalization_window` documents,
            # and it would read as "the kill missed" rather than "the fixture
            # measured nothing".
            if _WIDEN_SECONDS > 0 and not finalization_window.fired():
                print(
                    "widening was requested but no terminal write went through "
                    "it, so this run proves nothing",
                    file=sys.stderr,
                    flush=True,
                )
                raise SystemExit(97)
            print(f"WINDOW_MS {_window_ms():.2f}", flush=True)
            print("SURVIVED", flush=True)
            return
        time.sleep(0.005)
    print("TIMEOUT", flush=True)


main()
