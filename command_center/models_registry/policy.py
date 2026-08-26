"""Routing policy for the model registry: auto-select and the sensitive guard.

Two pure rules, kept out of the service so they can be unit-tested in isolation
and reused:

* :func:`auto_select` — pick the best model for a task, **preferring local**
  (cost): local candidates always win over external, and within a tier the
  cheapest, then highest-quality, then lowest-latency model is chosen. A context
  marked ``sensitive`` restricts the pool to local models only.
* :func:`assert_routing_allowed` — the guard: routing a ``sensitive`` context to
  an ``external`` model is refused, so data marked sensitive is never sent to a
  hosted provider. Called on every assignment/use, not only auto-selection.

Both operate on plain mappings (the repository row shape / the API contract's
``model_dump()``) so neither imports the persistence or HTTP layers.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


class SensitiveModelRoutingError(Exception):
    """Raised when a context marked sensitive would be routed to an external
    model. Surfaced as HTTP 400 — sensitive data must never leave for a hosted
    provider, so the assignment is rejected rather than silently downgraded."""


def _kind(model: Mapping[str, Any]) -> str:
    return str(model.get("kind") or "external")


def assert_routing_allowed(model: Mapping[str, Any], *, sensitive: bool) -> None:
    """Refuse to route a ``sensitive`` context to an ``external`` model.

    A no-op for a non-sensitive context or a local model; raises
    :class:`SensitiveModelRoutingError` otherwise. This is the single choke point
    both the assignment path and the auto-selector call, so the guard cannot be
    bypassed by picking a model directly."""
    if sensitive and _kind(model) == "external":
        raise SensitiveModelRoutingError(
            f"model {model.get('id')!r} is external; a sensitive context may not "
            "be routed to a hosted provider"
        )


def _rank(model: Mapping[str, Any]) -> tuple:
    """Sort key: local before external, then cheapest, then highest quality, then
    lowest latency. Missing metadata sorts last within its tier so a fully
    specified model is preferred over an under-described one."""
    kind_rank = 0 if _kind(model) == "local" else 1
    cost = model.get("cost")
    cost_rank = float(cost) if cost is not None else float("inf")
    quality = model.get("quality")
    # Negate quality so higher quality sorts first under ascending order.
    quality_rank = -float(quality) if quality is not None else float("inf")
    latency = model.get("latency_ms")
    latency_rank = float(latency) if latency is not None else float("inf")
    return (kind_rank, cost_rank, quality_rank, latency_rank)


def auto_select(
    models: Sequence[Mapping[str, Any]],
    *,
    sensitive: bool = False,
    usable_statuses: frozenset[str] = frozenset({"available", "installed"}),
) -> Mapping[str, Any] | None:
    """Choose the best usable model, preferring local for cost.

    Only models whose ``status`` is in ``usable_statuses`` are eligible — a model
    still ``downloading`` or in ``error`` is never auto-selected. When
    ``sensitive`` is set, external models are excluded entirely (the guard,
    applied as a filter here so the pool never contains a disallowed model).
    Returns ``None`` when nothing is eligible."""
    candidates = [
        m for m in models if str(m.get("status") or "") in usable_statuses
    ]
    if sensitive:
        candidates = [m for m in candidates if _kind(m) == "local"]
    if not candidates:
        return None
    return min(candidates, key=_rank)
