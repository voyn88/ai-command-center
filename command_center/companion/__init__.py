"""Companion Sync Service — the read/command surface a mobile client talks to.

Importing this package has **no side effects**: no port is bound, no server is
started, no file is written. That rule is deliberate and load-bearing. The
desktop architecture forbids a local HTTP listener outright; this service is
the single, explicit exception to that rule, and an exception that activated
merely by being imported would defeat the point. A listener starts only when a
caller explicitly runs the service.

Layering, mirroring `docs/mobile/API_REQUIREMENTS.md` §1:

    adapters.py  — thin, additive-only reads over the *existing* core. Each one
                   calls one existing function and serializes its existing
                   return shape. Where a screen needs a field no read model
                   returns, the gap is closed by adding to that module, never by
                   this package computing it independently.
    auth.py      — device pairing and token lifecycle (M1D).
    api.py       — the HTTP boundary (M1D).
    notify.py    — notification detection, a durable per-device offline queue,
                   and bidirectional sync (Phase C; VOYN-W0-F5).

`adapters.py` and `notify.py` exist today; `auth.py`/`api.py` remain
deliberate stubs so the package shape matches the specification before the
HTTP boundary they front (which needs the device identity `auth.py` will own)
is written.
"""

from __future__ import annotations

__all__ = ["adapters", "notify"]
