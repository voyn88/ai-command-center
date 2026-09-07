"""Deterministic scheduling & supervision-decision layer.

This module is the *decision* layer that sits directly above
`supervisor.Supervisor` (the process-lifecycle owner) and beside
`execution_queue` (dependency readiness). It answers exactly one question,
purely and deterministically:

    Given the work that wants to run, the agents that could run it, and the
    load already in flight — what should be assigned, deferred, or blocked
    right now, and *why*?

It embodies the same invariant `execution_queue` states in its own module
docstring: **there is no hidden scheduler**. Nothing here launches a process,
mutates a `run` row, spawns a thread, or touches a subprocess. `plan()` reads
immutable inputs and returns an immutable, fully serializable `SchedulingPlan`
— an ordered list of `SchedulingDecision`s, each carrying a machine-readable
`reason_code` and a human-readable `explanation` (the "scheduling decisions
must be explainable" invariant). The application layer decides whether to act
on an `ASSIGN` decision by calling the existing explicit launch path
(`ExecutionCenterAPI.start_run` -> `Supervisor.start_raw`); this module never
does that itself.

Why a pure planner rather than folding this into `Supervisor`:

- **No launcher/handshake regression risk.** `Supervisor` owns the frozen
  Sprint-1 process contract (Popen flags, the `process_started` vs
  `handshake_received` separation, timeout/cancel signal handling,
  reconciliation). None of that is imported, subclassed, or altered here.
- **Deterministic and testable.** Every decision is a pure function of the
  inputs plus an explicitly-passed `now` timestamp — no wall clock read
  inside, no randomness (the backoff is deliberately jitter-free; see
  `RetryPolicy`). The same inputs always produce byte-identical decisions,
  which is exactly what "deterministic scheduling decisions" and "complete
  audit trail" require.
- **Fail-safe on malformed state.** A work item missing the fields needed to
  schedule it safely resolves to `BLOCKED`/`malformed`, never to `ASSIGN` —
  malformed runtime state can never cause an execution.

The three actions:

- `ASSIGN`  — schedule this task now, on the named agent, as an explicit new
  attempt (`decision.attempt`). Emitted at most once per task per plan, and
  only after every capacity/exclusivity check below passes.
- `DEFER`   — the task is eligible in principle but a *transient* condition
  (backoff not elapsed, capacity full, workspace busy, agent temporarily
  unavailable, dependency not yet met) prevents scheduling it this tick. The
  caller is expected to re-plan later; the condition is expected to clear.
- `BLOCKED` — the task cannot be scheduled as-is and re-planning alone will
  not change that (malformed, no capable agent exists, the prior failure was
  terminal, or the retry budget is exhausted). A human/config change is
  required.

Duplicate-execution safety (`no duplicate execution of the same task
attempt`): a plan emits at most one `ASSIGN` per `task_id`, and workspace
exclusivity is enforced both against the live `LoadSnapshot.busy_workspaces`
and against workspaces already consumed earlier *within the same plan*, so two
items targeting one workspace can never both be assigned.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from command_center import agent_runner, executors, models
from command_center.runtime import db
from command_center.runtime import providers as runtime_providers

# --------------------------------------------------------------------------
# Actions & reason codes (the vocabulary of an explainable decision)
# --------------------------------------------------------------------------

ACTION_ASSIGN = "ASSIGN"
ACTION_DEFER = "DEFER"
ACTION_BLOCKED = "BLOCKED"

# ASSIGN
REASON_ASSIGNED = "assigned"
# DEFER (transient — re-plan later, condition is expected to clear)
REASON_WAITING_DEPENDENCY = "waiting_dependency"
REASON_BACKOFF = "backoff"
# A recoverable prior failure whose completion time is unknown/unparseable —
# the backoff deadline cannot be computed, so we fail safe (DEFER) rather than
# assign immediately or fabricate a next_eligible_at (finding F4).
REASON_RETRY_TIMING_UNKNOWN = "retry_timing_unknown"
# A second eligible item whose task_id was already ASSIGNED earlier in this same
# plan — enforces "at most one ASSIGN per task_id per plan" (finding F2).
REASON_DUPLICATE_TASK = "duplicate_task_assignment"
REASON_WORKSPACE_BUSY = "workspace_busy"
REASON_GLOBAL_AT_CAPACITY = "global_at_capacity"
REASON_AGENT_AT_CAPACITY = "agent_at_capacity"
REASON_AGENT_UNAVAILABLE = "agent_unavailable"
# BLOCKED (structural — re-planning alone will not help)
REASON_MALFORMED = "malformed"
REASON_INVALID_ATTEMPT_STATE = "invalid_attempt_state"
REASON_NO_CAPABLE_AGENT = "no_capable_agent"
REASON_TERMINAL_FAILURE = "terminal_failure"
REASON_RETRY_EXHAUSTED = "retry_exhausted"

# --------------------------------------------------------------------------
# Failure classification: recoverable vs terminal
# --------------------------------------------------------------------------

RECOVERABLE = "recoverable"
TERMINAL = "terminal"

# `Supervisor` records a terminal `run.state` plus (for FAILED runs) a
# `failure_reason` string. The classifier below maps those onto retry policy.
# `blocked:` (a permission/policy denial — see `runtime.outcome`) and an
# explicit human `CANCELLED` are terminal: retrying the identical attempt
# cannot change the outcome. A `timeout`, an `incomplete:` result (the agent
# produced no working-tree change), or a lost/unknown process (`INTERRUPTED`/
# `UNKNOWN`, the reconcilable process-loss / app-restart cases) are
# recoverable — a fresh attempt may well succeed.
_TERMINAL_STATES: frozenset[str] = frozenset({"CANCELLED"})
_TERMINAL_REASON_PREFIXES: tuple[str, ...] = ("blocked:",)

# Exceptions to `blocked:` being terminal.
#
# `blocked:` means the agent did not do the work, and most such refusals are
# genuinely terminal: `blocked:final_response` is the agent's own judgement
# that it cannot proceed, and repeating the identical attempt cannot change a
# judgement.
#
# A *permission* denial is different in kind. It records the state of the
# environment's tool policy at the moment of the run, not anything about the
# task — and that policy is exactly the thing an operator changes in response
# to seeing the failure. Treating it as terminal meant a task stayed
# permanently unrunnable after its permissions were fixed, with no way to
# retry short of editing run history. (Observed: eight runs blocked on
# `permission_denied:Glob,Read`, all still refused after the allow rule was
# added.)
#
# Recoverable does not mean unbounded: the retry budget and backoff still
# apply, so a genuinely mis-permissioned task exhausts its attempts and stops.
_RECOVERABLE_REASON_PREFIXES: tuple[str, ...] = ("blocked:permission_denied",)

# Prior run states that mean "an attempt was made and did NOT succeed", so the
# retry policy (budget + backoff + terminal classification) must apply whenever
# `attempts_made > 0`. This gate is deliberately **state-driven, never
# failure_reason-driven**: the runtime legitimately records FAILED (nonzero
# exit), INTERRUPTED and UNKNOWN (both produced by `Supervisor.reconcile()`)
# with `failure_reason=None`, and every one of those must still be gated
# (finding F1). COMPLETED is excluded — a success is not a retry. `classify_
# failure` (which *does* read `failure_reason`) only refines the outcome into
# RECOVERABLE vs TERMINAL once we are already inside the gate; it never decides
# whether the gate applies.
_RETRY_GATED_STATES: frozenset[str] = frozenset({"FAILED", "INTERRUPTED", "UNKNOWN", "CANCELLED"})


def classify_failure(*, state: str | None, failure_reason: str | None) -> str:
    """Classify a prior attempt's terminal outcome as `RECOVERABLE` or
    `TERMINAL` for retry purposes. Conservative and total: any unrecognized
    input classifies as `RECOVERABLE` (retry is bounded by
    `RetryPolicy.max_attempts` regardless, so an over-eager `RECOVERABLE` can
    at worst spend the retry budget — whereas an over-eager `TERMINAL` would
    silently strand recoverable work)."""
    if state in _TERMINAL_STATES:
        return TERMINAL
    reason = (failure_reason or "").strip()
    # The recoverable exception is checked first: it is a narrower match than
    # the `blocked:` prefix it lives under, and order is what makes it an
    # exception rather than dead code.
    if any(reason.startswith(prefix) for prefix in _RECOVERABLE_REASON_PREFIXES):
        return RECOVERABLE
    if any(reason.startswith(prefix) for prefix in _TERMINAL_REASON_PREFIXES):
        return TERMINAL
    return RECOVERABLE


# --------------------------------------------------------------------------
# Capabilities & the agent registry
# --------------------------------------------------------------------------

# A general-purpose agent advertises this sentinel and matches any requirement
# set — it is not the same as "an empty requirement set", which any agent
# trivially satisfies.
CAP_ANY = "*"


def capabilities_for_task_type(task_type: str) -> frozenset[str]:
    """The default capability set a `task_type` *requires* of an agent, derived
    from the same vocabulary the real executor uses: the task type itself plus
    its resolved execution profile (`read_only` vs `trusted_development`, via
    `agent_runner.profile_for_task_type`). A capability-restricted agent (e.g.
    a review-only agent that lacks `profile:trusted_development`) will
    correctly fail to match an implementation task built with this helper."""
    profile = agent_runner.profile_for_task_type(task_type)
    return frozenset({f"task_type:{task_type}", f"profile:{profile}"})


def capabilities_for_executor(executor_id: str) -> frozenset[str]:
    """Capabilities the real provider behind an executor can actually honor.

    Most CLI providers can run both read-only and trusted-development profiles.
    Ollama is intentionally different: its provider has no editor or shell and
    rejects every task type outside ``READ_ONLY_TASK_TYPES``. Keeping that fact
    in the scheduler registry prevents a plan that is guaranteed to fail at
    launch time.
    """
    if executor_id == "ollama":
        return frozenset(
            {f"profile:{agent_runner.PROFILE_READ_ONLY}"}
            | {f"task_type:{task_type}" for task_type in agent_runner.READ_ONLY_TASK_TYPES}
        )
    return frozenset({CAP_ANY})


@dataclass(frozen=True)
class AgentSpec:
    """One registered execution agent — a capacity- and capability-bearing
    binding over an `executors.Executor`.

    `capabilities` holding `CAP_ANY` means "can run anything". `available`
    gates scheduling without deregistering (a temporarily-down agent yields
    `DEFER`/`agent_unavailable`, not `BLOCKED`). `weight` breaks ties between
    otherwise-equal candidate agents deterministically (higher first)."""

    agent_id: str
    executor_id: str
    capabilities: frozenset[str]
    max_concurrency: int
    available: bool = True
    weight: int = 0

    def can_run(self, required_capabilities: frozenset[str]) -> bool:
        if CAP_ANY in self.capabilities:
            return True
        return required_capabilities <= self.capabilities


class AgentRegistry:
    """A deterministic, mutable-at-registration registry of `AgentSpec`s.

    Registration order does not affect scheduling: every iteration over the
    registry is sorted by a stable key (`weight` desc, then `agent_id` asc),
    so two registries with the same agents added in different orders produce
    identical plans."""

    def __init__(self, agents: list[AgentSpec] | None = None) -> None:
        self._agents: dict[str, AgentSpec] = {}
        for agent in agents or []:
            self.register(agent)

    def register(self, spec: AgentSpec) -> None:
        if spec.max_concurrency < 0:
            raise ValueError(f"Agent {spec.agent_id!r} max_concurrency must be >= 0, got {spec.max_concurrency}")
        if spec.executor_id not in executors.EXECUTORS:
            raise ValueError(f"Agent {spec.agent_id!r} references unknown executor {spec.executor_id!r}")
        self._agents[spec.agent_id] = spec

    def deregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def get(self, agent_id: str) -> AgentSpec | None:
        return self._agents.get(agent_id)

    def all(self) -> list[AgentSpec]:
        return sorted(self._agents.values(), key=lambda a: (-a.weight, a.agent_id))

    def capable_agents(self, required_capabilities: frozenset[str]) -> list[AgentSpec]:
        return [a for a in self.all() if a.can_run(required_capabilities)]


def default_registry(*, max_concurrency: int = 2) -> AgentRegistry:
    """A registry seeded from `executors.EXECUTORS`: every executor that has a
    provider behind it becomes an agent whose id equals its executor id and
    whose advertised capabilities match the provider's real launch contract.

    Availability is carried on the `AgentSpec` rather than deciding membership,
    and that distinction is the whole point. `executor.available` is a *live
    capability probe* — it shells out to the provider CLI — so it can report
    False for a reason that has nothing to do with the executor existing: a
    machine under load missing the probe timeout, a local daemon restarting.
    Omitting such an executor makes the planner answer `no_capable_agent`,
    which is a **structural** verdict meaning "no agent can ever run this" and
    which a human is told to resolve by changing configuration. Registering it
    as present-but-unavailable yields `agent_unavailable` instead — transient,
    self-healing on the next tick, and true.

    An executor with no provider at all (`availability_check is None`) is a
    genuine structural absence and is still omitted."""
    agents = [
        AgentSpec(
            agent_id=executor.id,
            executor_id=executor.id,
            capabilities=capabilities_for_executor(executor.id),
            max_concurrency=max_concurrency,
            available=executor.available,
        )
        for executor in executors.EXECUTORS.values()
        if executor.availability_check is not None
    ]
    return AgentRegistry(agents)


# --------------------------------------------------------------------------
# Retry policy — deterministic, jitter-free exponential backoff
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Bounds automatic retries and computes the earliest eligible time for the
    next attempt after a recoverable failure.

    The backoff is deliberately **jitter-free**: the "deterministic scheduling
    decisions" invariant means the same inputs must always yield the same
    `next_eligible_at`. (Jitter belongs — if anywhere — to a real distributed
    scheduler with thundering-herd concerns; this single-host planner has
    none, and determinism is worth more here.)"""

    max_attempts: int = 3
    base_backoff_seconds: float = 30.0
    multiplier: float = 2.0
    max_backoff_seconds: float = 900.0

    def backoff_seconds(self, attempts_made: int) -> float:
        """Delay after `attempts_made` failed attempts (>= 1) before the next
        attempt becomes eligible. `30, 60, 120, ...` capped at
        `max_backoff_seconds`."""
        if attempts_made < 1:
            return 0.0
        raw = self.base_backoff_seconds * (self.multiplier ** (attempts_made - 1))
        return min(raw, self.max_backoff_seconds)


