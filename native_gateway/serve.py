"""TLS-only runner for the gateway.

The native client hard-fails non-HTTPS endpoints (`GatewayConfiguration`
rejects any non-https scheme), and this runner enforces the mirror-image
server invariant: **no TLS material, no listener**.  There is no plaintext
fallback flag by design — a "temporary" HTTP mode is exactly the kind of
crutch the delivery rules prohibit.

Usage::

    AICC_GATEWAY_PROJECTION=/path/to/projection.json \
    AICC_GATEWAY_TOKEN_FILE=/path/to/device_tokens.json \
    AICC_GATEWAY_TLS_CERT=/path/to/cert.pem \
    AICC_GATEWAY_TLS_KEY=/path/to/key.pem \
    uv run --with-requirements requirements-gateway.txt \
        python -m native_gateway.serve --host 0.0.0.0 --port 8443

For local development, generate a self-signed localhost certificate with
``native_gateway/dev/gen_dev_cert.sh``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


class TLSConfigurationError(RuntimeError):
    pass


def resolve_tls(env: dict[str, str] | None = None) -> tuple[Path, Path]:
    """Return (certfile, keyfile) or raise — never fall back to plaintext."""
    e = os.environ if env is None else env
    cert = e.get("AICC_GATEWAY_TLS_CERT", "")
    key = e.get("AICC_GATEWAY_TLS_KEY", "")
    if not cert or not key:
        raise TLSConfigurationError(
            "TLS is mandatory: set AICC_GATEWAY_TLS_CERT and AICC_GATEWAY_TLS_KEY."
        )
    cert_path, key_path = Path(cert), Path(key)
    if not cert_path.is_file() or not key_path.is_file():
        raise TLSConfigurationError("TLS certificate or key file does not exist.")
    return cert_path, key_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AICC Native Gateway v1 (HTTPS only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args(argv)

    try:
        certfile, keyfile = resolve_tls()
    except TLSConfigurationError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from .app import build_default_app

    uvicorn.run(
        build_default_app(),
        host=args.host,
        port=args.port,
        ssl_certfile=str(certfile),
        ssl_keyfile=str(keyfile),
        server_header=False,
        access_log=False,  # access logs would record device ids per URL; keep off by default
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
