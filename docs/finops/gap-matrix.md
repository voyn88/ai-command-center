# FinOps GAP Matrix (AICC / VOYN, фаза A)

Фаза A: аудит кода и архитектурная подготовка, без внедрения runtime-изменений.

## 1. Карта платных LLM-путей (фактические точки вызова)

| Путь | Где вызывается API | Кто может инициировать | Тип оплаты | Статус FinOps-контроля |
|---|---|---|---|---|
| Worker `agent_run` через `openai_http` | `command_center/agent_runner.py:1119-1159` → `command_center/openai_exec.py:94-127` (`/chat/completions`) | Server chain: planner → queue → worker (`worker/handlers.py:718-742`) | За токены | Частично: есть bounded retry/failover, но нет обязательного денежного gate до вызова |
| Worker `agent_run` через Grok bridge | `ops/aicc_grok_agent.py:44-63` (`https://api.x.ai/v1/responses`) | Server chain через executor route (`routing.py:94-100`) | За токены | Частично: есть token/turn budget, но нет общего денежного ledger/reservation |
| Desktop Project Chat (OpenAI SDK) | `command_center/chat_service.py:145-191` (`client.responses.create`) | Пользователь desktop/web chat | За токены | Нет централизованного Budget Gate (прямой SDK-вызов) |
| CLI `claude/codex/copilot/cursor` | subprocess/runtimes (`agent_runner.py`, `runtime/providers.py`) | Worker/desktop launch paths | **Зависит от фактической конфигурации**: `subscription/quota` **или** `API metered` **или** `unknown` | Частично: usage/telemetry есть, но нет обязательной классификации billing-mode и единого guard; `unknown` нельзя трактовать как бесплатный режим |

## 2. Как сейчас выбирается модель/исполнитель

- Executor routing для worker задаётся каскадами и owner-профилями: `command_center/orchestrator/routing.py:35-104`.
- Фактический выбор звена каскада делает worker с fallback по retry: `command_center/worker/handlers.py:718-742,786-839,1311-1365`.
- Dispatch policy использует оценочную cost-модель (`cost_matrix`), а не фактический биллинг провайдера: `command_center/dispatch/models.py:337-360`.

## 3. Что уже есть по бюджету/учёту и где границы

| Механизм | Что есть | Граница / ограничение |
|---|---|---|
| Attempt budget в planner | Бюджет попыток ограничен длиной каскада (`planner.py:209,495-507`) | Это не денежный лимит; только ограничение числа попыток |
| Суточный gate в task pipeline | `task_pipeline.daily_spend_usd` сравнивается с `max_daily_spend_usd` (`task_pipeline.py:2230-2249,2411-2476`) | Работает только в desktop/web dispatch-path; **не подключён** к server chain planner→queue→worker |
| Дефолтное значение daily gate | `pipeline_settings.py:124` задаёт `max_daily_spend_usd = 0.0` | Проверка фактически выключена, пока значение не > 0 |
| Fail-closed при неизвестной стоимости в dispatch | `dispatch/service.py:170-196` ставит `budget_unknown` и блокирует assign | Не защищает пути вне dispatch и не покрывает server chain без dispatch/task_pipeline |

**Ключевой вывод GAP №1:** у серверной автономной цепочки `backlog planner → queue → worker` нет денежного лимита в коде. `orchestrator/*` и `worker/*` не используют `dispatch`/`task_pipeline` как бюджетный chokepoint.

## 4. Что отсутствует (ключевые GAP)

1. **GAP №1 (критический):** денежный budget gate отсутствует в server chain planner→queue→worker.
2. Единый атомарный spend ledger + reservation для токен-биллинг путей отсутствует.
3. Версионированная price table провайдеров (input/output/cached/tariff extras) отсутствует.
4. Обязательный pre-call Budget Gate на каждый paid request отсутствует.
5. Единая модель uncertain outcomes (timeout с неизвестной стоимостью) без двойного списания отсутствует.
6. FinOps-аудит trail как first-class финансовые события с `task_id/run_id` отсутствует как отдельная доменная модель.

