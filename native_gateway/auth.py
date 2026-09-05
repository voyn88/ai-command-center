"""Device/user authentication for the read-only v1 surface.

Model: pre-provisioned per-device bearer tokens with an explicit scope.
Tokens are minted out-of-band by the operator (`python -m
native_gateway.provision`), shown exactly once, and stored **only as SHA-256
hashes** in a registry file outside the repository.  The gateway never stores,
logs or echoes a plaintext token.

v1 issues only the ``read`` scope; every route requires it.  A registry entry
may be disabled without deleting it (revocation with audit trail).  Lookups
compare hash-to-hash via `hmac.compare_digest` (constant-time).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path

SCOPE_READ = "read"


@dataclass(frozen=True)
class Device:
    device_id: str
    token_sha256: str
    scope: str
    disabled: bool = False
    label: str = ""


class DeviceRegistry:
    """Read-only view of the device-token registry file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> list[Device]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return []
        devices = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(devices, list):
            return []
        parsed: list[Device] = []
        for entry in devices:
            if not isinstance(entry, dict):
                continue
            device_id = entry.get("device_id")
            token_hash = entry.get("token_sha256")
            if not isinstance(device_id, str) or not isinstance(token_hash, str):
                continue
            parsed.append(
                Device(
                    device_id=device_id,
                    token_sha256=token_hash.lower(),
                    scope=str(entry.get("scope", SCOPE_READ)),
                    disabled=bool(entry.get("disabled", False)),
                    label=str(entry.get("label", "")),
                )
            )
        return parsed

    def authenticate(self, token: str) -> Device | None:
        """Return the matching enabled device, or None.

        Iterates the full registry unconditionally so timing does not reveal
        whether a token hash prefix exists.
        """
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched: Device | None = None
        for device in self._load():
            if hmac.compare_digest(candidate, device.token_sha256):
                matched = device
        if matched is None or matched.disabled:
            return None
        return matched


def bearer_token(authorization_header: str | None) -> str | None:
    """Extract a bearer token from an Authorization header, or None."""
    if not authorization_header:
        return None
    scheme, _, value = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