# --------------------------------------------------------------------------
# Inputs: immutable work items and the current load snapshot
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItem:
    """One candidate unit of work the planner may schedule.

    `attempts_made` is the count of attempts already *completed* for this task
    (0 = never run). An `ASSIGN` proposes the explicit next attempt number,
    `attempts_made + 1` (the "retries must create an explicit new attempt"
    invariant — the actual new `run` row, with its own incremented
    `sequence`, is created downstream by `create_run`, never by reusing a
    prior attempt).

    `last_state`/`last_failure_reason` describe the *previous* attempt's
    terminal outcome (from `runtime.db`) and drive `classify_failure` +
    backoff. `workspace` is the exclusivity key (a resolved repository/worktree
    path); it is normalized (`~` expanded, `..`/`.` collapsed) before every
    comparison, never resolved against the filesystem, so the planner stays
    pure."""

    task_id: str
    workspace: str
    required_capabilities: frozenset[str] = frozenset()
    priority: str = "Medium"
    # Project/task authorization boundary. ``None`` means the caller supplied
    # no allow-list; an explicit set restricts scheduling to exactly those
    # agents. This is deliberately separate from ``preferred_agent``: an
    # allow-list can contain Claude Code and Codex so the scheduler may choose
    # the less-loaded compatible provider without ever escaping project policy.
    allowed_agents: frozenset[str] | None = None
    preferred_agent: str | None = None
    dependencies_met: bool = True
    attempts_made: int = 0
    last_state: str | None = None
    last_failure_reason: str | None = None
    last_completed_at: str | None = None
    enqueued_at: str | None = None
    sla_seconds: float | None = None

    def is_wellformed(self) -> bool:
        return bool(self.task_id) and bool(self.workspace) and self.attempts_made >= 0


