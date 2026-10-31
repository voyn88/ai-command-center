"""Autonomy proposal foundation — the pre-execution decision layer.

Experimental foundation (AICC-AUTONOMY-002). Persisted, evidence-backed proposal
lifecycle only — no UI, no automated evidence collectors, no policy resolver, no
background driver, no executor. `dispatch` returns a dry-run plan the caller must
run explicitly. Do not extend without first deciding whether to complete or retire
this layer; see `docs/adr/0005-autonomy-proposal-foundation.md`.

This module is the domain core of AICC-AUTONOMY-002. Where the *completion*
pipeline (AICC-AUTONOMY-001, `completion.py`) governs the **post-execution**
lifecycle of a run (validate -> PR -> merge), this module governs the
**pre-execution** decision: the explicit, auditable separation of

    RECOMMENDATION  ->  APPROVAL  ->  EXECUTION

An `AutonomyProposal` is a persisted, evidence-backed, risk-classified
suggestion that the autonomy engine wants to take some action. It **never
executes anything itself**. It moves through a state machine gated by
deterministic eligibility rules and an explicit approval decision. Risky
actions default to blocked, and nothing may be dispatched for execution
without an explicit policy *and* an explicit approval state.

Design invariants (mirroring `completion.py`, and enforced by the safety
contract in ADR 0005):

* Pure domain — this module imports no persistence, no UI, no subprocess, no
  network. Every function here is deterministic in its inputs. `db.py` imports
  *this* module for its transition guard; never the reverse (matching how
  `db.py` imports `completion` as `completion_domain`).
* Reproducible — `evaluate_eligibility` is a pure function of
  (kind, risk, evidence, policy). The same stored evidence always yields the
  same verdict, so every decision can be re-derived from the audit trail.
* No fabricated evidence — this module never *invents* evidence. It only
  classifies and evaluates evidence handed to it, each item carrying its own
  `source`. Callers (`autonomy_service`) are responsible for collecting real,
  attributable observations.
* Conservative by default — `AutonomyPolicy()` with no arguments disables
  autonomy entirely: no kinds allowed, nothing auto-approved, execution
  dispatch off. A brand-new install proposes nothing and auto-executes
  nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from command_center.models import iso_now

# --------------------------------------------------------------------------
# Proposal kinds — what class of action is being proposed. Creating a proposal
# of any kind is always side-effect-free; the *action itself* only ever happens
# later, through the existing execution routes, after explicit approval.
# --------------------------------------------------------------------------


class ProposalKind:
    TASK_CREATION = "TASK_CREATION"        # propose recording a new task (a gap was detected)
    TASK_EXECUTION = "TASK_EXECUTION"      # propose launching a run for an existing task
    PRIORITY_CHANGE = "PRIORITY_CHANGE"    # propose reprioritising an existing task
    DEPENDENCY_LINK = "DEPENDENCY_LINK"    # propose recording a dependency edge
    MERGE = "MERGE"                        # propose a merge (always highest risk)


ALL_KINDS: frozenset[str] = frozenset(
    {
        ProposalKind.TASK_CREATION,
        ProposalKind.TASK_EXECUTION,
        ProposalKind.PRIORITY_CHANGE,
        ProposalKind.DEPENDENCY_LINK,
        ProposalKind.MERGE,
    }
)


# --------------------------------------------------------------------------
# Risk classification
# --------------------------------------------------------------------------


class RiskLevel:
    NONE = "NONE"          # a sentinel used only by policy ("auto-approve nothing")
    LOW = "LOW"            # metadata-only, no repository mutation
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"          # launches a run / mutates a working tree
    CRITICAL = "CRITICAL"  # merges / irreversible-by-default; never auto


# Total order over risk, so a policy can say "auto-approve up to LEVEL".
RISK_ORDER: dict[str, int] = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}

ALL_RISK_LEVELS: frozenset[str] = frozenset(RISK_ORDER)


def risk_at_most(risk: str, ceiling: str) -> bool:
    """True if `risk` is no higher than `ceiling` in `RISK_ORDER`. An unknown
    risk or ceiling is treated as CRITICAL (the safe, most-restrictive answer),
    so a typo never silently widens what may be auto-approved."""
    r = RISK_ORDER.get(risk, RISK_ORDER[RiskLevel.CRITICAL])
    c = RISK_ORDER.get(ceiling, RISK_ORDER[RiskLevel.NONE])
    return r <= c


# --------------------------------------------------------------------------
# Proposal state machine
# --------------------------------------------------------------------------


class ProposalState:
    DRAFT = "DRAFT"                          # created; evidence attached; not yet assessed
    PROPOSED = "PROPOSED"                    # submitted for deterministic assessment
    ELIGIBLE = "ELIGIBLE"                    # passed eligibility; awaiting an approval decision
    BLOCKED = "BLOCKED"                      # failed eligibility / risk too high — needs a human
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # explicit human approval gate is required
    APPROVED = "APPROVED"                    # approved — the ONLY state execution may dispatch from
    REJECTED = "REJECTED"                    # terminal — declined
    DISPATCHED = "DISPATCHED"                # execution handed off to the caller (boundary crossed)
    EXECUTED = "EXECUTED"                    # terminal — execution confirmed & linked
    WITHDRAWN = "WITHDRAWN"                  # terminal — engine/human withdrew it


ALL_STATES: frozenset[str] = frozenset(
    {
        ProposalState.DRAFT,
        ProposalState.PROPOSED,
        ProposalState.ELIGIBLE,
        ProposalState.BLOCKED,
        ProposalState.AWAITING_APPROVAL,
        ProposalState.APPROVED,
        ProposalState.REJECTED,
        ProposalState.DISPATCHED,
        ProposalState.EXECUTED,
        ProposalState.WITHDRAWN,
    }
)

# Terminal: no ordinary transition ever leaves these.
TERMINAL_STATES: frozenset[str] = frozenset(
    {
        ProposalState.REJECTED,
        ProposalState.EXECUTED,
        ProposalState.WITHDRAWN,
    }
)

# The single state from which an approved plan may cross into execution.
DISPATCHABLE_STATE = ProposalState.APPROVED

# Explicit allowed-transition model — the domain-level structural guard,
# analogous to `completion.COMPLETION_TRANSITIONS`. `db.update_proposal`
# consults `is_valid_proposal_transition` before any compare-and-set write, so
# the persistence layer refuses a backward jump or a move out of a terminal
# state independent of the service's control flow. A same-state update
# (evidence enrichment / metadata) is always allowed and is deliberately NOT
# encoded as a self-edge here (see `is_valid_proposal_transition`).
#
# `WITHDRAWN` is reachable from every non-terminal state: a human (or the
# engine) may always abandon a proposal that has not yet executed.
PROPOSAL_TRANSITIONS: dict[str, frozenset[str]] = {
    ProposalState.DRAFT: frozenset(
        {ProposalState.PROPOSED, ProposalState.WITHDRAWN}
    ),
    ProposalState.PROPOSED: frozenset(
        {ProposalState.ELIGIBLE, ProposalState.BLOCKED, ProposalState.WITHDRAWN}
    ),
    ProposalState.ELIGIBLE: frozenset(
        {
            ProposalState.AWAITING_APPROVAL,
            ProposalState.APPROVED,
            ProposalState.BLOCKED,
            ProposalState.WITHDRAWN,
        }
    ),
    # A blocked proposal is not dead: a human may override it onto the approval
    # gate, or reject it outright. It can never jump straight to APPROVED.
    ProposalState.BLOCKED: frozenset(
        {ProposalState.AWAITING_APPROVAL, ProposalState.REJECTED, ProposalState.WITHDRAWN}
    ),
    ProposalState.AWAITING_APPROVAL: frozenset(
        {ProposalState.APPROVED, ProposalState.REJECTED, ProposalState.WITHDRAWN}
    ),
    ProposalState.APPROVED: frozenset(
        {ProposalState.DISPATCHED, ProposalState.WITHDRAWN}
    ),
    # Dispatch either confirms (EXECUTED) or fails back to BLOCKED for a human
    # to inspect. It never silently returns to APPROVED.
    ProposalState.DISPATCHED: frozenset(
        {ProposalState.EXECUTED, ProposalState.BLOCKED}
    ),
    ProposalState.REJECTED: frozenset(),
    ProposalState.EXECUTED: frozenset(),
    ProposalState.WITHDRAWN: frozenset(),
}


def is_valid_proposal_transition(current: str, new: str) -> bool:
    """Whether persisting proposal-state `current` -> `new` is legal.

    A same-state update (`current == new`) is always allowed: it carries
    refreshed evidence or metadata, not a lifecycle move. Otherwise `new` must
    appear in `PROPOSAL_TRANSITIONS[current]`; an unrecognised `current` permits
    no transition, and terminal states permit none."""
    if current == new:
        return True
    return new in PROPOSAL_TRANSITIONS.get(current, frozenset())


# --------------------------------------------------------------------------
# Reason codes — every eligibility outcome and audit event is tagged with one
# of these, so the audit trail is machine-readable and the "why" is explicit.
# --------------------------------------------------------------------------


class ReasonCode:
    # Eligibility — blocked
    POLICY_DISABLED = "POLICY_DISABLED"
    KIND_NOT_ALLOWED = "KIND_NOT_ALLOWED"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EVIDENCE_BLOCKER = "EVIDENCE_BLOCKER"
    RISK_EXCEEDS_POLICY = "RISK_EXCEEDS_POLICY"
    RISK_CRITICAL_NO_AUTO = "RISK_CRITICAL_NO_AUTO"
    DISPATCH_DISABLED = "DISPATCH_DISABLED"
    ACTION_PAYLOAD_INVALID = "ACTION_PAYLOAD_INVALID"
    EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"
    # Eligibility — allowed
    AUTO_APPROVED = "AUTO_APPROVED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    # Decisions
    APPROVED_BY_HUMAN = "APPROVED_BY_HUMAN"
    REJECTED_BY_HUMAN = "REJECTED_BY_HUMAN"
    OVERRIDDEN_BY_HUMAN = "OVERRIDDEN_BY_HUMAN"
    DISPATCHED = "DISPATCHED"
    EXECUTION_CONFIRMED = "EXECUTION_CONFIRMED"
    EXECUTION_MISMATCH = "EXECUTION_MISMATCH"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    WITHDRAWN = "WITHDRAWN"


# Event types for the append-only audit trail.
class EventType:
    CREATED = "created"
    ASSESSED = "assessed"
    TRANSITION = "transition"
    APPROVAL = "approval"
    REJECTION = "rejection"
    DISPATCH = "dispatch"
    EXECUTION = "execution"
    WITHDRAWAL = "withdrawal"
    OVERRIDE = "override"


# --------------------------------------------------------------------------
# Evidence model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One deterministic, attributable observation supporting a proposal.

    * `kind` — a stable category, e.g. "git_status", "task_gap", "test_result".
    * `source` — where it came from, e.g. "git_info.get_status" or a file path.
      Never blank: an observation with no source is not evidence, it is a claim.
    * `summary` — a short human-readable statement of what was observed.
    * `observed_at` — when it was observed (`iso_now()`), used for staleness.
    * `data` — structured, JSON-serialisable specifics.
    * `is_blocker` — the observation itself proves the action is currently
      unsafe (e.g. a dirty working tree for a TASK_EXECUTION). A single blocker
      forces the proposal to BLOCKED regardless of policy.
    """

    kind: str
    source: str
    summary: str
    observed_at: str = ""
    data: dict = field(default_factory=dict)
    is_blocker: bool = False

    def __post_init__(self) -> None:
        if not self.kind or not str(self.kind).strip():
            raise ValueError("Evidence.kind must be a non-empty string")
        if not self.source or not str(self.source).strip():
            raise ValueError("Evidence.source must be non-empty — evidence must be attributable")
        if self.observed_at == "":
            object.__setattr__(self, "observed_at", iso_now())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "source": self.source,
            "summary": self.summary,
            "observed_at": self.observed_at,
            "data": self.data,
            "is_blocker": self.is_blocker,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Evidence":
        return cls(
            kind=str(data["kind"]),
            source=str(data["source"]),
            summary=str(data.get("summary", "")),
            observed_at=str(data.get("observed_at", "")) or iso_now(),
            data=dict(data.get("data") or {}),
            is_blocker=bool(data.get("is_blocker", False)),
        )


