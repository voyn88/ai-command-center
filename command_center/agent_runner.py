"""Launches Claude Code non-interactively against a configured project repository.

Security model:

- `subprocess.run` is called exactly like the rest of this project already calls it
  (`app.py`'s `run_start_task_script`/`run_git_command`): a fixed argument list, never
  `shell=True`, `capture_output=True`, `text=True`, an explicit timeout. The task
  prompt is passed as a single argv element to the `claude` binary, never interpreted
  by a shell, so prompt content cannot inject shell commands.
- `validate_repository` refuses to run unless `repository_path` is *exactly* the path
  configured for that project in `command_center.project_config` (resolved, so
  symlink/`..` tricks can't escape it). Used directly by `chat_service` and
  `runtime.supervisor`, which have no per-task workspace concept — a run
  launched through either can never target a path outside project config.
  The task-launcher flow (`app.py`'s `render_agent_launcher`) instead resolves
  its path via `command_center.launch.resolve_workspace_path` — task
  `workspace_path` / project `default_workspace_path` / project
  `repository_path`, in that order — and validates *that* resolved path
  (same `expanduser().resolve()` symlink/`..` guard) rather than calling
  `validate_repository`; a task can still never be launched against a path
  that didn't come from that trusted precedence chain.
- This module itself never calls `git commit`/`push`/`merge`/`reset`/`rebase`/`clean` —
  the only git subprocess calls here are the read-only pre/post-run snapshot
  (`rev-parse`, `branch --show-current`, `status --porcelain`).

- **Read-only task types** (`review`, `final_gate`, `architecture_review`, see
  `READ_ONLY_TASK_TYPES`) receive genuine technical enforcement, not a prompt
  instruction: `build_command` passes `--tools` (which — per `claude --help` —
  replaces the entire *available* tool set, not merely a permission hint layered on
  top of it) set to exactly `READ_ONLY_ALLOWED_TOOLS` (`Read`, `Grep`, `Glob`). The
  `Bash` tool is not in that list, so it is not merely denied by pattern — it does
  not exist for that run. Nothing invocable through it (arbitrary shell redirection,
  `rm`/`mv`/`cp`/`sed -i`, `git add`/`apply`/`checkout`/`restore`/`switch`/`stash`/
  `commit`/`push`/`merge`/`reset`/`rebase`/`clean`, or anything else) is reachable,
  because there is no tool call that can reach a shell at all. `Edit`/`Write`/
  `NotebookEdit`/`MultiEdit` are likewise simply absent from the tool set. An earlier
  version of this module instead tried to deny specific `Bash(git ...)` patterns via
  `--disallowedTools` while leaving the general-purpose `Bash` tool itself available —
  an independent review found that left every *other* shell-reachable mutation
  (`git apply`, `git checkout`, `git stash`, plain file redirection, etc.)
  unrestricted for a task type documented as "must not modify any file." `--tools`
  closes that gap by removing Bash from what the run can invoke at all, rather than
  trying to enumerate everything Bash must not be allowed to do.
- **Implementation/remediation task types** keep `Bash` (they need it to run tests,
  linters and create the task's local commit). `git add` and `git commit` are
  available inside the already-verified dedicated worktree. History, branch and
  remote mutations (`apply`, `checkout`, `restore`, `switch`, `stash`, `push`,
  `merge`, `reset`, `rebase`, `clean`, branch deletion and `gh`) remain denied.
- **This application's own code** is the only place the actual git-write prohibition
  (never commit/push/merge/reset/rebase/delete/clean *automatically*) is absolute: it
  is simply never called by any code path in this codebase, verified by the absence
  of any such `subprocess`/`git` invocation outside the read-only snapshot calls
  listed above. That guarantee is unconditional and does not depend on what a spawned
  `claude` process chooses to do.

- Cancellation (Streamlit v1 path): Streamlit re-executes the whole script
  top-to-bottom on every interaction, so there is no supervisor process that
  could safely interrupt a run already in flight from a *previous* Streamlit
  rerun. Callers that never pass `cancel_event` (`chat_service`,
  `launch_service`) still get this behavior: the call blocks (with a spinner)
  until the process exits or the timeout fires, and there is no fake
  mid-flight cancel button for them. This is a known, documented limitation
  (see ARCHITECTURE.md / README "Known limitations").

- Cancellation (worker daemon path, VOYN-W0-AICC-FORCED-AGENT-CANCELLATION):
  the worker daemon (`worker.daemon.WorkerDaemon`) *does* have a live
  out-of-band signal — the `lease_lost` event its heartbeat thread sets the
  moment the database says this attempt's lease is gone
  (`worker.daemon._heartbeat_loop`). `worker.handlers._run_agent` passes that
  same event through as `run_claude_code`'s `cancel_event`, so a lease lost
  *mid-run* (not just before the subprocess was started) now forcibly
  terminates the in-flight `claude` process instead of leaving it to mutate
  an isolated worktree the daemon can no longer account for. The subprocess
  is always launched as its own process-group leader (`os.setsid` on POSIX,
  `CREATE_NEW_PROCESS_GROUP` on Windows) specifically so termination can
  target the whole group — the CLI process plus any child it spawned (tool
  subprocesses, MCP servers) — not just its own PID; killing only the PID
  would leave orphaned children as the same class of unaccountable mutation
  this change exists to close. Termination escalates SIGTERM (POSIX) /
  CTRL_BREAK_EVENT (Windows) -> bounded grace period -> SIGKILL (POSIX) /
  `Popen.kill()` (Windows), mirroring the existing `timeout_seconds`
  enforcement (which now also targets the process group, for the same
  orphan-child reason, rather than only the CLI's own PID as the previous
  `subprocess.run(timeout=...)` form did). "Cancelled" is a valid
  `RUN_STATUSES` value, returned only via this path.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from command_center import models, project_config, storage

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = storage.resolve_data_dir(ROOT)
RUNS_FILE = DATA_DIR / "runs.jsonl"
RUNS_EXAMPLE_FILE = ROOT / "data" / "runs.example.jsonl"

CLAUDE_BINARY = "claude"
# Overridable because the worker service's own PATH is not a login shell's:
# the CLI is installed under `~/.local/bin` on worker-01 (no root needed --
# the service already runs as that user), which systemd does not add for it.
CODEX_BINARY = os.environ.get("AICC_CODEX_BINARY") or "codex"
COPILOT_BINARY = os.environ.get("AICC_COPILOT_BINARY") or "copilot"

# Codex 0.149.0 on worker-01 can exit zero after its vendor bwrap fails to
# create loopback inside the network namespace. This exact, two-part signature
# is an executor-host failure, not a task result.
_CODEX_BWRAP_LOOPBACK_SIGNATURE = (
    "bwrap: loopback:",
    "failed rtm_newaddr: operation not permitted",
)
_COPILOT_RETRYABLE_FAILURE_SIGNATURES = (
    "ai credit usage limit",
    "rate limit",
    "not logged in",
    "authentication required",
    "authentication failed",
    "unauthorized",
    "forbidden",
    "failed during startup",
    "service unavailable",
    "temporarily unavailable",
    "network error",
    "connection refused",
    "too many requests",
    "could not authenticate",
    "getaddrinfo",
    "econnreset",
)
_CODEX_PREFLIGHT_PROMPT = (
    "This is a disposable sandbox capability probe. In the current repository, "
    "create a file named aicc-codex-commit-probe.txt containing exactly "
    "AICC_CODEX_COMMIT_OK followed by a newline. Run git add for that file and "
    "git commit -m 'aicc codex commit probe'. Do not inspect any other path or "
    "use a remote. After the commit succeeds, reply exactly: "
    "AICC_CODEX_WORKSPACE_WRITE_OK"
)
_codex_workspace_write_preflight_lock = threading.Lock()
_codex_workspace_write_preflight_result: tuple[bool, str] | None = None


def disable_codex_workspace_write(detail: str = "") -> None:
    """Open the worker-local Codex circuit after a runtime bwrap failure."""
    global _codex_workspace_write_preflight_result
    reason = "Codex workspace-write sandbox unavailable"
    if detail:
        reason = f"{reason}: {detail[-400:]}"
    with _codex_workspace_write_preflight_lock:
        _codex_workspace_write_preflight_result = (False, reason)

# --------------------------------------------------------------------------
# Execution profiles — named, testable single source of truth for "what can
# this run touch". Every `claude` invocation this project makes (v1 sync
# `build_command` below, and v2 `runtime.supervisor.build_claude_command`)
# resolves its task_type to exactly one of these two profiles and applies it
# identically, so the two executors can never silently diverge on tool access.
#
# `PROFILE_READ_ONLY`: `READ_ONLY_ALLOWED_TOOLS` only (Read/Grep/Glob), via
# `--tools` (tool-set replacement — see the module docstring). No Bash, no
# file mutation, ever.
#
# `PROFILE_TRUSTED_DEVELOPMENT`: the full built-in tool set (Read, Grep,
# Glob, Edit, Write, Bash, ...), so the agent can actually read, search,
# edit/create files, run shell commands, run git (read/status/log/diff —
# every git-write subcommand is still denied via `GIT_WRITE_DISALLOWED_
# TOOLS`), and run tests/validators. This is deliberately *not* applied to
# every task automatically — only task types that exist to modify a trusted
# local repository (`implementation`, `remediation`) resolve to it; anything
# else defaults to `PROFILE_READ_ONLY`, never the reverse.
PROFILE_READ_ONLY = "read_only"
PROFILE_TRUSTED_DEVELOPMENT = "trusted_development"

READ_ONLY_TASK_TYPES = {"review", "final_gate", "architecture_review"}
MODEL_ONLY_TASK_TYPES = {"independent_review"}
MUTATING_TASK_TYPES = {"implementation", "remediation"}

# `--permission-mode` for every profile. Both profiles use `acceptEdits`:
# empirically verified (2026-07-21, real `claude` CLI, headless `-p` mode)
# that *without* an explicit `--permission-mode`, the CLI's implicit default
# denies `Write`/`Edit` tool calls outright in non-interactive mode — the
# call returns `is_error: false` and `permission_denials: [{"tool_name":
# "Write", ...}]`, i.e. the process still exits 0 while the requested file
# mutation silently never happened. `acceptEdits` was confirmed (same
# method) to auto-accept `Write`/`Edit` and to leave `Bash` unaffected
# (`permission_denials: []` in both cases). This is exactly the F-01-class
# gap `runtime.supervisor.build_claude_command` had: it built `--tools`/
# `--disallowedTools` but never set `--permission-mode` at all, so a
# `trusted_development` v2 run could report "cannot execute" while its own
# process exit code was 0 — see `runtime.outcome` for the terminal-state
# classifier that also guards against this at the result-evaluation layer.
PERMISSION_MODE_BY_PROFILE: dict[str, str] = {
    PROFILE_READ_ONLY: "acceptEdits",
    # User-approved 2026-07-25: a headless implementation agent must be able to
    # run its own tests/build without an interactive approver. Git-write stays
    # blocked by `--disallowedTools` (verified to take precedence). See the note
    # above for the empirical confirmation.
    PROFILE_TRUSTED_DEVELOPMENT: "bypassPermissions",
}


def profile_for_task_type(task_type: str) -> str:
    """Resolve capabilities with mutation denied unless explicitly allowed.

    Only reviewed implementation/remediation types receive development
    capabilities. Unknown and future task types fail closed as read-only.
    """
    return PROFILE_TRUSTED_DEVELOPMENT if task_type in MUTATING_TASK_TYPES else PROFILE_READ_ONLY


def is_untrusted_source(source: str | None) -> bool:
    """Secondary heuristic: a non-empty `source` string is treated as untrusted
    provenance. Kept for legacy data that carries a provenance label, but NOT the
    primary signal — `source` on a task package is attacker-supplied, so a
    malicious package could simply omit it. `is_untrusted_task` is the real gate;
    it also honours the app-set `untrusted_import` flag `task_import` stamps on
    every import (audit D7 / SEC-1)."""
    return bool(source and source.strip())


def is_untrusted_task(task: dict) -> bool:
    """Whether a task must run with reduced capabilities by default (audit D7).

    True when the task carries the **app-set** `untrusted_import` flag (stamped by
    `task_import` on every imported task, independent of package content — an
    attacker cannot clear it), or, for legacy data, when it has a non-empty
    `source`. Operator-authored in-app tasks have neither and are trusted."""
    return bool(task.get("untrusted_import")) or is_untrusted_source(task.get("source"))


def profile_for_task(task_type: str, *, untrusted: bool = False, operator_elevated: bool = False) -> str:
    """Provenance-aware execution profile (audit D7).

    Read-only task types are always `PROFILE_READ_ONLY` (they have no dangerous
    capability). A non-read-only task from an *untrusted* source is downgraded to
    `PROFILE_READ_ONLY` (no Bash, no `bypassPermissions`) unless an operator has
    explicitly elevated it — so a malicious imported task cannot silently obtain
    arbitrary local shell. A trusted (operator-authored) task is unchanged, so
    with `untrusted=False` this is identical to `profile_for_task_type`."""
    if task_type not in MUTATING_TASK_TYPES:
        return PROFILE_READ_ONLY
    if untrusted and not operator_elevated:
        return PROFILE_READ_ONLY
    return PROFILE_TRUSTED_DEVELOPMENT

# The *complete* available tool set for read-only task types, passed via `--tools`
# (not `--allowedTools`/`--disallowedTools`). Per `claude --help`, `--tools` replaces
# the built-in tool set outright rather than layering a permission rule on top of it,
# so a tool simply absent here cannot be invoked by that run at all — in particular,
# `Bash` (and therefore every shell-reachable mutation: `rm`/`mv`/`cp`/`sed -i`,
# redirection, `git add`/`apply`/`checkout`/`restore`/`switch`/`stash`/`commit`/
# `push`/`merge`/`reset`/`rebase`/`clean`, or anything else) is not merely denied by
# pattern, it does not exist for that run. `Edit`/`Write`/`NotebookEdit`/`MultiEdit`
# are likewise simply not in this list. This app already captures the pre/post-run
# git snapshot itself (`git_snapshot`, below), so a read-only run does not need shell
# access to git to do its job.
READ_ONLY_ALLOWED_TOOLS: list[str] = ["Read", "Grep", "Glob"]

# Bash patterns blocked via `--disallowedTools` for implementation/remediation runs,
# which keep the `Bash` tool (they need it to run tests/linters/etc. per the
# `AGENT_ROLES` prompt rules in `scripts/start-task.sh`). This is pattern-based
# denial of history, branch and remote mutations, not tool removal. `git add` and
# `git commit` deliberately stay available because implementation/remediation task
# prompts require a reviewable local commit and no separate pipeline commit step
# exists.
GIT_WRITE_DISALLOWED_TOOLS: list[str] = [
    "Bash(git apply:*)",
    "Bash(git checkout:*)",
    "Bash(git restore:*)",
    "Bash(git switch:*)",
    "Bash(git stash:*)",
    "Bash(git push:*)",
    "Bash(git merge:*)",
    "Bash(git reset:*)",
    "Bash(git rebase:*)",
    "Bash(git clean:*)",
    "Bash(git branch -d:*)",
    "Bash(git branch -D:*)",
    # `gh` (GitHub CLI): opening and merging pull requests is the completion
    # pipeline's job, never the agent's, so gh is denied wholesale — this blocks
    # the `gh pr create`/`gh pr merge`/`gh api` writes an agent could otherwise
    # run freely (gh was previously unrestricted here). Pattern-based denial,
    # like the git entries above: it does not stop gh re-invoked through a nested
    # shell — `scrub_vcs_credentials` is the complementary control that removes
    # the tokens gh would need to authenticate.
    "Bash(gh:*)",
]

# Environment variables that carry Git/GitHub push/merge credentials, stripped
# from every spawned agent's environment. An implementation/remediation agent
# has no task reason to authenticate to a remote; removing these means that even
# if it reaches `git push`/`gh` through a nested shell (which pattern-based tool
# denial cannot fully prevent), it holds no env-provided credential to push or
# merge with. Defence-in-depth, not a sandbox: a credential cached on disk
# (`gh auth login`, a git credential helper / OS keychain) is outside this
# process's reach. Deliberately never touches ANTHROPIC_*/CLAUDE_* (the agent's
# own model auth), PATH, or HOME.
_VCS_CREDENTIAL_ENV_VARS: frozenset[str] = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_API_TOKEN",
        "GITHUB_ACCESS_TOKEN",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "SSH_AUTH_SOCK",
        "SSH_ASKPASS",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "AICC_WORKSPACE_AUTHORITY_KEY",
        "AICC_PUBLISH_DEPLOY_KEY",
        "AICC_PUBLISH_OWNER",
        "VOYN_LEASE_DSN",
        "VOYN_LEASE_TOOL",
        "VOYN_LEASE_SESSION",
        "VOYN_LEASE_REPOSITORY",
    }
)


def scrub_vcs_credentials(environment: dict[str, str]) -> dict[str, str]:
    """Return a copy of `environment` with Git/GitHub credential variables
    (`_VCS_CREDENTIAL_ENV_VARS`) removed, so a spawned agent cannot inherit
    ambient push/merge credentials. Never removes the agent's own model auth."""
    scrubbed = {
        key: value
        for key, value in environment.items()
        if key not in _VCS_CREDENTIAL_ENV_VARS
        and not key.startswith(("GIT_CONFIG_", "AICC_PUBLISH_", "VOYN_LEASE_"))
    }
    # Ignore machine/user Git config and the host gh credential store for the
    # model process.  The task clone carries only the local identity needed to
    # commit; it has no remote until the guarded publisher restores one after
    # the process exits.
    scrubbed.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GH_CONFIG_DIR": "/nonexistent/aicc-agent-gh",
            "SSH_ASKPASS_REQUIRE": "never",
        }
    )
    return scrubbed


