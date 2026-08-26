"""UI smoke coverage for the Integration Center "Projects" surface
(`command_center/ui/integration_center.py` + the `integration` page in
`app.py`) — AICC-INT-001 increment 1.

Two layers, mirroring the other `ui` page suites:

1. `AppTest.from_function` renders the component in isolation with a stubbed
   registry/collector so no real `git`/`gh` ever runs.
2. `AppTest.from_file` drives the real `app.py` with `nav_page =
   "integration"` — the page must render (registry seeded into the isolated
   `AICC_DATA_DIR`) without touching the network.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _render_with_stubbed_health() -> None:
    from unittest.mock import patch

    from command_center.integration import collectors, registry
    from command_center.ui import integration_center

    entries = [
        {
            "id": "demo-app",
            "name": "Demo App",
            "kind": "application",
            "project": "PERSONAL",
            "repo_path": "~/path/to/repo",
            "remote": None,
            "default_branch": "main",
        }
    ]
    health = {
        "id": "demo-app",
        "worktree_state": "ok",
        "git": {
            "available": True,
            "branch": "main",
            "dirty": True,
            "modified_count": 2,
            "untracked_count": 0,
            "last_commit_subject": "feat: something",
            "last_activity": "2026-08-12T10:00:00+05:00",
        },
        "github": {"available": True, "open_pr_count": 3, "ci_state": "failure", "error": None},
    }
    tasks = [
        {"id": "t1", "project": "PERSONAL", "title": "Ship rc5", "status": "Backlog", "priority": "High"},
        {"id": "t2", "project": "PERSONAL", "title": "Done thing", "status": "Done", "priority": "Low"},
        {"id": "t3", "project": "AICC", "title": "Other project", "status": "Backlog", "priority": "Low"},
    ]
    runs = [
        {"id": "run12345", "project": "PERSONAL", "task_type": "implementation", "status": "completed", "created_at": "2026-08-12T09:00:00"},
        {"id": "run99999", "project": "AICC", "task_type": "review", "status": "failed", "created_at": "2026-08-12T08:00:00"},
    ]
    with (
        patch.object(registry, "load_entries", return_value=entries),
        patch.object(collectors, "collect_health", return_value=health),
    ):
        import streamlit as st

        st.session_state["integration_center_health"] = {"demo-app": health}
        st.session_state["integration_drilldown"] = "demo-app"
        integration_center.render_integration_center(tasks, runs)


def test_component_renders_health_badges_and_drilldown():
    at = AppTest.from_function(_render_with_stubbed_health)
    at.run()
    assert not at.exception
    page_text = " ".join(str(getattr(el, "value", "")) for el in at.markdown) + " ".join(
        c.value for c in at.caption
    )
    assert "Demo App" in page_text
    # Drill-down: only the open PERSONAL task, not the Done one, not AICC's.
    assert "Ship rc5" in page_text
    assert "Done thing" not in page_text
    assert "Other project" not in page_text
    # Only the PERSONAL run appears in recent runs.
    assert "run12345"[:8] in page_text
    assert "run99999"[:8] not in page_text


def test_component_shows_uncollected_state_without_calling_collectors():
    def render_without_collection() -> None:
        from unittest.mock import patch

        from command_center.integration import collectors, registry
        from command_center.ui import integration_center

        def boom(entry):  # collectors must not run before the button is pressed
            raise AssertionError("collect_health must not be called on plain render")

        entries = [
            {
                "id": "example-svc",
                "name": "Example Service",
                "kind": "service",
                "project": "AICC",
                "repo_path": None,
                "remote": None,
                "default_branch": "main",
            }
        ]
        with (
            patch.object(registry, "load_entries", return_value=entries),
            patch.object(collectors, "collect_health", boom),
        ):
            integration_center.render_integration_center([], [])

    at = AppTest.from_function(render_without_collection)
    at.run()
    assert not at.exception
    captions = " ".join(str(getattr(el, "value", "")) for el in at.markdown)
    assert "Example Service" in captions


def test_full_app_page_renders_registry_without_network(monkeypatch):
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "integration"
    at.run()
    assert not at.exception
    page_text = (
        " ".join(str(getattr(el, "value", "")) for el in at.markdown)
        + " ".join(str(el.value) for el in at.subheader)
        + " ".join(c.value for c in at.caption)
    )
    # The seeded registry renders; no health collected yet, so no git/gh ran.
    assert "Integration Center" in page_text
    assert "example-app" in page_text
