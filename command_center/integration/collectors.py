"""Read-only health collectors for the Integration Center.

Strictly read-only by contract (``docs/INTEGRATION_CENTER.md``): no mutating
git command, no ``git fetch``, no mutating ``gh`` verb, no writes anywhere.
Every signal degrades to a structured ``available: False`` payload instead of
raising — a missing checkout, a missing ``gh`` binary or an offline network
must never break the Projects page.

Git reads reuse ``command_center.git_info`` (the same helpers
``ui/git_readers`` wraps). GitHub reads go through one private ``_run_gh``
seam so tests mock a single function.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from command_center import git_info

#: Worktree-state vocabulary — same as Workspace Home's `_WORKTREE_STATE_LABELS`.
WORKTREE_STATES: tuple[str, ...] = ("unconfigured", "invalid_path", "not_git_repo", "ok")

_GH_TIMEOUT_SECONDS = 10


def resolve_repo_path(entry: dict) -> Path | None:
    raw = entry.get("repo_path")
    if not raw:
        return None
    return Path(raw).expanduser()


def worktree_state(entry: dict) -> str:
    """`unconfigured` / `invalid_path` / `not_git_repo` / `ok`."""
    path = resolve_repo_path(entry)
    if path is None:
        return "unconfigured"
    if not path.is_dir():
        return "invalid_path"
    if not git_info.get_status(path).get("is_repo"):
        return "not_git_repo"
    return "ok"


def collect_git(repo_path: Path) -> dict:
    """Local, network-free git signals: branch, dirtiness, last activity."""
    status = git_info.get_status(repo_path)
    if not status.get("is_repo"):
        return {"available": False, "error": "not a git repository"}
    last_activity = None
    proc = git_info.run_git_command(repo_path, ["log", "-1", "--format=%cI"])
    if proc is not None and proc.returncode == 0:
        last_activity = proc.stdout.strip() or None
    return {
        "available": True,
        "branch": status.get("branch"),
        "dirty": bool(status.get("dirty")),
        "modified_count": status.get("modified_count", 0),
        "untracked_count": status.get("untracked_count", 0),
        "last_commit_subject": status.get("last_commit_subject"),
        "last_activity": last_activity,
    }


def _run_gh(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess | None:
    """The one seam every GitHub read goes through (tests mock exactly this).

    Read-only by construction: callers only ever pass `pr list` / `run list`.
    Returns `None` when `gh` is missing or does not answer in time.
    """
    try:
        return subprocess.run(
            ["gh", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _gh_json(repo_path: Path, args: list[str]) -> tuple[list | None, str | None]:
    proc = _run_gh(repo_path, args)
    if proc is None:
        return None, "gh unavailable (not installed or timed out)"
    if proc.returncode != 0:
        return None, (proc.stderr or "gh failed").strip()[:200]
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None, "gh returned invalid JSON"
    return (data if isinstance(data, list) else []), None


def collect_github(repo_path: Path, *, default_branch: str = "main") -> dict:
    """CI state of the default branch + open-PR count, via the `gh` CLI."""
    prs, pr_error = _gh_json(
        repo_path, ["pr", "list", "--state", "open", "--json", "number", "--limit", "100"]
    )
    runs, run_error = _gh_json(
        repo_path,
        [
            "run", "list",
            "--branch", default_branch,
            "--limit", "1",
            "--json", "status,conclusion",
        ],
    )
    if prs is None and runs is None:
        return {"available": False, "error": pr_error or run_error}

    ci_state = "unknown"
    if runs:
        latest = runs[0]
        if latest.get("status") != "completed":
            ci_state = "in_progress"
        else:
            ci_state = latest.get("conclusion") or "unknown"
    return {
        "available": True,
        "open_pr_count": len(prs) if prs is not None else None,
        "ci_state": ci_state,
        "error": pr_error or run_error,
    }


def collect_health(entry: dict) -> dict:
    """The full health record for one registry entry (see the design doc).

    Shape is stable regardless of what failed: `worktree_state` is always
    present; `git`/`github` each carry their own `available` flag.
    """
    state = worktree_state(entry)
    health: dict = {
        "id": entry["id"],
        "worktree_state": state,
        "git": {"available": False, "error": state},
        "github": {"available": False, "error": state},
    }
    if state != "ok":
        return health
    repo_path = resolve_repo_path(entry)
    health["git"] = collect_git(repo_path)
    health["github"] = collect_github(
        repo_path, default_branch=entry.get("default_branch") or "main"
    )
    return health
