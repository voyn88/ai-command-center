# Runbook: подпись и notarization desktop-инсталляторов (#197)

> **Принятая стратегия (2026-08-11): unsigned-first.** Дистрибуция стартует
> без сертификатов — через GitHub Releases + Homebrew cask + winget с
> проверкой SHA-256; см. `INSTALL_UNSIGNED.md` и workflow
> `release-desktop.yml`. Всё ниже — отложенный owner-шаг: когда владелец
> получит сертификаты, подпись встраивается в тот же релизный конвейер.

Статус на 2026-08-11: на сборочной машине (Apple Silicon) `security find-identity
-v -p codesigning` → **0 valid identities**, профиль `notarytool` отсутствует.
Всё в §1 может выполнить **только владелец** (платный аккаунт, секреты).
Агент секреты не вводит и не хранит (см. политику репозитория).

## 1. Owner-only: получить signing identity

### macOS
1. Membership в Apple Developer Program (99 $/год) на Apple ID владельца.
2. Сертификат **Developer ID Application** (+ **Developer ID Installer**, если
   будет `.pkg`): Xcode → Settings → Accounts → Manage Certificates → «+», либо
   developer.apple.com → Certificates. Ключ остаётся в Keychain этой машины.
3. App-specific password для notarization: appleid.apple.com → App-Specific
   Passwords.
4. Сохранить профиль notarization (секрет вводит владелец в свой терминал):

   ```
   xcrun notarytool store-credentials aicc-notary \
     --apple-id <apple-id> --team-id <TEAMID>
   ```

Проверка готовности: `security find-identity -v -p codesigning` показывает
`Developer ID Application: … (<TEAMID>)`; `xcrun notarytool history
--keychain-profile aicc-notary` отвечает без ошибки авторизации.

### Windows
Один из вариантов (решение владельца, фиксируется в ADR):
- OV/EV code-signing сертификат (Sectigo/DigiCert/…), ключ на USB-токене —
  классический `signtool`;
- **Azure Trusted Signing** — подписка Azure, подпись через
  `signtool … /dlib Azure.CodeSigning.Dlib.dll`; дешевле EV, без токена.

## 2. Автоматизируемая часть (после появления identity)

Скриптована (эта секция — push-button):

- **macOS:** `scripts/sign-desktop-macos.sh` — inside-out подпись вложенных
  Mach-O + бандла (hardened runtime, `packaging/macos/entitlements.plist`),
  `hdiutil` → DMG, `notarytool submit --wait`, `stapler staple`, финальная
  проверка `spctl … context:primary-signature` → `accepted`. Identity берётся
  из Keychain автоматически (или `AICC_SIGN_IDENTITY`), профиль —
  `AICC_NOTARY_PROFILE` (default `aicc-notary`). Без identity/профиля скрипт
  падает с отсылкой к §1.
- **Windows:** `scripts/sign-desktop-windows.ps1` — `signtool sign /fd SHA256
  /td SHA256 /tr <timestamp>` + `signtool verify /pa`; сертификат — по
  `AICC_SIGN_THUMBPRINT` или единственный из `Cert:\CurrentUser\My`.

Приёмка после подписи: чистая машина открывает DMG без обхода Gatekeeper;
первый запуск на физическом Windows 11 x64 без SmartScreen-предупреждения
«unknown publisher».

## 3. Границы

- Без identity выполняется только unsigned-ветка D4A/D4B (ad-hoc подпись
  PyInstaller); обход Gatekeeper средствами сборки запрещён
  (`packaging/macos/SMOKE_CHECKLIST.md`).
- Приёмка #197 требует физических чистых машин (macOS + Windows 11 x64,
  физический дисплей, PowerShell 5.1) — фиксируется владельцем в чеклистах
  `packaging/*/SMOKE_CHECKLIST.md`.
