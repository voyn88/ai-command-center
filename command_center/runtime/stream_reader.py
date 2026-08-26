"""Stream-reading side of the v2 Session Supervisor (NIGHT-W9 follow-up).

Extracted verbatim from ``supervisor.py`` behind a small interface: the
Supervisor owns orchestration (process lifecycle, the ``_active`` registry,
locks, cancellation, reconcile) and delegates the per-line consumption of a
child's stdout/stderr — provider-runtime parsing, the once-only handshake
milestone, diagnostics capture, and append-only event persistence — to this
class. Pure move: bodies are unchanged; only the receiving object differs.
The Supervisor keeps thin ``_drain_stdout``-style delegating methods so
existing callers and tests are unaffected.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from command_center.models import iso_now
from command_center.runtime import db, providers, stream_parser

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from command_center.runtime.supervisor import _ActiveRun

logger = logging.getLogger(__name__)


class StreamReader:
    """Reads a supervised child's output streams into the run_event log."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def write_stdin(self, run_id: str, active: _ActiveRun, prompt: str) -> None:
        try:
            active.process.stdin.write(prompt)
            active.process.stdin.close()
            db.append_run_event(
                self.db_path,
                run_id,
                "lifecycle",
                stream_parser.lifecycle_event("prompt_delivered", transport="stdin")["payload"],
            )
        except (BrokenPipeError, OSError, ValueError) as exc:
            db.append_run_event(
                self.db_path,
                run_id,
                "lifecycle",
                stream_parser.lifecycle_event("prompt_delivery_failed", error=type(exc).__name__)["payload"],
            )
    def record_handshake(self, run_id: str, active: _ActiveRun) -> None:
        """Record the provider startup/handshake milestone exactly once.

        This is deliberately separate from `process_started`: a valid PID
        proves the process was created; provider-approved readiness evidence
        proves it reached its protocol startup milestone. For Claude the
        historical any-output rule remains; for Codex only recognized
        lifecycle JSON qualifies. The gap
        between the two is exactly the window in which a run is "started but
        early output not yet received" — surfaced to the UI as
        `session_view.STATUS_STARTING`, never as a failure.

        Best-effort and non-fatal by construction:

        - Guarded by an in-memory `threading.Event` so it runs once even
          though both reader threads (stdout and stderr) call it, and without
          re-reading the run row for every subsequent line.
        - Any database error (a lost compare-and-set race against a
          concurrent `cancel()`/watchdog write, the run already gone, ...) is
          swallowed. Handshake timing is observability, not correctness — it
          must never crash a reader thread or fail a run, and the run's
          terminal state is decided entirely from process-exit facts
          regardless of whether this ever succeeded.

        The append-only `handshake_received` lifecycle event is written first
        (it touches only `run_event`, never `run.version`, so it never races
        anything), then the `first_output_at` column is set best-effort so the
        live projection layer (`session_view.derive_status`), which reads only
        the run row, can tell STARTING from RUNNING.
        """
        with active.handshake_lock:
            if active.handshake_recorded.is_set():
                return
            # Claim the milestone first, atomically: even if the DB writes
            # below fail or race, we must never spin re-attempting on every
            # line, nor let the other reader thread also claim it.
            active.handshake_recorded.set()
        now = iso_now()
        try:
            db.append_run_event(
                self.db_path, run_id, "lifecycle", stream_parser.lifecycle_event("handshake_received", at=now)["payload"]
            )
        except Exception:
            logger.debug("handshake event persist failed for run %s", run_id, exc_info=True)
        try:
            run = db.get_run(self.db_path, run_id)
            if run is None or run.get("first_output_at"):
                return
            db.update_run_fields(
                self.db_path, run_id, expected_version=run["version"], fields={"first_output_at": now}
            )
        except Exception:
            # LostUpdateError (a concurrent cancel/terminal write landed
            # first), KeyError (run gone), or any other db hiccup — the
            # milestone is best-effort; the append-only event above already
            # captured the timing for the audit log.
            logger.debug("first_output_at milestone failed for run %s", run_id, exc_info=True)

    def drain_stdout(self, run_id: str, active: _ActiveRun) -> None:
        process = active.process
        try:
            for chunk in process.stdout:
                for line in active.provider_runtime.feed_stdout(chunk):
                    self.persist_stdout_event(run_id, active, line)
        finally:
            for line in active.provider_runtime.flush_stdout():
                self.persist_stdout_event(run_id, active, line)
            try:
                process.stdout.close()
            except Exception:
                logger.debug("stdout close failed for run %s", run_id, exc_info=True)

    def persist_stdout_event(self, run_id: str, active: _ActiveRun, line: str) -> None:
        event = active.provider_runtime.parse_stdout_line(line)
        if active.provider_runtime.stdout_event_is_readiness(line, event):
            self.record_handshake(run_id, active)
        if event is None:
            return
        if active.provider_runtime.event_is_valid_result(event):
            active.valid_result_recorded.set()
        if active.provider_runtime.event_is_provider_error(event):
            active.add_diagnostic(json.dumps(event["payload"], ensure_ascii=False, sort_keys=True))
        self.append_stream_event(run_id, event["event_type"], event["payload"])

    def drain_stderr(self, run_id: str, active: _ActiveRun) -> None:
        process = active.process
        try:
            for chunk in process.stderr:
                for line in active.provider_runtime.feed_stderr(chunk):
                    self.persist_stderr_event(run_id, active, line)
        finally:
            for line in active.provider_runtime.flush_stderr():
                self.persist_stderr_event(run_id, active, line)
            try:
                process.stderr.close()
            except Exception:
                logger.debug("stderr close failed for run %s", run_id, exc_info=True)

    def persist_stderr_event(self, run_id: str, active: _ActiveRun, line: str) -> None:
        if active.provider_runtime.stderr_line_is_readiness(line):
            self.record_handshake(run_id, active)
        event = stream_parser.stderr_event(line[:providers.MAX_PERSISTED_EVENT_CHARS])
        active.add_diagnostic(event["payload"]["line"])
        self.append_stream_event(run_id, event["event_type"], event["payload"])

    def append_stream_event(self, run_id: str, event_type: str, payload: dict) -> None:
        """Persist one stream event, tolerating a concurrently deleted run.

        Stdout/stderr reader threads outlive the foreground launcher. If the
        run (or its task/session parent) is deleted while output is still
        draining, the append hits a FOREIGN KEY violation. That is a benign
        race — there is nothing left to persist for a run that no longer
        exists — so the reader thread stops recording quietly instead of
        dying with an unhandled exception. This mirrors how the codebase
        already treats a vanished run elsewhere (see `_record_handshake`).
        """
        try:
            db.append_run_event(self.db_path, run_id, event_type, payload)
        except sqlite3.IntegrityError:
            pass
