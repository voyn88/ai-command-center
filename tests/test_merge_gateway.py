"""merge_gateway: the privileged merge gateway (VOYN-W0-AICC-PRIVILEGED-
MERGE-GATEWAY-REM).

Every read goes through a fake HTTP transport recorded call-by-call; the
actual `gh pr merge` dispatch goes through a fake subprocess runner recorded
separately. The central property every refusal test in this file asserts is
not merely "the returned MergeResult says refused" but "the runner was never
invoked at all" -- a gateway whose internal bookkeeping says no but which
still made the privileged call underneath it is not fail-closed, it is a
label on an open door. (Independent review of an earlier version of this
gateway, PR #523 at fc377875, found exactly that gap: refusal tests that
never inspected whether the merge call itself had actually been made.)
"""

from __future__ import annotations

import subprocess

import pytest

from command_center.orchestrator import github_app_auth, merge_gateway

OWNER, REPO, NUMBER = "x", "y", "42"
PR_URL = f"https://github.com/{OWNER}/{REPO}/pull/{NUMBER}"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
AUTHOR = "task-author"
REVIEWER = "voyn88-acceptance-gate[bot]"
MERGE_SHA = "c" * 40

CREDS = github_app_auth.GitHubAppCredentials("1", "2", "unused")


def _pull(*, state="open", head=HEAD, author=AUTHOR):
    return {"state": state, "head": {"sha": head}, "user": {"login": author}}


def _accept_review(sha=HEAD, login=REVIEWER, state="COMMENTED"):
    return {"body": f"ACCEPTANCE: ACCEPT {sha}", "user": {"login": login}, "state": state}


def _reject_review(sha=HEAD, login=REVIEWER, state="COMMENTED"):
    return {"body": f"ACCEPTANCE: REJECT {sha}", "user": {"login": login}, "state": state}


def _check_runs(*, conclusion="success", name="CI"):
    return {
        "total_count": 1,
        "check_runs": [
            {"name": name, "conclusion": conclusion, "status": "completed",
             "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:05:00Z"}
        ],
    }


REVIEWS_PATH = f"/repos/{OWNER}/{REPO}/pulls/{NUMBER}/reviews"
CHECK_RUNS_PATH = f"/repos/{OWNER}/{REPO}/commits/{HEAD}/check-runs"
STATUSES_PATH = f"/repos/{OWNER}/{REPO}/commits/{HEAD}/statuses"
PULL_PATH = f"/repos/{OWNER}/{REPO}/pulls/{NUMBER}"


def _fake_transport(responses):
    """responses: {(method, path): value_or_callable}. Records every call
    made through it -- the seam every test in this file inspects to prove
    (or disprove) that the gateway read only what it claims to have read."""
    calls = []

    def transport(method, path, token, *, body=None):
        calls.append((method, path, token, body))
        key = (method, path)
        if key not in responses:
            raise AssertionError(f"unexpected transport call: {method} {path}")
        value = responses[key]
        return value() if callable(value) else value

    return transport, calls


def _fake_runner(*, returncode=0, stdout="merged", stderr=""):
    """The injected `gh` subprocess seam. Records every invocation -- an
    empty `calls` list after a refusal is the property this whole test file
    exists to pin."""
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return runner, calls


def _ok_responses(*, reviews=None, checks=None, statuses=None, pull=None):
    reviews = [_accept_review()] if reviews is None else reviews
    checks = _check_runs() if checks is None else checks
    statuses = [] if statuses is None else statuses
    pull = _pull() if pull is None else pull
    return {
        ("GET", PULL_PATH): pull,
        ("GET", f"{REVIEWS_PATH}?per_page=100&page=1"): reviews,
        ("GET", f"{CHECK_RUNS_PATH}?per_page=100&page=1"): checks,
        ("GET", f"{STATUSES_PATH}?per_page=100&page=1"): statuses,
    }


