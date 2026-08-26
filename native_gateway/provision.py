"""Operator CLI: mint and revoke device tokens for the read-only gateway.

Minting prints the plaintext token exactly once and stores only its SHA-256
hash in the registry file.  The registry lives outside the repository (pass
its path explicitly); losing a token means minting a new one — by design
there is no way to recover plaintext from the registry.

Revoking is the other half of the contract `native_gateway.auth` documents
("disabled without deleting — revocation with audit trail"): before this
module grew a `revoke` command, disabling a device meant hand-editing the
registry JSON, which is how ``tests/native_gateway/test_auth_and_errors.py``
exercised the ``disabled`` check and left no operator-facing lever or audit
trail behind it.
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


def revoke(registry_path: Path, device_id: str, reason: str) -> bool:
    """Disable a device token in place and record why.

    Idempotent — revoking an already-disabled device is not an error, the
    same contract ``identity_revoke_principal()`` gives the Postgres side of
    this protocol (``command_center/db/sql/0003_worker_enrollment.up.sql``).
    Returns whether this call was the one that disabled it.

    An unknown ``device_id`` *is* an error: unlike re-revoking, it is not a
    state the registry can already be in, and is far more likely a typo the
    operator wants to hear about than a device to silently skip.
    """
    if not registry_path.exists():
        raise SystemExit(f"registry not found: {registry_path}")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    devices = registry.get("devices") if isinstance(registry, dict) else None
    if not isinstance(devices, list):
        raise SystemExit(f"registry not found: {registry_path}")
    device = next(
        (d for d in devices if isinstance(d, dict) and d.get("device_id") == device_id),
        None,
    )
    if device is None:
        raise SystemExit(f"unknown device_id: {device_id}")
    if device.get("disabled"):
        return False

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    device["disabled"] = True
    device["disabled_at"] = now
    device["disabled_reason"] = reason
    registry.setdefault("audit", []).append(
        {"action": "revoke", "device_id": device_id, "reason": reason, "at": now}
    )
    registry_path.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    registry_path.chmod(0o600)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage read-only device tokens")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mint_parser = subparsers.add_parser("mint", help="Mint a new device token")
    mint_parser.add_argument("--registry", required=True, type=Path)
    mint_parser.add_argument("--device-id", required=True)
    mint_parser.add_argument("--label", default="")

    revoke_parser = subparsers.add_parser("revoke", help="Disable a device token")
    revoke_parser.add_argument("--registry", required=True, type=Path)
    revoke_parser.add_argument("--device-id", required=True)
    revoke_parser.add_argument("--reason", required=True)

    args = parser.parse_args(argv)

    if args.command == "mint":
        token = mint(args.registry, args.device_id, args.label)
        print("Device token (shown once, store it in the device keychain):")
        print(token)
        return 0

    changed = revoke(args.registry, args.device_id, args.reason)
    print("revoked" if changed else "already revoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
