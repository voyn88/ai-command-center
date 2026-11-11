"""Self-hosted PR-Agent (Qodo OSS) review companion
(VOYN-W0-AICC-PR-AGENT-INTEGRATION).

Why this exists
----------------
The control plane's own adversarial review (`review_merge.py`) is the only
process with authority to post the `ACCEPTANCE: ACCEPT/REJECT <sha>` marker
`merge_once` reads. PR-Agent is a SEPARATE, bounded, best-effort signal: it
runs the open-source `pr_agent.cli ... review` tool against every open pull
request and lets it post its own plain review comment, exactly as any other
third-party bot would. This module:

* never imports `github_app_auth` or `_post_marker_as_bot`, never constructs
  an `ACCEPTANCE:`/`VERDICT:` line, and invokes PR-Agent's CLI with the bare
  `review` tool and nothing else -- `auto_approve` is never named on the
  command line, and `--config.enable_auto_approval=false` is passed
  explicitly so a future PR-Agent release flipping that default upstream
  does not quietly grant this module an approval path it was never meant to
  have (`_run_pr_agent` below has the exact, tested argv);
* treats its own comment as one more finding for a human or the independent
  reviewer to read, consumed by the review contour -- not a gate the review
  contour (or `merge_once`) has to trust or even look at;
* is fail-closed on credential absence: a preflight (`preflight`) resolves
  which of `GEMINI_API_KEY`/`GROQ_API_KEY` to run under, without ever
  returning, logging, or redacting-and-printing the secret VALUE, and
  refuses outright (never silently prefers one) when both are set, since an
  operator who set both almost certainly meant to replace one, not layer a
  second model's opinion on top. No credential is minted, requested, or
  purchased by this module -- an absent one is reported and skipped, tick
  after tick, exactly like any other "not configured" skip reason elsewhere
  in this package.

Scope deliberately excludes a webhook trigger (the 2026-09-03 owner
decision allowed either "a systemd timer per open PR or webhook"): a
periodic bounded tick needs no inbound network surface on control-01 at
all, and the per-open-PR dedup below (`already_reviewed_at_head`) already
gives it the idempotence a webhook delivery would otherwise have to
provide on its own.

Rejected 2026-11 (PR #735, b350d6a5): the previous attempt at this module
(a) claimed in prose that an in-checkout `--state-path` "can never" make
`self_deploy`'s dirty-tree check (`git status --porcelain`, scoped to the
whole checkout) see a dirty tree, while the code accepted any string
unchecked; (b) spawned a bare `"python"` with no `sys.executable`, which a
host whose PATH carries no `python` symlink (common on current distros)
cannot even exec, and which on a host that DOES have one may not be the venv
PR-Agent was installed into; (c) let `subprocess.TimeoutExpired` and
`FileNotFoundError` propagate out of the per-PR loop uncaught, aborting the
whole tick instead of skipping the one PR as the per-PR-isolation docstring
promised; (d) could leak a token substring from a failed subprocess's
captured stderr/stdout into the in-memory skip reason; (e) its own CLI
handler returned 0 unconditionally, so an operational defect (any of the
above) would tick forever reporting success at the process level; (f) its
test suite asserted argv by substring membership, which passes on an
implementation that violates the very invariant it was meant to enforce.
All six are fixed here: `_state_file` enforces the outside-checkout
requirement in code and raises (not merely documents it), `_run_pr_agent`
uses `sys.executable` with an exact, single argv, every subprocess failure
mode is caught and becomes a report entry rather than an exception, output
captured into a report entry is redacted against every credential this
process holds, and the CLI command below returns non-zero exactly when
`report.errors` is non-empty (see `command_center/db/cli.py`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from command_center.orchestrator import gh_access

__all__ = ["PrAgentConfig", "PrAgentReport", "preflight", "review_once"]

#: Both free-tier providers PR-Agent supports out of the box. Exactly one may
#: be set; see `preflight`.
_CREDENTIAL_ENV_VARS = ("GEMINI_API_KEY", "GROQ_API_KEY")

_PR_URL_RE = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/pull/\d+$")
_ORIGIN_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

#: Tail of captured subprocess output kept in a skip/error reason -- enough
#: to diagnose, bounded so one runaway PR-Agent failure cannot blow up the
#: report (or whatever ingests it) with megabytes of traceback.
_OUTPUT_CAPTURE_CHARS = 500
_REST_PAGE_MAX = 100


@dataclass(frozen=True, slots=True)
class PrAgentConfig:
    repo_path: str = "."
    #: MUST resolve outside `repo_path` -- see `_state_file`. The default is
    #: a bare home-relative dotfile for local/manual runs; the deployed unit
    #: overrides this to its systemd `StateDirectory` path (`StateDirectory=`
    #: never places anything inside a source checkout).
    state_path: str = "~/.aicc-pr-agent-state.json"
    #: Per-PR subprocess budget. A tick killed partway through (process
    #: killed, host rebooted) loses nothing already recorded in the state
    #: file -- each PR's own entry is written and persisted immediately
    #: after that PR's subprocess returns, not batched to the end of the
    #: tick.
    timeout: int = 600
    #: Open PRs examined per tick, newest REST page only (no pagination): a
    #: bounded, best-effort companion tick, not a completeness guarantee --
    #: a PR this tick's single page does not reach is simply picked up by
    #: the next one, exactly like `already_reviewed_at_head` already lets a
    #: slow/failed PR be retried on a later tick without any special-casing.
    pr_limit: int = 20


@dataclass(slots=True)
class PrAgentReport:
    #: (pr_number, pr_url) PR-Agent ran against successfully this tick.
    reviewed: list[tuple[int, str]] = field(default_factory=list)
    #: (pr_number_or_None, reason) -- benign, expected states: no credential
    #: configured, a draft PR, a head already reviewed, a malformed listing
    #: entry. Never makes the CLI command exit non-zero.
    skipped: list[tuple[int | None, str]] = field(default_factory=list)
    #: (pr_number_or_None, reason) -- operational defects an operator should
    #: see: the PR listing itself failing, an unusable state path, PR-Agent's
    #: own subprocess failing/timing out for a specific PR. Makes the CLI
    #: command exit non-zero (see `command_center/db/cli.py`).
    errors: list[tuple[int | None, str]] = field(default_factory=list)
    #: Which credential this tick ran under, for operator visibility -- the
    #: env VAR NAME only, never the secret value (see `preflight`).
    credential_env: str | None = None
    quota: gh_access.GhQuota | None = None


def preflight(env: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Resolve which credential PR-Agent should run under this tick, without
    ever returning, logging, or printing the secret's VALUE.

    Returns ``(env_var_name, detail)``. ``env_var_name`` is ``None`` when
    there is nothing usable to run with -- either no provider is configured
    (``"not_configured"``, the expected steady state on a host with neither
    key set, per the 2026-09-03 owner decision: a missing key is confirmed
    and reported, never auto-acquired) or more than one is
    (``"multiple_providers_configured:..."``), which this refuses outright
    rather than picking a winner, since two different models silently
    disagreeing about which one is "the" PR-Agent opinion is worse than
    neither running.
    """
    env = os.environ if env is None else env
    configured = [name for name in _CREDENTIAL_ENV_VARS if env.get(name)]
    if len(configured) > 1:
        return None, f"multiple_providers_configured:{'+'.join(configured)}"
    if not configured:
        return None, "not_configured"
    return configured[0], f"configured:{configured[0]}"