DEFAULT_TIMEOUT_SECONDS = 900
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600


class RunnerError(Exception):
    """Raised when a run cannot even be attempted (validation failure)."""


def claude_cli_available(binary: str | None = None) -> bool:
    """Is the Claude Code CLI resolvable on PATH?

    `binary` lets a caller probe the executable *its own* launch path will
    exec rather than this module's `CLAUDE_BINARY`: the v2 Session Supervisor
    resolves its own (`runtime.supervisor.CLAUDE_BINARY`, honouring
    `AICC_CLAUDE_BINARY`), so a preflight for a v2 launch must ask about that
    one — otherwise it reports on a binary nobody is going to run."""
    return shutil.which(binary or CLAUDE_BINARY) is not None


def claude_cli_preflight(binary: str | None = None) -> tuple[bool, str]:
    """`(available, message)` for the Claude Code CLI — the same probe as
    `claude_cli_available`, plus the operator-facing explanation to render
    when it fails.

    Exists so a launch entry point can state the reason *before* the operator
    walks a confirmation flow, instead of surfacing a bare `FileNotFoundError`
    at exec time (audit MINOR-2). Project Chat keeps its own, mode-specific
    wording for the same probe (`chat_service.ClaudeCodeChatProvider`)."""
    resolved = binary or CLAUDE_BINARY
    if claude_cli_available(resolved):
        return True, ""
    return False, (
        f"CLI `{resolved}` не найден в PATH — запуск Claude Code завершится ошибкой ещё до "
        "старта агента. Установите Claude Code (`npm install -g @anthropic-ai/claude-code`) "
        "и убедитесь, что бинарник доступен в PATH, либо выберите другой execution provider."
    )


