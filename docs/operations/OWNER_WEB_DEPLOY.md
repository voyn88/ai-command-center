# AICC React: server backlog deployment

The existing `web/` client can be built against the server backlog rather than
the desktop's local runtime. Reads use the canonical export, not a new task
database. Commands are separately enabled. The native gateway and existing
platform-token queue API retain their authentication and authorization contracts.

## Build and run

1. Run `npm ci` in `web/`, then build with `VITE_AICC_DATA_SOURCE=server npm run build`.
2. Supply `AICC_MASTER_BACKLOG` pointing to the canonical backlog export, refreshed
   by the existing `command_center.db backlog-export` producer. Do not import it
   into `tasks.json`. The API requires the entire projection to parse successfully.
3. Set `AICC_WEB_DIST` to that build's `web/dist` directory, and set
   `AICC_OWNER_UPSTREAM_KEY` to a random secret of at least 32 characters.
4. Run `uvicorn command_center.webapi.owner_app:create_app --factory` bound to
   loopback only. Without the command configuration below it has no database
   credentials or task mutation routes.
5. Keep HTTPS and owner authentication at the edge. After verifying the owner,
   the proxy must **replace** the incoming Authorization header with
   `Bearer <AICC_OWNER_UPSTREAM_KEY>`. Never put this key in a frontend build.
   Protect all paths, including static assets and API routes.

The UI shows task status, not agent liveness or proof of deployment. Repository
names are mapped to AICC, AIOS or VOYN only when recognized; unmapped records are
explicitly unassigned. Sensitive text follows the native gateway redaction rules.
Snapshots older than three minutes are marked stale. Refresh failures retain the
last received snapshot with a visible warning. The client polls at most once per
minute while visible and prevents overlapping polls.

## Release checks

- Python: `pytest tests/webapi/test_owner_app.py --confcutdir=tests/webapi`
- Frontend: `npx vitest run`, `npm run build`, `npm run lint`
- Public endpoint: missing/wrong owner password returns 401 for `/`, `/api/backlog`
  and an asset URL. Correct credentials return the SPA and current canonical task
  count. A direct backend request without the upstream key returns 401.
- POST/PUT/DELETE/PATCH routes return 405 even with the upstream key.

Deploy the exact built source revision into a separate versioned directory.
Start and check it before switching the authenticated edge upstream. Keep the
previous proxy configuration and running service for rollback. No planner, worker
or queue service needs to be restarted for this deployment.

## Optional single-owner commands

This explicit deployment profile trusts the owner's HTTPS Basic-auth edge, not
client-supplied identity headers. It does not mint a platform identity or bypass
the existing platform-token routes. It is limited to the already authenticated
single owner; native-device tokens cannot call it.

1. Apply migration 0029 with the migrator role and reapply the role inventory.
2. Supply runtime PostgreSQL credentials for **aicc_app**, never migrator/admin.
3. Set `AICC_OWNER_COMMAND_KEY` to a different random secret (32+ characters).
   The read credential is structurally unable to call `/api/owner/*`.
   Bind it to exactly one authenticated username with `AICC_OWNER_ID`.
4. Set `AICC_OWNER_GRANTS_FILE` to a private JSON file mapping the authenticated
   owner name to `["read", "launch", "decision"]`. Missing/invalid grants deny
   actions. The file is read each time, so removing scopes revokes them immediately.
5. Set `AICC_OWNER_ORIGINS` to the exact comma-separated HTTPS origins in use and
   `AICC_OWNER_WIP_LIMIT` to 1–4 (default 2).
6. After Basic auth, proxy `/api/owner/*` with the command credential and replace
   `X-AICC-Owner` with the **authenticated** username. Never forward a caller's
   owner header. All other paths continue using the read credential.

`GET /api/owner/task` provides the current task revision and allowed actions.
`POST /api/owner/commands` requires the same origin, a custom request header,
owner grant, device/session UUID, request UUID and expected task revision.
No confirmation dialog is added: the owner selects the exact task and action.
The session UUID is an audit correlation label, not a separate authentication
factor. The user identity comes only from the authenticated edge.

Launch narrows the existing planner to one task. It preserves eligibility,
dependency/wave gates, review backpressure, repository leases and the WIP cap;
a distinct tick owner prevents a manual request from renewing an active timer's
planner lease. It queues work rather than claiming an agent has already started.

Decisions apply only to task records awaiting an owner answer or refinement:
resume → OPEN, reject → REJECTED, refine → NEEDS_REFINEMENT. The owner's answer
is appended to canonical task context. No decision can set DONE or bypass review.
Status changes and receipts commit in one PostgreSQL transaction. Durable
provenance receipts retain actor, session, policy version, request hash and
outcome. Replays return that outcome; changed payloads with the same key are
refused. Receipt failure rolls back the effect. The UI retains the exact request
after network failure for safe retries.
Refused outcomes are also stable on replay. After receiving one, the UI clears
the pending request; a deliberate new attempt receives a new request UUID.
The immutable receipt includes the original answer, so subsequent task edits
cannot erase it. A manual launch skips fleet-wide ingest and monitor creation.

Run `tests/db/test_owner_commands.py` against an isolated PostgreSQL cluster in
addition to the API and UI tests. Never exercise test decisions against real
owner tasks. To disable actions, remove the command route/key and restart this
web service; the read-only deployment and background fleet remain available.

Native iPhone/Mac release and its separate command gateway remain future work.
