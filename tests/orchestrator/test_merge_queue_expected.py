"""Real queue membership and bounded recovery of skipped-suite shadowing."""
import json
import subprocess

import pytest

from command_center.orchestrator import review_merge as r

HEAD = 'a' * 40
URL = 'https://github.com/owner/repo/pull/12'


def queue_view(monkeypatch, *, queued=False, head=HEAD, state='OPEN'):
    payload = {'data': {'repository': {'pullRequest': {
        'id': 'PR_node', 'state': state, 'headRefOid': head, 'isInMergeQueue': queued,
    }}}}
    monkeypatch.setattr(r, '_gh', lambda *args: subprocess.CompletedProcess([], 0, json.dumps(payload), ''))


def test_queue_membership_is_proven_without_reenqueue(monkeypatch):
    queue_view(monkeypatch, queued=True)
    monkeypatch.setattr(r.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected enqueue'))
    assert r._ensure_merge_queue_entry('/repo', URL, HEAD, 'merge-only') == (True, 'merge_queued_awaiting_target')


@pytest.mark.parametrize('changed', ['head', 'state'])
def test_queue_enqueue_refuses_changed_head_or_closed_pr(monkeypatch, changed):
    queue_view(monkeypatch, head='b'*40 if changed=='head' else HEAD,
               state='CLOSED' if changed=='state' else 'OPEN')
    monkeypatch.setattr(r.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected enqueue'))
    assert r._ensure_merge_queue_entry('/repo', URL, HEAD, 'merge-only')[0] is False


@pytest.mark.parametrize('message,expected', [
    ('Required status check "Final merge gate" is expected', 'required_check_expected:'),
    ('Pull request 2 of 2 required status checks are expected.', 'required_check_expected:'),
    ('Pull request is not mergeable', 'enqueue_refused:'),
])
def test_enqueue_is_exact_head_merge_identity_and_classifies_all_errors(monkeypatch, message, expected):
    queue_view(monkeypatch)
    def run(argv, **kwargs):
        assert 'expectedHeadOid:$head' in ' '.join(argv)
        assert f'head={HEAD}' in argv
        assert kwargs['env']['GH_TOKEN'] == 'merge-only'
        return subprocess.CompletedProcess(argv, 1, json.dumps({'errors': [
            {'message': 'other detail'}, {'message': message}]}), '')
    monkeypatch.setattr(r.subprocess, 'run', run)
    ok, reason = r._ensure_merge_queue_entry('/repo', URL, HEAD, 'merge-only')
    assert not ok and reason.startswith(expected)


@pytest.mark.parametrize('payload,expected', [
    ({'data': {'enqueuePullRequest': {'mergeQueueEntry': {'state': 'QUEUED'}}}}, True),
    ({'data': {'enqueuePullRequest': None}}, False),
    ({}, False),
])
def test_enqueue_success_requires_a_returned_queue_entry(monkeypatch, payload, expected):
    queue_view(monkeypatch)
    monkeypatch.setattr(r.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps(payload), ''))
    assert r._ensure_merge_queue_entry('/repo', URL, HEAD, 'merge-only')[0] is expected


def test_retrigger_budget_and_reserved_head_survive_repeated_ticks(monkeypatch):
    claims, writes = set(), []
    def claim(factory, task, url, head):
        key = (url, head)
        if key in claims:
            return False
        claims.add(key)
        return True
    monkeypatch.setattr(r, '_claim_gate_retrigger', claim)
    monkeypatch.setattr(r, '_retrigger_pr_gates', lambda *a: writes.append(a) or False)
    cfg = r.ReviewConfig(max_gate_retriggers_per_tick=1)
    budget = [1]
    def call(head, tick):
        return r._recover_expected_gate(None, '/repo', 'TASK', URL, head, cfg, tick, 'required_check_expected: x')
    assert call(HEAD, budget).endswith('gate_retrigger_failed')
    assert call('b'*40, budget).endswith('gate_retrigger_tick_capped')
    assert call(HEAD, [1]).endswith('gate_retrigger_already_attempted')
    assert len(writes) == 1
    assert cfg.gate_retrigger_label == r.DEFAULT_PR_WINDOW_CONFIG.label_active


@pytest.mark.parametrize('changed,delete_fails', [(False,False),(True,False),(False,True)])
def test_retrigger_checks_head_and_removal_before_add(monkeypatch, changed, delete_fails):
    writes=[]
    def gh(argv, repo):
        if argv[1] != '--method':
            data={'state':'open','head':{'sha':'b'*40 if changed else HEAD},
                  'labels':[{'name':'review-window:active'}]}
            return subprocess.CompletedProcess(argv,0,json.dumps(data),'')
        writes.append(argv[2])
        return subprocess.CompletedProcess(argv,1 if delete_fails else 0,'','')
    monkeypatch.setattr(r,'_gh',gh)
    assert r._retrigger_pr_gates('/repo',URL,'review-window:active',HEAD) is (not changed and not delete_fails)
    assert writes == ([] if changed else ['DELETE'] if delete_fails else ['DELETE','POST'])
