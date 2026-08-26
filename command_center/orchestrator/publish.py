"""Publish a completed mutating run as a pull request (BO-S3b, part 1).

The worker executes ``agent_run`` locally; its commits sit in the host clone
and go nowhere until this module publishes them. Publishing is gated by the
same single-writer invariant the pre-push hook enforces: a branch is pushed
only while this process holds the repository's writer lease, acquired through
the very tool the hook verifies (``voyn-lease``) — never bypassed with
``--no-verify``. ``gh``'s own OAuth credential over the HTTPS-rewritten
``origin`` is the push credential (see ``_https_push_target``); the deploy
key is the opt-in switch for publishing at all and the credential only on
the SSH fallback. ``gh`` also opens the PR carrying the ``HEAD_SHA:`` trailer
that result-ingest already parses.

That lease has an on-disk shadow, which is why every ``acquire`` here is
followed by ``install-hooks`` under the identity it just acquired (#351).
The hook presents repository/owner/session/task/pid/process-start read from
``voyn-lease.env`` — one file in the clone's common git dir, shared by every
worktree of that clone — and it used to be written once per host, so it froze
on a long-dead process and ``verify`` refused every push no matter who really
held the lease. The refresh buys exactly one thing, and it is not a standing
invariant: while this publish holds the lease, the file names this publisher.
``release`` leaves it behind, so between publishes the file names a writer
that already released — harmless, because ``verify`` then finds no matching
row and refuses. Nor can it come to name the wrong *live* holder: the lease
authority keys one row per repository id, which every worktree of this clone
publishes under, so no two of them hold it at once, and ``verify`` recomputes
worktree, branch and head from the pushing checkout rather than trusting the
file. Deleting the call brings the frozen identity back; it is not redundant.

Every outcome is data (a ``PublishResult``); this never raises into the
worker loop. A run that produced no commit is reported as ``nothing_to_publish``,
not an error — a review/analysis task legitimately changes no files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from command_center.worker import lease_client

_PR_VIEW_DECODE_ERRORS = (TypeError, ValueError)

__all__ = ["PublishConfig", "PublishResult", "publish_run"]


@dataclass(frozen=True, slots=True)
class PublishConfig:
    lease_tool: str  # path to voyn-lease
    repository: str  # e.g. "ai-command-center"
    owner: str  # writer identity, e.g. "server-worker"
    session: str
    task: str  # the backlog task id
    deploy_key: str  # path to the per-repo deploy private key
    base: str = "main"
    ttl: int = 600
    # False when the caller already holds the writer lease for the whole
    # provision->agent->tests->publish lifecycle (`writer_lease.hold`,
    # VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE) and will release it itself.
    # `acquire`/`install-hooks` stay unconditional either way -- both are
    # idempotent re-affirmations under an already-held lease -- but
    # `release` here is a real termination of the row, not a re-affirmation:
    # dropping it mid-function, before this call's own `gh pr view`/
    # `gh pr create` and the caller's post-publish worktree cleanup, would
    # reopen exactly the unfenced window that lease exists to close. Default
    # True preserves this module's own standalone behavior (its docstring's
    # "acquired through... never bypassed" contract) for any caller that
    # does not hold an outer lease.
    release_lease: bool = True
    # Exact base captured by standalone workspace provisioning. Appended to
    # preserve the positional constructor contract of the older fields.
    base_sha: str | None = None
    # Exact remote task-branch tip captured before the agent ran. None means
    # the branch was absent. It is the force-lease authority; a fresh read may
    # detect drift but must never authorize overwriting it.
    remote_sha: str | None = None
    remote_sha_known: bool = False


@dataclass(frozen=True, slots=True)
class PublishResult:
    ok: bool
    branch: str | None = None
    head_sha: str | None = None
    pr_url: str | None = None
    reason: str = ""


def _run(
    argv: list[str], cwd: Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=False, env=env, timeout=120
    )


def _lease_argv(cfg: PublishConfig, verb: str, repo_path: Path) -> list[str]:
    # Delegates to the shared `lease_client` module so this argv shape and
    # `writer_lease.py`'s (VOYN-W0-AICC-LEASE-FULL-LIFECYCLE-FENCE, the
    # lease held across the whole provision->agent->tests->publish
    # lifecycle, not just this push) can never drift apart.
    identity = lease_client.LeaseIdentity(
        lease_tool=cfg.lease_tool,
        repository=cfg.repository,
        owner=cfg.owner,
        session=cfg.session,
        task=cfg.task,
        ttl=cfg.ttl,
    )
    return lease_client.lease_argv(identity, verb, repo_path)


def _https_push_target(repo_path: Path) -> str | None:
    """The repo's own `origin`, rewritten to HTTPS when it is an
    `git@github.com:` SSH remote — pushed through the host's global `gh`
    credential helper (`gh auth setup-git`, already configured on every
    worker for `gh pr create`/`gh pr view` below) instead of a per-repo
    deploy key. Deploy keys proved unreliable in practice on 2026-08-21:
    GitHub silently blocks them for private repos on this org's Free plan,
    and one denied write even on a *public* repo despite a verified,
    correctly-registered, non-read-only key — with no actionable diagnostic
    on either side. `gh`'s own OAuth credential is what every manual
    recovery this session ultimately fell back to, so it becomes the
    primary path here rather than the last resort."""
    remote = _run(["git", "remote", "get-url", "origin"], repo_path)
    if remote.returncode != 0:
        return None
    url = remote.stdout.strip()
    prefix = "git@github.com:"
    if url.startswith(prefix):
        return "https://github.com/" + url[len(prefix) :]
    if url.startswith("https://github.com/"):
        return url
    return None


def _remote_branch_sha(
    repo_path: Path,
    target: str,
    branch: str,
    env_extra: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Read the exact remote branch tip used by an explicit force lease.

    An empty SHA is a valid observation: it means the branch did not exist,
    and ``--force-with-lease=<ref>:`` then protects creation against another
    writer racing us.  Any malformed or ambiguous answer fails closed.
    """
    ref = f"refs/heads/{branch}"
    remote = _run(["git", "ls-remote", "--heads", target, ref], repo_path, env_extra)
    if remote.returncode != 0:
        return False, ""
    lines = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if not lines:
        return True, ""
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref:
        return False, ""
    sha = lines[0][0]
    if len(sha) != 40 or any(char not in "0123456789abcdefABCDEF" for char in sha):
        return False, ""
    return True, sha.lower()


