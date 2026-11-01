"""Export the frozen contract artifacts: OpenAPI + DTO 1.0 JSON Schema.

Run from the repository root::

    uv run --with-requirements requirements-gateway.txt \
        python -m native_gateway.contract_export

The exported files under ``docs/aicc_native_gateway/`` are committed, and the
schema-compatibility tests fail whenever the code drifts from the committed
contract — a contract change must therefore be an explicit, reviewed act.
"""

from __future__ import annotations

import json
from pathlib import Path

from .app import GatewayRuntime, create_app
from .auth import DeviceRegistry
from .config import GatewaySettings
from .dto import SnapshotDTO
from .ratelimit import RateLimiter
from .source import FileProjectionSource

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "docs/aicc_native_gateway"


def build_openapi() -> dict:
    settings = GatewaySettings(
        projection_path=Path("unused"), token_file=Path("unused")
    )
    runtime = GatewayRuntime(
        settings=settings,
        source=FileProjectionSource(settings),
        registry=DeviceRegistry(settings.token_file),
        limiter=RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_s),
    )
    return create_app(runtime).openapi()


def build_snapshot_schema() -> dict:
    return SnapshotDTO.model_json_schema()


def main() -> int:
    CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    (CONTRACT_DIR / "openapi.json").write_text(
        json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    schema_dir = CONTRACT_DIR / "schemas"
    schema_dir.mkdir(exist_ok=True)
    (schema_dir / "snapshot-1.0.schema.json").write_text(
        json.dumps(build_snapshot_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"contract artifacts written to {CONTRACT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
