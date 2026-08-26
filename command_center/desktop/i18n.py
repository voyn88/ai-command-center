"""Russian UI strings — the single source of truth for user-visible text.

The desktop application is Russian-only for its operator, so translations live
here as one registry rather than as per-locale ``.qm`` files. Keeping every
user-facing string in this one module is the i18n seam: a later move to
``QTranslator``/multi-locale would change only this module and the accessors,
not the call sites. Widget *identifiers* (``objectName``) are not translated —
they stay stable English keys.

The automated gate ``tests/desktop/test_ui_language_russian.py`` walks the live
widget tree and fails on any user-visible Latin string that is not an allowed
proper noun / technical abbreviation, so a newly-added English string is caught.
"""

from __future__ import annotations

# Product name — a proper noun, intentionally not translated.
APP_TITLE = "AI Command Center"

# --- Sidebar / navigation -------------------------------------------------
NAV_HEADER = "РАЗДЕЛЫ"
NAV_ACCESSIBLE = "Основная навигация"
DISABLED_TOOLTIP = "Появится в следующем выпуске"

# Section labels keyed by the stable section key (`sections.py`). "Git" stays a
# proper noun.
SECTION_LABELS: dict[str, str] = {
    "home": "Главная",
    "projects": "Проекты",
    "sessions": "Сессии",
    "execution": "Выполнение",
    "git": "Git",
    "artifacts": "Артефакты",
    "reports": "Отчёты",
    "agents": "Агенты",
    "settings": "Настройки",
}

OPERATIONS_NOT_LOADED = "Данные загрузятся при открытии раздела."
OPERATIONS_LOADING = "Загрузка данных…"
OPERATIONS_ERROR = "Не удалось загрузить данные. Повторите обновление."
OPERATIONS_EMPTY = "Данных пока нет."
OPERATIONS_ROWS = "Записей: {count}"
OPERATIONS_TABLE_ACCESSIBLE = "Таблица раздела «{title}»"

OPERATIONAL_PAGE_TEXT: dict[str, tuple[str, str]] = {
    "sessions": ("Сессии", "Активные и завершённые рабочие сессии."),
    "execution": ("Выполнение", "Текущие и недавние запуски агентов."),
    "git": ("Git", "Состояние репозиториев, веток и worktree."),
    "artifacts": ("Артефакты", "Созданные в проектах рабочие материалы."),
    "reports": ("Отчёты", "Результаты и заключения по выполненным запускам."),
    "agents": ("Агенты", "Доступность провайдеров и текущая загрузка."),
}

_OPERATION_STATE_LABELS = {
    "PREPARED": "Подготовлен",
    "QUEUED": "В очереди",
    "RUNNING": "Выполняется",
    "COMPLETED": "Завершён",
    "FAILED": "Ошибка",
    "CANCELLED": "Отменён",
    "INTERRUPTED": "Прерван",
    "UNKNOWN": "Неизвестно",
    "ok": "Готов",
    "missing": "Не найден",
    "invalid": "Некорректен",
    "non_git": "Не является репозиторием Git",
    "available": "Доступен",
    "not_installed": "Не установлен",
    "login_required": "Требуется вход",
    "daemon_unavailable": "Служба недоступна",
    "no_models": "Нет моделей",
    "status_unavailable": "Статус недоступен",
    "auth_unknown": "Авторизация не подтверждена",
    "contract_pending": "Контракт ожидается",
}


def operation_value(key: str, value: object) -> str:
    if value in (None, ""):
        return "—"
    if key in {"state", "readiness"}:
        text = str(value)
        return _OPERATION_STATE_LABELS.get(text, text if any("А" <= char <= "я" for char in text) else "Неизвестно")
    return str(value)

# --- Top bar --------------------------------------------------------------
PROJECT_SWITCHER_PLACEHOLDER = "Выберите проект"
PROJECT_SWITCHER_ACCESSIBLE = "Переключатель проектов"
PROJECT_SWITCHER_DESCRIPTION = "Выбор проекта появится в следующем инкременте"
STATUS_AREA_ACCESSIBLE = "Область статуса"
REFRESH_TEXT = "Обновить"
REFRESH_TOOLTIP = "Обновить текущую страницу"

