# Worker release deploy (immutable root-owned releases)

Задача: **VOYN-W0-AICC-WORKER-SELF-DEPLOY-MUST-STAGE-IMMUTABLE-RELEASE**.

На worker-01 `/opt/aicc/current` должен следовать за `origin/main` автоматически,
через **root-owned** immutable tree под `/opt/aicc/releases/<sha>` (layout как у
installer: `dr-xr-xr-x root:root`, `.venv` → `/opt/aicc/runtime-venv`, AIOS
wheels уже в runtime-venv). Код из voynadmin-writable checkout
(`/opt/aicc/source`) **не исполняется** root-ом: fetch идёт в bare mirror
`/var/lib/aicc-principal-isolation/mirror.git`, release собирается через
`git archive` + `record_release_manifest` / `publish_release_tree` /
`verify_release_manifest` / `select_release`.

Control-01 по-прежнему использует `voyn-aicc-self-deploy` (migrations). Worker
вариант `voyn-aicc-self-deploy-worker` **снят**.

## Unit

| Unit | Role |
|------|------|
| `voyn-aicc-worker-release-deploy.service` | oneshot, `User=root`, `ProtectSystem=strict` |
| `voyn-aicc-worker-release-deploy.timer` | every 5 min (`OnUnitActiveSec=5min`) |

Stdout одной строки: `NOOP|DEPLOYED|REFUSED|ROLLED_BACK <sha> <detail>`.

Поведение:

- **NOOP** — `current` уже указывает на SHA `origin/main`.
- **DEPLOYED** — release опубликован/выбран, активные lanes из
  `/etc/voyn/aicc-worker-lanes.conf` drain-restarted до `STATUS=aicc-ready`.
  Зарегистрированный lane в `inactive` **не стартуется**.
- **REFUSED** — rollout lock `/run/aicc-staged-rollout.lock`, dependency
  manifest change (нужен полный installer), mirror/fetch failure, и т.п.
- **ROLLED_BACK** — ни один restarted lane не стал ready; `current` возвращён
  на предыдущий release (дерево предыдущего SHA сохраняется).

## One-time install on worker-01 (root console)

После merge этого коммита в `main` (или с checkout нужного SHA):

```bash
# 1) Положить unit-файлы и бинарь (или прогнать worker-профиль installer'а)
install -m 0755 -o root -g root \
  /opt/aicc/source/ops/aicc_release_deploy.py \
  /usr/libexec/aicc-worker-release-deploy
install -m 0644 -o root -g root \
  /opt/aicc/source/deploy/systemd/voyn-aicc-worker-release-deploy.service \
  /etc/systemd/system/voyn-aicc-worker-release-deploy.service
install -m 0644 -o root -g root \
  /opt/aicc/source/deploy/systemd/voyn-aicc-worker-release-deploy.timer \
  /etc/systemd/system/voyn-aicc-worker-release-deploy.timer

# 2) Убрать старый voynadmin self-deploy на worker
systemctl disable --now voyn-aicc-self-deploy.timer || true
systemctl disable voyn-aicc-self-deploy.service || true

# 3) Включить новый timer
systemctl daemon-reload
systemctl enable --now voyn-aicc-worker-release-deploy.timer
```

Полный `deploy/install-agent-principal-isolation.sh --profile worker` делает то
же при следующей установке/переустановке isolation.

## Verify

```bash
systemctl status voyn-aicc-worker-release-deploy.timer
systemctl start voyn-aicc-worker-release-deploy.service
journalctl -u voyn-aicc-worker-release-deploy.service -n 50 --no-pager
readlink -f /opt/aicc/current
# ожидается /opt/aicc/releases/<40-hex origin/main>
stat -c '%A %U:%G' /opt/aicc/current /opt/aicc/releases/*
systemctl show voyn-aicc-worker@1.service -p ActiveState -p SubState -p StatusText
```

Первый успешный tick после отставания создаст bare mirror (если его ещё нет),
опубликует SHA и перезапустит только **активные** lanes (`@1`/`@2` на
worker-01; остановленные `@3`/`@4` не трогает).