@dataclass(frozen=True)
class LoadSnapshot:
    """The execution load already in flight, against which capacity and
    task/workspace exclusivity are checked. Immutable; build one per planning
    tick (see `build_load_snapshot` for the read-only DB projection)."""

    running_by_agent: Mapping[str, int] = field(default_factory=dict)
    busy_workspaces: frozenset[str] = frozenset()
    active_task_ids: frozenset[str] = frozenset()
    global_running: int = 0


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerConfig:
    max_global_concurrency: int = 3


# --------------------------------------------------------------------------
# Output: decisions and the plan
# --------------------------------------------------------------------------

_PRIORITY_RANK: dict[str, int] = {name: rank for rank, name in enumerate(models.TASK_PRIORITIES)}


def _priority_rank(priority: str | None) -> int:
    """Higher = more urgent. An unknown/None priority ranks as the lowest known
    priority rather than raising — malformed priority never crashes a plan."""
    return _PRIORITY_RANK.get(priority or "", 0)


def _priority_recognized(priority: str | None) -> bool:
    """Whether `priority` is a known `models.TASK_PRIORITIES` value (vs. having
    been normalized to the lowest rank by `_priority_rank`) — surfaced per
    decision so the normalization is auditable, not silent (finding F6)."""
    return priority in _PRIORITY_RANK


