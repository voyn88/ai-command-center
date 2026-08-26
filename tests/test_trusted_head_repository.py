"""The credential guard refuses everything it cannot positively vouch for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.assert_trusted_head_repository import (
    UntrustedContextError,
    assert_trusted,
    head_repository_of,
    parse_queue_ref,
)


REPO = "dimastov-lab/ai-command-center"
FORK = "outsider/ai-command-center"
QUEUE_REF = "refs/heads/gh-readonly-queue/main/pr-311-" + "f" * 40


def _event(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _env(tmp_path: Path, event_name: str, payload: dict, **extra: str) -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": _event(tmp_path, payload),
        "GITHUB_TOKEN": "t",
        **extra,
    }


def _pull_request(full_name: str | None) -> dict:
    repo = None if full_name is None else {"full_name": full_name}
    return {"head": {"repo": repo}}


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_refs_that_cannot_carry_foreign_code_are_trusted(event: str) -> None:
    assert assert_trusted({"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": event}) == REPO


def test_a_same_repository_pull_request_is_trusted(tmp_path: Path) -> None:
    env = _env(tmp_path, "pull_request", {"pull_request": _pull_request(REPO)})
    assert assert_trusted(env) == REPO


def test_a_fork_pull_request_is_refused(tmp_path: Path) -> None:
    env = _env(tmp_path, "pull_request", {"pull_request": _pull_request(FORK)})
    with pytest.raises(UntrustedContextError, match=FORK):
        assert_trusted(env)


def test_a_queued_same_repository_pull_request_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scripts.assert_trusted_head_repository._fetch_pull_request",
        lambda number, env: _pull_request(REPO),
    )
    env = _env(tmp_path, "merge_group", {"merge_group": {"head_ref": QUEUE_REF}})
    assert assert_trusted(env) == REPO


def test_a_queued_fork_pull_request_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the merge queue introduces, and the reason this guard exists."""
    seen: list[int] = []

    def resolve(number: int, env: dict[str, str]) -> dict:
        seen.append(number)
        return _pull_request(FORK)

    monkeypatch.setattr("scripts.assert_trusted_head_repository._fetch_pull_request", resolve)
    env = _env(tmp_path, "merge_group", {"merge_group": {"head_ref": QUEUE_REF}})
    with pytest.raises(UntrustedContextError, match=FORK):
        assert_trusted(env)
    assert seen == [311], "the guard must resolve the pull request named by the queue ref"


def test_an_unresolvable_queued_pull_request_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(number: int, env: dict[str, str]) -> dict:
        raise UntrustedContextError("cannot resolve pull request #311")

    monkeypatch.setattr("scripts.assert_trusted_head_repository._fetch_pull_request", explode)
    env = _env(tmp_path, "merge_group", {"merge_group": {"head_ref": QUEUE_REF}})
    with pytest.raises(UntrustedContextError, match="cannot resolve"):
        assert_trusted(env)


def test_the_queue_ref_carries_the_pull_request_number() -> None:
    assert parse_queue_ref(QUEUE_REF) == 311


@pytest.mark.parametrize(
    "head_ref",
    [
        None,
        "",
        "refs/heads/feature/pr-311-" + "f" * 40,  # an ordinary branch posing as a queue ref
        "refs/heads/gh-readonly-queue/main/pr-311",  # no base sha
        "refs/heads/gh-readonly-queue/main/pr-x-" + "f" * 40,  # no number
    ],
)
def test_a_ref_that_does_not_parse_is_refused(head_ref: object) -> None:
    with pytest.raises(UntrustedContextError):
        parse_queue_ref(head_ref)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"head": {}},
        {"head": {"repo": None}},  # a deleted fork
        {"head": {"repo": {"full_name": ""}}},
    ],
)
def test_an_unknown_head_repository_is_refused(payload: object) -> None:
    with pytest.raises(UntrustedContextError):
        head_repository_of(payload)


def test_an_unexpected_event_is_refused(tmp_path: Path) -> None:
    """New triggers are refused until someone decides they are safe."""
    env = _env(tmp_path, "pull_request_target", {"pull_request": _pull_request(FORK)})
    with pytest.raises(UntrustedContextError, match="pull_request_target"):
        assert_trusted(env)


def test_a_missing_event_payload_is_refused() -> None:
    with pytest.raises(UntrustedContextError, match="GITHUB_EVENT_PATH"):
        assert_trusted({"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "merge_group"})


def test_a_missing_token_is_refused(tmp_path: Path) -> None:
    env = _env(tmp_path, "merge_group", {"merge_group": {"head_ref": QUEUE_REF}})
    del env["GITHUB_TOKEN"]
    with pytest.raises(UntrustedContextError, match="GITHUB_TOKEN"):
        assert_trusted(env)
