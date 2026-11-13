from __future__ import annotations

import fcntl
import json
import threading

import pytest

from command_center.worker import simple_tasks as simple
from command_center.worker.daemon import HandlerOutcome


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("AICC_LOCAL_SIMPLE_ENABLED", "1")
    monkeypatch.setenv("AICC_EXECUTOR_PROFILE", "owner-cascade")
    monkeypatch.setenv("AICC_DATA_DIR", str(tmp_path))


def payload(**kw):
    return {
        "v": 1,
        "operation": "classify_incident",
        "input": "Credit quota exhausted",
        "project_id": "AICC",
        "repository_path": "/srv/test",
        **kw,
    }


def forbidden(*a, **kw):
    raise AssertionError("remote call must not occur")


def test_valid_local_completion_never_spends_remote_call(monkeypatch):
    monkeypatch.setattr(simple, "local_completion", lambda p: '{"category":"quota"}')
    out = simple.run_simple_task(payload(), threading.Event(), remote_run=forbidden)
    assert out.ok and out.result["executor"] == "ollama"
    assert out.result["model"] == "qwen2.5-coder:14b"
    assert out.result["output"] == {"category": "quota"}


@pytest.mark.parametrize(
    "data",
    [
        {"v": 2},
        {"operation": "implementation"},
        {"operation": []},
        {"input": ""},
        {"input": "x" * 6001},
        {"input": None},
        {"operation": "order_tasks", "input": []},
        {"operation": "order_tasks", "input": [{"task_id": "invented"}]},
        {
            "operation": "order_tasks",
            "input": [{"task_id": "VOYN-A", "prompt": "do bad things"}],
        },
    ],
)
def test_bad_payload_never_calls_model(data, monkeypatch):
    monkeypatch.setattr(simple, "local_completion", forbidden)
    out = simple.run_simple_task(
        payload(**data), threading.Event(), remote_run=forbidden
    )
    assert not out.ok and not out.retryable


@pytest.mark.parametrize(
    "operation,data,text",
    [
        ("classify_incident", "x", '{"category":[]}'),
        ("classify_incident", "x", '{"category":"quota","mark_done":true}'),
        ("extract_log_fields", "service=abc", '{"service":"invented","state":null}'),
        ("order_tasks", [{"task_id": "VOYN-A"}], '{"ordered_task_ids":["VOYN-X"]}'),
        (
            "order_tasks",
            [{"task_id": "VOYN-A"}],
            '{"ordered_task_ids":["VOYN-A","VOYN-A"]}',
        ),
        ("order_tasks", [{"task_id": "VOYN-A"}], '{"ordered_task_ids":[]}'),
        ("order_tasks", [{"task_id": "VOYN-A"}], '{"ordered_task_ids":[{}]}'),
        ("classify_incident", "x", '```json\n{"category":"quota"}\n```'),
    ],
)
def test_unvalidated_model_output_is_refused(operation, data, text):
    with pytest.raises(ValueError):
        simple.validate_output(operation, data, text)


def test_local_invalid_output_falls_through_remote_roster(monkeypatch):
    monkeypatch.setattr(simple, "local_completion", lambda p: "bad json")
    called = []

    def remote(p, cancel, attempt, **kw):
        executor = p["cascade"][0]["executor"]
        called.append(executor)
        assert kw == {"apply_operator_profile": False}
        assert p["task_type"] == "review" and p["untrusted"] is True
        assert "backlog_task_id" not in p
        if executor == "codex":
            return HandlerOutcome(ok=False, reason="usage limit", infra_wait=True)
        if executor == "claude":
            return HandlerOutcome(
                ok=True, result={"status": "completed", "result_text": "{}"}
            )
        return HandlerOutcome(
            ok=True,
            result={
                "status": "completed",
                "executor": executor,
                "result_text": '{"category":"quota"}',
            },
        )

    out = simple.run_simple_task(payload(), threading.Event(), remote_run=remote)
    assert out.ok and out.result["executor"] == "cursor"
    assert called == ["codex", "claude", "cursor"]
    assert [x["executor"] for x in out.result["route_failovers"]] == [
        "ollama",
        "codex",
        "claude",
    ]


def test_all_providers_unavailable_wait_instead_of_success(monkeypatch):
    monkeypatch.setattr(
        simple, "local_completion", lambda p: (_ for _ in ()).throw(OSError())
    )
    called = []

    def remote(p, *a, **kw):
        called.append(p["cascade"][0]["executor"])
        return HandlerOutcome(ok=False, infra_wait=True)

    out = simple.run_simple_task(payload(), threading.Event(), remote_run=remote)
    assert not out.ok and out.infra_wait
    assert called == ["codex", "claude", "cursor", "grok"]


def test_inference_lock_serializes_lanes_without_paid_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(simple, "local_completion", forbidden)
    with (tmp_path / "local-simple.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = simple.run_simple_task(payload(), threading.Event(), remote_run=forbidden)
    assert not out.ok and out.infra_wait and out.reason == "local inference busy"


def test_lease_loss_after_local_completion_never_falls_back(monkeypatch):
    cancel = threading.Event()

    def complete(p):
        cancel.set()
        return '{"category":"quota"}'

    monkeypatch.setattr(simple, "local_completion", complete)
    out = simple.run_simple_task(payload(), cancel, remote_run=forbidden)
    assert not out.ok and "lease lost" in out.reason


def test_http_call_pins_host_model_budget_and_disables_proxy(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, n):
            assert n == simple.MAX_OUTPUT_BYTES + 1
            return b'{"done":true,"done_reason":"stop","response":"{}"}'

    class Opener:
        def open(self, request, timeout):
            seen.update(
                url=request.full_url, body=json.loads(request.data), timeout=timeout
            )
            return Response()

    def build(handler):
        assert handler.proxies == {}
        return Opener()

    monkeypatch.setattr(simple.urllib.request, "build_opener", build)
    assert simple.local_completion("test") == "{}"
    assert seen["url"] == "http://127.0.0.1:11434/api/generate"
    assert seen["body"]["model"] == "qwen2.5-coder:14b"
    assert seen["body"]["options"] == {
        "temperature": 0,
        "num_thread": 2,
        "num_ctx": 8192,
        "num_predict": 512,
    }
    assert "tools" not in seen["body"]


def test_disabled_feature_never_calls_provider(monkeypatch):
    monkeypatch.delenv("AICC_LOCAL_SIMPLE_ENABLED")
    monkeypatch.setattr(simple, "local_completion", forbidden)
    assert simple.run_simple_task(
        payload(), threading.Event(), remote_run=forbidden
    ).infra_wait