def evidence_digest(evidence: list[Evidence]) -> str:
    """A stable content hash over the normalised evidence set.

    Two proposals with the same evidence (ignoring order) produce the same
    digest; changing any observed fact changes the digest. Stored alongside the
    proposal so a reviewer can confirm the evidence the decision was made on has
    not silently changed. `observed_at` is included: re-observing the same fact
    at a different time is a genuinely different evidence set for staleness
    purposes."""
    normalised = sorted(
        (
            json.dumps(e.to_dict(), ensure_ascii=False, sort_keys=True)
            for e in evidence
        )
    )
    blob = "\n".join(normalised)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def evidence_age_seconds(evidence: Evidence, *, now: str | None = None) -> float | None:
    """Age of an evidence item in seconds relative to `now` (default
    `iso_now()`), both read as naive UTC (the project-wide convention — see
    `models.iso_now`). None if either timestamp is unparseable."""
    now_dt = _parse_iso(now or iso_now())
    obs_dt = _parse_iso(evidence.observed_at)
    if now_dt is None or obs_dt is None:
        return None
    return (now_dt - obs_dt).total_seconds()


# --------------------------------------------------------------------------
# Autonomy policy — conservative by construction
# --------------------------------------------------------------------------


@dataclass
class AutonomyPolicy:
    """The explicit governance envelope for autonomy. A default-constructed
    policy is fully closed: autonomy disabled, no kinds allowed, nothing
    auto-approved, dispatch off. Autonomy only ever does something because a
    policy was *explicitly* opened up."""

    enabled: bool = False
    # Kinds the engine may even propose. Empty = none. `ALL_KINDS` opens all.
    allowed_kinds: frozenset[str] = field(default_factory=frozenset)
    # Highest risk that may be auto-approved without a human. NONE = never auto.
    # CRITICAL is intentionally NOT auto-approvable even if named here.
    auto_approve_max_risk: str = RiskLevel.NONE
    # Even an APPROVED proposal cannot be dispatched to execution unless True.
    allow_execution_dispatch: bool = False
    # Evidence older than this (seconds) is stale and blocks eligibility.
    max_evidence_age_seconds: int = 3600

    def __post_init__(self) -> None:
        # Configuration is an authority boundary, so it must not use Python's
        # permissive coercions (for example bool("false") is True).  If any
        # field has the wrong type or vocabulary, close the *whole* policy
        # rather than trying to salvage a partially permissive configuration.
        allowed_container = isinstance(self.allowed_kinds, (set, frozenset, list, tuple))
        allowed_values = (
            list(self.allowed_kinds)
            if allowed_container
            else []
        )
        valid = (
            type(self.enabled) is bool
            and allowed_container
            and all(type(kind) is str and kind in ALL_KINDS for kind in allowed_values)
            and type(self.auto_approve_max_risk) is str
            and self.auto_approve_max_risk in RISK_ORDER
            and type(self.allow_execution_dispatch) is bool
            and type(self.max_evidence_age_seconds) is int
            and self.max_evidence_age_seconds >= 0
        )
        if not valid:
            self.enabled = False
            self.allowed_kinds = frozenset()
            self.auto_approve_max_risk = RiskLevel.NONE
            self.allow_execution_dispatch = False
            self.max_evidence_age_seconds = 3600
            return

        self.allowed_kinds = frozenset(allowed_values)
        # CRITICAL is never auto-approvable; clamp a policy that tries.
        if self.auto_approve_max_risk == RiskLevel.CRITICAL:
            self.auto_approve_max_risk = RiskLevel.HIGH

    def allows_kind(self, kind: str) -> bool:
        return kind in self.allowed_kinds

    def intersect(self, other: "AutonomyPolicy | None") -> "AutonomyPolicy":
        """Conservative combination — the result grants a capability only if
        BOTH `self` and `other` grant it. A caller-supplied runtime policy can
        thus only ever *further restrict* the persisted policy, never widen it
        (the F1 invariant: persisted policy is authoritative). `other is None`
        returns `self` unchanged. Every field combines by its safe direction:

        * `enabled`, `allow_execution_dispatch` — logical AND;
        * `allowed_kinds` — set intersection;
        * `auto_approve_max_risk` — the lower ceiling (min over `RISK_ORDER`);
        * `max_evidence_age_seconds` — the smaller (stricter) window.

        There is no OR/max anywhere, so no combination can be more permissive
        than either input."""
        if other is None:
            return self
        lower_ceiling = (
            self.auto_approve_max_risk
            if risk_at_most(self.auto_approve_max_risk, other.auto_approve_max_risk)
            else other.auto_approve_max_risk
        )
        return AutonomyPolicy(
            enabled=self.enabled and other.enabled,
            allowed_kinds=self.allowed_kinds & other.allowed_kinds,
            auto_approve_max_risk=lower_ceiling,
            allow_execution_dispatch=self.allow_execution_dispatch and other.allow_execution_dispatch,
            max_evidence_age_seconds=min(self.max_evidence_age_seconds, other.max_evidence_age_seconds),
        )

    def may_auto_approve(self, risk: str) -> bool:
        if not self.enabled:
            return False
        if risk == RiskLevel.CRITICAL:
            return False
        if self.auto_approve_max_risk == RiskLevel.NONE:
            return False
        return risk_at_most(risk, self.auto_approve_max_risk)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "allowed_kinds": sorted(self.allowed_kinds),
            "auto_approve_max_risk": self.auto_approve_max_risk,
            "allow_execution_dispatch": self.allow_execution_dispatch,
            "max_evidence_age_seconds": self.max_evidence_age_seconds,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict | None) -> "AutonomyPolicy":
        if type(data) is not dict:
            return cls()
        known_fields = {
            "enabled",
            "allowed_kinds",
            "auto_approve_max_risk",
            "allow_execution_dispatch",
            "max_evidence_age_seconds",
        }
        if set(data) - known_fields:
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            allowed_kinds=data.get("allowed_kinds", frozenset()),
            auto_approve_max_risk=data.get("auto_approve_max_risk", RiskLevel.NONE),
            allow_execution_dispatch=data.get("allow_execution_dispatch", False),
            max_evidence_age_seconds=data.get("max_evidence_age_seconds", 3600),
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "AutonomyPolicy":
        if not raw:
            return cls()
        try:
            return cls.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            return cls()


