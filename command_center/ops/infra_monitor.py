"""Fail-closed health check for the current AICC worker architecture.

The predecessor lived only in ``/usr/local/sbin`` and watched retired unit
names.  This module deliberately derives health from the templated worker
lanes, the durable queue, and the configured metrics endpoint instead.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

_PROMETHEUS_ERRORS = (OSError, ValueError, http.client.HTTPException)


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    ready: int
    claimed: int
    succeeded: int
    dead: int
    success_age_seconds: float | None
    pending_age_seconds: float | None
    recent_dead: int = 0
    #: dead-lettered in the trailing hour by an executor quota/spend/rate refusal
    recent_quota_dead: int = 0
    #: succeeded in the trailing hour (throughput); None when not measured
    recent_succeeded: int | None = None


@dataclass(frozen=True, slots=True)
class PrWindowSnapshot:
    """What the PR review-window reconciler has and has not labelled.

    The window-gated workflows (CI, the Acceptance gate, boundary fitness)
    run on a pull request only while it carries a `review-window:` label, and
    the only thing that puts one there is the `backlog-pr-window` tick. So a
    tick that is not deployed is not a quiet degradation: every fleet PR opens
    with no CI at all and nothing says so. That is what happened -- the tick
    had never been installed on the control host, and 24 PRs plus #907 sat
    checkless until an operator labelled them by hand on 2026-09-09
    (VOYN-W0-AICC-PR-WINDOW-RECONCILER-NOT-DEPLOYED-ON-CONTROL). This probe is
    the alarm that was missing: it watches the reconciler's OUTPUT on GitHub,
    not its unit, so it is red whether the tick is absent, disabled, failing,
    or out of GitHub quota.
    """

    #: (number, age_seconds) per open PR that carries fleet `pr` evidence and
    #: none of the reconciler's window labels, older than the grace period.
    unlabelled: tuple[tuple[int, int], ...]
    #: Open PRs listed, and how many of those carry fleet `pr` evidence.
    open_prs: int
    evidence_prs: int
    grace_seconds: float
    #: Set when the probe could not measure (gh, the database, or a parse).
    #: A probe that cannot see is not a probe that saw nothing.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SourceCloneSnapshot:
    """Whether the source clone used by isolated reviews tracks origin.

    Read-only review lanes clone from the local bound source, so a local clone
    that stopped advancing is a silent review-quality outage even when workers
    and queues look healthy.  This probe compares the local checkout's HEAD to
    the remote default HEAD without updating refs, so it is safe for the
    fail-closed monitor's read-only systemd envelope.
    """

    path: str
    local_head: str | None
    remote_head: str | None
    stale: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class UnitState:
    restarts: int
    active_state: str
    result: str


@dataclass(frozen=True, slots=True)
class UnitHealthSnapshot:
    """Every service unit on the host, judged by restarts and failed state.

    worker-01 2026-09-02..14: `ollama.service` restarted every 3 s for twelve
    days (315 891 restarts) and `voyn-canary.service` every 15 s for eighteen
    (70 305) while every monitor stayed green, because each probe asked
    `is-active` of its own hand-picked units and a `Restart=always` unit is
    `activating` again by the time anyone looks. NRestarts is the number that
    cannot be hidden. Failed template instances (per-connection launcher
    units) collapse to one `template@*.service` with a count so a hundred
    dead connections are one finding, not a hundred.
    """

    crash_loops: tuple[tuple[str, int], ...]
    failed_units: tuple[tuple[str, int], ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DeployLagSnapshot:
    """What production reports versus the branch head it should be running.

    worker-01 2026-09-13 14:48 .. 09-14 18:49: six merged PRs sat behind a
    refused promotion for 28 hours while test and preprod advanced. A green
    branch says nothing about what customers run; only comparing the two
    SHAs does. The branch head is allowed a grace period (CI plus the merge
    queue plus the promotion tick) before its absence from production is a
    finding. A red branch head older than the grace period is a finding too,
    deliberately: a branch that stays red that long is a delivery outage.
    """

    repo: str
    branch: str
    branch_head: str | None
    branch_head_age_seconds: float | None
    deployed_sha: str | None
    grace_seconds: float
    error: str | None = None

    @property
    def lagging(self) -> bool:
        if self.error is not None or self.branch_head is None or self.deployed_sha is None:
            return False
        if self.deployed_sha == self.branch_head:
            return False
        return (self.branch_head_age_seconds or 0.0) > self.grace_seconds


@dataclass(frozen=True, slots=True)
class MonitorReport:
    ok: bool
    active_workers: int
    discovered_workers: int
    queue: QueueSnapshot | None
    prometheus_ready: bool
    failures: tuple[str, ...]
    pr_window: PrWindowSnapshot | None = None
    source_clone: SourceCloneSnapshot | None = None
    unit_health: UnitHealthSnapshot | None = None
    deploy_lag: DeployLagSnapshot | None = None


def parse_worker_units(output: str) -> dict[str, str]:
    """Parse ``systemctl list-units`` without accepting retired unit names."""
    states: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 4:
            continue
        unit, _load, active, _sub = fields[:4]
        if unit.startswith("voyn-aicc-worker@") and unit.endswith(".service"):
            states[unit] = active
    return states


def discover_worker_units() -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "list-units",
            "voyn-aicc-worker@*.service",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return parse_worker_units(completed.stdout)


def read_queue_snapshot() -> QueueSnapshot:
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE state = 'ready'),
                    count(*) FILTER (WHERE state = 'claimed'),
                    count(*) FILTER (WHERE state = 'succeeded'),
                    count(*) FILTER (WHERE state = 'dead'),
                    extract(epoch FROM (
                        now() - max(updated_at) FILTER (WHERE state = 'succeeded')
                    )),
                    extract(epoch FROM (
                        now() - min(updated_at)
                        FILTER (WHERE state IN ('ready', 'claimed'))
                    )),
                    count(*) FILTER (
                        WHERE state = 'dead'
                          AND updated_at > now() - interval '1 hour'
                    ),
                    count(*) FILTER (
                        WHERE state = 'dead'
                          AND updated_at > now() - interval '1 hour'
                          AND dead_reason ~* '(quota|spend limit|weekly limit|session limit|credit balance|rate limit)'
                    ),
                    count(*) FILTER (
                        WHERE state = 'succeeded'
                          AND updated_at > now() - interval '1 hour'
                    )
                FROM work_item
                """
            )
            (
                ready,
                claimed,
                succeeded,
                dead,
                success_age,
                pending_age,
                recent_dead,
                recent_quota_dead,
                recent_succeeded,
            ) = cur.fetchone()
    finally:
        pool.close_pool()
    return QueueSnapshot(
        ready=int(ready),
        claimed=int(claimed),
        succeeded=int(succeeded),
        dead=int(dead),
        success_age_seconds=(float(success_age) if success_age is not None else None),
        pending_age_seconds=(float(pending_age) if pending_age is not None else None),
        recent_dead=int(recent_dead),
        recent_quota_dead=int(recent_quota_dead),
        recent_succeeded=int(recent_succeeded),
    )


