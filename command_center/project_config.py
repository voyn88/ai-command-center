"""Project configuration: repository paths, sensitivity, allowed agents, context files.

Local, machine-specific configuration (currently: a project's repository path) is stored
in `data/project_config.json`, which is gitignored — see `.gitignore`. A tracked
`data/project_config.example.json` documents the shape without any personal absolute
path. Everything else (display name, sensitivity, context files, reports/generated
directories) is derived in code from the project id, matching the existing
`PROJECTS`/`CONTEXT_FILES` tables in `app.py`.

Repository paths are never invented. `discover_candidate_repository_path` only ever
returns a path that is independently verified to exist and to be a git repository on
this machine — and even then, it is surfaced as a *suggestion* in the settings UI that
the user must explicitly save. Nothing under this module writes a repository path
without an explicit call from the UI layer.
"""

from __future__ import annotations

from pathlib import Path

from command_center import git_info, models, storage

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = storage.resolve_data_dir(ROOT)
CONFIG_FILE = DATA_DIR / "project_config.json"
# Dedicated lock file guarding the config read-modify-write (audit AR-5).
CONFIG_LOCK_FILE = DATA_DIR / "project_config.lock"
CONFIG_EXAMPLE_FILE = DATA_DIR / "project_config.example.json"

# Engineering-environment defaults a new task inherits from its project (see
# `task_defaults_from_project`), plus the descriptive/planning fields the
# Projects settings UI exposes. All are optional overrides layered onto
# `default_project_config` by `load_project_configs` — a project missing any
# of these in `project_config.json` just keeps the built-in default below,
# which is how pre-existing project configs (only `repository_path`/
# `default_workspace_path`) keep loading unchanged.
OVERRIDABLE_FIELDS: list[str] = [
    "repository_path",
    "default_workspace_path",
    "default_branch",
    "default_executor",
    "default_prompt",
    "allowed_agents",
    "description",
    "status",
    "priority",
    "progress",
    "current_sprint",
    "current_milestone",
    "owner",
    # Autonomous completion pipeline policy (AICC-AUTONOMY-001). All optional;
    # a project config that omits them keeps the conservative defaults below.
    "merge_mode",
    "merge_method",
    "allow_pr_recovery",
    "requires_pull_request",
    "allow_local_only",
    "validation_required",
    "validation_commands",
    "max_retries",
]

PROJECT_STATUSES: list[str] = ["Planning", "Active", "Paused", "Blocked", "Done"]

# Merge mode/method vocabulary the completion pipeline understands (mirrors
# `command_center.runtime.completion`, kept independent so a `command_center`
# module never imports a `runtime` one). Defaults are deliberately conservative:
# manual merge, squash method, no automatic PR recovery.
MERGE_MODES: list[str] = ["manual", "auto_after_checks", "auto_after_checks_and_review"]
MERGE_METHODS: list[str] = ["squash", "merge", "rebase"]

# Same value set as `app.py`'s task-level `PRIORITIES` — kept as an independent
# constant here (rather than imported from `app.py`, which must never be
# imported by a `command_center` module) since a project's priority and a
# task's priority are conceptually separate fields that merely happen to
# share a vocabulary today.
PROJECT_PRIORITIES: list[str] = ["Low", "Medium", "High", "Critical"]

DISPLAY_NAMES: dict[str, str] = {
    "AICC": "AI Command Center",
    "AIOS": "AIOS",
    "AICOS": "AICOS",
    "PRODUCT": "AIOS Product",
    "ECOSYSTEM": "Ecosystem",
    "ESF": "ESF Корпоративная платформа",
    "AML": "AML Платформа управления рисками",
    "CRM": "CRM VOYN",
    "BANK": "Bank Strategy",
    "LEGAL": "Legal",
    "BUSINESS": "Business",
    "PERSONAL": "Personal",
}

