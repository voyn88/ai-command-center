"""Субъектные турниры — the monthly agent tournament protocol.

Each calendar month, every completed run whose task declares a tournament
category (Dev / Security / Ops / Planning / UX) counts as one point for the
agent that ran it. :func:`build_monthly_protocol` tallies those points per
category and ranks the agents into a :class:`TournamentProtocol` — the
"protocol" a month's Board publishes.

This module is pure: no filesystem, no database, no Streamlit. Persisting and
publishing a protocol is :mod:`command_center.tournament_store`'s job;
rendering it is :mod:`command_center.ui.tournament_panel`'s.

A run never earns a category by guesswork. `task_category` reads a *declared*
field on the run's task — `metadata.category`/`metadata.discipline`, or a
top-level `category`/`discipline` — exactly the precedence
:func:`command_center.waves.wave_label` already uses for `parallel_group`/
`wave`. A task that declares no recognized category contributes to no
tournament; nothing here infers a category from a title, task type or
project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: The five tournament tracks, in canonical display order.
CATEGORIES: tuple[str, ...] = ("Dev", "Security", "Ops", "Planning", "UX")

_CATEGORY_BY_LOWER: dict[str, str] = {category.lower(): category for category in CATEGORIES}

# Same key-precedence convention as `waves._WAVE_KEYS`: metadata first (where
# task-import provenance lands), then a top-level field for hand-authored tasks.
_CATEGORY_KEYS: tuple[str, ...] = ("category", "discipline")

_COMPLETED_STATE = "COMPLETED"


def task_category(task: dict) -> str | None:
    """The tournament category `task` declares, normalized to one of
    :data:`CATEGORIES`, or `None` if it declares none (or an unrecognized
    one). A blank/non-string value is treated as absent, same as
    `waves.wave_label`."""
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    for source in (metadata, task):
        for key in _CATEGORY_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                canonical = _CATEGORY_BY_LOWER.get(value.strip().lower())
                if canonical:
                    return canonical
    return None


def current_month(*, now: datetime | None = None) -> str:
    """The `YYYY-MM` a protocol built now belongs to."""
    return (now or datetime.now()).strftime("%Y-%m")


def _run_month(run: dict) -> str | None:
    ts = run.get("completed_at") or run.get("created_at") or ""
    return ts[:7] if len(ts) >= 7 else None


@dataclass(frozen=True)
class Standing:
    """One agent's placement within one category's monthly tournament."""

    participant: str
    completed: int
    rank: int


@dataclass(frozen=True)
class TournamentProtocol:
    """One month's published Субъектные турниры protocol: a ranked
    :class:`Standing` tuple per category, empty for a category no one scored
    in — every category is always present, never omitted."""

    month: str
    generated_at: str
    categories: dict[str, tuple[Standing, ...]]

    def champion(self, category: str) -> Standing | None:
        standings = self.categories.get(category) or ()
        return standings[0] if standings else None


def _rank_standings(tallies: dict[str, int]) -> tuple[Standing, ...]:
    """Competition ranking (1224): ties share a rank, and the next distinct
    score skips to the count of agents ahead of it. Deterministic tie-break by
    participant name so equal scores don't depend on dict/tally order."""
    ordered = sorted(tallies.items(), key=lambda item: (-item[1], item[0]))
    standings: list[Standing] = []
    for index, (participant, completed) in enumerate(ordered):
        rank = index + 1
        if index > 0 and completed == ordered[index - 1][1]:
            rank = standings[index - 1].rank
        standings.append(Standing(participant=participant, completed=completed, rank=rank))
    return tuple(standings)


def build_monthly_protocol(
    runs: list[dict],
    tasks_by_id: dict[str, dict],
    *,
    month: str | None = None,
    now: datetime | None = None,
) -> TournamentProtocol:
    """Tally `month`'s (default: the current month) completed runs by
    category and agent, and rank each category's standings.

    A run counts only when: its `state` is `COMPLETED`, its `completed_at` (or
    `created_at`) falls in `month`, and its task (`tasks_by_id[task_id]`)
    declares a recognized category. Everything else — queued/failed runs,
    runs outside the month, runs whose task declares no category, or whose
    `task_id` is unknown — is silently excluded, the same way a task with no
    declared wave is excluded from every wave (see module docstring)."""
    month = month or current_month(now=now)
    tallies: dict[str, dict[str, int]] = {category: {} for category in CATEGORIES}

    for run in runs:
        if run.get("state") != _COMPLETED_STATE:
            continue
        if _run_month(run) != month:
            continue
        task = tasks_by_id.get(run.get("task_id"))
        if task is None:
            continue
        category = task_category(task)
        if category is None:
            continue
        participant = run.get("agent") or "—"
        tallies[category][participant] = tallies[category].get(participant, 0) + 1

    return TournamentProtocol(
        month=month,
        generated_at=(now or datetime.now()).isoformat(timespec="seconds"),
        categories={category: _rank_standings(tallies[category]) for category in CATEGORIES},
    )


def protocol_to_dict(protocol: TournamentProtocol) -> dict:
    """JSON-serializable form of `protocol` (round-trips via
    :func:`protocol_from_dict`)."""
    return {
        "month": protocol.month,
        "generated_at": protocol.generated_at,
        "categories": {
            category: [
                {"participant": s.participant, "completed": s.completed, "rank": s.rank}
                for s in standings
            ]
            for category, standings in protocol.categories.items()
        },
    }


def protocol_from_dict(data: dict) -> TournamentProtocol:
    return TournamentProtocol(
        month=data["month"],
        generated_at=data["generated_at"],
        categories={
            category: tuple(
                Standing(participant=row["participant"], completed=row["completed"], rank=row["rank"])
                for row in rows
            )
            for category, rows in (data.get("categories") or {}).items()
        },
    )