def window_label_names() -> frozenset[str]:
    """The labels the reconciler owns, taken from the reconciler itself.

    Imported rather than restated: a monitor holding its own copy of these
    strings goes green the moment the labels are renamed, which is exactly
    when it is needed most.
    """
    from command_center.orchestrator.review_merge import PrWindowConfig

    cfg = PrWindowConfig()
    return frozenset({cfg.label_active, cfg.label_waiting, cfg.label_blocked})


def pr_identity(url: str) -> str | None:
    """`owner/repo/pull/number` for a PR url, or None.

    Both sides of the comparison -- the `pr` evidence a task recorded and the
    url `gh pr list` reports -- are reduced to this, so a scheme, host-case or
    trailing-slash difference between them cannot silently empty the
    intersection and report a healthy fleet with nothing in view.
    """
    parts = [part for part in str(url).strip().lower().split("/") if part]
    if len(parts) < 4 or parts[-2] != "pull" or not parts[-1].isdigit():
        return None
    return "/".join(parts[-4:])


def unlabelled_evidence_prs(
    prs: list[dict[str, Any]],
    evidence: frozenset[str],
    *,
    now: float,
    labels: frozenset[str],
    grace_seconds: float,
) -> tuple[tuple[int, int], ...]:
    """Open fleet PRs the reconciler has left without a window label.

    Scoped to PRs that carry `pr` evidence: those are the ones the fleet
    opened and the ones whose gated workflows the fleet is waiting on. An
    unrelated PR on the same repo is somebody else's business.

    `grace_seconds` is measured from `createdAt`, not from the last label
    write -- there is no "when should this have been labelled" timestamp to
    read, and a PR that has been open for longer than several tick intervals
    with no label at all is the symptom either way. A PR younger than the
    grace period is simply not yet evidence of anything.
    """
    found: list[tuple[int, int]] = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        identity = pr_identity(pr.get("url") or "")
        if identity is None or identity not in evidence:
            continue
        carried = {
            str(label.get("name") or "")
            for label in (pr.get("labels") or [])
            if isinstance(label, dict)
        }
        if carried & labels:
            continue
        created = _parse_iso8601(str(pr.get("createdAt") or ""))
        if created is None:
            # An unparseable timestamp is not a licence to accuse the
            # reconciler; the PR is reported by the next tick that can read it.
            continue
        age = now - created
        if age > grace_seconds:
            found.append((int(pr.get("number") or 0), int(age)))
    return tuple(sorted(found))


