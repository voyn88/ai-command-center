#!/usr/bin/env python3
"""A stand-in for the `claude` binary, used by supervisor/cancellation/
reconciliation tests so they exercise a *real* `subprocess.Popen` (real pid,
real process group, real signal delivery) without ever invoking the actual
`claude` CLI or spending API credits.

Controlled entirely by environment variables so tests can shape its behavior
without needing a new script per scenario:

- `FAKE_CLAUDE_LINES`: JSON list of stdout lines to print, one per line, each
  already a JSON string (so a test can also ask for a deliberately malformed
  line by including plain non-JSON text here). Defaults to a small realistic
  stream-json sequence.
- `FAKE_CLAUDE_LINES_FILE`: path to a UTF-8 JSON file containing that same
  list. Takes precedence over `FAKE_CLAUDE_LINES` and lets tests provide large
  payloads without exceeding Linux's per-environment-string exec limit.
- `FAKE_CLAUDE_INITIAL_DELAY`: seconds to sleep *before* emitting the first
  line (default 0) — simulates a spawned, alive process that produces no early
  stdout (a late handshake). Distinct from `FAKE_CLAUDE_DELAY` (between lines)
  and `FAKE_CLAUDE_EXTRA_SLEEP` (after the last line).
- `FAKE_CLAUDE_DELAY`: seconds to sleep between lines (default 0.05).
- `FAKE_CLAUDE_EXIT_CODE`: process exit code (default 0).
- `FAKE_CLAUDE_STDERR`: a line to print to stderr before exiting, if set.
- `FAKE_CLAUDE_IGNORE_SIGTERM`: if "1", installs a SIGTERM handler that does
  nothing, so a test can exercise the grace-period -> SIGKILL escalation.
- `FAKE_CLAUDE_READY_FILE`: if set, a file written immediately *after* the
  signal disposition above is installed. A test that cancels before this file
  exists is racing the handler: the process still carries the default SIGTERM
  disposition, dies on the first signal, and no escalation happens — which then
  looks like an escalation bug and is not one. Wait for this file, not a fixed
  sleep.
- `FAKE_CLAUDE_EXTRA_SLEEP`: extra seconds to sleep after emitting all lines,
  before exiting (default 0) — gives a test time to cancel mid-flight.
- `FAKE_CLAUDE_HOLD_FILE`: if set, a file whose continued existence keeps the
  fake process alive after it emits its output. This lets UI tests synchronize
  cancellation deterministically instead of racing a short fixed sleep. The
  wait has a 300-second safety ceiling so a broken test cannot leave the fake
  process alive indefinitely.
- `FAKE_CLAUDE_TOUCH_FILE`: if set, a file path (relative to cwd) to write to
  right after emitting all lines — simulates the working tree changing during
  a run, for cancellation "working tree changed" tests.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

DEFAULT_LINES = [
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "stream_event", "event": {"delta": "hello "}}),
    json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello world"}]}}),
    json.dumps({"type": "result", "result": "Verdict: APPROVED_FOR_COMMIT"}),
]


def main() -> int:
    if os.environ.get("FAKE_CLAUDE_IGNORE_SIGTERM") == "1":
        signal.signal(signal.SIGTERM, lambda signum, frame: None)

    ready_file = os.environ.get("FAKE_CLAUDE_READY_FILE")
    if ready_file:
        # Written after the disposition above is in place, never before.
        with open(ready_file, "w", encoding="utf-8") as handle:
            handle.write("ready")

    lines_file = os.environ.get("FAKE_CLAUDE_LINES_FILE")
    lines_env = os.environ.get("FAKE_CLAUDE_LINES")
    if lines_file:
        with open(lines_file, encoding="utf-8") as handle:
            lines = json.load(handle)
    else:
        lines = json.loads(lines_env) if lines_env else DEFAULT_LINES
    delay = float(os.environ.get("FAKE_CLAUDE_DELAY", "0.05"))

    initial_delay = float(os.environ.get("FAKE_CLAUDE_INITIAL_DELAY", "0"))
    if initial_delay:
        time.sleep(initial_delay)

    for line in lines:
        print(line)
        sys.stdout.flush()
        time.sleep(delay)

    stderr_line = os.environ.get("FAKE_CLAUDE_STDERR")
    if stderr_line:
        print(stderr_line, file=sys.stderr)
        sys.stderr.flush()

    touch_file = os.environ.get("FAKE_CLAUDE_TOUCH_FILE")
    if touch_file:
        with open(touch_file, "a") as handle:
            handle.write("modified by fake_claude\n")

    # Simulate an agent that *commits* its work: stage+commit so HEAD advances
    # while leaving the working tree clean — exactly the real-world case the
    # supervisor's `head_advanced` detection must catch (see supervisor.py's
    # working_tree_changed computation). A non-empty commit message is
    # required; git refuses empty commits without --allow-empty.
    commit_msg = os.environ.get("FAKE_CLAUDE_COMMIT")
    if commit_msg:
        import subprocess

        # Identity supplied on the command line, not read from the
        # environment. A CI runner has no global `user.email`, and this fake
        # runs in a linked worktree whose config it does not own — so a commit
        # that relies on ambient identity fails there and passes here, which is
        # precisely the shape of flake this fixture is supposed to avoid
        # creating. `-c` is scoped to the single invocation and needs no
        # cleanup.
        identity = [
            "-c",
            "user.name=fake-claude",
            "-c",
            "user.email=fake-claude@example.invalid",
        ]
        subprocess.run(["git", *identity, "add", "-A"], check=True)
        # --allow-empty so HEAD advances even when the run made no file changes
        # (e.g. an agent that amended an earlier interrupted run by committing
        # staged state, or simply produced an empty commit to record progress).
        subprocess.run(
            ["git", *identity, "commit", "-q", "--allow-empty", "-m", commit_msg], check=True
        )

    hold_file = os.environ.get("FAKE_CLAUDE_HOLD_FILE")
    if hold_file:
        deadline = time.monotonic() + 300
        while os.path.exists(hold_file) and time.monotonic() < deadline:
            time.sleep(0.01)

    extra_sleep = float(os.environ.get("FAKE_CLAUDE_EXTRA_SLEEP", "0"))
    if extra_sleep:
        time.sleep(extra_sleep)

    return int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