def _pr_snapshot(repo_path: Path, reference: str) -> tuple[int, dict[str, str] | None]:
    result = _run(
        [
            "gh",
            "pr",
            "view",
            reference,
            "--json",
            "url,headRefOid,baseRefName,state",
        ],
        repo_path,
    )
    if result.returncode != 0:
        return result.returncode, None
    try:
        value = json.loads(result.stdout)
    except _PR_VIEW_DECODE_ERRORS:
        return 0, None
    return 0, value if isinstance(value, dict) else None


def _verified_pr_result(
    repo_path: Path,
    cfg: PublishConfig,
    branch: str,
    head_sha: str,
    durable_target: str,
    durable_env: dict[str, str] | None,
) -> PublishResult:
    """Resolve/create the PR while the repository writer lease is held.

    A URL is not evidence that the PR still points at the commit we pushed.
    Validate head, base and open state, then make one final remote read before
    returning success so the lease fences the entire push -> PR handoff.
    """
    view_status, snapshot = _pr_snapshot(repo_path, branch)
    if view_status != 0:
        body = f"Autonomous delivery of {cfg.task}.\n\nHEAD_SHA: {head_sha}\n"
        created = _run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                cfg.base,
                "--head",
                branch,
                "--title",
                f"{cfg.task}: autonomous delivery",
                "--body",
                body,
            ],
            repo_path,
        )
        if created.returncode != 0:
            return PublishResult(
                ok=False,
                branch=branch,
                head_sha=head_sha,
                reason=f"pr_create_failed: {created.stderr.strip()[:160]}",
            )
        pr_reference = created.stdout.strip()
        if not pr_reference:
            return PublishResult(
                ok=False,
                branch=branch,
                head_sha=head_sha,
                reason="pr_create_missing_url",
            )
        view_status, snapshot = _pr_snapshot(repo_path, pr_reference)
        if view_status != 0:
            return PublishResult(
                ok=False,
                branch=branch,
                head_sha=head_sha,
                reason="pr_unreadable_after_create",
            )

    if snapshot is None:
        return PublishResult(
            ok=False, branch=branch, head_sha=head_sha, reason="pr_snapshot_malformed"
        )
    pr_url = snapshot.get("url", "")
    if snapshot.get("headRefOid", "").lower() != head_sha.lower():
        return PublishResult(
            ok=False, branch=branch, head_sha=head_sha, reason="pr_head_sha_mismatch"
        )
    if snapshot.get("baseRefName") != cfg.base:
        return PublishResult(
            ok=False, branch=branch, head_sha=head_sha, reason="pr_base_mismatch"
        )
    if snapshot.get("state") != "OPEN":
        return PublishResult(
            ok=False, branch=branch, head_sha=head_sha, reason="pr_not_open"
        )
    if not pr_url:
        return PublishResult(
            ok=False, branch=branch, head_sha=head_sha, reason="pr_snapshot_missing_url"
        )
    durable, durable_sha = _remote_branch_sha(
        repo_path, durable_target, branch, durable_env
    )
    if not durable or durable_sha != head_sha.lower():
        return PublishResult(
            ok=False,
            branch=branch,
            head_sha=head_sha,
            reason="remote_branch_changed_during_pr_handoff",
        )
    return PublishResult(ok=True, branch=branch, head_sha=head_sha, pr_url=pr_url)


