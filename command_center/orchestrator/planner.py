"""The planner tick (BO-S2): eligible tasks -> atomic dispatches -> a report.

One tick, no loop: the schedule is a systemd oneshot timer
(deploy/systemd/aicc-backlog-planner.timer), the reaper's pattern — a missed
tick delays planning and never corrupts it, because every mutating step is
one call to ``backlog_dispatch`` (0006), which is atomic or refused.

Single planner, machine-held: the tick first takes the ``planner:global``
lease. A second control host running the same timer gets ``planner_busy``
and an empty report — not a second writer.

The report is the owner's answer to "why is my task waiting": every
non-dispatched candidate lands in exactly one bucket with the dispatch
function's own refusal reason, including ``skipped_by_wave_gate``
(approved decision 1) so the UI can say "wave N is still working".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from command_center import git_info
from command_center.orchestrator.routing import cascade_for
from command_center.worker.payloads import AGENT_RUN_SCHEMA_VERSION

__all__ = [
    "MergedAuthority",
    "PlanLimits",
    "PlanReport",
    "acceptance_symbols",
    "merged_authority",
    "plan_once",
    "remediation_parent",
]

_PLANNER_AUTHORITY = "planner:global"


@dataclass(frozen=True, slots=True)
class PlanLimits:
    planner: str = "aicc-planner"
    wip_limit: int = 4
    #: Repo leases must outlive the dispatched RUN (hours).
    lease_ttl_seconds: int = 7200
    #: The planner:global lease covers one TICK (seconds) — its own parameter
    #: (VOYN-W0-AICC-PLANNER-LEASE-TTL, resolved and pinned by
    #: test_the_tick_lease_uses_its_own_ttl_not_the_repo_horizon): a tick
    #: that dies before releasing must not lock every control host out for
    #: the repo-lease horizon.
    planner_lease_ttl_seconds: int = 300
    #: Per-tick dispatch cap, distinct from WIP: one tick must stay short.
    max_dispatches_per_tick: int = 4
    #: Wall-time envelope handed to each dispatched RUN. 900 s timed out 6 of
    #: the first 68 isolated implementation runs on 2026-09-09 (VOYN-W0-AICC-
    #: FLEET-LAST-MILE-PUBLISH); 45 min matches the launcher's own run bound
    #: while staying under the 2 h repo lease horizon above.
    timeout_seconds: int = 2700
    #: Per-tick cap on DEFER_TO_USER auto-resumes (VOYN-W0-AICC-DEFER-AUTO-
    #: RESUME). Bounded so a large parked backlog drains gradually across
    #: ticks instead of flooding OPEN in one; 0 disables the reconcile.
    max_resumes_per_tick: int = 10
    #: The planner's dispatch-only backpressure fence: once this many
    #: READY_TO_REVIEW tasks are carrying `pr` evidence, this tick dispatches
    #: nothing new, so implementation supply stops outrunning review/merge
    #: capacity. Scoped to dispatch alone -- see `Planner.plan_once`'s
    #: comment at the fence check for why ingest and the DEFER_TO_USER
    #: resume reconcile above run unconditionally regardless of this fence.
    #: 0 disables the fence, matching `max_resumes_per_tick`'s convention
    #: in this same class (not a threshold of 1: an operator following that
    #: sibling field's convention would otherwise get a silently different
    #: meaning for the same sentinel value on this field).
    review_backlog_limit: int = 20


@dataclass(slots=True)
class PlanReport:
    dispatched: list[tuple[str, str]] = field(default_factory=list)  # (task, work_item)
    skipped_by_wave_gate: list[tuple[str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    undispatchable: list[tuple[str, str]] = field(default_factory=list)
    ingested: list[tuple[str, str]] = field(default_factory=list)  # (task, action)
    #: (task, original park reason) — DEFER_TO_USER technical parks the 0014
    #: gate returned to OPEN this tick (VOYN-W0-AICC-DEFER-AUTO-RESUME).
    resumed: list[tuple[str, str]] = field(default_factory=list)
    planner_busy: bool = False
    #: The review backlog count that tripped `PlanLimits.review_backlog_
    #: limit` this tick (None when the fence never fired). Ingest and the
    #: DEFER_TO_USER resume reconcile still ran -- only dispatch paused.
    review_window_full: int | None = None
    #: Pipeline tasks dispatched in decomposition mode this tick (the second
    #: non-technical return asked for a split; 0021).
    split_dispatched: list[str] = field(default_factory=list)
    #: (task, failure) pipeline tasks created this tick from open monitor
    #: findings (0021 monitor_finding).
    monitor_tasks: list[tuple[str, str]] = field(default_factory=list)
    #: Pipeline-class tasks dispatched THROUGH the review-backlog fence this
    #: tick: work that repairs CI, review, merge train, planner or queue must
    #: never wait behind the backlog it exists to drain
    #: (VOYN-W0-AICC-PLANNER-PIPELINE-CLASS-PRIORITY-AND-WINDOW-PAUSE).
    pipeline_bypass: list[str] = field(default_factory=list)
    #: The fence fired while no execution work item was ready or claimed:
    #: idle lanes are pure waste, so the tick dispatches its ordinary bounded
    #: batch anyway (backpressure resumes as soon as a lane has work).
    idle_trickle: bool = False
    #: Functional candidates held back by the fence this tick (count only).
    fenced: int = 0
    #: (task, evidence) remediation candidates the reuse gate closed as
    #: superseded instead of dispatching, because the parent's acceptance was
    #: already satisfied on main (VOYN-W0-AICC-DISPATCH-REUSE-GATE).
    superseded: list[tuple[str, str]] = field(default_factory=list)

    @property
    def prevented_duplicate_dispatches(self) -> int:
        """The gate's telemetry: how many duplicate dispatches this tick did
        not make. Derived rather than counted separately so the number and
        the evidence behind it cannot drift apart; the durable count across
        ticks is the `close_superseded` granted event in `backlog_event`."""
        return len(self.superseded)


# Repo → (canonical project_id, worker-host repository path). The worker's
# validate_repository accepts only PROJECT_IDS members with the configured
# path, so the planner must translate the backlog's repo hint into that
# vocabulary — a task whose repo has no route is reported, never dispatched
# into a guaranteed dead-letter. One fleet, one table, env-overridable;
# per-host routing belongs to the multi-host slice (recorded in the epic).
_DEFAULT_REPO_ROUTES: dict[str, tuple[str, str]] = {
    "ai-command-center": ("AICC", "/home/voynadmin/Projects/ai-command-center"),
    "aios": ("AIOS", "/home/voynadmin/Projects/aios"),
    "voyn-logistics-crm": ("CRM", "/home/voynadmin/Projects/voyn-logistics-crm"),
    "~/Projects/aios": ("AIOS", "/home/voynadmin/Projects/aios"),
    "~/Projects/ai-command-center": ("AICC", "/home/voynadmin/Projects/ai-command-center"),
    "~/Projects/voyn-logistics-crm": ("CRM", "/home/voynadmin/Projects/voyn-logistics-crm"),
}


def repo_route(repo: str) -> tuple[str, str] | None:
    raw = os.environ.get("AICC_PLANNER_REPO_ROUTES", "")
    if raw:
        # A broken override routes NOTHING — and "broken" includes decodable
        # JSON of the wrong shape: a bare string would be silently misrouted
        # by indexing ("AICC" -> ("A", "I")), a dict raises out of the tick,
        # a non-string path dead-letters downstream. Review proved all three
        # live; every value must be exactly two non-empty strings.
        try:
            decoded = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(decoded, dict):
            return None
        table: dict[str, tuple[str, str]] = {}
        for key, value in decoded.items():
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 2
                or not all(isinstance(part, str) and part for part in value)
            ):
                return None
            table[key] = (value[0], value[1])
        return table.get(repo)
    return _DEFAULT_REPO_ROUTES.get(repo)


#: The decomposition assignment for a pipeline task the fleet returned twice
#: without a technical cause (VOYN-W0-AICC-PLANNER-AUTO-SPLIT-PIPELINE-TASKS).
#: The run makes NO code change: its whole result is the trailer that
#: `backlog_ingest_results` turns into subtasks via `backlog_split_task`.
_SPLIT_INSTRUCTIONS = (
    "This task was returned twice by executors without a technical cause: it is "
    "too large for one run. Do NOT implement it and do NOT commit anything. "
    "Decompose it into 2 to 8 bounded subtasks, each closable by one run with one "
    "pull request and its own acceptance criteria and tests, ordered so that "
    "producers come before consumers. End your final message with a single line of "
    "exactly this form (one line, valid JSON, no code fences):\n"
    'SPLIT_TASKS_JSON: [{"suffix": "S1-<SHORT-NAME>", "title": "<imperative title>", '
    '"body": "<scope, acceptance, tests, files>", "priority": "P1"}, ...]\n'
    "Suffixes are uppercase [A-Z0-9-], unique, 2-40 chars; the orchestrator creates "
    "<this task id>-<suffix> for each entry and closes this task as SPLIT."
)


# --- the pre-dispatch reuse gate (VOYN-W0-AICC-DISPATCH-REUSE-GATE) --------
#
# Live case, 2026-09-06: a `-REM` task's run re-implemented
# `checkpoint_dirty_task_workspace`, a function PR 624 had already merged to
# main. The colliding signatures made the merged result fail CI with
# TypeErrors -- a whole run, a whole review and a red main, spent producing
# something that already existed. The planner could not have known: its
# candidate query asks what is ELIGIBLE, never what is already DONE ON MAIN.
#
# The gate below is that missing question, asked once per remediation
# candidate, immediately before dispatch. It reads the default branch of the
# control host's own checkout and closes the task as superseded (with the
# merged pull request and sha as evidence, `backlog_close_superseded`, 0025)
# only when it can point at the merged authority. Everything else -- a repo
# it cannot read, a stale checkout, an ambiguous body, a commit with no pull
# request number -- abstains and dispatches exactly as before: the gate may
# only ever prevent provable duplicates, never withhold real work on a guess.

#: Suffixes for "another attempt at an earlier task": `-REM` is what
#: `review_merge._remediate_rejection` appends, `-RETRY` is the backlog
#: file's own convention for a hand-written follow-up.
_REMEDIATION_SUFFIXES = ("-REM", "-RETRY")

#: Symbols an acceptance criterion names, in backticks: lower snake_case with
#: at least one underscore (`checkpoint_dirty_task_workspace`), which is
#: specific enough to be a claim about the code rather than an English word.
_ACCEPTANCE_SYMBOL = re.compile(r"`([a-z_][a-z0-9_]*_[a-z0-9_]+)`")

#: More named symbols than this and the body is describing a landscape, not a
#: deliverable: the gate abstains rather than guess which ones it must find.
_MAX_ACCEPTANCE_SYMBOLS = 5

#: `Title of the change (#624)` -- the squash-merge subject GitHub writes.
#: The number is what makes a commit citable as the merged PULL REQUEST the
#: acceptance criterion asks for, so a subject without one is not evidence.
_MERGED_PR_NUMBER = re.compile(r"\(#(\d+)\)")

_ORIGIN_OWNER_REPO = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)(?P<owner>[^/]+)/(?P<repo>[^/.]+)"
)

#: The refs a checkout may know the default branch by, most authoritative
#: first. A checkout that knows none of them is not readable evidence.
_DEFAULT_BRANCH_REFS = (
    "refs/remotes/origin/HEAD",
    "refs/remotes/origin/main",
    "refs/heads/main",
)

#: History reads walk the whole log; they get their own budget rather than
#: `git_info.run_git_command`'s 5 s status-read default.
_HISTORY_TIMEOUT = 30


@dataclass(frozen=True, slots=True)
class MergedAuthority:
    """Evidence, read off the default branch, that a parent task's acceptance
    is already satisfied there — and therefore that dispatching its
    remediation would re-implement merged work."""

    parent_task_id: str
    #: The merge commit on the default branch that carries the authority.
    commit: str
    subject: str
    pr_url: str
    #: Which signal found it: `commit_title` or `symbols`.
    signal: str
    #: The symbols the parent named, when the signal is `symbols`.
    symbols: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """One line for the audit, the report and the operator."""
        found = f" [{', '.join(self.symbols)}]" if self.symbols else ""
        return (
            f"{self.signal}: {self.parent_task_id} is already on main as "
            f"{self.commit[:12]} ({self.subject}){found}"
        )


def remediation_parent(task_id: str, linked_parent: str | None = None) -> str | None:
    """The task this one is another attempt at, or None if it is not one.

    The recorded lineage (`backlog_task_remediation`, written only by
    `backlog_record_remediation`) wins when it exists; the suffix convention
    covers the follow-ups that were written into the backlog file by hand and
    so have no lineage row. `backlog_close_superseded` re-derives exactly
    this relationship from the same two sources, so the planner cannot talk
    the store into closing a task the store does not agree is a remediation.
    """
    if linked_parent:
        return linked_parent
    for suffix in _REMEDIATION_SUFFIXES:
        if task_id.endswith(suffix) and len(task_id) > len(suffix):
            return task_id[: -len(suffix)]
    return None


def acceptance_symbols(title: str, body: str) -> tuple[str, ...]:
    """The symbols a task's acceptance criteria name, in first-seen order.

    Empty when the task names none, or names so many (`> _MAX_ACCEPTANCE_
    SYMBOLS`) that "all of them are present" stops being a statement about
    this task's deliverable — both cases make the symbol signal abstain.
    """
    seen: list[str] = []
    for match in _ACCEPTANCE_SYMBOL.finditer(f"{title}\n{body}"):
        symbol = match.group(1)
        if symbol not in seen:
            seen.append(symbol)
    if not seen or len(seen) > _MAX_ACCEPTANCE_SYMBOLS:
        return ()
    return tuple(seen)


def _git(repo: Path, args: list[str], timeout: int = 5) -> str | None:
    """Read-only git through the one shared subprocess primitive; None when
    the command failed, timed out, or the directory is not a repository."""
    proc = git_info.run_git_command(repo, args, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout or ""


def repository_name(repo: Path) -> str | None:
    """The `origin` repository's own name, e.g. `ai-command-center`."""
    url = _git(repo, ["remote", "get-url", "origin"])
    if not url:
        return None
    match = _ORIGIN_OWNER_REPO.match(url.strip())
    return match.group("repo") if match else None


