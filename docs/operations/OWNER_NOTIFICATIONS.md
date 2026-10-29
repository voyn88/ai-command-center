# Уведомления владельца на телефон (ntfy)

Алерты с control-01 (`voyn-queue-monitor.service` → `command_center.ops.infra_monitor`)
уходят на телефон через [ntfy](https://ntfy.sh): простой HTTP POST на URL топика.
Бот-аккаунты не нужны. Без `AICC_OWNER_NTFY_URL` монитор молчит (no-op).

## Установка

1. Поставить приложение **ntfy** (iOS / Android).
2. Выбрать длинный случайный топик (или свой self-hosted сервер) и подписаться в приложении.
3. На control-01 в `/home/voynadmin/aicc-preprod/.env` задать:

   ```bash
   AICC_OWNER_NTFY_URL=https://ntfy.sh/<длинный-случайный-топик>
   # опционально, если топик защищён токеном:
   # AICC_OWNER_NTFY_TOKEN=...
   # AICC_OWNER_NOTIFY_COOLDOWN=21600   # секунды между повторными пушами по одному ключу
   # AICC_OWNER_NOTIFY_STATE=/var/lib/aicc/owner-notify.json
   ```

4. Перезапустить unit монитора, чтобы подтянуть env.

## Что означает каждый алерт

| Ключ | Смысл | Что сделать |
| --- | --- | --- |
| `queue_stalled` | В очереди есть ready/claimed, самый старый pending старше порога | Смотреть lanes на worker-01, журналы `voyn-aicc-worker@*.service`, dead letters |
| `active_workers` | Активных lane меньше минимума | `systemctl status 'voyn-aicc-worker@*.service'` на worker-01, поднять упавшие |
| `dead_letter_growth` | Рост необъяснённых dead за час | Разбирать `work_item` / `dead_reason`, чинить причину |
| `throughput_stalled` | Есть работа, но 0 succeeded за час | Проверить, не крутятся ли агенты на отказах (quota/auth) |
| `executor_quota_exhausted` | Dead из-за quota/spend/rate | Подождать лимит или сменить executor / бюджет |
| `executor_auth_failed` | ≥5 попыток за 30 мин с logout/auth ошибкой | Агенты на worker разлогинены — см. ниже |
| `credential_rotation_stalled` | Ротация credential застряла | Журнал `/var/lib/voyn-aicc-credential-rotation/`, `WORKER_CREDENTIAL_ROTATION.md` |
| `deploy_lag` | Production отстаёт от ветки дольше grace | Разобрать отказ promotion / выкатить релиз |
| `prometheus_unready` | Metrics ready-probe не отвечает | Поднять metrics endpoint на хосте |
| `defer:<task_id>` | Задача в `DEFER_TO_USER` | Нужно решение владельца по задаче в backlog |

Повтор одного и того же ключа не шлётся чаще cooldown (по умолчанию 6 часов). Когда условие исчезло, приходит одно сообщение «resolved».

## Агенты разлогинены (`executor_auth_failed`)

На worker:

```bash
ssh aicc-worker01
claude          # затем /login в TUI
codex logout && codex login --device-auth
sudo /opt/aicc/current/deploy/migrate-agent-model-auth.sh
```

После успешного логина следующие тики монитора снимут алерт (resolve).
