# Gap matrix: backlog → planner → dispatch → worker → review → merge → deploy → evidence

Фаза A (аудит кода и ADR), дата: 2026-11-07.

Статусы:
- **реализовано в коде** — подтверждено чтением кода/SQL (без live-доказательства в production).
- **частично** — часть звена реализована, но отсутствует проверка полного контура в этой среде.
- **нет** — звено не найдено.
- **не проверено (нет доступа)** — нужна рабочая БД/сервер/внешняя среда.

## 1) Цикл исполнения

| Звено | Статус | Доказательство (файл:строка) |
|---|---|---|
| Backlog (каноническое хранилище) | реализовано в коде | SECURITY DEFINER + machine model: `command_center/db/sql/0005_backlog_store.up.sql:30,173,233,306,342`; Python API: `command_center/db/backlog_store.py:45,66,82,128,150,212,258` |
| Planner tick | реализовано в коде | Lease single-owner + tick flow: `command_center/orchestrator/planner.py:8-10,301-315`; вызов `backlog_dispatch`: `planner.py:499`; authority planner lease: `planner.py:44,304` |
| Atomic dispatch | реализовано в коде | `backlog_eligible` view + `backlog_dispatch` SECURITY DEFINER: `command_center/db/sql/0006_backlog_planner.up.sql:30,93,97` |
| Worker execution | реализовано в коде | claim/heartbeat/complete/fail: `command_center/worker/daemon.py:281,438,347,349,353` (исполняющий цикл: `daemon.py:233`, обработка попытки: `daemon.py:319`) |
| Review loop | реализовано в коде | `review_once` и `publish_review_verdicts`: `command_center/orchestrator/review_merge.py:1510,2741` |
| Merge loop | реализовано в коде | `merge_once` + запись evidence + transition DONE: `command_center/orchestrator/review_merge.py:4322,4627,4630` |
| Deploy | частично | Автодеплой как процесс описан: `docs/operations/WORKER_RELEASE_DEPLOY.md:1`; live-состояние таймеров/хостов здесь не проверено |
| Evidence back to backlog | частично | PR/SHA evidence пишется (`review_merge.py:366,4627`). Поиск в рамках аудита (`command_center/**`, `command_center/db/sql/**`) не выявил явной авто-записи deployed SHA в backlog evidence; вне этого scope (инфра/внешние сервисы) не проверено |

## 2) Авторитет бэклога и вход задач

| Вопрос | Статус | Доказательство (файл:строка) |
|---|---|---|
| Авторитет бэклога | частично (зафиксирован переходный режим) | В migration-период Markdown = incumbent authority: `command_center/db/backlog_store.py:9-14`, `command_center/db/sql/0005_backlog_store.up.sql:168`; одновременно canonical store = PostgreSQL: `command_center/db/backlog_export.py:1-6`; экспорт как projection: `command_center/db/cli.py:115,548` |
| Создание/уточнение задач через Backlog Engine | реализовано в коде | Backlog CLI команды: `command_center/db/cli.py:103-188`; Python seam: `command_center/db/backlog_store.py:45-186` |
| Единственный вход для всех акторов (операционно) | не проверено (нет доступа) | Нужна проверка реальных операторских/агентских потоков и прод-БД; по коду enforce не закрывает все внешние каналы |

## 3) Сверка сортировки `backlog_eligible` (обязательная доработка)

- Базовая сортировка кандидатов в SQL view: numeric-wave first, затем wave asc, priority asc (`coalesce(P9)`), created_at asc — `command_center/db/sql/0006_backlog_planner.up.sql:42-46`.
- В `Planner.plan_once` чтение идёт `SELECT ... FROM backlog_eligible` без локального `ORDER BY` — `command_center/orchestrator/planner.py:450-452`.
- Потенциальное переупорядочивание в том же тике выполняет advisory слой `apply_advisory_order` — `planner.py:28,463`.
- Отдельная сортировка есть только для авто-resume DEFER (`ORDER BY t.priority NULLS LAST, t.wave, t.task_id`) — `planner.py:367`; это **другой под-процесс**, не основной порядок dispatch.