def _normalize_workspace(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path))


def _to_epoch(iso: str | None) -> float | None:
    """Epoch seconds for an `enqueued_at`/`last_completed_at`/`now` string —
    all `models.iso_now()`-sourced naive UTC (`VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL`).

    A bare `.timestamp()` on a naive `datetime` assumes *local* time. `now`
    and every `enqueued_at`/`last_completed_at` this feeds are naive UTC on
    the same clock, so a fixed local misreading would cancel out of every
    subtraction (`_queued_seconds`, backoff) — except across this process's
    own DST fall-back, where the local offset used to interpret one naive
    value can differ from the offset used moments earlier for another,
    reintroducing the exact non-monotonic comparison this fix closes
    elsewhere: `_order_key`'s FIFO tiebreak and the SLA/backoff deadlines
    would again pick the wrong item for the span of the repeated hour.
    Attaching UTC before calling `.timestamp()` makes this an actual epoch."""
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class SchedulingDecision:
    """One explainable, serializable decision for one task — the atom of the
    audit trail."""

    task_id: str
    action: str
    reason_code: str
    explanation: str
    priority: str
    priority_rank: int
    decided_at: str
    # False when `priority` was not one of `models.TASK_PRIORITIES` and was
    # normalized to the lowest rank — surfaces the silent normalization in the
    # audit trail rather than hiding it (finding F6).
    priority_recognized: bool = True
    required_capabilities: frozenset[str] = frozenset()
    agent_id: str | None = None
    executor_id: str | None = None
    attempt: int | None = None
    failure_classification: str | None = None
    next_eligible_at: str | None = None
    queued_seconds: float | None = None
    sla_seconds: float | None = None
    sla_breached: bool | None = None
    sla_remaining_seconds: float | None = None

    def as_dict(self) -> dict:
        data = {
            "task_id": self.task_id,
            "action": self.action,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "priority": self.priority,
            "priority_rank": self.priority_rank,
            "priority_recognized": self.priority_recognized,
            "decided_at": self.decided_at,
            "required_capabilities": sorted(self.required_capabilities),
            "agent_id": self.agent_id,
            "executor_id": self.executor_id,
            "attempt": self.attempt,
            "failure_classification": self.failure_classification,
            "next_eligible_at": self.next_eligible_at,
            "queued_seconds": self.queued_seconds,
            "sla_seconds": self.sla_seconds,
            "sla_breached": self.sla_breached,
            "sla_remaining_seconds": self.sla_remaining_seconds,
        }
        return data


