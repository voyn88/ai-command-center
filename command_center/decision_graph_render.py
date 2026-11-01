"""Render the decision-memory graph to SVG (VOYN-MIN-GRAPH-SQL).

No plotting library is a dependency of this repository (no `graphviz`,
`pydot` or `networkx`, and no system `dot` binary either — checked before
writing this), so the layout and the SVG itself are both hand-rolled here
rather than adding one for a single diagram. The graph is small and shaped
predictably (one causal chain per incident, plus a detached corrective
decision), which is exactly the case a bespoke layered layout handles well.

Layout: nodes are grouped by `incident_ref`. Within a group, `causes` /
`depends_on` / `leads_to` edges form a DAG; each node's column is its longest
path from a source (Kahn's algorithm, layer = 1 + max(layer of causal
predecessors)). A node with no causal edges at all — the corrective decision,
which only carries a `mitigates` edge — is drawn on its own row under the
node it mitigates, connected by a dashed arrow, so it reads as "addressed
this" rather than as a step on the road to failure.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from xml.sax.saxutils import escape

from command_center import decision_graph as graph_module

_COL_WIDTH = 260
_BOX_WIDTH = 220
_BOX_HEIGHT = 96
_ROW_GAP = 60
_INCIDENT_GAP = 70
_MARGIN = 40
_TITLE_HEIGHT = 90
_LEGEND_HEIGHT = 70
_LINE_HEIGHT = 14
_WRAP_CHARS = 28
_MAX_LINES = 6

_TYPE_STYLE = {
    "decision": dict(fill="#EBF8FF", stroke="#2B6CB0", text="#1A365D"),
    "dependency": dict(fill="#FAF5FF", stroke="#6B46C1", text="#322659"),
    "error": dict(fill="#FFFAF0", stroke="#C05621", text="#652B06"),
    "effect": dict(fill="#F0FFF4", stroke="#2F855A", text="#1C4532"),
}
_FAILURE_STYLE = dict(fill="#FFF5F5", stroke="#C53030", text="#63171B")

_EDGE_CRITICAL = dict(stroke="#C53030", width=3, dash="none", marker="arrow-critical")
_EDGE_NORMAL = dict(stroke="#A0AEC0", width=1.5, dash="none", marker="arrow-normal")
_EDGE_MITIGATES = dict(stroke="#2F855A", width=2, dash="6,4", marker="arrow-mitigates")


def _wrap(title: str) -> list[str]:
    lines = textwrap.wrap(title, width=_WRAP_CHARS) or [""]
    if len(lines) > _MAX_LINES:
        lines = lines[: _MAX_LINES - 1] + [lines[_MAX_LINES - 1][: _WRAP_CHARS - 1] + "…"]
    return lines


def _layered_layout(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[int, int]]:
    """`node_id -> (col, row)` for one incident's nodes.

    `col` is the causal layer (longest path from a source over non-mitigates
    edges); detached nodes (no causal edge at all) share `col` with the node
    they mitigate, one `row` below it.
    """
    node_ids = [n["id"] for n in nodes]
    causal = [e for e in edges if e["relation"] != "mitigates"]
    preds: dict[str, list[str]] = {nid: [] for nid in node_ids}
    succs: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in causal:
        if e["from_id"] in preds and e["to_id"] in preds:
            preds[e["to_id"]].append(e["from_id"])
            succs[e["from_id"]].append(e["to_id"])

    layer: dict[str, int] = {}
    remaining = dict(preds)
    frontier = [nid for nid, p in remaining.items() if not p]
    seen_causal = set(frontier)
    while frontier:
        nxt: list[str] = []
        for nid in frontier:
            layer[nid] = layer.get(nid, 0)
        for nid in frontier:
            for s in succs[nid]:
                layer[s] = max(layer.get(s, 0), layer[nid] + 1)
                if s not in seen_causal:
                    seen_causal.add(s)
                    nxt.append(s)
        frontier = nxt

    detached = [nid for nid in node_ids if not preds[nid] and not succs[nid]]
    mitigate_target = {
        e["from_id"]: e["to_id"] for e in edges if e["relation"] == "mitigates"
    }

    positions: dict[str, tuple[int, int]] = {}
    for nid in node_ids:
        if nid in layer:
            positions[nid] = (layer[nid], 0)
    for nid in detached:
        target = mitigate_target.get(nid)
        col = layer.get(target, 0) if target else 0
        positions[nid] = (col, 1)
    # Anything left over (shouldn't happen for the seeded shape, but keep the
    # renderer from crashing on an unexpected graph shape) gets its own layer.
    fallback_col = (max((c for c, _ in positions.values()), default=-1)) + 1
    for nid in node_ids:
        if nid not in positions:
            positions[nid] = (fallback_col, 0)
            fallback_col += 1
    return positions


def _node_style(node: dict) -> dict:
    if node["node_type"] == "effect" and node["is_failure"]:
        return _FAILURE_STYLE
    return _TYPE_STYLE[node["node_type"]]


def _box(x: int, y: int, node: dict) -> str:
    style = _node_style(node)
    lines = _wrap(node["title"])
    text_h = len(lines) * _LINE_HEIGHT
    ty0 = y + (_BOX_HEIGHT - text_h) / 2 + _LINE_HEIGHT * 0.8
    border_width = 3 if node.get("is_failure") else 1.6
    parts = [
        f'<rect x="{x}" y="{y}" width="{_BOX_WIDTH}" height="{_BOX_HEIGHT}" rx="10" '
        f'fill="{style["fill"]}" stroke="{style["stroke"]}" stroke-width="{border_width}"/>',
        f'<text x="{x + _BOX_WIDTH / 2}" y="{y + 16}" text-anchor="middle" '
        f'font-family="Helvetica, Arial, sans-serif" font-size="9" font-weight="700" '
        f'letter-spacing="0.05em" fill="{style["stroke"]}">'
        f'{escape(node["node_type"].upper())}{" • FAILURE" if node.get("is_failure") else ""}</text>',
    ]
    for i, line in enumerate(lines):
        parts.append(
            f'<text x="{x + _BOX_WIDTH / 2}" y="{ty0 + i * _LINE_HEIGHT}" text-anchor="middle" '
            f'font-family="Helvetica, Arial, sans-serif" font-size="11.5" fill="{style["text"]}">'
            f"{escape(line)}</text>"
        )
    return "\n".join(parts)


def _arrow(x1: float, y1: float, x2: float, y2: float, edge_style: dict) -> str:
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{edge_style["stroke"]}" stroke-width="{edge_style["width"]}" '
        f'stroke-dasharray="{edge_style["dash"]}" '
        f'marker-end="url(#{edge_style["marker"]})"/>'
    )


_MARKERS = "".join(
    f'<marker id="{style["marker"]}" viewBox="0 0 10 10" refX="9" refY="5" '
    f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{style["stroke"]}"/></marker>'
    for style in (_EDGE_CRITICAL, _EDGE_NORMAL, _EDGE_MITIGATES)
)

_LEGEND_ENTRIES = [
    ("decision", _TYPE_STYLE["decision"]),
    ("dependency", _TYPE_STYLE["dependency"]),
    ("error", _TYPE_STYLE["error"]),
    ("effect", _TYPE_STYLE["effect"]),
    ("effect (failure)", _FAILURE_STYLE),
]


def render_svg(graph: graph_module.DecisionGraph) -> str:
    """Render every node/edge in `graph` as one SVG document.

    Nodes are grouped by `incident_ref` (one row-group per incident) and laid
    out left-to-right in causal order; edges on some node's shortest
    `path_to_failure` are drawn in red, everything else in gray, and
    `mitigates` edges as a dashed green arrow into the effect they address.
    """
    nodes = graph_module.list_nodes(graph)
    edges = graph_module.list_edges(graph)
    critical = graph_module.critical_edges(graph)

    groups: dict[str, list[dict]] = {}
    for n in nodes:
        groups.setdefault(n["incident_ref"] or "(ungrouped)", []).append(n)

    max_cols = 1
    y_cursor = _MARGIN + _TITLE_HEIGHT + _LEGEND_HEIGHT
    body_parts: list[str] = []
    for incident_ref in sorted(groups):
        group_nodes = groups[incident_ref]
        group_ids = {n["id"] for n in group_nodes}
        group_edges = [e for e in edges if e["from_id"] in group_ids and e["to_id"] in group_ids]
        positions = _layered_layout(group_nodes, group_edges)
        max_cols = max(max_cols, max(c for c, _ in positions.values()) + 1)

        body_parts.append(
            f'<text x="{_MARGIN}" y="{y_cursor}" font-family="Helvetica, Arial, sans-serif" '
            f'font-size="14" font-weight="700" fill="#1A202C">{escape(incident_ref)}</text>'
        )
        row_top = y_cursor + 16
        coords: dict[str, tuple[float, float]] = {}
        for n in group_nodes:
            col, row = positions[n["id"]]
            x = _MARGIN + col * _COL_WIDTH
            y = row_top + row * (_BOX_HEIGHT + _ROW_GAP)
            coords[n["id"]] = (x, y)
            body_parts.append(_box(x, y, n))

        for e in group_edges:
            x1, y1 = coords[e["from_id"]]
            x2, y2 = coords[e["to_id"]]
            if e["relation"] == "mitigates":
                style = _EDGE_MITIGATES
                start = (x1 + _BOX_WIDTH / 2, y1)
                end = (x2 + _BOX_WIDTH / 2, y2 + _BOX_HEIGHT)
            else:
                style = _EDGE_CRITICAL if (e["from_id"], e["to_id"]) in critical else _EDGE_NORMAL
                start = (x1 + _BOX_WIDTH, y1 + _BOX_HEIGHT / 2)
                end = (x2, y2 + _BOX_HEIGHT / 2)
            body_parts.append(_arrow(*start, *end, style))

        rows_used = max(row for _, row in positions.values()) + 1
        y_cursor = row_top + rows_used * (_BOX_HEIGHT + _ROW_GAP) + _INCIDENT_GAP

    width = _MARGIN * 2 + max_cols * _COL_WIDTH
    height = y_cursor

    legend_x = _MARGIN
    legend_y = _MARGIN + _TITLE_HEIGHT - 18
    legend_parts = []
    for label, style in _LEGEND_ENTRIES:
        legend_parts.append(
            f'<rect x="{legend_x}" y="{legend_y - 11}" width="16" height="16" rx="3" '
            f'fill="{style["fill"]}" stroke="{style["stroke"]}" stroke-width="1.5"/>'
            f'<text x="{legend_x + 22}" y="{legend_y + 1}" font-family="Helvetica, Arial, sans-serif" '
            f'font-size="11" fill="#2D3748">{escape(label)}</text>'
        )
        legend_x += 16 + 10 + len(label) * 6 + 26
    legend_parts.append(
        f'<line x1="{legend_x}" y1="{legend_y - 3}" x2="{legend_x + 34}" y2="{legend_y - 3}" '
        f'stroke="{_EDGE_CRITICAL["stroke"]}" stroke-width="3" marker-end="url(#arrow-critical)"/>'
        f'<text x="{legend_x + 42}" y="{legend_y + 1}" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="11" fill="#2D3748">path to failure</text>'
    )
    legend_x += 42 + len("path to failure") * 6 + 26
    legend_parts.append(
        f'<line x1="{legend_x}" y1="{legend_y - 3}" x2="{legend_x + 34}" y2="{legend_y - 3}" '
        f'stroke="{_EDGE_MITIGATES["stroke"]}" stroke-width="2" stroke-dasharray="6,4" '
        f'marker-end="url(#arrow-mitigates)"/>'
        f'<text x="{legend_x + 42}" y="{legend_y + 1}" font-family="Helvetica, Arial, sans-serif" '
        f'font-size="11" fill="#2D3748">mitigates</text>'
    )

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif">',
        f'<defs>{_MARKERS}</defs>',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#FFFFFF"/>',
        f'<text x="{_MARGIN}" y="{_MARGIN + 20}" font-size="20" font-weight="700" '
        f'fill="#1A202C">Decision-Memory Graph — Path to Failure for Past Incidents</text>',
        f'<text x="{_MARGIN}" y="{_MARGIN + 42}" font-size="12" fill="#4A5568">'
        f"VOYN-MIN-GRAPH-SQL — semantic graph of decisions, errors, dependencies and "
        f"effects, mined from this repository's own incident history</text>",
        *legend_parts,
        *body_parts,
        "</svg>",
    ]
    return "\n".join(svg)


def render_to_file(graph: graph_module.DecisionGraph, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_svg(graph), encoding="utf-8")
    return out_path