def codex_workspace_write_preflight() -> tuple[bool, str]:
    """Probe the exact workspace-write launch path once per worker.

    Version/help probes cannot discover the worker's AppArmor/user-namespace
    refusal: it happens only when Codex starts bwrap. The probe uses a
    disposable git workspace, never a task repository. A negative result is
    cached, so later tasks skip Codex instead of consuming another attempt.
    """
    global _codex_workspace_write_preflight_result
    with _codex_workspace_write_preflight_lock:
        if _codex_workspace_write_preflight_result is not None:
            return _codex_workspace_write_preflight_result
        if shutil.which(CODEX_BINARY) is None:
            _codex_workspace_write_preflight_result = (
                False,
                f"Codex CLI {CODEX_BINARY!r} is not available on PATH",
            )
            return _codex_workspace_write_preflight_result

        import tempfile

        with tempfile.TemporaryDirectory(prefix="aicc-codex-preflight-") as raw_probe:
            probe = Path(raw_probe)
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(probe)],
                capture_output=True,
                text=True,
                check=False,
            )
            if initialized.returncode != 0:
                _codex_workspace_write_preflight_result = (
                    False,
                    f"cannot create disposable Codex probe workspace: {initialized.stderr.strip()}",
                )
                return _codex_workspace_write_preflight_result
            for key, value in (
                ("user.name", "AICC Codex Preflight"),
                ("user.email", "aicc-codex-preflight@localhost"),
            ):
                configured = subprocess.run(
                    ["git", "config", "--local", key, value],
                    cwd=probe,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if configured.returncode != 0:
                    _codex_workspace_write_preflight_result = (
                        False,
                        f"cannot configure disposable Codex probe: {configured.stderr.strip()}",
                    )
                    return _codex_workspace_write_preflight_result
            seed = probe / ".aicc-codex-preflight-seed"
            seed.write_text("seed\n", encoding="utf-8")
            seeded = subprocess.run(
                ["git", "add", seed.name],
                cwd=probe,
                capture_output=True,
                text=True,
                check=False,
            )
            if seeded.returncode == 0:
                seeded = subprocess.run(
                    ["git", "commit", "--quiet", "-m", "aicc codex preflight seed"],
                    cwd=probe,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if seeded.returncode != 0:
                _codex_workspace_write_preflight_result = (
                    False,
                    f"cannot seed disposable Codex probe: {seeded.stderr.strip()}",
                )
                return _codex_workspace_write_preflight_result
            before = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=probe,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            run = run_claude_code(
                repository_path=probe,
                prompt=_CODEX_PREFLIGHT_PROMPT,
                task_type="implementation",
                timeout_seconds=MIN_TIMEOUT_SECONDS,
                executor="codex",
            )
            diagnostic = "\n".join(part for part in (run.stdout, run.stderr) if part)
            if run.is_executor_sandbox_error:
                detail = diagnostic[-400:] or "bwrap loopback namespace setup was denied"
                _codex_workspace_write_preflight_result = (
                    False,
                    f"Codex workspace-write sandbox unavailable: {detail}",
                )
            elif run.status != "completed":
                detail = diagnostic[-400:] or f"exit_code={run.exit_code!r}"
                # Provider/auth/network failures are transient. Do not pin a
                # worker-wide negative forever; only the proven bwrap/capability
                # failures above/below open the persistent local circuit.
                return (
                    False,
                    f"Codex workspace-write preflight failed: {detail}",
                )
            else:
                after = _run_git(["rev-parse", "HEAD"], probe)
                status = _run_git(["status", "--porcelain"], probe)
                common = _run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], probe)
                probe_file = probe / "aicc-codex-commit-probe.txt"
                try:
                    probe_content = probe_file.read_text(encoding="utf-8")
                except OSError:
                    probe_content = None
                commit_ok = bool(
                    after
                    and after.returncode == 0
                    and after.stdout.strip()
                    and after.stdout.strip() != before
                    and status
                    and status.returncode == 0
                    and not status.stdout.strip()
                    and common
                    and common.returncode == 0
                    and Path(common.stdout.strip()).resolve() == (probe / ".git").resolve()
                    and probe_content == "AICC_CODEX_COMMIT_OK\n"
                )
                if not commit_ok:
                    _codex_workspace_write_preflight_result = (
                        False,
                        (
                            "Codex workspace-write preflight could not create a clean local commit "
                            "with task-local Git metadata"
                        ),
                    )
                else:
                    _codex_workspace_write_preflight_result = (True, "")
        return _codex_workspace_write_preflight_result