@dataclass(frozen=True)
class SchedulingPlan:
    """The complete, ordered, deterministic result of one `plan()` call.

    `decisions` is in the exact order the planner considered the work
    (priority-desc, SLA-breach-first, then FIFO by `enqueued_at`, then
    `task_id`), so it doubles as the scheduling audit trail: replaying the
    same inputs reproduces it byte-for-byte."""

    decided_at: str
    config: SchedulerConfig
    decisions: tuple[SchedulingDecision, ...]

    def assignments(self) -> list[SchedulingDecision]:
        return [d for d in self.decisions if d.action == ACTION_ASSIGN]

    def deferrals(self) -> list[SchedulingDecision]:
        return [d for d in self.decisions if d.action == ACTION_DEFER]

    def blocked(self) -> list[SchedulingDecision]:
        return [d for d in self.decisions if d.action == ACTION_BLOCKED]

    def audit_records(self) -> list[dict]:
        return [d.as_dict() for d in self.decisions]


def _order_key(item: WorkItem, now_epoch: float | None) -> tuple:
    """Deterministic scheduling order: highest priority first; within a
    priority, an SLA-breached item jumps ahead; then FIFO by `enqueued_at`;
    then `task_id`; final tiebreak the normalized `workspace`.

    The trailing `(task_id, workspace)` pair makes this a **total** order even
    for two items that share a `task_id` (the F2 duplicate case): they differ
    by workspace, so the canonical winner is fixed regardless of input order —
    reversing the input list yields the identical plan (byte-for-byte)."""
    breached = _sla_breached(item, now_epoch)
    enqueued = _to_epoch(item.enqueued_at)
    # `None` timestamps sort last within their group (treated as +inf).
    enqueued_key = enqueued if enqueued is not None else float("inf")
    return (
        -_priority_rank(item.priority),
        0 if breached else 1,
        enqueued_key,
        item.task_id,
        _normalize_workspace(item.workspace or ""),
    )


def _queued_seconds(item: WorkItem, now_epoch: float | None) -> float | None:
    enqueued = _to_epoch(item.enqueued_at)
    if enqueued is None or now_epoch is None:
        return None
    return max(0.0, now_epoch - enqueued)


def _sla_breached(item: WorkItem, now_epoch: float | None) -> bool:
    if item.sla_seconds is None:
        return False
    queued = _queued_seconds(item, now_epoch)
    if queued is None:
        return False
    return queued > item.sla_seconds


def _sla_remaining(item: WorkItem, now_epoch: float | None) -> float | None:
    if item.sla_seconds is None:
        return None
    queued = _queued_seconds(item, now_epoch)
    if queued is None:
        return None
    return item.sla_seconds - queued