# --- Home page ------------------------------------------------------------
HOME_TITLE = "Главная"
HOME_SUBTITLE = "Сводка по всем проектам: проекты, запуски и активность."
HOME_EMPTY_TITLE = "Рабочий стол ещё не подключён к данным"
HOME_EMPTY_BODY = (
    "Здесь появится сводка по всем проектам — проекты, активные запуски, "
    "недавняя активность, артефакты и отчёты. Подключение к «живым» данным "
    "появится в следующем инкременте. Пока настройте проект, чтобы начать."
)
HOME_EMPTY_ACTION = "Перейти к проектам"

# Workspace Home section titles + header-metric captions (D2C §1).
HOME_SECTION_PROJECTS = "Проекты"
HOME_SECTION_ACTIVE_RUNS = "Активные запуски"
HOME_SECTION_RECENT_RUNS = "Недавние запуски"
HOME_SECTION_ARTIFACTS = "Артефакты"
HOME_SECTION_REPORTS = "Отчёты"
HOME_SECTION_ACTIVITY = "Активность"
HOME_SECTION_EMPTY = "Нет данных"
AIOS_CORE_TITLE = "AIOS Core"
AIOS_CORE_SOURCE = "Источник: {source}"
AIOS_CORE_VERSION = "Версия: {version}"
AIOS_CORE_HEALTH = "Здоровье: {health}"
AIOS_CORE_CAPABILITIES = "Возможности: {items}"
AIOS_CORE_GATES = "Гейты приёмки: {items}"
AIOS_CORE_EVIDENCE = "Доказательства: {items}"
PROVIDERS_TITLE = "Провайдеры ИИ"
PROVIDERS_ACCESSIBLE_DESCRIPTION = (
    "Доступность провайдеров по подтверждённым локальным признакам."
)
_PROVIDER_READINESS_LABELS = {
    "available": "доступен",
    "auth_unknown": "установлен, авторизация не подтверждена",
    "login_required": "установлен, требуется вход",
    "daemon_unavailable": "служба недоступна",
    "no_models": "служба доступна, модели не найдены",
    "status_unavailable": "статус недоступен",
    "not_installed": "не установлен",
    "contract_pending": "контракт AIOS ожидается",
}
HOME_LOADING = "Загрузка данных…"
HOME_LOADING_ACCESSIBLE_DESCRIPTION = (
    "Содержимое главной страницы обновляется. Дождитесь окончания загрузки."
)
HOME_LOAD_ERROR = "Не удалось загрузить данные"
HOME_LOAD_ERROR_DETAIL = (
    "Внутренняя ошибка скрыта из соображений безопасности. Повторите обновление."
)
UNKNOWN_REPOSITORY_STATE = "Неизвестное состояние репозитория"
UNKNOWN_RUN_STATE = "Неизвестное состояние запуска"
UNKNOWN_ACTIVITY_EVENT = "Неизвестное событие"
UNKNOWN_REPORT_VERDICT = "Неизвестный результат проверки"
METRIC_PROJECTS = "Проекты"
METRIC_ACTIVE_RUNS = "Активные запуски"
METRIC_RECENT_RUNS = "Недавние запуски"
METRIC_ARTIFACTS = "Артефакты"
METRIC_REPORTS = "Отчёты"

_AIOS_READINESS_LABELS = {
    "ready": "Готов",
    "not_ready": "Не готов",
    "offline": "Нет связи",
    "error": "Ошибка контракта",
    "contract_pending": "Контракт ожидается",
}
_AIOS_SOURCE_LABELS = {
    "configuration": "конфигурация",
    "AIOS API": "AIOS API",
}


def aios_readiness_label(value: object) -> str:
    return _AIOS_READINESS_LABELS.get(str(value or ""), "Статус неизвестен")


def aios_source_label(value: object) -> str:
    return _AIOS_SOURCE_LABELS.get(str(value or ""), str(value or "не указан"))


def aios_health_label(value: object) -> str:
    return {"healthy": "исправен", "degraded": "ограничен"}.get(
        str(value or ""), str(value or "не подтверждено")
    )


def provider_readiness_label(value: object) -> str:
    return _PROVIDER_READINESS_LABELS.get(str(value or ""), "статус неизвестен")

