#!/usr/bin/env python3
"""Build and render the decision-memory graph (VOYN-MIN-GRAPH-SQL).

Seeds a fresh, in-memory graph from `command_center.decision_graph_incidents`
(six real incidents mined from this repository's own history), prints each
incident's path-to-failure chain, and renders the whole graph to one SVG.

Usage:
    python3 scripts/render_decision_graph.py [--out PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from command_center import decision_graph as graph_module  # noqa: E402
from command_center import decision_graph_render as render  # noqa: E402
from command_center.decision_graph_incidents import build_graph  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("docs/operations/decision_memory_graph.svg")
    )
    args = parser.parse_args()

    graph = build_graph()

    incident_refs = sorted({n["incident_ref"] for n in graph.nodes.values()})
    for ref in incident_refs:
        roots = graph_module.list_nodes(graph, node_type="decision", incident_ref=ref)
        for root in roots:
            chain = graph_module.path_to_failure(graph, root["id"])
            if chain is None:
                continue
            print(f"[{ref}]")
            print("  " + " -> ".join(n["title"] for n in chain))

    out = render.render_to_file(graph, args.out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
