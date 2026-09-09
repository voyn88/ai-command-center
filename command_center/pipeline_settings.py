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

  Note what "conservative" means per field, because it is not always the
  default. For the switches and the concurrency caps the default *is* the
  restrictive answer, so falling back to it is safe. For `max_daily_spend_usd`
  it is the opposite — `0.0` means "no cap" — so that field does not fall back
  at all: a value that is present but unusable is carried as NaN and refuses
  dispatch. See `_spend_ceiling`.

  The same asymmetry applies one level up, to the *document*. Falling back to
  the all-off defaults is right for a file that does not exist yet and wrong
  for one that exists and cannot be read, because it launders the failure into
  a configuration: the switches read off (safe, but indistinguishable from an
  operator's own kill switch) while the spend ceiling reads `0.0`, i.e. no cap.
  `read_settings_document` therefore separates the two and raises
  `UnreadableSettings` for the latter; `load_settings` stays total for the
  display surfaces, while `update_settings` and `dispatch.service.plan` — the
  writer, and the reader that reports a cause to an operator — handle it.

Storage is `data/pipeline_settings.json`, using the same atomic-write +
sibling-lock-file convention as `execution_queue.json` and `tasks.json` (see
`command_center.storage`), so a read-modify-write from two Streamlit sessions
cannot lose an update. Deliberately *not* a `runtime.db` table: this is
operator configuration, not execution state, and ADR 0003 reserves `runtime.db`
for the latter.
"""

from __future__ import annotations

import contextlib
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

from command_center import models, storage

SETTINGS_FILE_NAME = "pipeline_settings.json"
SETTINGS_LOCK_FILE_NAME = "pipeline_settings.lock"
SETTINGS_LOCK_TIMEOUT_SECONDS = 30.0
_SETTINGS_LOCK_POLL_SECONDS = 0.05

# Conservative defaults. Two concurrent agents is what a single developer
# machine comfortably sustains; the ceiling exists so a typo (`200`) cannot
# fork-bomb the host.
DEFAULT_MAX_GLOBAL_CONCURRENCY = 2
DEFAULT_MAX_AGENT_CONCURRENCY = 2
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 16

# How many times a task whose validation failed may be relaunched
# automatically before it is left for a human. Deliberately small: an agent
# that cannot fix its own failure in two further attempts is usually facing a
# problem the prompt does not describe, and burning attempts costs real money
# without converging. 0 disables rework even when the switch is on.
DEFAULT_MAX_REWORK_ATTEMPTS = 2
MIN_REWORK_ATTEMPTS = 0
MAX_REWORK_ATTEMPTS = 5

# How many *execution* attempts the scheduler allows a task before it refuses
# to schedule further ones (`retry_exhausted`). Distinct from the rework budget
# above: that governs relaunching after a failed validation, this governs
# relaunching after a failed run. The default matches
# `scheduler.RetryPolicy.max_attempts`, which this replaces once persisted.
#
# Raising it is the honest remedy when attempts were consumed by a condition
# outside the task — an expired session, an unreachable daemon — because it
# grants a fresh attempt without rewriting the run history that recorded the
# failures.
DEFAULT_MAX_RUN_ATTEMPTS = 3
MIN_RUN_ATTEMPTS = 1
MAX_RUN_ATTEMPTS = 10

# How long an autopilot-launched agent may run before the supervisor times it
# out. `agent_runner.DEFAULT_TIMEOUT_SECONDS` (900s) was written for a single
# interactive launch; a founder audit reading a whole repository routinely
# needs more, and hitting the ceiling costs a full run's tokens for nothing.
# Exposed as a setting rather than raised in code because the right value
# depends on the work: a review is minutes, an audit is tens of minutes.
DEFAULT_RUN_TIMEOUT_SECONDS = 2700
MIN_RUN_TIMEOUT_SECONDS = 300
MAX_RUN_TIMEOUT_SECONDS = 14_400


def settings_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / SETTINGS_FILE_NAME


def settings_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / SETTINGS_LOCK_FILE_NAME


def _opt_in(value: object) -> bool:
    """`True` only for a genuine JSON boolean `true`.

    Deliberately stricter than `bool(value)`: under that, the strings
    `"false"`, `"no"` and `"0"` are all truthy, so a corrupted or
    hand-edited settings file could *enable* autopilot. Every gate in this
    module is a safety gate, so ambiguity resolves to off."""
    return value is True


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    """An integer within `[minimum, maximum]`, or `default`. A bool is rejected
    explicitly (`True` is an `int` in Python and would otherwise silently mean
    the value 1). Out-of-range falls back to `default` rather than clamping: a
    hand-edited `200` is a mistake, and silently reading it as the ceiling would
    hide that."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    if number < minimum or number > maximum:
        return default
    return number


def _spend_ceiling(data: dict, key: str, *, maximum: float) -> float:
    """The daily spend ceiling `data[key]` configures: the amount when it is
    usable money, `0.0` ("no ceiling configured") when the key is **absent**,
    and NaN when the key is present but is not usable money.

    This deliberately does *not* share `_bounded_int`'s fallback-to-default
    rule, and the asymmetry is the whole point. Falling back is safe for the
    concurrency caps because their defaults are the *restrictive* answer: a
    hand-edited `200` decaying to `2` can only ever launch less work. It is
    exactly backwards for a spend ceiling, whose default `0.0` means "no cap"
    (see the field, and every `max_daily_spend_usd > 0` guard). Under the
    fallback rule a corrupt ceiling did not degrade to something stricter, it
    *deleted the operator's cap* — and silently, since `0.0` is also what a
    never-configured ceiling reads as.

    Measured on the real `dispatch.service.plan` before this changed: a
    configured $5.00 ceiling with $100.00 already spent defers both queued
    tasks with `daily_budget_exhausted`; write that same ceiling as `"5.0"`,
    `20000`, `-5`, `null` or `NaN` and the identical call assigns 2 of 2 with
    `budget_unknown=False`.

    So corruption is *carried* rather than normalised away, exactly as
    `dispatch.models._configured_amount` carries a corrupt policy ceiling:
    NaN is a value no `>` comparison silently accepts, which routes it into
    the fail-closed gate the readers already have (`plan_dispatch` engages
    `budget_unknown`; the pipeline tick engages `spend_budget_exhausted`).

    A *missing* key stays `0.0`, so a fresh install with no settings file
    dispatches normally. JSON `null` is not in that group: it is precisely
    what `as_dict` writes for a NaN, so reading it back as unusable is what
    makes the refusal survive a save/load round trip instead of being cleared
    the next time an unrelated field is edited.

    Out-of-range is unusable rather than a fallback for the same reason: an
    operator who typed `20000` meant a large cap, and the one reading that
    must never win is "no cap at all".
    """
    if key not in data:
        return 0.0
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("nan")
    number = float(value)
    # The range test alone does not reject NaN (`NaN < 0` and `NaN > maximum`
    # are *both* False), and `json.loads` parses bare `NaN` by default.
    if not math.isfinite(number) or number < 0.0 or number > maximum:
        return float("nan")
    return number


def _json_safe_amount(value: float) -> float | None:
    """`None` for a non-finite amount, the number otherwise.

    `atomic_write_json` calls `json.dump`, which emits bare `NaN` — invalid
    JSON that `JSON.parse` rejects, so persisting an unusable ceiling verbatim
    would corrupt the settings file for every other reader. Null round-trips
    back through `_spend_ceiling` as unusable, which is the meaning that has
    to survive. Mirrors `dispatch.models._json_safe`.
    """
    return value if math.isfinite(value) else None


def _concurrency(value: object, default: int) -> int:
    """A concurrency cap within `[MIN_CONCURRENCY, MAX_CONCURRENCY]`."""
    return _bounded_int(value, default, MIN_CONCURRENCY, MAX_CONCURRENCY)


@dataclass(frozen=True)
class PipelineSettings:
    """The complete persisted opt-in surface for the pipeline.

    `enabled` is the master switch: with it off, no other field can cause any
    automatic action, which is why the `*_active` properties below — not the
    raw booleans — are what `task_pipeline` branches on. Storing the two
    switches separately (rather than collapsing them into one "autopilot"
    flag) keeps "launch queued work for me" and "merge my pull requests for
    me" as distinct decisions with distinct blast radii."""

    enabled: bool = False
    auto_launch: bool = False
    auto_merge_after_checks: bool = False
    auto_rework: bool = False
    auto_remediate_workspace: bool = False
    require_independent_review: bool = False
    max_global_concurrency: int = DEFAULT_MAX_GLOBAL_CONCURRENCY
    max_agent_concurrency: int = DEFAULT_MAX_AGENT_CONCURRENCY
    max_rework_attempts: int = DEFAULT_MAX_REWORK_ATTEMPTS
    max_run_attempts: int = DEFAULT_MAX_RUN_ATTEMPTS
    run_timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS
    # Daily agent-spend ceiling in USD, summed from the providers' own
    # result-event `total_cost_usd` over the trailing 24h. `0.0` = no budget
    # (off, the default). Gates NEW launches only — running work finishes.
    #
    # NaN = "a ceiling is configured but cannot be read as money". Because
    # `0.0` here means *no cap*, an unusable value must not decay to it; see
    # `_spend_ceiling`. Both readers treat NaN as a refusal to launch rather
    # than as an absent ceiling.
    max_daily_spend_usd: float = 0.0
    updated_at: str | None = None
    updated_by: str | None = None

    @property
    def independent_review_active(self) -> bool:
        """Whether a blocking independent review must approve a change before
        any pull request is opened for it.

        Requires only the master switch, not auto-launch: a *closed* gate is
        meaningful on its own — with auto-launch off the completion simply waits
        in `AWAITING_REVIEW` for a reviewer the operator starts, which is still
        stricter than opening a pull request unreviewed."""
        return self.enabled and self.require_independent_review

    @property
    def auto_remediate_workspace_active(self) -> bool:
        """Whether the pipeline may tidy a workspace it owns so a task that
        would otherwise never start can start.

        Scope is deliberately narrow and non-destructive: leftovers in a *linked
        worktree of the task's own project repository* are stashed (recoverable
        via `git stash list`), never discarded. A human's primary working tree,
        and any repository that is not the project's, are never touched — see
        `workspace_provisioning.is_pipeline_owned_worktree`. Requires
        auto-launch, because the only reason to tidy is to launch."""
        return self.auto_launch_active and self.auto_remediate_workspace

    @property
    def auto_rework_active(self) -> bool:
        """Whether a task whose validation failed may be relaunched
        automatically as a new attempt, carrying the failure output into its
        prompt. Same both-switches rule as `auto_launch_active` — and rework
        additionally requires `auto_launch_active`, because a rework *is* a
        launch: enabling "fix it again" while "start work for me" is off would
        be a contradiction, and the more restrictive answer is the safe one."""
        return self.auto_launch_active and self.auto_rework

    @property
    def auto_launch_active(self) -> bool:
        """Whether a tick may actually start processes. Requires *both* the
        master switch and the launch switch — a single `auto_launch=true` left
        in the file by an earlier experiment can never launch anything on its
        own."""
        return self.enabled and self.auto_launch

    @property
    def auto_merge_active(self) -> bool:
        """Whether newly-seeded completion rows may be given an auto-merge
        policy. Same both-switches rule as `auto_launch_active`. Note this only
        governs *policy assignment*: the checks/review/mergeability gates in
        `runtime.completion` remain authoritative over whether a merge actually
        happens."""
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
            "max_daily_spend_usd": _json_safe_amount(self.max_daily_spend_usd),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_dict(cls, data: object) -> "PipelineSettings":
        """Total and fail-closed: anything that is not a dict of recognized,
        well-typed values yields the all-off defaults."""
        if not isinstance(data, dict):
            return cls()
        updated_at = data.get("updated_at")
        updated_by = data.get("updated_by")
        return cls(
            enabled=_opt_in(data.get("enabled")),
            auto_launch=_opt_in(data.get("auto_launch")),
            auto_merge_after_checks=_opt_in(data.get("auto_merge_after_checks")),
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
            max_daily_spend_usd=_spend_ceiling(
                data, "max_daily_spend_usd", maximum=10_000.0
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
    """Cross-process mutual exclusion for the settings read-modify-write cycle
    — same OS advisory-lock primitive as `execution_queue.queue_lock`."""
    with storage.file_lock(
        settings_lock_path(root), timeout=timeout, poll_seconds=_SETTINGS_LOCK_POLL_SECONDS
    ):
        yield


class UnreadableSettings(RuntimeError):
    """The settings file exists but could not be turned into a settings
    document — an OS error, malformed JSON, or a document that is not a JSON
    object.

    Worth a distinct exception, rather than the all-off defaults every reader
    used to get, because those defaults are only the *safe* answer to one of
    the two questions they currently answer. As "nothing has been configured
    yet" they are exactly right: every switch off, nothing automatic can
    happen. As "the configuration could not be read" they are a fabrication,
    and one that runs in both directions at once:

    * the switches decay to *off*, which is safe but **misattributed** — a
      reader sees `enabled=False` and reports the operator's master kill
      switch as engaged, when in fact the file that would have said so is
      unreadable. The remedy the operator is then pointed at is the wrong one;
    * `max_daily_spend_usd` decays to `0.0`, which is **not** safe, because
      `0.0` here means *no cap* (see `_spend_ceiling`). The same laundering
      that turns the switches restrictive turns the spend ceiling permissive.

    Put together they compose into a fail-open path with no corrupt data left
    anywhere in it. Measured on the real modules: an operator running
    `enabled=True, max_daily_spend_usd=5.0, max_global_concurrency=1` suffers a
    torn write; `dispatch.service.plan` reports `kill_switch_engaged`; the
    operator turns the switch back on; `update_settings` merges that one change
    onto the laundered defaults and **persists** `max_daily_spend_usd: 0.0` and
    `max_global_concurrency: 2`, stamped `updated_by` with their own name as
    though they had asked for it. From that write on, every gate reads healthy
    and dispatch is unbounded — the ceiling is not bypassed, it is *gone*.

    This is the same defect `dispatch.policy_config.UnreadablePolicy` closes
    for `dispatch_policy.json`, on the file that actually holds the ceiling
    this ticket is about. As there, a *missing* file is still the defaults —
    that is a fresh install, not a failure.
    """


def read_settings_document(root: Path) -> dict:
    """The settings file's JSON object, `{}` when nothing has been saved yet.

    Fails closed on everything in between; see `UnreadableSettings`. Read
    directly rather than through `storage.read_json`, whose swallow-and-default
    is right for a display surface and wrong for a guardrail: it collapses
    "absent", "malformed" and "unreadable" into one value before `from_dict`
    can tell them apart.

    An existing but empty file counts as unreadable rather than unconfigured:
    writes go through `storage.atomic_write_json`, which never produces a
    zero-byte settings file, so emptiness is a torn write rather than a state
    an operator can legitimately have asked for.
    """
    path = settings_file_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise UnreadableSettings(
            f"pipeline settings at {path} could not be read: {exc}"
        ) from exc
    if not raw.strip():
        raise UnreadableSettings(
            f"pipeline settings at {path} are empty; an atomically-written "
            "settings file is never zero bytes, so this is a torn write, not "
            "an unconfigured pipeline"
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise UnreadableSettings(
            f"pipeline settings at {path} are not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise UnreadableSettings(
            f"pipeline settings at {path} are a {type(document).__name__}, not "
            "a JSON object; they cannot express a settings document"
        )
    return document


def load_settings(root: Path) -> PipelineSettings:
    """Read the persisted settings, or the all-off defaults if nothing has been
    saved yet. Unlocked by design (a plain read of an atomically-written file);
    use `update_settings` for anything that writes.

    Deliberately **total**: an unreadable file yields the all-off defaults, so
    the display surfaces and the pipeline tick keep the fail-closed behaviour
    they already have (`enabled=False` disables every automatic action). What
    it cannot do is tell its caller *why* everything is off. A caller for which
    that distinction is load-bearing — because it reports the reason to an
    operator, or because it is about to write the laundered values back — reads
    `read_settings_document` directly and handles `UnreadableSettings`. Both
    such callers exist today: `dispatch.service.plan` and `update_settings`.
    """
    try:
        document = read_settings_document(root)
    except UnreadableSettings:
        document = {}
    return PipelineSettings.from_dict(document)


def save_settings(root: Path, settings: PipelineSettings) -> PipelineSettings:
    """Persist `settings` wholesale under `settings_lock`. Prefer
    `update_settings` when changing individual fields, so a concurrent writer's
    unrelated change is not discarded."""
    with settings_lock(root):
        storage.atomic_write_json(settings_file_path(root), settings.as_dict())
    return settings


def update_settings(root: Path, *, actor: str | None = None, **changes) -> PipelineSettings:
    """Lost-update-safe partial update: re-reads the current on-disk settings
    under `settings_lock`, applies `changes`, stamps `updated_at`/`updated_by`,
    and writes back — so toggling auto-merge in one session never reverts a
    concurrency change made in another.

    Unknown field names raise `TypeError` rather than being silently dropped: a
    typo'd `auto_merge=True` must not read as "auto-merge is enabled" while
    persisting nothing. Values pass through the same fail-closed coercion as
    `from_dict`.

    Raises `UnreadableSettings` rather than merging onto the all-off defaults
    when the current file is present but unusable, and that matters more here
    than on any read path: this is the call that makes a *recoverable* torn
    file permanently guardrail-free. The merge base would be the laundered
    defaults, so the write persists `max_daily_spend_usd: 0.0` — which means
    **no cap**, not the operator's ceiling — plus the default concurrency caps,
    over the top of whatever was actually configured, and stamps `updated_by`
    with the authenticated operator as if they had asked for it. Toggling one
    switch is not consent to drop every limit the file used to hold. The remedy
    is `save_settings`, which states the whole document explicitly instead of
    inheriting the unreadable part. Mirrors
    `dispatch.policy_config.update_policy`."""
    unknown = set(changes) - {
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
    }
    if unknown:
        raise TypeError(f"Unknown pipeline setting(s): {', '.join(sorted(unknown))}")

    with settings_lock(root):
        current = PipelineSettings.from_dict(read_settings_document(root))
        merged = PipelineSettings.from_dict({**current.as_dict(), **changes})
        updated = replace(merged, updated_at=models.iso_now(), updated_by=actor)
        storage.atomic_write_json(settings_file_path(root), updated.as_dict())
    return updated
