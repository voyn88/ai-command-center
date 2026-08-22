"""The Python surface over the structured backlog store (BO-S1).

Thin by the queue-store rule: migration ``0005_backlog_store`` owns the
semantics — the status machine, the cycle check, the lease protocol, the
audit — as SECURITY DEFINER functions, and duplicating any decision here
would create a second authority. What Python adds is the seam the CLI, the
importer and (BO-S2) the planner call, plus the one composite act SQL cannot
own: reconciling the Markdown projection into the store.

Import model, recorded: during the migration period the Markdown file is the
incumbent authority, so ``import_markdown`` reconciles via
``backlog_upsert_task`` — the ONE path allowed to set status directly,
because ingest of current truth is not a transition. Everything after import
moves through ``backlog_transition`` and its machine model. Dependencies are
NOT imported: the file records them as prose ("Связи: …"), and prose is
exactly what the no-substring rule forbids acting on; edges enter through
``add_dependency`` (cycle-checked) as BO-S2 formalizes them.

Graduating that exemption, recorded as a query rather than a judgement call:
the direct-status power is load-bearing only while an import still
contradicts the machine model about a task the machine model already owns —
one carrying queue history, i.e. a ``work_item`` under its ``task_id``.
``import_markdown`` counts exactly those into ``ImportReport.status_forced``,
so the retirement condition is read off a run instead of argued: when a full
import of the current file reports ``status_forced == []``, nothing depends
on the exemption any more and ``backlog_upsert_task`` can be narrowed to
reconciling wave/priority/kind/title/body/repo, leaving ``status`` to
``backlog_transition`` alone. Until then the list names which tasks are
holding it open — the remaining migration work, itemised rather than
estimated. A count that only ever said "some tasks have queue history" would
never reach zero and so would never be a criterion at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from command_center.db.backlog_parser import ParsedTask, parse_backlog

__all__ = ["BacklogStore", "ImportReport"]


@dataclass(slots=True)
class ImportReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: (line_no, reason, excerpt) — straight from the parser; never dropped.
    unparsed: list[tuple[int, str, str]] = field(default_factory=list)
    #: (task_id, stored_status, file_status) — the migration-period
    #: direct-status exemption, measured: an import overruling the machine
    #: model on a task that already has queue history. Empty over a full
    #: import of the canonical file IS the graduation criterion (see the
    #: module docstring); it is not an error while the migration is running.
    status_forced: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.inserted + self.updated


class BacklogStore:
    """Calls into the 0005 functions; refusals come back as data."""

    def __init__(self, connection_factory: Any = None) -> None:
        self._factory = connection_factory

    def _connection(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from command_center.db import pool

        return pool.connection()

    def _row(self, sql: str, params: tuple[Any, ...]) -> tuple:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    # -- writes (all through the SQL protocol) --------------------------------

    def upsert_task(self, task: ParsedTask) -> tuple[bool, str, bool]:
        ok, reason, changed, _revision = self._row(
            "SELECT * FROM backlog_upsert_task(%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                task.task_id,
                task.wave,
                task.priority,
                task.status,
                task.kind,
                task.title,
                task.body,
                task.repo,
            ),
        )
        return bool(ok), str(reason), bool(changed)

    def transition(
        self, task_id: str, to_status: str, expected_revision: int
    ) -> tuple[bool, str, int | None]:
        ok, reason, revision = self._row(
            "SELECT * FROM backlog_transition(%s, %s, %s)",
            (task_id, to_status, expected_revision),
        )
        return bool(ok), str(reason or ""), revision

    def record_evidence(self, task_id: str, kind: str, value: str) -> tuple[bool, str]:
        ok, reason, _revision = self._row(
            "SELECT * FROM backlog_record_evidence(%s, %s, %s)",
            (task_id, kind, value),
        )
        return bool(ok), str(reason or "")

    def record_remediation(
        self, task_id: str, parent_task_id: str, pr_url: str, rejected_head_sha: str
    ) -> tuple[bool, str]:
        """Link a freshly-created remediation task to the rejected task it
        follows up on. Audit lineage, not a readiness gate -- unlike
        `add_dependency`, this never affects when the planner may dispatch
        `task_id` (0010's rationale: the parent is REJECTED, a terminal leaf
        that can never become DONE, so a readiness dependency on it would
        block the remediation task forever)."""
        ok, reason, _revision = self._row(
            "SELECT * FROM backlog_record_remediation(%s, %s, %s, %s)",
            (task_id, parent_task_id, pr_url, rejected_head_sha),
        )
        return bool(ok), str(reason or "")

    def add_dependency(
        self, task_id: str, depends_on: str
    ) -> tuple[bool, str, list[str] | None]:
        ok, reason, path = self._row(
            "SELECT * FROM backlog_add_dependency(%s, %s)",
            (task_id, depends_on),
        )
        return bool(ok), str(reason or ""), list(path) if path is not None else None

    def lease_acquire(
        self, authority: str, owner: str, ttl_seconds: int
    ) -> tuple[bool, str]:
        ok, reason, _owner, _until = self._row(
            "SELECT * FROM backlog_lease_acquire(%s, %s, %s)",
            (authority, owner, ttl_seconds),
        )
        return bool(ok), str(reason or "")

    def lease_heartbeat(
        self, authority: str, owner: str, ttl_seconds: int
    ) -> tuple[bool, str]:
        ok, reason, _owner, _until = self._row(
            "SELECT * FROM backlog_lease_heartbeat(%s, %s, %s)",
            (authority, owner, ttl_seconds),
        )
        return bool(ok), str(reason or "")

    def lease_release(self, authority: str, owner: str) -> tuple[bool, str]:
        ok, reason, _owner, _until = self._row(
            "SELECT * FROM backlog_lease_release(%s, %s)",
            (authority, owner),
        )
        return bool(ok), str(reason or "")

    # -- reads ----------------------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._row(
            "SELECT task_id, wave, priority, status, kind, title, repo, revision "
            "FROM backlog_task WHERE task_id = %s",
            (task_id,),
        )
        if row is None:
            return None
        keys = (
            "task_id",
            "wave",
            "priority",
            "status",
            "kind",
            "title",
            "repo",
            "revision",
        )
        return dict(zip(keys, row, strict=True))

    def counts_by_status(self) -> dict[str, int]:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, count(*) FROM backlog_task GROUP BY status")
                return {status: int(count) for status, count in cur.fetchall()}

    def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """A page of tasks, newest-updated first, plus the total count for
        that filter (paging metadata, not a second query the caller has to
        issue to know whether there is a next page)."""
        keys = (
            "task_id",
            "wave",
            "priority",
            "status",
            "kind",
            "title",
            "repo",
            "revision",
        )
        with self._connection() as conn:
            with conn.cursor() as cur:
                if status is not None:
                    cur.execute(
                        "SELECT count(*) FROM backlog_task WHERE status = %s",
                        (status,),
                    )
                    total = int(cur.fetchone()[0])
                    cur.execute(
                        "SELECT task_id, wave, priority, status, kind, title, repo, "
                        "revision FROM backlog_task WHERE status = %s "
                        "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                        (status, limit, offset),
                    )
                else:
                    cur.execute("SELECT count(*) FROM backlog_task")
                    total = int(cur.fetchone()[0])
                    cur.execute(
                        "SELECT task_id, wave, priority, status, kind, title, repo, "
                        "revision FROM backlog_task "
                        "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                        (limit, offset),
                    )
                tasks = [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]
        return tasks, total

    def list_events(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        keys = ("event", "outcome", "reason", "actor", "detail", "created_at")
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event, outcome, reason, actor, detail, created_at "
                    "FROM backlog_event WHERE task_id = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (task_id, limit),
                )
                return [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]

    def list_evidence(self, task_id: str) -> list[dict[str, Any]]:
        keys = ("kind", "value", "recorded_at")
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT kind, value, recorded_at FROM backlog_evidence "
                    "WHERE task_id = %s ORDER BY recorded_at",
                    (task_id,),
                )
                return [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]

    def machine_governed_statuses(self) -> dict[str, str]:
        """Stored status of every task the machine model has already taken
        responsibility for.

        Queue history is the marker: a ``work_item`` under the ``task_id``
        means something after import moved this task, so an import that
        disagrees is overwriting a decision rather than ingesting current
        truth. One query, not one per task — the importer walks the whole
        file and a per-row probe would turn reconciliation into N round
        trips for a number it only reports.
        """
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT t.task_id, t.status FROM backlog_task t "
                    "WHERE EXISTS (SELECT 1 FROM work_item w "
                    "WHERE w.task_id = t.task_id)"
                )
                return {str(task_id): str(status) for task_id, status in cur.fetchall()}

    # -- the importer ---------------------------------------------------------

    def import_markdown(self, text: str) -> ImportReport:
        """Reconcile the Markdown projection. Idempotent by construction:
        ``backlog_upsert_task`` reports ``changed=false`` for an identical
        record, so a second run over the same text yields ``changed == 0`` —
        measurable, not hoped for.

        The run also measures the direct-status exemption it is spending:
        every task whose stored status this text contradicts *and* which
        already has queue history lands in ``report.status_forced``, the
        graduation criterion the module docstring names. Statuses are read
        once, BEFORE the first upsert — read after, they would already be
        the file's own values and the measurement would report zero forever,
        which is the one wrong answer that looks like success.
        """
        parsed = parse_backlog(text)
        report = ImportReport(unparsed=list(parsed.unparsed))
        governed = self.machine_governed_statuses()
        for task in parsed.tasks:
            ok, reason, changed = self.upsert_task(task)
            if not ok:
                # A refused record set nothing, so it spent no exemption.
                report.refused.append((task.task_id, reason))
                continue
            stored = governed.get(task.task_id)
            if stored is not None and stored != task.status:
                report.status_forced.append((task.task_id, stored, task.status))
            if not changed:
                report.unchanged += 1
            elif reason == "inserted":
                report.inserted += 1
            else:
                report.updated += 1
        return report
