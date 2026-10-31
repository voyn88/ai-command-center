"""Gateway settings — environment-driven, no secrets in the repository.

Everything the gateway needs at runtime lives outside the source tree and is
pointed to by environment variables:

- ``AICC_GATEWAY_PROJECTION`` — path to the AIOS-owned projection artifact.
- ``AICC_GATEWAY_TOKEN_FILE`` — path to the device-token registry (hashes
  only; see `native_gateway.provision`).
- ``AICC_GATEWAY_TLS_CERT`` / ``AICC_GATEWAY_TLS_KEY`` — TLS material for
  `native_gateway.serve`; the server refuses to start without them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GatewaySettings:
    projection_path: Path
    token_file: Path
    # Freshness thresholds, seconds since the projection's generated_at.
    fresh_max_age_s: int = 120
    stale_max_age_s: int = 900
    # Per-device rate limit: requests per rolling window.
    rate_limit_requests: int = 120
    rate_limit_window_s: int = 60
    # Page-size bounds for the collection routes.
    default_page_size: int = 50
    max_page_size: int = 200

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> GatewaySettings:
        e = os.environ if env is None else env

        def _int(name: str, default: int) -> int:
            raw = e.get(name, "")
            return int(raw) if raw.isdigit() else default

        return GatewaySettings(
            projection_path=Path(e.get("AICC_GATEWAY_PROJECTION", "")),
            token_file=Path(e.get("AICC_GATEWAY_TOKEN_FILE", "")),
            fresh_max_age_s=_int("AICC_GATEWAY_FRESH_MAX_AGE_S", 120),
            stale_max_age_s=_int("AICC_GATEWAY_STALE_MAX_AGE_S", 900),
            rate_limit_requests=_int("AICC_GATEWAY_RATE_LIMIT_REQUESTS", 120),
            rate_limit_window_s=_int("AICC_GATEWAY_RATE_LIMIT_WINDOW_S", 60),
        )
