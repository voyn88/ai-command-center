# Decision-memory graph (VOYN-MIN-GRAPH-SQL)

The acceptance this closes: **one visual graph with a path-to-failure for
past incidents.** [`decision_memory_graph.svg`](decision_memory_graph.svg) in
this directory is that graph — six real incidents from this repository's own
history, each as a `decision -> dependency -> error -> effect` chain ending in
the failure it actually produced, plus the corrective decision that fixed it.

## Model

`command_center/decision_graph_store.py` is a standalone SQLite store (same
shape as `evidence_store.py` / `alert_store.py`: `init_db` + a `.db` file
under the data dir) — not part of the AICC/AIOS PostgreSQL schema in
`command_center/db/`, because this graph is a side table for post-incident
analysis, not production state anything else depends on.

- **Nodes** are one of four kinds: `decision`, `error`, `dependency`,
  `effect`. An `effect` node can carry `is_failure=1` — the terminal node a
  `path_to_failure` search is looking for.
- **Edges** are directed and typed: `causes` / `depends_on` / `leads_to`
  describe forward causal flow (`from_id` contributes to `to_id`); `mitigates`
  points the other way in time, from a later corrective decision back at the
  effect it addressed, and is excluded from path traversal so a fix doesn't
  read as a step on the road to its own failure.
- `path_to_failure(db, node_id)` finds the shortest chain from a node to the
  nearest reachable failure with one `WITH RECURSIVE` query, cycle-safe by a
  delimited-substring guard on the accumulated path.
- `critical_edges(db)` returns every edge that lies on some node's shortest
  path to failure — what the renderer highlights in red.

## Regenerating the graph

```bash
python3 scripts/seed_decision_graph_incidents.py --db /tmp/decision_graph.db
python3 scripts/render_decision_graph.py --db /tmp/decision_graph.db \
    --out docs/operations/decision_memory_graph.svg
```

`seed_decision_graph_incidents.py` seeds the six incidents named below; each
one's `incident_ref` names its source so a claim can be checked against the
actual migration comment, module docstring or commit message it came from.
`render_decision_graph.py` reads the store and writes the SVG — no plotting
dependency (`graphviz`/`pydot`/`networkx`, or a system `dot` binary) exists in
this repository, so `command_center/decision_graph_render.py` is a small
hand-rolled layered-DAG-to-SVG renderer instead of a new one.

## The six incidents

| Incident | Failure |
|---|---|
| `VOYN-W0-AICC-DEFER-AUTO-RESUME-REM` (migrations 0014, 0017) | Tasks stuck `DEFER_TO_USER` forever after the pipeline that parked them was fixed |
| `VOYN-W0-AICC-TASK-IMPORT-CONCURRENCY-FLAKE` (#507, #727) | First task id of a bulk import package silently disappears under concurrent import |
| `VOYN-W0-AICC-MIGRATOR-PASSWORD-FLAKE` | `password authentication failed for user aicc_migrator` in CI |
| `VOYN-W0-AICC-RUNS-READ-ZERO` (#733) | An empty `runtime.db` and one that errors on read were indistinguishable in the written artifact |
| `VOYN-W0-AICC-DISPATCH-PLAN-FABRICATED-SPEND-REM` (#641 rejected, #706 fix) | A fabricated budget ceiling shipped as if it were a measured spend value |
| `VOYN-W0-AICC-GITLEAKS-TEST-FIXTURE-FINGERPRINT` (#766) | Secret scan gate failed on every open PR over one synthetic test fixture |

## Extending it

Adding a seventh incident is `add_node` / `add_edge` calls, not a schema
change — see `_chain()` in `scripts/seed_decision_graph_incidents.py` for the
shape (`decision --leads_to--> dependency --causes--> error --causes-->
effect(is_failure=True)`, plus a `decision --mitigates--> effect`). Any node
or edge can be added directly through the store API without going through the
seed script; `render_decision_graph.py` picks up whatever is in the database
at render time.
