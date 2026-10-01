"""Agent Tuning Policy engine — UI-configurable metric weight, fallback and
SLA policies for the scheduler, evaluated at runtime with zero code changes.

The product requirement (VOYN-MIN-AGT-TUNING) is that an operator can add a
*new* policy through the UI and have it take effect on the very next
scheduling tick — no code edit, no redeploy. This module is the data layer
that makes that true: policies live in a small SQLite store (same pattern as
`rule_engine.py`'s AML rules), are matched against a task's ``task_type`` and
``priority`` at read time, and are applied to the scheduler's pure,
already-existing `AgentRegistry` / `WorkItem` primitives via
`apply_agent_weights` and `resolve_effective_policy` — neither of which
mutates scheduler.py.

A policy row carries three independent knobs, each optional:

* ``fallback_agents`` — an explicit ordered agent-id list. The first agent is
  the primary choice, the rest are the fallback chain if it is unavailable or
  fails. This is translated into descending integer weights so it composes
  with the scheduler's existing `AgentSpec.weight` tie-break rule without any
  new scheduler concept.
* ``agent_weights`` — explicit ``{agent_id: weight}`` overrides, applied on
  top of (and taking precedence over) the weights implied by
  ``fallback_agents``.
* ``sla_seconds`` — the SLA for matching work, consumed by
  `sla_seconds_for` and fed into `scheduler.WorkItem.sla_seconds`.

Matching is by ``(task_type, priority)`` where either may be ``None``/``"*"``
to mean "any". The most specific enabled policy wins; ties break on the most
recently updated policy so an operator's latest edit always governs.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from command_center import storage
from command_center.runtime import scheduler as scheduler_mod

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1

ANY = "*"  # wildcard match for task_type / priority


class AgentPolicyError(Exception):
    pass


class PolicyNotFound(AgentPolicyError):
    pass


class InvalidPolicy(AgentPolicyError):
    pass


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "agent_tuning_policies.db"


@contextmanager
def _db(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS agent_policies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                task_type TEXT NOT NULL DEFAULT '*',
                priority TEXT NOT NULL DEFAULT '*',
                agent_weights TEXT,
                fallback_agents TEXT,
                sla_seconds REAL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            );

            PRAGMA user_version = 1;
            COMMIT;
            """
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _validate(
    *,
    name: str,
    task_type: str,
    priority: str,
    agent_weights: dict[str, int] | None,
    fallback_agents: list[str] | None,
    sla_seconds: float | None,
) -> None:
    if not name or not name.strip():
        raise InvalidPolicy("policy name must be non-empty")
    if not task_type:
        raise InvalidPolicy("task_type must be non-empty (use '*' for any)")
    if not priority:
        raise InvalidPolicy("priority must be non-empty (use '*' for any)")
    if priority != ANY and priority not in _priorities():
        raise InvalidPolicy(f"unknown priority {priority!r}; valid: {_priorities()} or {ANY!r}")
    if agent_weights is not None:
        for agent_id, weight in agent_weights.items():
            if not isinstance(weight, int) or isinstance(weight, bool):
                raise InvalidPolicy(f"agent_weights[{agent_id!r}] must be an int, got {weight!r}")
    if fallback_agents is not None:
        if not fallback_agents:
            raise InvalidPolicy("fallback_agents, if given, must be non-empty")
        if len(fallback_agents) != len(set(fallback_agents)):
            raise InvalidPolicy("fallback_agents must not contain duplicate agent ids")
    if sla_seconds is not None and sla_seconds <= 0:
        raise InvalidPolicy(f"sla_seconds must be positive, got {sla_seconds!r}")
    if agent_weights is None and fallback_agents is None and sla_seconds is None:
        raise InvalidPolicy("a policy must set at least one of agent_weights, fallback_agents, sla_seconds")


def _priorities() -> tuple[str, ...]:
    from command_center import models

    return tuple(models.TASK_PRIORITIES)


