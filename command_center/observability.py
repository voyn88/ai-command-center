"""Prometheus telemetry for the server control plane.

The collector deliberately queries redacted/public queue views.  Payloads and
claim-token hashes must never become labels: labels are copied into alerts and
long-lived time-series databases.
"""

from __future__ import annotations

import resource
import sys
import threading
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sample(name: str, value: object, labels: dict[str, object] | None = None) -> str:
    rendered = ""
    if labels:
        rendered = "{" + ",".join(
            f'{key}="{_escape(labels[key])}"' for key in sorted(labels)
        ) + "}"
    return f"{name}{rendered} {value}\n"


def _resident_memory_bytes(usage: resource.struct_rusage) -> float:
    """`ru_maxrss` is kilobytes on Linux and bytes on macOS (getrusage(2))."""
    return float(usage.ru_maxrss) * (1 if sys.platform == "darwin" else 1024)


def _connection(factory: Any = None) -> Any:
    if factory is not None:
        return factory()
    from command_center.db import pool

    return pool.connection()


def render_control_metrics(connection_factory: Any = None) -> str:
    """Render bounded-cardinality queue, lease and worker telemetry."""
    now = time.time()
    lines = [
        "# HELP aicc_control_up Control process liveness.\n",
        "# TYPE aicc_control_up gauge\n",
        _sample("aicc_control_up", 1),
        "# HELP aicc_queue_items Queue items by queue and state.\n",
        "# TYPE aicc_queue_items gauge\n",
        "# HELP aicc_queue_lag_seconds Age of the oldest ready item.\n",
        "# TYPE aicc_queue_lag_seconds gauge\n",
        "# HELP aicc_lease_age_seconds Age since the latest lease heartbeat.\n",
        "# TYPE aicc_lease_age_seconds gauge\n",
        "# HELP aicc_lease_remaining_seconds Time until the lease expires.\n",
        "# TYPE aicc_lease_remaining_seconds gauge\n",
    ]
    try:
        with _connection(connection_factory) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT queue, state, count(*) FROM work_item_public "
                "GROUP BY queue, state ORDER BY queue, state"
            )
            rows = cur.fetchall()
            for queue, state, count in rows:
                lines.append(
                    _sample(
                        "aicc_queue_items", count, {"queue": queue, "state": state}
                    )
                )
            cur.execute(
                "SELECT queue, GREATEST(COALESCE(EXTRACT(EPOCH FROM "
                "(now() - min(available_at))), 0), 0) FROM work_item_public "
                "WHERE state = 'ready' GROUP BY queue ORDER BY queue"
            )
            for queue, lag in cur.fetchall():
                lines.append(
                    _sample("aicc_queue_lag_seconds", float(lag), {"queue": queue})
                )
            cur.execute(
                "SELECT i.task_id, i.repository_id, a.claimed_by_role, a.attempt_id, "
                "EXTRACT(EPOCH FROM (now() - COALESCE(a.heartbeat_at, a.created_at))), "
                "EXTRACT(EPOCH FROM (a.visible_until - now())) "
                "FROM work_item_public i JOIN work_attempt_public a "
                "ON a.attempt_id = i.current_attempt_id WHERE i.state = 'claimed' "
                "ORDER BY a.attempt_id"
            )
            for task, repository, worker, attempt, age, remaining in cur.fetchall():
                labels = {
                    "task": task or "unknown", "repository": repository or "unknown",
                    "worker": worker, "attempt": attempt,
                }
                lines.append(_sample("aicc_lease_age_seconds", float(age), labels))
                lines.append(
                    _sample("aicc_lease_remaining_seconds", float(remaining), labels)
                )
        lines.append(_sample("aicc_metrics_scrape_error", 0))
    except Exception:
        # A scrape endpoint must remain parseable during a database incident.
        lines.append(_sample("aicc_metrics_scrape_error", 1))
    usage = resource.getrusage(resource.RUSAGE_SELF)
    lines.extend(
        [
            _sample("aicc_process_cpu_seconds_total", usage.ru_utime + usage.ru_stime),
            _sample(
                "aicc_process_resident_memory_bytes",
                _resident_memory_bytes(usage),
            ),
            _sample("aicc_metrics_generated_unixtime", now),
        ]
    )
    return "".join(lines)


