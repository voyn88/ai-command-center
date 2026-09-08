"""Durable side of the quarterly self-assessment (VOYN-MIN-AGT-EVO2).

``self_assessment.py`` is deliberately pure: given outcomes, a routing matrix
and "now", it computes what the best configuration per domain currently
looks like. Pure functions cannot, by themselves, satisfy the acceptance
criterion literally — "each quarter the best configuration per domain is
updated" describes something that happens to state that persists BETWEEN
runs, and a function with no memory has no "last time" to compare "now"
against. Something has to remember when the last assessment ran and what it
produced, across process restarts, or ``is_reassessment_due`` has nothing to
be due relative to.

This module is that memory, using the same shape ``daily_audit.py`` already
uses for its own scheduling: a small SQLite database independent of the
Postgres runtime schema, because the fact being stored (when did a
self-assessment last happen, and what did it recommend) is orchestration
bookkeeping, not fleet-execution state that belongs on the same transactional
footing as ``work_item``/``work_attempt``.

What it deliberately does NOT do: write to ``routing.ROUTING_MATRIX``. A
:class:`SelfAssessmentStore` records what the assessment recommended for a
domain and lets a caller ask "what's the latest recommended configuration for
this domain", but turning that recommendation into the routing matrix's
actual cascade order stays the reviewed code change ``self_assessment.py``'s
own docstring insists on. The store closes the "is it persisted and is it
per-quarter" half of the acceptance criterion; the human reviewing a diff
still closes the "and it actually took effect" half.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from command_center.orchestrator.self_assessment import (
    AttemptOutcome,
    Quarter,
    quarterly_self_assessment,
)

__all__ = [
    "AssessmentRecord",
    "SelfAssessmentStore",
    "run_quarterly_self_assessment",
]


def _iso(value: date | datetime) -> str:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value.isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class AssessmentRecord:
    """One persisted quarterly assessment for one domain."""

    task_class: str
    quarter: Quarter
    assessed_at: datetime
    recommendation: list[dict] | None


class SelfAssessmentStore:
    """SQLite-backed history of quarterly self-assessments.

    One row per (task_class, quarter): re-running the assessment inside a
    quarter that already has a row for a domain replaces it rather than
    appending, so "the latest recommendation for this domain" is always a
    single unambiguous lookup, never a scan needing its own tie-break.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._migrate()

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_self_assessment (
                    task_class      TEXT NOT NULL,
                    quarter_year    INTEGER NOT NULL,
                    quarter_number  INTEGER NOT NULL,
                    assessed_at     TEXT NOT NULL,
                    recommendation  TEXT,
                    PRIMARY KEY (task_class, quarter_year, quarter_number)
                );
                """
            )

    def last_assessed_at(self) -> datetime | None:
        """The timestamp of the most recent assessment across every domain —
        the single fact ``is_reassessment_due`` needs to gate the next run.
        A domain withheld this quarter (``None`` recommendation) still moves
        this forward, because withholding IS an assessment outcome, distinct
        from never having assessed at all."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT assessed_at FROM agent_self_assessment ORDER BY assessed_at DESC LIMIT 1"
            ).fetchone()
        return _parse(row["assessed_at"]) if row else None

    def record(
        self,
        task_class: str,
        quarter: Quarter,
        assessed_at: datetime,
        recommendation: list[dict] | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_self_assessment
                       (task_class, quarter_year, quarter_number, assessed_at, recommendation)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (task_class, quarter_year, quarter_number)
                   DO UPDATE SET assessed_at = excluded.assessed_at,
                                 recommendation = excluded.recommendation""",
                (
                    task_class,
                    quarter.year,
                    quarter.number,
                    _iso(assessed_at),
                    json.dumps(recommendation) if recommendation is not None else None,
                ),
            )

    def latest_configuration(self, task_class: str) -> list[dict] | None:
        """The most recently recommended cascade for ``task_class``, or
        ``None`` when no assessment has ever recommended a change (either
        because none has run yet, or every one so far withheld — see
        :func:`command_center.orchestrator.self_assessment.recommend_cascade`).
        This is "the best configuration per domain" the acceptance criterion
        names, read back after being updated."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT recommendation FROM agent_self_assessment
                   WHERE task_class = ? AND recommendation IS NOT NULL
                   ORDER BY quarter_year DESC, quarter_number DESC LIMIT 1""",
                (task_class,),
            ).fetchone()
        return json.loads(row["recommendation"]) if row else None

    def history(self, task_class: str) -> list[AssessmentRecord]:
        """Every recorded assessment for ``task_class``, oldest first — the
        audit trail proving the cadence actually ran each quarter rather than
        merely being capable of running."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT task_class, quarter_year, quarter_number, assessed_at, recommendation
                   FROM agent_self_assessment WHERE task_class = ?
                   ORDER BY quarter_year ASC, quarter_number ASC""",
                (task_class,),
            ).fetchall()
        return [
            AssessmentRecord(
                task_class=row["task_class"],
                quarter=Quarter(row["quarter_year"], row["quarter_number"]),
                assessed_at=_parse(row["assessed_at"]),
                recommendation=(
                    json.loads(row["recommendation"]) if row["recommendation"] else None
                ),
            )
            for row in rows
        ]


def run_quarterly_self_assessment(
    store: SelfAssessmentStore,
    outcomes: Iterable[AttemptOutcome],
    routing_matrix: Mapping[str, list[dict]],
    *,
    now: date | datetime,
    min_samples: int | None = None,
) -> dict:
    """The operational counterpart of :func:`quarterly_self_assessment`: reads
    ``store`` for when the last assessment ran, and — only when a new quarter
    is due — persists this run's recommendation for every domain before
    returning the same report shape the pure function produces.

    A quarter that is not yet due persists nothing and returns
    ``{"due": False, "quarter": None, "recommendations": {}}`` unchanged,
    so calling this on every scheduler tick (the same pattern
    ``DailyAuditService.tick`` uses) is always safe — most calls are a single
    read-only query against ``store``.
    """
    kwargs = {} if min_samples is None else {"min_samples": min_samples}
    report = quarterly_self_assessment(
        outcomes,
        routing_matrix,
        now=now,
        last_assessed_at=store.last_assessed_at(),
        **kwargs,
    )
    if not report["due"]:
        return report

    assessed_at = now if isinstance(now, datetime) else datetime(now.year, now.month, now.day)
    for task_class, recommendation in report["recommendations"].items():
        store.record(task_class, report["quarter"], assessed_at, recommendation)
    return report