# --- Projects page --------------------------------------------------------
PROJECTS_TITLE = "Проекты"
PROJECTS_SUBTITLE = "Статус репозитория, worktree и настройка по каждому проекту."
PROJECTS_EMPTY_TITLE = "Настройка проектов ещё не подключена"
PROJECTS_EMPTY_BODY = (
    "Здесь появится просмотр и изменение пути к репозиторию каждого проекта, "
    "а также состояние его worktree и репозитория. Раздел активируется в "
    "следующем инкременте на основе существующего сервиса настройки проектов."
)
PROJECTS_REPOSITORY_GROUP = "Репозиторий"
PROJECTS_PATH_LABEL = "Путь к репозиторию"
PROJECTS_PATH_PLACEHOLDER = "Укажите абсолютный путь к каталогу"
PROJECTS_SAVE_ACTION = "Сохранить"
PROJECTS_CLEAR_ACTION = "Очистить"
PROJECTS_PATH_NOT_CONFIGURED = "Путь пока не настроен."
PROJECTS_PATH_SAVED = "Путь сохранён."
PROJECTS_PATH_CLEARED = "Путь очищен."
PROJECTS_SAVE_ERROR = "Не удалось сохранить путь. Повторите попытку."
PROJECTS_LOAD_ERROR_TITLE = "Не удалось загрузить проекты"
PROJECTS_LOAD_ERROR_BODY = (
    "Проверьте доступность файла конфигурации и повторно откройте раздел."
)
PROJECT_DISPLAY_NAMES_RU: dict[str, str] = {
    "AICC": APP_TITLE,
    "AIOS": "AIOS",
    "AICOS": "AICOS",
    "PRODUCT": "Продукт AIOS",
    "ECOSYSTEM": "Экосистема",
    "BANK": "Банковская стратегия",
    "LEGAL": "Юридические вопросы",
    "BUSINESS": "Бизнес",
    "PERSONAL": "Личное",
}


def project_display_name(project_id: str, fallback: str) -> str:
    """Russian presentation name while preserving canonical product acronyms."""
    return PROJECT_DISPLAY_NAMES_RU.get(project_id, fallback)


def project_editor_accessible_name(display_name: str) -> str:
    return f"Настройка проекта «{display_name}»"


def project_path_accessible_name(display_name: str) -> str:
    return f"Путь к репозиторию проекта «{display_name}»"


def project_save_accessible_name(display_name: str) -> str:
    return f"Сохранить путь проекта «{display_name}»"


def project_clear_accessible_name(display_name: str) -> str:
    return f"Очистить путь проекта «{display_name}»"

# --- Settings page --------------------------------------------------------
SETTINGS_TITLE = "Настройки"
SETTINGS_SUBTITLE = "Внешний вид, окно и параметры рабочего пространства."
SETTINGS_APPEARANCE_GROUP = "Внешний вид"
SETTINGS_APPEARANCE_ACCESSIBLE = "Настройки внешнего вида"
THEME_LIGHT = "Светлая"
THEME_DARK = "Тёмная"
THEME_SYSTEM = "Системная (как в macOS)"
SETTINGS_DENSITY_GROUP = "Плотность интерфейса"
SETTINGS_DENSITY_ACCESSIBLE = "Настройки плотности интерфейса"
DENSITY_COMFORTABLE = "Комфортная"
DENSITY_COMPACT = "Компактная"
SETTINGS_WINDOW_GROUP = "Окно"
SETTINGS_RESET_GEOMETRY = "Сбросить размер и положение окна"
SETTINGS_RESET_GEOMETRY_DESCRIPTION = (
    "Вернуть стандартные размер и положение окна при следующем запуске"
)
SETTINGS_WORKSPACE_GROUP = "Рабочее пространство"
SETTINGS_SELECTED_PROJECT_LABEL = "Проект по умолчанию"
SETTINGS_SELECTED_PROJECT_DESCRIPTION = (
    "Идентификатор проекта, который будет выбран при следующем запуске"
)
SETTINGS_SELECTED_PROJECT_PLACEHOLDER = "Например, проект-1"
SETTINGS_SAVE_WORKSPACE = "Сохранить параметры рабочего пространства"
SETTINGS_SAVED = "Параметры сохранены"

