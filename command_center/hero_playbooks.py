"""Hero Playbooks: a catalog of the (project, task_type, agent) combos that
have historically finished with the best return — success rate weighted
against how long they took — built purely from the existing task list, the
same "no second data source" convention `command_center.recommend` uses for
"next task" scoring.

Two entry points:

- `build_playbook_catalog(tasks)` — scans every finished task and groups it
  by combo signature, returning the catalog best-first (the "Hero"
  playbooks are simply the top of this list).
- `suggest_hero_playbook(new_task, catalog)` — given a new, not-yet-run
  task (a "new scenario"), finds the catalog entry whose context is the
  same or most similar and returns it as a suggestion, so a task that looks
  like a previous winning combo gets pointed at that combo automatically
  (VOYN-MIN-HERO acceptance: "a new scenario automatically suggests a Hero
  Playbook for similar context").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from command_center import models

DEFAULT_TASK_TYPE = "implementation"
DEFAULT_AGENT = "unspecified"

# A combo needs at least this many finished tasks before its success rate/
# duration are trusted enough to publish as a "Hero" recommendation — one
# lucky run is a data point, not a playbook.
MIN_SAMPLE_SIZE = 2

# Word-overlap floor for the title/goal fallback match in
# `suggest_hero_playbook` — below this, two scenarios are treated as
# unrelated rather than "similar context".
MIN_TEXT_SIMILARITY = 0.3

_STOPWORDS = {
    "и", "в", "во", "не", "на", "с", "со", "к", "ко", "по", "для", "от", "до",
    "the", "a", "an", "to", "of", "for", "in", "on", "and", "or",
}


@dataclass
class HeroPlaybook:
    project: str
    task_type: str
    agent: str
    sample_size: int
    success_rate: float
    avg_duration_seconds: float | None
    return_score: float
    example_task_ids: list[str] = field(default_factory=list)
    example_titles: list[str] = field(default_factory=list)

    @property
    def signature(self) -> tuple[str, str]:
        return (self.project, self.task_type)


def _task_type(task: dict) -> str:
    return task.get("task_type") or DEFAULT_TASK_TYPE


def _agent(task: dict) -> str:
    return task.get("agent") or task.get("executor") or DEFAULT_AGENT


def _is_successful(task: dict) -> bool:
    """A finished task "won" if its last verdict passed or its PR merged —
    the same two success signals `recommend._score_candidates` already
    trusts (`models.is_passing_verdict`, `pull_request_status == "merged"`)."""
    if models.is_passing_verdict(task.get("latest_verdict")):
        return True
    return task.get("pull_request_status") == "merged"


def _duration_seconds(task: dict) -> float | None:
    started = task.get("started_at")
    finished = task.get("finished_at")
    if not started or not finished:
        return None
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except ValueError:
        return None
    seconds = delta.total_seconds()
    return seconds if seconds >= 0 else None


def _tokenize(text: str) -> set[str]:
    words = "".join(ch.lower() if ch.isalnum() else " " for ch in text or "").split()
    return {word for word in words if word not in _STOPWORDS and len(word) > 2}


def _text_similarity(a: str, b: str) -> float:
    """Jaccard similarity over tokenized words — a cheap, dependency-free
    stand-in for semantic similarity, good enough to notice "similar
    context" when two task titles/goals share most of their vocabulary."""
    tokens_a, tokens_b = _tokenize(a), _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def build_playbook_catalog(tasks: list[dict], *, min_sample_size: int = MIN_SAMPLE_SIZE) -> list[HeroPlaybook]:
    """Group every finished task by (project, task_type, agent) and score
    each combo's historical return. Returns the catalog sorted best-first;
    combos with fewer than `min_sample_size` finished tasks are dropped —
    not enough history to call them a "Hero" playbook yet.

    Return score = success rate (0-100) with a small bonus for finishing
    faster than the catalog's slowest combo — success matters far more than
    speed, mirroring how `recommend.py` weights its own scoring rules
    (large point swings for correctness signals, small nudges for the
    rest).
    """
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for task in tasks:
        if task.get("status") != "Done":
            continue
        project = task.get("project")
        if not project:
            continue
        key = (project, _task_type(task), _agent(task))
        groups.setdefault(key, []).append(task)

    durations = []
    for group_tasks in groups.values():
        durations.extend(d for t in group_tasks if (d := _duration_seconds(t)) is not None)
    slowest = max(durations) if durations else None

    catalog: list[HeroPlaybook] = []
    for (project, task_type, agent), group_tasks in groups.items():
        sample_size = len(group_tasks)
        if sample_size < min_sample_size:
            continue

        wins = sum(1 for t in group_tasks if _is_successful(t))
        success_rate = wins / sample_size

        combo_durations = [d for t in group_tasks if (d := _duration_seconds(t)) is not None]
        avg_duration = sum(combo_durations) / len(combo_durations) if combo_durations else None

        return_score = success_rate * 100
        if avg_duration is not None and slowest:
            return_score += (1 - avg_duration / slowest) * 10

        catalog.append(
            HeroPlaybook(
                project=project,
                task_type=task_type,
                agent=agent,
                sample_size=sample_size,
                success_rate=success_rate,
                avg_duration_seconds=avg_duration,
                return_score=return_score,
                example_task_ids=[t["id"] for t in group_tasks if t.get("id")],
                example_titles=[t.get("title") for t in group_tasks if t.get("title")],
            )
        )

    catalog.sort(key=lambda p: p.return_score, reverse=True)
    return catalog


