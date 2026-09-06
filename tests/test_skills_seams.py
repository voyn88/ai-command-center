"""Unit tests for the two injectable seams
(``command_center.skills.finder``, ``command_center.skills.executor``) --
the supply-chain safety defaults, in isolation from the service/db.
"""

from __future__ import annotations

from command_center.api import models
from command_center.skills.executor import NullSkillExecutor
from command_center.skills.finder import CapabilityRequest, NullCandidateFinder


def test_null_candidate_finder_proposes_nothing() -> None:
    finder = NullCandidateFinder()
    request = CapabilityRequest(
        task_id="t1", task_class="pdf", need="extract text", requested_by="alice",
        requested_at="2026-09-02T00:00:00Z",
    )
    sources = [{"id": "s1", "kind": "mcp_registry", "status": "approved"}]
    assert finder.find(request, sources) == []


def test_null_skill_executor_denies_network_secrets_push() -> None:
    executor = NullSkillExecutor()
    skill = models.SkillItem(
        id="sk1", name="pdf-extract", kind="mcp_server", version="1.0.0",
        content_hash="a" * 64, source_id="s1", status="candidate",
    )
    outcome = executor.acquire(skill)
    assert outcome.metadata == {
        "mode": "null", "network": "denied", "secrets": "denied", "push": "denied",
    }
    assert "pdf-extract" in outcome.detail and "1.0.0" in outcome.detail
