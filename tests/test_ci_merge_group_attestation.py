"""The push-run attestation reuses a merge-queue verdict only on positive proof."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "merge_group_attestation.py"
SPEC = importlib.util.spec_from_file_location("ci_merge_group_attestation", SCRIPT)
assert SPEC and SPEC.loader
attestation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attestation
SPEC.loader.exec_module(attestation)

REPO = "voyn88/ai-command-center"
SHA = "a" * 40
WORKFLOW_ID = 318759118
LISTING = (
    f"/repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs"
    f"?event=merge_group&head_sha={SHA}&per_page=100"
)


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": SHA,
        "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/ci.yml@refs/heads/main",
        "GITHUB_RUN_ID": "900",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_TOKEN": "t",
    }
    env.update(overrides)
    return env


def _run(**overrides: object) -> dict:
    run = {
        "id": 777,
        "event": "merge_group",
        "head_sha": SHA,
        "path": ".github/workflows/ci.yml",
        "workflow_id": WORKFLOW_ID,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "head_branch": f"gh-readonly-queue/main/pr-1130-{'b' * 40}",
        "status": "completed",
        "conclusion": "success",
    }
    run.update(overrides)
    return run


def _fetcher(*listings: list[dict], current: object = None):
    calls: list[str] = []
    pages = list(listings)

    def fetch(path: str) -> object:
        calls.append(path)
        if path == f"/repos/{REPO}/actions/runs/900":
            if current is not None:
                return current
            return {"path": ".github/workflows/ci.yml", "workflow_id": WORKFLOW_ID}
        assert path == LISTING
        return {"workflow_runs": pages.pop(0) if len(pages) > 1 else pages[0]}

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _no_sleep(_: float) -> None:
    return None


def test_an_exact_successful_merge_group_run_is_reused() -> None:
    run = attestation.attest(_env(), _fetcher([_run()]), _no_sleep)
    assert run["id"] == 777


def test_the_newest_of_several_successful_runs_is_named() -> None:
    run = attestation.attest(_env(), _fetcher([_run(id=5), _run(id=9), _run(id=7)]), _no_sleep)
    assert run["id"] == 9


@pytest.mark.parametrize(
    "overrides",
    [
        {"event": "push"},
        {"event": "pull_request"},
        {"head_sha": "c" * 40},
        {"path": ".github/workflows/acceptance-gate.yml"},
        {"workflow_id": WORKFLOW_ID + 1},
        {"repository": {"full_name": "outsider/ai-command-center"}},
        {"head_repository": {"full_name": "outsider/ai-command-center"}},
        {"head_repository": None},
        {"head_branch": "feature/pr-1130"},
        {"head_branch": "gh-readonly-queue/release/pr-1-x"},
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"conclusion": "skipped"},
        {"conclusion": "neutral"},
        {"id": "777"},
        {"id": True},
    ],
)
def test_any_mismatch_refuses_reuse(overrides: dict) -> None:
    with pytest.raises(attestation.NotAttested):
        attestation.attest(_env(), _fetcher([_run(**overrides)]), _no_sleep)


@pytest.mark.parametrize(
    "overrides",
    [
        {"GITHUB_EVENT_NAME": "merge_group"},
        {"GITHUB_EVENT_NAME": "workflow_dispatch"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_REF": "refs/heads/feature"},
        {"GITHUB_RUN_ATTEMPT": "2"},
        {"GITHUB_RUN_ATTEMPT": ""},
        {"GITHUB_SHA": "abc"},
        {"GITHUB_REPOSITORY": ""},
        {"GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/other.yml@refs/heads/main"},
        {"GITHUB_RUN_ID": "x"},
    ],
)
def test_anything_but_a_first_attempt_push_to_main_refuses(overrides: dict) -> None:
    fetch = _fetcher([_run()])
    with pytest.raises(attestation.NotAttested):
        attestation.attest(_env(**overrides), fetch, _no_sleep)


@pytest.mark.parametrize(
    "current",
    [
        {"path": ".github/workflows/other.yml", "workflow_id": WORKFLOW_ID},
        {"path": ".github/workflows/ci.yml"},
        {"path": ".github/workflows/ci.yml", "workflow_id": "318759118"},
        [],
    ],
)
def test_the_current_run_must_be_ci_yml_with_a_workflow_id(current: object) -> None:
    with pytest.raises(attestation.NotAttested):
        attestation.attest(_env(), _fetcher([_run()], current=current), _no_sleep)


def test_a_malformed_listing_refuses() -> None:
    def fetch(path: str) -> object:
        if path.endswith("/runs/900"):
            return {"path": ".github/workflows/ci.yml", "workflow_id": WORKFLOW_ID}
        return {"workflow_runs": ["not-a-run"]}

    with pytest.raises(attestation.NotAttested):
        attestation.attest(_env(), fetch, _no_sleep)


def test_a_still_running_queue_run_is_polled_then_reused() -> None:
    slept: list[float] = []
    fetch = _fetcher([_run(status="in_progress", conclusion=None)], [_run()])
    run = attestation.attest(_env(), fetch, slept.append)
    assert run["id"] == 777
    assert slept == [attestation.POLL_DELAY_SECONDS]


def test_polling_is_bounded_and_ends_in_a_full_run() -> None:
    slept: list[float] = []
    fetch = _fetcher([_run(status="queued", conclusion=None)])
    with pytest.raises(attestation.NotAttested):
        attestation.attest(_env(), fetch, slept.append, attempts=3)
    assert len(slept) == 2


def test_no_candidate_at_all_does_not_wait() -> None:
    slept: list[float] = []
    with pytest.raises(attestation.NotAttested):
        attestation.attest(_env(), _fetcher([]), slept.append)
    assert slept == []


def _outputs(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


def test_main_writes_reuse_and_names_the_run(tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    env = _env(GITHUB_OUTPUT=str(out))
    assert attestation.main(env, _fetcher([_run()])) == 0
    url = f"https://github.com/{REPO}/actions/runs/777"
    assert _outputs(out) == {"reuse": "true", "run_id": "777", "run_url": url}
    printed = capsys.readouterr().out
    assert "777" in printed and url in printed


@pytest.mark.parametrize("error", [OSError("network down"), ValueError("bad json"), KeyError("x")])
def test_main_turns_every_error_into_a_full_run(tmp_path: Path, error: Exception) -> None:
    def fetch(path: str) -> object:
        raise error

    out = tmp_path / "out"
    assert attestation.main(_env(GITHUB_OUTPUT=str(out)), fetch) == 0
    assert _outputs(out) == {"reuse": "false", "run_id": "", "run_url": ""}


def test_main_without_a_token_is_a_full_run(tmp_path: Path) -> None:
    out = tmp_path / "out"
    env = _env(GITHUB_OUTPUT=str(out))
    del env["GITHUB_TOKEN"]
    assert attestation.main(env) == 0
    assert _outputs(out)["reuse"] == "false"