# Relative to ROOT. Empty/missing entries just mean "no dedicated file yet" — the UI
# handles that gracefully rather than failing, matching the v1.1 behavior.
#
# Every key in `models.PROJECT_IDS` must appear here, even if the file doesn't
# exist on disk yet (AICC/AICOS/PRODUCT/ECOSYSTEM have none today) — this dict is
# consulted with `.get(project_id, ...)` everywhere, so a missing *entry* silently
# degrades to a generic guess instead of surfacing the gap. Omitting AICOS entirely
# was the root cause of a real bug: `app.py` used to keep its own second,
# hand-maintained project dict (not this one) that dropped AICOS from every
# project selector/filter in the app. See `models.PROJECT_IDS` for the single
# canonical id list `app.py` must iterate instead.
PROJECT_STATUS_FILES: dict[str, str] = {
    "AICC": "projects/AICC.md",
    "AIOS": "projects/AIOS.md",
    "AICOS": "projects/AICOS.md",
    "PRODUCT": "projects/PRODUCT.md",
    "ECOSYSTEM": "projects/ECOSYSTEM.md",
    "ESF": "projects/ESF.md",
    "AML": "projects/AML.md",
    "CRM": "projects/CRM.md",
    "BANK": "projects/BANK_STRATEGY.md",
    "LEGAL": "projects/LEGAL.md",
    "BUSINESS": "projects/BUSINESS.md",
    "PERSONAL": "projects/PERSONAL.md",
}

CONTEXT_FILES: dict[str, str] = {
    "AIOS": "context/AIOS_CONTEXT.md",
    "BANK": "context/BANK_CONTEXT.md",
    "LEGAL": "context/LEGAL_CONTEXT.md",
}

GLOBAL_CONTEXT_FILES: list[str] = ["CURRENT_STATE.md"]

DEFAULT_ALLOWED_AGENTS: list[str] = ["claude_code"]
EXECUTION_PROVIDER_IDS: frozenset[str] = frozenset({"claude_code", "codex", "ollama", "copilot_cli"})


class ProviderAuthorizationError(ValueError):
    """The selected execution provider is not authorized for the project."""


def default_project_config(project_id: str) -> dict:
    context_paths = [
        path
        for path in (PROJECT_STATUS_FILES.get(project_id), CONTEXT_FILES.get(project_id))
        if path
    ] + list(GLOBAL_CONTEXT_FILES)
    return {
        "id": project_id,
        "display_name": DISPLAY_NAMES.get(project_id, project_id),
        "repository_path": None,
        # Optional per-project override consulted by `command_center.launch.
        # resolve_workspace_path` between a task's own `workspace_path` and the
        # `repository_path` fallback below. `None` unless explicitly set in
        # `project_config.json` — no existing project config needs to define it.
        "default_workspace_path": None,
        # Engineering-environment defaults a new task inherits at creation time
        # (see `task_defaults_from_project`) — same "unset unless explicitly
        # configured" contract as `default_workspace_path` above.
        "default_branch": None,
        "default_executor": None,
        "default_prompt": "",
        # Descriptive/planning fields surfaced in the Projects settings UI.
        # Purely informational — nothing in Launch or task inheritance reads
        # these — so their defaults are just sensible blanks.
        "description": "",
        "status": PROJECT_STATUSES[1],  # "Active"
        "priority": PROJECT_PRIORITIES[1],  # "Medium"
        "progress": 0,
        "current_sprint": None,
        "current_milestone": None,
        "owner": "",
        "allowed_agents": list(DEFAULT_ALLOWED_AGENTS),
        "sensitive": project_id in models.SENSITIVE_PROJECT_IDS,
        "context_file_paths": context_paths,
        "reports_dir": f"reports/{project_id}",
        "generated_dir": f"generated/{project_id}",
        # Autonomous completion pipeline policy — conservative defaults. The
        # pipeline never auto-merges and never auto-recovers a closed PR unless
        # a project explicitly opts in via `project_config.json`. `merge_method`
        # governs how an auto-merge (when enabled) is performed; `validation_
        # commands=None` uses the safe built-in default (byte-compile).
        "merge_mode": "manual",
        "merge_method": "squash",
        "allow_pr_recovery": False,
        "requires_pull_request": True,
        "allow_local_only": False,
        "validation_required": True,
        "validation_commands": None,
        "max_retries": 20,
    }