def _state_file(repo_path: str, state_path: str) -> Path:
    """Resolve the state file path, REFUSING one that resolves inside the
    checkout -- enforced here, not merely documented.

    `self_deploy.self_deploy_once` treats ANY untracked or modified path
    under the checkout root as a dirty tree (`git status --porcelain`,
    unscoped) and refuses to deploy over it. A relative `--state-path`, or
    one that is absolute but still happens to sit under `repo_path`, would
    make this module's own bookkeeping file trip that check on every tick
    after the first write -- exactly the failure mode a prior version of
    this module claimed in prose was impossible while enforcing nothing of
    the kind in code (rejected review of PR #735, chunk 1/3).
    """
    resolved_repo = Path(repo_path).expanduser().resolve()
    resolved_state = Path(state_path).expanduser().resolve()
    try:
        resolved_state.relative_to(resolved_repo)
    except ValueError:
        return resolved_state
    raise ValueError(
        f"state_path {resolved_state} resolves inside repo_path {resolved_repo}; "
        "pass a path outside the checkout (e.g. a systemd StateDirectory path)"
    )


def _load_state(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, str)}


def _save_state(path: Path, state: dict[str, str]) -> None:
    """Atomic whole-file replace (tmp + rename) -- a tick killed mid-write
    must never leave a half-written, unparseable state file behind, which
    would otherwise make every subsequent tick re-review every open PR."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _origin_owner_repo(repo_path: str) -> tuple[str, str] | None:
    """(owner, repo) of the checkout's `origin`, read from git -- the same
    fact and the same parse `review_merge._origin_owner_repo` derives, kept
    as a small local copy rather than an import of that module's private
    helper so this bounded, optional companion carries no coupling to the
    acceptance pipeline's internals."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = _ORIGIN_REMOTE.match((proc.stdout or "").strip())
    return (match.group("owner"), match.group("repo")) if match else None


def _list_open_prs(repo_path: str, limit: int) -> list[dict[str, Any]] | None:
    """Up to `limit` open pull requests, one REST page, through the fleet
    identity `gh_access` resolves (its own quota, never a human's -- see
    that module's docstring). REST, not the GraphQL-backed `gh pr list`
    porcelain, for the same reason `review_merge._list_open_pulls` uses it:
    a plain-request budget this tick cannot blow by itself."""
    origin = _origin_owner_repo(repo_path)
    if origin is None:
        return None
    owner, repo = origin
    per_page = min(max(limit, 1), _REST_PAGE_MAX)
    proc = gh_access.run(
        ["api", f"repos/{owner}/{repo}/pulls?state=open&per_page={per_page}"],
        repo_path,
    )
    if proc.returncode != 0:
        return None
    try:
        decoded = json.loads(proc.stdout or "null")
    except ValueError:
        return None
    return decoded if isinstance(decoded, list) else None


