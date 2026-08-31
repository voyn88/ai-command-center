"""Operator CLI: mint and revoke device tokens for the read-only gateway.

Prints a freshly minted plaintext token exactly once and stores only its
SHA-256 hash in the registry file.  The registry lives outside the
repository (pass its path explicitly); losing a token means minting a new
one — by design there is no way to recover plaintext from the registry.
Revocation disables an existing entry (keeping it, and its audit trail, in
the registry) so `auth.DeviceRegistry.authenticate` stops accepting it.

Every read-modify-write cycle against the registry — minting a new device or
disabling an existing one — is protected end to end by a dedicated lock file
(`command_center.storage.file_lock`) and published with
`command_center.storage.atomic_write_json`, so two operators racing a mint
against a mint, or a mint against a revoke, can neither lose an update nor
observe a half-written registry.

CLI usage:

    python -m native_gateway.provision mint --registry <path> --device-id <id> [--label <text>]
    python -m native_gateway.provision revoke --registry <path> --device-id <id>

The pre-subcommand form `python -m native_gateway.provision --registry <path>
--device-id <id>` (documented and used by existing deployment scripts before
`revoke` was added) keeps working: a command-less invocation is treated as
`mint` for backward compatibility.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from command_center import storage

from .auth import SCOPE_READ

REGISTRY_LOCK_TIMEOUT_SECONDS = 30.0


def _lock_path(registry_path: Path) -> Path:
    return registry_path.with_name(registry_path.name + ".lock")


@contextlib.contextmanager
def _registry_lock(registry_path: Path):
    """Mutual exclusion for the *entire* read-modify-write cycle on
    `registry_path` — every writer below (`mint`, `revoke`) acquires this
    before its first read and holds it through the final write, never just
    around the write itself. Creating the parent directory happens before
    acquiring the lock (the lock file itself lives there), and *before* any
    check of whether the registry exists, so a revoke racing the very first
    mint waits for that mint's write instead of observing a transient
    "missing registry" and failing fast.
    """
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with storage.file_lock(_lock_path(registry_path), timeout=REGISTRY_LOCK_TIMEOUT_SECONDS):
        yield


def _load_registry(registry_path: Path) -> dict:
    loaded = storage.read_json(registry_path, {"devices": []})
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
    with _registry_lock(registry_path):
        registry = _load_registry(registry_path)
        if any(d.get("device_id") == device_id for d in registry["devices"]):
            raise SystemExit(f"device_id already provisioned: {device_id}")
        registry["devices"].append(entry)
        storage.atomic_write_json(registry_path, registry)
        registry_path.chmod(0o600)
    return token


def revoke(registry_path: Path, device_id: str) -> None:
    """Disable `device_id`'s registry entry so it can no longer authenticate.

    The entry is kept (with `disabled: True`), not deleted, preserving an
    audit trail. Both the "no registry yet" and "no such device" outcomes,
    and the write that disables the match, happen inside the same
    `_registry_lock` acquisition as the read they are based on — see that
    function's docstring for why the existence check in particular must not
    happen before the lock is held.
    """
    with _registry_lock(registry_path):
        if not registry_path.exists():
            raise SystemExit(f"registry not found: {registry_path}")
        registry = _load_registry(registry_path)
        target = next(
            (d for d in registry["devices"] if d.get("device_id") == device_id), None
        )
        if target is None:
            raise SystemExit(f"device_id not provisioned: {device_id}")
        if not target.get("disabled"):
            target["disabled"] = True
            storage.atomic_write_json(registry_path, registry)
            registry_path.chmod(0o600)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage read-only device tokens")
    subparsers = parser.add_subparsers(dest="command")

    mint_parser = subparsers.add_parser("mint", help="Mint a new device token")
    mint_parser.add_argument("--registry", required=True, type=Path)
    mint_parser.add_argument("--device-id", required=True)
    mint_parser.add_argument("--label", default="")

    revoke_parser = subparsers.add_parser("revoke", help="Revoke an existing device token")
    revoke_parser.add_argument("--registry", required=True, type=Path)
    revoke_parser.add_argument("--device-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if not raw_argv or raw_argv[0] not in {"mint", "revoke", "-h", "--help"}:
        # Legacy interface, predating `revoke`: no subcommand meant "mint".
        # Keep accepting it so existing deployment scripts don't break.
        raw_argv = ["mint", *raw_argv]
    args = _build_parser().parse_args(raw_argv)

    if args.command == "revoke":
        revoke(args.registry, args.device_id)
        print(f"Revoked device token: {args.device_id}")
        return 0

    token = mint(args.registry, args.device_id, args.label)
    print("Device token (shown once, store it in the device keychain):")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
