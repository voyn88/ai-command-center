from __future__ import annotations

import json
import subprocess

import pytest

from scripts import aicc_aider_benchmark as benchmark


def test_mechanical_verifier_rejects_a_corrupted_prefixed_value(tmp_path):
    (tmp_path / "limits.py").write_text("MAX_ITEMS = 100\n", encoding="utf-8")
    assert benchmark._verify_mechanical_rename_constant(tmp_path) is False


def test_fixture_verifier_requires_the_whole_document_unchanged(tmp_path):
    fixture = tmp_path / "fixtures" / "golden.json"
    fixture.parent.mkdir()
    fixture.write_text(
        json.dumps({"expected_count": 7, "label": "corrupted"}), encoding="utf-8"
    )
    assert benchmark._verify_fixture_update_golden_value(tmp_path) is False


def test_seed_repo_surfaces_git_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="git failed"
        ),
    )
    with pytest.raises(RuntimeError, match="git failed"):
        benchmark._seed_repo(tmp_path, {"README.md": "x"})


def test_main_is_nonzero_when_any_case_fails(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "run_suite",
        lambda **kwargs: [benchmark.CaseResult("case", False, "bad patch")],
    )
    assert benchmark.main([]) == 1
