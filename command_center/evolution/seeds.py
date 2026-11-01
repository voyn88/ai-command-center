"""Hand-authored generation-0 config-agents to bootstrap a population.

One per fleet executor actually proven runnable
(`orchestrator.routing.ROUTING_MATRIX`'s `implementation` cascade), each
starting from the same baseline knobs (`orchestrator.planner.PlanLimits`'
defaults for `timeout_seconds`/`wip_limit`, 3 attempts) so the only initial
difference between them is which executor they pin -- the population's first
generation should measure *that* difference before anything else evolves.
"""

from __future__ import annotations

from command_center.evolution.types import ConfigAgent

DEFAULT_SEED_CONFIG_AGENTS: tuple[ConfigAgent, ...] = (
    ConfigAgent(
        config_id="claude-baseline",
        genes={"executor": "claude", "max_attempts": 3, "timeout_seconds": 900, "wip_limit": 4},
    ),
    ConfigAgent(
        config_id="codex-baseline",
        genes={"executor": "codex", "max_attempts": 3, "timeout_seconds": 900, "wip_limit": 4},
    ),
    ConfigAgent(
        config_id="copilot-baseline",
        genes={"executor": "copilot", "max_attempts": 3, "timeout_seconds": 900, "wip_limit": 4},
    ),
)
