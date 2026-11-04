"""Pinned branch-update requests and confirmed-only evidence carry-over."""
import json
import subprocess

import pytest

from command_center.orchestrator import review_merge as r

PR = "https://github.com/owner/repo/pull/41"
OLD, NEW, BASE = "a" * 40, "b" * 40, "c" * 40
ENDPOINT = "repos/owner/repo/pulls/41"


def pull(head=OLD, **fields):
    return {"state": "open", "head": {"sha": head}, "base": {"sha": BASE}, **fields}


def install_transport(monkeypatch, *, before=None, after=None, status=202, rc=0, commit=None):
    snapshots = iter([pull() if before is None else before,
                      pull(NEW) if after is None else after])
    calls = []
    def gh(argv, repo):
        calls.append(argv)
        if argv == ["api", ENDPOINT]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(next(snapshots)), "")
        if argv[:3] == ["api", "--method", "PUT"]:
            return subprocess.CompletedProcess(argv, rc, f"HTTP/2.0 {status} Accepted\r\n\r\n{{}}", "refused" if rc else "")
        if argv == ["api", f"repos/owner/repo/commits/{NEW}"]:
            value = commit if commit is not None else {"sha": NEW, "parents": [{"sha": OLD}, {"sha": BASE}]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        raise AssertionError(f"Unexpected command {argv}")
    monkeypatch.setattr(r, "_gh", gh)
    return calls


def test_pinned_rest_update_confirms_exact_two_parent_merge(monkeypatch):
    calls = install_transport(monkeypatch)
    assert r._update_behind_branch("/repo", PR, OLD) == (NEW, "branch_updated_behind_main")
    assert calls[1] == ["api", "--method", "PUT", ENDPOINT + "/update-branch",
                        "-f", f"expected_head_sha={OLD}", "--include"]
    assert len(calls) == 4


@pytest.mark.parametrize("before", [pull(NEW), pull(state="closed"), pull(base={}), [], "broken"])
def test_changed_or_invalid_snapshot_cannot_issue_update(monkeypatch, before):
    calls = install_transport(monkeypatch, before=before)
    head, reason = r._update_behind_branch("/repo", PR, OLD)
    assert head is None and reason.startswith("branch_update_failed:")
    assert len(calls) == 1


@pytest.mark.parametrize("status,rc", [(403, 1), (422, 1), (500, 1), (202, 124), (200, 0)])
def test_refused_or_ambiguous_response_cannot_confirm_update(monkeypatch, status, rc):
    calls = install_transport(monkeypatch, status=status, rc=rc)
    head, reason = r._update_behind_branch("/repo", PR, OLD)
    assert head is None and reason.startswith("branch_update_failed:")
    assert len(calls) == 2


def test_accepted_but_unchanged_head_is_pending_not_completed(monkeypatch):
    calls = install_transport(monkeypatch, after=pull())
    assert r._update_behind_branch("/repo", PR, OLD) == (None, "branch_update_pending")
    assert len(calls) == 3


@pytest.mark.parametrize("after", [pull(NEW, state="closed"), pull("invalid"), [], "broken"])
def test_closed_or_invalid_confirmation_never_grants_carryover(monkeypatch, after):
    calls = install_transport(monkeypatch, after=after)
    assert r._update_behind_branch("/repo", PR, OLD)[0] is None
    assert len(calls) == 3


@pytest.mark.parametrize("commit", [
    {"sha": NEW, "parents": [{"sha": OLD}, {"sha": "d" * 40}]},
    {"sha": NEW, "parents": [{"sha": OLD}]},
    {"sha": NEW, "parents": [{"sha": []}, {"sha": BASE}]},
    {"sha": OLD, "parents": [{"sha": OLD}, {"sha": BASE}]},
    "broken",
])
def test_unrelated_or_malformed_commit_does_not_confirm_update(monkeypatch, commit):
    install_transport(monkeypatch, commit=commit)
    assert r._update_behind_branch("/repo", PR, OLD) == (None, "branch_update_failed: update_not_confirmed")


def prepare_merge_tick(monkeypatch, count=1, ready=True, state="BEHIND"):
    tasks = [(f"VOYN-W0-UPDATE-{n}", PR.replace("41", str(41+n))) for n in range(count)]
    monkeypatch.setattr(r, "_scan_tasks", lambda *a: (tasks, "token"))
    monkeypatch.setattr(r, "_scan_commit", lambda *a: None)
    monkeypatch.setattr(r, "_merge_window_open_pr_snapshot", lambda *a: (None, None))
    monkeypatch.setattr(r, "_pr_window_expensive_read_decision", lambda *a, **kw:
                        r._PrWindowReadDecision(True, "active", {"state": "open"}))
    monkeypatch.setattr(r, "_pr_is_mergeable", lambda *a: (ready, OLD if ready else "no_accept_marker_on_head"))
    monkeypatch.setattr(r, "_merge_state", lambda *a: state)
    return tasks


@pytest.mark.parametrize("updated_head,reason", [(None, "branch_update_pending"),
    (None, "branch_update_failed: request_refused"), (NEW, "branch_updated_behind_main")])
def test_merge_tick_carries_marker_only_after_confirmed_update(monkeypatch, updated_head, reason):
    prepare_merge_tick(monkeypatch)
    monkeypatch.setattr(r, "_update_behind_branch", lambda *a: (updated_head, reason))
    carried = []
    monkeypatch.setattr(r, "_carry_over_marker_if_patch_id_stable",
                        lambda *a, **kw: carried.append(kw["expected_head"]) or True)
    report = r._merge_once(lambda: None, "/repo")
    assert carried == ([NEW] if updated_head else [])
    assert not report.merged
    assert report.skipped[0][1] == ("branch_updated_marker_carried_over" if updated_head else reason)


def test_failed_updates_still_consume_existing_per_tick_cap(monkeypatch):
    prepare_merge_tick(monkeypatch, count=4)
    updates = []
    monkeypatch.setattr(r, "_update_behind_branch", lambda *a:
                        updates.append(a) or (None, "branch_update_failed: request_refused"))
    report = r._merge_once(lambda: None, "/repo", r.ReviewConfig(max_branch_updates_per_tick=2))
    assert len(updates) == 2 and all(args[2] == OLD for args in updates)
    assert sum(reason == "branch_behind_update_capped" for _, reason in report.skipped) == 2


@pytest.mark.parametrize("ready,state", [(False, "BEHIND"), (True, "DIRTY")])
def test_unaccepted_or_dirty_pr_never_updates(monkeypatch, ready, state):
    prepare_merge_tick(monkeypatch, ready=ready, state=state)
    def forbidden(*args):
        raise AssertionError("unsafe update")
    monkeypatch.setattr(r, "_update_behind_branch", forbidden)
    assert not r._merge_once(lambda: None, "/repo").merged


def test_carryover_declines_a_head_changed_after_update_confirmation(monkeypatch):
    monkeypatch.setattr(r, "_gh", lambda *args:
                        subprocess.CompletedProcess([], 0, json.dumps(pull("d" * 40)), ""))
    assert not r._carry_over_marker_if_patch_id_stable("/repo", PR, "VOYN-W0-UPDATE", expected_head=NEW)
