"""Transport contract for server-authoritative CTO decisions, without a DB."""
import hashlib

import pytest

from command_center.db.work_queue_store import WorkQueueStore
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
