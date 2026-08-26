"""Operator CLI: mint a device token for the read-only gateway.

Prints the plaintext token exactly once and stores only its SHA-256 hash in
the registry file.  The registry lives outside the repository (pass its path
explicitly); losing a token means minting a new one — by design there is no
way to recover plaintext from the registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from .auth import SCOPE_READ


def mint(registry_path: Path, device_id: str, label: str) -> str:
    token = secrets.token_urlsafe(32)
    entry = {
        "device_id": device_id,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "scope": SCOPE_READ,
        "disabled": False,
        "label": label,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    registry = {"devices": []}
    if registry_path.exists():
        loaded = json.loads(registry_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("devices"), list):
            registry = loaded
    if any(d.get("device_id") == device_id for d in registry["devices"]):
        raise SystemExit(f"device_id already provisioned: {device_id}")
    registry["devices"].append(entry)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    registry_path.chmod(0o600)
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mint a read-only device token")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    token = mint(args.registry, args.device_id, args.label)
    print("Device token (shown once, store it in the device keychain):")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
