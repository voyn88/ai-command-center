"""Edge/core split for the pre-analysis loop (VOYN-MIN-DEVICE-AI-LOOP).

Hardware acceptance for this task: *"до 30% простых задач выполняются на edge
без потери трассируемости"* — up to 30% of *simple* tasks may run their
preliminary analysis on the edge device instead of core, and doing so must
never drop the audit trail that ties an edge result back to the task that
produced it.

Two guarantees make that true:

1. **Budget enforcement** (:meth:`EdgeConsensusService.decide`). The 30% cap is
   a ratio over *simple* tasks only — complex tasks are never eligible for
   edge execution regardless of budget. The ratio is computed from the
   append-only ledger itself (never a separate in-memory counter that could
   drift from what was actually recorded), so the cap holds across process
   restarts and concurrent instances reading the same ledger file.
2. **Signed, chained digests** (:meth:`EdgeConsensusService.submit_digest`).
   Every edge-produced result is wrapped in an :class:`EdgeDigest` that carries
   the originating decision id, the task id, a content hash of the analysis
   payload, and an HMAC-SHA256 signature core can verify without trusting the
   edge device's transport. The digest is appended to the same ledger as the
   decision that authorized it, so :meth:`trace` can always walk
   decision → digest → core-ack for a task; a digest with a signature that
   fails verification is recorded as ``verified: False`` rather than dropped,
   so the loss is itself traceable instead of silent.

Persistence is the append-only JSON Lines convention used elsewhere in this
app (see :mod:`command_center.storage`'s module docstring) — a single ledger
file at ``data/edge_consensus.jsonl``, never rewritten in place.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from command_center import storage
from command_center.models import iso_now, new_id

ROOT = Path(__file__).resolve().parents[2]

#: Default share of *simple* tasks that may be offloaded to the edge device.
DEFAULT_EDGE_BUDGET_RATIO = 0.30

#: Ledger entry kinds.
KIND_DECISION = "decision"
KIND_DIGEST = "digest"
KIND_CORE_ACK = "core_ack"

#: Task fields (in priority order) consulted to estimate whether a task is
#: "simple" enough to be a candidate for edge pre-analysis. A task is simple
#: when it declares itself so explicitly, or — absent that — when it carries
#: no dependencies and its title/description are short. This mirrors the
#: heuristic used nowhere else yet in this codebase, so it is deliberately
#: conservative: anything ambiguous is treated as complex (core-only).
_SIMPLE_MAX_TEXT_LEN = 240


class EdgeConsensusError(Exception):
    """Base error for the edge-consensus loop."""


class DigestVerificationError(EdgeConsensusError):
    """A digest's signature did not match its claimed content."""


@dataclass(frozen=True)
class EdgeVerdict:
    """The outcome of :meth:`EdgeConsensusService.decide` for one task."""

    task_id: str
    decision_id: str
    simple: bool
    offloaded: bool
    reason: str
    ts: str


@dataclass(frozen=True)
class EdgeDigest:
    """A signed summary of an edge device's pre-analysis result.

    ``digest_id`` and ``signature`` are hex strings so the digest can be
    logged, transmitted and compared without any binary-safety concerns.
    """

    digest_id: str
    decision_id: str
    task_id: str
    node_id: str
    result_hash: str
    signature: str
    produced_at: str

    def to_dict(self) -> dict:
        return {
            "digest_id": self.digest_id,
            "decision_id": self.decision_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "result_hash": self.result_hash,
            "signature": self.signature,
            "produced_at": self.produced_at,
        }

    def _signing_payload(self) -> bytes:
        return _canonical_bytes(
            {
                "digest_id": self.digest_id,
                "decision_id": self.decision_id,
                "task_id": self.task_id,
                "node_id": self.node_id,
                "result_hash": self.result_hash,
                "produced_at": self.produced_at,
            }
        )


def default_is_simple(task: dict) -> bool:
    """Conservative default heuristic: a task is a candidate for edge
    pre-analysis only when it has no unmet dependency edges and both its
    title and description are short. Anything else — including a task that
    does not declare these fields at all — is treated as complex."""
    if task.get("depends_on"):
        return False
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    if len(title) > _SIMPLE_MAX_TEXT_LEN or len(description) > _SIMPLE_MAX_TEXT_LEN:
        return False
    complexity = task.get("complexity")
    if complexity is not None:
        return str(complexity).lower() == "simple"
    return True


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _hash_result(result: object) -> str:
    if isinstance(result, (bytes, bytearray)):
        raw = bytes(result)
    elif isinstance(result, str):
        raw = result.encode("utf-8")
    else:
        raw = _canonical_bytes(result if isinstance(result, dict) else {"value": result})
    return hashlib.sha256(raw).hexdigest()


