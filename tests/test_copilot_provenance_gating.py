"""Regression for SEC-1 residual (D-01, Copilot variant): the Copilot executor
launches with a hardcoded `--allow-all-tools --no-ask-user` and ignored
`untrusted`/`operator_elevated`, so an imported (untrusted) task got full tool
access with no approval. Copilot has no read-only tool mode wired here, so it
must fail closed for untrusted input rather than grant all tools.
"""

from __future__ import annotations

import types

import pytest

from command_center.runtime import providers


def _force_available(monkeypatch):
    avail = types.SimpleNamespace(
        provider_id="copilot_cli",
        available=True,
        code="usable",
        message="ok",
        executable="/usr/bin/true",
        version="copilot-fake",
    )
    monkeypatch.setattr(providers.CopilotProvider, "availability", lambda self: avail)


def _launch(tmp_path, *, task_type="implementation", **kwargs):
    return providers.CopilotProvider().build_launch(
        repository_path=tmp_path,
        session_id="s",
        prompt="p",
        task_type=task_type,
        is_resume=False,
        model=None,
        **kwargs,
    )


def test_copilot_refuses_untrusted_task(tmp_path, monkeypatch):
    _force_available(monkeypatch)
    with pytest.raises(RuntimeError):
        _launch(tmp_path, untrusted=True)


def test_copilot_trusted_task_launches_with_all_tools(tmp_path, monkeypatch):
    _force_available(monkeypatch)
    spec = _launch(tmp_path, untrusted=False)
    assert "--allow-all-tools" in list(spec.argv)


def test_copilot_untrusted_but_operator_elevated_launches(tmp_path, monkeypatch):
    _force_available(monkeypatch)
    spec = _launch(tmp_path, untrusted=True, operator_elevated=True)
    assert "--allow-all-tools" in list(spec.argv)


def test_copilot_untrusted_independent_review_is_model_only(tmp_path, monkeypatch):
    _force_available(monkeypatch)
    spec = _launch(tmp_path, task_type="independent_review", untrusted=True)

    assert "--available-tools=" in spec.argv
    assert "--allow-all-tools" not in spec.argv
    assert "--allow-tool" not in spec.argv