def _redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


def _run_pr_agent(pr_url: str, cfg: PrAgentConfig) -> tuple[bool, str]:
    """Run PR-Agent's CLI against exactly one pull request, isolated from
    every other PR in the tick by being its own subprocess with its own
    timeout.

    `sys.executable`, not a bare `"python"`: this process is already
    running inside whatever venv PR-Agent was installed into, and a child
    spawned any other way is not guaranteed to be -- or to exist at all on a
    host whose PATH carries no unversioned `python` (review of PR #735,
    chunk 2/3, finding 1). The argv is exactly five tokens: the interpreter,
    `-m pr_agent.cli`, `--pr_url <url>`, the bare `review` command, and one
    explicit `--config.enable_auto_approval=false` override -- never an
    additional positional argument, which is what would be needed to select
    any tool other than the comment-only review (see the module docstring).

    Every failure mode this subprocess can produce -- non-zero exit, a hung
    process past `cfg.timeout`, a missing interpreter/module, a process
    that cannot even be spawned -- is caught here and returned as
    `(False, reason)` rather than raised, so one bad PR costs that PR alone;
    a prior version let `TimeoutExpired`/`FileNotFoundError` propagate out
    of the per-PR loop, aborting every PR after the one that hung (review
    of PR #735, chunk 2/3, finding 2). `reason` is redacted against every
    credential this process holds before being returned, since a child
    process environment includes them and some failure modes of the
    underlying HTTP client echo request headers into stderr (review of PR
    #735, chunk 2/3, finding 3).
    """
    argv = [
        sys.executable,
        "-m",
        "pr_agent.cli",
        "--pr_url",
        pr_url,
        "review",
        "--config.enable_auto_approval=false",
    ]
    secrets = [value for name in _CREDENTIAL_ENV_VARS if (value := os.environ.get(name))]
    for name in ("GITHUB__USER_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            secrets.append(value)
    try:
        proc = subprocess.run(
            argv,
            cwd=cfg.repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=cfg.timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed_out_after_{cfg.timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, _redact(f"spawn_failed: {exc}", secrets)[:_OUTPUT_CAPTURE_CHARS]
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-_OUTPUT_CAPTURE_CHARS:]
        return False, _redact(f"exit_{proc.returncode}: {tail}", secrets)
    return True, "ok"


def _review_once(cfg: PrAgentConfig) -> PrAgentReport:
    report = PrAgentReport()
    credential_env, detail = preflight()
    report.credential_env = credential_env
    if credential_env is None:
        report.skipped.append((None, detail))
        return report
    try:
        state_path = _state_file(cfg.repo_path, cfg.state_path)
    except ValueError as exc:
        report.errors.append((None, str(exc)))
        return report
    state = _load_state(state_path)
    prs = _list_open_prs(cfg.repo_path, cfg.pr_limit)
    if prs is None:
        report.errors.append((None, "pr_list_failed"))
        return report
    for pr in prs:
        number = pr.get("number")
        url = pr.get("html_url") or pr.get("url")
        head = (pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None
        if not isinstance(number, int) or not isinstance(url, str) or not isinstance(head, str) or not head:
            report.skipped.append(
                (number if isinstance(number, int) else None, "malformed_pr_list_entry")
            )
            continue
        if pr.get("draft"):
            report.skipped.append((number, "draft"))
            continue
        if not _PR_URL_RE.match(url):
            report.skipped.append((number, f"malformed_pr_url:{url!r}"))
            continue
        if state.get(str(number)) == head:
            report.skipped.append((number, "already_reviewed_at_head"))
            continue
        ok, run_detail = _run_pr_agent(url, cfg)
        if not ok:
            report.errors.append((number, f"pr_agent_failed:{run_detail}"))
            continue
        state[str(number)] = head
        # Persisted after EVERY successful PR, not batched to the end of the
        # tick: a tick killed partway through must not re-run PR-Agent
        # against a PR it already finished (see `PrAgentConfig.timeout`).
        _save_state(state_path, state)
        report.reviewed.append((number, url))
    return report


def review_once(cfg: PrAgentConfig | None = None) -> PrAgentReport:
    """One bounded tick: review every open, non-draft pull request whose
    head sha this state file has not already recorded.

    Never posts, and has no credential or code path to post, the
    `ACCEPTANCE:`/`VERDICT:` marker `review_merge.py`'s own adversarial
    review produces -- see the module docstring. Scoped to one `gh` identity
    and one quota counter (`gh_access.tick`), same as every other control
    tick in this package, so a quota-exhausted credential shows up in the
    report instead of as a bare subprocess failure.
    """
    cfg = cfg or PrAgentConfig()
    with gh_access.tick(cfg.repo_path) as quota:
        report = _review_once(cfg)
        report.quota = quota
    return report
