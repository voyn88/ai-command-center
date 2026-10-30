# AICC React: server backlog deployment

The existing `web/` client can be built against the server backlog rather than
the desktop's local runtime. This is a read-only deployment profile, not a new
task database. The native gateway and privileged queue API are unchanged.

## Build and run

1. Run `npm ci` in `web/`, then build with `VITE_AICC_DATA_SOURCE=server npm run build`.
2. Supply `AICC_MASTER_BACKLOG` pointing to the canonical backlog export, refreshed
   by the existing `command_center.db backlog-export` producer. Do not import it
   into `tasks.json`. The API requires the entire projection to parse successfully.
3. Set `AICC_WEB_DIST` to that build's `web/dist` directory, and set
   `AICC_OWNER_UPSTREAM_KEY` to a random secret of at least 32 characters.
4. Run `uvicorn command_center.webapi.owner_app:create_app --factory` bound to
   loopback only. It has no database credentials and no task mutation routes.
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

## Scope still outstanding

This mode does not enqueue agents or submit owner decisions. Those operations
must use the existing platform identity and authorization boundary. Native
iPhone/Mac release and its gateway remain separate delivery work.
