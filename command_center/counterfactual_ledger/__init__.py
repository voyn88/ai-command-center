"""Counterfactual Ledger (VOYN-MIN-COMP): decisions and the alternatives
considered and rejected alongside them.

Layering mirrors the other Wave engines: HTTP routes
(:mod:`command_center.api.counterfactual_ledger_routes`) → **service**
(:mod:`command_center.counterfactual_ledger.service`) → repository
(:mod:`command_center.runtime.db.counterfactual_ledger`, schema v26) → db. No
business logic lives in the routes; no data access lives above the repository.
"""

from __future__ import annotations
