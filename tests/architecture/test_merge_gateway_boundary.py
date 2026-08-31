"""Architecture fitness: only the merge gateway may ask GitHub to merge a
pull request (VOYN-W0-AICC-PRIVILEGED-MERGE-GATEWAY).

Credential isolation — a worker or planner process holding no merge-capable
token (see ``deploy/systemd/aicc-backlog-merge.service`` and
``test_worker_and_planner_units_carry_no_merge_credential`` below) — only
closes the bypass if application code also cannot route around it by
constructing its own `gh pr merge` / merge-endpoint call somewhere else. This
file is the mechanical half of that guarantee: it scans every non-test Python
file under the backlog automation fleet's own packages — the Postgres-backed
``command_center/orchestrator/`` and ``command_center/worker/`` trees this
task's threat model is about — for the two shapes a merge request can take —
the `gh` CLI subcommand pair and the REST "Merge a pull request" endpoint
path — and fails if either appears anywhere but
``command_center/orchestrator/merge_gateway.py``. A grep, not an AST walk:
the two shapes are literal source text in every legitimate use (a `gh` argv
list, an f-string endpoint path), so a text scan catches them exactly as
reliably and stays readable as the single source of truth for "what counts as
a merge call site" here.

Deliberately excluded: ``command_center/runtime/`` — the desktop Execution
Center's own ``CompletionOrchestrator``/``GitHubClient``
(``runtime/completion_service.py``, ``runtime/github.py``) also calls
`gh pr merge`, but it predates this task (merged as `fix/p1-automerge-safety`,
PR #52) and is a different trust domain by design, not an oversight this scan
should paper over: it runs inside a single developer's desktop app under that
developer's own ambient `gh auth`, gated by its own pre-existing independent
review verdict (`completion.review_verdict`, populated by a separate reviewer
session — see ``completion_service._step_review_gate``), not the shared
service-identity credential a headless worker/planner fleet would hold. Rather
than collapsing two purpose-built acceptance mechanisms into one gateway
built and reviewed for the fleet, that pipeline's isolation is that task's own
concern.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = (
    REPO_ROOT / "command_center" / "orchestrator",
    REPO_ROOT / "command_center" / "worker",
)
GATEWAY_REL_PATH = "command_center/orchestrator/merge_gateway.py"

DEPLOY_DIR = REPO_ROOT / "deploy" / "systemd"
WORKER_UNITS = ("aicc-worker.service",)
PLANNER_UNITS = ("aicc-backlog-planner.service",)
GATEWAY_TOKEN_ENV = "VOYN_MERGE_GATEWAY_TOKEN"

# The `gh pr merge <pr>` CLI argv shape.
_CLI_MERGE = re.compile(r'"pr",\s*"merge"')
# The REST "Merge a pull request" endpoint path, however the owner/repo/number
# segments are spelled (f-string interpolation, %-formatting, string concat).
_API_MERGE = re.compile(r"pulls/[^\"']*/merge")


def _python_files():
    for scanned in SCANNED_DIRS:
        for path in sorted(scanned.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "__pycache__" in rel:
                continue
            yield rel, path


def test_only_the_merge_gateway_calls_gh_pr_merge():
    offenders = []
    for rel, path in _python_files():
        if rel == GATEWAY_REL_PATH:
            continue
        text = path.read_text(encoding="utf-8")
        if _CLI_MERGE.search(text) or _API_MERGE.search(text):
            offenders.append(rel)
    assert offenders == [], (
        "only merge_gateway.py may construct a `gh pr merge` / pull-request-merge "
        f"call site: found one in {offenders}"
    )


def test_the_gateway_file_itself_still_matches_the_pattern():
    """Negative control: if this goes green because the merge call in
    merge_gateway.py stopped matching the patterns above, the assertion in
    the previous test would be vacuously true rather than meaningful."""
    text = (REPO_ROOT / GATEWAY_REL_PATH).read_text(encoding="utf-8")
    assert _API_MERGE.search(text), (
        "merge_gateway.py no longer contains a recognizable merge-endpoint call "
        "— update the pattern this fitness test checks, deliberately"
    )


def _service_text(name: str) -> str:
    path = DEPLOY_DIR / name
    assert path.is_file(), f"expected systemd unit {path} to exist"
    return path.read_text(encoding="utf-8")


def test_worker_and_planner_units_carry_no_merge_credential():
    """The merge gateway's credential must not be reachable from a worker or
    planner process's own environment. This can't be established by reading
    application code — a process's environment is whatever its systemd unit
    (or EnvironmentFile) hands it — so this test reads the deployed unit
    files directly, the same artifact systemd itself acts on."""
    for name in WORKER_UNITS + PLANNER_UNITS:
        text = _service_text(name)
        assert GATEWAY_TOKEN_ENV not in text, (
            f"{name} must never reference {GATEWAY_TOKEN_ENV} — that credential "
            "belongs to the merge gateway's own service identity only"
        )


def test_the_merge_tick_runs_under_the_gateways_own_identity():
    """`aicc-backlog-merge.service` is what actually runs merge_once, so it is
    the one unit that legitimately needs the gateway credential — but it must
    get it from the gateway's own user/env file, not the worker's. Sharing
    `User=aicc-worker` with the worker daemon was the exact deployment-level
    version of the bug this task fixes: two roles, one identity."""
    text = _service_text("aicc-backlog-merge.service")
    assert "User=aicc-worker" not in text, (
        "aicc-backlog-merge.service must not run as aicc-worker — that reunites "
        "the merge identity with the identity that pushes branches and opens "
        "pull requests"
    )
    assert "User=aicc-merge-gateway" in text
    assert "/etc/ai-command-center.env" not in text, (
        "the merge tick must not read the worker's shared env file"
    )