def _run(responses, *, expected_head_sha=HEAD, runner_kwargs=None, creds=CREDS):
    transport, transport_calls = _fake_transport(responses)
    runner, runner_calls = _fake_runner(**(runner_kwargs or {}))
    result = merge_gateway.evaluate_and_merge(
        PR_URL, expected_head_sha, creds=creds, request=transport, runner=runner,
    )
    return result, transport_calls, runner_calls


def _assert_refused(result, runner_calls, *, reason_prefix=None):
    assert result.attempted is False
    assert result.returncode is None
    if reason_prefix is not None:
        assert result.refusal_reason.startswith(reason_prefix), result.refusal_reason
    # The property chunk 2 of the PR #523 rejection demands: a refusal must
    # mean the privileged command was never dispatched, not merely that the
    # result object says so.
    assert runner_calls == []


# -- success ------------------------------------------------------------------


def test_merges_when_every_check_passes(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    result, transport_calls, runner_calls = _run(_ok_responses())
    assert result.attempted is True
    assert result.returncode == 0
    assert result.reviewer == REVIEWER
    assert len(runner_calls) == 1
    argv, kwargs = runner_calls[0]
    assert argv == ["gh", "pr", "merge", PR_URL, "--squash"]
    # The gateway's own freshly-minted token, not any ambient credential --
    # and GITHUB_TOKEN must not linger in the subprocess env to compete
    # with it.
    assert kwargs["env"]["GH_TOKEN"] == "gw-token"
    assert "GITHUB_TOKEN" not in kwargs["env"]
    # Every read carried the gateway's own token, never a caller-supplied one.
    assert all(token == "gw-token" for _method, _path, token, _body in transport_calls)


def test_success_reads_reviews_checks_and_statuses_exactly_once(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    _result, transport_calls, _runner_calls = _run(_ok_responses())
    paths = [path for _method, path, _token, _body in transport_calls]
    assert paths == [
        PULL_PATH,
        f"{REVIEWS_PATH}?per_page=100&page=1",
        f"{CHECK_RUNS_PATH}?per_page=100&page=1",
        f"{STATUSES_PATH}?per_page=100&page=1",
    ]


# -- credential / auth refusals ------------------------------------------------


def test_refuses_when_gateway_credentials_are_not_configured(monkeypatch):
    monkeypatch.delenv("VOYN_MERGE_GATEWAY_APP_ID", raising=False)
    monkeypatch.delenv("VOYN_MERGE_GATEWAY_INSTALLATION_ID", raising=False)
    monkeypatch.delenv("VOYN_MERGE_GATEWAY_PRIVATE_KEY_PATH", raising=False)
    result, transport_calls, runner_calls = _run(_ok_responses(), creds=None)
    _assert_refused(result, runner_calls, reason_prefix="gateway_credentials_not_configured")
    assert transport_calls == []


def test_refuses_when_installation_token_mint_fails(monkeypatch):
    def boom(creds):
        raise github_app_auth.AppAuthError("token exchange failed")

    monkeypatch.setattr(merge_gateway.github_app_auth, "installation_token", boom)
    result, transport_calls, runner_calls = _run(_ok_responses())
    _assert_refused(result, runner_calls, reason_prefix="gateway_auth_failed")
    assert transport_calls == []


# -- input validation -----------------------------------------------------------


@pytest.mark.parametrize("bad_sha", ["", "not-a-sha", "a" * 39, "g" * 40, None])
def test_refuses_on_a_malformed_expected_head_sha(monkeypatch, bad_sha):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    result, transport_calls, runner_calls = _run(_ok_responses(), expected_head_sha=bad_sha)
    _assert_refused(result, runner_calls, reason_prefix="invalid_expected_head_sha")
    assert transport_calls == []


def test_refuses_on_an_unparseable_pr_url(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    transport, transport_calls = _fake_transport(_ok_responses())
    runner, runner_calls = _fake_runner()
    result = merge_gateway.evaluate_and_merge(
        "https://example.com/not/a/pr", HEAD, creds=CREDS, request=transport, runner=runner,
    )
    _assert_refused(result, runner_calls, reason_prefix="invalid_pr_url")
    assert transport_calls == []


# -- pull-request state refusals ------------------------------------------------


def test_refuses_when_the_pull_request_is_closed(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(pull=_pull(state="closed"))
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="pr_not_open")


def test_refuses_on_head_sha_mismatch(monkeypatch):
    """A PR that moved between the caller's readiness check and this call
    must never be merged at whatever its new head happens to be."""
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(pull=_pull(head=OTHER_HEAD))
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="head_sha_mismatch")


def test_refuses_when_the_pull_request_response_is_malformed(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(pull=["not", "an", "object"])
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="pull_request_response_malformed")


def test_refuses_when_the_author_is_unresolvable(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(pull={"state": "open", "head": {"sha": HEAD}, "user": None})
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="pr_author_unresolvable")


# -- acceptance / independence refusals -----------------------------------------


def test_refuses_when_there_is_no_verdict_at_all(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(reviews=[])
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="acceptance_refused")


def test_refuses_a_self_issued_accept(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(reviews=[_accept_review(login=AUTHOR)])
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="acceptance_refused")


def test_refuses_a_dismissed_accept(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(reviews=[_accept_review(state="DISMISSED")])
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="acceptance_refused")


def test_refuses_a_stale_verdict_for_a_different_head(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(reviews=[_accept_review(sha=OTHER_HEAD)])
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="acceptance_refused")


def test_refuses_when_a_reject_stands_on_the_head_even_with_an_accept_present(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(reviews=[_accept_review(), _reject_review()])
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="acceptance_refused")


def test_refuses_when_reviews_response_is_not_a_list(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses()
    responses[("GET", f"{REVIEWS_PATH}?per_page=100&page=1")] = {"not": "a list"}
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="reviews_response_malformed")


# -- the pagination fix (PR #523 rejection, chunk 1) -----------------------------


def _paged(n_pages, page_factory):
    return {i: page_factory(i) for i in range(1, n_pages + 1)}


def test_a_reject_on_a_later_review_page_still_blocks_the_merge(monkeypatch):
    """The exact scenario the PR #523 rejection named: `gh pr view --json
    reviews` resolves through a single bounded GraphQL page, so a REJECT
    sitting past that page could be invisible while an ACCEPT on the first
    page authorized a merge. This plants the ACCEPT on page 1 (100 filler
    reviews for a different, irrelevant head, so the page is genuinely
    full) and the live REJECT as the single review on page 2, and asserts
    the gateway still refuses -- proving it paginated to the end before
    evaluating anything."""
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    filler = [_accept_review(sha=OTHER_HEAD, login=f"filler-{i}") for i in range(99)]
    page_1 = [_accept_review()] + filler
    assert len(page_1) == 100
    page_2 = [_reject_review()]
    responses = _ok_responses()
    responses[("GET", f"{REVIEWS_PATH}?per_page=100&page=1")] = page_1
    responses[("GET", f"{REVIEWS_PATH}?per_page=100&page=2")] = page_2
    result, transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="acceptance_refused")
    paths = [path for _m, path, _t, _b in transport_calls]
    assert f"{REVIEWS_PATH}?per_page=100&page=2" in paths


def test_an_accept_on_a_later_review_page_is_still_found(monkeypatch):
    """The positive half of the same fix: a genuine ACCEPT that only shows
    up once pagination continues past a full first page must still
    authorize the merge -- pagination is not merely a source of NEW
    refusals, it must also see real acceptances it would otherwise miss."""
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    filler = [_accept_review(sha=OTHER_HEAD, login=f"filler-{i}") for i in range(100)]
    responses = _ok_responses()
    responses[("GET", f"{REVIEWS_PATH}?per_page=100&page=1")] = filler
    responses[("GET", f"{REVIEWS_PATH}?per_page=100&page=2")] = [_accept_review()]
    result, _transport_calls, runner_calls = _run(responses)
    assert result.attempted is True
    assert len(runner_calls) == 1


def test_refuses_when_review_pagination_does_not_terminate(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses()
    full_page = [_accept_review(sha=OTHER_HEAD, login=f"filler-{i}") for i in range(100)]
    for method, path in list(responses):
        if path.startswith(f"{REVIEWS_PATH}?"):
            del responses[method, path]
    for page, body in _paged(merge_gateway._MAX_REVIEW_PAGES, lambda i: full_page).items():
        responses[("GET", f"{REVIEWS_PATH}?per_page=100&page={page}")] = body
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="reviews_pagination_did_not_terminate")


# -- required-checks refusals -----------------------------------------------------


def test_refuses_when_a_check_run_is_still_pending(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    pending = {
        "total_count": 1,
        "check_runs": [
            {"name": "CI", "conclusion": None, "status": "in_progress",
             "started_at": "2026-01-01T00:00:00Z", "completed_at": None}
        ],
    }
    responses = _ok_responses(checks=pending)
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="checks_not_green")


def test_refuses_when_a_check_run_failed(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(checks=_check_runs(conclusion="failure"))
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="checks_not_green")


def test_refuses_when_a_legacy_status_context_is_pending(monkeypatch):
    """Legacy Status API contexts report lowercase `state`, unlike check-runs'
    lowercase `conclusion` mapped to the rollup's uppercase contract -- both
    must normalize the same way `_pr_is_mergeable` already requires."""
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(
        checks={"total_count": 0, "check_runs": []},
        statuses=[{"context": "legacy-ci", "state": "pending",
                   "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}],
    )
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="checks_not_green")


def test_a_green_legacy_status_context_authorizes_merge(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(
        checks={"total_count": 0, "check_runs": []},
        statuses=[{"context": "legacy-ci", "state": "success",
                   "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}],
    )
    result, _transport_calls, runner_calls = _run(responses)
    assert result.attempted is True
    assert len(runner_calls) == 1


def test_refuses_when_no_checks_are_reported_at_all(monkeypatch):
    """An EMPTY check set is inconclusive, not green -- the same rule
    `review_merge._merged_target_sha` already applies."""
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses(checks={"total_count": 0, "check_runs": []}, statuses=[])
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="no_required_checks_reported")


def test_refuses_when_check_runs_pagination_does_not_terminate(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses()
    del responses[("GET", f"{CHECK_RUNS_PATH}?per_page=100&page=1")]
    full_page = {
        "total_count": merge_gateway._PER_PAGE * merge_gateway._MAX_CHECK_PAGES + 1,
        "check_runs": [
            {"name": f"CI-{i}", "conclusion": "success", "status": "completed",
             "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:05:00Z"}
            for i in range(merge_gateway._PER_PAGE)
        ],
    }
    for page in range(1, merge_gateway._MAX_CHECK_PAGES + 1):
        responses[("GET", f"{CHECK_RUNS_PATH}?per_page=100&page={page}")] = full_page
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="check_runs_pagination_did_not_terminate")


def test_refuses_when_statuses_response_is_not_a_list(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )
    responses = _ok_responses()
    responses[("GET", f"{STATUSES_PATH}?per_page=100&page=1")] = {"not": "a list"}
    result, _transport_calls, runner_calls = _run(responses)
    _assert_refused(result, runner_calls, reason_prefix="statuses_response_malformed")


# -- transport failures fail closed ----------------------------------------------


def test_refuses_when_a_read_raises_a_transport_error(monkeypatch):
    monkeypatch.setattr(
        merge_gateway.github_app_auth, "installation_token", lambda creds: "gw-token"
    )

    def failing_transport(method, path, token, *, body=None):
        raise merge_gateway.GatewayError(f"api_unreachable:{method}:{path}")

    runner, runner_calls = _fake_runner()
    result = merge_gateway.evaluate_and_merge(
        PR_URL, HEAD, creds=CREDS, request=failing_transport, runner=runner,
    )
    _assert_refused(result, runner_calls, reason_prefix="api_unreachable")
