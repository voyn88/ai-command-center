"""Gate for Founder Functional Audit 9761459, row AUDIT-W1-008.

The row's remediation (git `c72c4ed`, "opt-in background sync daemon for
headless hosts") added `task_pipeline.start_background_sync`/`stop_background_sync`
as a bounded daemon poller, with the explicit safety property that it stays off
in the interactive default: `app.get_execution_center_api()` only starts it when
`AICC_BACKGROUND_SYNC` is set (`app.py`):

    if os.environ.get("AICC_BACKGROUND_SYNC"):
        task_pipeline.start_background_sync(ROOT, api, project_config.load_project_configs)

`tests/test_task_pipeline_background_sync.py` gates the daemon's own mechanics
(start/stop/idempotency/failure survival), but nothing gated this specific `if` —
the one line that actually keeps the sync daemon opt-in. A later edit that drops
the guard (or inverts it) would start a background daemon touching every task in
`data/tasks.json` on every interactive server process, with no test going red.
This module is that gate.
"""

from __future__ import annotations

import app
import streamlit as st

from command_center import task_pipeline


def _clear_singleton() -> None:
    st.cache_resource.clear()


def test_background_sync_does_not_start_when_env_var_is_unset(monkeypatch):
    monkeypatch.delenv("AICC_BACKGROUND_SYNC", raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(
        task_pipeline, "start_background_sync", lambda *a, **kw: calls.append((a, kw))
    )
    _clear_singleton()

    app.get_execution_center_api()

    assert calls == [], (
        "start_background_sync must not run when AICC_BACKGROUND_SYNC is unset — "
        f"it was called with: {calls!r}"
    )


def test_background_sync_starts_when_env_var_is_set(monkeypatch):
    monkeypatch.setenv("AICC_BACKGROUND_SYNC", "1")
    calls: list[tuple] = []
    monkeypatch.setattr(
        task_pipeline, "start_background_sync", lambda *a, **kw: calls.append((a, kw))
    )
    _clear_singleton()

    api = app.get_execution_center_api()

    assert len(calls) == 1, (
        "start_background_sync must run exactly once when AICC_BACKGROUND_SYNC is set; "
        f"got: {calls!r}"
    )
    args, _kwargs = calls[0]
    assert args[0] == app.ROOT
    assert args[1] is api
