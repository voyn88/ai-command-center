"""Wave-3 Marketplace domain package (VOYN-W3-MARKET).

The operator-facing catalogue of installable modules/add-ons and its install
path, layered routes → **service** → repository → db:

* :mod:`command_center.marketplace.installer` — the injectable ``Installer``
  interface and the safe default used in this baseline wave.
* :mod:`command_center.marketplace.service` — the lifecycle policy: register a
  listing, install it once (recording who/when/what), read its install log.

Persistence lives in :mod:`command_center.runtime.db.marketplace` (schema v21);
the HTTP surface in :mod:`command_center.api.marketplace_routes`.
"""

from __future__ import annotations