def _row_to_policy(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["agent_weights"] = json.loads(d["agent_weights"]) if d.get("agent_weights") else None
    d["fallback_agents"] = json.loads(d["fallback_agents"]) if d.get("fallback_agents") else None
    d["enabled"] = bool(d["enabled"])
    return d


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_policy(
    db_path: Path,
    *,
    name: str,
    description: str | None = None,
    task_type: str = ANY,
    priority: str = ANY,
    agent_weights: dict[str, int] | None = None,
    fallback_agents: list[str] | None = None,
    sla_seconds: float | None = None,
) -> dict:
    """Create and immediately-enable a new tuning policy. This is the whole
    "1 new policy is deployed without a code deploy" acceptance path: the row
    lands in the SQLite store and the very next `resolve_effective_policy` /
    `apply_agent_weights` / `sla_seconds_for` call picks it up."""
    _validate(
        name=name,
        task_type=task_type,
        priority=priority,
        agent_weights=agent_weights,
        fallback_agents=fallback_agents,
        sla_seconds=sla_seconds,
    )
    policy_id = str(uuid.uuid4())
    now = _utcnow()
    with _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO agent_policies(
                id, name, description, task_type, priority,
                agent_weights, fallback_agents, sla_seconds, enabled,
                created_at, updated_at, schema_version
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)
            """,
            (
                policy_id,
                name,
                description,
                task_type,
                priority,
                json.dumps(agent_weights) if agent_weights else None,
                json.dumps(fallback_agents) if fallback_agents else None,
                sla_seconds,
                now,
                now,
                SCHEMA_VERSION,
            ),
        )
        row = conn.execute("SELECT * FROM agent_policies WHERE id=?", (policy_id,)).fetchone()
    return _row_to_policy(row)


def get_policy(db_path: Path, policy_id: str) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM agent_policies WHERE id=?", (policy_id,)).fetchone()
    if row is None:
        raise PolicyNotFound(policy_id)
    return _row_to_policy(row)


def list_policies(
    db_path: Path, *, enabled_only: bool = False, task_type: str | None = None
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if enabled_only:
        clauses.append("enabled = 1")
    if task_type is not None:
        clauses.append("task_type IN (?, ?)")
        params.extend([task_type, ANY])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM agent_policies {where} ORDER BY updated_at DESC", params
        ).fetchall()
    return [_row_to_policy(r) for r in rows]


def toggle_policy(db_path: Path, policy_id: str, *, enabled: bool) -> dict:
    with _db(db_path) as conn:
        cur = conn.execute(
            "UPDATE agent_policies SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, _utcnow(), policy_id),
        )
        if cur.rowcount == 0:
            raise PolicyNotFound(policy_id)
        row = conn.execute("SELECT * FROM agent_policies WHERE id=?", (policy_id,)).fetchone()
    return _row_to_policy(row)


def delete_policy(db_path: Path, policy_id: str) -> None:
    with _db(db_path) as conn:
        cur = conn.execute("DELETE FROM agent_policies WHERE id=?", (policy_id,))
        if cur.rowcount == 0:
            raise PolicyNotFound(policy_id)


# ---------------------------------------------------------------------------
# Applying policies to the (unchanged) scheduler primitives
# ---------------------------------------------------------------------------


def _specificity(policy: dict) -> int:
    """0 = both wildcards, 1 = one specific, 2 = both specific — used so a
    policy scoped to an exact (task_type, priority) always beats a broader
    one, regardless of insertion order."""
    return (policy["task_type"] != ANY) + (policy["priority"] != ANY)


def resolve_effective_policy(
    db_path: Path, *, task_type: str | None = None, priority: str | None = None
) -> dict | None:
    """The single enabled policy that governs a given ``(task_type,
    priority)`` combination, or ``None`` if none match. Most-specific wins;
    ties break on most-recently updated (rows already arrive sorted that way
    from `list_policies`)."""
    task_type = task_type or ANY
    priority = priority or ANY
    candidates = [
        p
        for p in list_policies(db_path, enabled_only=True)
        if p["task_type"] in (ANY, task_type) and p["priority"] in (ANY, priority)
    ]
    if not candidates:
        return None
    candidates.sort(key=_specificity, reverse=True)
    return candidates[0]


def sla_seconds_for(
    db_path: Path, *, task_type: str | None = None, priority: str | None = None
) -> float | None:
    """SLA (seconds) an operator has configured for this kind of work, or
    ``None`` when no matching policy sets one — the caller then leaves
    `WorkItem.sla_seconds` unset, exactly as before this policy layer
    existed."""
    policy = resolve_effective_policy(db_path, task_type=task_type, priority=priority)
    if policy is None:
        return None
    return policy.get("sla_seconds")


def effective_weights(
    db_path: Path, *, task_type: str | None = None, priority: str | None = None
) -> dict[str, int]:
    """The ``{agent_id: weight}`` map implied by the single matching policy:
    `fallback_agents` positions become descending weights (first = highest),
    then `agent_weights` overrides on top."""
    policy = resolve_effective_policy(db_path, task_type=task_type, priority=priority)
    if policy is None:
        return {}
    weights: dict[str, int] = {}
    fallback = policy.get("fallback_agents") or []
    n = len(fallback)
    for index, agent_id in enumerate(fallback):
        weights[agent_id] = n - index
    weights.update(policy.get("agent_weights") or {})
    return weights


def apply_agent_weights(
    registry: scheduler_mod.AgentRegistry, weights: dict[str, int]
) -> scheduler_mod.AgentRegistry:
    """Return a new `AgentRegistry` with `weights` overlaid on `registry`'s
    agents (agents absent from `weights` keep their existing weight). Pure —
    `scheduler.py` itself is never modified; this only recomposes its
    existing frozen `AgentSpec` dataclass."""
    if not weights:
        return registry
    updated = [
        replace(spec, weight=weights[spec.agent_id]) if spec.agent_id in weights else spec
        for spec in registry.all()
    ]
    return scheduler_mod.AgentRegistry(updated)


def tuned_registry(
    registry: scheduler_mod.AgentRegistry,
    db_path: Path | None = None,
    *,
    task_type: str | None = None,
    priority: str | None = None,
) -> scheduler_mod.AgentRegistry:
    """Convenience: `effective_weights` + `apply_agent_weights` in one call,
    against the default policy store location."""
    path = db_path or resolve_db_path()
    weights = effective_weights(path, task_type=task_type, priority=priority)
    return apply_agent_weights(registry, weights)