Вывод: в текущей реализации порядок dispatch опирается на порядок view `backlog_eligible` и может модифицироваться только advisory-reorder. Риск: `ORDER BY` внутри view без внешнего `ORDER BY` в запросе planner формально хрупок для SQL-переносимости/оптимизатора; фактическое поведение подтверждено только на текущем стеке.

## 4) Независимость ревью + cursor/grok в routing.py (обязательная доработка)

- Текущий baseline routing:
  - implementation: `claude -> codex` — `command_center/orchestrator/routing.py:36-55`
  - review: `codex -> claude` — `routing.py:67-76`
- Профили owner:
  - `owner-cascade` добавляет `cursor`, `grok` после `codex/claude` — `routing.py:84-95`
  - `owner-alternatives` даёт только `cursor`, `grok` — `routing.py:98-100`

Вывод: независимость review от исполнителя **не гарантируется на уровне executor-routing**: `codex` присутствует в обеих каскадах (implementation и review), а `cursor/grok` подключаются только через env profile. При этом в коде есть отдельный слой identity-независимости acceptance-маркера (отдельный app identity): `command_center/orchestrator/review_merge.py:14,538-554,604-617`, `scripts/assert_independent_acceptance.py:19,207` (live-проверка этого слоя в данной среде не выполнена).

## 5) Срез backlog/* PR (обязательная доработка)

Источник: `gh pr list` по `headRefName startswith("backlog/")` (снимок на момент аудита). Причины ниже — это наблюдения по `mergeStateStatus`/`reviewDecision`/labels/check-rollup, а не полная root-cause диагностика CI-логов.

- Общее число открытых backlog PR: **125**.
- Сводка по состояниям mergeStateStatus: **BEHIND=68 CLEAN=3 DIRTY=51 UNSTABLE=3**.

### 10 старейших backlog/* PR и наблюдаемая причина блокировки

(Причина формулируется по `mergeStateStatus` + `reviewDecision`)

1. `#632` (2026-09-06): **BEHIND**, review empty → отстаёт от main, review не завершён.
2. `#667` (2026-09-06): **CLEAN**, review=APPROVED → по наблюдаемым полям выглядит готовым по merge-state; фактическая причина непрохождения дальше без анализа CI/queue-логов не утверждается.
3. `#670` (2026-09-06): **BEHIND**, review empty → отстаёт от main, review не завершён.
4. `#684` (2026-09-06): **BEHIND**, review empty.
5. `#685` (2026-09-06): **BEHIND**, review empty.
6. `#691` (2026-09-06): **BEHIND**, review empty.
7. `#692` (2026-09-06): **UNSTABLE**, review empty → нестабильное состояние checks/mergeability.
8. `#693` (2026-09-06): **BEHIND**, review empty.
9. `#698` (2026-09-06): **BEHIND**, review empty.
10. `#702` (2026-09-06): **DIRTY**, review empty → конфликтует с base.

### Детализация checks/labels для 3 старейших

- `#632`: label `review-window:blocked`; есть failing checks (например `Boundary fitness ...: FAILURE`, `Linux quality shard 2 of 4: FAILURE`, `Final merge gate: FAILURE`) и множество `SKIPPED`.
- `#667`: labels `queue-waiting-review`, `review-window:waiting`; non-success в основном `SKIPPED` (gate не запускался вне активного review window).
- `#670`: labels `queue-waiting-review`, `review-window:waiting`; картина аналогична `#667` (в основном `SKIPPED`).

## 6) Что не проверено из-за отсутствия доступа

1. Фактическое состояние production backlog в PostgreSQL (`backlog_task`, `backlog_event`, `backlog_evidence`).
2. Реальная работа таймеров review/merge/deploy и попадание deployed SHA на сервере (`readlink -f /opt/aicc/current`).
3. Операционное соблюдение «backlog как единственный вход» всеми участниками вне кода репозитория.

## 7) Вывод фазы A

Кодовая цепочка `backlog → planner → dispatch → worker → review → merge` реализована в репозитории, но это именно статус **реализовано в коде**. Для статуса «проверено в проде» не хватает доступа к рабочей БД/хостам и live-подтверждений deploy/evidence замыкания.