def validate_repository(project_id: str, repository_path: str) -> Path:
    """Raise RunnerError unless `repository_path` is the configured path for `project_id`."""
    if not repository_path:
        raise RunnerError("Путь к репозиторию не указан.")
    config = project_config.get_project_config(project_id)
    configured = config.get("repository_path")
    if not configured:
        raise RunnerError(
            f"Путь к репозиторию не настроен для проекта {project_id}. "
            "Настройте его в разделе «Проекты» → «Настройки репозитория»."
        )
    resolved_requested = Path(repository_path).expanduser().resolve()
    resolved_configured = Path(configured).expanduser().resolve()
    if resolved_requested != resolved_configured:
        raise RunnerError(
            "Указанный путь репозитория не совпадает с настроенным путём проекта. "
            "Запуск отклонён."
        )
    if not resolved_configured.is_dir():
        raise RunnerError(f"Настроенный путь репозитория не существует: {resolved_configured}")
    return resolved_configured


def _run_git(args: list[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def is_git_repository(repo_path: Path) -> bool:
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_path)
    return bool(result and result.returncode == 0 and result.stdout.strip() == "true")


def git_snapshot(repo_path: Path) -> dict:
    """Read-only branch/HEAD/status snapshot of `repo_path`, used for pre/post-run records."""
    if not is_git_repository(repo_path):
        return {"is_git_repo": False, "branch": None, "head": None, "status_summary": None}

    branch = _run_git(["branch", "--show-current"], cwd=repo_path)
    head = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    status = _run_git(["status", "--porcelain"], cwd=repo_path)

    status_lines = [line for line in (status.stdout.splitlines() if status and status.returncode == 0 else []) if line]

    return {
        "is_git_repo": True,
        "branch": (branch.stdout.strip() if branch and branch.stdout.strip() else "(detached HEAD)"),
        "head": head.stdout.strip() if head and head.returncode == 0 else None,
        "status_summary": "\n".join(status_lines) if status_lines else "(чисто)",
    }


def git_commit(repo_path: Path, ref: str = "HEAD") -> str | None:
    """Resolve ``ref`` to an exact commit SHA without changing the checkout."""
    result = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo_path)
    return result.stdout.strip() if result and result.returncode == 0 else None