@dataclass
class PlaybookSuggestion:
    playbook: HeroPlaybook
    match_kind: str  # "exact_context" | "same_task_type" | "similar_title"
    similarity: float
    reason: str


def suggest_hero_playbook(new_task: dict, catalog: list[HeroPlaybook]) -> PlaybookSuggestion | None:
    """Match a new, not-yet-run scenario against the Hero Playbook catalog.

    Tries progressively looser context signals, each restricted to the
    best-scoring (catalog is already sorted) candidate at that tier:

    1. Exact context — same project and task_type.
    2. Same task_type in any project — the recipe (agent choice) still
       likely transfers even if the project differs.
    3. Similar title/goal text against any catalog entry's example
       titles — catches a new scenario that reads like a past winner even
       when its project/task_type metadata weren't filled in the same way.

    Returns `None` when nothing clears `MIN_TEXT_SIMILARITY` at every tier —
    no confident suggestion is better than a misleading one.
    """
    if not catalog:
        return None

    project = new_task.get("project")
    task_type = _task_type(new_task)

    for playbook in catalog:
        if playbook.project == project and playbook.task_type == task_type:
            return PlaybookSuggestion(
                playbook=playbook,
                match_kind="exact_context",
                similarity=1.0,
                reason=(
                    f"тот же проект «{project}» и тип задачи «{task_type}» — "
                    f"комбо «{playbook.agent}» сработало в {playbook.success_rate:.0%} "
                    f"из {playbook.sample_size} прошлых запусков"
                ),
            )

    for playbook in catalog:
        if playbook.task_type == task_type:
            return PlaybookSuggestion(
                playbook=playbook,
                match_kind="same_task_type",
                similarity=0.5,
                reason=(
                    f"тот же тип задачи «{task_type}» (в проекте «{playbook.project}») — "
                    f"комбо «{playbook.agent}» сработало в {playbook.success_rate:.0%} "
                    f"из {playbook.sample_size} прошлых запусков"
                ),
            )

    new_text = f"{new_task.get('title') or ''} {new_task.get('goal') or ''}"
    best_playbook: HeroPlaybook | None = None
    best_similarity = 0.0
    for playbook in catalog:
        for title in playbook.example_titles:
            similarity = _text_similarity(new_text, title)
            if similarity > best_similarity:
                best_similarity = similarity
                best_playbook = playbook

    if best_playbook is not None and best_similarity >= MIN_TEXT_SIMILARITY:
        return PlaybookSuggestion(
            playbook=best_playbook,
            match_kind="similar_title",
            similarity=best_similarity,
            reason=(
                f"похожий сценарий по формулировке — комбо «{best_playbook.agent}» "
                f"для «{best_playbook.task_type}» сработало в {best_playbook.success_rate:.0%} "
                f"из {best_playbook.sample_size} прошлых запусков"
            ),
        )

    return None