def policy_fingerprint(policy: "AutonomyPolicy | None") -> str | None:
    """A short, non-sensitive content fingerprint of a policy (first 12 hex chars
    of the SHA-256 over its canonical JSON), for audit metadata. `None` for a
    missing policy. Deliberately a digest, not the raw JSON, so audit records
    stay low-cardinality and never embed arbitrary policy blobs."""
    if policy is None:
        return None
    return hashlib.sha256(policy.to_json().encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Risk classification — deterministic from kind + evidence
# --------------------------------------------------------------------------

# Base risk floor per kind, before evidence-based escalation.
_BASE_RISK: dict[str, str] = {
    ProposalKind.TASK_CREATION: RiskLevel.LOW,     # records a task; no repo mutation
    ProposalKind.PRIORITY_CHANGE: RiskLevel.LOW,   # metadata only
    ProposalKind.DEPENDENCY_LINK: RiskLevel.LOW,   # metadata only
    ProposalKind.TASK_EXECUTION: RiskLevel.HIGH,   # launches a run that mutates a working tree
    ProposalKind.MERGE: RiskLevel.CRITICAL,        # irreversible-by-default
}


def classify_risk(kind: str, evidence: list[Evidence]) -> str:
    """Deterministically classify the risk of acting on a proposal.

    Starts from a per-kind floor and escalates on evidence that signals danger
    (a blocker observation, or a `data["risk_signal"]`/`data["sensitive"]`
    marker). Never de-escalates below the floor. An unknown kind is treated as
    HIGH — the safe default for "we don't recognise this, don't take it lightly"
    (MERGE stays CRITICAL via its floor)."""
    risk = _BASE_RISK.get(kind, RiskLevel.HIGH)
    for e in evidence:
        if e.is_blocker and risk_at_most(risk, RiskLevel.MEDIUM):
            risk = RiskLevel.HIGH
        signal = e.data.get("risk_signal")
        if signal in RISK_ORDER and RISK_ORDER[signal] > RISK_ORDER[risk]:
            risk = signal
        if e.data.get("sensitive") and RISK_ORDER[risk] < RISK_ORDER[RiskLevel.HIGH]:
            risk = RiskLevel.HIGH
    return risk


# --------------------------------------------------------------------------
# Deterministic eligibility evaluation — the heart of "safe & explainable"
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EligibilityVerdict:
    """The pure, reproducible result of evaluating a proposal against a policy.

    * `decision` — ELIGIBLE or BLOCKED.
    * `next_state` — the exact `ProposalState` the proposal should move to.
    * `risk` — the classified risk level (echoed for the audit record).
    * `primary_reason` — a single `ReasonCode` summarising the outcome.
    * `reasons` — every reason that contributed, most-decisive first.
    * `requires_human` — True when a human approval gate is required.
    """

    decision: str  # "ELIGIBLE" | "BLOCKED"
    next_state: str
    risk: str
    primary_reason: str
    reasons: list[str]
    requires_human: bool

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "next_state": self.next_state,
            "risk": self.risk,
            "primary_reason": self.primary_reason,
            "reasons": list(self.reasons),
            "requires_human": self.requires_human,
        }


