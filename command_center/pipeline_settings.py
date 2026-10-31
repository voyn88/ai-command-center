"""Persisted, explicitly opted-in settings for the desktop task pipeline
(`command_center.task_pipeline`).

Two things make this module worth existing rather than reading a few keys out
of `st.session_state`:

- **Persistence is the point.** "Autopilot is on" must survive a Streamlit
  rerun, a page switch, a browser refresh, and an app restart — otherwise the
  operator cannot tell whether the machine is currently allowed to launch work
  on their behalf. Session state answers "what did this browser tab do
  recently"; this file answers "what is this machine permitted to do", which
  is the question the safety invariants are written about.
- **Fail-closed parsing.** Every gate here defaults to *off*, and a value that
  is not exactly a JSON boolean `true` reads as `False` (see `_opt_in`). A
  hand-edited or half-written `pipeline_settings.json` can therefore only ever
  *disable* automation, never silently enable it. The same rule applies to the
  concurrency caps: an unparseable or out-of-range value falls back to the
  conservative default rather than being clamped up from garbage.

Storage is `data/pipeline_settings.json`, using the same atomic-write +
sibling-lock-file convention as `execution_queue.json` and `tasks.json` (see
`command_center.storage`), so a read-modify-write from two Streamlit sessions
cannot lose an update. Deliberately *not* a `runtime.db` table: this is
operator configuration, not execution state, and ADR 0003 reserves `runtime.db`
for the latter.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, replace
from pathlib import Path

from command_center import models, storage

SETTINGS_FILE_NAME = "pipeline_settings.json"
SETTINGS_LOCK_FILE_NAME = "pipeline_settings.lock"
SETTINGS_LOCK_TIMEOUT_SECONDS = 30.0
_SETTINGS_LOCK_POLL_SECONDS = 0.05

DEFAULT_MAX_GLOBAL_CONCURRENCY = 2
DEFAULT_MAX_AGENT_CONCURRENCY = 2
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 16

DEFAULT_MAX_REWORK_ATTEMPTS = 2
MIN_REWORK_ATTEMPTS = 0
MAX_REWORK_ATTEMPTS = 5

DEFAULT_MAX_RUN_ATTEMPTS = 3
MIN_RUN_ATTEMPTS = 1
MAX_RUN_ATTEMPTS = 10

DEFAULT_RUN_TIMEOUT_SECONDS = 2700
MIN_RUN_TIMEOUT_SECONDS = 300
MAX_RUN_TIMEOUT_SECONDS = 14_400

# Fields accepted by update_settings. Keep in lockstep with PipelineSettings.
_UPDATABLE_FIELDS = frozenset(
    {
        "enabled",
        "auto_launch",
        "auto_merge_after_checks",
        "auto_rework",
        "auto_remediate_workspace",
        "require_independent_review",
        "max_global_concurrency",
        "max_agent_concurrency",
        "max_rework_attempts",
        "max_run_attempts",
        "run_timeout_seconds",
        "max_daily_spend_usd",
    }
)


def settings_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / SETTINGS_FILE_NAME


def settings_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / SETTINGS_LOCK_FILE_NAME


def _opt_in(value: object) -> bool:
    return value is True


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    if number < minimum or number > maximum:
        return default
    return number


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    """A float within `[minimum, maximum]`, or `default`; bools rejected like
    `_bounded_int`, out-of-range falls back rather than clamping.

    Non-finite values fall back too, and NaN is why that is checked rather
    than left to the range comparison: `nan < minimum` and `nan > maximum`
    are *both* False, so a NaN is the one malformed value that sails through
    a bounds test which rejects every other. `load_settings` reaches here via
    `json.loads`, which accepts a bare `NaN` literal, so it is a value a
    settings file can genuinely carry.

    The one field this guards is `max_daily_spend_usd`. A NaN ceiling gates
    no differently than the `0.0` default does (both mean "no cap"), so this
    is not the money gate fail-open `daily_spend_usd` refuses to return a NaN
    for (VOYN-W0-AICC-SPEND-CAP-CALLER-SWALLOWS) — it is the stored value
    being malformed rather than replaced. What that costs: it is written
    straight back out by `as_dict`/`save_settings` as a bare `NaN` token,
    which is not valid JSON and is rejected by any strict reader of this file
    (the PostgreSQL mirror, the web client); it renders as `nan` wherever the
    ceiling is displayed; and it makes every comparison against the ceiling
    False, including the `max_daily_spend_usd <= 0` test in
    `DispatchPlan.budget_remaining_usd`, which then reports a NaN budget
    instead of "no ceiling". Falling back makes a malformed ceiling behave
    like every other malformed setting. `inf` is already rejected by the range
    test; it is covered here only so "a bound is a real number" holds for one
    reason rather than two.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    if number < minimum or number > maximum:
        return default
    return number


def _concurrency(value: object, default: int) -> int:
    return _bounded_int(value, default, MIN_CONCURRENCY, MAX_CONCURRENCY)