def _parse_iso8601(value: str) -> float | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # An offsetless timestamp is UTC, not the monitor host's local time: a
    # naive `.timestamp()` would make the same PR's age differ by the TZ
    # offset from host to host, and near the grace boundary that is the
    # difference between a finding and silence.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _open_prs(repo_path: str, scan_limit: int) -> list[dict[str, Any]]:
    """One `gh pr list` call, oldest-created first.

    Deliberately one request with no per-PR detail lookups: this shares a
    GitHub GraphQL quota with the review, merge and window ticks themselves,
    and a monitor that exhausts the quota manufactures the outage it is
    watching for (VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS). Ascending
    creation order means a repo with more open PRs than `scan_limit` keeps the
    OLDEST -- the ones a missing label has hurt longest -- in view.
    """
    completed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--search",
            "sort:created-asc",
            "--limit",
            str(max(scan_limit, 1)),
            "--json",
            "number,url,createdAt,labels",
        ],
        cwd=repo_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"gh pr list failed: {(completed.stderr or '').strip()[:120]}"
        )
    listed = json.loads(completed.stdout or "[]")
    if not isinstance(listed, list):
        raise RuntimeError("gh pr list returned no array")
    return listed


def read_pr_evidence() -> frozenset[str]:
    """Every PR the backlog has recorded as `pr` evidence, as identities."""
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT value FROM backlog_evidence WHERE kind = 'pr'")
            rows = cur.fetchall()
    finally:
        pool.close_pool()
    return frozenset(
        identity
        for identity in (pr_identity(row[0]) for row in rows)
        if identity is not None
    )


def read_pr_window_snapshot(
    repo_path: str, *, grace_seconds: float, scan_limit: int, now: float | None = None
) -> PrWindowSnapshot:
    """Measure the reconciler by its effect on GitHub, never raising.

    A probe failure travels in `error` rather than out of this function: it
    is a finding of its own class, and losing the queue and worker
    measurements of the same tick to it would be trading one blind spot for
    a bigger one.
    """
    try:
        labels = window_label_names()
        prs = _open_prs(repo_path, scan_limit)
        evidence = read_pr_evidence()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return PrWindowSnapshot(
            unlabelled=(),
            open_prs=0,
            evidence_prs=0,
            grace_seconds=grace_seconds,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
    listed = [pr for pr in prs if isinstance(pr, dict)]
    fleet = [
        pr for pr in listed if (pr_identity(pr.get("url") or "") or "") in evidence
    ]
    return PrWindowSnapshot(
        unlabelled=unlabelled_evidence_prs(
            listed,
            evidence,
            now=time.time() if now is None else now,
            labels=labels,
            grace_seconds=grace_seconds,
        ),
        open_prs=len(listed),
        evidence_prs=len(fleet),
        grace_seconds=grace_seconds,
    )


def _git_stdout(repo_path: str, args: list[str], *, timeout: int = 30) -> str:
    completed = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            (completed.stderr or completed.stdout or "git failed").strip()[:160]
        )
    return completed.stdout.strip()


