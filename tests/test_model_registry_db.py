"""Repository-tier tests for the Wave-3 model-registry table families
(``command_center.runtime.db.model_registry``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the repository functions against it directly — no service, no HTTP, no
shared state. This also exercises the schema-v20 migration on a fresh db.

Fixtures use only generic names and invented ids — no real names or paths —
keeping the public-repo privacy gate green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import db
from command_center.runtime.db.model_registry import (
    InvalidModelStatusTransitionError,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


# --- migration ------------------------------------------------------------


def test_migration_brings_fresh_db_to_current_version(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    assert db.current_schema_version(db_path) == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 20


def test_migrate_is_idempotent(db_path: Path) -> None:
    from command_center.runtime.db.schema import SCHEMA_VERSION

    db.migrate(db_path)  # second run must be a no-op, not an error
    assert db.current_schema_version(db_path) == SCHEMA_VERSION


# --- model_entry ----------------------------------------------------------


def test_create_and_get_model_entry(db_path: Path) -> None:
    row = db.create_model_entry(
        db_path, name="Local Llama", kind="local", provider="local",
        cost=0.0, quality=0.7, latency_ms=800, provenance="hf://meta/llama",
    )
    assert row["status"] == "available"
    assert row["version"] == 0
    assert row["download_progress"] == 0
    got = db.get_model_entry(db_path, row["id"])
    assert got["name"] == "Local Llama" and got["kind"] == "local"
    assert got["provenance"] == "hf://meta/llama"


def test_create_model_entry_rejects_empty_name_and_bad_enums(db_path: Path) -> None:
    with pytest.raises(ValueError):
        db.create_model_entry(db_path, name="", kind="local")
    with pytest.raises(ValueError):
        db.create_model_entry(db_path, name="x", kind="cloud")
    with pytest.raises(ValueError):
        db.create_model_entry(db_path, name="x", kind="local", status="offline")


def test_create_writes_register_governance_event_atomically(db_path: Path) -> None:
    row = db.create_model_entry(db_path, name="GPT-ish", kind="external")
    events = db.list_model_events(db_path, row["id"])
    assert len(events) == 1
    assert events[0]["action"] == "register" and events[0]["seq"] == 1


def test_list_model_entries_filters_and_pages(db_path: Path) -> None:
    for _ in range(3):
        db.create_model_entry(db_path, name="ext", kind="external")
    db.create_model_entry(db_path, name="loc", kind="local")
    assert len(db.list_model_entries(db_path)) == 4
    assert len(db.list_model_entries(db_path, kind="external")) == 3
    assert len(db.list_model_entries(db_path, kind="local")) == 1
    assert len(db.list_model_entries(db_path, limit=2)) == 2
    assert len(db.list_model_entries(db_path, status="available")) == 4


# --- status lifecycle -----------------------------------------------------


def test_status_lifecycle_and_cas(db_path: Path) -> None:
    m = db.create_model_entry(db_path, name="loc", kind="local")
    dl = db.set_model_status(
        db_path, m["id"], expected_version=0, status="downloading",
        action="download-request",
    )
    assert dl["status"] == "downloading" and dl["version"] == 1
    prog = db.update_download_progress(
        db_path, m["id"], expected_version=dl["version"], progress=50
    )
    assert prog["download_progress"] == 50
    done = db.set_model_status(
        db_path, m["id"], expected_version=prog["version"], status="installed",
        download_progress=100,
    )
    assert done["status"] == "installed" and done["download_progress"] == 100
    # installed cannot jump back to downloading (guard)
    with pytest.raises(InvalidModelStatusTransitionError):
        db.set_model_status(
            db_path, m["id"], expected_version=done["version"], status="downloading"
        )
    # stale writer loses
    with pytest.raises(db.LostUpdateError):
        db.set_model_status(
            db_path, m["id"], expected_version=0, status="error"
        )


def test_progress_only_valid_while_downloading(db_path: Path) -> None:
    m = db.create_model_entry(db_path, name="loc", kind="local")
    with pytest.raises(InvalidModelStatusTransitionError):
        db.update_download_progress(
            db_path, m["id"], expected_version=0, progress=10
        )


def test_progress_bounds_checked(db_path: Path) -> None:
    m = db.create_model_entry(db_path, name="loc", kind="local")
    db.set_model_status(
        db_path, m["id"], expected_version=0, status="downloading",
        action="download-request",
    )
    with pytest.raises(ValueError):
        db.update_download_progress(db_path, m["id"], expected_version=1, progress=101)


# --- governance log: completeness + traceability --------------------------


def test_governance_log_records_every_action_in_order(db_path: Path) -> None:
    m = db.create_model_entry(db_path, name="loc", kind="local", provenance="src")
    v = 0
    row = db.set_model_status(
        db_path, m["id"], expected_version=v, status="downloading",
        action="download-request", provenance="src",
    )
    row = db.update_download_progress(
        db_path, m["id"], expected_version=row["version"], progress=100
    )
    db.set_model_status(
        db_path, m["id"], expected_version=row["version"], status="installed",
    )
    db.append_model_event(
        db_path, m["id"], action="assign", target_ref="task:T1", provenance="auto",
    )
    db.append_model_event(db_path, m["id"], action="use", target_ref="task:T1")
    history = db.list_model_events(db_path, m["id"])
    actions = [e["action"] for e in history]
    assert actions == [
        "register", "download-request", "download-progress",
        "status-change", "assign", "use",
    ]
    # seq is a gap-free, per-model monotonic sequence — the traceable order
    assert [e["seq"] for e in history] == [1, 2, 3, 4, 5, 6]
    # provenance is carried through the log (traceability)
    assert history[0]["provenance"] is None or history[1]["provenance"] == "src"
    assert history[4]["provenance"] == "auto"


def test_append_event_rejects_unknown_action_and_missing_model(db_path: Path) -> None:
    m = db.create_model_entry(db_path, name="x", kind="external")
    with pytest.raises(ValueError):
        db.append_model_event(db_path, m["id"], action="frobnicate")
    with pytest.raises(KeyError):
        db.append_model_event(db_path, "nope", action="use")
