"""Self-hosted PR-Agent (Qodo OSS) wired to the review contour
(VOYN-W0-AICC-PR-AGENT-INTEGRATION).

What this is and is not
------------------------
`review_merge.py` already runs this pipeline's OWN adversarial reviewer and
is the only thing that ever writes the ``ACCEPTANCE: ACCEPT <sha>`` marker
`scripts/assert_independent_acceptance.py` and `_has_accept_marker` look
for -- that is the one mechanism the acceptance gate is built around, and it
must stay singular (see `_acceptance_app_credentials`'s docstring for why
a second same-purpose identity was exactly the bug on PRs #354/#355).

This module adds a SECOND, independent, off-the-shelf reviewer
(https://github.com/qodo-ai/pr-agent) that runs beside it and never
participates in that decision at all: its findings are posted as an
ordinary PR comment for a human or the existing reviewer to weigh, and
nothing here ever calls `gh pr review`, never authors a line matching the
marker's shape, and never gates a merge. "Consumed by the acceptance lane"
means exactly that -- informational input to a decision this module does
not make, not a second vote.

Why comment-only is safe by construction, not by content filtering
--------------------------------------------------------------------
The one way PR-Agent can affect anything GitHub-visible beyond a comment is
its own `auto_approve` feature (`pr_reviewer.py`'s `auto_approve_logic`,
gated on `config.enable_auto_approval`) -- and that branch is reachable
ONLY when the CLI is invoked with the extra positional argument
``review auto_approve``. `_run_pr_agent` below never passes it, so this
integration cannot submit a formal GitHub PR review (approve or otherwise)
regardless of `.pr_agent.toml` drift; the `enable_auto_approval=false`
overrides it still passes are defense in depth, not the primary control.

That also means a diff engineered to provoke the LLM into writing text that
*looks like* the marker has nothing to attach to: `_has_accept_marker` and
`assert_independent_acceptance.py`'s `evaluate` both read only a PR's
formal ``reviews`` (`GET .../pulls/{n}/reviews`), never its issue comments,
and PR-Agent's `review` tool publishes exclusively via
`GithubProvider.publish_comment` -- a plain issue comment, a different API
object the marker scan never looks at.

Why GEMINI_API_KEY / GROQ_API_KEY, not a hard-coded model
-------------------------------------------------------------
Free-tier credential, free-tier model -- and like `OPENAI_MODEL` beside
`OPENAI_API_KEY` in `.env.example`, the model name is never hard-coded in
code here either: `PR_AGENT_MODEL` names it, so an operator can swap
providers or move to a newer model generation without a code change. Both
keys are optional; `review_once` skips loudly (`pr_agent_not_configured`)
until the control host's `.env` carries one of them plus the model name --
this is the literal BLOCKED-ON-CREDENTIAL state the task decision recorded,
and it stays a normal, successful tick, not a failure.

Why a local JSON state file, not the review-cycle Postgres tables
----------------------------------------------------------------------
PR-Agent decides nothing and gates nothing, so it needs no durable row in
the queue schema `review_once`/`publish_review_verdicts` own -- only enough
memory to avoid re-spending free-tier LLM quota re-reviewing a PR whose
head has not moved since the last tick. Losing this file (a fresh host, a
wiped state directory) costs one redundant comment update per open PR, not
correctness. It is written outside the git checkout on purpose: a stray
file inside the working tree would make `self_deploy.py`'s clean-checkout
refusal see a dirty tree it did not create.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["LoopReport", "PrAgentConfig", "review_once"]


@dataclass(frozen=True, slots=True)
class PrAgentConfig:
    repo_path: str
    #: Outside the git checkout -- see the module docstring's last section.
    state_path: Path
    #: Free-tier LLM quota is the scarce resource here, not compute.
    max_per_tick: int = 3
    #: Per-PR subprocess budget.
    timeout: int = 480


@dataclass
class LoopReport:
    #: (pr_number, pr_url) PR-Agent ran `review` against this tick.
    reviewed: list[tuple[str, str]] = field(default_factory=list)
    #: (pr_number, reason) -- "*" as the number for a whole-tick skip.
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Credentials:
    model: str
    #: Extra environment PR-Agent's dynaconf settings loader reads --
    #: never anything already in this process's own environment.
    env: dict[str, str]


def _credentials() -> _Credentials | None:
    """All-or-nothing like `_acceptance_app_credentials`: a model name with
    no key, or a key with no model name, is a misconfiguration reported as
    a skip, never guessed at."""
    model = os.environ.get("PR_AGENT_MODEL", "").strip()
    gemini = os.environ.get("GEMINI_API_KEY", "").strip()
    groq = os.environ.get("GROQ_API_KEY", "").strip()
    if not model or not (gemini or groq):
        return None
    env: dict[str, str] = {}
    if gemini:
        env["GOOGLE_AI_STUDIO__GEMINI_API_KEY"] = gemini
    if groq:
        env["GROQ__KEY"] = groq
    return _Credentials(model=model, env=env)


def _gh(argv: list[str], repo_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *argv], cwd=repo_path, capture_output=True, text=True,
        check=False, timeout=120,
    )


def _gh_token(repo_path: str) -> str | None:
    """PR-Agent posts its comment via this same ambient `gh` identity --
    unlike the acceptance marker, a plain comment needs no independence
    from the PR's own author (see the module docstring)."""
    token = _gh(["auth", "token"], repo_path)
    if token.returncode != 0:
        return None
    value = token.stdout.strip()
    return value or None