## 5. Минимальная целевая архитектура Budget Gate (Phase B proposal, не внедрено)

1. **Сфера покрытия gate (важно):**
   - Покрывает все **metered/token-billed** вызовы: `openai_exec` (включая OpenRouter/Groq-коннекторы), Grok bridge, `chat_service`, а также CLI-пути, если они в конкретной конфигурации работают как API metered.
   - Для CLI в режиме `subscription/quota` нужен отдельный quota-guard/availability-guard.
   - Режим `unknown` для любого пути считается **финансово небезопасным**: fail-closed (запрет выполнения до явной классификации billing mode), а не «бесплатно по умолчанию».
2. **Atomic ledger (PostgreSQL):** `reserve -> call -> reconcile(commit/refund)` с idempotency key (`task_id`, `run_id`, `attempt_id`, `provider_request_id`).
3. **Price table (versioned):** effective-from, provider/model, input/output/cached-input, tariff extras.
4. **Policy checks до вызова:** task/day/month/model/project + fail-closed на unknown price.
5. **Uncertain outcome protocol:** timeout/network unknown-cost = `pending_reconcile`, новые paid calls по задаче блокируются до разрешения.
6. **Backlog integration:** budget stop отражается через VOYN Backlog Engine reason-коды (без второго backlog).
7. **ADR-0008 boundary question:** Budget Gate как capability AIOS или как capability AICC. Решение фиксируется в Phase B до runtime wiring.
8. **Anti-bypass security:** provider keys только у gate-брокера; исполнители получают ограниченный ephemeral доступ.

## 6. Классификация расходов в этом аудите

- **measured**: только уже записанные `total_cost_usd`/usage-события, где они есть.
- **estimated**: только оценки трудоёмкости внедрения.
- **unknown**: реальные денежные списания путей без достоверной записи стоимости.

Важно: `cost_usd` в ai-org submissions — операционная оценка сессии, не бухгалтерский ledger AICC.

## 7. Тест-сценарии Phase B (дизайн)

1. Два worker одновременно резервируют последние средства (race: один success, один hard-refuse).
2. Задача достигает `task_budget_usd` (новые paid calls блокируются, backlog reason записан).
3. Исчерпан `daily_budget_usd` (новые paid dispatch blocked, in-flight не списываются повторно).
4. Unknown price (fail-closed refusal до вызова провайдера).
5. Timeout → `pending_reconcile` без двойного списания.
6. Retry не обходит лимит (учитывается paid_retry_limit).
7. Попытка обхода gate прямым provider-вызовом блокируется и аудируется.

## 8. Риски и оценка внедрения Phase B

- Технические риски: гонки reservation/commit, дрейф прайс-таблицы, reconcile при задержке billing.
- Операционные риски: ложные блокировки при fail-closed, governance owner override.

**Оценка 2–4 недели — это именно оценка с допущениями**, а не SLA:
- Допущения: 1) без миграций инфраструктуры вне текущего PostgreSQL; 2) без внешнего биллингового брокера; 3) интеграция малыми PR и без freeze релизной очереди; 4) неизменный scope (без расширения на full chargeback).

## 9. Артефакты backlog-import

Подготовлены артефакты для owner-импорта в канонический Backlog Engine:
- Для **текущей** задачи фазы A (регистрация не подтверждена): `.ai-team/tasks/VOYN-FINOPS-PHASE-A/backlog-import-finops-phase-a.md`
- Для следующей фазы B (proposal): `.ai-team/tasks/VOYN-FINOPS-PHASE-A/backlog-import-finops-phase-b.md` со статусом `NOT_APPLIED` (не регистрировать и не запускать в рамках этой задачи).

## 10. Статус фазы A

- FinOps аудит оформлен отдельным документом в `docs/finops/gap-matrix.md`.
- `docs/autonomy/gap-matrix.md` возвращён к состоянию `origin/main` (без FinOps-разделов).
- Фаза B не начиналась: runtime/production policy код не менялся.
- Backlog-import для Phase A подготовлен как операционный артефакт; backlog-import для Phase B оставлен только как proposal (`NOT_APPLIED`).
