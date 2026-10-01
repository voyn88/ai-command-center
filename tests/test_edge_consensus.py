"""Unit tests for the edge/core pre-analysis loop (``command_center.edge.consensus``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox, so
the ledger file each service instance writes is throwaway.
"""

from __future__ import annotations

import pytest

from command_center.edge.consensus import (
    DigestVerificationError,
    EdgeConsensusError,
    EdgeConsensusService,
    EdgeDigest,
    default_is_simple,
)


def make_service(**kwargs) -> EdgeConsensusService:
    return EdgeConsensusService(secret_key="test-secret-key", **kwargs)


def simple_task(task_id: str) -> dict:
    return {"id": task_id, "title": "trivial check", "description": "short"}


def complex_task(task_id: str) -> dict:
    return {"id": task_id, "title": "x" * 500, "description": ""}


# -- classification ----------------------------------------------------------


def test_default_is_simple_rejects_dependencies():
    assert default_is_simple({"id": "t1", "title": "a"}) is True
    assert default_is_simple({"id": "t1", "title": "a", "depends_on": ["t0"]}) is False


def test_default_is_simple_rejects_long_text():
    assert default_is_simple({"id": "t1", "title": "x" * 300}) is False


def test_default_is_simple_honours_explicit_complexity_field():
    assert default_is_simple({"id": "t1", "title": "a", "complexity": "complex"}) is False
    assert default_is_simple({"id": "t1", "title": "a", "complexity": "Simple"}) is True


# -- decision / budget --------------------------------------------------------


def test_complex_task_is_never_offloaded():
    svc = make_service()
    verdict = svc.decide(complex_task("t1"))
    assert verdict.simple is False
    assert verdict.offloaded is False
    assert verdict.reason == "complex_task_core_only"


def test_decide_requires_traceable_task_id():
    svc = make_service()
    with pytest.raises(ValueError):
        svc.decide({"title": "no id"})


def test_offload_budget_caps_near_30_percent():
    svc = make_service(budget_ratio=0.30)
    verdicts = [svc.decide(simple_task(f"t{i}")) for i in range(20)]
    offloaded = sum(1 for v in verdicts if v.offloaded)
    ratio = offloaded / len(verdicts)
    assert ratio <= 0.30
    # The budget should actually be used, not starved to zero.
    assert offloaded >= 5
    assert svc.offload_ratio() == pytest.approx(ratio)


def test_offload_ratio_ignores_complex_tasks():
    svc = make_service(budget_ratio=1.0)
    svc.decide(complex_task("c1"))
    svc.decide(complex_task("c2"))
    verdict = svc.decide(simple_task("s1"))
    assert verdict.offloaded is True
    assert svc.offload_ratio() == pytest.approx(1.0)


def test_budget_persists_across_service_instances(tmp_path):
    svc1 = make_service(root=tmp_path, budget_ratio=0.30)
    for i in range(10):
        svc1.decide(simple_task(f"t{i}"))
    svc2 = make_service(root=tmp_path, budget_ratio=0.30)
    verdict = svc2.decide(simple_task("t-new"))
    # Ratio computed from the shared ledger must respect the same cap.
    assert svc2.offload_ratio() <= 0.30 + 1e-9
    assert verdict.task_id == "t-new"


# -- signed digest -------------------------------------------------------------


def test_sign_digest_round_trips_and_verifies():
    svc = make_service()
    verdict = svc.decide(simple_task("t1"))
    if not verdict.offloaded:
        # Force a clean budget for this test's purpose.
        svc = make_service(budget_ratio=1.0)
        verdict = svc.decide(simple_task("t1"))
    digest = svc.sign_digest(decision=verdict, node_id="edge-node-1", result={"score": 0.9})
    assert digest.task_id == "t1"
    assert digest.decision_id == verdict.decision_id
    assert svc.verify_digest(digest) is True


def test_sign_digest_rejects_unauthorized_decision():
    svc = make_service(budget_ratio=0.0)
    verdict = svc.decide(simple_task("t1"))
    assert verdict.offloaded is False
    with pytest.raises(EdgeConsensusError):
        svc.sign_digest(decision=verdict, node_id="edge-node-1", result={"score": 0.1})