def discover_candidate_repository_path(project_id: str) -> str | None:
    """Return a verified-existing git repository path for `project_id`, or None.

    This never guesses: the candidate must exist on disk *and* contain a `.git`
    directory before it is returned. Callers must still treat the result as a
    suggestion requiring explicit user confirmation, never as configuration.
    """
    candidates: dict[str, Path] = {
        "AIOS": Path.home() / "Projects" / "aios",
        "CRM": Path.home() / "Projects" / "voyn-logistics-crm",
    }
    candidate = candidates.get(project_id)
    if candidate is None:
        return None
    if candidate.is_dir() and (candidate / ".git").is_dir():
        return str(candidate)
    return None


def _read_overrides() -> dict:
    overrides = storage.read_json(CONFIG_FILE, {})
    return overrides if isinstance(overrides, dict) else {}


def load_project_configs() -> dict[str, dict]:
    overrides = _read_overrides()
    configs: dict[str, dict] = {}
    for project_id in models.PROJECT_IDS:
        cfg = default_project_config(project_id)
        override = overrides.get(project_id)
        if isinstance(override, dict):
            for field in OVERRIDABLE_FIELDS:
                if override.get(field) not in (None, ""):
                    cfg[field] = override[field]
        configs[project_id] = cfg
    return configs


def get_project_config(project_id: str) -> dict:
    return load_project_configs().get(project_id, default_project_config(project_id))


def allowed_execution_providers(project_id: str) -> tuple[str, ...]:
    """Return the project's explicitly authorized execution providers.

    Missing policy is the legacy-safe Claude-only default. Malformed policy
    and unknown identifiers fail closed instead of silently broadening access.
    """
    cfg = get_project_config(project_id)
    raw = cfg.get("allowed_agents")
    if raw is None:
        return ("claude_code",)
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
        raise ProviderAuthorizationError(
            f"Project {project_id!r} has malformed allowed_agents policy; execution is disabled."
        )
    unknown = sorted(set(raw) - EXECUTION_PROVIDER_IDS)
    if unknown:
        raise ProviderAuthorizationError(
            f"Project {project_id!r} authorizes unknown execution provider(s): {unknown!r}."
        )
    return tuple(dict.fromkeys(raw))


def require_execution_provider_allowed(project_id: str, provider_id: str) -> None:
    allowed = allowed_execution_providers(project_id)
    if provider_id not in allowed:
        raise ProviderAuthorizationError(
            f"Execution provider {provider_id!r} is not authorized for project {project_id!r}."
        )


def save_allowed_agents(project_id: str, allowed_agents: list[str]) -> None:
    """Persist an explicit provider allow-list after validating it fail-closed."""
    if project_id not in models.PROJECT_IDS:
        raise ValueError(f"Unknown project: {project_id!r}")
    if (
        not isinstance(allowed_agents, list)
        or not allowed_agents
        or any(not isinstance(item, str) or not item for item in allowed_agents)
    ):
        raise ProviderAuthorizationError("allowed_agents must be a non-empty list of provider identifiers.")
    unknown = sorted(set(allowed_agents) - EXECUTION_PROVIDER_IDS)
    if unknown:
        raise ProviderAuthorizationError(f"Unknown execution provider(s): {unknown!r}.")
    # Lock the whole read-modify-write so a concurrent config writer can't
    # lost-update this change (audit AR-5).
    with storage.file_lock(CONFIG_LOCK_FILE):
        overrides = _read_overrides()
        project_override = overrides.get(project_id)
        if not isinstance(project_override, dict):
            project_override = {}
        project_override["allowed_agents"] = list(dict.fromkeys(allowed_agents))
        overrides[project_id] = project_override
        storage.atomic_write_json(CONFIG_FILE, overrides)