def _static_quality_gate(repo_path: Path, head_sha: str) -> PublishResult | None:
    """Non-executing pre-push checks over the candidate tree as data
    (VOYN-W0-AICC-PREPUSH-FAST-GATE v2). See the call site in
    ``publish_run`` for the trust rationale. Returns a refusal
    ``PublishResult`` on a red check, ``None`` to proceed.

    Fail-open ONLY on missing tooling (a worker venv without ruff): the
    gate is an economy device and the required CI suite stays
    authoritative, so absent tooling defers to CI rather than blocking
    every publish on that host. A finding in the candidate tree itself
    always refuses. ``VOYN_QUALITY_BAND=off`` skips (operator escape
    hatch, same contract as the interactive band; the skip is visible as
    the publish succeeding without a gate refusal it would otherwise
    hit)."""
    if os.environ.get("VOYN_QUALITY_BAND") == "off":
        return None
    # Repo opt-in mirrors v1: repositories that vendored scripts/ci/prepush/
    # get the gate; others publish as before. The marker is candidate data,
    # so a candidate can only opt OUT -- which merely defers its own red
    # verdict to the authoritative CI suite, never widens anything.
    if not (repo_path / "scripts" / "ci" / "prepush").is_dir():
        return None
    # Never `python -m ruff` with the candidate tree as cwd: `-m` prepends
    # the cwd to sys.path, so a committed `ruff/__main__.py` package would
    # SHADOW the installed tool and execute candidate code right here
    # (verification finding on f232c81). The ruff binary next to the
    # worker's interpreter is a native executable -- no Python path
    # resolution at all; the `-m` fallback (older layouts) runs the
    # interpreter with -P (safe path: cwd never enters sys.path).
    ruff_binary = Path(sys.executable).with_name("ruff")
    if ruff_binary.is_file() and os.access(ruff_binary, os.X_OK):
        argv = [str(ruff_binary), "check", "--no-cache", "."]
    else:
        argv = [sys.executable, "-P", "-m", "ruff", "check", "--no-cache", "."]
    # --isolated: candidate configuration is never read -- a committed
    # `[tool.ruff] exclude = ["**"]` silently neuters the gate otherwise
    # (verification finding on 9e02e55; reproduced live: rc 0 with the
    # exclusion, rc 1 under --isolated). The tree under check contributes
    # only code, never policy; the gate lints by ruff's defaults, which
    # this repository's own committed config matches (default selection,
    # line-length 88). `--isolated --config <trusted>` is not an option:
    # ruff rejects the combination.
    argv.append("--isolated")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    try:
        run = subprocess.run(
            argv,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return PublishResult(
            ok=False, head_sha=head_sha, reason="quality_band_timeout"
        )
    if run.returncode == 0:
        return None
    stderr = run.stderr or ""
    if "No module named ruff" in stderr:
        return None  # tooling absent on this host: defer to CI
    tail = (run.stdout + stderr).strip().splitlines()
    detail = " | ".join(tail[-3:]) if tail else "no output"
    return PublishResult(
        ok=False,
        head_sha=head_sha,
        reason=f"quality_band_failed: {detail[:160]}",
    )


def publish_run(repo_path: Path, cfg: PublishConfig) -> PublishResult:
    """Acquire the lease, push a branch, open a PR. Idempotent on the branch
    name (``backlog/<task>``): a re-run force-updates the same branch and
    reuses the open PR, so a redelivered attempt does not fan out PRs."""
    head = _run(["git", "rev-parse", "HEAD"], repo_path)
    if head.returncode != 0:
        return PublishResult(ok=False, reason="cannot read HEAD")
    head_sha = head.stdout.strip()

    status = _run(["git", "status", "--porcelain"], repo_path)
    if status.returncode != 0:
        return PublishResult(
            ok=False, reason="cannot_read_worktree_status", head_sha=head_sha
        )
    if status.stdout.strip():
        return PublishResult(ok=False, reason="uncommitted_changes", head_sha=head_sha)

    if cfg.base_sha is not None:
        base_sha_value = cfg.base_sha
        base_present = _run(
            ["git", "cat-file", "-e", f"{base_sha_value}^{{commit}}"], repo_path
        )
        if base_present.returncode != 0:
            return PublishResult(
                ok=False, reason="pinned_base_sha_missing", head_sha=head_sha
            )
    else:
        base_sha = _run(["git", "rev-parse", f"origin/{cfg.base}"], repo_path)
        if base_sha.returncode != 0:
            return PublishResult(
                ok=False, reason="cannot_read_base_sha", head_sha=head_sha
            )
        base_sha_value = base_sha.stdout.strip()

    already_durable = (
        cfg.remote_sha_known
        and cfg.remote_sha is not None
        and cfg.remote_sha == head_sha.lower()
    )
    if base_sha_value == head_sha and not already_durable:
        return PublishResult(ok=False, reason="nothing_to_publish", head_sha=head_sha)
    if base_sha_value != head_sha:
        ancestry = _run(
            ["git", "merge-base", "--is-ancestor", base_sha_value, head_sha], repo_path
        )
        if ancestry.returncode != 0:
            return PublishResult(
                ok=False, reason="head_not_descendant_of_pinned_base", head_sha=head_sha
            )

    # Pre-push static quality gate (VOYN-W0-AICC-PREPUSH-FAST-GATE, v2 after
    # the verification REJECT on 254154a): red PR CI runs cost an agent a
    # full diagnose->fix->SHA->CI->review round-trip, so the cheap part of
    # that verdict is taken here, before the push. v2 is deliberately
    # NON-EXECUTING. The v1 ran `scripts/ci/prepush/quality_band.sh` FROM
    # THE CANDIDATE WORKTREE inside this credentialed worker context --
    # candidate-controlled host command execution (verification finding 1),
    # and its env `setdefault` let an inherited variable override the
    # validated selection base (finding 2). Candidate code only ever
    # executes inside the agent's isolated principal, so the publish side
    # keeps exactly the checks that treat the tree as DATA (ruff: parse +
    # lint, which also catches syntax errors), run by the worker's own
    # trusted interpreter with explicit argv and a minimal explicit env --
    # nothing inherited or worktree-resident can redirect them. NOTHING on
    # this publish path runs the impacted-TEST phase: enforcing it for
    # agents requires an allowlisted profile in the privileged
    # principal-isolation launcher (candidate code may only execute under
    # the isolated principal), which is VOYN-W0-AICC-SANDBOX-PREPUSH-TESTS
    # -- a separate security-designed task. Until it lands, tests pre-push
    # exist only in interactive `make prepush`, and the authoritative
    # enforcement remains the required CI suite. `already_durable`
    # redeliveries skip the gate: that head's verdict was taken before the
    # original push.
    if not already_durable:
        gate_failure = _static_quality_gate(repo_path, head_sha)
        if gate_failure is not None:
            return gate_failure

    branch = f"backlog/{cfg.task}"
    if already_durable:
        durable_target = _https_push_target(repo_path)
        durable_env = None
        if durable_target is None:
            durable_target = "origin"
            durable_env = {
                "GIT_SSH_COMMAND": (f"ssh -i {cfg.deploy_key} -o IdentitiesOnly=yes")
            }
        durable, durable_sha = _remote_branch_sha(
            repo_path, durable_target, branch, durable_env
        )
        if not durable or durable_sha != head_sha.lower():
            return PublishResult(
                ok=False, reason="remote_branch_changed_before_pr", head_sha=head_sha
            )
    lease = _run(_lease_argv(cfg, "acquire", repo_path), repo_path)
    if lease.returncode != 0:
        # The lease is held by another writer: a data refusal, the attempt
        # returns to the pool and a later tick retries — never a forced push.
        return PublishResult(
            ok=False, reason=f"lease_unavailable: {lease.stderr.strip()[:120]}"
        )
    # Live-reproduced 2026-08-21: `install-hooks` is what writes the
    # pre-push hook's `voyn-lease.env` (repository/owner/session/task/pid/
    # process-start) -- and it had only ever been run once, at whatever
    # moment the hooks were first provisioned on this host. The pre-push
    # hook reads that FROZEN file on every push and compares it against the
    # lease row THIS `acquire` just wrote fresh, so `verify` refused every
    # push with `VOYN_LEASE_REFUSED verify mismatch` once the identity that
    # provisioned the hooks (an old, long-dead process) no longer matched
    # anything this or any later worker process would ever present. Calling
    # `install-hooks` with the exact same identity args as the `acquire`
    # that just succeeded keeps the hook's on-disk copy of "who currently
    # holds this lease" in lockstep with the database row `verify` actually
    # checks against -- every acquire re-provisions it, not just the first
    # one ever run on a host. Failing this fails closed (release, refuse to
    # push) rather than attempting a push `verify` is already known to
    # reject with this stale a file.
    hooks = _run(_lease_argv(cfg, "install-hooks", repo_path), repo_path)
    if hooks.returncode != 0:
        if cfg.release_lease:
            _run(_lease_argv(cfg, "release", repo_path), repo_path)
        return PublishResult(
            ok=False, reason=f"install_hooks_failed: {hooks.stderr.strip()[:120]}"
        )
    try:
        if already_durable:
            push = None
        else:
            pinned_remote_sha = cfg.remote_sha or ""
            https_target = _https_push_target(repo_path)
            if https_target is not None:
                observed, observed_sha = _remote_branch_sha(
                    repo_path, https_target, branch
                )
                if not observed:
                    return PublishResult(
                        ok=False, reason="cannot_read_remote_branch_for_force_lease"
                    )
                if cfg.remote_sha_known and observed_sha != pinned_remote_sha:
                    return PublishResult(
                        ok=False, reason="remote_branch_changed_before_push"
                    )
                branch_ref = f"refs/heads/{branch}"
                expected_remote_sha = (
                    pinned_remote_sha if cfg.remote_sha_known else observed_sha
                )
                push = _run(
                    [
                        "git",
                        "push",
                        f"--force-with-lease={branch_ref}:{expected_remote_sha}",
                        https_target,
                        f"HEAD:{branch_ref}",
                    ],
                    repo_path,
                )
                durable_target = https_target
                durable_env = None
            else:
                # origin isn't a github.com remote this host knows how to
                # rewrite to HTTPS -- fall back to the configured deploy key.
                git_ssh = f"ssh -i {cfg.deploy_key} -o IdentitiesOnly=yes"
                ssh_env = {"GIT_SSH_COMMAND": git_ssh}
                observed, observed_sha = _remote_branch_sha(
                    repo_path, "origin", branch, ssh_env
                )
                if not observed:
                    return PublishResult(
                        ok=False, reason="cannot_read_remote_branch_for_force_lease"
                    )
                if cfg.remote_sha_known and observed_sha != pinned_remote_sha:
                    return PublishResult(
                        ok=False, reason="remote_branch_changed_before_push"
                    )
                branch_ref = f"refs/heads/{branch}"
                expected_remote_sha = (
                    pinned_remote_sha if cfg.remote_sha_known else observed_sha
                )
                push = _run(
                    [
                        "git",
                        "push",
                        f"--force-with-lease={branch_ref}:{expected_remote_sha}",
                        "origin",
                        f"HEAD:{branch_ref}",
                    ],
                    repo_path,
                    ssh_env,
                )
                durable_target = "origin"
                durable_env = ssh_env
        if push is not None and push.returncode != 0:
            return PublishResult(
                ok=False, reason=f"push_failed: {push.stderr.strip()[:160]}"
            )
        if push is not None:
            durable, durable_sha = _remote_branch_sha(
                repo_path, durable_target, branch, durable_env
            )
            if not durable or durable_sha != head_sha.lower():
                return PublishResult(
                    ok=False,
                    reason="remote_branch_head_not_durable_after_push",
                    head_sha=head_sha,
                )
        return _verified_pr_result(
            repo_path,
            cfg,
            branch,
            head_sha,
            durable_target,
            durable_env,
        )
    finally:
        if cfg.release_lease:
            _run(_lease_argv(cfg, "release", repo_path), repo_path)
