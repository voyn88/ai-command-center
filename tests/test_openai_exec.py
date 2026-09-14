"""The model-only OpenAI-compatible bridge and its executor wiring
(VOYN-W0-AICC-GROQ-VERDICT-BENCH)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from command_center import agent_runner, openai_exec
from command_center.worker import handlers


def test_model_only_sets_agree_by_value():
    """The bridge's standalone copy of MODEL_ONLY_TASK_TYPES must equal the
    runner's authoritative set — pinned here instead of imported there, so
    the module stays standalone-runnable on a worker host."""
    assert openai_exec.MODEL_ONLY_TASK_TYPES == frozenset(
        agent_runner.MODEL_ONLY_TASK_TYPES
    )


def test_provider_routing_is_a_closed_table(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k-groq")
    base, key, bare = openai_exec.resolve_provider("groq/openai/gpt-oss-120b")
    assert base == "https://api.groq.com/openai/v1"
    assert key == "k-groq"
    assert bare == "openai/gpt-oss-120b"

    with pytest.raises(ValueError, match="unknown provider prefix"):
        openai_exec.resolve_provider("acme/some-model")
    with pytest.raises(ValueError, match="unknown provider prefix"):
        openai_exec.resolve_provider("no-prefix-model")

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(LookupError, match="MISTRAL_API_KEY"):
        openai_exec.resolve_provider("mistral/codestral-latest")


def test_builder_serves_only_model_only_task_types():
    with pytest.raises(ValueError, match="MODEL_ONLY"):
        agent_runner.build_openai_http_command(
            "p", task_type="implementation", model="groq/x"
        )
    with pytest.raises(ValueError, match="MODEL_ONLY"):
        agent_runner.build_openai_http_command(
            "p", task_type="verification_review", model="groq/x"
        )
    with pytest.raises(ValueError, match="explicit provider/model"):
        agent_runner.build_openai_http_command("p", task_type="independent_review")
    with pytest.raises(ValueError, match="no capabilities"):
        agent_runner.build_openai_http_command(
            "p",
            task_type="independent_review",
            model="groq/x",
            capability_override="read_only",
        )


def test_builder_argv_shape_and_flag_injection_guard():
    hostile = "--model evil/override rest of envelope"
    argv = agent_runner.build_openai_http_command(
        hostile, task_type="independent_review", model="groq/openai/gpt-oss-120b"
    )
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "command_center.openai_exec"]
    separator = argv.index("--")
    # Everything after `--` is the untrusted prompt, verbatim; the hostile
    # flag text can never replace the selected model.
    assert argv[separator + 1] == hostile
    assert argv[argv.index("--model") + 1] == "groq/openai/gpt-oss-120b"
    assert agent_runner.COMMAND_BUILDERS["openai_http"] == "build_openai_http_command"
    assert agent_runner._command_builder("openai_http") is (
        agent_runner.build_openai_http_command
    )


def test_main_refuses_wrong_task_type_and_empty_prompt(capsys):
    assert (
        openai_exec.main(
            ["--model", "groq/x", "--task-type", "implementation", "--", "p"]
        )
        == 2
    )
    assert "refuses task type" in capsys.readouterr().err
    assert (
        openai_exec.main(
            ["--model", "groq/x", "--task-type", "independent_review", "--", "  "]
        )
        == 2
    )


def test_main_missing_key_is_a_distinct_loud_exit(monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    rc = openai_exec.main(
        ["--model", "groq/x", "--task-type", "independent_review", "--", "prompt"]
    )
    assert rc == 3
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_run_completion_refuses_empty_content(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        assert request.headers.get("User-agent", "").startswith("aicc-openai-exec")
        return _Resp(
            json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": ""}}]}
            ).encode()
        )

    monkeypatch.setattr(openai_exec.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="empty content"):
        openai_exec.run_completion("groq/m", "prompt")


def test_main_prints_model_text_verbatim(monkeypatch, capsys):
    monkeypatch.setattr(
        openai_exec,
        "run_completion",
        lambda model, prompt: "findings...\nVERDICT: ACCEPT\nHEAD_SHA: abc",
    )
    rc = openai_exec.main(
        ["--model", "groq/m", "--task-type", "independent_review", "--", "prompt"]
    )
    assert rc == 0
    assert capsys.readouterr().out.endswith("HEAD_SHA: abc")


def test_worker_preflight_gates_on_provider_keys(monkeypatch):
    for env in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    ok, detail, label = handlers._executor_preflight(
        "openai_http", "independent_review"
    )
    assert not ok and "no provider key" in detail
    assert label == "openai_http provider key unavailable"

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    ok, detail, _ = handlers._executor_preflight("openai_http", "independent_review")
    assert ok and "OPENROUTER_API_KEY" in detail
