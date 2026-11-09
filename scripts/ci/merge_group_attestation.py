"""Decide whether a push to main may reuse the merge queue's verdict.

With the merge queue on, `main` only ever advances to a commit the queue has
already tested: the `merge_group` run of `ci.yml` checks out the exact commit
that then becomes main's head. The `push` run that follows re-runs the same
suite on the same SHA with the same workflow file, and nothing gates on it.

This script lets the push run's expensive jobs stand down — while still
concluding `success` under their unchanged names — **only** when it can
positively establish that a successful `merge_group` run of this same workflow
exists for this exact commit. Every other outcome, including any error, is
"run everything as before". It never fails the step: the worst it can do is
cost the minutes it was meant to save.

The rule, all of which must hold:

* this run is `push` to `refs/heads/main`, attempt 1 (a manual re-run is a
  request for a real run), of `.github/workflows/ci.yml` in this repository;
* the listed run is in this repository (`repository` and `head_repository`),
  of the same workflow id and path, event `merge_group`, on a
  `gh-readonly-queue/main/` ref, `head_sha == GITHUB_SHA`,
  `status == completed`, `conclusion == success`.

Identical SHA means identical tree, so the `merge_group` run executed this very
`ci.yml` — including its coverage gate.

Outputs (`$GITHUB_OUTPUT`): `reuse` (`true` / `false`), `run_id`, `run_url`.
Standard library only: it runs on the runner's own Python, before setup.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen

WORKFLOW_PATH = ".github/workflows/ci.yml"
MAIN_REF = "refs/heads/main"
QUEUE_BRANCH_PREFIX = "gh-readonly-queue/main/"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_BYTES = 4 * 1024 * 1024
# The queue merges as soon as its required checks pass; the run's own
# `completed` status can trail the last job by a few seconds.
POLL_ATTEMPTS = 4
POLL_DELAY_SECONDS = 15.0

FetchJson = Callable[[str], Any]


class NotAttested(Exception):
    """No positive proof; run the full suite."""


def _api_fetcher(env: dict[str, str]) -> FetchJson:
    token = env.get("GITHUB_TOKEN")
    if not token:
        raise NotAttested("GITHUB_TOKEN is not set")
    api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not api.startswith("https://"):
        raise NotAttested(f"refusing non-https API URL {api!r}")

    def fetch(path: str) -> Any:
        request = Request(  # noqa: S310 - https API host from the runner environment
            f"{api}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "aicc-merge-group-attestation",
            },
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - see above
            body = response.read(_MAX_BYTES + 1)
        if len(body) > _MAX_BYTES:
            raise NotAttested("API response is implausibly large")
        return json.loads(body.decode("utf-8"))

    return fetch


def _require(env: dict[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise NotAttested(f"{name} is not set")
    return value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _full_name(run: dict, key: str) -> object:
    repo = run.get(key)
    return repo.get("full_name") if isinstance(repo, dict) else None


def _matches(run: object, *, repository: str, sha: str, workflow_id: int) -> bool:
    """Everything except completion: is this the queue run for our commit?"""
    if not isinstance(run, dict):
        raise NotAttested("workflow run listing has a non-object entry")
    branch = run.get("head_branch")
    return (
        _is_int(run.get("id"))
        and run.get("event") == "merge_group"
        and run.get("head_sha") == sha
        and run.get("path") == WORKFLOW_PATH
        and run.get("workflow_id") == workflow_id
        and _full_name(run, "repository") == repository
        and _full_name(run, "head_repository") == repository
        and isinstance(branch, str)
        and branch.startswith(QUEUE_BRANCH_PREFIX)
    )


def _succeeded(run: dict) -> bool:
    return run.get("status") == "completed" and run.get("conclusion") == "success"


def attest(
    env: dict[str, str],
    fetch: FetchJson,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = POLL_ATTEMPTS,
) -> dict:
    """Return the successful `merge_group` run for this SHA, or raise NotAttested."""
    if env.get("GITHUB_EVENT_NAME") != "push":
        raise NotAttested(f"event is {env.get('GITHUB_EVENT_NAME')!r}, not push")
    if env.get("GITHUB_REF") != MAIN_REF:
        raise NotAttested(f"ref is {env.get('GITHUB_REF')!r}, not {MAIN_REF}")
    if env.get("GITHUB_RUN_ATTEMPT") != "1":
        raise NotAttested("re-run attempt: a re-run asks for a real run")
    repository = _require(env, "GITHUB_REPOSITORY")
    sha = _require(env, "GITHUB_SHA")
    if not _SHA.fullmatch(sha):
        raise NotAttested("GITHUB_SHA is not a full commit id")
    expected_ref = f"{repository}/{WORKFLOW_PATH}@{MAIN_REF}"
    if env.get("GITHUB_WORKFLOW_REF") != expected_ref:
        raise NotAttested(f"workflow ref is not {expected_ref!r}")
    run_id = _require(env, "GITHUB_RUN_ID")
    if not run_id.isdigit():
        raise NotAttested("GITHUB_RUN_ID is not numeric")

    current = fetch(f"/repos/{repository}/actions/runs/{run_id}")
    if not isinstance(current, dict) or current.get("path") != WORKFLOW_PATH:
        raise NotAttested("this run is not reported as ci.yml")
    workflow_id = current.get("workflow_id")
    if not _is_int(workflow_id):
        raise NotAttested("this run has no workflow id")

    listing = (
        f"/repos/{repository}/actions/workflows/{workflow_id}/runs"
        f"?event=merge_group&head_sha={quote(sha)}&per_page=100"
    )
    for attempt in range(attempts):
        body = fetch(listing)
        runs = body.get("workflow_runs") if isinstance(body, dict) else None
        if not isinstance(runs, list):
            raise NotAttested("workflow run listing is malformed")
        candidates = [
            run
            for run in runs
            if _matches(run, repository=repository, sha=sha, workflow_id=workflow_id)
        ]
        passed = [run for run in candidates if _succeeded(run)]
        if passed:
            return max(passed, key=lambda run: run["id"])
        pending = [run for run in candidates if run.get("status") != "completed"]
        if not pending or attempt == attempts - 1:
            break
        sleep(POLL_DELAY_SECONDS)
    raise NotAttested(f"no successful merge_group run of {WORKFLOW_PATH} for {sha}")


def _write_outputs(env: dict[str, str], values: dict[str, str]) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main(env: dict[str, str] | None = None, fetch: FetchJson | None = None) -> int:
    env = dict(os.environ) if env is None else env
    try:
        run = attest(env, fetch or _api_fetcher(env))
        server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        url = f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{run['id']}"
        outputs = {"reuse": "true", "run_id": str(run["id"]), "run_url": url}
        message = (
            f"REUSE: merge_group run {run['id']} ({url}) passed {WORKFLOW_PATH} "
            f"on exact SHA {env['GITHUB_SHA']}; heavy steps stand down."
        )
    except Exception as error:  # noqa: BLE001 - any doubt means the full run
        outputs = {"reuse": "false", "run_id": "", "run_url": ""}
        message = f"FULL RUN: {type(error).__name__}: {error}"
    print(message)
    _write_outputs(env, outputs)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