def _open_prs(repo_path: str) -> list[dict[str, Any]] | None:
    listed = _gh(
        [
            "pr", "list", "--state", "open",
            "--json", "number,url,headRefOid,isDraft",
        ],
        repo_path,
    )
    if listed.returncode != 0:
        return None
    try:
        prs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(prs, list):
        return None
    # A draft is not ready for review commentary any more than it is ready
    # for review_merge.py's own reviewer.
    return [pr for pr in prs if isinstance(pr, dict) and not pr.get("isDraft")]


def _load_state(state_path: Path) -> dict[str, str]:
    try:
        raw = state_path.read_text()
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state_path: Path, state: dict[str, str]) -> None:
    """Atomic tmp+rename, same durability idiom as `backlog-export`'s
    whole-file writes -- a tick killed mid-write must not corrupt the
    cache, only cost one redundant re-review next tick."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(state_path)


def _run_pr_agent(
    pr_url: str,
    creds: _Credentials,
    github_token: str,
    cfg: PrAgentConfig,
) -> subprocess.CompletedProcess[str]:
    """Invoke PR-Agent's bare ``review`` tool -- and only that tool. See
    the module docstring for why no other positional command is ever
    passed here."""
    env = dict(os.environ)
    env.update(creds.env)
    env["GITHUB__USER_TOKEN"] = github_token
    env["GITHUB__DEPLOYMENT_TYPE"] = "user"
    return subprocess.run(
        [
            "python", "-m", "pr_agent.cli",
            "--pr_url", pr_url,
            "review",
            f"--config.model={creds.model}",
            "--pr_reviewer.enable_auto_approval=false",
            "--config.enable_auto_approval=false",
        ],
        cwd=cfg.repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=cfg.timeout,
        env=env,
    )


def review_once(cfg: PrAgentConfig) -> LoopReport:
    """One tick: run PR-Agent's `review` against every open, non-draft PR
    whose head is not already the one recorded in the state file, up to
    `cfg.max_per_tick`. Idempotent and crash-safe: the state file is
    updated after each PR individually, so a tick killed partway through
    loses nothing already covered."""
    report = LoopReport()
    creds = _credentials()
    if creds is None:
        report.skipped.append(("*", "pr_agent_not_configured"))
        return report
    token = _gh_token(cfg.repo_path)
    if not token:
        report.skipped.append(("*", "github_token_unavailable"))
        return report
    prs = _open_prs(cfg.repo_path)
    if prs is None:
        report.skipped.append(("*", "gh_pr_list_failed"))
        return report
    state = _load_state(cfg.state_path)
    reviewed_this_tick = 0
    for pr in prs:
        if reviewed_this_tick >= cfg.max_per_tick:
            break
        number = str(pr.get("number") or "")
        head = pr.get("headRefOid") or ""
        url = pr.get("url") or ""
        if not (number and head and url):
            continue
        if state.get(number) == head:
            continue
        result = _run_pr_agent(url, creds, token, cfg)
        if result.returncode != 0:
            reason = (result.stderr or result.stdout or "").strip()[-500:]
            report.skipped.append((number, f"pr_agent_failed: {reason}"))
            continue
        state[number] = head
        _save_state(cfg.state_path, state)
        report.reviewed.append((number, url))
        reviewed_this_tick += 1
    return report