def _iso_add(iso: str | None, seconds: float) -> str | None:
    """`iso` plus `seconds`, rendered the way `models.iso_now()` renders:
    naive UTC. `datetime.fromtimestamp` without a zone renders local wall
    clock, which would hand `next_eligible_at` back in a different
    convention than every other timestamp this plan reports."""
    epoch = _to_epoch(iso)
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch + seconds, tz=timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def plan(
    work_items: list[WorkItem],
    *,
    registry: AgentRegistry,
    load: LoadSnapshot | None = None,
    config: SchedulerConfig | None = None,
    policy: RetryPolicy | None = None,
    now: str | None = None,
) -> SchedulingPlan:
    """Produce a deterministic, explainable `SchedulingPlan` from immutable
    inputs. Never launches and never mutates anything.

    For a fully deterministic result the caller **must pass `now`** (an ISO
    timestamp): when `now` is omitted this falls back to `models.iso_now()`
    (the wall clock) purely as a convenience for callers that genuinely want
    "as of this instant" — that fallback is the only wall-clock read in this
    module, and supplying `now` avoids it entirely (finding F5).

    Capacity is consumed greedily in the deterministic order defined by
    `_order_key`, so a higher-priority item always claims scarce global/agent
    capacity ahead of a lower-priority one, and the result is reproducible."""
    config = config or SchedulerConfig()
    policy = policy or RetryPolicy()
    load = load or LoadSnapshot()
    now = now or models.iso_now()
    now_epoch = _to_epoch(now)

    ordered = sorted(work_items, key=lambda it: _order_key(it, now_epoch))

    # Mutable, plan-local capacity ledgers — start from the live snapshot and
    # are drawn down as ASSIGN decisions are made, so later (lower-priority)
    # items see the capacity earlier items have already claimed. Snapshot
    # counts are clamped to >= 0 (finding F3): a corrupted/negative count must
    # never *increase* available capacity below.
    global_used = max(0, load.global_running)
    agent_used: dict[str, int] = {
        a.agent_id: max(0, load.running_by_agent.get(a.agent_id, 0)) for a in registry.all()
    }
    consumed_workspaces: set[str] = {_normalize_workspace(w) for w in load.busy_workspaces}
    scheduled_tasks: set[str] = set(load.active_task_ids)

    decisions: list[SchedulingDecision] = []

    def _base(item: WorkItem) -> dict:
        return {
            "task_id": item.task_id,
            "priority": item.priority,
            "priority_rank": _priority_rank(item.priority),
            "priority_recognized": _priority_recognized(item.priority),
            "decided_at": now,
            "required_capabilities": item.required_capabilities,
            "queued_seconds": _queued_seconds(item, now_epoch),
            "sla_seconds": item.sla_seconds,
            "sla_breached": _sla_breached(item, now_epoch) if item.sla_seconds is not None else None,
            "sla_remaining_seconds": _sla_remaining(item, now_epoch),
        }

    for item in ordered:
        base = _base(item)

        # 0) Malformed — fail safe. Never assign against unusable state.
        if not item.is_wellformed():
            decisions.append(
                SchedulingDecision(
                    action=ACTION_BLOCKED,
                    reason_code=REASON_MALFORMED,
                    explanation="work item is missing a task_id or workspace, or has a negative attempt count",
                    **base,
                )
            )
            continue

        # 0b) Duplicate task_id — at most one active or newly-assigned attempt
        #     per task. `scheduled_tasks` begins with the task IDs in the live
        #     load snapshot, then gains only task IDs actually ASSIGNED in this
        #     canonically-ordered pass. An earlier candidate that merely
        #     DEFERRED/BLOCKED does not consume its task ID.
        if item.task_id in scheduled_tasks:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_DEFER,
                    reason_code=REASON_DUPLICATE_TASK,
                    explanation=(
                        f"task_id {item.task_id!r} already has an active or newly-assigned attempt; "
                        "at most one concurrent attempt per task_id is allowed"
                    ),
                    **base,
                )
            )
            continue

        workspace = _normalize_workspace(item.workspace)

        # 1) A completed-attempt count without a recognized prior state is
        #    corrupt/incomplete history. Never treat it as a fresh launch:
        #    doing so bypasses retry budget and can create an unbounded attempt.
        if item.attempts_made > 0 and item.last_state not in {
            "COMPLETED",
            *_RETRY_GATED_STATES,
        }:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_BLOCKED,
                    reason_code=REASON_INVALID_ATTEMPT_STATE,
                    explanation=(
                        f"task reports {item.attempts_made} completed attempt(s) but has "
                        f"unrecognized prior state {item.last_state!r}; refusing to bypass retry policy"
                    ),
                    **base,
                )
            )
            continue

        # 2) Dependencies not satisfied — transient, will clear.
        if not item.dependencies_met:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_DEFER,
                    reason_code=REASON_WAITING_DEPENDENCY,
                    explanation="task dependencies are not yet satisfied",
                    **base,
                )
            )
            continue

        # 3) Retry gating — STATE-driven, never failure_reason-driven (finding
        #    F1). Any prior non-success terminal state with a completed attempt
        #    (FAILED / INTERRUPTED / UNKNOWN / CANCELLED, incl. reconcile output
        #    that carries no failure_reason) enters the retry policy.
        #    `classify_failure` — which *does* read failure_reason — only
        #    refines the gated outcome into TERMINAL vs RECOVERABLE; it never
        #    decides whether the gate applies. COMPLETED is not in
        #    `_RETRY_GATED_STATES`, so a successful prior attempt is never
        #    gated (it falls through to normal assignment, e.g. a resume).
        if item.attempts_made > 0 and item.last_state in _RETRY_GATED_STATES:
            classification = classify_failure(state=item.last_state, failure_reason=item.last_failure_reason)
            if classification == TERMINAL:
                decisions.append(
                    SchedulingDecision(
                        action=ACTION_BLOCKED,
                        reason_code=REASON_TERMINAL_FAILURE,
                        explanation=(
                            f"previous attempt ended terminally "
                            f"(state={item.last_state!r}, reason={item.last_failure_reason!r}); "
                            "retrying the identical attempt cannot change the outcome"
                        ),
                        failure_classification=TERMINAL,
                        **base,
                    )
                )
                continue
            # RECOVERABLE from here — budget first, then backoff timing.
            #
            # Provider-unavailable failures (expired OAuth, exhausted quota,
            # 5xx outage, startup crash, …) are *not* a signal about the task —
            # they mean this executor cannot run it right now. `task_sync` has
            # already recorded the dead provider in `failed_executors` and the
            # next launch's `select_available_executor` will pick a live one, so
            # the prior attempt carries no information about the next executor.
            # Such an attempt must NOT consume the retry budget (otherwise a
            # `max_run_attempts=1` config strands the task before failover can
            # happen) and needs no backoff (a different agent is tried, not the
            # same one). Fall through to normal assignment. Bounded by
            # `failed_executors`: once every allowed executor has failed, the
            # launch layer strands the task at LAUNCH_SKIP_NO_AVAILABLE_EXECUTOR.
            provider_unavailable = item.last_failure_reason in runtime_providers.PROVIDER_UNAVAILABLE_REASONS
            if not provider_unavailable and item.attempts_made >= policy.max_attempts:
                decisions.append(
                    SchedulingDecision(
                        action=ACTION_BLOCKED,
                        reason_code=REASON_RETRY_EXHAUSTED,
                        explanation=(
                            f"recoverable failure but retry budget exhausted "
                            f"({item.attempts_made}/{policy.max_attempts} attempts made)"
                        ),
                        failure_classification=RECOVERABLE,
                        **base,
                    )
                )
                continue
            if not provider_unavailable:
                backoff = policy.backoff_seconds(item.attempts_made)
                last_epoch = _to_epoch(item.last_completed_at)
                if last_epoch is None or now_epoch is None:
                    # F4: recoverable, but the backoff deadline cannot be computed
                    # (missing/unparseable last_completed_at, or unparseable now).
                    # Fail safe — DEFER; never assign immediately, never fabricate
                    # a next_eligible_at.
                    decisions.append(
                        SchedulingDecision(
                            action=ACTION_DEFER,
                            reason_code=REASON_RETRY_TIMING_UNKNOWN,
                            explanation=(
                                f"recoverable failure after attempt {item.attempts_made}, but the completion "
                                "time is unknown so the backoff deadline cannot be computed; deferring rather "
                                "than retrying immediately"
                            ),
                            failure_classification=RECOVERABLE,
                            next_eligible_at=None,
                            **base,
                        )
                    )
                    continue
                if now_epoch < last_epoch + backoff:
                    decisions.append(
                        SchedulingDecision(
                            action=ACTION_DEFER,
                            reason_code=REASON_BACKOFF,
                            explanation=(
                                f"recoverable failure; backing off {backoff:.0f}s after attempt "
                                f"{item.attempts_made} before the next attempt"
                            ),
                            failure_classification=RECOVERABLE,
                            next_eligible_at=_iso_add(item.last_completed_at, backoff),
                            **base,
                        )
                    )
                    continue
            # else: backoff already elapsed — fall through to normal assignment
            # as a fresh attempt (attempt number attempts_made + 1). For a
            # provider-unavailable failure, fall through immediately (no budget,
            # no backoff) so the next available executor is tried right away.

        # 4) Capability match — structural. No capable agent is BLOCKED.
        if item.preferred_agent is not None:
            preferred = registry.get(item.preferred_agent)
            if (
                preferred is None
                or (
                    item.allowed_agents is not None
                    and preferred.agent_id not in item.allowed_agents
                )
                or not preferred.can_run(item.required_capabilities)
            ):
                decisions.append(
                    SchedulingDecision(
                        action=ACTION_BLOCKED,
                        reason_code=REASON_NO_CAPABLE_AGENT,
                        explanation=(
                            f"preferred agent {item.preferred_agent!r} is not registered or cannot satisfy "
                            f"required capabilities {sorted(item.required_capabilities)}"
                        ),
                        **base,
                    )
                )
                continue
            capable = [preferred]
        else:
            capable = registry.capable_agents(item.required_capabilities)
            if item.allowed_agents is not None:
                capable = [a for a in capable if a.agent_id in item.allowed_agents]
        if not capable:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_BLOCKED,
                    reason_code=REASON_NO_CAPABLE_AGENT,
                    explanation=(
                        f"no registered agent can satisfy required capabilities "
                        f"{sorted(item.required_capabilities)}"
                    ),
                    **base,
                )
            )
            continue

        # 5) Availability — transient. Capable agents exist but are all down.
        available = [a for a in capable if a.available]
        if not available:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_DEFER,
                    reason_code=REASON_AGENT_UNAVAILABLE,
                    explanation="every capable agent is currently marked unavailable",
                    **base,
                )
            )
            continue

        # 6) Workspace exclusivity — transient. Mirrors the DB workspace lock
        #    (`create_run(enforce_workspace_lock=True)`) *and* guards against
        #    two items in this same plan claiming one workspace.
        if workspace in consumed_workspaces:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_DEFER,
                    reason_code=REASON_WORKSPACE_BUSY,
                    explanation=f"workspace {item.workspace!r} already has an active or just-assigned run",
                    **base,
                )
            )
            continue

        # 7) Global capacity — transient.
        if global_used >= config.max_global_concurrency:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_DEFER,
                    reason_code=REASON_GLOBAL_AT_CAPACITY,
                    explanation=(
                        f"global concurrency limit reached "
                        f"({global_used}/{config.max_global_concurrency} running)"
                    ),
                    **base,
                )
            )
            continue

        # 8) Per-agent capacity + workload distribution — pick the available,
        #    capable agent with the most spare capacity (spreads load), tie
        #    broken by weight desc then agent_id asc (both already the order
        #    `registry.all()` yields, so `max` on a stable-sorted list is
        #    deterministic).
        def _spare(a: AgentSpec) -> int:
            return a.max_concurrency - agent_used.get(a.agent_id, 0)

        with_capacity = [a for a in available if _spare(a) > 0]
        if not with_capacity:
            decisions.append(
                SchedulingDecision(
                    action=ACTION_DEFER,
                    reason_code=REASON_AGENT_AT_CAPACITY,
                    explanation="every capable, available agent is at its concurrency limit",
                    **base,
                )
            )
            continue

        chosen = min(with_capacity, key=lambda a: (-_spare(a), -a.weight, a.agent_id))

        # 9) ASSIGN — draw down every ledger so later items see it.
        global_used += 1
        agent_used[chosen.agent_id] = agent_used.get(chosen.agent_id, 0) + 1
        consumed_workspaces.add(workspace)
        scheduled_tasks.add(item.task_id)
        decisions.append(
            SchedulingDecision(
                action=ACTION_ASSIGN,
                reason_code=REASON_ASSIGNED,
                explanation=(
                    f"assigned to agent {chosen.agent_id!r} as attempt {item.attempts_made + 1}"
                ),
                agent_id=chosen.agent_id,
                executor_id=chosen.executor_id,
                attempt=item.attempts_made + 1,
                **base,
            )
        )

    return SchedulingPlan(decided_at=now, config=config, decisions=tuple(decisions))


