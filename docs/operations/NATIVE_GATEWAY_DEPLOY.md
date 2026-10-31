# Развёртывание AICC Native Gateway на control-01

Цель: владелец смотрит за автономным конвейером с iPhone/Mac (приложение
`clients/aicc-native/apple`) без ноутбучной сессии. Шлюз — только чтение
(`GET /v1/snapshot`, `/v1/tasks`, `/v1/dialogs`, `/v1/decisions`,
`/v1/events`); команд и записи в v1 нет (см.
`docs/aicc_native_gateway/COMMAND_GATEWAY_CONTRACT.md`).

## Схема

```
control DB (aicc_preprod, SELECT по public-view)
      │  aicc-gateway-projection.timer (раз в минуту)
      ▼
/var/lib/aicc-gateway/projection.json   ← артефакт проекции (+ pipeline_health)
      │  только чтение
      ▼
aicc-native-gateway.service  — HTTPS, bearer-токен устройства, bind = Tailscale IP
      │  tailnet
      ▼
iPhone / Mac (Tailscale + приложение AICC)
```

Шлюз не держит пароля БД и не ходит в БД: всё, что он отдаёт, лежит в
артефакте. Продюсер читает БД только через view, на которые у `aicc_app` уже
есть `SELECT` (`work_item_public`, `work_attempt_public`, `backlog_task`,
`monitor_finding`).

### Что в `pipelineHealth` снимка

| Поле | Источник |
| --- | --- |
| `queue.ready/claimed/dead` | `work_item_public` (ready = доступные сейчас) |
| `queue.oldestReadyAgeSeconds` | возраст самого старого доступного ready |
| `queue.succeededLastHour` | succeeded с `updated_at` за последний час |
| `activeLanes[]` | `claimed_by_role` с активной попыткой и `visible_until > now()` |
| `executorAuthFailures30m` | попытки за 30 мин с `outcome_reason ~* 'failed to authenticate\|oauth session expired\|refresh token'` (только число) |
| `backlogByStatus` | `backlog_task` по статусам |
| `ownerEscalations[]` | задачи `DEFER_TO_USER`: id, заголовок, приоритет, `updatedAt` |
| `monitor` | открытые `monitor_finding`: вердикт `ok`/`failing`, коды отказов без деталей; поле отсутствует, если таблица недоступна |

Текст причин, детали мониторинга, промпты, URL и IP в снимок не попадают;
редакция шлюза (`native_gateway/redaction.py`) проверяет каждое значение и
итоговое тело ответа.

## Однократная настройка (оператор, на control-01)

Юниты ставятся файлами вместе с control-профилем
(`ops/aicc_install_transaction.py: CONTROL_ONLY_UNITS`), но **не включаются**
инсталлятором — сначала нужны сертификат и токены. Если релиз ещё не
переустановлен, скопируйте их вручную:

```sh
sudo install -m 0644 /opt/aicc/current/deploy/systemd/aicc-native-gateway.service \
    /opt/aicc/current/deploy/systemd/aicc-gateway-projection.service \
    /opt/aicc/current/deploy/systemd/aicc-gateway-projection.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### 1. Зависимости шлюза в runtime-venv

```sh
/opt/aicc/current/.venv/bin/python -c "import fastapi, uvicorn, pydantic" \
  || /opt/aicc/current/.venv/bin/python -m pip install -r /opt/aicc/current/requirements-gateway.txt
```

(Если venv ставится self-deploy'ем из манифеста — добавьте туда
`requirements-gateway.txt`, иначе следующий релиз их потеряет.)

### 2. Tailscale-имя и сертификат

В админке Tailscale должны быть включены MagicDNS и HTTPS Certificates.

```sh
tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//'   # например control-01.tailXXXX.ts.net
tailscale ip -4                                                   # Tailscale IP control-01 (100.x.y.z)

NAME=control-01.tailXXXX.ts.net        # подставьте вывод выше
sudo install -d -o voynadmin -g voynadmin -m 0700 /etc/aicc/native-gateway /etc/aicc/native-gateway/tls
sudo tailscale cert \
    --cert-file /etc/aicc/native-gateway/tls/gateway.crt \
    --key-file  /etc/aicc/native-gateway/tls/gateway.key "$NAME"
sudo chown voynadmin:voynadmin /etc/aicc/native-gateway/tls/gateway.*
sudo chmod 0600 /etc/aicc/native-gateway/tls/gateway.key
```

Сертификат Let's Encrypt живёт ~90 дней: повторяйте ту же команду (например,
ежемесячным root-cron) и затем `sudo systemctl restart aicc-native-gateway`.

### 3. Файл окружения шлюза

```sh
sudo tee /etc/aicc/native-gateway.env >/dev/null <<EOF
AICC_GATEWAY_BIND=$(tailscale ip -4)
AICC_GATEWAY_PORT=8443
EOF
sudo chmod 0644 /etc/aicc/native-gateway.env
```

Остальные пути (проекция, реестр токенов, cert/key) заданы в юните по
умолчанию и при необходимости переопределяются здесь же. Bind — только
Tailscale IP: порт не открыт ни в публичный интернет, ни в LAN.

### 4. Токен устройства

```sh
cd /opt/aicc/current
sudo -u voynadmin /opt/aicc/current/.venv/bin/python -m native_gateway.provision \
    --registry /etc/aicc/native-gateway/device_tokens.json \
    --device-id iphone-owner-01 --label "iPhone владельца"
```

Токен печатается **один раз**; в реестре хранится только его SHA-256. Отдельный
токен на каждое устройство (`mac-owner-01` и т. д.). Отзыв — `"disabled": true`
у записи в реестре (без удаления).

### 5. Запуск

```sh
sudo systemctl enable --now aicc-gateway-projection.timer
sudo systemctl start aicc-gateway-projection.service && journalctl -u aicc-gateway-projection -n 5
sudo systemctl enable --now aicc-native-gateway.service
curl -sS -H 'Accept: application/json' -H 'X-AICC-Client-Version: 1.0' \
     -H "Authorization: Bearer <токен>" "https://$NAME:8443/v1/snapshot" | jq '.freshness, .pipelineHealth.queue'
```

Ожидается `"fresh"` и счётчики очереди. `degraded` означает, что продюсер не
смог прочитать БД (см. журнал `aicc-gateway-projection`); `offline` — артефакт
не обновляется дольше 15 минут.

После каждого релиза (`/opt/aicc/current` переключается self-deploy'ем)
шлюз продолжает работать на старом коде до
`sudo systemctl restart aicc-native-gateway`; таймер продюсера подхватывает
новый релиз сам.

## Телефон и Mac владельца

1. Установить Tailscale из App Store (iPhone и Mac), войти в тот же tailnet,
   что и control-01; включить VPN-переключатель в приложении Tailscale.
2. Проверить в Safari: `https://<имя control-01 в tailnet>:8443/v1/snapshot`
   должен ответить ошибкой `401` в JSON — значит, сеть и TLS в порядке.
3. В приложении AICC указать адрес сервера
   `https://<имя control-01 в tailnet>:8443` (ключ `AICCServerURL` в
   Info.plist или `AICC_SERVER_URL` для сборки) и вставить токен устройства из
   шага 4 (хранится в Keychain). Сертификат публичный (Let's Encrypt через
   Tailscale), поэтому пиннинг не обязателен; при включении пина обновлять его
   после каждого перевыпуска сертификата.

## Проверка и откат

- `systemctl status aicc-native-gateway aicc-gateway-projection.timer`
- Отключить доступ: `sudo systemctl disable --now aicc-native-gateway`
  (артефакт и токены остаются), отдельное устройство — `disabled: true`.