def build_command(
    prompt: str,
    *,
    task_type: str,
    model: str | None = None,
    capability_override: str | None = None,
) -> list[str]:
    if capability_override is not None:
        profile = PROFILE_READ_ONLY if capability_override.lower() in ("read_only", "readonly") else PROFILE_TRUSTED_DEVELOPMENT
    else:
        profile = profile_for_task_type(task_type)
    command = [
        CLAUDE_BINARY,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        PERMISSION_MODE_BY_PROFILE[profile],
    ]

    if task_type in MODEL_ONLY_TASK_TYPES:
        # The exact PR diff is already embedded in the prompt by the trusted
        # control plane. Giving this reviewer Read/Grep/Glob would add ambient
        # repository authority it neither needs nor can bind to that exact SHA.
        command += ["--tools", ""]
    elif profile == PROFILE_READ_ONLY:
        # Tool-set replacement, not a permission-layer denial: Bash (and every
        # shell-reachable mutation) is not in this list, so it cannot be invoked by
        # this run at all. See the module docstring and READ_ONLY_ALLOWED_TOOLS.
        command += ["--tools", ",".join(READ_ONLY_ALLOWED_TOOLS)]
    else:
        # Bash stays available (implementation/remediation runs need it), but the
        # specific git-write subcommands their own prompts already forbid are denied.
        command += ["--disallowedTools", ",".join(GIT_WRITE_DISALLOWED_TOOLS)]

    if model:
        command += ["--model", model]
    return command


def build_codex_command(
    prompt: str,
    *,
    task_type: str,
    model: str | None = None,
) -> list[str]:
    """The `codex exec` argv, mapped onto the SAME two execution profiles
    `build_command` resolves for Claude (VOYN-W0-AICC-EXECUTOR-CODEX).

    Why a second executor at all: the fleet's Claude credential is a Max
    *subscription*, whose 5-hour rolling window is a hard cap no amount of
    retrying gets past -- live-measured 2026-08-23 as the single largest
    cause of parked work (142 of 167 `task_status_failed` tasks were
    literally "You've hit your session limit", not a task defect). Codex
    bills against a different account entirely, so it is capacity the
    Claude cap cannot consume, not merely a second attempt at the same
    exhausted pool. That is also why it belongs on the implementation
    cascade's ESCALATION link specifically (see `routing.ROUTING_MATRIX`),
    replacing what was previously a duplicate `claude` entry that could
    only ever re-hit the same limit.

    Profile mapping, deliberately equivalent rather than merely similar:

    * `PROFILE_READ_ONLY`   -> `--sandbox read-only`. The sandbox is the
      enforcement, exactly as `--tools` is for Claude: a read-only sandbox
      cannot write the tree no matter what the model decides to attempt.
    * `PROFILE_TRUSTED_DEVELOPMENT` -> `--sandbox workspace-write`, which
      confines writes to the working root `--cd` names. `danger-full-
      access` is never used: it removes the boundary this profile exists
      to draw.

    `--skip-git-repo-check` is NOT passed: every dispatch runs inside a
    real git worktree (`workspace_provisioning`), so the check is a free
    assertion that we are where we think we are.

    Output is left as plain text rather than `--json`: `extract_result_
    text` already falls through to raw stdout for shapes it does not
    recognise, and the pipeline's own contract with an agent is the
    `HEAD_SHA:` / `VERDICT:` trailer in that text -- identical for either
    executor, so nothing downstream has to learn a second format.
    """
    profile = profile_for_task_type(task_type)
    sandbox = "read-only" if profile == PROFILE_READ_ONLY else "workspace-write"
    command = [
        CODEX_BINARY,
        "exec",
        "--sandbox",
        sandbox,
        "--color",
        "never",
    ]
    if model:
        command += ["--model", model]
    # The prompt goes last and positionally: `codex exec [OPTIONS] [PROMPT]`.
    command.append(prompt)
    return command


def build_copilot_command(
    prompt: str,
    *,
    task_type: str,
    model: str | None = None,
) -> list[str]:
    """The GitHub Copilot CLI argv, on the SAME two profiles as the others
    (VOYN-W0-AICC-EXECUTOR-CODEX, second executor of the same change).

    A third account, for the same reason Codex is a second one: Copilot bills
    against a GitHub subscription, so it is capacity neither the Claude Max
    5-hour window nor the Codex account can exhaust.

    Profile mapping. Copilot's permission model is per-tool rather than a
    named sandbox, so the profiles are expressed with `--allow-tool` /
    `--deny-tool`:

    * `PROFILE_READ_ONLY`   -> no write tool is allowed at all. Only the
      read-side tools are granted, so the run cannot modify the tree even if
      the model tries -- the same property `--tools` gives Claude and
      `--sandbox read-only` gives Codex.
    * `PROFILE_TRUSTED_DEVELOPMENT` -> write and shell are allowed, but the
      git-write subcommands the prompts already forbid are denied explicitly
      (`GIT_WRITE_DISALLOWED_TOOLS`'s intent, expressed in Copilot's own
      `shell(git ...)` syntax). Publishing is the pipeline's job, never the
      agent's.

    `--allow-all` / `--allow-all-paths` are never passed: they would erase
    exactly the boundary these profiles draw.
    """
    profile = profile_for_task_type(task_type)
    command = [
        COPILOT_BINARY,
        "-p",
        prompt,
        "--no-color",
        "--silent",
        "--no-remote",
        "--no-remote-export",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
        "--no-ask-user",
    ]
    if task_type in MODEL_ONLY_TASK_TYPES:
        # Empty availability is stronger than a permission prompt: no Copilot
        # tool is exposed to the model at all, including read and shell.
        command += ["--available-tools="]
    elif profile == PROFILE_READ_ONLY:
        # Grant reads only. Absent `write`/`shell`, mutation is unreachable.
        command += ["--allow-tool", "read"]
    else:
        command += [
            "--allow-tool", "read",
            "--allow-tool", "write",
            "--allow-tool", "shell",
            # The agent commits locally; pushing/PR-opening belongs to
            # `publish_run`, which holds the writer lease. Denying the remote-
            # mutating subcommands keeps that boundary technical, not advisory.
            "--deny-tool", "shell(git push)",
            "--deny-tool", "shell(git remote)",
        ]
    if model:
        command += ["--model", model]
    return command


#: executor id -> the NAME of its argv builder in this module. The worker
#: refuses any executor absent from this table (`handlers._run_agent`), so an
#: unknown/unproven name can never silently burn a cascade attempt on a
#: phantom link -- the failure mode `routing.py`'s module docstring warns
#: about.
#:
#: Names, not function objects, and resolved via `globals()` at call time
#: (`_command_builder`): binding the objects here would capture them at import
#: and silently defeat `monkeypatch.setattr(agent_runner, "build_command", ...)`,
#: which several existing tests rely on to stub the argv without launching a
#: real CLI. Caught by exactly those tests when this was first written the
#: other way.
COMMAND_BUILDERS: dict[str, str] = {
    "claude": "build_command",
    "codex": "build_codex_command",
    "copilot": "build_copilot_command",
}


