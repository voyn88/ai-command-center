"""AICC Native Gateway v1 — read-only HTTPS projection API for native clients.

AIOS remains the single owner of tasks, decisions, queues, access and
evidence.  This package serves only a redacted, allowlisted, versioned
projection of that state to the native Mac/iPhone client.  It never talks to
PostgreSQL, SSH, systemd, GitHub or worker filesystems: its only input is a
projection artifact written by the existing AIOS pipeline (see
`native_gateway.source`).

Write operations are deliberately absent from v1.  The future command surface
is specified contract-first in `docs/aicc_native_gateway/COMMAND_GATEWAY_CONTRACT.md`.
"""

GATEWAY_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"
