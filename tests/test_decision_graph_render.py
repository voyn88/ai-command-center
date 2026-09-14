"""Tests for command_center.decision_graph_render (VOYN-MIN-GRAPH-SQL)."""

from __future__ import annotations

import xml.dom.minidom as minidom

import pytest

from command_center.decision_graph import DecisionGraph, add_edge, add_node, new_graph
from command_center.decision_graph_render import render_svg, render_to_file


@pytest.fixture()
def graph() -> DecisionGraph:
    g = new_graph()
    d = add_node(g, node_type="decision", title="Decision <one>", incident_ref="INC-1")
    dep = add_node(g, node_type="dependency", title="Dependency", incident_ref="INC-1")
    e = add_node(g, node_type="error", title="Error", incident_ref="INC-1")
    eff = add_node(g, node_type="effect", title="Failure", incident_ref="INC-1", is_failure=True)
    fix = add_node(g, node_type="decision", title="Fix", incident_ref="INC-1")
    add_edge(g, from_id=d["id"], to_id=dep["id"], relation="leads_to")
    add_edge(g, from_id=dep["id"], to_id=e["id"], relation="causes")
    add_edge(g, from_id=e["id"], to_id=eff["id"], relation="causes")
    add_edge(g, from_id=fix["id"], to_id=eff["id"], relation="mitigates")
    return g


def test_render_svg_is_well_formed_xml(graph):
    svg = render_svg(graph)
    minidom.parseString(svg)  # raises on malformed XML
    assert svg.startswith("<svg")


def test_render_svg_escapes_node_titles(graph):
    svg = render_svg(graph)
    assert "<one>" not in svg
    assert "&lt;one&gt;" in svg


def test_render_svg_contains_every_node_title(graph):
    svg = render_svg(graph)
    for title in ("Dependency", "Error", "Failure", "Fix"):
        assert title in svg


def test_render_svg_highlights_the_failure_chain_and_dashes_mitigates(graph):
    svg = render_svg(graph)
    # Three causal edges on the path to failure -> red/critical stroke.
    assert svg.count('stroke="#C53030"') >= 3
    # Exactly one mitigates edge -> dashed green stroke.
    assert 'stroke-dasharray="6,4"' in svg
    assert svg.count('stroke="#2F855A"') >= 1


def test_render_svg_has_no_overlapping_node_boxes(graph):
    import re

    svg = render_svg(graph)
    boxes = [
        tuple(map(float, m))
        for m in re.findall(
            r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)" rx="10"', svg
        )
    ]
    assert len(boxes) == 5

    def overlaps(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not overlaps(boxes[i], boxes[j])


def test_render_to_file_writes_svg(graph, tmp_path):
    out = tmp_path / "nested" / "graph.svg"
    result = render_to_file(graph, out)
    assert result == out
    assert out.exists()
    minidom.parse(str(out))