def validate_repository_path(path_str: str) -> tuple[bool, str]:
    """Validate a candidate path before it may be saved as a project's repository path."""
    if not path_str or not path_str.strip():
        return False, "Путь не указан."
    path = Path(path_str.strip()).expanduser()
    if not path.is_absolute():
        return False, "Укажите абсолютный путь."
    if not path.exists():
        return False, f"Путь не существует: {path}"
    if not path.is_dir():
        return False, f"Путь не является директорией: {path}"
    return True, "OK"


def save_repository_path(project_id: str, repository_path: str | None) -> None:
    if project_id not in models.PROJECT_IDS:
        raise ValueError(f"Unknown project: {project_id}")
    # Lock the whole read-modify-write (audit AR-5).
    with storage.file_lock(CONFIG_LOCK_FILE):
        overrides = _read_overrides()
        entry = overrides.get(project_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if repository_path:
            entry["repository_path"] = repository_path
        else:
            entry.pop("repository_path", None)
        overrides[project_id] = entry
        storage.atomic_write_json(CONFIG_FILE, overrides)


def is_sensitive(project_id: str) -> bool:
    return project_id in models.SENSITIVE_PROJECT_IDS


# --------------------------------------------------------------------------
# Project name normalization (task import)
# --------------------------------------------------------------------------

# Founder-authored task packages (see `command_center.task_import`) refer to
# projects by free-text names, not by `models.PROJECT_IDS`. `PROJECT_NAME_ALIASES`
# (built just below from `DISPLAY_NAMES`) maps every name a package is allowed to
# use onto a canonical id — deliberately not a fallback/default: a name absent
# from it fails validation in `task_import.validate_task_package` rather than
# being silently assigned to a guessed project.
#
# Founder Review (AICC-AUDIT-001 remediation) established the invariant that no
# two distinct entities may fold into one id ("AI Command Center"/"Ecosystem"
# must NOT collapse into AICOS, "AIOS Product" must NOT collapse into AIOS).
# Deriving the table straight from `DISPLAY_NAMES` preserves that by
# construction: each display name maps to its own id and to nothing else.
def _alias_key(name: str) -> str:
    """The single case/whitespace-insensitive normalization applied to both
    every alias-table key and every lookup in `normalize_project_id`, so the
    two can never disagree about how a name is matched."""
    return " ".join(name.strip().lower().split())


# Extra names founder task packages are historically allowed to use that are
# NOT a project's display name — merged on top of the auto-derived table below.
# Kept deliberately tiny: the display-name reverse mapping and the bare
# canonical id are derived automatically in `_build_project_name_aliases`, so
# this dict only ever needs a truly *aliased* spelling. (Empty today — every
# supported name is either a display name or a bare id — but preserved as the
# explicit merge point the module contract promises.)
_EXPLICIT_PROJECT_ALIASES: dict[str, str] = {}


def _build_project_name_aliases() -> dict[str, str]:
    """Derive the free-text-name -> canonical-id table from `DISPLAY_NAMES`
    (the single source of truth for a project's human name) rather than hand-
    maintaining a second, drift-prone list. For every `models.PROJECT_IDS`
    entry this registers both its display name (`"AI Command Center"` ->
    `AICC`, `"Bank Strategy"` -> `BANK`) and its bare id (`"aicc"` -> `AICC`),
    then merges `_EXPLICIT_PROJECT_ALIASES`. Guarantees every project — not
    just the five that used to be listed — resolves from its display name, so
    `canonical_project_id`'s "a display name matches its canonical id" promise
    holds for all nine projects, not a subset."""
    aliases: dict[str, str] = {}
    for project_id in models.PROJECT_IDS:
        aliases[_alias_key(DISPLAY_NAMES.get(project_id, project_id))] = project_id
        aliases[_alias_key(project_id)] = project_id
    for name, project_id in _EXPLICIT_PROJECT_ALIASES.items():
        aliases[_alias_key(name)] = project_id
    return aliases


# Founder-authored task packages (see `command_center.task_import`) refer to
# projects by free-text names, not by `models.PROJECT_IDS`. This table maps
# every name a package is allowed to use onto a canonical id already registered
# in `models.PROJECT_IDS` — it is deliberately not a fallback/default: a name
# absent from this table fails validation in `task_import.validate_task_package`
# rather than being silently assigned to a guessed project. It is *derived* from
# `DISPLAY_NAMES` (see `_build_project_name_aliases`) so it can never drift out
# of sync with the display names the UI renders.
#
# Every name maps to its OWN canonical id, never folding distinct entities into
# one — the invariant Founder Review (AICC-AUDIT-001 remediation) established:
#   "AI Command Center" -> AICC   "AIOS Product" -> PRODUCT   "Ecosystem" -> ECOSYSTEM
#   "Bank Strategy" -> BANK        "Legal" -> LEGAL            "Business" -> BUSINESS ...
PROJECT_NAME_ALIASES: dict[str, str] = _build_project_name_aliases()


def normalize_project_id(raw: str | None) -> str | None:
    """Resolve a task package's free-text `project` field to a canonical
    `models.PROJECT_IDS` entry, or `None` if it cannot be resolved.

    Already-canonical ids pass through unchanged; every other supported name
    is looked up case/whitespace-insensitively in `PROJECT_NAME_ALIASES`.
    Returning `None` on a miss (rather than guessing a default project) is
    deliberate — see the module-level note above.
    """
    if not raw or not raw.strip():
        return None
    if raw in models.PROJECT_IDS:
        return raw
    return PROJECT_NAME_ALIASES.get(_alias_key(raw))


def canonical_project_id(value: str | None) -> str | None:
    """The single canonical-id function every project-scoped filter, counter,
    ranking, and grouping in the app must use to compare a task's/run's stored
    `project` against a selected project id.

    It is `normalize_project_id` with a raw-value fallback: a resolvable name
    (canonical id, display name, or alias — `"AICC"`, `"AI Command Center"`,
    `"ai command center"`) collapses to its canonical `models.PROJECT_IDS` id,
    while a genuinely unknown value falls back to itself so it still matches
    only itself exactly and is never silently broadened onto a real lane.

    Crucially this is the identity on already-canonical ids, so applying it at
    a comparison site that already only ever saw canonical ids changes nothing
    there — it only *additionally* makes display-name/alias values line up.
    That is why it can be dropped in at every project comparison uniformly.

    Root cause it fixes (AICC-UI-001): eight tasks store a display name
    (`AICC-CI-001` -> `"AI Command Center"`) while the project selector emits
    the canonical id (`"AICC"`). Comparing raw strings dropped those tasks from
    their own project lane, undercounted every per-project pill/metric, and
    excluded them from recommendations — present in `tasks.json`, invisible to
    anyone working inside their project. Normalizing both sides of every such
    comparison through this one helper keeps the Kanban lane, the pill count,
    the project-intelligence strip, and the recommendation inputs in exact
    agreement.
    """
    return normalize_project_id(value) or value


def project_matches(stored: str | None, selected: str | None) -> bool:
    """Whether a task/run whose stored `project` is `stored` belongs to the
    `selected` project filter. `selected is None` means "all projects" and
    always matches. Otherwise both sides are compared on canonical ids via
    `canonical_project_id`, so a display name matches its canonical id."""
    if selected is None:
        return True
    return canonical_project_id(stored) == canonical_project_id(selected)


def save_project_settings(project_id: str, **fields: object) -> None:
    """Generic setter for any subset of `OVERRIDABLE_FIELDS` — the Projects
    settings UI's single save action for Default Workspace/Branch/Executor/
    Prompt/Description/Status/Priority/Progress/Sprint/Milestone/Owner.

    `save_repository_path` above is kept as its own dedicated function (its
    existing call sites and tests are untouched); this is the superset used
    by every other field. A field value of `None` or `""` clears the override
    (falls back to `default_project_config`'s default), matching
    `save_repository_path`'s existing clear-on-falsy contract.
    """
    if project_id not in models.PROJECT_IDS:
        raise ValueError(f"Unknown project: {project_id}")
    unknown = set(fields) - set(OVERRIDABLE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown project config field(s): {sorted(unknown)}")

    # Lock the whole read-modify-write (audit AR-5).
    with storage.file_lock(CONFIG_LOCK_FILE):
        overrides = _read_overrides()
        entry = overrides.get(project_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        for key, value in fields.items():
            if value in (None, ""):
                entry.pop(key, None)
            else:
                entry[key] = value
        overrides[project_id] = entry
        storage.atomic_write_json(CONFIG_FILE, overrides)


def task_defaults_from_project(cfg: dict) -> dict:
    """Fields a new task inherits from its project's configuration at creation
    time (see `tasks_repository.new_task_record`'s `workspace_path`/`branch`/
    `executor`/`prompt` kwargs). The user is never required to enter these
    manually — the Create Task UI pre-fills them from here and only overrides
    what the user explicitly changes.

    `workspace_path` mirrors `command_center.launch.resolve_workspace_path`'s
    precedence (project default workspace, else repository path) so the value
    baked onto the task at creation is exactly what Launch would already have
    resolved for it — this function does not change Launch's own fallback
    chain, it just materializes the same result earlier, for display and for
    legacy tasks created without ever visiting Launch.
    """
    return {
        "workspace_path": cfg.get("default_workspace_path") or cfg.get("repository_path") or None,
        "branch": cfg.get("default_branch") or None,
        "executor": cfg.get("default_executor") or None,
        "prompt": cfg.get("default_prompt") or "",
    }


def validate_project_settings(cfg: dict) -> list[str]:
    """Non-fatal warnings for the Projects settings UI to show before saving.

    Read-only — never mutates the filesystem or git state — and never blocks
    a save; the caller always persists the values via `save_project_settings`
    regardless of what is returned here, the same tolerance the pre-existing
    repository-path field already has (an unconfigured/invalid path is only
    ever caught later, by Launch's own `validate_launch`).
    """
    warnings: list[str] = []

    repository_path = cfg.get("repository_path")
    if repository_path:
        ok, message = validate_repository_path(repository_path)
        if not ok:
            warnings.append(f"Путь к репозиторию: {message}")

    workspace_path = cfg.get("default_workspace_path")
    if workspace_path:
        ok, message = validate_repository_path(workspace_path)
        if not ok:
            warnings.append(f"Workspace по умолчанию: {message}")
        else:
            status = git_info.get_status(Path(workspace_path).expanduser())
            if not status.get("is_repo"):
                warnings.append(f"Workspace по умолчанию не является git-репозиторием: {workspace_path}")
            else:
                branch = cfg.get("default_branch")
                if branch:
                    branches = git_info.get_branches(Path(workspace_path).expanduser())
                    if branches and branch not in branches:
                        warnings.append(f"Ветка «{branch}» не найдена в workspace по умолчанию.")

    executor_id = cfg.get("default_executor")
    if executor_id:
        # Imported locally: `executors` imports `agent_runner`, which imports
        # this module — a module-level import here would be circular.
        from command_center import executors

        if executor_id not in executors.EXECUTOR_IDS:
            warnings.append(f"Неизвестный executor: {executor_id}")
        elif not executors.get_executor(executor_id).available:
            warnings.append(f"Executor «{executor_id}» зарегистрирован, но пока недоступен для запуска.")

    prompt = cfg.get("default_prompt") or ""
    if len(prompt) > 20000:
        warnings.append("Prompt по умолчанию длиннее 20000 символов — проверьте содержимое.")

    return warnings
