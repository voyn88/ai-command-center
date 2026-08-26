"""Wave-3 Networking engine (VOYN-W3-NET): contacts, messages, an inbound
feedback→task intake, and Council invitations.

Layering mirrors the other Wave engines: HTTP routes
(:mod:`command_center.api.networking_routes`) → **service**
(:mod:`command_center.networking.service`) → repository
(:mod:`command_center.runtime.db.networking`, schema v23) → db. No business logic
lives in the routes; no data access lives above the repository.
"""