DECISION_ELIGIBLE = "ELIGIBLE"
DECISION_BLOCKED = "BLOCKED"


def evaluate_eligibility(
    *,
    kind: str,
    evidence: list[Evidence],
    policy: AutonomyPolicy,
    now: str | None = None,
) -> EligibilityVerdict:
    """Purely and reproducibly decide what should happen to a proposal.

    This is the single source of truth for autonomy safety. It has no side
    effects and depends only on its arguments, so re-running it on the stored
    evidence + policy always reproduces the same verdict — the guarantee behind
    "all decisions must be reproducible from stored evidence".

    Ordering is defence-in-depth: the hardest blocks are checked first, so a
    disabled policy or a disallowed kind can never be overtaken by a later
    "auto-approve" branch. Every path that returns BLOCKED defaults risky
    actions to blocked, as required.
    """
    risk = classify_risk(kind, evidence)
    reasons: list[str] = []

    def blocked(reason: str, state: str = ProposalState.BLOCKED) -> EligibilityVerdict:
        reasons.insert(0, reason)
        return EligibilityVerdict(
            decision=DECISION_BLOCKED,
            next_state=state,
            risk=risk,
            primary_reason=reason,
            reasons=reasons,
            requires_human=True,
        )

    # 1. Master switch. Autonomy off => nothing is eligible.
    if not policy.enabled:
        return blocked(ReasonCode.POLICY_DISABLED)

    # 2. Kind must be explicitly allowed.
    if not policy.allows_kind(kind):
        return blocked(ReasonCode.KIND_NOT_ALLOWED)

    # 3. There must be at least one piece of evidence.
    if not evidence:
        return blocked(ReasonCode.EVIDENCE_MISSING)

    # 4. No evidence item may be stale. Unparseable, older than the window, OR
    #    future-dated (negative age — an observation "from the future" is not a
    #    valid basis for a decision) all count as stale and block.
    for e in evidence:
        age = evidence_age_seconds(e, now=now)
        if age is None or age < 0 or age > policy.max_evidence_age_seconds:
            reasons.append(ReasonCode.EVIDENCE_STALE)
            return blocked(ReasonCode.EVIDENCE_STALE)

    # 5. Any blocker observation forces BLOCKED regardless of risk/policy.
    if any(e.is_blocker for e in evidence):
        return blocked(ReasonCode.EVIDENCE_BLOCKER)

    # 6. CRITICAL risk is never eligible for anything but a human gate.
    if risk == RiskLevel.CRITICAL:
        reasons.append(ReasonCode.RISK_CRITICAL_NO_AUTO)
        reasons.append(ReasonCode.HUMAN_APPROVAL_REQUIRED)
        return EligibilityVerdict(
            decision=DECISION_ELIGIBLE,
            next_state=ProposalState.AWAITING_APPROVAL,
            risk=risk,
            primary_reason=ReasonCode.HUMAN_APPROVAL_REQUIRED,
            reasons=reasons,
            requires_human=True,
        )

    # 7. Auto-approve only when policy explicitly permits this risk level.
    if policy.may_auto_approve(risk):
        reasons.append(ReasonCode.AUTO_APPROVED)
        return EligibilityVerdict(
            decision=DECISION_ELIGIBLE,
            next_state=ProposalState.APPROVED,
            risk=risk,
            primary_reason=ReasonCode.AUTO_APPROVED,
            reasons=reasons,
            requires_human=False,
        )

    # 8. Otherwise eligible, but a human must approve.
    reason = (
        ReasonCode.RISK_EXCEEDS_POLICY
        if not risk_at_most(risk, policy.auto_approve_max_risk)
        else ReasonCode.HUMAN_APPROVAL_REQUIRED
    )
    reasons.append(reason)
    return EligibilityVerdict(
        decision=DECISION_ELIGIBLE,
        next_state=ProposalState.AWAITING_APPROVAL,
        risk=risk,
        primary_reason=ReasonCode.HUMAN_APPROVAL_REQUIRED,
        reasons=reasons,
        requires_human=True,
    )


