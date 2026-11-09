"""Transport contract for server-authoritative CTO decisions, without a DB."""
import hashlib

import pytest

from command_center.db.work_queue_store import WorkQueueStore
from command_center.db import work_queue_store
from tests.worker.test_daemon import _work


@pytest.mark.parametrize("allowed", [False, True])
def test_start_sends_fenced_capability_hash_not_payload_authority(monkeypatch, allowed):
    store = WorkQueueStore()
    work = _work({"actor": "CTO", "executor": "invented", "kind": "agent_run"})
    calls = []

    def call(sql, params):
        calls.append((sql, params))
        return {"ok": allowed}

    monkeypatch.setattr(store, "_call", call)
    assert store.authorize_start(work) is allowed
    assert calls == [("SELECT * FROM cto_authorize_start(%s, %s)", (
        work.attempt_id, hashlib.sha256(work.claim_token.encode("ascii")).hexdigest(),
    ))]
    assert work.claim_token not in repr(calls)


def test_enqueue_refusal_never_becomes_a_work_item_id(monkeypatch):
    store = WorkQueueStore()
    monkeypatch.setattr(store, "_call", lambda *args: {"work_item_id": None})
    with pytest.raises(ValueError, match="^queue admission refused$"):
        store.enqueue("execution", idempotency_key="denied", payload={"kind": "agent_run"})


@pytest.mark.parametrize("delay", [0, 1, 2])
def test_claim_budget_includes_entire_database_roundtrip(monkeypatch, delay):
    clock = [100.0]
    monkeypatch.setattr(work_queue_store.time, "monotonic", lambda: clock[0])
    store = WorkQueueStore()

    def call(*args):
        clock[0] += delay
        return dict(ok=True, work_item_id="w", attempt_id="a", attempt_no=1,
                    visible_until="2099-01-01T00:00:00Z", payload={})

    monkeypatch.setattr(store, "_call", call)
    result = store.claim("execution", visibility_seconds=1)
    if delay:
        assert result.reason == "claim_lease_elapsed"
    else:
        assert result.start_deadline == 101.0