class EdgeConsensusService:
    """Decides which simple tasks may run their pre-analysis on the edge
    device, keeping the 30% budget and the signed-digest audit trail.

    Every collaborator (root path, secret key, clock, simplicity predicate) is
    held on the instance so tests can inject fakes; defaults wire the real
    ledger file, a process-local signing key and the real classifier.
    """

    def __init__(
        self,
        *,
        root: Path = ROOT,
        secret_key: bytes | str,
        budget_ratio: float = DEFAULT_EDGE_BUDGET_RATIO,
        is_simple: Callable[[dict], bool] = default_is_simple,
        clock: Callable[[], str] = iso_now,
        id_factory: Callable[[], str] = new_id,
        submit_to_core: Callable[[dict], None] | None = None,
    ) -> None:
        if not secret_key:
            raise ValueError("secret_key is required to sign edge digests")
        if not 0.0 <= budget_ratio <= 1.0:
            raise ValueError("budget_ratio must be between 0.0 and 1.0")
        self._root = root
        self._secret_key = secret_key.encode("utf-8") if isinstance(secret_key, str) else secret_key
        self._budget_ratio = budget_ratio
        self._is_simple = is_simple
        self._clock = clock
        self._id_factory = id_factory
        self._submit_to_core = submit_to_core or self._default_submit_to_core

    def _ledger_path(self) -> Path:
        return storage.resolve_data_dir(self._root) / "edge_consensus.jsonl"

    def _ledger(self) -> list[dict]:
        return storage.read_jsonl(self._ledger_path())

    def _append(self, entry: dict) -> dict:
        storage.append_jsonl(self._ledger_path(), entry)
        return entry

    # -- budget --------------------------------------------------------

    def offload_ratio(self) -> float:
        """Current share of *simple* decisions that were offloaded to the
        edge, over every decision recorded so far. ``0.0`` when no simple
        task has been decided yet."""
        simple_count = 0
        offloaded_count = 0
        for entry in self._ledger():
            if entry.get("kind") != KIND_DECISION or not entry.get("simple"):
                continue
            simple_count += 1
            if entry.get("offloaded"):
                offloaded_count += 1
        if simple_count == 0:
            return 0.0
        return offloaded_count / simple_count

    # -- decision --------------------------------------------------------

    def decide(self, task: dict) -> EdgeVerdict:
        """Decide whether ``task``'s pre-analysis may run on the edge device.

        Complex tasks are never offloaded. A simple task is offloaded only
        while doing so keeps the running ratio at or below the configured
        budget (default 30%) — the check is a *projection*: it offloads when
        ``(offloaded + 1) / (simple + 1) <= budget_ratio``, so the ledger
        never overshoots the cap by one entry the way a post-hoc check would.
        The decision itself is appended to the ledger unconditionally
        (offloaded or not), which is what makes the ratio computable from the
        ledger alone.
        """
        task_id = task.get("id") or task.get("task_id")
        if not task_id:
            raise ValueError("task must carry an 'id' (or 'task_id') to be traceable")

        simple = bool(self._is_simple(task))
        offloaded = False
        reason = "complex_task_core_only"
        if simple:
            simple_count = 0
            offloaded_count = 0
            for entry in self._ledger():
                if entry.get("kind") != KIND_DECISION or not entry.get("simple"):
                    continue
                simple_count += 1
                if entry.get("offloaded"):
                    offloaded_count += 1
            projected_ratio = (offloaded_count + 1) / (simple_count + 1)
            if projected_ratio <= self._budget_ratio:
                offloaded = True
                reason = "simple_task_within_edge_budget"
            else:
                reason = "simple_task_edge_budget_exhausted"

        decision_id = self._id_factory()
        ts = self._clock()
        self._append(
            {
                "kind": KIND_DECISION,
                "decision_id": decision_id,
                "task_id": str(task_id),
                "simple": simple,
                "offloaded": offloaded,
                "reason": reason,
                "ts": ts,
            }
        )
        return EdgeVerdict(
            task_id=str(task_id),
            decision_id=decision_id,
            simple=simple,
            offloaded=offloaded,
            reason=reason,
            ts=ts,
        )

    # -- signed digest --------------------------------------------------

    def sign_digest(
        self,
        *,
        decision: EdgeVerdict,
        node_id: str,
        result: object,
    ) -> EdgeDigest:
        """Build and sign an :class:`EdgeDigest` for the edge device's
        analysis ``result``, chained to the ``decision`` that authorized it.
        Raises if the decision did not actually authorize an edge run, so a
        misbehaving edge device cannot mint a digest for work it was not
        allowed to do."""
        if not decision.offloaded:
            raise EdgeConsensusError(
                f"decision {decision.decision_id} for task {decision.task_id} "
                "did not authorize edge execution"
            )
        digest_id = self._id_factory()
        produced_at = self._clock()
        result_hash = _hash_result(result)
        digest = EdgeDigest(
            digest_id=digest_id,
            decision_id=decision.decision_id,
            task_id=decision.task_id,
            node_id=node_id,
            result_hash=result_hash,
            signature="",
            produced_at=produced_at,
        )
        signature = hmac.new(self._secret_key, digest._signing_payload(), hashlib.sha256).hexdigest()
        return EdgeDigest(
            digest_id=digest_id,
            decision_id=decision.decision_id,
            task_id=decision.task_id,
            node_id=node_id,
            result_hash=result_hash,
            signature=signature,
            produced_at=produced_at,
        )

    def verify_digest(self, digest: EdgeDigest) -> bool:
        """``True`` when ``digest.signature`` matches its content under this
        service's key. Constant-time comparison — never a plain ``==``."""
        expected = hmac.new(self._secret_key, digest._signing_payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, digest.signature)

    def submit_digest(self, digest: EdgeDigest) -> dict:
        """Send a signed digest to core and record the full round trip in the
        ledger, so the task's trace never loses the edge → core hop. Verifies
        the signature first: a forged or corrupted digest is still logged
        (``verified: False``) rather than silently discarded, then raises —
        the point of traceability is that a rejected digest is exactly as
        visible as an accepted one."""
        verified = self.verify_digest(digest)
        entry = self._append(
            {
                "kind": KIND_DIGEST,
                **digest.to_dict(),
                "verified": verified,
            }
        )
        if not verified:
            raise DigestVerificationError(
                f"digest {digest.digest_id} for task {digest.task_id} failed signature verification"
            )
        ack = self._submit_to_core(digest.to_dict())
        self._append(
            {
                "kind": KIND_CORE_ACK,
                "digest_id": digest.digest_id,
                "decision_id": digest.decision_id,
                "task_id": digest.task_id,
                "ts": self._clock(),
                "core_ref": (ack or {}).get("core_ref") if isinstance(ack, dict) else None,
            }
        )
        return entry

    def _default_submit_to_core(self, digest: dict) -> dict:
        """No-op transport used when no real core endpoint is wired in: the
        digest is still fully recorded in the ledger by :meth:`submit_digest`,
        only the network hop is stubbed out."""
        return {"core_ref": digest["digest_id"]}

    # -- traceability ------------------------------------------------------

    def trace(self, task_id: str) -> list[dict]:
        """Every ledger entry for ``task_id``, in recorded order — the full
        decision → digest → core-ack chain (or as much of it as exists),
        satisfying the "no loss of traceability" acceptance criterion."""
        return [entry for entry in self._ledger() if entry.get("task_id") == str(task_id)]

    def stats(self) -> dict:
        """Aggregate counters over the whole ledger: how many simple tasks
        were seen, how many were offloaded, the current ratio, and how many
        digests failed verification."""
        simple = offloaded = digests = failed = 0
        for entry in self._ledger():
            if entry.get("kind") == KIND_DECISION and entry.get("simple"):
                simple += 1
                if entry.get("offloaded"):
                    offloaded += 1
            elif entry.get("kind") == KIND_DIGEST:
                digests += 1
                if not entry.get("verified"):
                    failed += 1
        return {
            "simple_tasks": simple,
            "offloaded_tasks": offloaded,
            "offload_ratio": (offloaded / simple) if simple else 0.0,
            "budget_ratio": self._budget_ratio,
            "digests_submitted": digests,
            "digests_failed_verification": failed,
        }
