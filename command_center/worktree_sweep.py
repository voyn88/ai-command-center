"""Periodic `git worktree prune` sweep for every project's configured
repository (VOYN-W0-AICC-ISOLATED-WORKTREE-PER-ATTEMPT hardening follow-up).

`workspace_provisioning.remove_workspace` only reconciles the one dangling
`.git/worktrees/<name>` entry left by the worktree it just removed. Three
paths never reach that reconcile point: `remove_workspace` returning
`"not_owned"` or `"remove_failed"` (the directory is deliberately left in
place for inspection/reuse, so its prune never runs), and a worker process
killed between `provision_workspace` and any cleanup attempt at all. This
module is the standalone sweep that reconciles those — run periodically, it
closes the gap without adding any new git-write surface: it calls
`workspace_provisioning.prune_repository`, the exact same read-mostly
primitive `remove_workspace` already uses.

Deliberately its own DB-free entry point rather than a
`python -m command_center.db` subcommand. `queue-reap`
(`command_center/db/cli.py`) runs as the `aicc_app` PostgreSQL role because
lease recovery is a database privilege that role was specifically granted.
This sweep never touches PostgreSQL at all — it reads
`project_config.load_project_configs()` (local host configuration) and runs
`git worktree prune` against each configured repository on this host's
filesystem — so routing it through the DB CLI would hand it a database
dependency and credential it has no use for. It reuses the *pattern*
instead: a oneshot systemd timer (`deploy/systemd/aicc-worktree-prune.timer`)
invoking this module's `main()`, exactly as `aicc-queue-reaper.timer` invokes
`queue-reap` — new unit, no new scheduling infrastructure.
"""

from __future__ import annotations

import logging

from command_center import project_config, workspace_provisioning

logger = logging.getLogger(__name__)

__all__ = ["main", "sweep_configured_repositories"]


def sweep_configured_repositories() -> dict[str, str]:
    """Run `git worktree prune` against every distinct, non-empty
    `repository_path` configured on this host.

    Returns a `{repository_path: outcome}` map — see
    `workspace_provisioning.prune_repository` for outcome values. Best
    effort across the whole set: one repository's failure never stops the
    sweep over the rest, and a `repository_path` shared by two projects is
    pruned once."""
    repository_paths = sorted(
        {
            cfg["repository_path"]
            for cfg in project_config.load_project_configs().values()
            if cfg.get("repository_path")
        }
    )
    return {
        repository_path: workspace_provisioning.prune_repository(repository_path)
        for repository_path in repository_paths
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    results = sweep_configured_repositories()
    if not results:
        print("no configured repositories to sweep")
        return 0
    failures = 0
    for repository_path, outcome in results.items():
        print(f"{outcome:16s} {repository_path}")
        if outcome == "prune_failed":
            failures += 1
    # "not_a_repository" is not a failure: a project can be configured with a
    # repository_path that does not exist on this particular host (a laptop
    # missing a repo another host has cloned), and that is this host's
    # ordinary state, not something for the timer to flag.
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
