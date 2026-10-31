"""Prompt templates per task type.

A task launched without its own `prompt` used to reach the agent as nothing but
its title — "Audit Task Model, Ordering & Progress" and no instruction about
what to produce, what not to touch, or how to report. Every task type has the
same shape of instruction, so the shape belongs here once rather than being
re-typed into every task.

Two rules this module follows:

- **A task's own prompt always wins.** These templates fill a gap; they never
  override an instruction someone wrote deliberately. Silently replacing an
  author's prompt with a generic one would be worse than having no template.
- **The template states the boundary the run already enforces.** A `review`
  runs under the read-only execution profile, so its tool set contains no
  shell and no editor — the template says so plainly rather than leaving the
  agent to discover it by having a call refused. Saying it does not *create*
  the boundary (the process contract does); it stops the agent wasting a turn
  on something it cannot do.
"""

from __future__ import annotations

from command_center import agent_runner

# The report contract every template ends with. One shape for every task type,
# because `report_parser` already parses exactly this and the completion
# pipeline reads its verdict — an agent inventing its own format would break
# the machine-readable half of the pipeline.
_REPORT_CONTRACT = """
## Формат ответа

Заверши ответ отчётом:

- **Verdict:** один из `APPROVED_FOR_COMMIT`, `NOT_APPROVED_FOR_COMMIT`,
  `READY_FOR_FINAL_REVIEW`, `NOT_READY_FOR_FINAL_REVIEW`, `FAILED`.
- **Что сделано:** кратко, по пунктам.
- **Что проверено:** какие проверки запускались и с каким результатом.
- **Осталось:** незакрытые вопросы, или «нет».
""".strip()

_READ_ONLY_BOUNDARY = (
    "Ты работаешь в режиме **только для чтения**: инструментов записи и shell у "
    "тебя нет физически. Ничего не правь, не коммить, не создавай PR — только "
    "читай и делай выводы."
).strip()

_WRITE_BOUNDARY = (
    "Работай **только** в этом worktree. Не переключай ветку, не трогай другие "
    "каталоги, не делай push и не открывай PR — этим занимается конвейер после "
    "проверок."
).strip()

_TEMPLATES: dict[str, str] = {
    "implementation": (
        "Ты — инженер реализации. Выполни задачу до рабочего состояния.\n\n"
        "{objective}\n\n"
        "## Что сделать\n\n"
        "1. Изучи существующий код, прежде чем добавлять новый — используй то, что уже есть.\n"
        "2. Реализуй задачу целиком; частичная реализация хуже отсутствия.\n"
        "3. Запусти проверки проекта (тесты, линтер) и убедись, что они проходят.\n"
        "4. Закоммить результат в текущей ветке.\n\n"
        f"{_WRITE_BOUNDARY}\n\n{{report}}"
    ),
    "remediation": (
        "Ты — инженер по исправлениям. Устрани подтверждённые замечания.\n\n"
        "{objective}\n\n"
        "## Что сделать\n\n"
        "1. Исправляй **только** то, что названо в задаче — попутные улучшения "
        "делают правку непроверяемой.\n"
        "2. На каждое исправление убедись, что причина устранена, а не замаскирована.\n"
        "3. Запусти проверки проекта и закоммить результат.\n\n"
        f"{_WRITE_BOUNDARY}\n\n{{report}}"
    ),
    "review": (
        "Ты — независимый ревьюер. Оцени изменение и вынеси вердикт.\n\n"
        "{objective}\n\n"
        "## Что проверить\n\n"
        "1. Решает ли изменение поставленную задачу целиком.\n"
        "2. Нет ли регрессий, потери данных, дыр в безопасности.\n"
        "3. Адекватны ли тесты тому, что изменено.\n\n"
        f"{_READ_ONLY_BOUNDARY}\n\n{{report}}"
    ),
    "architecture_review": (
        "Ты — архитектурный аудитор. Оцени решение, а не только код.\n\n"
        "{objective}\n\n"
        "## Что проверить\n\n"
        "1. Соответствует ли реализация заявленной архитектуре и ADR.\n"
        "2. Границы модулей: что где живёт, нет ли дублирующих источников истины.\n"
        "3. Что сломается при следующем изменении в этой области.\n\n"
        f"{_READ_ONLY_BOUNDARY}\n\n{{report}}"
    ),
    "final_gate": (
        "Ты — финальный контролёр релиза. Реши, можно ли вливать.\n\n"
        "{objective}\n\n"
        "## Что проверить\n\n"
        "1. Все ли заявленные требования закрыты.\n"
        "2. Проходят ли проверки проекта.\n"
        "3. Нет ли незакрытых блокеров.\n\n"
        "Отказ — нормальный исход. Одобряй только то, что действительно готово.\n\n"
        f"{_READ_ONLY_BOUNDARY}\n\n{{report}}"
    ),
    "verification_review": (
        "Ты — верификатор находок ревью. Проверь каждую находку по дереву "
        "репозитория и вынеси вердикт.\n\n"
        "{objective}\n\n"
        "## Что проверить\n\n"
        "1. Для каждой находки найди и прочитай код, о котором она говорит.\n"
        "2. Блокирует только находка, ПОДТВЕРЖДЁННАЯ доказательствами из дерева "
        "(файл:строка, конкретный сценарий отказа).\n"
        "3. Заявление о безопасности снимается только доказательством, что атака "
        "невозможна.\n\n"
        f"{_READ_ONLY_BOUNDARY}\n\n{{report}}"
    ),
    "research": (
        "Ты — исследователь. Собери факты и сделай выводы.\n\n"
        "{objective}\n\n"
        "## Что сделать\n\n"
        "1. Опирайся на то, что можно проверить в репозитории или в источниках.\n"
        "2. Отделяй факты от предположений — помечай, где что.\n\n"
        f"{_READ_ONLY_BOUNDARY}\n\n{{report}}"
    ),
}

# Used for a task type with no template of its own. Deliberately generic and
# read-only-safe: guessing that an unknown type may write would be the
# dangerous direction.
_FALLBACK = (
    "Выполни задачу.\n\n"
    "{objective}\n\n"
    "Не выходи за пределы этого worktree. Не делай push и не открывай PR.\n\n"
    "{report}"
)


def objective_of(task: dict) -> str:
    """The task's own description, in the order a human would read it."""
    for field in ("goal", "prompt", "title"):
        value = (task.get(field) or "").strip()
        if value:
            return value
    return "(описание задачи отсутствует)"


def build_prompt(task: dict) -> str:
    """The instruction to launch `task` with.

    A task carrying its own `prompt` gets it verbatim — templates fill a gap,
    they never overwrite a deliberate instruction. Everything else is composed
    from the template for its `task_type`, with the task's objective and the
    shared report contract."""
    own = (task.get("prompt") or "").strip()
    if own:
        return own

    task_type = task.get("task_type") or "implementation"
    template = _TEMPLATES.get(task_type, _FALLBACK)
    return template.format(objective=objective_of(task), report=_REPORT_CONTRACT).strip()


def has_template(task_type: str | None) -> bool:
    return task_type in _TEMPLATES


def is_read_only(task_type: str | None) -> bool:
    """Whether this type runs under the read-only profile. Read from
    `agent_runner`, not duplicated, so the template's stated boundary and the
    tool set the process actually gets can never disagree."""
    return task_type in agent_runner.READ_ONLY_TASK_TYPES
