"""Shared helpers for completion-pipeline integration tests and demonstrations.

Builds a *real* git topology — a bare "remote" plus a working clone with a task
branch and a committed change — so the pipeline's git inspection, push, and
target-branch verification run against genuine git rather than stubs. Only the
GitHub API is faked (via `FakeGitHubClient`); the repository is real, which is
what makes the demonstration scenarios trustworthy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from command_center.runtime import db as runtime_db

_GIT_ENV = [
    "-c",
    "user.name=AICC Test",
    "-c",
    "user.email=aicc-test@example.com",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *_GIT_ENV, *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {repo}: {proc.stderr}\n{proc.stdout}")
    return proc


def rev_parse(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def build_repo(tmp: Path) -> tuple[Path, Path]:
    """Create a bare remote (`remote.git`) with a `main` branch and one initial
    commit, plus a working clone (`work`) tracking it. Returns (remote, work)."""
    tmp.mkdir(parents=True, exist_ok=True)
    remote = tmp / "remote.git"
    seed = tmp / "seed"
    work = tmp / "work"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")

    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    (seed / "README.md").write_text("# project\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "initial commit")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")

    git(tmp, "clone", str(remote), str(work))
    return remote, work


def make_task_branch(work: Path, branch: str, *, filename: str = "feature.py", content: str = "x = 1\n") -> str:
    """Create `branch` off main in the working clone with one commit. Returns
    the head commit sha. Does NOT push."""
    git(work, "checkout", "-b", branch)
    (work / filename).write_text(content)
    git(work, "add", "-A")
    git(work, "commit", "-m", f"implement {branch}")
    return rev_parse(work)


def merge_into_main(remote: Path, tmp: Path, branch: str, *, base: str = "main", squash: bool = False) -> str:
    """Perform a REAL merge of `branch` into `base` on the remote (via a throwaway
    clone) and push. Returns the resulting base-branch head commit sha. Use as a
    `FakeGitHubClient(on_merge=...)` hook so target verification exercises real
    git — including squash merges, where the original commit hash does not
    survive on `base`."""
    server = tmp / f"server-{branch.replace('/', '_')}"
    git(tmp, "clone", str(remote), str(server))
    git(server, "checkout", base)
    if squash:
        git(server, "merge", "--squash", f"origin/{branch}")
        git(server, "commit", "-m", f"squash merge {branch}")
    else:
        git(server, "merge", "--no-ff", f"origin/{branch}", "-m", f"merge {branch}")
    git(server, "push", "origin", base)
    return rev_parse(server, base)


def seed_completed_run(
    db_path: Path,
    *,
    repository_path: str,
    branch: str,
    project: str = "AICC",
    task_title: str = "Autonomous task",
    task_type: str = "implementation",
    state: str = "COMPLETED",
) -> dict:
    """Create task/session/run rows and drive the run to a terminal state."""
    task = runtime_db.create_task(db_path, project=project, title=task_title, task_type=task_type)
    session = runtime_db.create_session(
        db_path, task_id=task["id"], project=project, repository_path=repository_path
    )
    run = runtime_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project=project,
        task_type=task_type,
        repository_path=repository_path,
        prompt="do the task",
        is_resume=False,
        expected_branch=branch,
        launch_source="kanban_task",
    )
    version = run["version"]
    for target in ("QUEUED", "RUNNING"):
        updated = runtime_db.update_run_state(db_path, run["id"], expected_version=version, new_state=target)
        version = updated["version"]
    if state != "RUNNING":
        updated = runtime_db.update_run_state(
            db_path, run["id"], expected_version=version, new_state=state,
            fields={"completed_at": "2026-07-22T12:00:00"},
        )
        if state in runtime_db.TERMINAL_STATES:
            # This helper stands in for the whole Supervisor lifecycle, whose
            # own finalization (report, auto-commit, `process_exited` event)
            # is what `runtime.run_finalizer` marks with `finalized_at` —
            # required by `task_sync` before it will project terminal fields
            # or seed a completion row (VOYN-W0-AICC-SRV-09-FINALIZED-AT).
            # Callers exercising the pre-finalization window itself construct
            # that state directly rather than through this helper.
            runtime_db.mark_run_finalized(db_path, run["id"])
    return runtime_db.get_run(db_path, run["id"])
