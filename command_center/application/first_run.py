"""First-run setup services for the desktop shell (D-1 "workable out of the box").

Qt-free application layer for the first-run wizard: local dependency health
checks, workspace-root validation, workspace initialization (the directory
layout `command_center.platform.paths.configure_runtime_environment` expects),
and humanization of known startup failures into actionable Russian messages.

Perimeter rules (`tests/architecture/test_desktop_engine_fitness.py`) apply:
no ``subprocess`` — dependency presence comes from ``shutil.which`` and
filesystem/network probes only, following `provider_capabilities.py`'s
"binary presence is never proof of authentication" philosophy.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from command_center.platform.paths import configure_runtime_environment

# --- dependency health checks ----------------------------------------------

_NETWORK_PROBE_HOST = "api.github.com"
_NETWORK_PROBE_PORT = 443
_NETWORK_PROBE_TIMEOUT_S = 2.0


class HealthStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class HealthCheckItem:
    """One row of the first-run dependency checklist."""

    check_id: str
    name: str
    status: HealthStatus
    detail: str
    fix_hint: str | None = None


def _default_network_probe() -> bool:
    try:
        with socket.create_connection(
            (_NETWORK_PROBE_HOST, _NETWORK_PROBE_PORT),
            timeout=_NETWORK_PROBE_TIMEOUT_S,
        ):
            return True
    except OSError:
        return False


def _default_gh_hosts_path() -> Path:
    return Path.home() / ".config" / "gh" / "hosts.yml"


# A top-level hosts.yml mapping key whose host is exactly ``github.com`` or a
# subdomain of it. Anchored per line and per label so a look-alike host such as
# ``evil-github.com:`` or ``github.com.evil.io:`` never matches
# (CodeQL py/incomplete-url-substring-sanitization).
_GH_HOSTS_KEY = re.compile(
    r"^(?:[A-Za-z0-9-]+\.)*github\.com:", flags=re.MULTILINE
)


def _gh_appears_authenticated(hosts_path: Path) -> bool:
    """Filesystem-only signal: ``hosts.yml`` has a github.com host entry.

    Never proof of a valid token — only that ``gh auth login`` was completed at
    some point. A stale token still surfaces later as a per-operation error.
    """
    try:
        content = hosts_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _GH_HOSTS_KEY.search(content) is not None


def run_health_checks(
    *,
    which: Callable[[str], str | None] = shutil.which,
    network_probe: Callable[[], bool] = _default_network_probe,
    gh_hosts_path: Path | None = None,
) -> tuple[HealthCheckItem, ...]:
    """Probe local dependencies; every probe is injectable for unit tests."""
    items: list[HealthCheckItem] = []

    git_path = which("git")
    items.append(
        HealthCheckItem(
            "git",
            "Git",
            HealthStatus.OK if git_path else HealthStatus.ERROR,
            git_path or "Не найден в PATH",
            None if git_path else "Установите: https://git-scm.com/downloads "
            "или `xcode-select --install` (macOS)",
        )
    )

    gh_path = which("gh")
    if gh_path is None:
        items.append(
            HealthCheckItem(
                "gh",
                "GitHub CLI (gh)",
                HealthStatus.ERROR,
                "Не найден в PATH",
                "Установите: `brew install gh`, затем выполните `gh auth login`",
            )
        )
    else:
        hosts = gh_hosts_path if gh_hosts_path is not None else _default_gh_hosts_path()
        authenticated = _gh_appears_authenticated(hosts)
        items.append(
            HealthCheckItem(
                "gh",
                "GitHub CLI (gh)",
                HealthStatus.OK if authenticated else HealthStatus.WARNING,
                gh_path if authenticated else "Установлен, вход не выполнен",
                None if authenticated else "Выполните в терминале: `gh auth login`",
            )
        )

    uv_path = which("uv")
    items.append(
        HealthCheckItem(
            "uv",
            "uv",
            HealthStatus.OK if uv_path else HealthStatus.ERROR,
            uv_path or "Не найден в PATH",
            None
            if uv_path
            else "Установите: `curl -LsSf https://astral.sh/uv/install.sh | sh`",
        )
    )

    docker_path = which("docker")
    items.append(
        HealthCheckItem(
            "docker",
            "Docker (необязательно)",
            HealthStatus.OK if docker_path else HealthStatus.WARNING,
            docker_path or "Не найден в PATH",
            None
            if docker_path
            else "Нужен только для контейнерных сценариев: "
            "https://docs.docker.com/get-docker/",
        )
    )

    try:
        network_ok = network_probe()
    except Exception:
        network_ok = False
    items.append(
        HealthCheckItem(
            "network",
            "Сеть (github.com)",
            HealthStatus.OK if network_ok else HealthStatus.WARNING,
            "Соединение установлено" if network_ok else "Нет соединения",
            None
            if network_ok
            else "Проверьте подключение к интернету и настройки прокси",
        )
    )

    return tuple(items)


def blocking_errors(items: tuple[HealthCheckItem, ...]) -> tuple[HealthCheckItem, ...]:
    """Hard failures worth flagging prominently (the app still starts)."""
    return tuple(item for item in items if item.status is HealthStatus.ERROR)


# --- workspace root configuration ------------------------------------------

_WORKSPACE_CHILDREN = ("data", "generated", "reports")


def default_workspace_root() -> Path:
    return Path.home() / "Projects" / "ai-command-center"


def workspace_is_configured(
    environ: Mapping[str, str] | None = None, *, home: Path | None = None
) -> bool:
    """True when a usable workspace root is already known to this process.

    Mirrors `paths._discover_workspace_root`: an explicit ``AICC_WORKSPACE_ROOT``
    (or an already-exported ``AICC_DATA_DIR``) or a conventional directory with a
    ``data/`` child.
    """
    values = os.environ if environ is None else environ
    explicit = values.get("AICC_WORKSPACE_ROOT")
    if explicit and Path(explicit).expanduser().is_dir():
        return True
    data_dir = values.get("AICC_DATA_DIR")
    if data_dir and Path(data_dir).expanduser().is_dir():
        return True
    base = Path.home() if home is None else home
    for candidate in (
        base / "Projects" / "ai-command-center",
        base / "ai-command-center",
    ):
        if (candidate / "data").is_dir():
            return True
    return False


def validate_workspace_root(raw: str) -> str | None:
    """Return a Russian validation error for the proposed root, or None if OK."""
    text = (raw or "").strip()
    if not text:
        return "Укажите каталог рабочего пространства."
    path = Path(text).expanduser()
    if not path.is_absolute():
        return "Путь должен быть абсолютным (начинаться с /)."
    if path.exists() and not path.is_dir():
        return "По этому пути находится файл, а не каталог."
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    if not os.access(probe, os.W_OK):
        return "Нет прав на запись в этот каталог."
    return None


def initialize_workspace(
    raw: str, environ: MutableMapping[str, str] | None = None
) -> Path:
    """Create the workspace layout and export the runtime environment.

    Creates ``<root>/{data,generated,reports}`` and points the process
    environment at them (the exact contract packaged entrypoints rely on via
    `configure_runtime_environment`). Returns the resolved root.

    Raises ``ValueError`` with the human validation message on invalid input.
    """
    error = validate_workspace_root(raw)
    if error is not None:
        raise ValueError(error)
    root = Path(raw).expanduser().resolve()
    for child in _WORKSPACE_CHILDREN:
        (root / child).mkdir(parents=True, exist_ok=True)
    values = os.environ if environ is None else environ
    values["AICC_WORKSPACE_ROOT"] = str(root)
    values["AICC_DATA_DIR"] = str(root / "data")
    values["AICC_GENERATED_ROOT"] = str(root / "generated")
    values["AICC_REPORTS_ROOT"] = str(root / "reports")
    if environ is None:
        # Keep the canonical helper the one that fills any remaining defaults.
        configure_runtime_environment(root)
    return root


# --- startup error humanization --------------------------------------------


@dataclass(frozen=True)
class StartupFailure:
    """A humanized startup failure: what happened and what to do about it."""

    title: str
    message: str
    hint: str


_UNKNOWN_FAILURE = StartupFailure(
    title="Не удалось запустить приложение",
    message="Произошла непредвиденная ошибка при запуске.",
    hint="Подробности — ниже, в разделе «Показать детали». "
    "Перезапустите приложение; если ошибка повторяется, создайте issue.",
)


def humanize_startup_error(error: BaseException) -> StartupFailure:
    """Map a known startup exception to an actionable Russian message.

    Known cases (D-1 AC): missing/unwritable workspace configuration,
    unauthenticated GitHub CLI, unreachable AIOS backend. Anything else falls
    back to a generic message; the full traceback stays available behind the
    details expander either way.
    """
    text = f"{type(error).__name__}: {error}".lower()

    if isinstance(error, (FileNotFoundError, NotADirectoryError, PermissionError)) or (
        "unable to open database" in text or "aicc_data_dir" in text
    ):
        return StartupFailure(
            title="Рабочее пространство не настроено",
            message="Не удалось открыть каталог данных приложения.",
            hint="Откройте настройку первого запуска заново: удалите параметр "
            "«workspace/root» в настройках или задайте переменную "
            "AICC_WORKSPACE_ROOT на существующий каталог с правом записи.",
        )

    if "aiosstatus" in text or "aios" in text:
        return StartupFailure(
            title="Сервис AIOS недоступен",
            message="Локальные данные доступны, но связаться с ядром AIOS не удалось.",
            hint="Проверьте, что backend AIOS запущен и адрес в конфигурации "
            "верен. Приложение можно перезапустить после восстановления связи.",
        )

    gh_named = re.search(r"\bgh\b", text) is not None or "github" in text
    if gh_named and ("auth" in text or "login" in text or "credentials" in text):
        return StartupFailure(
            title="GitHub CLI не авторизован",
            message="Команды GitHub недоступны без входа в аккаунт.",
            hint="Выполните в терминале `gh auth login` и перезапустите приложение.",
        )

    return _UNKNOWN_FAILURE
