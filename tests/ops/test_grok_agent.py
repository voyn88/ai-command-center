import importlib.util
import io
import json
from pathlib import Path

import pytest


def driver():
    spec = importlib.util.spec_from_file_location('grok_agent', Path(__file__).parents[2] / 'ops/aicc_grok_agent.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _isolate(module, monkeypatch):
    monkeypatch.setattr(module.os, 'getcwd', lambda: '/workspace')
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1234, raising=False)
    monkeypatch.setenv('XAI_API_KEY', 'private-test-value')


def test_driver_refuses_tools_outside_broker(monkeypatch):
    module = driver()
    monkeypatch.setattr(module.os, 'getcwd', lambda: '/outside')
    with pytest.raises(RuntimeError, match='isolated'):
        module.run('task', 'grok-4.3')


def test_model_only_http_request_disables_tools(monkeypatch):
    module = driver()
    def open_request(request, timeout):
        assert request.full_url == "https://api.x.ai/v1/responses"
        body = json.loads(request.data)
        assert body['tools'] == []
        assert 'tool_choice' not in body  # xAI rejects this field with an empty tools list.
        assert body['store'] is False
        assert 'reasoning' not in body  # grok-4.3 keeps provider defaults
        return io.StringIO('{"status":"completed","output":[]}')
    monkeypatch.setattr(module.urllib.request, 'urlopen', open_request)
    module.completion('test-secret', 'grok-4.3', [], tools=False)


def test_grok_4_6_request_sets_reasoning_effort_low(monkeypatch):
    """Economy/latency default for grok-4.6 only; official docs default is high."""
    module = driver()
    seen = []

    def open_request(request, timeout):
        body = json.loads(request.data)
        seen.append(body)
        assert body['model'] == 'grok-4.6'
        assert body['reasoning'] == {'effort': 'low'}
        assert body['max_output_tokens'] == module.MAX_OUTPUT_TOKENS
        assert body['store'] is False
        return io.StringIO(
            '{"status":"completed","usage":{"total_tokens":1},'
            '"output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}]}'
        )

    monkeypatch.setattr(module.urllib.request, 'urlopen', open_request)
    module.completion('test-secret', 'grok-4.6', [{'role': 'user', 'content': 'x'}], tools=False)
    # Other models must not inherit the grok-4.6 override.
    def open_other(request, timeout):
        body = json.loads(request.data)
        assert body['model'] == 'grok-4.3'
        assert 'reasoning' not in body
        return io.StringIO('{"status":"completed","output":[]}')
    monkeypatch.setattr(module.urllib.request, 'urlopen', open_other)
    module.completion('test-secret', 'grok-4.3', [], tools=True)
    assert len(seen) == 1


def test_driver_bounds_tool_loop(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    count = []
    def completion(*args, **kwargs):
        count.append(1)
        return {'status': 'completed', 'usage': {'total_tokens': 100}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': f'call-{len(count)}',
             'arguments': json.dumps({'command': f'echo turn-{len(count)}'})}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: 'ok')
    code = module.run('task', 'grok-4.3')
    assert code == 1
    # Tool loop is capped at MAX_TURNS; no optional extra summary call.
    assert len(count) == module.MAX_TURNS
    captured = capsys.readouterr()
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'incomplete_budget_or_guard_stop' in captured.out
    assert 'AICC_LOCAL_PARTIAL_CHECKPOINT' not in captured.out
    assert 'AICC_STOP: reason=turn_budget' in captured.err
    assert 'AICC_USAGE:' in captured.err
    assert 'summary_reserve_tokens=' not in captured.err


def test_driver_refuses_incomplete_result(monkeypatch):
    module = driver()
    _isolate(module, monkeypatch)
    monkeypatch.setattr(module, 'completion', lambda *a: {'status': 'incomplete', 'output': [{'type': 'message'}]})
    with pytest.raises(RuntimeError, match='incomplete'):
        module.run('task', 'grok-4.3')


