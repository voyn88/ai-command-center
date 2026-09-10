"""Persistence for the evolving config-agent population.

Single writer of `data/evolution_population.json`, following the same
atomic-replace-write-guarded-by-advisory-file-lock pattern as
`dispatch.policy_config`/`pipeline_settings` -- so a read-modify-write cycle
(record an outcome, then breed a new config-agent from the updated
population) from two sessions cannot tear the file or lose an update.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from command_center import storage
from command_center.evolution.breeding import breed_new_config_agent
from command_center.evolution.types import ConfigAgent, ConfigMetrics, RunOutcome

__all__ = [
    "population_file_path",
    "load_population",
    "seed_population",
    "record_outcome",
    "evolve_population",
]

POPULATION_FILE_NAME = "evolution_population.json"
POPULATION_LOCK_FILE_NAME = "evolution_population.lock"

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05

Population = dict[str, tuple[ConfigAgent, ConfigMetrics]]


def population_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / POPULATION_FILE_NAME


def population_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / POPULATION_LOCK_FILE_NAME


@contextlib.contextmanager
def population_lock(root: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Cross-process mutual exclusion for the population read-modify-write
    cycle -- the same OS advisory-lock primitive as
    `dispatch.policy_config.policy_lock`."""
    with storage.file_lock(
        population_lock_path(root), timeout=timeout, poll_seconds=_LOCK_POLL_SECONDS
    ):
        yield


def _decode(raw: object) -> Population:
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("config_agents")
    if not isinstance(entries, list):
        return {}
    population: Population = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        agent = ConfigAgent.from_dict(entry.get("config_agent"))
        if agent is None:
            continue
        population[agent.config_id] = (agent, ConfigMetrics.from_dict(entry.get("metrics")))
    return population


def _encode(population: Population) -> dict:
    return {
        "config_agents": [
            {"config_agent": agent.as_dict(), "metrics": metrics.as_dict()}
            for agent, metrics in population.values()
        ]
    }


def _read(root: Path) -> Population:
    return _decode(storage.read_json(population_file_path(root), {}))


def load_population(root: Path) -> Population:
    """Read the persisted population, or empty if nothing is saved yet.
    Unlocked by design (a plain read of an atomically-written file); use
    `seed_population`/`record_outcome`/`evolve_population` for anything that
    writes."""
    return _read(root)


def seed_population(root: Path, config_agents: list[ConfigAgent]) -> None:
    """Register hand-authored config-agents with zero recorded runs. A
    no-op for any id already present, so re-running a bootstrap never
    clobbers real accumulated metrics."""
    with population_lock(root):
        population = _read(root)
        for agent in config_agents:
            population.setdefault(agent.config_id, (agent, ConfigMetrics()))
        storage.atomic_write_json(population_file_path(root), _encode(population))


def record_outcome(root: Path, outcome: RunOutcome) -> ConfigMetrics:
    """Fold one more recorded run into its config-agent's metrics. Refuses
    an outcome for a config-agent id that was never seeded/bred (fail
    closed: a typo'd id would otherwise silently start a brand-new,
    disconnected lineage with no lineage at all)."""
    with population_lock(root):
        population = _read(root)
        if outcome.config_id not in population:
            raise KeyError(f"unknown config-agent id: {outcome.config_id!r}")
        agent, metrics = population[outcome.config_id]
        updated = metrics.with_outcome(outcome)
        population[outcome.config_id] = (agent, updated)
        storage.atomic_write_json(population_file_path(root), _encode(population))
        return updated


def evolve_population(root: Path, *, child_id: str | None = None) -> ConfigAgent:
    """The package's automatic-assembly entry point: read the current
    population under the lock, breed one new config-agent from its two
    fittest members, register it (generation `max(parents) + 1`, zero
    recorded runs of its own yet) and persist. Returns the new config-agent.

    Raises `ValueError` if the resulting id already exists in the
    population -- re-breeding the same top-two pair without any new
    outcomes recorded in between would otherwise silently reset a
    previously-bred config-agent's accumulated metrics back to zero."""
    with population_lock(root):
        population = _read(root)
        child = breed_new_config_agent(list(population.values()), child_id=child_id)
        if child.config_id in population:
            raise ValueError(
                f"config-agent {child.config_id!r} already exists in the population; "
                "record more outcomes for its parents or pass an explicit child_id"
            )
        population[child.config_id] = (child, ConfigMetrics())
        storage.atomic_write_json(population_file_path(root), _encode(population))
        return child
