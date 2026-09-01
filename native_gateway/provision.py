"""Operator CLI: mint and revoke device tokens for the read-only gateway.

Prints the plaintext token exactly once on mint and stores only its SHA-256
hash in the registry file.  The registry lives outside the repository (pass
its path explicitly); losing a token means minting a new one — by design
there is no way to recover plaintext from the registry.

Revocation disables a device without deleting its entry (audit trail is kept)
so `native_gateway.auth.DeviceRegistry.authenticate` rejects it immediately.

Every read-modify-write cycle against the registry — mint's duplicate check
plus append, revoke's lookup plus disable — holds a dedicated lock file for
its full span (`command_center.storage.file_lock`) and publishes the result
via fsync + `os.replace` (`command_center.storage.atomic_write_json`), so
concurrent CLI invocations can neither race each other into a lost update nor
observe a partially written registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from command_center import storage

from .auth import SCOPE_READ

_KNOWN_COMMANDS = ("mint", "revoke")


class RegistryCorruptError(Exception):
    """The registry file exists but is not readable/parseable device-registry JSON.

    Raised instead of silently substituting an empty registry: a mint() or
    revoke() that fell back to an empty registry on a read failure would go
    on to overwrite the file, permanently deleting every already-provisioned
    device the moment the operator (or a transient I/O error) hit this path.
    Loading must fail closed.
    """


def _lock_path(registry_path: Path) -> Path:
    return registry_path.with_name(registry_path.name + ".lock")


def _load_registry(registry_path: Path) -> dict:
    """Load the registry, or `{"devices": []}` if it has never been created.

    Must be called only while holding `_lock_path(registry_path)`, and the
    existence check must happen under that same lock: checking existence
    before acquiring it lets a revoke racing a device's first-ever mint
    observe "no registry yet" and report the device as unknown instead of
    waiting for the mint to land and revoking it.
    """
    if not registry_path.exists():
        return {"devices": []}
    try:
        loaded = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise RegistryCorruptError(f"cannot read registry {registry_path}: {exc}") from exc
    if not (isinstance(loaded, dict) and isinstance(loaded.get("devices"), list)):
        raise RegistryCorruptError(f"{registry_path} does not hold a valid device registry")
    return loaded


def _write_registry(registry_path: Path, registry: dict) -> None:
    storage.atomic_write_json(registry_path, registry)
    registry_path.chmod(0o600)


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
    with storage.file_lock(_lock_path(registry_path)):
        registry = _load_registry(registry_path)
        if any(d.get("device_id") == device_id for d in registry["devices"]):
            raise SystemExit(f"device_id already provisioned: {device_id}")
        registry["devices"].append(entry)
        _write_registry(registry_path, registry)
    return token


def revoke(registry_path: Path, device_id: str) -> None:
    """Disable every registry entry for `device_id` so it can no longer authenticate."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with storage.file_lock(_lock_path(registry_path)):
        registry = _load_registry(registry_path)
        matches = [d for d in registry["devices"] if d.get("device_id") == device_id]
        if not matches:
            raise SystemExit(f"device_id not found: {device_id}")
        for device in matches:
            device["disabled"] = True
        _write_registry(registry_path, registry)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mint or revoke a read-only device token")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mint_parser = subparsers.add_parser("mint", help="Mint a new device token")
    mint_parser.add_argument("--registry", required=True, type=Path)
    mint_parser.add_argument("--device-id", required=True)
    mint_parser.add_argument("--label", default="")

    revoke_parser = subparsers.add_parser("revoke", help="Revoke an existing device token")
    revoke_parser.add_argument("--registry", required=True, type=Path)
    revoke_parser.add_argument("--device-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Back-compat: the original CLI had no subcommand at all (every invocation
    # minted). Only insert the default when the caller hasn't already named a
    # subcommand, so `--help`/`-h` and the new `revoke` form keep working.
    if not raw_argv or raw_argv[0] not in (*_KNOWN_COMMANDS, "-h", "--help"):
        raw_argv = ["mint", *raw_argv]
    args = _build_parser().parse_args(raw_argv)

    if args.command == "mint":
        token = mint(args.registry, args.device_id, args.label)
        print("Device token (shown once, store it in the device keychain):")
        print(token)
    else:
        revoke(args.registry, args.device_id)
        print(f"Revoked device: {args.device_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