def _origin_pull_url(repo: Path, number: str) -> str | None:
    url = _git(repo, ["remote", "get-url", "origin"])
    if not url:
        return None
    match = _ORIGIN_OWNER_REPO.match(url.strip())
    if not match:
        return None
    return f"https://github.com/{match.group('owner')}/{match.group('repo')}/pull/{number}"


def default_branch_ref(repo: Path) -> str | None:
    """The ref this checkout knows the default branch by, or None.

    Deliberately local-only: fetching is the self-deploy unit's job, and a
    stale checkout must make the gate abstain (it cannot see the merge yet),
    never dispatch something it would have caught with fresher refs.
    """
    for ref in _DEFAULT_BRANCH_REFS:
        if _git(repo, ["rev-parse", "--verify", "--quiet", ref]):
            return ref
    return None


def _task_id_in_subject(subject: str, task_id: str) -> bool:
    """`VOYN-X` is in `VOYN-X: fix` but NOT in `VOYN-X-REM: fix` — the
    remediation's own merge says nothing about its parent."""
    return re.search(f"{re.escape(task_id)}(?![A-Za-z0-9._-])", subject) is not None


def _log_entries(
    repo: Path, ref: str, extra: list[str], limit: int
) -> list[tuple[str, str, int]]:
    """(sha, subject, commit time) for `git log ref <extra>`, newest first."""
    out = _git(
        repo,
        ["log", ref, f"--max-count={limit}", "--format=%H%x1f%ct%x1f%s", *extra],
        timeout=_HISTORY_TIMEOUT,
    )
    entries: list[tuple[str, str, int]] = []
    for line in (out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        entries.append((parts[0], parts[2], int(parts[1])))
    return entries


def _merged_by_commit_title(
    repo: Path, ref: str, parent_task_id: str
) -> MergedAuthority | None:
    """Signal one: a merged pull request naming the parent task in its title."""
    for sha, subject, _when in _log_entries(
        repo, ref, ["--fixed-strings", f"--grep={parent_task_id}"], 20
    ):
        if not _task_id_in_subject(subject, parent_task_id):
            continue
        number = _MERGED_PR_NUMBER.search(subject)
        if number is None:
            continue
        pr_url = _origin_pull_url(repo, number.group(1))
        if pr_url is None:
            continue
        return MergedAuthority(
            parent_task_id=parent_task_id,
            commit=sha,
            subject=subject,
            pr_url=pr_url,
            signal="commit_title",
        )
    return None


def _symbol_is_defined(repo: Path, ref: str, symbol: str) -> bool:
    """A DEFINITION of `symbol` on `ref` — not a mention of it in prose."""
    pattern = (
        r"^[[:space:]]*(async def |def |class |CREATE (OR REPLACE )?FUNCTION )"
        f"{symbol}[ (:]"
    )
    return _git(repo, ["grep", "-l", "-E", pattern, ref], timeout=_HISTORY_TIMEOUT) is not None


def _symbol_introduced(
    repo: Path, ref: str, symbol: str
) -> tuple[str, str, int] | None:
    """The OLDEST commit on `ref` that changed the symbol's definition count —
    i.e. the one that put it there."""
    entries = _log_entries(
        repo, ref, [f"-S(def|class|FUNCTION) {symbol}", "--pickaxe-regex"], 20
    )
    return entries[-1] if entries else None


def _merged_by_symbols(
    repo: Path, ref: str, parent_task_id: str, symbols: tuple[str, ...], since: int
) -> MergedAuthority | None:
    """Signal two: every symbol the parent's acceptance names is defined on
    the default branch, put there AFTER the parent task was written.

    The `since` bound is what keeps this from closing a task for naming
    something that always existed: a symbol older than the task cannot be
    evidence that the task's own work landed.
    """
    if not symbols:
        return None
    newest: tuple[str, str, int] | None = None
    for symbol in symbols:
        if not _symbol_is_defined(repo, ref, symbol):
            return None
        introduced = _symbol_introduced(repo, ref, symbol)
        if introduced is None or introduced[2] <= since:
            continue
        if newest is None or introduced[2] > newest[2]:
            newest = introduced
    if newest is None:
        return None
    sha, subject, _when = newest
    number = _MERGED_PR_NUMBER.search(subject)
    if number is None:
        return None
    pr_url = _origin_pull_url(repo, number.group(1))
    if pr_url is None:
        return None
    return MergedAuthority(
        parent_task_id=parent_task_id,
        commit=sha,
        subject=subject,
        pr_url=pr_url,
        signal="symbols",
        symbols=symbols,
    )


def merged_authority(
    repo: Path,
    ref: str,
    parent_task_id: str,
    *,
    title: str,
    body: str,
    created_at: int,
) -> MergedAuthority | None:
    """Both signals, cheapest first; None means "no proof — dispatch"."""
    found = _merged_by_commit_title(repo, ref, parent_task_id)
    if found is not None:
        return found
    return _merged_by_symbols(
        repo, ref, parent_task_id, acceptance_symbols(title, body), created_at
    )


@dataclass(frozen=True, slots=True)
class _ReuseGate:
    """One tick's answer to "can this host judge what is already on main?"."""

    checkout: Path
    ref: str
    repository: str
    #: task_id -> recorded parent, for every candidate that has lineage.
    linked: dict[str, str]


def _repo_hint_name(repo_hint: str) -> str:
    """`~/Projects/ai-command-center` and `ai-command-center` are the same
    repository to the backlog; both must compare equal to `origin`'s name."""
    return (repo_hint or "").rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _monitor_task_id(source: str, failure: str) -> str:
    """Deterministic, exact task id for a monitor finding: the same
    (source, failure) always maps to the same id, so re-opening a finding
    re-uses the task instead of creating a twin. The readable slug is
    followed by a digest of the UNMODIFIED pair: two findings that differ
    only in punctuation, case, or past the slug's length must not share an
    id (review of fc167cf7) -- the second upsert would have collided and
    that finding would have stayed unlinked forever."""
    slug = re.sub(r"[^A-Z0-9]+", "-", f"{source}-{failure}".upper()).strip("-")[:70]
    digest = hashlib.sha256(f"{source}\x00{failure}".encode("utf-8")).hexdigest()[:10].upper()
    return f"VOYN-MON-{slug}-{digest}"


def _split_requested(rows: Any, task_id: str) -> bool:
    """True when the task's latest granted return_to_pool asked for a split."""
    row = rows(
        "SELECT (e.detail ->> 'split_requested')::boolean "
        "FROM backlog_event e WHERE e.task_id = %s AND e.event = 'return_to_pool' "
        "AND e.outcome = 'granted' ORDER BY e.event_id DESC LIMIT 1",
        (task_id,),
    )
    return bool(row and row[0] and row[0][0])


def _payload_for(
    task: dict[str, Any], limits: PlanLimits, route: tuple[str, str], *, mode: str = "implement"
) -> tuple[dict[str, Any], int]:
    """The agent_run payload plus the attempt budget (= cascade length).

    ``mode="split"`` sends the decomposition assignment instead of the
    implementation contract; everything else (route, cascade, provenance) is
    identical, so a split run passes the same gates as any run.

    Prompt discipline: the task record IS the assignment — id, title, body
    travel verbatim; the worker's provenance gate still applies, and
    ``untrusted=False`` is on the authority of the planner being the control
    plane acting on the canonical store.
    """
    cascade = cascade_for("implementation")
    project_id, repository_path = route
    if mode == "split":
        prompt = (
            f"Central task: {task['task_id']} ({task['title']}).\n"
            f"Wave {task['wave']}, priority {task['priority'] or 'unset'}.\n\n"
            f"{task['body']}\n\n"
            f"{_SPLIT_INSTRUCTIONS}"
        ).strip()
        payload = {
            "kind": "agent_run",
            "v": AGENT_RUN_SCHEMA_VERSION,
            "project_id": project_id,
            "repository_path": repository_path,
            "prompt": prompt,
            "task_type": cascade[0]["task_type"],
            "timeout_seconds": limits.timeout_seconds,
            "untrusted": False,
            "cascade": cascade,
            "backlog_task_id": task["task_id"],
            "mode": "split",
        }
        return payload, len(cascade)
    prompt = (
        f"Central task: {task['task_id']} ({task['title']}).\n"
        f"Wave {task['wave']}, priority {task['priority'] or 'unset'}.\n\n"
        f"{task['body']}\n\n"
        # The publisher (`orchestrator.publish.publish_run`, under the writer
        # lease) refuses a task clone that is not clean, and an agent has no
        # push capability at all. Asking the agent to "open a pull request"
        # therefore asked for the one thing it cannot do, while never asking
        # for the one thing it must: the commit. Live consequence
        # (VOYN-W0-AICC-AGENT-COMMIT-CONTRACT-GAP): completed work sat
        # uncommitted in the clone, `agent_worktree_clean` refused it, and the
        # cascade spent every remaining attempt reproducing the same refusal.
        "Commit every change you make to the task branch in this clone before "
        "you finish -- `git add` and `git commit` are yours to run, and an "
        "uncommitted change is discarded work, not a result. Do NOT push and "
        "do NOT open a pull request: you have no push capability, and the "
        "orchestrator publishes your commits through the guarded publisher "
        "after you exit.\n"
        "End your final message with a line of exactly this form so the "
        "orchestrator can record the evidence:\n"
        "HEAD_SHA: <the branch head commit sha>"
    ).strip()
    payload = {
        "kind": "agent_run",
        "v": AGENT_RUN_SCHEMA_VERSION,
        "project_id": project_id,
        "repository_path": repository_path,
        "prompt": prompt,
        "task_type": cascade[0]["task_type"],
        "timeout_seconds": limits.timeout_seconds,
        "untrusted": False,
        "cascade": cascade,
        "backlog_task_id": task["task_id"],
    }
    return payload, len(cascade)


class Planner:
    """Owns nothing but the composition; every decision is the database's."""

    def __init__(
        self, connection_factory: Any, source_path: str | None = None
    ) -> None:
        self._factory = connection_factory
        #: The checkout the reuse gate reads the default branch from — this
        #: control host's own tree (`--repo-path`, the unit's WorkingDirectory
        #: by default). None disables the gate entirely, which is exactly the
        #: pre-gate behaviour: every candidate is dispatched.
        self._source_path = source_path

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def _row(self, sql: str, params: tuple[Any, ...]) -> tuple:
        return self._rows(sql, params)[0]

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def _reuse_gate(self, candidates: list[tuple]) -> _ReuseGate | None:
        """This tick's reuse-gate context, or None when the gate cannot run.

        Costs one query and two cheap git reads, and only when a remediation
        candidate is actually eligible: a tick with none does no git at all.
        """
        if not self._source_path or not candidates:
            return None
        task_ids = [row[0] for row in candidates]
        linked = {
            task_id: parent
            for task_id, parent in self._rows(
                "SELECT task_id, parent_task_id FROM backlog_task_remediation "
                "WHERE task_id = ANY(%s)",
                (task_ids,),
            )
        }
        if not any(remediation_parent(t, linked.get(t)) for t in task_ids):
            return None
        checkout = Path(self._source_path)
        ref = default_branch_ref(checkout)
        repository = repository_name(checkout)
        if ref is None or repository is None:
            return None
        return _ReuseGate(
            checkout=checkout, ref=ref, repository=repository, linked=linked
        )

    def _close_if_superseded(
        self, gate: _ReuseGate, task_id: str, repo: str
    ) -> tuple[bool, str]:
        """(closed, detail) for one candidate.

        `(True, evidence)` — the task is DONE, superseded by merged work.
        `(False, "")` — the gate abstained: not a remediation, a repository
        this host cannot read, or no proof on the default branch. Dispatch
        proceeds exactly as it did before the gate existed.
        `(False, reason)` — the evidence stood but the store refused the
        close, which is the one case the caller must report.
        """
        parent_id = remediation_parent(task_id, gate.linked.get(task_id))
        if parent_id is None:
            return False, ""
        # Only the repository this checkout IS may be judged from it. The
        # worker-host paths in `_DEFAULT_REPO_ROUTES` are not readable from
        # the control host (the planner unit runs with ProtectHome=true), so
        # a candidate for another repository abstains rather than being
        # judged against the wrong history.
        if _repo_hint_name(repo) != gate.repository:
            return False, ""
        parent = self._rows(
            "SELECT title, body, EXTRACT(EPOCH FROM created_at)::bigint "
            "FROM backlog_task WHERE task_id = %s",
            (parent_id,),
        )
        if not parent:
            return False, ""
        title, body, created_at = parent[0]
        authority = merged_authority(
            gate.checkout,
            gate.ref,
            parent_id,
            title=title or "",
            body=body or "",
            created_at=int(created_at or 0),
        )
        if authority is None:
            return False, ""
        ok, reason, _revision = self._row(
            "SELECT * FROM backlog_close_superseded(%s, %s, %s, %s, %s)",
            (
                task_id,
                parent_id,
                authority.pr_url,
                authority.commit,
                authority.summary,
            ),
        )
        if ok:
            return True, authority.summary
        return False, reason or "refused"

    def plan_once(self, limits: PlanLimits = PlanLimits()) -> PlanReport:
        report = PlanReport()
        ok, reason, *_ = self._row(
            "SELECT * FROM backlog_lease_acquire(%s, %s, %s)",
            (
                _PLANNER_AUTHORITY,
                limits.planner,
                max(limits.planner_lease_ttl_seconds, 60),
            ),
        )
        if not ok:
            report.planner_busy = True
            return report
        try:
            # Ingest finished work first (BO-S3): evidence + READY_TO_REVIEW
            # for the succeeded, return-to-pool (or park) for the dead, lanes
            # freed — so this very tick can refill them.
            for task_id, queue_state, action, _detail in self._rows(
                "SELECT * FROM backlog_ingest_results(%s)", (limits.planner,)
            ):
                report.ingested.append((task_id, action))

            # Reconcile technical DEFER_TO_USER parks back to OPEN (VOYN-W0-
            # AICC-DEFER-AUTO-RESUME) before selecting candidates, so a
            # resumed task is eligible in this very tick. The candidate query
            # mirrors the 0014 gate's own conditions purely as a FILTER --
            # so ineligible parks are not attempted (and not audit-spammed)
            # every tick; the SECURITY DEFINER function remains the only
            # authority and revalidates everything under the row lock.
            if limits.max_resumes_per_tick > 0:
                resumable = self._rows(
                    "SELECT t.task_id, park.reason FROM backlog_task t "
                    "CROSS JOIN LATERAL ("
                    "  SELECT e.reason, e.event_id FROM backlog_event e"
                    "   WHERE e.task_id = t.task_id"
                    "     AND e.event = 'return_to_pool'"
                    "     AND e.outcome = 'granted'"
                    "     AND e.detail->>'target' = 'DEFER_TO_USER'"
                    "   ORDER BY e.event_id DESC LIMIT 1"
                    ") park "
                    "WHERE t.status = 'DEFER_TO_USER' AND t.kind = 'task' "
                    "  AND park.reason LIKE 'cascade_exhausted:%%' "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM backlog_event e2"
                    "     WHERE e2.task_id = t.task_id"
                    "       AND e2.outcome = 'granted'"
                    "       AND e2.event IN ('upsert', 'transition', 'triage',"
                    "                        'dispatch', 'return_to_pool',"
                    "                        'resume_deferred')"
                    "       AND e2.event_id > park.event_id) "
                    # The anti-ping-pong bound is a sliding WINDOW, not a
                    # lifetime score: three granted resumes within the
                    # trailing 48h say "this park re-arms itself faster than
                    # automation can help — a human should look". A lifetime
                    # count buried tasks forever: parks from the dead-codex
                    # era (2026-09) exhausted their 3 and stayed DEFER even
                    # after the pipeline that parked them was fixed
                    # (VOYN-W0-AICC-DEFER-AUTO-RESUME-REM).
                    "  AND (SELECT count(*) FROM backlog_event e"
                    "        WHERE e.task_id = t.task_id"
                    "          AND e.event = 'resume_deferred'"
                    "          AND e.outcome = 'granted'"
                    "          AND e.created_at > now() - interval '48 hours'"
                    "       ) < 3 "
                    "ORDER BY t.priority NULLS LAST, t.wave, t.task_id "
                    "LIMIT %s",
                    (limits.max_resumes_per_tick,),
                )
                for task_id, park_reason in resumable:
                    ok, _reason, _revision = self._row(
                        "SELECT * FROM backlog_resume_deferred(%s)", (task_id,)
                    )
                    if ok:
                        report.resumed.append((task_id, park_reason))

            # Fail-closed monitors record what they measured (0021
            # monitor_finding); every open finding becomes a pipeline task
            # once, so a red monitor is a task the fleet fixes rather than a
            # permanently failed unit (owner instruction 2026-09-08).
            for finding_id, source, failure in self._rows(
                "SELECT finding_id, source, failure FROM monitor_finding "
                "WHERE state = 'open' AND task_id IS NULL ORDER BY finding_id LIMIT 20",
                (),
            ):
                task_id_for_finding = _monitor_task_id(source, failure)
                ok, reason, _changed, _rev = self._row(
                    "SELECT * FROM backlog_upsert_task(%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        task_id_for_finding, "0", "P1", "OPEN", "task",
                        f"monitor finding: {failure} on {source}",
                        f"Recorded by the fail-closed monitor '{source}' (monitor_finding "
                        f"#{finding_id}): failure '{failure}'. Find the root cause and fix "
                        "it so the measurement is healthy again; the monitor clears the "
                        "finding when it measures healthy. Acceptance: the monitor reports "
                        "ok for 24h and the root-cause fix is merged with a regression test.",
                        "ai-command-center",
                    ),
                )
                if ok or reason == "unchanged":
                    self._exec(
                        "SELECT backlog_set_task_class(%s, 'pipeline')",
                        (task_id_for_finding,),
                    )
                    self._exec(
                        "SELECT monitor_link_task(%s, %s)", (finding_id, task_id_for_finding)
                    )
                    report.monitor_tasks.append((task_id_for_finding, failure))

            # The review-backlog fence is DISPATCH-only backpressure: it
            # must never short-circuit anything above it in the tick. An
            # earlier version of this fence was a blanket `return report`
            # placed before the DEFER_TO_USER reconcile above, which meant a
            # full review backlog also silently froze parked-task recovery
            # for as long as PRs sat unreviewed -- a control whose blast
            # radius (the whole rest of the tick) was wider than its stated
            # purpose (gate new dispatch). Ingest and the resume reconcile
            # both run unconditionally above; only the candidate/dispatch
            # loop below is skipped when the fence fires.
            fence_active = False
            lanes_idle = False
            if limits.review_backlog_limit > 0:
                (review_backlog,) = self._row(
                    "SELECT count(DISTINCT t.task_id) FROM backlog_task t "
                    "JOIN backlog_evidence e "
                    "  ON e.task_id = t.task_id AND e.kind = 'pr' "
                    "WHERE t.status = 'READY_TO_REVIEW'",
                    (),
                )
                review_backlog = int(review_backlog)
                if review_backlog >= limits.review_backlog_limit:
                    report.review_window_full = review_backlog
                    fence_active = True
                    # Backpressure exists to keep review from drowning, not to
                    # idle the fleet: with nothing ready or claimed on the
                    # execution queue the fence holds nothing back this tick
                    # (observed 2026-09-08: four lanes idle for hours at
                    # review backlog 164 while accepted PRs waited on CI).
                    (live_items,) = self._row(
                        "SELECT count(*) FROM work_item_public "
                        "WHERE queue = 'execution' "
                        "  AND state IN ('ready', 'claimed')",
                        (),
                    )
                    lanes_idle = int(live_items) == 0
                    report.idle_trickle = lanes_idle

            candidates = self._rows(
                "SELECT task_id, wave, priority, title, body, repo, dispatchable, "
                "       task_class "
                "FROM backlog_eligible"
            )
            gate = self._reuse_gate(candidates)
            for (
                task_id, wave, priority, title, body, repo, dispatchable, task_class,
            ) in candidates:
                if len(report.dispatched) >= limits.max_dispatches_per_tick:
                    break
                # The reuse gate runs BEFORE the review-backlog fence on
                # purpose: closing a task whose work is already merged adds
                # nothing to the review queue, it removes work from the
                # fleet. Holding that behind backpressure would keep proven
                # duplicates eligible for exactly as long as review is busy,
                # which is when a wasted run costs the most.
                if gate is not None:
                    closed, detail = self._close_if_superseded(gate, task_id, repo)
                    if closed:
                        report.superseded.append((task_id, detail))
                        continue
                    if detail:
                        # The store refused the close although the evidence
                        # stands (a concurrent status change is the only way
                        # in: the planner re-derives the lineage the same way
                        # `backlog_close_superseded` does). Report it and
                        # leave the task OPEN for the next tick rather than
                        # dispatch a run this tick has proof is redundant.
                        report.refused.append((task_id, f"supersede_refused:{detail}"))
                        continue
                if fence_active and not lanes_idle and task_class != "pipeline":
                    report.fenced += 1
                    continue
                if fence_active and task_class == "pipeline":
                    report.pipeline_bypass.append(task_id)
                task = {
                    "task_id": task_id,
                    "wave": wave,
                    "priority": priority,
                    "title": title,
                    "body": body,
                    "repo": repo,
                }
                if not dispatchable:
                    report.undispatchable.append((task_id, "no_repo"))
                    continue
                route = repo_route(repo)
                if route is None:
                    # An unrouted repo is a report line, never a dispatch into
                    # a guaranteed dead-letter (the first live tick proved the
                    # worker refuses unknown projects three times, honestly).
                    report.undispatchable.append((task_id, "unknown_repo_route"))
                    continue
                mode = "split" if _split_requested(self._rows, task_id) else "implement"
                payload, budget = _payload_for(task, limits, route, mode=mode)
                if mode == "split":
                    report.split_dispatched.append(task_id)
                ok, reason, work_item_id, _revision = self._row(
                    "SELECT * FROM backlog_dispatch(%s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        task_id,
                        limits.planner,
                        limits.lease_ttl_seconds,
                        limits.wip_limit,
                        json.dumps(payload),
                        budget,
                    ),
                )
                if ok:
                    report.dispatched.append((task_id, work_item_id))
                elif reason == "earlier_wave_has_eligible_work":
                    report.skipped_by_wave_gate.append((task_id, reason))
                elif reason == "wip_exhausted":
                    report.refused.append((task_id, reason))
                    break  # the cap is global; later candidates cannot pass
                else:
                    report.refused.append((task_id, reason))
        finally:
            # The global lease is per TICK; repo leases outlive it by design.
            self._row(
                "SELECT * FROM backlog_lease_release(%s, %s)",
                (_PLANNER_AUTHORITY, limits.planner),
            )
        return report


def plan_once(
    connection_factory: Any,
    limits: PlanLimits = PlanLimits(),
    *,
    source_path: str | None = None,
) -> PlanReport:
    """One tick. `source_path` is this host's own checkout, which the reuse
    gate reads the default branch from; omitting it leaves the gate off."""
    return Planner(connection_factory, source_path).plan_once(limits)