@dataclass
class WorkerTelemetry:
    """In-process worker state; served by the optional worker metrics listener."""

    worker: str = "unknown"
    task: str = "idle"
    sha: str = "unknown"
    attempt: str = "none"
    cost_per_hour: float = 0.0
    lease_started: float | None = None
    completed: int = 0
    failed: int = 0
    runtime_seconds: float = 0.0
    _lock: threading.Lock = dataclass_field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def start(self, *, task: object, sha: object, attempt: object) -> None:
        """Publish one consistent active-task snapshot to the scrape thread."""
        with self._lock:
            self.task = str(task)
            self.sha = str(sha)
            self.attempt = str(attempt)
            self.lease_started = time.time()

    def finish(self, *, succeeded: bool, sha: object | None = None) -> None:
        """Account for runtime and clear liveness state atomically."""
        with self._lock:
            if self.lease_started is not None:
                self.runtime_seconds += max(0.0, time.time() - self.lease_started)
            if succeeded:
                self.completed += 1
            else:
                self.failed += 1
            if sha:
                self.sha = str(sha)
            self.lease_started = None

    def render(self) -> str:
        with self._lock:
            worker = self.worker
            task = self.task
            sha = self.sha
            attempt = self.attempt
            lease_started = self.lease_started
            completed = self.completed
            failed = self.failed
            runtime_seconds = self.runtime_seconds
            cost_per_hour = self.cost_per_hour
        labels = {"worker": worker}
        active = lease_started is not None
        detail = {**labels, "task": task, "sha": sha, "attempt": attempt}
        age = max(0.0, time.time() - lease_started) if active else 0.0
        accounted_runtime = runtime_seconds + age
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return "".join((
            "# HELP aicc_worker_up Worker process liveness.\n",
            "# TYPE aicc_worker_up gauge\n",
            _sample("aicc_worker_up", 1, labels),
            "# HELP aicc_worker_active Current task identity and source SHA.\n",
            "# TYPE aicc_worker_active gauge\n",
            _sample("aicc_worker_active", int(active), detail),
            "# HELP aicc_worker_lease_age_seconds Current task execution age.\n",
            "# TYPE aicc_worker_lease_age_seconds gauge\n",
            _sample("aicc_worker_lease_age_seconds", age, detail),
            "# HELP aicc_worker_tasks_total Completed tasks by outcome.\n",
            "# TYPE aicc_worker_tasks_total counter\n",
            _sample(
                "aicc_worker_tasks_total",
                completed,
                {**labels, "outcome": "succeeded"},
            ),
            _sample(
                "aicc_worker_tasks_total",
                failed,
                {**labels, "outcome": "failed"},
            ),
            "# HELP aicc_worker_task_runtime_seconds_total Worker wall time consumed by tasks.\n",
            "# TYPE aicc_worker_task_runtime_seconds_total counter\n",
            _sample(
                "aicc_worker_task_runtime_seconds_total", accounted_runtime, labels
            ),
            "# HELP aicc_worker_estimated_cost_total Estimated task compute cost in configured currency.\n",
            "# TYPE aicc_worker_estimated_cost_total counter\n",
            _sample(
                "aicc_worker_estimated_cost_total",
                accounted_runtime * cost_per_hour / 3600,
                labels,
            ),
            "# HELP aicc_worker_cpu_seconds_total Worker user and system CPU time.\n",
            "# TYPE aicc_worker_cpu_seconds_total counter\n",
            _sample(
                "aicc_worker_cpu_seconds_total",
                usage.ru_utime + usage.ru_stime,
                labels,
            ),
            "# HELP aicc_worker_resident_memory_bytes Worker peak resident memory.\n",
            "# TYPE aicc_worker_resident_memory_bytes gauge\n",
            _sample(
                "aicc_worker_resident_memory_bytes",
                _resident_memory_bytes(usage),
                labels,
            ),
        ))
