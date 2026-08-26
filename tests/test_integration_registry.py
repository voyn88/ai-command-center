"""Unit coverage for the Integration Center project registry
(`command_center/integration/registry.py`) — AICC-INT-001 increment 1.

The registry is the single writer of `data/integration_registry.json`
(docs/AUTHORITY_MAP.md); these tests pin the documented data model
(docs/INTEGRATION_CENTER.md): validated kinds, `models.PROJECT_IDS`
namespace join key, first-read seeding, and locked upsert semantics.
"""

from __future__ import annotations

import pytest

from command_center import models
from command_center.integration import registry


@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_FILE", tmp_path / "integration_registry.json")
    monkeypatch.setattr(registry, "REGISTRY_LOCK_FILE", tmp_path / "integration_registry.lock")
    return tmp_path


def test_first_read_seeds_generic_placeholders_only(isolated_registry):
    entries = registry.load_entries()
    assert [e["id"] for e in entries] == ["example-app", "example-lib"]
    assert registry.REGISTRY_FILE.exists()
    # Placeholders are unconfigured (no machine-local paths in committed
    # defaults — this repo is public) and use real task-board namespaces,
    # the join key onto tasks_repository records.
    assert all(e["repo_path"] is None and e["remote"] is None for e in entries)
    assert all(e["project"] in models.PROJECT_IDS for e in entries)


def test_second_read_returns_persisted_entries_not_reseed(isolated_registry):
    first = registry.load_entries()
    updated = dict(first[0], name="Renamed Engine")
    registry.upsert_entry(updated)
    again = registry.load_entries()
    assert again[0]["name"] == "Renamed Engine"
    assert len(again) == len(first)


def test_upsert_appends_a_new_entry_and_replaces_by_id(isolated_registry):
    registry.load_entries()
    new_entry = {
        "id": "sandbox",
        "name": "Sandbox",
        "kind": "library",
        "project": "PERSONAL",
        "repo_path": None,
        "remote": None,
        "default_branch": "main",
    }
    registry.upsert_entry(new_entry)
    assert registry.get_entry("sandbox")["name"] == "Sandbox"
    registry.upsert_entry(dict(new_entry, name="Sandbox v2"))
    entries = registry.load_entries()
    assert [e["name"] for e in entries if e["id"] == "sandbox"] == ["Sandbox v2"]


def test_unknown_project_namespace_is_rejected(isolated_registry):
    with pytest.raises(registry.RegistryValidationError):
        registry.upsert_entry(
            {"id": "x", "kind": "application", "project": "NOT_A_PROJECT"}
        )


def test_unknown_kind_and_unknown_fields_are_rejected(isolated_registry):
    with pytest.raises(registry.RegistryValidationError):
        registry.upsert_entry({"id": "x", "kind": "mothership", "project": "AICC"})
    assert "application" in registry.ENTRY_KINDS
    with pytest.raises(registry.RegistryValidationError):
        registry.upsert_entry({"id": "x", "kind": "application", "project": "AICC", "extra": 1})


def test_normalize_fills_defaults(isolated_registry):
    entry = registry.normalize_entry({"id": " spaced ", "project": "AICC"})
    assert entry == {
        "id": "spaced",
        "name": "spaced",
        "kind": "other",
        "project": "AICC",
        "repo_path": None,
        "remote": None,
        "default_branch": "main",
    }
