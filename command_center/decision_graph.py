"""Decision-memory graph — a semantic graph of decisions, errors, dependencies
and effects, queryable for the path a past incident actually took to failure
(VOYN-MIN-GRAPH-SQL).

Deliberately in-memory only: no database driver, no file on disk, no growing
state anything else depends on. AICC's anti-engine-growth architecture fitness
gate (ADR-0008, `docs/AIOS_BOUNDARY.md`) mechanically forbids a new module in
this repository from owning a persistence engine — "new engine capabilities
are prohibited in AI Command Center", enforced by
`tests/architecture/test_aios_boundary_fitness.py` classifying any file that
imports a DB driver or writes durably to disk as a new `memory` engine. A
one-off analysis graph, rebuilt fresh by whichever caller wants one (see
`command_center/decision_graph_incidents.py` for the seeded incident data),
needs none of that.

A node is one of four kinds (`NODE_TYPES`): a `decision` made, an `error`
introduced or triggered, a `dependency` the failure ran through, or an
`effect` observed afterwards — `is_failure=True` marks an effect as the
incident's actual failure, the target `path_to_failure` searches for. Edges
are directed in the direction of causal flow (`from_id` contributes to
`to_id`) and typed (`RELATIONS`): `causes`, `depends_on`, `leads_to` describe
how a decision, dependency or error fed the next step toward failure;
`mitigates` points the other way in time — from a later corrective decision
back at the effect it addresses — and is deliberately excluded from
`path_to_failure`'s traversal.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field

NODE_TYPES: frozenset[str] = frozenset({"decision", "error", "dependency", "effect"})
RELATIONS: frozenset[str] = frozenset({"causes", "depends_on", "mitigates", "leads_to"})


class DecisionGraphError(Exception):
    pass


class InvalidValue(DecisionGraphError):
    pass


class NodeNotFound(DecisionGraphError):
    pass


@dataclass
class DecisionGraph:
    """An in-memory node/edge set. Construct with `new_graph()`."""

    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)


def new_graph() -> DecisionGraph:
    return DecisionGraph()


def add_node(
    graph: DecisionGraph,
    *,
    node_type: str,
    title: str,
    detail: str | None = None,
    incident_ref: str | None = None,
    is_failure: bool = False,
    occurred_at: str | None = None,
    node_id: str | None = None,
) -> dict:
    if node_type not in NODE_TYPES:
        raise InvalidValue(f"unknown node_type {node_type!r}; valid: {sorted(NODE_TYPES)}")
    if not title.strip():
        raise InvalidValue("title must not be blank")
    if is_failure and node_type != "effect":
        raise InvalidValue("is_failure is only meaningful on an 'effect' node")

    nid = node_id or str(uuid.uuid4())
    node = {
        "id": nid,
        "node_type": node_type,
        "title": title.strip(),
        "detail": detail,
        "incident_ref": incident_ref,
        "is_failure": bool(is_failure),
        "occurred_at": occurred_at,
    }
    graph.nodes[nid] = node
    return node


def add_edge(
    graph: DecisionGraph,
    *,
    from_id: str,
    to_id: str,
    relation: str,
    detail: str | None = None,
) -> dict:
    if relation not in RELATIONS:
        raise InvalidValue(f"unknown relation {relation!r}; valid: {sorted(RELATIONS)}")
    if from_id == to_id:
        raise InvalidValue("an edge cannot connect a node to itself")
    for nid in (from_id, to_id):
        if nid not in graph.nodes:
            raise NodeNotFound(nid)
    if any(
        e["from_id"] == from_id and e["to_id"] == to_id and e["relation"] == relation
        for e in graph.edges
    ):
        raise InvalidValue(f"duplicate edge {from_id}->{to_id} ({relation})")

    edge = {"from_id": from_id, "to_id": to_id, "relation": relation, "detail": detail}
    graph.edges.append(edge)
    return edge


def get_node(graph: DecisionGraph, node_id: str) -> dict:
    try:
        return graph.nodes[node_id]
    except KeyError:
        raise NodeNotFound(node_id) from None


def list_nodes(
    graph: DecisionGraph, *, node_type: str | None = None, incident_ref: str | None = None
) -> list[dict]:
    nodes = graph.nodes.values()
    if node_type is not None:
        nodes = (n for n in nodes if n["node_type"] == node_type)
    if incident_ref is not None:
        nodes = (n for n in nodes if n["incident_ref"] == incident_ref)
    return list(nodes)


def list_edges(graph: DecisionGraph, *, incident_ref: str | None = None) -> list[dict]:
    if incident_ref is None:
        return list(graph.edges)
    return [e for e in graph.edges if graph.nodes[e["from_id"]]["incident_ref"] == incident_ref]


def path_to_failure(graph: DecisionGraph, from_id: str) -> list[dict] | None:
    """The shortest node chain from `from_id` to the nearest reachable
    ``is_failure`` effect, or ``None`` if no such chain exists.

    Returned as ordered node dicts, `from_id` first and the failure node last
    — the walk a past incident actually took, not just the fact that it
    failed. Breadth-first with one shared `visited` set: the first time a
    failure node is popped is necessarily via a shortest path to it, and the
    shared set makes cycles a non-issue rather than something to guard
    against.

    Traversal follows `causes`/`depends_on`/`leads_to` edges only. `mitigates`
    is excluded on purpose: it points from a *later* corrective decision back
    at the effect it addresses, and if the walk followed it, that decision
    would show up as if it had been a step on the road to the very failure it
    was made to fix.
    """
    if from_id not in graph.nodes:
        raise NodeNotFound(from_id)

    adjacency: dict[str, list[str]] = {}
    for e in graph.edges:
        if e["relation"] == "mitigates":
            continue
        adjacency.setdefault(e["from_id"], []).append(e["to_id"])

    visited = {from_id}
    queue: deque[list[str]] = deque([[from_id]])
    while queue:
        path = queue.popleft()
        if graph.nodes[path[-1]]["is_failure"]:
            return [graph.nodes[nid] for nid in path]
        for nxt in adjacency.get(path[-1], ()):
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append([*path, nxt])
    return None


def critical_edges(graph: DecisionGraph) -> set[tuple[str, str]]:
    """`(from_id, to_id)` pairs that lie on *some* node's shortest path to
    failure — the edges a renderer highlights as the incident's real
    path-to-failure, as opposed to every edge in the graph.
    """
    edges: set[tuple[str, str]] = set()
    for nid in graph.nodes:
        chain = path_to_failure(graph, nid)
        if not chain:
            continue
        for a, b in zip(chain, chain[1:]):
            edges.add((a["id"], b["id"]))
    return edges
