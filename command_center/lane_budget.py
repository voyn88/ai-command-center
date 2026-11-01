"""Bounded dispatch budget and pause switch for AICC lanes.

Not an orchestration engine: no process launch, no queue claim, no scheduler.
Operators pause a lane and cap how many PRs/reruns/remediations a repo may
open in a window. Persistence is the existing `command_center.storage` helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from command_center import models, storage

STATE_FILE_NAME = "lane_budget.json"
LOCK_FILE_NAME = "lane_budget.lock"
DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_MAX_PR_PER_WINDOW = 4
DEFAULT_MAX_RERUNS_PER_WINDOW = 8
DEFAULT_MAX_REMEDIATIONS_PER_WINDOW = 4
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

REASON_PAUSED = "lane_paused"
REASON_BUDGET_PR = "budget_pr_window_exhausted"
REASON_BUDGET_RERUN = "budget_rerun_window_exhausted"
REASON_BUDGET_REMEDIATE = "budget_remediation_window_exhausted"
REASON_BUDGET_FAILURES = "budget_consecutive_failures"
REASON_OK = "ok"


def state_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / STATE_FILE_NAME


def _empty_state() -> dict[str, Any]:
    return {
        "paused": False,
        "pause_reason": None,
        "pause_sha": None,
        "paused_at": None,
        "paused_by": None,
        "window_seconds": DEFAULT_WINDOW_SECONDS,
        "limits": {
            "pr_per_window": DEFAULT_MAX_PR_PER_WINDOW,
            "reruns_per_window": DEFAULT_MAX_RERUNS_PER_WINDOW,
            "remediations_per_window": DEFAULT_MAX_REMEDIATIONS_PER_WINDOW,
            "consecutive_failures": DEFAULT_MAX_CONSECUTIVE_FAILURES,
        },
        "repos": {},
    }


def load_state(root: Path) -> dict[str, Any]:
    raw = storage.read_json(state_path(root), _empty_state())
    if not isinstance(raw, dict):
        return _empty_state()
    base = _empty_state()
    base.update({k: raw[k] for k in base if k in raw})
    if not isinstance(base.get("limits"), dict):
        base["limits"] = _empty_state()["limits"]
    if not isinstance(base.get("repos"), dict):
        base["repos"] = {}
    return base


def save_state(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    with storage.file_lock(storage.resolve_data_dir(root) / LOCK_FILE_NAME):
        storage.atomic_write_json(state_path(root), state)
    return state


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str
    repo: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "repo": self.repo,
            "detail": self.detail,
        }


def _repo_bucket(state: dict[str, Any], repo: str) -> dict[str, Any]:
    repos = state.setdefault("repos", {})
    bucket = repos.get(repo)
    if not isinstance(bucket, dict):
        bucket = {
            "window_started_at": models.iso_now(),
            "pr_count": 0,
            "rerun_count": 0,
            "remediation_count": 0,
            "consecutive_failures": 0,
        }
        repos[repo] = bucket
    return bucket


def _window_expired(started_at: object, window_seconds: int) -> bool:
    if not isinstance(started_at, str):
        return True
    try:
        started = datetime.fromisoformat(started_at)
        now = datetime.fromisoformat(models.iso_now())
    except (TypeError, ValueError):
        return True
    return (now - started).total_seconds() >= window_seconds


def _roll_window(bucket: dict[str, Any], window_seconds: int) -> None:
    if _window_expired(bucket.get("window_started_at"), window_seconds):
        bucket["window_started_at"] = models.iso_now()
        bucket["pr_count"] = 0
        bucket["rerun_count"] = 0
        bucket["remediation_count"] = 0


def pause(root: Path, *, reason: str, actor: str | None = None, sha: str | None = None) -> dict[str, Any]:
    state = load_state(root)
    state["paused"] = True
    state["pause_reason"] = reason
    state["pause_sha"] = sha
    state["paused_at"] = models.iso_now()
    state["paused_by"] = actor
    return save_state(root, state)


def resume(root: Path, *, actor: str | None = None) -> dict[str, Any]:
    state = load_state(root)
    state["paused"] = False
    state["pause_reason"] = None
    state["pause_sha"] = None
    state["paused_at"] = None
    state["paused_by"] = actor
    return save_state(root, state)


def pause_for_red_smoke(root: Path, *, sha: str, actor: str = "self_deploy") -> dict[str, Any]:
    return pause(root, reason=f"red_smoke sha={sha}", actor=actor, sha=sha)


def check_dispatch(root: Path, repo: str, kind: str = "pr") -> GuardDecision:
    state = load_state(root)
    if state.get("paused") is True:
        return GuardDecision(
            False,
            REASON_PAUSED,
            repo=repo,
            detail={
                "pause_reason": state.get("pause_reason"),
                "pause_sha": state.get("pause_sha"),
            },
        )
    limits = state.get("limits") or {}
    bucket = _repo_bucket(state, repo)
    _roll_window(bucket, int(state.get("window_seconds") or DEFAULT_WINDOW_SECONDS))
    consecutive = int(bucket.get("consecutive_failures") or 0)
    max_fail = int(limits.get("consecutive_failures") or DEFAULT_MAX_CONSECUTIVE_FAILURES)
    if consecutive >= max_fail:
        return GuardDecision(False, REASON_BUDGET_FAILURES, repo=repo, detail={"consecutive_failures": consecutive})
    kind_to_key = {
        "pr": ("pr_count", "pr_per_window", REASON_BUDGET_PR),
        "rerun": ("rerun_count", "reruns_per_window", REASON_BUDGET_RERUN),
        "remediation": ("remediation_count", "remediations_per_window", REASON_BUDGET_REMEDIATE),
    }
    if kind not in kind_to_key:
        raise TypeError(f"Unknown dispatch kind: {kind}")
    count_key, limit_key, reason = kind_to_key[kind]
    count = int(bucket.get(count_key) or 0)
    limit = int(limits.get(limit_key) or 0)
    if count >= limit:
        return GuardDecision(False, reason, repo=repo, detail={count_key: count, "limit": limit})
    return GuardDecision(True, REASON_OK, repo=repo, detail={count_key: count, "limit": limit})


def record_event(root: Path, repo: str, kind: str, *, failed: bool = False) -> dict[str, Any]:
    state = load_state(root)
    bucket = _repo_bucket(state, repo)
    _roll_window(bucket, int(state.get("window_seconds") or DEFAULT_WINDOW_SECONDS))
    if kind == "pr":
        bucket["pr_count"] = int(bucket.get("pr_count") or 0) + 1
    elif kind == "rerun":
        bucket["rerun_count"] = int(bucket.get("rerun_count") or 0) + 1
    elif kind == "remediation":
        bucket["remediation_count"] = int(bucket.get("remediation_count") or 0) + 1
    else:
        raise TypeError(f"Unknown dispatch kind: {kind}")
    if failed:
        bucket["consecutive_failures"] = int(bucket.get("consecutive_failures") or 0) + 1
    else:
        bucket["consecutive_failures"] = 0
    return save_state(root, state)


def status(root: Path) -> dict[str, Any]:
    return load_state(root)
