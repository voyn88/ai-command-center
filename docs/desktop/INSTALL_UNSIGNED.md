# Установка desktop-сборок без сертификатов (unsigned-first, #197)

Стратегия: стартуем без платных сертификатов. Сборки собирает CI
(`.github/workflows/release-desktop.yml`) по тегу `desktop-vX.Y.Z` и публикует
в GitHub Release с SHA-256. Целостность гарантируется хешами (brew и winget
проверяют их автоматически), доверие ОС — позже, когда владелец получит
сертификаты (см. `SIGNING_RUNBOOK.md`).

## Выпуск релиза (мейнтейнер)

1. `git tag desktop-vX.Y.Z && git push origin desktop-vX.Y.Z` — CI соберёт
   macOS arm64 (`.app`, ad-hoc подпись) и Windows x64 (portable-папку) и
   создаст Release с `SHA256SUMS.txt`.
2. Обновить `packaging/homebrew/Casks/ai-command-center.rb`: `version` и
   `sha256` из `SHA256SUMS.txt`; скопировать файл в tap-репозиторий
   `dimastov-lab/homebrew-tap` → `Casks/ai-command-center.rb` (репозиторий
   создаётся один раз: публичный, пустой, только каталог `Casks/`).
3. Обновить `packaging/winget/*.yaml`: `PackageVersion`, URL, `InstallerSha256`.

## macOS (Homebrew)

```bash
brew tap dimastov-lab/tap
brew trust dimastov-lab/tap
brew install --cask ai-command-center
xattr -dr com.apple.quarantine "/Applications/AI Command Center.app"
```

Homebrew 6: сторонние tap требуют `brew trust`, флаг `--no-quarantine`
удалён — карантин снимается `xattr` после установки (сборка не нотаризована,
иначе Gatekeeper заблокирует запуск). Альтернатива без `xattr` — один раз:
правый клик по `AI Command Center.app` → «Открыть» → «Открыть».

Ручная установка без brew: скачать `AI-Command-Center-macos-arm64.zip` из
Releases, проверить хеш (`shasum -a 256 -c *.sha256`), распаковать в
`/Applications`, снять карантин:

```bash
xattr -dr com.apple.quarantine "/Applications/AI Command Center.app"
```

## Windows (winget)

Пока манифест не отправлен в microsoft/winget-pkgs — локальный манифест:

```powershell
winget settings --enable LocalManifestFiles   # один раз, от администратора
winget install --manifest packaging\winget
```

winget проверит SHA-256 и установит portable-сборку (алиас `aicc`).
При первом запуске SmartScreen покажет «Неизвестный издатель» →
«Подробнее» → «Выполнить в любом случае» (сборка не подписана).

Ручная установка: скачать `AI-Command-Center-windows-x64.zip`, сверить хеш
(`Get-FileHash -Algorithm SHA256`), распаковать, запустить
`AI Command Center.exe`.

## Что остаётся на потом (owner-only)

- Apple Developer Program + Developer ID → notarization (уберёт шаг снятия
  карантина); Azure Trusted Signing / OV-серт → уберёт SmartScreen.
  Скрипты подписи уже готовы: `scripts/sign-desktop-*.{sh,ps1}`.
- Публикация манифеста в microsoft/winget-pkgs (PR в публичный репозиторий,
  принимают и неподписанные zip/portable — валидация по хешу).