# --------------------------------------------------------------------------
# Read-only DB projection: the load already in flight
# --------------------------------------------------------------------------


def build_load_snapshot(db_path, *, agent_of_run=None) -> LoadSnapshot:
    """Project the current in-flight load from `runtime.db` — read-only, never
    mutates a row. `global_running` and `busy_workspaces` come straight from
    every run in `db.EXECUTION_CENTER_ACTIVE_STATES` (`PREPARED`/`QUEUED`/
    `RUNNING`), so the workspace exclusivity the planner enforces exactly
    matches the DB workspace lock's own active-state set.

    Current run rows persist ``provider_id`` at launch, so the default
    attribution uses that authoritative value. ``agent_of_run`` remains an
    override for callers with a custom agent registry."""
    if agent_of_run is None:
        agent_of_run = lambda run: run.get("provider_id") or "claude_code"  # noqa: E731
    rows = db.list_runs(db_path, states=db.EXECUTION_CENTER_ACTIVE_STATES)
    running_by_agent: dict[str, int] = {}
    busy: set[str] = set()
    active_task_ids: set[str] = set()
    for run in rows:
        agent_id = agent_of_run(run)
        running_by_agent[agent_id] = running_by_agent.get(agent_id, 0) + 1
        workspace = run.get("repository_path")
        if workspace:
            busy.add(_normalize_workspace(workspace))
        task_id = run.get("task_id")
        if task_id:
            active_task_ids.add(task_id)
    return LoadSnapshot(
        running_by_agent=running_by_agent,
        busy_workspaces=frozenset(busy),
        active_task_ids=frozenset(active_task_ids),
        global_running=len(rows),
    )
