"""Tests for command_center.decision_graph (VOYN-MIN-GRAPH-SQL)."""

from __future__ import annotations

import pytest

from command_center.decision_graph import (
    DecisionGraph,
    InvalidValue,
    NodeNotFound,
    add_edge,
    add_node,
    critical_edges,
    get_node,
    list_edges,
    list_nodes,
    new_graph,
    path_to_failure,
)


@pytest.fixture()
def graph() -> DecisionGraph:
    return new_graph()


def _chain(graph: DecisionGraph, *, incident_ref: str = "INC-1") -> dict[str, dict]:
    """decision --leads_to--> dependency --causes--> error --causes--> effect(failure)."""
    d = add_node(graph, node_type="decision", title="Decision", incident_ref=incident_ref)
    dep = add_node(graph, node_type="dependency", title="Dependency", incident_ref=incident_ref)
    e = add_node(graph, node_type="error", title="Error", incident_ref=incident_ref)
    eff = add_node(
        graph, node_type="effect", title="Failure", incident_ref=incident_ref, is_failure=True
    )
    add_edge(graph, from_id=d["id"], to_id=dep["id"], relation="leads_to")
    add_edge(graph, from_id=dep["id"], to_id=e["id"], relation="causes")
    add_edge(graph, from_id=e["id"], to_id=eff["id"], relation="causes")
    return {"decision": d, "dependency": dep, "error": e, "effect": eff}


# ---------------------------------------------------------------------------
# add_node / add_edge validation
# ---------------------------------------------------------------------------


def test_add_node_rejects_unknown_type(graph):
    with pytest.raises(InvalidValue):
        add_node(graph, node_type="bogus", title="x")


def test_add_node_rejects_blank_title(graph):
    with pytest.raises(InvalidValue):
        add_node(graph, node_type="decision", title="   ")


def test_is_failure_only_valid_on_effect(graph):
    with pytest.raises(InvalidValue):
        add_node(graph, node_type="error", title="x", is_failure=True)
    # Legal on effect.
    node = add_node(graph, node_type="effect", title="x", is_failure=True)
    assert node["is_failure"] is True


def test_add_edge_rejects_unknown_relation(graph):
    a = add_node(graph, node_type="decision", title="A")
    b = add_node(graph, node_type="error", title="B")
    with pytest.raises(InvalidValue):
        add_edge(graph, from_id=a["id"], to_id=b["id"], relation="bogus")


def test_add_edge_rejects_self_loop(graph):
    a = add_node(graph, node_type="decision", title="A")
    with pytest.raises(InvalidValue):
        add_edge(graph, from_id=a["id"], to_id=a["id"], relation="causes")


def test_add_edge_requires_existing_nodes(graph):
    a = add_node(graph, node_type="decision", title="A")
    with pytest.raises(NodeNotFound):
        add_edge(graph, from_id=a["id"], to_id="missing", relation="causes")


def test_add_edge_rejects_duplicate(graph):
    a = add_node(graph, node_type="decision", title="A")
    b = add_node(graph, node_type="error", title="B")
    add_edge(graph, from_id=a["id"], to_id=b["id"], relation="causes")
    with pytest.raises(InvalidValue):
        add_edge(graph, from_id=a["id"], to_id=b["id"], relation="causes")


def test_get_node_missing_raises(graph):
    with pytest.raises(NodeNotFound):
        get_node(graph, "missing")


# ---------------------------------------------------------------------------
# list_nodes / list_edges
# ---------------------------------------------------------------------------


def test_list_nodes_filters_by_type_and_incident(graph):
    _chain(graph, incident_ref="INC-1")
    _chain(graph, incident_ref="INC-2")

    all_nodes = list_nodes(graph)
    assert len(all_nodes) == 8

    inc1_only = list_nodes(graph, incident_ref="INC-1")
    assert len(inc1_only) == 4
    assert {n["incident_ref"] for n in inc1_only} == {"INC-1"}

    decisions = list_nodes(graph, node_type="decision")
    assert len(decisions) == 2
    assert all(n["node_type"] == "decision" for n in decisions)


def test_list_edges_filters_by_incident(graph):
    _chain(graph, incident_ref="INC-1")
    _chain(graph, incident_ref="INC-2")

    assert len(list_edges(graph)) == 6
    assert len(list_edges(graph, incident_ref="INC-1")) == 3


# ---------------------------------------------------------------------------
# path_to_failure
# ---------------------------------------------------------------------------


def test_path_to_failure_returns_the_full_chain(graph):
    nodes = _chain(graph)
    chain = path_to_failure(graph, nodes["decision"]["id"])
    assert chain is not None
    assert [n["title"] for n in chain] == ["Decision", "Dependency", "Error", "Failure"]


def test_path_to_failure_none_when_unreachable(graph):
    a = add_node(graph, node_type="decision", title="Dead end")
    assert path_to_failure(graph, a["id"]) is None


def test_path_to_failure_missing_start_raises(graph):
    with pytest.raises(NodeNotFound):
        path_to_failure(graph, "missing")


def test_path_to_failure_is_cycle_safe(graph):
    a = add_node(graph, node_type="decision", title="A")
    b = add_node(graph, node_type="error", title="B")
    add_edge(graph, from_id=a["id"], to_id=b["id"], relation="causes")
    add_edge(graph, from_id=b["id"], to_id=a["id"], relation="causes")
    # No failure node reachable at all -- must terminate, not loop forever.
    assert path_to_failure(graph, a["id"]) is None


def test_path_to_failure_finds_shortest_route_around_a_cycle(graph):
    a = add_node(graph, node_type="decision", title="A")
    b = add_node(graph, node_type="error", title="B")
    eff = add_node(graph, node_type="effect", title="Failure", is_failure=True)
    add_edge(graph, from_id=a["id"], to_id=b["id"], relation="causes")
    add_edge(graph, from_id=b["id"], to_id=a["id"], relation="causes")  # cycle back to A
    add_edge(graph, from_id=a["id"], to_id=eff["id"], relation="causes")  # direct shortcut

    chain = path_to_failure(graph, a["id"])
    assert [n["title"] for n in chain] == ["A", "Failure"]


def test_path_to_failure_excludes_mitigates_edges(graph):
    """A `mitigates` edge from a later decision into the failure must not make
    that decision look like it was on the road to its own fix."""
    eff = add_node(graph, node_type="effect", title="Failure", is_failure=True)
    fix = add_node(graph, node_type="decision", title="Fix")
    add_edge(graph, from_id=fix["id"], to_id=eff["id"], relation="mitigates")

    assert path_to_failure(graph, fix["id"]) is None


# ---------------------------------------------------------------------------
# critical_edges
# ---------------------------------------------------------------------------


def test_critical_edges_covers_the_failure_chain_only(graph):
    nodes = _chain(graph)
    fix = add_node(graph, node_type="decision", title="Fix", incident_ref="INC-1")
    add_edge(graph, from_id=fix["id"], to_id=nodes["effect"]["id"], relation="mitigates")

    edges = critical_edges(graph)
    assert edges == {
        (nodes["decision"]["id"], nodes["dependency"]["id"]),
        (nodes["dependency"]["id"], nodes["error"]["id"]),
        (nodes["error"]["id"], nodes["effect"]["id"]),
    }
    assert (fix["id"], nodes["effect"]["id"]) not in edges