# --------------------------------------------------------------------------
# Dry-run execution planning — describe what WOULD happen; never do it
# --------------------------------------------------------------------------


# The exact, bounded action vocabulary for each proposal kind.  Proposal
# metadata (title/rationale/evidence) explains the decision; only these fields
# authorise an eventual side effect.  Unknown fields are rejected at proposal
# creation so a caller cannot hide an unreviewed action argument beside the
# reviewed payload.
ACTION_PARAMETER_KEYS: dict[str, frozenset[str]] = {
    ProposalKind.TASK_CREATION: frozenset({"project", "title", "task_type"}),
    ProposalKind.TASK_EXECUTION: frozenset(
        {
            "task_id",
            "project",
            "repository_path",
            "expected_branch",
            "task_type",
            "instruction",
        }
    ),
    ProposalKind.PRIORITY_CHANGE: frozenset({"task_id", "priority"}),
    ProposalKind.DEPENDENCY_LINK: frozenset({"task_id", "dependency_task_id"}),
    ProposalKind.MERGE: frozenset(
        {"project", "repository_path", "head_branch", "base_branch"}
    ),
}

ACTION_REQUIRED_PARAMETERS: dict[str, frozenset[str]] = {
    kind: keys for kind, keys in ACTION_PARAMETER_KEYS.items()
}


