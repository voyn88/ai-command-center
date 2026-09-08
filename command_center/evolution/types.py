"""Value objects for evolutionary config-agent breeding.

A `ConfigAgent` is the unit this package evolves: the tunable knobs of one
concrete way to run a task, plus the lineage that produced it. It is
deliberately NOT named "profile" -- `agent_runner.py` and `capabilities.py`
already own that word for the read_only/trusted_development tool-access
profile, a different axis entirely (what an agent may touch, not how well a
config performs). "Config-agent" matches this feature's own vocabulary
("конфиг-агент").

`RunOutcome`/`ConfigMetrics` are the fitness inputs: one recorded execution of
a config-agent, and the aggregate of many. Nothing here touches storage or
the network -- see `store.py` for persistence and `scorer.py`/`breeding.py`
for the pure functions that consume these types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: Genes this package knows how to crossbreed/mutate. Each is a real,
#: already-tunable knob elsewhere in the fleet (`executor` is a
#: `orchestrator.routing.ROUTING_MATRIX` cascade-link field; the rest mirror
#: `orchestrator.planner.PlanLimits`) -- this module searches the existing
#: knob space for a better combination rather than inventing new execution
#: semantics.
GENE_NAMES = ("executor", "max_attempts", "timeout_seconds", "wip_limit")


def _coerce_genes(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if k in GENE_NAMES}


@dataclass(frozen=True, slots=True)
class ConfigAgent:
    """One evolvable config-agent: an id, its genes, and its lineage.

    `generation` is 0 for a hand-authored seed and `max(parent generations)
    + 1` for anything `breeding.crossbreed` assembled. `parent_ids` is
    `None` for a seed and the two contributing config-agents' ids for a bred
    one -- the only provenance a caller needs to answer "where did this come
    from".
    """

    config_id: str
    genes: Mapping[str, object]
    generation: int = 0
    parent_ids: tuple[str, str] | None = None

    def gene(self, name: str, default: object = None) -> object:
        return self.genes.get(name, default)

    def as_dict(self) -> dict:
        return {
            "config_id": self.config_id,
            "genes": dict(self.genes),
            "generation": self.generation,
            "parent_ids": list(self.parent_ids) if self.parent_ids else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ConfigAgent | None":
        """Fail-closed: anything not a well-typed record yields `None`
        rather than a half-built config-agent a caller might silently run."""
        if not isinstance(data, dict):
            return None
        config_id = data.get("config_id")
        if not isinstance(config_id, str) or not config_id:
            return None
        genes = _coerce_genes(data.get("genes"))
        if not genes:
            return None
        generation = data.get("generation", 0)
        generation = generation if isinstance(generation, int) and generation >= 0 and not isinstance(generation, bool) else 0
        parents = data.get("parent_ids")
        parent_ids = (
            (str(parents[0]), str(parents[1]))
            if isinstance(parents, (list, tuple)) and len(parents) == 2
            else None
        )
        return cls(config_id=config_id, genes=genes, generation=generation, parent_ids=parent_ids)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One recorded execution of a config-agent, in the vocabulary
    `runtime.outcome`/`runtime.db`'s `RUN_STATES` already use: did it
    succeed, what did it cost, how long did it take."""

    config_id: str
    succeeded: bool
    cost_usd: float
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ConfigMetrics:
    """The aggregate of every `RunOutcome` recorded for one config-agent."""

    runs: int = 0
    successes: int = 0
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    @property
    def avg_cost_usd(self) -> float:
        return self.total_cost_usd / self.runs if self.runs else 0.0

    @property
    def avg_duration_seconds(self) -> float:
        return self.total_duration_seconds / self.runs if self.runs else 0.0

    def with_outcome(self, outcome: RunOutcome) -> "ConfigMetrics":
        return ConfigMetrics(
            runs=self.runs + 1,
            successes=self.successes + (1 if outcome.succeeded else 0),
            total_cost_usd=self.total_cost_usd + max(0.0, outcome.cost_usd),
            total_duration_seconds=self.total_duration_seconds
            + max(0.0, outcome.duration_seconds),
        )

    def as_dict(self) -> dict:
        return {
            "runs": self.runs,
            "successes": self.successes,
            "total_cost_usd": self.total_cost_usd,
            "total_duration_seconds": self.total_duration_seconds,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ConfigMetrics":
        """Fail-closed: garbage of any shape yields the zero-runs default,
        never a metrics object with a negative or made-up count."""
        if not isinstance(data, dict):
            return cls()

        def _nonneg_int(value: object) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        def _nonneg_float(value: object) -> float:
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
                else 0.0
            )

        runs = _nonneg_int(data.get("runs"))
        return cls(
            runs=runs,
            successes=min(_nonneg_int(data.get("successes")), runs),
            total_cost_usd=_nonneg_float(data.get("total_cost_usd")),
            total_duration_seconds=_nonneg_float(data.get("total_duration_seconds")),
        )


def metrics_from_outcomes(outcomes) -> ConfigMetrics:
    metrics = ConfigMetrics()
    for outcome in outcomes:
        metrics = metrics.with_outcome(outcome)
    return metrics
