"""Storage-free domain logic for the Wave-3 model registry (VOYN-W3-MODELS).

This package holds the parts of the model-registry feature that carry *no*
persistence: the injectable downloader interface a local-model download drives
(so tests never touch the network) and the routing policy — the auto-select
helper (prefer local for cost) and the sensitive-data guard (never route a
context marked sensitive to an external model).

The persistence tier lives in :mod:`command_center.runtime.db.model_registry`
and the orchestration in :mod:`command_center.api.model_registry_service`; this
package is what both call for the rules that are pure functions of their inputs.
"""

from __future__ import annotations

from command_center.models_registry.downloader import (
    Downloader,
    DownloadProgress,
    StubDownloader,
)
from command_center.models_registry.policy import (
    SensitiveModelRoutingError,
    assert_routing_allowed,
    auto_select,
)

__all__ = [
    "Downloader",
    "DownloadProgress",
    "StubDownloader",
    "SensitiveModelRoutingError",
    "assert_routing_allowed",
    "auto_select",
]