# --- First-run wizard (D-1) -----------------------------------------------
FIRST_RUN_TITLE = "Первый запуск"
FIRST_RUN_SUBTITLE = (
    "Проверим окружение и настроим рабочее пространство — это займёт минуту."
)
FIRST_RUN_CHECKS_GROUP = "Проверка окружения"
FIRST_RUN_CHECKS_ACCESSIBLE = "Список проверок окружения"
FIRST_RUN_STATUS_LABELS = {
    "ok": "Готово",
    "warning": "Внимание",
    "error": "Ошибка",
}
FIRST_RUN_RECHECK = "Перепроверить"
FIRST_RUN_WORKSPACE_GROUP = "Рабочее пространство"
FIRST_RUN_WORKSPACE_LABEL = "Каталог рабочего пространства"
FIRST_RUN_WORKSPACE_DESCRIPTION = (
    "Здесь приложение будет хранить данные, артефакты и отчёты. "
    "Каталог будет создан, если его ещё нет."
)
FIRST_RUN_BROWSE = "Выбрать…"
FIRST_RUN_CONTINUE = "Продолжить"
FIRST_RUN_QUIT = "Выйти"
FIRST_RUN_ERRORS_NOTE = (
    "Часть обязательных зависимостей не найдена — приложение запустится, "
    "но соответствующие функции будут недоступны до их установки."
)

# --- Startup error boundary (D-1) -----------------------------------------
STARTUP_ERROR_WINDOW_TITLE = "AI Command Center — ошибка запуска"
STARTUP_ERROR_DETAILS_HINT = "Техническая информация — в разделе «Показать детали»."


# --- Status badges / project cards (D2C) ----------------------------------
BADGE_SENSITIVE = "Конфиденциально"
PROJECT_TASKS_LABEL = "Задачи"
PROJECT_ACTIVE_RUNS_LABEL = "Активные запуски"
PROJECT_CONFIGURE_ACTION = "Настроить путь к репозиторию"

# Repository health labels, keyed by `_discover_worktrees`'s repository_state
# (WORKSPACE_HOME_SPEC.md §3 — this mapping must not drift).
REPO_STATE_LABELS: dict[str, str] = {
    "unconfigured": "Не настроен",
    "invalid_path": "Путь недействителен",
    "not_git_repo": "Не git-репозиторий",
    "ok": "Настроен",
}


# Run states (WORKSPACE_HOME_SPEC.md §5/§6), keyed by runtime.db state strings.
RUN_STATE_LABELS: dict[str, str] = {
    "PREPARED": "Подготовлен",
    "QUEUED": "В очереди",
    "RUNNING": "Выполняется",
    "COMPLETED": "Завершён",
    "FAILED": "Ошибка",
    "CANCELLED": "Отменён",
    "INTERRUPTED": "Прерван",
    "UNKNOWN": "Неизвестно",
}

# Activity events (WORKSPACE_HOME_SPEC.md §6), keyed by activity_log event_type.
ACTIVITY_EVENT_LABELS: dict[str, str] = {
    "run_started": "Запуск начат",
    "run_completed": "Запуск завершён",
    "run_failed": "Запуск завершился ошибкой",
}

TASK_TYPE_LABELS: dict[str, str] = {
    "implementation": "Реализация",
    "review": "Проверка",
    "audit": "Аудит",
    "testing": "Тестирование",
}
RUN_SOURCE_LABELS: dict[str, str] = {
    "v2": "Новый контур выполнения",
    "legacy": "Прежний контур выполнения",
}

# A report file with no linked run (WORKSPACE_HOME_SPEC.md §8) — shown without a
# verdict badge rather than a misleading blank one.
REPORT_UNMATCHED_NOTE = "Без привязки к запуску"


def task_type_label(task_type: str | None) -> str:
    if not task_type:
        return ""
    return TASK_TYPE_LABELS.get(task_type, "Задача")


def run_source_label(source: str | None) -> str:
    if not source:
        return ""
    return RUN_SOURCE_LABELS.get(source, "Источник выполнения")


def page_accessible_name(title: str) -> str:
    """Accessible name for a page given its (already-Russian) visible title."""
    return f"Страница «{title}»"


def theme_accessible_name(label: str) -> str:
    """Accessible name for a theme radio given its (already-Russian) label."""
    return f"Тема: {label}"


def density_accessible_name(label: str) -> str:
    return f"Плотность интерфейса: {label}"
