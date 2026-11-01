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
        return io.StringIO('{"status":"completed","output":[]}')
    monkeypatch.setattr(module.urllib.request, 'urlopen', open_request)
    module.completion('test-secret', 'grok-4.3', [], tools=False)


def test_driver_bounds_tool_loop(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    count = []
    def completion(*args, **kwargs):
        count.append(1)
        tools = True if len(args) < 4 else args[3]
        if kwargs.get('tools') is False or tools is False:
            return {'status': 'completed', 'usage': {'total_tokens': 10}, 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'summary attempt'}]}]}
        # Distinct commands so turn-budget is exercised, not identical-call guard.
        return {'status': 'completed', 'usage': {'total_tokens': 100}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': f'call-{len(count)}',
             'arguments': json.dumps({'command': f'echo turn-{len(count)}'})}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: 'ok')
    code = module.run('task', 'grok-4.3')
    assert code == 1
    # Tool loop is capped at MAX_TURNS; optional single summary may add one call.
    assert module.MAX_TURNS <= len(count) <= module.MAX_TURNS + 1
    captured = capsys.readouterr()
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'incomplete_budget_or_guard_stop' in captured.out
    assert 'AICC_STOP: reason=turn_budget' in captured.err
    assert 'AICC_USAGE:' in captured.err


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
    # First response already consumes the soft tool ceiling; tools must not run.
    soft = module.MAX_TOTAL_TOKENS - module.SUMMARY_RESERVE_TOKENS
    calls = {'n': 0}
    def completion(key, model, items, tools=True):
        calls['n'] += 1
        if tools:
            return {'status': 'completed', 'usage': {'total_tokens': soft}, 'output': [
                {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
                 'arguments': '{"command":"echo should-not-run"}'}]}
        return {'status': 'completed', 'usage': {'total_tokens': 10}, 'output': [
            {'type': 'message', 'content': [{'type': 'output_text',
             'text': 'model partial: stopped on budget'}]}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('tools must not run after token ceiling'))
    code = module.run('task', 'grok-4.3')
    assert code == 1
    assert calls['n'] == 2  # one tool-bearing turn + one reserved summary
    captured = capsys.readouterr()
    assert 'model partial: stopped on budget' in captured.out
    assert 'AICC_LOCAL_PARTIAL_CHECKPOINT' in captured.out
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_STOP: reason=token_budget' in captured.err
    assert 'usage_unknown=false' in captured.err


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
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert code != 0


def test_repeated_identical_calls_stop_with_no_progress(monkeypatch, capsys):
    module = driver()
    _isolate(module, monkeypatch)
    executed = []
    def completion(key, model, items, tools=True):
        if tools is False:
            return {'status': 'completed', 'usage': {'total_tokens': 5}, 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'no progress summary'}]}]}
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


def test_hard_token_cap_skips_summary_and_keeps_local_partial(monkeypatch, capsys):
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
    assert 'AICC_PARTIAL_RESULT' in captured.out
    assert 'AICC_LOCAL_PARTIAL_CHECKPOINT' not in captured.out  # no model summary
    assert 'model partial' not in captured.out


def test_budget_stop_stderr_is_not_exact_provider_http_line(monkeypatch, capsys):
    """Worker only treats exact `provider HTTP NNN` stderr as grok provider failover."""
    module = driver()
    _isolate(module, monkeypatch)
    soft = module.MAX_TOTAL_TOKENS - module.SUMMARY_RESERVE_TOKENS
    def completion(key, model, items, tools=True):
        if tools:
            return {'status': 'completed', 'usage': {'total_tokens': soft}, 'output': [
                {'type': 'function_call', 'name': 'shell', 'call_id': 'c1',
                 'arguments': '{"command":"true"}'}]}
        raise RuntimeError('provider HTTP 500')  # summary failure must not become the process stderr
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
