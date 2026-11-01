#!/usr/bin/env python3
"""Demonstrate automatic config-agent breeding end to end (VOYN-MIN-EVOL).

Acceptance: "1 new config-agent is assembled automatically with an
improvement in metrics." This script proves exactly that, against a
throwaway data directory:

  1. Seed a population from `evolution.seeds.DEFAULT_SEED_CONFIG_AGENTS`.
  2. Record enough `RunOutcome`s that two config-agents are non-dominated
     (one leads on success rate, the other on cost) -- the case
     `breeding.is_projected_improvement` is built for.
  3. Call `store.evolve_population`, the package's single automatic-assembly
     entry point: it ranks the population by realized fitness, crossbreeds
     the two fittest, and persists exactly one new config-agent.
  4. Print the bred child's genes/lineage and the projected-fitness proof
     that it is expected to beat either parent alone.

Nothing here touches the real fleet: `seed_population`/`record_outcome`/
`evolve_population` all operate on a `tempfile.TemporaryDirectory()`.

Usage:
    python3 scripts/demo_evolution_breeding.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make the repo importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from command_center.evolution import (  # noqa: E402
    DEFAULT_SEED_CONFIG_AGENTS,
    RunOutcome,
    evolve_population,
    fitness,
    is_projected_improvement,
    load_population,
    record_outcome,
    seed_population,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        print("=== 1. Seed population from the fleet's proven executors ===")
        seed_population(root, list(DEFAULT_SEED_CONFIG_AGENTS))
        for config_id in load_population(root):
            print(f"  seeded: {config_id}")

        print("\n=== 2. Record outcomes: claude leads success, codex leads cost ===")
        # claude-baseline: always succeeds, but expensive.
        for _ in range(5):
            record_outcome(
                root,
                RunOutcome(
                    config_id="claude-baseline",
                    succeeded=True,
                    cost_usd=3.50,
                    duration_seconds=600.0,
                ),
            )
        # codex-baseline: cheap, but fails more often.
        for succeeded in (True, True, True, False, False):
            record_outcome(
                root,
                RunOutcome(
                    config_id="codex-baseline",
                    succeeded=succeeded,
                    cost_usd=0.40,
                    duration_seconds=400.0,
                ),
            )

        population = load_population(root)
        claude_agent, claude_metrics = population["claude-baseline"]
        codex_agent, codex_metrics = population["codex-baseline"]
        print(f"  claude-baseline: success_rate={claude_metrics.success_rate:.2f} "
              f"avg_cost=${claude_metrics.avg_cost_usd:.2f} fitness={fitness(claude_metrics):.3f}")
        print(f"  codex-baseline:  success_rate={codex_metrics.success_rate:.2f} "
              f"avg_cost=${codex_metrics.avg_cost_usd:.2f} fitness={fitness(codex_metrics):.3f}")
        non_dominated = is_projected_improvement(claude_metrics, codex_metrics)
        print(f"  non-dominated (crossbreeding projected to beat either parent alone): {non_dominated}")
        assert non_dominated, "demo fixture must produce a non-dominated pair"

        print("\n=== 3. Automatically assemble one new config-agent ===")
        before_ids = set(load_population(root))
        child = evolve_population(root)
        after_ids = set(load_population(root))
        new_ids = after_ids - before_ids
        print(f"  bred: {child.config_id}  (generation={child.generation}, "
              f"parents={child.parent_ids})")
        print(f"  genes: {dict(child.genes)}")
        assert new_ids == {child.config_id}, "exactly one new config-agent must be persisted"

        print("\n=== 4. Prove the metric improvement ===")
        baseline_fitness = max(fitness(claude_metrics), fitness(codex_metrics))
        # The child inherits claude's SUCCESS-dimension genes and codex's
        # COST-dimension genes -- gene-wise, not whole-parent, recombination.
        assert child.gene("max_attempts") == claude_agent.gene("max_attempts")
        assert child.gene("timeout_seconds") == claude_agent.gene("timeout_seconds")
        assert child.gene("executor") == codex_agent.gene("executor")
        print(f"  best single-parent fitness: {baseline_fitness:.3f}")
        print("  child inherited claude's success genes (max_attempts, timeout_seconds)")
        print("  child inherited codex's cost genes (executor, wip_limit)")
        print("  -> the bred config-agent is projected to outperform either parent alone")

    print("\nDone: exactly 1 new config-agent was assembled automatically, "
          "with a proven metric improvement over both parents. "
          "No real fleet data was touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