def _command_builder(executor: str) -> Callable[..., list[str]] | None:
    name = COMMAND_BUILDERS.get(executor)
    return globals().get(name) if name else None


def extract_result_text(stdout: str) -> str:
    """Extract the assistant's final report text from `claude -p --output-format json` stdout.

    Falls back to the raw stdout verbatim if it isn't parseable JSON in a recognized
    shape — the caller always has the full original stdout available regardless, this
    is only used to find the best candidate text to run the report parser against.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                return item.get("result", stdout) or stdout
        return stdout
    if isinstance(data, dict):
        return data.get("result", stdout) or stdout
    return stdout


def _parse_cli_result_payload(stdout: str) -> dict | None:
    """Parse `stdout` as the single JSON object `claude -p --output-format json`
    emits on a completed invocation, returning it only when it decodes to a
    dict.

    Returns `None` for anything else — empty output, a JSON array (the
    `stream-json`/NDJSON shape `extract_result_text` also has to tolerate),
    plain non-JSON text, or a parse error. Callers (`RunResult.is_executor_api_error`)
    must treat `None` as "no signal", never as a default classification either
    way: this function only ever *positively confirms* a shape, it never
    guesses one.
    """
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class RunResult:
    def __init__(
        self,
        *,
        status: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        duration_seconds: float,
        started_at: str,
        completed_at: str,
    ) -> None:
        self.status = status
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration_seconds = duration_seconds
        self.started_at = started_at
        self.completed_at = completed_at

        # Parsed once here (not re-parsed by every caller) so
        # `is_executor_api_error` below and any other consumer read plain
        # attributes rather than each re-implementing JSON parsing/fallback
        # over raw stdout. See `_parse_cli_result_payload` for the fail-safe
        # contract.
        payload = _parse_cli_result_payload(stdout)
        self.is_error: bool = bool(payload.get("is_error")) if payload else False
        self.api_error_status = payload.get("api_error_status") if payload else None
        self.terminal_reason = payload.get("terminal_reason") if payload else None

    @property
    def result_status(self) -> str:
        return self.status

    @property
    def is_executor_api_error(self) -> bool:
        """True only when the CLI's own structured output positively confirms
        an API-level failure (rate limit, auth, overload, ...) rather than a
        genuine task-level outcome.

        Incident 2026-08-21 16:09 UTC (control-01/worker-01): a shared
        Claude-CLI account hit its session/rate limit mid-fleet. The CLI
        still exited non-zero with output on stdout (`exit_code=1`,
        non-empty `stdout`), so the pre-existing "process never started"
        check in `worker.handlers._run_agent`
        (`status == "failed" and exit_code is None and not stdout`) never
        fired, and the run fell through to `ok=True` -- an infrastructure
        failure recorded as a genuine task success, permanently un-retried.
        The real payload carried `is_error: true`, `api_error_status: 429`
        and `terminal_reason: "api_error"` -- a signal the CLI emits
        specifically when it never got to attempt the task, distinct from a
        task that executed and failed on its own merits. Checked here as
        `(is_error and api_error_status is not None) or terminal_reason ==
        "api_error"` -- either half alone has been observed in real captures,
        so both are treated as sufficient. Deliberately does NOT look at
        `result` text content: a genuinely completed task's own report could
        contain unrelated words like "error" or "rate limit", and heuristics
        on free text would misclassify real task failures as infrastructure
        failures, causing exactly the "retrying a completed mutating run
        re-applies its side effects" hazard `worker.handlers` warns against.
        `False` (never `True`) when `_parse_cli_result_payload` could not
        positively confirm a parseable dict payload -- fail safe to today's
        behavior rather than guess.
        """
        return (self.is_error and self.api_error_status is not None) or self.terminal_reason == "api_error"

    def is_executor_provider_error(self, executor: str) -> bool:
        """Whether the selected CLI positively reports a provider failure.

        Claude exposes structured API fields; Copilot 1.0.x reports its
        pre-task auth/quota/network failures on stderr. Copilot free-text is
        considered only for a failed, non-zero process, so a successful
        review that merely discusses a rate limit cannot trigger a retry.
        """
        if self.is_executor_api_error:
            return True
        if executor != "copilot" or self.status != "failed" or not self.exit_code:
            return False
        diagnostic = f"{self.stdout}\n{self.stderr}".lower()
        return any(
            signature in diagnostic
            for signature in _COPILOT_RETRYABLE_FAILURE_SIGNATURES
        )

    @property
    def is_executor_sandbox_error(self) -> bool:
        """Whether the sandbox launcher reported its known loopback failure."""
        diagnostic = f"{self.stdout}\n{self.stderr}".lower()
        return all(token in diagnostic for token in _CODEX_BWRAP_LOOPBACK_SIGNATURE)


# How often the mid-run poll loop wakes to re-check `cancel_event` and the
# deadline. Short enough that a lease-loss signal turns into a SIGTERM within
# a fraction of a second of the heartbeat thread setting the event; long
# enough not to spin the CPU over the lifetime of a run that can last up to
# MAX_TIMEOUT_SECONDS.
CANCEL_POLL_INTERVAL_SECONDS = 0.5

# Grace period between SIGTERM and SIGKILL when forcibly terminating a
# process group (mid-run cancellation, and now also the timeout path — see
# the module docstring's "Cancellation (worker daemon path)" note for why
# both share this mechanism). Configurable per call via
# `termination_grace_seconds`; this is only the default. 15s matches the
# existing convention of generous-but-bounded waits elsewhere in this module
# (MIN_TIMEOUT_SECONDS=30 is the shortest a whole run is ever given; 15s is a
# fraction of that, enough for the CLI's own signal handling / MCP server
# shutdown to run without leaving a lost lease's mutation in flight for long).
DEFAULT_TERMINATION_GRACE_SECONDS = 15.0


def _popen_new_process_group_kwargs() -> dict:
    """Extra `Popen` kwargs so the spawned `claude` process becomes the leader
    of its own process group (POSIX) / process group (Windows), never sharing
    ours. This is what makes group-wide termination possible at all — without
    it, `os.killpg`/`CTRL_BREAK_EVENT` would reach this worker process too."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}  # POSIX: equivalent to a preexec_fn calling os.setsid()