def test_model_only_review_never_executes_returned_tool(monkeypatch):
    module = driver()
    _isolate(module, monkeypatch)
    def completion(key, model, items, tools):
        assert tools is False
        return {'status': 'completed', 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'x', 'arguments': '{}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('model-only review invoked shell'))
    with pytest.raises(RuntimeError, match='tools are disabled'):
        module.run('review', 'grok-4.3', tools=False)


def test_oversized_tool_batch_executes_allowance_and_refuses_rest(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    executed = []
    responses = [
        {'status': 'completed', 'usage': {'total_tokens': 50}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': f'c{i}',
             'arguments': json.dumps({'command': f'echo {i}'})} for i in range(6)
        ]},
        {'status': 'completed', 'usage': {'total_tokens': 20}, 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'done HEAD_SHA=abc'}]}]},
    ]
    def completion(key, model, items, tools=True):
        assert tools is True
        # Prior turn must have protocol outputs for every call_id, including refusals.
        if len(responses) == 1:
            outputs = [item for item in items if item.get('type') == 'function_call_output']
            assert len(outputs) == 6
            refused = [json.loads(item['output']) for item in outputs[module.MAX_TOOLS_PER_TURN:]]
            assert all(entry.get('error') == 'tool_budget_refused' for entry in refused)
            assert all(entry.get('reason') == 'max_tools_per_turn' for entry in refused)
        return responses.pop(0)
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda args: executed.append(args) or '{"exit_code":0,"output":"ok"}')
    code = module.run('task', 'grok-4.3')
    assert code == 0
    assert len(executed) == module.MAX_TOOLS_PER_TURN
    captured = capsys.readouterr()
    assert 'done HEAD_SHA=abc' in captured.out
    assert 'AICC_STOP: reason=completed' in captured.err
    assert 'tools_refused=2' in captured.err


