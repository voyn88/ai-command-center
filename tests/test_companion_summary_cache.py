"""Companion Sync Service — summary_cache (VOYN-MIN-OFFLINE-SUMMARY).

The acceptance criterion this suite defends: when a device loses network, the
owner still sees priority events without losing context. That decomposes into
the properties tested below —

  * `build` composes its payload entirely from the existing, already-tested
    `adapters.recommendations` and `notify.pending_for_device` — it never
    invents its own ranking or event shape;
  * `write` persists a *signed* envelope, and `read` verifies that signature
    on every read, refusing a payload whose signature does not match rather
    than trusting on-disk content merely because it exists at the expected
    path;
  * a cache written with one secret is not readable with a different one, and
    a tampered payload fails verification even when the stored signature
    string is left untouched.
"""

from __future__ import annotations

from command_center import models, tasks_repository
from command_center.companion import notify, summary_cache

SECRET = "test-secret"


def _task(task_id="t1", **overrides):
    task = {
        "id": task_id,
        "project": "AIOS",
        "title": "Task",
        "status": "Backlog",
        "priority": "High",
        "depends_on": [],
    }
    task.update(models.default_task_execution_fields())
    task.update(models.default_task_workflow_fields())
    task.update(overrides)
    return task


# --------------------------------------------------------------------------
# build: composed from existing, already-tested sources
# --------------------------------------------------------------------------


def test_build_uses_the_existing_recommendation_view_shape(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1"), _task("t2", priority="Low")])
    payload = summary_cache.build("device-1", root=tmp_path, limit=5)
    assert payload["device_id"] == "device-1"
    for view in payload["priority_events"]:
        assert {"task_id", "title", "score", "reasons", "ready"} <= set(view)


def test_build_prefers_high_priority_events_when_present(tmp_path):
    tasks_repository.save_tasks(
        tmp_path, [_task("t1", priority="High"), _task("t2", priority="Low")]
    )
    payload = summary_cache.build("device-1", root=tmp_path, limit=5)
    priorities = {view["priority"] for view in payload["priority_events"]}
    assert priorities <= {"High", "Critical"}


def test_build_includes_this_devices_own_pending_notification_backlog(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1")])
    notify.record_transition(
        "task", "t1", from_status="Backlog", to_status="Ready", at="t0", root=tmp_path
    )
    payload = summary_cache.build("device-1", root=tmp_path)
    assert [event["entity_id"] for event in payload["pending_notifications"]] == ["t1"]
    assert payload["cursor"] == 0  # unregistered device -- nothing acknowledged yet


# --------------------------------------------------------------------------
# write/read: a signed, tamper-evident envelope
# --------------------------------------------------------------------------


def test_write_then_read_round_trips_the_same_payload(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1")])
    written = summary_cache.write("device-1", root=tmp_path, secret=SECRET)
    read_back = summary_cache.read("device-1", root=tmp_path, secret=SECRET)
    assert read_back == written["payload"]


def test_read_with_no_cache_returns_none(tmp_path):
    assert summary_cache.read("device-1", root=tmp_path, secret=SECRET) is None


def test_read_with_the_wrong_secret_fails_verification(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1")])
    summary_cache.write("device-1", root=tmp_path, secret=SECRET)
    assert summary_cache.read("device-1", root=tmp_path, secret="wrong-secret") is None


def test_a_tampered_payload_fails_verification_even_with_the_right_signature_string(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1")])
    summary_cache.write("device-1", root=tmp_path, secret=SECRET)

    from command_center import storage

    registry = storage.read_json(summary_cache._cache_path(tmp_path), {})
    registry["device-1"]["payload"]["priority_events"] = [{"task_id": "injected"}]
    storage.atomic_write_json(summary_cache._cache_path(tmp_path), registry)

    assert summary_cache.read("device-1", root=tmp_path, secret=SECRET) is None


def test_write_overwrites_a_previous_cache_for_the_same_device_not_merges_it(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1")])
    summary_cache.write("device-1", root=tmp_path, secret=SECRET)
    tasks_repository.save_tasks(tmp_path, [_task("t2", title="Second")])
    summary_cache.write("device-1", root=tmp_path, secret=SECRET)

    payload = summary_cache.read("device-1", root=tmp_path, secret=SECRET)
    task_ids = {view["task_id"] for view in payload["priority_events"]}
    assert task_ids == {"t2"}


def test_write_keeps_separate_devices_independent(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1")])
    summary_cache.write("device-1", root=tmp_path, secret=SECRET)
    summary_cache.write("device-2", root=tmp_path, secret=SECRET)

    assert summary_cache.read("device-1", root=tmp_path, secret=SECRET)["device_id"] == "device-1"
    assert summary_cache.read("device-2", root=tmp_path, secret=SECRET)["device_id"] == "device-2"


# --------------------------------------------------------------------------
# sign_payload / verify_signature: deterministic and constant-time
# --------------------------------------------------------------------------


def test_sign_payload_is_deterministic():
    payload = {"b": 2, "a": 1}
    assert summary_cache.sign_payload(payload, secret=SECRET) == summary_cache.sign_payload(
        {"a": 1, "b": 2}, secret=SECRET
    )


def test_verify_signature_rejects_a_mismatched_signature():
    payload = {"a": 1}
    signature = summary_cache.sign_payload(payload, secret=SECRET)
    assert summary_cache.verify_signature(payload, signature, secret=SECRET)
    assert not summary_cache.verify_signature({"a": 2}, signature, secret=SECRET)