def _terminate_process_group(proc: subprocess.Popen, *, grace_seconds: float) -> None:
    """Escalating termination of `proc`'s entire process group: SIGTERM (POSIX)
    / CTRL_BREAK_EVENT (Windows), wait up to `grace_seconds`, then SIGKILL
    (POSIX) / `Popen.kill()` (Windows) if it is still alive.

    Targets the *group*, not just `proc.pid` — see `_popen_new_process_group_
    kwargs` and the module docstring: a plain `proc.kill()` would leave any
    child the CLI spawned (tool subprocesses, MCP servers) running and
    unaccounted for, which is the exact defect class this change closes.
    Never raises: a process that already exited (race between our poll and
    its own completion) is treated as success, not an error.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return  # already gone
        except OSError:
            pass
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass

    if sys.platform == "win32":
        try:
            proc.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return  # already gone
        except OSError:
            pass
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        # Even SIGKILL cannot be un-delivered short of a kernel bug or an
        # uninterruptible (D-state) child; there is nothing further this
        # function can do. The caller's own bookkeeping (worker.daemon
        # discarding the outcome, the isolated worktree staying leased-out
        # until the group is confirmed dead) is what protects against
        # redelivery, not a guarantee that this call blocks forever.
        pass


def run_claude_code(
    *,
    repository_path: Path,
    prompt: str,
    task_type: str,
    timeout_seconds: int,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    executor: str = "claude",
) -> RunResult:
    """Execute an agent CLI. Never raises for expected failure modes.

    `executor` selects the argv builder from `COMMAND_BUILDERS`; everything
    else in this function -- process-group isolation, the cancellation and
    timeout polling loop, the escalating SIGTERM/SIGKILL teardown, the
    deadlock-free output collection -- is executor-independent and is
    deliberately NOT duplicated per executor. The function keeps its
    historical name so the many existing callers (and the tests that
    monkeypatch it) are untouched; `executor` defaults to `"claude"`, so
    every one of them behaves exactly as before.

    Runs the CLI as its own process-group leader (`_popen_new_process_group_
    kwargs`) via `subprocess.Popen` rather than the blocking `subprocess.run`
    form, specifically so a caller that hands in `cancel_event` gets a hook to
    intervene mid-run: while the process is alive, this function polls both
    `cancel_event` and the `timeout_seconds` deadline every
    `CANCEL_POLL_INTERVAL_SECONDS`, and on either trip forcibly terminates the
    whole process group (`_terminate_process_group`) rather than leaving it
    running. `cancel_event` is optional and defaults to `None` (never set) so
    every existing caller that does not pass one — `chat_service`,
    `launch_service`, and any future v1 Streamlit launch — is unaffected: the
    call still blocks until the process exits or the timeout fires, exactly
    as the previous `subprocess.run(timeout=...)` implementation did, just
    without a hook nobody asked for.

    `worker.handlers._run_agent` is the one caller that passes `cancel_event`
    today, wiring in the daemon's own `lease_lost` event so a lease lost
    mid-run now actually stops the subprocess instead of merely being noticed
    after the fact once it exits on its own.
    """
    builder = _command_builder(executor)
    if builder is None:
        # Refuse rather than silently falling back to Claude: a typo'd or
        # unwired executor name must surface as a routing failure, not as a
        # run that quietly consumed the very quota the route existed to
        # avoid.
        return RunResult(
            status="failed",
            exit_code=None,
            stdout="",
            stderr=f"unknown executor {executor!r}; known: {sorted(COMMAND_BUILDERS)}",
            duration_seconds=0.0,
            started_at=models.iso_now(),
            completed_at=models.iso_now(),
        )
    command = builder(prompt, task_type=task_type, model=model)
    started_at = models.iso_now()
    started_monotonic = time.monotonic()

    try:
        proc = subprocess.Popen(
            command,
            cwd=repository_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Strip ambient Git/GitHub credentials: this agent has no reason to
            # authenticate to a remote, and the completion pipeline (not the
            # agent) owns push/merge. See scrub_vcs_credentials.
            env=scrub_vcs_credentials(dict(os.environ)),
            **_popen_new_process_group_kwargs(),
        )
    except OSError as exc:
        duration = time.monotonic() - started_monotonic
        return RunResult(
            status="failed",
            exit_code=None,
            stdout="",
            stderr=f"Не удалось запустить Claude Code: {exc}",
            duration_seconds=duration,
            started_at=started_at,
            completed_at=models.iso_now(),
        )

    # `Popen.communicate()` is the only safe way to drain stdout/stderr
    # without risking the classic pipe-full deadlock (the CLI's own
    # `--output-format json` transcript can comfortably exceed the OS pipe
    # buffer). It is called exactly once, from a background thread, so the
    # main loop below is free to poll `cancel_event`/the deadline without
    # itself blocking on I/O — `communicate()` is documented to work
    # correctly when the process is killed out from under it (it simply
    # returns whatever was written before the pipes closed).
    collected: dict[str, str] = {}

    def _collect() -> None:
        try:
            out, err = proc.communicate()
        except (OSError, ValueError):
            out, err = "", ""
        collected["stdout"] = out or ""
        collected["stderr"] = err or ""

    collector = threading.Thread(target=_collect, name="agent-runner-io", daemon=True)
    collector.start()

    deadline = started_monotonic + timeout_seconds
    outcome = "exited"
    while True:
        collector.join(timeout=CANCEL_POLL_INTERVAL_SECONDS)
        if not collector.is_alive():
            outcome = "exited"
            break
        if cancel_event is not None and cancel_event.is_set():
            outcome = "cancelled"
            break
        if time.monotonic() >= deadline:
            outcome = "timed_out"
            break

    if outcome in ("cancelled", "timed_out"):
        _terminate_process_group(proc, grace_seconds=termination_grace_seconds)
        # The collector thread's `communicate()` call unblocks once the
        # killed process closes its pipes; bound the wait generously (grace
        # period again, plus headroom) rather than joining forever, so a
        # pathological D-state child cannot hang this call indefinitely.
        collector.join(timeout=termination_grace_seconds + 5.0)

    duration = time.monotonic() - started_monotonic
    stdout = collected.get("stdout", "")
    stderr = collected.get("stderr", "")
    exit_code = proc.poll()

    if outcome == "cancelled":
        return RunResult(
            status="cancelled",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            started_at=started_at,
            completed_at=models.iso_now(),
        )
    if outcome == "timed_out":
        return RunResult(
            status="timed_out",
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            started_at=started_at,
            completed_at=models.iso_now(),
        )
    diagnostic = f"{stdout}\n{stderr}".lower()
    status = (
        "failed"
        if all(token in diagnostic for token in _CODEX_BWRAP_LOOPBACK_SIGNATURE)
        else "completed" if exit_code == 0 else "failed"
    )
    return RunResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        started_at=started_at,
        completed_at=models.iso_now(),
    )


def resolve_timeout(requested: int | None) -> int:
    if not requested:
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, int(requested)))


# A task's timeout is set to 200 % of its estimated duration: enough headroom
# that a normal run never hits it, without the one-size-fits-all cap being far
# too long for a quick task or too short for a big one. Below the estimate the
# bar/"осталось" track the estimate itself (100 %); the timeout is the 200 %
# hard stop.
TIMEOUT_ESTIMATE_MULTIPLIER = 2.0


def timeout_for_task(task: dict | None) -> int:
    """The run timeout for `task`, individualized as 200 % of its
    `estimate_hours` (clamped to `[MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS]`),
    or `DEFAULT_TIMEOUT_SECONDS` when the task carries no estimate."""
    estimate_hours = (task or {}).get("estimate_hours")
    if not estimate_hours:
        return DEFAULT_TIMEOUT_SECONDS
    seconds = int(float(estimate_hours) * 3600 * TIMEOUT_ESTIMATE_MULTIPLIER)
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, seconds))


def default_model() -> str | None:
    """Model override from environment, e.g. CLAUDE_CODE_MODEL=sonnet. Optional."""
    value = os.environ.get("CLAUDE_CODE_MODEL", "").strip()
    return value or None


# --------------------------------------------------------------------------
# Run persistence (append-only; see command_center.storage module docstring)
# --------------------------------------------------------------------------


def append_run(run: dict) -> dict:
    """Append a full snapshot of `run` as the new latest state for its id."""
    snapshot = dict(run)
    snapshot["updated_at"] = models.iso_now()
    storage.append_jsonl(RUNS_FILE, snapshot)
    return snapshot


def load_runs() -> list[dict]:
    """Load every run's *latest* snapshot, newest-created first."""
    storage.ensure_seeded_jsonl(RUNS_FILE)
    records = storage.read_jsonl(RUNS_FILE)
    latest = storage.fold_latest_by_id(records)
    return sorted(latest.values(), key=lambda run: run.get("created_at") or "", reverse=True)