def test_verify_digest_detects_tampering():
    svc = make_service(budget_ratio=1.0)
    verdict = svc.decide(simple_task("t1"))
    digest = svc.sign_digest(decision=verdict, node_id="edge-node-1", result={"score": 0.9})
    tampered = EdgeDigest(
        digest_id=digest.digest_id,
        decision_id=digest.decision_id,
        task_id=digest.task_id,
        node_id=digest.node_id,
        result_hash="0" * 64,
        signature=digest.signature,
        produced_at=digest.produced_at,
    )
    assert svc.verify_digest(tampered) is False


def test_different_secret_keys_do_not_cross_verify():
    svc_a = make_service()
    svc_b = EdgeConsensusService(secret_key="another-secret", root=svc_a._root)
    verdict = svc_a.decide(simple_task("t1"))
    if not verdict.offloaded:
        svc_a = make_service(budget_ratio=1.0, root=svc_a._root)
        verdict = svc_a.decide(simple_task("t1"))
    digest = svc_a.sign_digest(decision=verdict, node_id="edge-node-1", result="ok")
    assert svc_a.verify_digest(digest) is True
    assert svc_b.verify_digest(digest) is False


# -- submission + traceability --------------------------------------------------


def test_submit_digest_records_full_chain_and_calls_core():
    calls = []
    svc = make_service(budget_ratio=1.0, submit_to_core=lambda d: calls.append(d) or {"core_ref": "core-1"})
    verdict = svc.decide(simple_task("t1"))
    digest = svc.sign_digest(decision=verdict, node_id="edge-node-1", result={"ok": True})
    svc.submit_digest(digest)

    assert calls and calls[0]["digest_id"] == digest.digest_id

    trace = svc.trace("t1")
    kinds = [entry["kind"] for entry in trace]
    assert kinds == ["decision", "digest", "core_ack"]
    assert trace[1]["verified"] is True
    assert trace[2]["core_ref"] == "core-1"


def test_submit_digest_raises_and_still_logs_forged_signature():
    svc = make_service(budget_ratio=1.0)
    verdict = svc.decide(simple_task("t1"))
    digest = svc.sign_digest(decision=verdict, node_id="edge-node-1", result={"ok": True})
    forged = EdgeDigest(
        digest_id=digest.digest_id,
        decision_id=digest.decision_id,
        task_id=digest.task_id,
        node_id=digest.node_id,
        result_hash=digest.result_hash,
        signature="deadbeef",
        produced_at=digest.produced_at,
    )
    with pytest.raises(DigestVerificationError):
        svc.submit_digest(forged)

    trace = svc.trace("t1")
    digest_entries = [e for e in trace if e["kind"] == "digest"]
    assert digest_entries[0]["verified"] is False
    # No core_ack was recorded for a rejected digest.
    assert all(e["kind"] != "core_ack" for e in trace)


def test_trace_is_scoped_per_task():
    svc = make_service(budget_ratio=1.0)
    svc.decide(simple_task("t1"))
    svc.decide(simple_task("t2"))
    trace_t1 = svc.trace("t1")
    assert all(entry["task_id"] == "t1" for entry in trace_t1)
    assert len(trace_t1) == 1


def test_stats_reports_ratio_and_verification_failures():
    svc = make_service(budget_ratio=1.0)
    verdict = svc.decide(simple_task("t1"))
    digest = svc.sign_digest(decision=verdict, node_id="edge-node-1", result="ok")
    svc.submit_digest(digest)
    stats = svc.stats()
    assert stats["simple_tasks"] == 1
    assert stats["offloaded_tasks"] == 1
    assert stats["offload_ratio"] == pytest.approx(1.0)
    assert stats["digests_submitted"] == 1
    assert stats["digests_failed_verification"] == 0


def test_secret_key_required():
    with pytest.raises(ValueError):
        EdgeConsensusService(secret_key="")


def test_budget_ratio_must_be_in_unit_interval():
    with pytest.raises(ValueError):
        EdgeConsensusService(secret_key="k", budget_ratio=1.5)
