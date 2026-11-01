# Fleet status and lifecycle (VOYN-MIN-FARM)

The acceptance this closes: **10 devices managed by one operational panel.**
"Devices" are the `worker_host` rows in `principal` — the identity table
`0003_worker_enrollment` created, one row per execution host admitted through
the enrolment protocol (see `docs/postgres-foundation.md` and
`docs/operations/WORKER_CREDENTIAL_ROTATION.md`). Before this, an operator
answered "what does the fleet look like right now" by reading `principal`,
`principal_credential_public` and `principal_event` separately and
correlating them by hand — no single query, and no CLI surface, did it.

`command_center.db.fleet_admin.FleetAdmin` is that single query, and
`python -m command_center.db fleet-status` / `fleet-suspend` are its CLI
surface — the operational panel. Both are additive: no new table, no new
grant, no new privileged function. They read `principal` /
`principal_credential_public` / `principal_event` under whatever role
connects, and `fleet-suspend` calls the existing `identity_revoke_principal`.

## Usage

```bash
# Every enrolled device, newest first: state, host, live credential expiry,
# and the most recent audit event, in one call.
AICC_PG_USER=aicc_app ... python -m command_center.db fleet-status

# Narrow to devices that need attention.
AICC_PG_USER=aicc_app ... python -m command_center.db fleet-status --state suspended

# Suspend a device and revoke its live credential(s). Operator-only: revoking
# a host is an incident decision, not a routine one (see `command_center/db/
# roles.py`, `_OPERATOR_FUNCTIONS`). A control-plane connection gets
# `psycopg.errors.InsufficientPrivilege`, not a soft refusal — the grant
# graph enforces the boundary, `fleet_admin.py` does not re-implement it.
AICC_PG_USER=aicc_operator ... python -m command_center.db fleet-suspend worker:edge-07 --reason "incident: compromised host"
```

## What "managed" means here

- **Visibility**: `fleet-status` is the one-panel inventory — lifecycle state
  (`active` / `suspended` / `retired`), host, live credential expiry (or
  "none issued"), and the last audit event, per device.
- **Lifecycle**: `fleet-suspend` is the one lifecycle mutation the shipped
  protocol exposes beyond enrolment itself — suspending a device revokes
  every live credential it holds and disables its database login
  (`identity_revoke_principal` → `identity_disable_role`). There is no
  `retired` transition or reinstatement lever yet; readmitting a suspended
  host is the existing `enroll_mint_ticket(..., purpose => 're_enroll')` path
  an operator already has (`docs/operations/WORKER_CREDENTIAL_ROTATION.md`).
- **Scale**: `fleet-status` takes no per-device round trip — it is proved
  against a ten-device fleet in `tests/db/test_fleet_admin.py`
  (`test_the_whole_fleet_is_one_query`), which is the acceptance criterion
  as a test rather than a claim.