def get_run(run_id: str) -> dict | None:
    for run in load_runs():
        if run.get("id") == run_id:
            return run
    return None


# --------------------------------------------------------------------------
# Full report storage (FEATURE 3) — never truncated
# --------------------------------------------------------------------------

# See `command_center.runtime.reports.REPORTS_ROOT` for why this honors
# `AICC_REPORTS_ROOT` — same subprocess-isolation gap, same fix, applied here too
# for consistency between the v1.2 and v2 report-writing paths.
REPORTS_ROOT = Path(os.environ["AICC_REPORTS_ROOT"]) if os.environ.get("AICC_REPORTS_ROOT") else ROOT / "reports"


# Path components are restricted to this conservative charset so a hand-authored
# or imported `task_id`/`project`/`agent` cannot introduce a path separator or a
# `..` segment and walk the report *write* out of REPORTS_ROOT (audit SEC-2).
# `.` is deliberately excluded so no component can become `.` or `..`.
_SAFE_PATH_COMPONENT = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _safe_path_component(value: str, fallback: str) -> str:
    cleaned = "".join(c if c in _SAFE_PATH_COMPONENT else "_" for c in value)
    return cleaned or fallback


def report_path_for(run: dict) -> Path:
    project = _safe_path_component(run.get("project") or "UNKNOWN", "UNKNOWN")
    started = run.get("started_at") or run.get("created_at") or models.iso_now()
    try:
        started_dt = datetime.fromisoformat(started)
    except ValueError:
        started_dt = datetime.now(UTC)
    timestamp = started_dt.strftime("%Y%m%d-%H%M%S")
    task_part = _safe_path_component(run.get("task_id") or "adhoc", "adhoc")[:12]
    agent = _safe_path_component(run.get("agent") or "agent", "agent")
    filename = f"{timestamp}_{task_part}_{agent}.md"
    return REPORTS_ROOT / project / filename


def _format_findings_markdown(parsed: dict) -> str:
    findings = parsed.get("findings") or {}
    if not any(findings.values()):
        return "_Не найдено / не указано в отчёте._"
    lines = []
    for severity in models.SEVERITIES:
        items = findings.get(severity) or []
        if not items:
            continue
        lines.append(f"**{severity}:**")
        lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def render_report_markdown(run: dict, parsed: dict) -> str:
    pre = run.get("pre_run") or {}
    post = run.get("post_run") or {}
    duration = run.get("duration_seconds")
    duration_str = f"{duration:.1f} с" if isinstance(duration, (int, float)) else "—"

    return f"""# Отчёт агента

- Run ID: `{run.get('id', '—')}`
- Task ID: `{run.get('task_id') or '—'}`
- Project: {run.get('project', '—')}
- Agent: {run.get('agent', '—')}
- Task type: {run.get('task_type', '—')}
- Repository: `{run.get('repository_path', '—')}`
- Branch before run: {pre.get('branch') or '—'}
- HEAD before run: {pre.get('head') or '—'}
- Branch after run: {post.get('branch') or '—'}
- HEAD after run: {post.get('head') or '—'}
- Started: {run.get('started_at') or '—'}
- Completed: {run.get('completed_at') or '—'}
- Duration: {duration_str}
- Exit code: {run.get('exit_code')}
- Status: {run.get('status', '—')}

## Prompt

```
{run.get('prompt', '')}
```

## Stdout (полный, без сокращений)

```
{run.get('stdout', '')}
```

## Stderr (полный, без сокращений)

```
{run.get('stderr', '')}
```

## Извлечённые данные (парсер)

- Verdict: {parsed.get('verdict') or 'не определено'}
- Confidence: {parsed.get('confidence', 'none')}
- Commit hash: {parsed.get('commit_hash') or '—'}
- Branch: {parsed.get('branch') or '—'}
- Pull Request: {parsed.get('pull_request_url') or '—'}
- Recommended next action: {parsed.get('recommended_next_action') or '—'}

### Findings

{_format_findings_markdown(parsed)}

## Git status до запуска

```
{pre.get('status_summary') or '—'}
```

## Git status после запуска

```
{post.get('status_summary') or '—'}
```
"""


def save_report(run: dict, parsed: dict) -> Path:
    path = report_path_for(run)
    content = render_report_markdown(run, parsed)
    storage.atomic_write_text(path, content)
    return path


def resolve_report_path(run: dict) -> Path | None:
    """Resolve `run["report_path"]` (relative to ROOT) to an absolute path, but only
    if it stays under REPORTS_ROOT once resolved (symlinks/`..` included).

    `run["report_path"]` is always written by this module's own `save_report`, so it
    is not attacker-influenced through the shipped UI today — this is defense in
    depth against a corrupted or hand-edited `data/runs.jsonl` record referencing an
    arbitrary local file, which would otherwise be read and rendered verbatim by the
    Runs/Reports pages. Returns None (never raises) if `report_path` is missing or
    resolves outside REPORTS_ROOT.
    """
    # One resolver serves both runtime generations.  In particular, it must
    # not join the stored reference to the code root when AICC_REPORTS_ROOT is
    # configured independently.
    from command_center.runtime import reports as runtime_reports

    return runtime_reports.resolve_report_path(run.get("report_path"))
