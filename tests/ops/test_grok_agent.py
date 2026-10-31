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


def test_driver_refuses_tools_outside_broker(monkeypatch):
    module = driver()
    monkeypatch.setattr(module.os, 'getcwd', lambda: '/outside')
    with pytest.raises(RuntimeError, match='isolated'):
        module.run('task', 'grok-4.3')


def test_model_only_http_request_disables_tools(monkeypatch):
    module = driver()
    def open_request(request, timeout):
        body = json.loads(request.data)
        assert body['tools'] == []
        assert 'tool_choice' not in body  # xAI rejects this field with an empty tools list.
        assert body['store'] is False
        return io.StringIO('{"status":"completed","output":[]}')
    monkeypatch.setattr(module.urllib.request, 'urlopen', open_request)
    module.completion('test-secret', 'grok-4.3', [], tools=False)


def test_driver_bounds_tool_loop(monkeypatch):
    module = driver()
    monkeypatch.setattr(module.os, 'getcwd', lambda: '/workspace')
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1234, raising=False)
    monkeypatch.setenv('XAI_API_KEY', 'private-test-value')
    count = []
    def completion(*args):
        count.append(1)
        return {'status': 'completed', 'usage': {'total_tokens': 100}, 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'call', 'arguments': '{"command":"true"}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: 'ok')
    with pytest.raises(RuntimeError, match='turn budget'):
        module.run('task', 'grok-4.3')
    assert len(count) == module.MAX_TURNS


def test_driver_refuses_incomplete_result(monkeypatch):
    module = driver()
    monkeypatch.setattr(module.os, 'getcwd', lambda: '/workspace')
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1234, raising=False)
    monkeypatch.setenv('XAI_API_KEY', 'private-test-value')
    monkeypatch.setattr(module, 'completion', lambda *a: {'status': 'incomplete', 'output': [{'type': 'message'}]})
    with pytest.raises(RuntimeError, match='incomplete'):
        module.run('task', 'grok-4.3')


def test_model_only_review_never_executes_returned_tool(monkeypatch):
    module = driver()
    monkeypatch.setattr(module.os, 'getcwd', lambda: '/workspace')
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1234, raising=False)
    monkeypatch.setenv('XAI_API_KEY', 'private-test-value')
    def completion(key, model, items, tools):
        assert tools is False
        return {'status': 'completed', 'output': [
            {'type': 'function_call', 'name': 'shell', 'call_id': 'x', 'arguments': '{}'}]}
    monkeypatch.setattr(module, 'completion', completion)
    monkeypatch.setattr(module, 'shell', lambda _: pytest.fail('model-only review invoked shell'))
    with pytest.raises(RuntimeError, match='tools are disabled'):
        module.run('review', 'grok-4.3', tools=False)
