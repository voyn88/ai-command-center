"""Read-only compatibility projection for installed AI providers.

AIOS is the future authoritative owner. This adapter never launches a provider
and never treats binary presence as proof of authentication.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
import shutil
from collections.abc import Mapping
from typing import Callable, Protocol
from urllib.error import URLError
from urllib.request import urlopen


class ProviderReadiness(str, Enum):
    AVAILABLE = "available"
    AUTH_UNKNOWN = "auth_unknown"
    LOGIN_REQUIRED = "login_required"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    NO_MODELS = "no_models"
    STATUS_UNAVAILABLE = "status_unavailable"
    NOT_INSTALLED = "not_installed"
    CONTRACT_PENDING = "contract_pending"


@dataclass(frozen=True)
class ProviderCapability:
    provider_id: str
    display_name: str
    readiness: ProviderReadiness
    provenance: str
    detail: str | None = None


class ProviderCapabilityPort(Protocol):
    def list_capabilities(self) -> tuple[ProviderCapability, ...]: ...


def _probe_ollama_models() -> tuple[str, ...] | None:
    try:
        with urlopen(  # nosec B310 - literal loopback URL, not derived from input
            "http://127.0.0.1:11434/api/tags", timeout=0.5
        ) as response:
            payload = json.loads(response.read(65_537))
    except (OSError, TimeoutError, URLError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    return tuple(
        item["name"]
        for item in models
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )


class CompatibilityProviderCapabilityClient:
    """Temporary local readiness adapter; contains no execution capability."""

    _PROVIDERS = (
        ("claude", "Claude", "claude", ProviderReadiness.AUTH_UNKNOWN),
        ("codex", "Codex", "codex", ProviderReadiness.AUTH_UNKNOWN),
        ("ollama", "Ollama", "ollama", ProviderReadiness.DAEMON_UNAVAILABLE),
        ("copilot", "GitHub Copilot", "copilot", ProviderReadiness.AUTH_UNKNOWN),
        ("opencode", "OpenCode", "opencode", ProviderReadiness.LOGIN_REQUIRED),
        ("antigravity", "Antigravity", "agy", ProviderReadiness.LOGIN_REQUIRED),
    )

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        ollama_probe: Callable[[], tuple[str, ...] | None] = _probe_ollama_models,
    ) -> None:
        self._which = which
        self._ollama_probe = ollama_probe

    def list_capabilities(self) -> tuple[ProviderCapability, ...]:
        result: list[ProviderCapability] = []
        for provider_id, display_name, binary, installed_state in self._PROVIDERS:
            path = self._which(binary)
            if path is None:
                result.append(
                    ProviderCapability(
                        provider_id,
                        display_name,
                        ProviderReadiness.NOT_INSTALLED,
                        "локальный PATH",
                    )
                )
                continue
            readiness = installed_state
            detail = None
            if provider_id == "ollama":
                try:
                    models = self._ollama_probe()
                except Exception:
                    models = None
                    readiness = ProviderReadiness.STATUS_UNAVAILABLE
                    detail = "Статус службы получить не удалось"
                if models is not None:
                    models = tuple(
                        model.strip()
                        for model in models
                        if isinstance(model, str) and model.strip()
                    )
                if models:
                    readiness = ProviderReadiness.AVAILABLE
                    detail = f"Доступно моделей: {len(models)}"
                elif models == ():
                    readiness = ProviderReadiness.NO_MODELS
                    detail = "Служба доступна, модели не найдены"
            result.append(
                ProviderCapability(
                    provider_id,
                    display_name,
                    readiness,
                    f"локальный PATH ({binary})",
                    detail=detail,
                )
            )
        return tuple(result)


class DisabledProviderCapabilityClient:
    def list_capabilities(self) -> tuple[ProviderCapability, ...]:
        return tuple(
            ProviderCapability(
                provider_id,
                display_name,
                ProviderReadiness.CONTRACT_PENDING,
                "конфигурация Command Center",
                detail="Контракт AIOS ожидается",
            )
            for provider_id, display_name, _binary, _state
            in CompatibilityProviderCapabilityClient._PROVIDERS
        )


def create_provider_capability_client(
    environ: Mapping[str, str] | None = None,
) -> ProviderCapabilityPort:
    values = os.environ if environ is None else environ
    if values.get("AICC_PROVIDER_STATUS_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return DisabledProviderCapabilityClient()
    return CompatibilityProviderCapabilityClient()
