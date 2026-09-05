"""The merge gateway — the one component allowed to spend merge-capability
(VOYN-W0-AICC-MERGE-GATEWAY, P0-5).

Before this module existed, `orchestrator.review_merge` read a PR's reviews
*and* ran `gh pr merge` through the same private `_gh()` helper — the same
ambient `gh` credential that the worker uses to push branches and open pull
requests, and the same one the same host process would use to fetch a PR's
reviews. A component that can publish evidence about a PR and also merge it
has not separated those two acts, it has just run them one after another; a
verdict is only independent of the thing it authorizes if a *different*
credential is required to act on it. This module is the one place that
credential is spent, and it is deliberately its own:

- ``_gh`` here always overrides ``GH_TOKEN``/``GITHUB_TOKEN`` from the value
  named by ``GatewayConfig.token_env_var`` (``AICC_MERGE_GATEWAY_TOKEN`` by
  default) rather than inheriting whatever `gh` session the host process
  happens to be authenticated as. Missing that variable is a refusal, not a
  silent fall-through to the ambient session — a gateway that quietly reused
  the worker's credential when its own was absent would not be a second
  identity, just an unenforced convention.
- ``merge_once`` resolves its own login via ``gh api user`` on every tick
  (never a configured or cached value) and passes it to
  ``acceptance_policy.evaluate`` as ``merger``: a verdict published by *this*
  identity is refused on the same footing as one published by the PR's
  author (VOYN-W0-AICC-MARKER-REVIEWER-INDEPENDENCE). Publishing the marker
  and spending the merge credential are meant to be two identities that
  cannot stand in for each other, not two checks run by whichever identity
  holds both.

Every other condition the ticket calls out is `evaluate`'s job, not
duplicated here: PR state, exact head sha, an independent non-dismissed
verdict, no active rejection. What this module adds on top is the piece
`evaluate` cannot see from a review list alone — required checks in a
*terminal* success state (a check that has not finished yet is not evidence
of anything, so it blocks exactly like a failure, not like a pass) — and
fail-closed handling of the GitHub API itself: a `gh` invocation that errors,
times out, or returns unparseable JSON is recorded as an ``error``, distinct
from an ordinary not-ready-yet ``skip``, so an operator sees "the gateway
could not reach GitHub" rather than a task that silently never merges. The
CLI entry point (`command_center.db backlog-merge`) turns a non-empty
``errors`` list into a non-zero exit, so this surfaces as a failed run
instead of an invisible skip-and-retry-forever.

``worker`` and ``planner`` hold no GitHub write credential and call no
`gh` subcommand at all (`orchestrator.publish` pushes branches and opens
pull requests under the worker's own push credential, which is a distinct
concern from merging one); this module is the only caller of `gh pr merge`
in the autonomous backlog pipeline.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from command_center.orchestrator.acceptance_policy import POLICY_VERSION, AcceptanceError, evaluate

__all__ = ["GatewayConfig", "LoopReport", "merge_once"]

_PR_FIELDS = "state,headRefOid,reviews,statusCheckRollup,author"


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    #: Name of the environment variable carrying the gateway's own GitHub
    #: credential. Never a token value itself — the value lives in the
    #: process environment, provisioned separately from whatever `gh`
    #: session the worker/review host is authenticated as.
    token_env_var: str = "AICC_MERGE_GATEWAY_TOKEN"
    merge_method: str = "squash"
    max_per_tick: int = 8


@dataclass
class LoopReport:
    merged: list[tuple[str, str]] = field(default_factory=list)
    #: An ordinary, expected reason to do nothing yet: no marker, checks
    #: still red, an active rejection, a verdict issued by the author or by
    #: this gateway itself. Retried next tick without anyone needing to look.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: GitHub was unreachable, unauthenticated, or returned something this
    #: gateway could not parse — the ticket's "no access to GitHub is a
    #: refusal, not skip-and-retry-forever". Kept apart from `skipped` so a
    #: caller can fail loudly (non-zero exit) instead of looking identical
    #: to a PR that just isn't accepted yet.
    errors: list[tuple[str, str]] = field(default_factory=list)


def _gh(argv: list[str], repo_path: str, token: str) -> subprocess.CompletedProcess[str]:
    # GH_TOKEN takes precedence over any cached `gh auth login` state and
    # over GITHUB_TOKEN; both are set explicitly so this call can never fall
    # through to whatever session the host process happens to hold.
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    return subprocess.run(
        ["gh", *argv], cwd=repo_path, capture_output=True, text=True,
        check=False, timeout=120, env=env,
    )


def _rows(factory: Any, sql: str, params: tuple = ()) -> list[tuple]:
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _resolve_identity(repo_path: str, token: str) -> str:
    """The gateway's own GitHub login, resolved fresh on every call from
    whichever credential `token` names — never configured or cached, so it
    cannot drift from the credential actually in effect."""
    proc = _gh(["api", "user", "--jq", ".login"], repo_path, token)
    if proc.returncode != 0:
        raise AcceptanceError(f"cannot resolve gateway identity: {proc.stderr.strip()[:200]}")
    login = proc.stdout.strip()
    if not login:
        raise AcceptanceError("gateway identity resolved to an empty login")
    return login


def _fetch_pr(repo_path: str, token: str, pr_url: str) -> dict:
    proc = _gh(["pr", "view", pr_url, "--json", _PR_FIELDS], repo_path, token)
    if proc.returncode != 0:
        raise AcceptanceError(f"gh_view_failed: {proc.stderr.strip()[:200]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"gh_view_unparseable: {error}") from error
    if not isinstance(data, dict):
        raise AcceptanceError("gh_view returned a non-object")
    return data


def _checks_terminal_success(rollup: object) -> tuple[bool, str]:
    """Every required check must have *finished* successfully — a check that
    is still queued or in progress (``conclusion`` absent) is not evidence of
    anything and blocks exactly like a failure would, rather than passing by
    default. The acceptance-gate check itself is excluded: it re-evaluates
    the same policy this gateway enforces directly, and requiring it green
    here would make the gateway depend on its own judgement being echoed
    back by a separate CI run rather than made once, here."""
    if not isinstance(rollup, list):
        return False, "checks_rollup_missing"
    bad = []
    for check in rollup:
        if not isinstance(check, dict):
            return False, "checks_rollup_malformed"
        name = check.get("name") or check.get("context") or "?"
        if "cceptance" in str(name):
            continue
        if check.get("conclusion") not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            bad.append(name)
    if bad:
        return False, f"checks_not_terminal_success: {bad[:5]}"
    return True, ""


def merge_once(factory: Any, repo_path: str, cfg: GatewayConfig | None = None) -> LoopReport:
    """Merge every READY_TO_REVIEW task whose PR is open on the expected
    head, carries an independent non-dismissed ACCEPT verdict naming that
    exact head, and whose required checks have all finished successfully;
    close it DONE with the merged sha as evidence. Fails closed: a task
    whose PR state cannot be established from GitHub is an error, never a
    silent, indefinite skip."""
    cfg = cfg or GatewayConfig()
    report = LoopReport()

    token = os.environ.get(cfg.token_env_var)
    if not token:
        report.errors.append(("*", f"{cfg.token_env_var} is not set; refusing to merge with no credential"))
        return report
    try:
        merger_login = _resolve_identity(repo_path, token)
    except AcceptanceError as error:
        report.errors.append(("*", str(error)))
        return report

    tasks = _rows(
        factory,
        "SELECT t.task_id, e.value FROM backlog_task t "
        "JOIN backlog_evidence e ON e.task_id = t.task_id AND e.kind = 'pr' "
        "WHERE t.status = 'READY_TO_REVIEW' ORDER BY t.updated_at LIMIT %s",
        (cfg.max_per_tick,),
    )
    for task_id, pr_url in tasks:
        try:
            data = _fetch_pr(repo_path, token, pr_url)
        except AcceptanceError as error:
            report.errors.append((task_id, str(error)))
            continue

        if data.get("state") != "OPEN":
            report.skipped.append((task_id, f"pr_{str(data.get('state')).lower()}"))
            continue

        head = data.get("headRefOid")
        author = data.get("author")
        author_login = author.get("login") if isinstance(author, dict) else None
        try:
            accepting = evaluate(data.get("reviews"), head, author_login, merger=merger_login)
        except AcceptanceError as error:
            report.skipped.append((task_id, str(error)))
            continue

        checks_ok, reason = _checks_terminal_success(data.get("statusCheckRollup"))
        if not checks_ok:
            report.skipped.append((task_id, reason))
            continue

        merged = _gh(
            ["pr", "merge", pr_url, f"--{cfg.merge_method}", "--match-head-commit", head],
            repo_path, token,
        )
        if merged.returncode != 0:
            report.errors.append((task_id, f"merge_failed: {merged.stderr.strip()[:200]}"))
            continue

        # Evidence and the DONE transition commit together or not at all —
        # the app factory is autocommit, so this needs an explicit
        # transaction. backlog_transition's third argument is the
        # optimistic-lock revision (read here as a plain SELECT: the app
        # role writes only through functions, so no row lock is needed); the
        # actor is session_user inside the function, not an argument.
        with factory() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT revision FROM backlog_task WHERE task_id = %s",
                        (task_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        conn.rollback()
                        report.skipped.append((task_id, "task_vanished"))
                        continue
                    revision = row[0]
                    cur.execute(
                        "SELECT backlog_record_evidence(%s, 'sha', %s)", (task_id, head)
                    )
                    cur.execute(
                        "SELECT backlog_record_evidence(%s, 'acceptance', %s)",
                        (task_id, f"reviewer={accepting} merger={merger_login} policy={POLICY_VERSION} sha={head}"),
                    )
                    cur.execute(
                        "SELECT ok, reason FROM backlog_transition(%s, 'DONE', %s)",
                        (task_id, revision),
                    )
                    ok, transition_reason = cur.fetchone()
                if ok:
                    conn.commit()
                    report.merged.append((task_id, head))
                else:
                    conn.rollback()
                    report.skipped.append((task_id, f"transition:{transition_reason}"))
            finally:
                conn.autocommit = True
    return report
