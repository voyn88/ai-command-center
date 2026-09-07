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
    notify.py    — run state-transition detection, an offline per-device
                   notification queue, and bidirectional sync (`sync`/`ack`)
                   (Phase C).

`adapters.py` and `notify.py` exist today; `auth.py` and `api.py` remain
deliberate stubs — `notify.py`'s queue and sync functions take an
`execution_center_api` and a `device_id` directly rather than depending on
either, so they work standalone (a script, a test) today and slot under the
HTTP boundary unchanged once `api.py` exists.
"""

from __future__ import annotations

__all__ = ["adapters", "notify"]