def canonical_action_parameters(
    *,
    kind: str,
    project: str,
    title: str,
    task_id: str | None = None,
    parameters: dict | None = None,
) -> dict:
    """Return the canonical, JSON-safe action payload for a proposal.

    The proposal's top-level identity fields are authoritative: a duplicated
    ``project``, ``title`` or ``task_id`` inside ``parameters`` must agree with
    them.  TASK_CREATION retains the existing safe default
    ``task_type="implementation"``; other absent required values remain absent
    and therefore make the dry-run plan non-dispatchable.
    """
    if kind not in ACTION_PARAMETER_KEYS:
        raise ValueError(f"Unknown proposal kind: {kind!r}")
    if parameters is None:
        raw: dict = {}
    elif type(parameters) is dict:
        raw = dict(parameters)
    else:
        raise ValueError("proposal parameters must be a JSON object")

    unknown = set(raw) - ACTION_PARAMETER_KEYS[kind]
    if unknown:
        raise ValueError(
            f"Unknown action parameters for {kind}: {sorted(unknown)!r}"
        )

    canonical = dict(raw)

    def bind_authoritative(key: str, value: str | None) -> None:
        if key in canonical and canonical[key] != value:
            raise ValueError(
                f"Action parameter {key!r} conflicts with the proposal identity"
            )
        if value is not None:
            canonical[key] = value

    if kind in {
        ProposalKind.TASK_CREATION,
        ProposalKind.TASK_EXECUTION,
        ProposalKind.MERGE,
    }:
        bind_authoritative("project", project)
    if kind == ProposalKind.TASK_CREATION:
        bind_authoritative("title", title)
        canonical.setdefault("task_type", "implementation")
    if kind in {
        ProposalKind.TASK_EXECUTION,
        ProposalKind.PRIORITY_CHANGE,
        ProposalKind.DEPENDENCY_LINK,
    }:
        bind_authoritative("task_id", task_id)

    # Every accepted action value is a non-blank string.  The intentionally
    # small vocabulary makes both the digest and later confirmation exact and
    # avoids bool/int/string coercion surprises at an authority boundary.
    for key, value in canonical.items():
        if type(value) is not str or not value.strip():
            raise ValueError(
                f"Action parameter {key!r} must be a non-empty string"
            )
        canonical[key] = value.strip()

    # Round-trip through canonical JSON both to reject non-JSON values and to
    # detach the stored payload from any caller-owned mutable object.
    try:
        return json.loads(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("proposal parameters must be JSON serializable") from exc


def action_digest(*, kind: str, dispatch_route: str, parameters: dict) -> str:
    """SHA-256 of the exact action identity returned to the executor."""
    payload = {
        "kind": kind,
        "dispatch_route": dispatch_route,
        "parameters": parameters,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionPlan:
    """A description of the action a proposal would take if executed — the
    dry-run. Building a plan has no side effects: it does not create tasks,
    launch runs, or touch a repository. It exists so a human (or the audit
    trail) can see exactly what an approval authorises *before* anything runs.

    `dispatch_route` names the existing, explicit execution route the caller
    must invoke (e.g. `ExecutionCenterAPI.start_run`) — autonomy never launches
    directly. `requires_confirmation` restates that that route itself demands an
    explicit confirmation flag."""

    kind: str
    summary: str
    dispatch_route: str
    steps: list[str]
    parameters: dict
    requires_confirmation: bool = True
    executable: bool = False
    missing_parameters: tuple[str, ...] = ()
    action_digest: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "dispatch_route": self.dispatch_route,
            "steps": list(self.steps),
            "parameters": dict(self.parameters),
            "requires_confirmation": self.requires_confirmation,
            "executable": self.executable,
            "missing_parameters": list(self.missing_parameters),
            "action_digest": self.action_digest,
        }


# Which concrete route each kind would use. TASK_CREATION records a task row;
# everything that actually runs code goes through the existing, confirmation-
# gated `ExecutionCenterAPI.start_run`.
_DISPATCH_ROUTES: dict[str, str] = {
    ProposalKind.TASK_CREATION: "db.create_task",
    ProposalKind.TASK_EXECUTION: "ExecutionCenterAPI.start_run",
    ProposalKind.PRIORITY_CHANGE: "unsupported (planning-store adapter required)",
    ProposalKind.DEPENDENCY_LINK: "unsupported (planning-store adapter required)",
    ProposalKind.MERGE: "completion_service.merge (manual gate)",
}

_EXECUTABLE_KINDS: frozenset[str] = frozenset(
    {ProposalKind.TASK_CREATION, ProposalKind.TASK_EXECUTION}
)


def build_execution_plan(
    *,
    kind: str,
    title: str,
    rationale: str,
    parameters: dict | None = None,
) -> ExecutionPlan:
    """Produce the dry-run plan for a proposal. Pure and side-effect-free."""
    params = dict(parameters or {})
    route = _DISPATCH_ROUTES.get(kind, "unsupported")
    missing = tuple(
        sorted(
            key
            for key in ACTION_REQUIRED_PARAMETERS.get(kind, frozenset())
            if type(params.get(key)) is not str or not params[key].strip()
        )
    )
    executable = kind in _EXECUTABLE_KINDS and not missing
    if kind == ProposalKind.TASK_CREATION:
        steps = [
            f"Record a new task: {title!r}",
            "Task is created in an unstarted state; no run is launched",
            "A separate TASK_EXECUTION proposal is required to run it",
        ]
    elif kind == ProposalKind.TASK_EXECUTION:
        steps = [
            f"Invoke {route} with confirmed=True for task {params.get('task_id', '<unknown>')!r}",
            "The Supervisor launches a real run against the configured repository",
            "The completion pipeline then governs validation -> PR (never auto-merge)",
        ]
    elif kind == ProposalKind.MERGE:
        steps = [
            "Merging is never performed by autonomy; this plan is advisory only",
            "A human must perform or explicitly authorise the merge",
        ]
    elif kind in {ProposalKind.PRIORITY_CHANGE, ProposalKind.DEPENDENCY_LINK}:
        steps = [
            f"{kind} is advisory until a durable planning-store result adapter exists",
            "The proposal cannot cross the dispatch boundary in this implementation",
        ]
    else:
        steps = [f"Apply {kind} via {route} (metadata change only)"]
    return ExecutionPlan(
        kind=kind,
        summary=f"{kind}: {title}",
        dispatch_route=route,
        steps=steps,
        parameters=params,
        requires_confirmation=True,
        executable=executable,
        missing_parameters=missing,
        action_digest=action_digest(kind=kind, dispatch_route=route, parameters=params),
    )