def read_source_clone_snapshot(repo_path: str) -> SourceCloneSnapshot:
    """Measure source-clone freshness, never raising."""
    try:
        local_head = _git_stdout(repo_path, ["rev-parse", "HEAD"])
        remote_output = _git_stdout(
            repo_path, ["ls-remote", "origin", "HEAD"], timeout=60
        )
        remote_head = remote_output.split(None, 1)[0] if remote_output else ""
        if not remote_head:
            raise RuntimeError("origin HEAD was empty")
        return SourceCloneSnapshot(
            path=repo_path,
            local_head=local_head,
            remote_head=remote_head,
            stale=local_head != remote_head,
        )
    except Exception as exc:  # noqa: BLE001 - see read_pr_window_snapshot
        return SourceCloneSnapshot(
            path=repo_path,
            local_head=None,
            remote_head=None,
            stale=True,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def parse_unit_show(output: str) -> dict[str, UnitState]:
    """Parse blank-line separated `systemctl show` blocks into unit states."""
    units: dict[str, UnitState] = {}
    for block in output.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        unit = fields.get("Id")
        if not unit:
            continue
        try:
            restarts = int(fields.get("NRestarts", "0") or 0)
        except ValueError:
            restarts = 0
        units[unit] = UnitState(
            restarts=restarts,
            active_state=fields.get("ActiveState", ""),
            result=fields.get("Result", ""),
        )
    return units


def _template_name(unit: str) -> str:
    """`aicc-agent-launcher@2079-27636-984.service` -> `aicc-agent-launcher@*.service`."""
    name, at, rest = unit.partition("@")
    if not at:
        return unit
    suffix = rest[rest.rfind(".") :] if "." in rest else ""
    return f"{name}@*{suffix}"


def evaluate_unit_health(
    units: dict[str, UnitState], *, crash_loop_restarts: int
) -> UnitHealthSnapshot:
    crash_loops = tuple(
        sorted(
            (unit, state.restarts)
            for unit, state in units.items()
            if state.restarts >= max(crash_loop_restarts, 1)
        )
    )
    failed: dict[str, int] = {}
    for unit, state in units.items():
        if state.active_state == "failed":
            key = _template_name(unit)
            failed[key] = failed.get(key, 0) + 1
    return UnitHealthSnapshot(
        crash_loops=crash_loops, failed_units=tuple(sorted(failed.items()))
    )


def read_unit_health_snapshot(*, crash_loop_restarts: int) -> UnitHealthSnapshot:
    """Two unprivileged systemctl calls for the whole host, never raising."""
    try:
        listed = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--type=service",
                "--all",
                "--plain",
                "--no-legend",
                "--no-pager",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        names = sorted(
            {
                line.split(None, 1)[0]
                for line in listed.stdout.splitlines()
                if line.strip() and line.split(None, 1)[0].endswith(".service")
            }
        )
        if not names:
            raise RuntimeError("systemctl listed no service units")
        shown = subprocess.run(
            [
                "systemctl",
                "show",
                "--property=Id,NRestarts,ActiveState,Result",
                "--no-pager",
                "--",
                *names,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return evaluate_unit_health(
            parse_unit_show(shown.stdout), crash_loop_restarts=crash_loop_restarts
        )
    except Exception as exc:  # noqa: BLE001 - see read_pr_window_snapshot
        return UnitHealthSnapshot(
            crash_loops=(),
            failed_units=(),
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def _fetch_json(url: str, timeout: int = 5) -> dict[str, Any]:
    """GET a small loopback/LAN JSON document with the same URL hygiene as
    the Prometheus probe (no credentials in the URL, http(s) only)."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError("version url must be a plain http(s) url")
    client = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = client(parsed.hostname, parsed.port, timeout=timeout)
    try:
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection.request("GET", target)
        response = connection.getresponse()
        body = response.read(64 * 1024).decode("utf-8", errors="replace")
        if response.status != 200:
            raise RuntimeError(f"version url returned {response.status}")
        document = json.loads(body)
    finally:
        connection.close()
    if not isinstance(document, dict):
        raise TypeError("version url returned no object")
    return document


def read_deploy_lag_snapshot(
    repo: str, version_url: str, *, branch: str, grace_seconds: float
) -> DeployLagSnapshot:
    """One `gh api` request for the branch head, one GET for what runs.

    `gh` authenticates from the unit's GH_CONFIG_DIR (the fleet's GitHub App
    store on the worker), never from a token in arguments or logs.
    """
    try:
        deployed = _fetch_json(version_url)
        deployed_sha = str(deployed.get("release_sha") or "").strip()
        if not deployed_sha:
            raise RuntimeError("version document carries no release_sha")
        completed = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{branch}"],
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"gh api failed: {(completed.stderr or '').strip()[:120]}"
            )
        commit = json.loads(completed.stdout or "{}")
        head = str(commit.get("sha") or "").strip()
        committed = _parse_iso8601(
            str(commit.get("commit", {}).get("committer", {}).get("date") or "")
        )
        if not head or committed is None:
            raise RuntimeError("gh api returned no sha or committer date")
        return DeployLagSnapshot(
            repo=repo,
            branch=branch,
            branch_head=head,
            branch_head_age_seconds=max(time.time() - committed, 0.0),
            deployed_sha=deployed_sha,
            grace_seconds=grace_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - see read_pr_window_snapshot
        return DeployLagSnapshot(
            repo=repo,
            branch=branch,
            branch_head=None,
            branch_head_age_seconds=None,
            deployed_sha=None,
            grace_seconds=grace_seconds,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def prometheus_is_ready(url: str) -> bool:
    connection: http.client.HTTPConnection | None = None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        client = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = client(parsed.hostname, parsed.port, timeout=5)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection.request("GET", target)
        response = connection.getresponse()
        body = response.read(256).decode("utf-8", errors="replace")
        return response.status == 200 and "ready" in body.lower()
    except _PROMETHEUS_ERRORS:
        return False
    finally:
        if connection is not None:
            connection.close()


def evaluate(
    worker_states: dict[str, str],
    queue: QueueSnapshot | None,
    *,
    minimum_active_workers: int,
    max_stalled_seconds: float,
    prometheus_ready: bool,
    max_recent_dead: int = 0,
    pr_window: PrWindowSnapshot | None = None,
    source_clone: SourceCloneSnapshot | None = None,
    unit_health: UnitHealthSnapshot | None = None,
    deploy_lag: DeployLagSnapshot | None = None,
) -> MonitorReport:
    active_workers = sum(state == "active" for state in worker_states.values())
    failures: list[str] = []
    if active_workers < minimum_active_workers:
        failures.append(f"active_workers:{active_workers}<{minimum_active_workers}")
    if not prometheus_ready:
        failures.append("prometheus_unready")

    # An old last-success timestamp is normal when there is no work. It becomes
    # context in the report once work appears; the oldest pending item is the
    # alert clock. Unrelated successful work must not hide a zombie claim.
    if queue is not None:
        pending_is_stale = (
            queue.pending_age_seconds is not None
            and queue.pending_age_seconds > max_stalled_seconds
        )
        if queue.ready + queue.claimed > 0 and pending_is_stale:
            failures.append("queue_stalled")
        if queue.recent_dead > max_recent_dead:
            failures.append(f"dead_letter_growth:{queue.recent_dead}>{max_recent_dead}")
        # Executor quota/spend/rate refusals are a capacity fact the fleet
        # cannot retry through; surface them as their own class so routing
        # (quota-aware cascade) and budgets get a task, not a guess.
        if queue.recent_quota_dead > 0:
            failures.append(f"executor_quota_exhausted:{queue.recent_quota_dead}")
        # Throughput: work waiting (past the stall clock) with nothing having
        # succeeded in the trailing hour is a stalled pipeline even when every
        # lane shows active -- the lanes may be spinning on refusals.
        if (
            queue.recent_succeeded is not None
            and queue.recent_succeeded == 0
            and queue.ready + queue.claimed > 0
            and not pending_is_stale
        ):
            failures.append("throughput_stalled:0_succeeded_in_1h")

    if pr_window is not None:
        if pr_window.error is not None:
            failures.append(f"pr_window_probe_failed:{pr_window.error}")
        elif pr_window.unlabelled:
            # One finding for the reconciler, not one per PR: the identity is
            # the code before the first ':' (`finding_key`), and the PR
            # numbers ride in the detail. The oldest first, because that age
            # is how long the fleet has been opening PRs that get no CI.
            oldest = max(age for _number, age in pr_window.unlabelled)
            numbers = ",".join(str(number) for number, _age in pr_window.unlabelled)
            failures.append(
                f"pr_window_unlabelled:{len(pr_window.unlabelled)}_prs"
                f"_oldest_{oldest}s>{int(pr_window.grace_seconds)}s:{numbers}"
            )

    if source_clone is not None:
        if source_clone.error is not None:
            failures.append(f"source_clone_probe_failed:{source_clone.error}")
        elif source_clone.stale:
            local = (source_clone.local_head or "unknown")[:12]
            remote = (source_clone.remote_head or "unknown")[:12]
            failures.append(f"source_clone_stale:{local}!={remote}")

    if unit_health is not None:
        if unit_health.error is not None:
            failures.append(f"unit_health_probe_failed:{unit_health.error}")
        else:
            # One finding per class, every unit in the detail: the identity
            # is the code before the first ':' (`finding_key`).
            if unit_health.crash_loops:
                listing = ",".join(f"{u}={n}" for u, n in unit_health.crash_loops)
                failures.append(
                    f"crash_loop:{len(unit_health.crash_loops)}_units:{listing}"
                )
            if unit_health.failed_units:
                listing = ",".join(f"{u}={n}" for u, n in unit_health.failed_units)
                failures.append(
                    f"failed_units:{len(unit_health.failed_units)}:{listing}"
                )

    if deploy_lag is not None:
        if deploy_lag.error is not None:
            failures.append(f"deploy_lag_probe_failed:{deploy_lag.error}")
        elif deploy_lag.lagging:
            failures.append(
                f"deploy_lag:{deploy_lag.repo}:{(deploy_lag.deployed_sha or '')[:8]}"
                f"!={(deploy_lag.branch_head or '')[:8]}"
                f"_age_{int(deploy_lag.branch_head_age_seconds or 0)}s"
                f">{int(deploy_lag.grace_seconds)}s"
            )

    return MonitorReport(
        ok=not failures,
        active_workers=active_workers,
        discovered_workers=len(worker_states),
        queue=queue,
        prometheus_ready=prometheus_ready,
        failures=tuple(failures),
        pr_window=pr_window,
        source_clone=source_clone,
        unit_health=unit_health,
        deploy_lag=deploy_lag,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m command_center.ops.infra_monitor")
    parser.add_argument("--minimum-active-workers", type=int, default=1)
    parser.add_argument("--max-stalled-seconds", type=float, default=900)
    parser.add_argument(
        "--max-recent-dead",
        type=int,
        default=0,
        help="Maximum dead-lettered items allowed in the trailing hour.",
    )
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument(
        "--skip-workers",
        action="store_true",
        help="Do not inspect local worker units (for the control-host queue probe).",
    )
    parser.add_argument(
        "--skip-queue",
        action="store_true",
        help="Do not read queue tables (for the least-privileged worker-host probe).",
    )
    parser.add_argument(
        "--pr-window-repo",
        default=os.environ.get("AICC_PR_WINDOW_REPO", ""),
        metavar="PATH",
        help=(
            "Directory to run one `gh pr list` in, to check that the PR "
            "review-window reconciler is actually labelling open fleet PRs. "
            "Empty (the default) skips the probe, so a host without gh or "
            "without the backlog database is unaffected. Defaults to "
            "$AICC_PR_WINDOW_REPO, which is how the control unit turns it on."
        ),
    )
    parser.add_argument(
        "--pr-window-grace-seconds",
        type=float,
        default=900,
        help=(
            "How long an open PR carrying `pr` evidence may go without a "
            "window label before it is a finding. Default 15 minutes: three "
            "intervals of the five-minute tick."
        ),
    )
    parser.add_argument(
        "--pr-window-scan-limit",
        type=int,
        default=200,
        help="Open PRs the single `gh pr list` asks for, oldest-created first.",
    )
    parser.add_argument(
        "--source-clone-repo",
        default=os.environ.get("AICC_SOURCE_CLONE_REPO", ""),
        metavar="PATH",
        help=(
            "Local source clone used by isolated read-only reviews. When set, "
            "the monitor compares its HEAD to origin/HEAD via git ls-remote "
            "and fails if the clone is stale or cannot be measured."
        ),
    )
    parser.add_argument(
        "--record-findings",
        metavar="SOURCE",
        default="",
        help=(
            "Record every failure as an open monitor_finding under this source "
            "(and clear the source's findings when healthy), so the planner turns "
            "a red monitor into a pipeline task instead of a failed unit."
        ),
    )
    parser.add_argument(
        "--unit-health",
        action="store_true",
        help="Read every service unit on the host: NRestarts crash loops and failed units.",
    )
    parser.add_argument(
        "--crash-loop-restarts",
        type=int,
        default=5,
        help="NRestarts at or above which a unit is a crash loop (default 5).",
    )
    parser.add_argument(
        "--deploy-lag-repo",
        default=None,
        help="GitHub owner/name whose branch head production must run (off by default).",
    )
    parser.add_argument(
        "--deploy-lag-version-url",
        default=None,
        help="URL of the deployed service's /version document carrying release_sha.",
    )
    parser.add_argument("--deploy-lag-branch", default="main")
    parser.add_argument(
        "--deploy-lag-grace-seconds",
        type=float,
        default=2700.0,
        help="How old the branch head may be before its absence from production is a finding.",
    )
    return parser


def finding_key(failure: str) -> str:
    """The stable identity of a failure: its code before the first ':'
    (`active_workers:2<4` -> `active_workers`), truncated like the column."""
    return failure.split(":", 1)[0].strip()[:200] or failure[:200]


def record_findings(
    source: str, failures: tuple[str, ...], detail: dict[str, Any]
) -> None:
    """Write the measurement to the database through the SECURITY DEFINER
    functions of migration 0021 (granted to aicc_app and aicc_worker). A red
    monitor becomes a task the fleet fixes; a healthy one clears its rows.
    Never lets a recording problem mask the measurement: raises, and main()
    reports monitor_error while still exiting non-zero."""
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if failures:
                for failure in failures:
                    # Identity is the failure CODE (before the first ':'),
                    # never the measurement: "dead_letter_growth:53>0" and
                    # "dead_letter_growth:54>0" are one open finding and one
                    # task, not one per tick (control-01, 2026-09-08: four
                    # tasks in ten minutes for the same red probe). The
                    # measured text travels in the detail.
                    cur.execute(
                        "SELECT monitor_record_finding(%s, %s, %s::jsonb)",
                        (
                            source,
                            finding_key(failure),
                            json.dumps({**detail, "failure": failure}, sort_keys=True),
                        ),
                    )
            else:
                cur.execute("SELECT monitor_clear_finding(%s)", (source,))
            conn.commit()
    finally:
        pool.close_pool()


def _json_report(report: MonitorReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["failures"] = list(report.failures)
    if report.deploy_lag is not None:
        # `asdict` serialises fields only; the verdict is a property.
        payload["deploy_lag"]["lagging"] = report.deploy_lag.lagging
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workers = {} if args.skip_workers else discover_worker_units()
        queue = None if args.skip_queue else read_queue_snapshot()
        metrics_ready = prometheus_is_ready(args.prometheus_url)
        pr_window = (
            read_pr_window_snapshot(
                args.pr_window_repo,
                grace_seconds=args.pr_window_grace_seconds,
                scan_limit=args.pr_window_scan_limit,
            )
            if args.pr_window_repo
            else None
        )
        source_clone = (
            read_source_clone_snapshot(args.source_clone_repo)
            if args.source_clone_repo
            else None
        )
        unit_health = (
            read_unit_health_snapshot(crash_loop_restarts=args.crash_loop_restarts)
            if args.unit_health
            else None
        )
        if bool(args.deploy_lag_repo) != bool(args.deploy_lag_version_url):
            raise RuntimeError(
                "--deploy-lag-repo and --deploy-lag-version-url must be given together"
            )
        deploy_lag = (
            read_deploy_lag_snapshot(
                args.deploy_lag_repo,
                args.deploy_lag_version_url,
                branch=args.deploy_lag_branch,
                grace_seconds=args.deploy_lag_grace_seconds,
            )
            if args.deploy_lag_repo
            else None
        )
        report = evaluate(
            workers,
            queue,
            minimum_active_workers=args.minimum_active_workers,
            max_stalled_seconds=args.max_stalled_seconds,
            prometheus_ready=metrics_ready,
            max_recent_dead=args.max_recent_dead,
            pr_window=pr_window,
            source_clone=source_clone,
            unit_health=unit_health,
            deploy_lag=deploy_lag,
        )
    except Exception as exc:  # noqa: BLE001 - the monitor itself must fail closed
        print(json.dumps({"ok": False, "failures": [f"monitor_error:{exc}"]}))
        return 1
    payload = _json_report(report)
    recorded = True
    if args.record_findings:
        try:
            record_findings(args.record_findings, report.failures, payload)
            payload["findings_recorded"] = True
        except Exception as exc:  # noqa: BLE001 - recording must not hide the measurement
            # The measurement is still printed in full; the exit code says
            # the monitor did NOT do its whole job. A healthy measurement
            # whose persistence failed exited 0 before (review of fc167cf7),
            # so a broken finding store went unnoticed exactly when the
            # monitor is trusted to turn red into a task.
            recorded = False
            payload["findings_recorded"] = False
            payload["findings_error"] = str(exc)[:200]
    print(json.dumps(payload, sort_keys=True))
    return 0 if report.ok and recorded else 1


if __name__ == "__main__":
    raise SystemExit(main())