@dataclass(frozen=True)
class PipelineSettings:
    enabled: bool = False
    auto_launch: bool = False
    auto_merge_after_checks: bool = True  # founder 2026-09-27: merge after checks
    auto_rework: bool = False
    auto_remediate_workspace: bool = False
    require_independent_review: bool = False
    max_global_concurrency: int = DEFAULT_MAX_GLOBAL_CONCURRENCY
    max_agent_concurrency: int = DEFAULT_MAX_AGENT_CONCURRENCY
    max_rework_attempts: int = DEFAULT_MAX_REWORK_ATTEMPTS
    max_run_attempts: int = DEFAULT_MAX_RUN_ATTEMPTS
    run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS
    max_daily_spend_usd: float = 0.0
    updated_at: str | None = None
    updated_by: str | None = None

    @property
    def independent_review_active(self) -> bool:
        return self.enabled and self.require_independent_review

    @property
    def auto_remediate_workspace_active(self) -> bool:
        return self.auto_launch_active and self.auto_remediate_workspace

    @property
    def auto_rework_active(self) -> bool:
        return self.auto_launch_active and self.auto_rework

    @property
    def auto_launch_active(self) -> bool:
        return self.enabled and self.auto_launch

    @property
    def auto_merge_active(self) -> bool:
        return self.enabled and self.auto_merge_after_checks

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "auto_launch": self.auto_launch,
            "auto_merge_after_checks": self.auto_merge_after_checks,
            "auto_rework": self.auto_rework,
            "auto_remediate_workspace": self.auto_remediate_workspace,
            "require_independent_review": self.require_independent_review,
            "max_global_concurrency": self.max_global_concurrency,
            "max_agent_concurrency": self.max_agent_concurrency,
            "max_rework_attempts": self.max_rework_attempts,
            "max_run_attempts": self.max_run_attempts,
            "run_timeout_seconds": self.run_timeout_seconds,
            "max_daily_spend_usd": self.max_daily_spend_usd,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_dict(cls, data: object) -> "PipelineSettings":
        if not isinstance(data, dict):
            return cls()
        updated_at = data.get("updated_at")
        updated_by = data.get("updated_by")
        return cls(
            enabled=_opt_in(data.get("enabled")),
            auto_launch=_opt_in(data.get("auto_launch")),
            auto_merge_after_checks=(
                _opt_in(data["auto_merge_after_checks"])
                if "auto_merge_after_checks" in data
                else True
            ),
            max_global_concurrency=_concurrency(
                data.get("max_global_concurrency"), DEFAULT_MAX_GLOBAL_CONCURRENCY
            ),
            max_agent_concurrency=_concurrency(
                data.get("max_agent_concurrency"), DEFAULT_MAX_AGENT_CONCURRENCY
            ),
            auto_rework=_opt_in(data.get("auto_rework")),
            auto_remediate_workspace=_opt_in(data.get("auto_remediate_workspace")),
            require_independent_review=_opt_in(data.get("require_independent_review")),
            max_rework_attempts=_bounded_int(
                data.get("max_rework_attempts"),
                DEFAULT_MAX_REWORK_ATTEMPTS,
                MIN_REWORK_ATTEMPTS,
                MAX_REWORK_ATTEMPTS,
            ),
            max_run_attempts=_bounded_int(
                data.get("max_run_attempts"),
                DEFAULT_MAX_RUN_ATTEMPTS,
                MIN_RUN_ATTEMPTS,
                MAX_RUN_ATTEMPTS,
            ),
            max_daily_spend_usd=_bounded_float(
                data.get("max_daily_spend_usd"), 0.0, 0.0, 10_000.0
            ),
            run_timeout_seconds=_bounded_int(
                data.get("run_timeout_seconds"),
                DEFAULT_RUN_TIMEOUT_SECONDS,
                MIN_RUN_TIMEOUT_SECONDS,
                MAX_RUN_TIMEOUT_SECONDS,
            ),
            updated_at=updated_at if isinstance(updated_at, str) else None,
            updated_by=updated_by if isinstance(updated_by, str) else None,
        )


@contextlib.contextmanager
def settings_lock(root: Path, *, timeout: float = SETTINGS_LOCK_TIMEOUT_SECONDS):
    with storage.file_lock(
        settings_lock_path(root), timeout=timeout, poll_seconds=_SETTINGS_LOCK_POLL_SECONDS
    ):
        yield


def load_settings(root: Path) -> PipelineSettings:
    return PipelineSettings.from_dict(storage.read_json(settings_file_path(root), {}))


def save_settings(root: Path, settings: PipelineSettings) -> PipelineSettings:
    with settings_lock(root):
        storage.atomic_write_json(settings_file_path(root), settings.as_dict())
    return settings


def update_settings(root: Path, *, actor: str | None = None, **changes) -> PipelineSettings:
    unknown = set(changes) - _UPDATABLE_FIELDS
    if unknown:
        raise TypeError(f"Unknown pipeline setting(s): {', '.join(sorted(unknown))}")

    with settings_lock(root):
        current = PipelineSettings.from_dict(storage.read_json(settings_file_path(root), {}))
        merged = PipelineSettings.from_dict({**current.as_dict(), **changes})
        updated = replace(merged, updated_at=models.iso_now(), updated_by=actor)
        storage.atomic_write_json(settings_file_path(root), updated.as_dict())
    return updated