def test_token_budget_emits_partial_not_success(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    calls = {'n': 0}
    def completion(key, model, items, tools=True):
        calls['n'] += 1
        return {'status': 'completed', 'usage': {'total_tokens': module.MAX_TOTAL_TOKENS}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
             'arguments': '{"command":"echo should-not-run"}'},
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c2',
             'arguments': '{"command":"echo also-refused"}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('tools must not run after token ceiling'))
    code = module.run('task', 'grok-4.3')
    assert code == 1
    assert calls['n'] == 1  # hard cap: local partial only, no extra model summary
    captured = capsys.readouterr()
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_LOCAL_PARTIAL_CHECKPOINT' not in captured.out
    assert 'AICC_STOP: reason=token_budget' in captured.err
    assert 'tools_refused=2' in captured.err
    assert 'usage_unknown=false' in captured.err
    assert 'ACCEPT' not in captured.out
    assert code != 0


def test_missing_usage_is_not_treated_as_zero(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    def completion(key, model, items, tools=True):
        return {'status': 'completed', 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
             'arguments': '{"command":"true"}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('must not run tools when usage unknown'))
    code = module.run('task', 'grok-4.3')
    assert code == 1
    captured = capsys.readouterr()
    assert 'AICC_STOP: reason=usage_unknown' in captured.err
    assert 'usage_tokens=unknown' in captured.err
    assert 'usage_unknown=true' in captured.err
    assert 'tools_refused=1' in captured.err
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert code != 0


def test_repeated_identical_calls_stop_with_no_progress(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    executed = []
    def completion(key, model, items, tools=True):
        return {'status': 'completed', 'usage': {'total_tokens': 20}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': f'c{len(executed)}',
             'arguments': '{"command":"echo same"}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda args: executed.append(args) or '{"exit_code":0,"output":"same"}')
    code = module.run('task', 'grok-4.3')
    assert code == 1
    assert len(executed) == module.MAX_IDENTICAL_TOOL_REPEATS
    captured = capsys.readouterr()
    assert 'AICC_STOP: reason=no_progress' in captured.err
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_LOCAL_PARTIAL_CHECKPOINT' not in captured.out


def test_hard_token_cap_skips_tools_and_keeps_local_partial(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    def completion(key, model, items, tools=True):
        assert tools is True
        return {'status': 'completed', 'usage': {'total_tokens': module.MAX_TOTAL_TOKENS}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
             'arguments': '{"command":"echo x"}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('hard cap must not execute tools'))
    code = module.run('task', 'grok-4.3')
    assert code == 1
    captured = capsys.readouterr()
    assert 'AICC_STOP: reason=token_budget' in captured.err
    assert 'tools_refused=1' in captured.err
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_LOCAL_PARTIAL_CHECKPOINT' not in captured.out


def test_budget_stop_stderr_is_not_exact_provider_http_line(monkeypatch, capsys):
    """Worker only treats exact `provider HTTP NNN` stderr as grok provider failover."""
    module = driver()
    _isolate(module, monkeypatch)
    def completion(key, model, items, tools=True):
        return {'status': 'completed', 'usage': {'total_tokens': module.MAX_TOTAL_TOKENS}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
             'arguments': '{"command":"true"}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('no tools'))
    code = module.run('task', 'grok-4.3')
    assert code == 1
    err = capsys.readouterr().err.strip()
    assert err != 'provider HTTP 429'
    assert err != 'provider HTTP 500'
    assert 'AICC_STOP: reason=token_budget' in err
    # Unchanged exhausted tasks must not look like provider quota for automatic retry/failover.
    assert err.splitlines()[0].startswith('AICC_STOP:')


def test_token_budget_refuses_pending_calls_before_partial(monkeypatch, capsys):
    """Hard ceiling must append function_call_output refusals before durable partial."""
    module = driver()
    _isolate(module, monkeypatch)
    seen_refusals = []
    def completion(key, model, items, tools=True):
        return {'status': 'completed', 'usage': {'total_tokens': module.MAX_TOTAL_TOKENS}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'pending-a',
             'arguments': '{"command":"echo a"}'},
            {'type': 'function_call', 'name': 'shell', 'call_id': 'pending-b',
             'arguments': '{"command":"echo b"}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('budget must refuse, not run'))
    original = module._append_call_refusals
    def spy(items, calls, key, *, reason, limit):
        count = original(items, calls, key, reason=reason, limit=limit)
        seen_refusals.extend(
            json.loads(item['output'])
            for item in items
            if item.get('type') == 'function_call_output'
        )
        return count
    monkeypatch.setattr(module, '_append_call_refusals', spy)
    code = module.run('task', 'grok-4.3')
    assert code == 1
    assert len(seen_refusals) == 2
    assert all(entry.get('error') == 'tool_budget_refused' for entry in seen_refusals)
    assert all(entry.get('reason') == 'token_budget' for entry in seen_refusals)
    captured = capsys.readouterr()
    assert 'tools_refused=2' in captured.err
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert code != 0


def test_provider_oserror_after_tools_emits_partial(monkeypatch, capsys):
    """Network OSError after tool progress must emit honest partial, exit 1, no retry."""
    module = driver()
    _isolate(module, monkeypatch)
    calls = {'n': 0}
    def completion(key, model, items, tools=True):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'status': 'completed', 'usage': {'total_tokens': 40}, 'output': [
                {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
                 'arguments': '{"command":"echo progressed"}'}]}
        raise OSError('network down')
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: '{"exit_code":0,"output":"ok"}')
    code = module.run('task', 'grok-4.3')
    assert code == 1
    assert calls['n'] == 2  # no unbounded retry
    captured = capsys.readouterr()
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_STOP: reason=run_error' in captured.err
    assert 'failure_class=OSError' in captured.err
    assert 'tools_executed=1' in captured.err
    assert 'echo progressed' in captured.out
    assert code != 0


def test_incomplete_after_progress_emits_partial_not_success(monkeypatch, capsys):
    """Incomplete provider response with prior tool progress → honest partial, never success."""
    module = driver()
    _isolate(module, monkeypatch)
    calls = {'n': 0}
    def completion(key, model, items, tools=True):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'status': 'completed', 'usage': {'total_tokens': 30}, 'output': [
                {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
                 'arguments': '{"command":"echo prior"}'}]}
        return {'status': 'incomplete', 'usage': {'total_tokens': 5},
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'x'}]}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: '{"exit_code":0,"output":"ok"}')
    code = module.run('task', 'grok-4.3')
    assert code == 1
    captured = capsys.readouterr()
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_STOP: reason=provider_incomplete' in captured.err
    assert 'failure_class=incomplete_status' in captured.err
    assert 'ACCEPT' not in captured.out
    assert 'AICC_STOP: reason=completed' not in captured.err


def test_shell_timeout_after_progress_emits_partial(monkeypatch, capsys):
    """Shell timeout mid-batch after earlier progress preserves local partial."""
    module = driver()
    _isolate(module, monkeypatch)
    def completion(key, model, items, tools=True):
        return {'status': 'completed', 'usage': {'total_tokens': 25}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
             'arguments': '{"command":"echo one"}'},
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c2',
             'arguments': '{"command":"echo two"}'}]}
    executed = []
    def shell(arguments):
        executed.append(arguments)
        if len(executed) == 1:
            return '{"exit_code":0,"output":"one"}'
        raise RuntimeError('shell timed out; stop this run')
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', shell)
    code = module.run('task', 'grok-4.3')
    assert code == 1
    assert len(executed) == 2
    captured = capsys.readouterr()
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_STOP: reason=run_error' in captured.err
    assert 'failure_class=RuntimeError' in captured.err
    assert 'echo one' in captured.out


def test_no_summary_reserve_constants():
    module = driver()
    assert not hasattr(module, 'SUMMARY_RESERVE_TOKENS')
    assert not hasattr(module, 'SUMMARY_CHECKPOINT_CHARS')
    assert not hasattr(module, '_try_final_summary')
    assert module.MAX_TURNS == 12
    assert module.MAX_TOTAL_TOKENS == 60000
    assert module.MAX_TOOLS_PER_TURN == 4
    assert module.MAX_OUTPUT_TOKENS == 4096
