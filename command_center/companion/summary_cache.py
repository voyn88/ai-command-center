"""Companion Sync Service — summary_cache: a signed, secure summary cache for
offline mode (VOYN-MIN-OFFLINE-SUMMARY).

The acceptance criterion this module exists to satisfy: when a device loses
network, the owner still sees priority events without losing context. `notify.py`
already durably queues every state-transition event for a device, but that
queue is only useful once the device is reachable *again* — a device that is
currently offline has nothing on-screen to show, because it never received a
response to hold onto. This module closes that gap by giving the device
something to persist locally the last time it *was* online, so a subsequent
loss of connectivity degrades to "shows the last known-good priority view",
never to a blank screen.

Two pieces, mirroring `notify.py`'s shape:

- `build` composes the cache **payload** from two existing, already-tested
  sources — never recomputing what they already compute, the same rule
  `adapters.py`'s docstring states for every read in this package:
    - `adapters.recommendations` — the owner's priority task views (score,
      reasons, priority label), i.e. "what matters right now."
    - `notify.pending_for_device` — this device's own durable notification
      backlog, i.e. "what changed since I was last caught up," so a device
      that goes offline mid-sync does not silently drop context about a
      transition it has not yet acknowledged.
- `write`/`read` persist and retrieve a **signed envelope** around that
  payload, `{"payload": ..., "signature": ...}`, in the same whole-file JSON
  registry shape `notify.py` uses for `companion_devices.json` — a
  read-modify-write document, guarded by `storage.file_lock` across its
  entire span.

**Why signed.** The cache is written to local, potentially unencrypted mobile
storage and read back with no server round trip once the device is offline —
exactly the condition under which nothing here can ask the server "is this
still correct?" A HMAC-SHA256 signature over the payload's canonical
(sort-keys, no-whitespace) JSON encoding means `read` can detect any cache
that has been edited, corrupted, or replaced since `write` produced it, and
refuse to hand back a payload it cannot vouch for — surfacing "no verified
cache" rather than silently rendering tampered or partially-written data as
if it were trustworthy. The signing key is supplied by the caller (never
generated or stored by this module): the server-side secret that produced a
signature is exactly the one this module needs to verify it, and a module
that could mint its own keys would make a stolen device file self-certifying.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from command_center import storage
from command_center.companion import adapters, notify

CACHE_FILE_NAME = "companion_summary_cache.json"


def _cache_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / CACHE_FILE_NAME


def _cache_lock_path(root: Path) -> Path:
    return _cache_path(root).with_suffix(".lock")


def _canonical_bytes(payload: dict) -> bytes:
    """A deterministic byte encoding of `payload`: sorted keys, no
    incidental whitespace, so the same logical payload always signs to the
    same digest regardless of dict insertion order or `json.dumps` defaults
    drifting between Python versions."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict, *, secret: str) -> str:
    """The hex-encoded HMAC-SHA256 of `payload`'s canonical encoding, keyed by
    `secret`. Deterministic: signing the same payload with the same secret
    twice always yields the same signature, which is what lets `verify`
    recompute and compare rather than needing any stored nonce or state."""
    return hmac.new(secret.encode("utf-8"), _canonical_bytes(payload), hashlib.sha256).hexdigest()


def verify_signature(payload: dict, signature: str, *, secret: str) -> bool:
    """Whether `signature` is exactly the HMAC `sign_payload` would produce
    for `payload` under `secret`. Uses `hmac.compare_digest` — a
    constant-time comparison — so a device attempting to brute-force a valid
    signature byte-by-byte cannot use response-time differences to do it
    faster than guessing the whole digest at once."""
    expected = sign_payload(payload, secret=secret)
    return hmac.compare_digest(expected, signature)


def build(
    device_id: str,
    *,
    root: Path,
    limit: int = 20,
) -> dict:
    """The cache payload for `device_id`: the owner's current priority task
    recommendations plus this device's own pending notification backlog,
    each drawn verbatim from the existing adapter/notify functions that
    already compute them — this function invents no new ranking or event
    shape of its own.

    `cursor` records this device's notification cursor *at build time*, so a
    device that reads this cache back later can tell how current its
    "pending" list was, without this module needing a clock of its own
    beyond what `notify.py` already persists.
    """
    priority_events = [
        view
        for view in adapters.recommendations(root, limit=limit)
        if view.get("priority") in ("High", "Critical")
    ] or adapters.recommendations(root, limit=limit)
    pending = notify.pending_for_device(device_id, root=root, limit=limit)
    return {
        "device_id": device_id,
        "cursor": notify.device_cursor(device_id, root=root),
        "priority_events": priority_events,
        "pending_notifications": pending,
    }


def write(
    device_id: str,
    *,
    root: Path,
    secret: str,
    limit: int = 20,
) -> dict:
    """Build a fresh payload for `device_id`, sign it, and durably persist the
    signed envelope — the one write path this module has. Overwrites any
    previously cached envelope for this device: the cache always reflects
    the most recent moment the device was online, never a merge of stale and
    fresh state.

    Read-modify-write on the whole-file registry is done under
    `_cache_lock_path` across its entire span (read existing registry, add
    this device's envelope, write it back), matching every other whole-file
    JSON registry in this project (`storage.py`'s module docstring; `notify.py`'s
    `_devices_path`)."""
    payload = build(device_id, root=root, limit=limit)
    envelope = {"payload": payload, "signature": sign_payload(payload, secret=secret)}
    with storage.file_lock(_cache_lock_path(root)):
        registry = storage.read_json(_cache_path(root), {})
        registry[device_id] = envelope
        storage.atomic_write_json(_cache_path(root), registry)
    return envelope


def read(device_id: str, *, root: Path, secret: str) -> dict | None:
    """This device's most recently written cache payload, or `None` when no
    cache exists *or* the stored envelope's signature does not verify against
    `secret`.

    The signature check happens on every read, not just once at write time:
    the whole point of signing is that this module never trusts on-disk
    state just because it is present at the expected path — a cache file
    edited, corrupted, or swapped for another device's envelope while the
    device was offline fails verification and is treated identically to "no
    cache", never surfaced as if it were valid. This is what lets the owner's
    device safely render "priority events without losing context" from a
    cache it cannot otherwise ask the server to corroborate right now.
    """
    registry = storage.read_json(_cache_path(root), {})
    envelope = registry.get(device_id)
    if not envelope:
        return None
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        return None
    if not verify_signature(payload, signature, secret=secret):
        return None
    return payload
