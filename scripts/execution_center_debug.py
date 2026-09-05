#!/usr/bin/env python3
"""Minimal internal/debug CLI for the v2 Execution Center backend.

Exists only to validate `command_center.runtime` end to end without
redesigning the Streamlit UI (out of scope for Sprint 1 — see the brief).

**This is not a persistent multi-command daemon.** Every invocation of this
script is a separate OS process with its own, empty `Supervisor._active`
registry — it never shares in-memory state (a `Popen` handle, a stdout pipe,
a waitable-child relationship) with any other invocation, past or future.
That is exactly why `launch` below is a *foreground, blocking* command
rather than "start and return a run id": a Claude Code process launched by
one invocation of this CLI cannot be safely or truthfully controlled
(streamed, cancelled) by a *different* invocation, because the second
process has no handle to it at all — only the OS pid, which is not enough
(see `identity.py`/`Supervisor.reconcile`). An earlier version of this
script had a separate `cancel RUN_ID` subcommand; it has been removed
because it could only ever raise (there is nothing for a fresh process to
cancel) or, worse, silently do nothing truthful — see the Sprint 1
remediation notes (finding F1) for the full incident this fixes: a `launch`
that returned immediately left the underlying `claude` process orphaned,
with stream capture permanently stopped and no way to cancel it from a
later invocation.

The read-only inspection subcommands (`list-sessions`, `list-runs`,
`run-status`, `events`, `reconcile`) have no such problem — they only read
(or, for `reconcile`, conservatively reclassify) rows in the shared SQLite
db, which genuinely is persistent cross-process state — and remain safe to
call from any number of separate invocations at any time.

Usage:
    python scripts/execution_center_debug.py list-sessions [--task-id ID]
    python scripts/execution_center_debug.py list-runs [--session-id ID] [--task-id ID] [--state STATE]
    python scripts/execution_center_debug.py run-status RUN_ID
    python scripts/execution_center_debug.py events RUN_ID [--after-seq N]
    python scripts/execution_center_debug.py reconcile
    python scripts/execution_center_debug.py offline-finalization-cutover --confirm-offline
    python scripts/execution_center_debug.py launch PROJECT REPO_PATH TASK_TYPE INSTRUCTION --confirm
        Blocks in the foreground, printing each new event as it is
        persisted, until the run reaches a terminal state. Ctrl+C requests
        confirmed cancellation of the run and waits for cleanup before
        exiting — it does not just kill this CLI process. Exit code: 0 for
        COMPLETED, non-zero (1-4) for FAILED/CANCELLED/INTERRUPTED/UNKNOWN.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from command_center.runtime import db, identity, supervisor  # noqa: E402
from command_center.runtime.api import ExecutionCenterAPI  # noqa: E402
from command_center.runtime.supervisor import SupervisorError  # noqa: E402

_POLL_INTERVAL_SECONDS = 0.2
_DEFAULT_CANCEL_GRACE_SECONDS = 10.0
# Diagnostic ceiling for the post-terminal finalization wait (see
# `_await_finalization`). Generous relative to the work it covers — an event
# append, `git commit`, and one report write — so that hitting it means
# "finalization is wedged", never "finalization was merely slow".
def _finalization_timeout() -> float:
    """60 s, unless a test needs to prove the wedged path in less than a minute.

    `AICC_TEST_` in the name, because review pointed out the previous spelling
    said "not a knob for operators" in a comment and did nothing to be one: it
    read like a supported setting, was parsed at import so a garbage value
    crashed even read-only subcommands with a traceback, and `0` made every run
    exit 5. A comment is not an enforcement.
    """
    raw = os.environ.get("AICC_TEST_FINALIZATION_TIMEOUT_SECONDS")
    if raw is None:
        return 60.0
    try:
        seconds = float(raw)
    except ValueError:
        raise SystemExit(
            f"AICC_TEST_FINALIZATION_TIMEOUT_SECONDS must be a number, got {raw!r}"
        ) from None
    if seconds <= 0:
        # Zero made `waited >= timeout` true on every run, so every finalization
        # reported itself wedged.
        raise SystemExit("AICC_TEST_FINALIZATION_TIMEOUT_SECONDS must be positive")
    return seconds


_FINALIZATION_TIMEOUT_SECONDS = _finalization_timeout()

#: A run that reached a terminal state whose finalization never completed. Not
#: any of the state codes below, because it is not a statement about the run's
#: outcome: the outcome may be `COMPLETED` while the work it describes was
#: never committed and never reported.
_EXIT_CODE_UNFINALIZED = 5

#: Ctrl+C asked for cancellation and cancellation did not happen, on a run that
#: is still live. Distinct from every state code because the run's state is not
#: the news: the news is that this CLI is exiting without having stopped a
#: process it started.
_EXIT_CODE_CANCEL_FAILED = 6

# The operator requested an offline cutover but the database still contains a
# shape that cannot safely cross the v24 -> v25 fencing boundary.
_EXIT_CODE_CUTOVER_REFUSED = 7

#: How long a row may be behind its process before this CLI stops waiting and
#: decides from the process instead. Short: the sites that raise mid-write
#: settle almost immediately.
_CANCEL_SETTLE_SECONDS = 2.0

_EXIT_CODE_BY_STATE = {
    "COMPLETED": 0,
    "FAILED": 1,
    "CANCELLED": 2,
    "INTERRUPTED": 3,
    "UNKNOWN": 4,
}


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _print_event(event: dict) -> None:
    print(f"[{event['seq']:>5}] {event['event_type']}: {json.dumps(event['payload'], ensure_ascii=False)[:300]}")


def _await_finalization(api: ExecutionCenterAPI, run_id: str) -> tuple[dict, bool]:
    """A terminal `state` on the run row is *necessary but not sufficient* for
    this CLI to exit.

    `Supervisor._supervise` commits the terminal row first and only afterwards
    appends the `process_exited` lifecycle event, auto-commits the agent's work
    and persists the run report — all on a **daemon** thread. Interpreter
    shutdown does not join daemon threads, so returning the moment the row
    turns terminal truncates finalization whenever the poll tick lands inside
    that window (measured at ~2.5 ms median, 41 ms max, against a 200 ms poll
    interval: roughly one run in a hundred). The visible symptom is a
    COMPLETED run with no `process_exited` event, no auto-commit and no report.

    So wait on the supervisor's own finalization signal instead — the same
    `done_event` that `Supervisor.cancel()` already waits on before returning,
    which is why the Ctrl+C path never had this defect."""
    started = time.monotonic()
    final = api.supervisor.wait_for_run(run_id, timeout=_FINALIZATION_TIMEOUT_SECONDS)
    waited = time.monotonic() - started
    if waited >= _FINALIZATION_TIMEOUT_SECONDS:
        # Diagnostic, not a retry: say loudly which guarantee was lost rather
        # than exiting as if the run had finalized cleanly.
        print(
            f"WARNING: run {run_id} reached a terminal state but its supervisor did not "
            f"finish finalizing within {_FINALIZATION_TIMEOUT_SECONDS:.0f}s — the "
            "process_exited event, the auto-commit of the agent's work and the run "
            "report may all be missing.",
            file=sys.stderr,
        )
        # And the exit code says so too. Printing a warning while exiting 0 is
        # the same defect one layer up: a caller that reads the status — a
        # script, a queue, CI — was told the run finished cleanly, which is
        # precisely the guarantee that had just been lost.
        return final or api.get_run(run_id), False
    return final or api.get_run(run_id), True


def _settled_run(api: ExecutionCenterAPI, run_id: str) -> dict | None:
    """Read the run, allowing a bounded moment for a mid-flight row to catch up.

    Several of `Supervisor.cancel`'s raise sites fire precisely while the
    terminal state is being written, so one instantaneous read is guaranteed to
    catch the row before it settles. Bounded and short: this is not a wait for
    finalization, only for the state itself, and the alternative is classifying
    on a value the raise site tells us is in flight.
    """
    deadline = time.monotonic() + _CANCEL_SETTLE_SECONDS
    current = api.get_run(run_id)
    while current and current["state"] not in db.TERMINAL_STATES:
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
        current = api.get_run(run_id)
    return current


def _run_foreground(
    api: ExecutionCenterAPI,
    run: dict,
    *,
    deferred_interrupt: dict[str, bool] | None = None,
    previous_sigint_handler=None,
) -> int:
    """Block until `run` reaches a terminal state *and its supervisor has
    finished finalizing it*, printing new events as they're persisted. Ctrl+C
    triggers confirmed cancellation and waits for it to finish before this
    function (and the process) exits — it never just kills this CLI process and
    abandons the child."""
    run_id = run["id"]
    after_seq = 0

    try:
        # `start_run()` can make the child visible before it returns to this
        # CLI.  Defer SIGINT across that narrow hand-off, then restore normal
        # KeyboardInterrupt delivery *inside* this try block so a Ctrl+C can
        # never strand the just-created run between launch and supervision.
        if previous_sigint_handler is not None:
            signal.signal(signal.SIGINT, previous_sigint_handler)
        if deferred_interrupt and deferred_interrupt["pending"]:
            raise KeyboardInterrupt
        while True:
            current = api.get_run(run_id)
            for event in api.get_events(run_id, after_seq=after_seq):
                _print_event(event)
                after_seq = event["seq"]
            if current["state"] in db.TERMINAL_STATES:
                current, finalized = _await_finalization(api, run_id)
                # Drain again: `process_exited` (and `auto_committed`) are
                # appended *after* the terminal row, so the loop above can
                # never have seen them.
                for event in api.get_events(run_id, after_seq=after_seq):
                    _print_event(event)
                    after_seq = event["seq"]
                _print(current)
                if not finalized:
                    return _EXIT_CODE_UNFINALIZED
                return _EXIT_CODE_BY_STATE.get(current["state"], 1)
            time.sleep(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        # A run that is already terminal has nothing to cancel, and asking
        # anyway raises `SupervisorError` — so Ctrl+C *during the finalization
        # wait* used to kill this CLI with a traceback instead of reporting the
        # run it had just waited for. The wait is the window this covers: it is
        # the one place where the run is finished and the CLI is still running.
        current = api.get_run(run_id)
        if current and current["state"] in db.TERMINAL_STATES:
            print(
                f"\nCtrl+C received — run {run_id} is already {current['state']}; "
                "there is nothing to cancel. Its finalization may still be in progress.",
                file=sys.stderr,
            )
            for event in api.get_events(run_id, after_seq=after_seq):
                _print_event(event)
            _print(current)
            return _EXIT_CODE_BY_STATE.get(current["state"], 1)
        print("\nCtrl+C received — requesting confirmed cancellation and waiting for cleanup...", file=sys.stderr)
        try:
            final = api.request_cancel(run_id, confirmed=True, grace_seconds=_DEFAULT_CANCEL_GRACE_SECONDS)
        except SupervisorError as exc:
            # `Supervisor.cancel` raises this from **nine** sites, and they do
            # not mean the same thing. An earlier version of this handler
            # counted four — everything before the signal is sent — and treated
            # them all as "nothing to cancel", which was false for the one that
            # fires on a live run after CAS exhaustion.
            #
            # Counting four was itself the defect. Five more sites live in the
            # post-signal tail, and two of them ("has already exited and is
            # finalizing", "exited after cancellation, but its terminal state
            # was not persisted in time") fire when the child is **already
            # dead** and the row has simply not caught up. A single
            # instantaneous re-read returns a live state there, so the handler
            # sent the operator hunting a process that no longer exists, under
            # the exit code reserved for "I exited without stopping what I
            # started".
            #
            # So the row is given a bounded moment to settle, and the process
            # itself is the tiebreaker when it does not. A row mid-flight is
            # not evidence; a pid is.
            final = _settled_run(api, run_id)
            if final and final["state"] in db.TERMINAL_STATES:
                print(f"\nNothing to cancel — {exc}", file=sys.stderr)
            elif final and final.get("pid") and not identity.process_exists(final["pid"]):
                # The row is still live but the process is gone: cancellation
                # took effect and only the bookkeeping is behind. Saying
                # "may still be running" here would be the same false statement
                # in the other direction.
                print(
                    f"\nCancelled — {exc}\n"
                    f"Run {run_id} still reads {final['state']}, but its process "
                    f"({final['pid']}) is gone, so the cancellation took effect "
                    "and the terminal state has not been persisted yet.",
                    file=sys.stderr,
                )
            else:
                state = final["state"] if final else "UNKNOWN"
                print(
                    f"\nCANCELLATION FAILED — {exc}\n"
                    f"Run {run_id} is {state}; the agent process may still be "
                    "running and this CLI is exiting without having stopped it. "
                    "Check it and cancel it explicitly.",
                    file=sys.stderr,
                )
                for event in api.get_events(run_id, after_seq=after_seq):
                    _print_event(event)
                _print(final)
                return _EXIT_CODE_CANCEL_FAILED
        for event in api.get_events(run_id, after_seq=after_seq):
            _print_event(event)
        _print(final)
        return _EXIT_CODE_BY_STATE.get(final["state"], 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-sessions")
    p.add_argument("--task-id")

    p = sub.add_parser("list-runs")
    p.add_argument("--session-id")
    p.add_argument("--task-id")
    p.add_argument("--state")

    p = sub.add_parser("run-status")
    p.add_argument("run_id")

    p = sub.add_parser("events")
    p.add_argument("run_id")
    p.add_argument("--after-seq", type=int, default=0)

    p = sub.add_parser("launch", help="Foreground/blocking — see module docstring.")
    p.add_argument("project")
    p.add_argument("repository_path")
    p.add_argument("task_type")
    p.add_argument("instruction")
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--model")
    p.add_argument("--timeout-seconds", type=int, default=None)
    p.add_argument(
        "--confirmed-item", action="append", default=[], dest="confirmed_items",
        help="Repeatable. A candidate_content key to confirm for a sensitive project (BANK/LEGAL).",
    )

    sub.add_parser("reconcile")

    p = sub.add_parser(
        "offline-finalization-cutover",
        help=(
            "After intake and every pre-v25 Supervisor are stopped, atomically "
            "fence legacy terminal crash rows and recover their finalization."
        ),
    )
    p.add_argument(
        "--confirm-offline",
        action="store_true",
        help="Confirm that intake and every pre-v25 Supervisor are stopped.",
    )

    args = parser.parse_args()

    # This must run before constructing ExecutionCenterAPI: Supervisor.__init__
    # invokes the ordinary fail-closed migration, which deliberately refuses a
    # v24 terminal-but-unfinalized crash row. The cutover first installs the
    # fence and gives this exact process ownership; the API then recovers the
    # durable artifacts under that same process-scoped token.
    if args.command == "offline-finalization-cutover":
        db_path = db.resolve_db_path()
        if not args.confirm_offline:
            print(
                "CUTOVER REFUSED: stop and mask intake plus every pre-v25 "
                "Supervisor, verify no legacy PID remains, then pass "
                "--confirm-offline",
                file=sys.stderr,
            )
            return _EXIT_CODE_CUTOVER_REFUSED
        try:
            maintenance_token, fence_created, maintenance_lock_handle = (
                supervisor.acquire_offline_cutover_fence(db_path)
            )
            owner_pid, owner_identity, owner_token = (
                supervisor._ensure_process_finalization_context()
            )
            seeded = db.bootstrap_finalization_claim_cutover(
                db_path,
                owner_token=owner_token,
                owner_pid=owner_pid,
                owner_identity=owner_identity.as_string(),
                offline_confirmed=True,
            )
            api = ExecutionCenterAPI(
                db_path=db_path,
                maintenance_token=maintenance_token,
                maintenance_lock_handle=maintenance_lock_handle,
                enable_completion_autopilot=False,
            )
            outcomes = api.reconcile()
            remaining = db.count_unfinalized_runs(db_path)
        except (db.FinalizationClaimCutoverRequired, SupervisorError) as exc:
            print(f"CUTOVER REFUSED: {exc}", file=sys.stderr)
            return _EXIT_CODE_CUTOVER_REFUSED
        result = {
            "schema_version": db.current_schema_version(db_path),
            "claims_seeded": seeded,
            "reconciliation": outcomes,
            "unfinalized_remaining": remaining,
            "restart_fence": str(supervisor.offline_cutover_fence_path(db_path)),
            "restart_fence_created": fence_created,
        }
        _print(result)
        if remaining:
            print(
                "CUTOVER INCOMPLETE: restart fence remains active; rerun recovery "
                "before starting any Supervisor",
                file=sys.stderr,
            )
            return _EXIT_CODE_UNFINALIZED
        supervisor.release_offline_cutover_fence(
            db_path, maintenance_token, maintenance_lock_handle
        )
        return 0

    api = ExecutionCenterAPI()

    if args.command == "list-sessions":
        _print(api.list_sessions(task_id=args.task_id))
    elif args.command == "list-runs":
        _print(api.list_runs(session_id=args.session_id, task_id=args.task_id, state=args.state))
    elif args.command == "run-status":
        _print(api.get_run(args.run_id))
    elif args.command == "events":
        _print(api.get_events(args.run_id, after_seq=args.after_seq))
    elif args.command == "launch":
        deferred_interrupt = {"pending": False}
        previous_sigint_handler = signal.getsignal(signal.SIGINT)

        def _defer_sigint(_signum, _frame) -> None:
            deferred_interrupt["pending"] = True

        signal.signal(signal.SIGINT, _defer_sigint)
        try:
            run = api.start_run(
                project=args.project,
                repository_path=args.repository_path,
                task_type=args.task_type,
                instruction=args.instruction,
                confirmed=args.confirm,
                confirmed_items=args.confirmed_items or None,
                model=args.model,
                timeout_seconds=args.timeout_seconds,
            )
        except BaseException:
            signal.signal(signal.SIGINT, previous_sigint_handler)
            raise
        if run["state"] in db.TERMINAL_STATES:
            signal.signal(signal.SIGINT, previous_sigint_handler)
            # Popen itself failed (e.g. `claude` binary missing) — nothing
            # to wait on, no process was ever left running.
            _print(run)
            return _EXIT_CODE_BY_STATE.get(run["state"], 1)
        return _run_foreground(
            api,
            run,
            deferred_interrupt=deferred_interrupt,
            previous_sigint_handler=previous_sigint_handler,
        )
    elif args.command == "reconcile":
        _print(api.reconcile())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
