"""Operator CLI: mint and revoke device tokens for the read-only gateway.

Minting prints the plaintext token exactly once and stores only its SHA-256
hash in the registry file.  The registry lives outside the repository (pass
its path explicitly); losing a token means minting a new one — by design
there is no way to recover plaintext from the registry.

Revoking is the other half of the contract `native_gateway.auth` documents
("disabled without deleting — revocation with audit trail"): it flips the
device's `disabled` flag (checked fresh on every request, so a revoked
device is refused on its very next call) and appends an `audit` entry.

Every read-modify-write cycle against the registry file holds a dedicated
`file_lock` for its entire span and lands its write through
`atomic_write_json` (temp file + fsync + `os.replace`), so concurrent
mint/revoke calls cannot silently lose an update and a crash mid-write
cannot truncate or corrupt the registry.

The historical invocation `python -m native_gateway.provision --registry
<file> --device-id <id> [--label <text>]` (no subcommand) still works as an
alias for `mint`, so existing deployment scripts keep functioning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from command_center.storage import atomic_write_json, file_lock

from .auth import SCOPE_READ


def _lock_path(registry_path: Path) -> Path:
    return registry_path.with_name(registry_path.name + ".lock")


def _load_registry(registry_path: Path) -> dict:
    if not registry_path.exists():
        return {"devices": []}
    loaded = json.loads(registry_path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict) and isinstance(loaded.get("devices"), list):
        return loaded
    return {"devices": []}


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
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(_lock_path(registry_path)):
        registry = _load_registry(registry_path)
        if any(d.get("device_id") == device_id for d in registry["devices"]):
            raise SystemExit(f"device_id already provisioned: {device_id}")
        registry["devices"].append(entry)
        atomic_write_json(registry_path, registry)
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
    with file_lock(_lock_path(registry_path)):
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
        atomic_write_json(registry_path, registry)
        registry_path.chmod(0o600)
    return True


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if not raw_argv or raw_argv[0] not in ("mint", "revoke", "-h", "--help"):
        # Back-compat: the original CLI had no subcommands, so scripts written
        # against it invoke this module with bare `--registry`/`--device-id`
        # flags. Treat any invocation that doesn't already name a subcommand
        # as an implicit `mint`.
        raw_argv = ["mint", *raw_argv]

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

    args = parser.parse_args(raw_argv)

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
