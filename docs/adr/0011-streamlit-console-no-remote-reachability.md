# ADR-0011: the Streamlit console is never remotely reachable

Status: **Accepted**, superseding four rejected attempts
(`VOYN-W0-AICC-CONSOLE-NO-AUTH`, `-REM`, `-REM-REM`, `-REM-REM-REM`; PRs #408, #433,
#480, #490) to authenticate it instead.

## Context

`app.py` performs privileged git/gh and local-subprocess operations and has no authentication
layer. `tests/test_deployment_exposure.py` and [ARCHITECTURE.md](../../ARCHITECTURE.md) keep every
launch path off-host by default, but `docker-compose.aml.yml` — the image built for external,
bank-side deployment of the AML compliance surface (`docs/aml/ACCEPTANCE_PACKAGE.md`) — has always
shipped an operator escape hatch, `AML_BIND_HOST`, documented as safe "only behind a reviewed
authenticating proxy." No such proxy was ever specified, so the control an external deployment
actually relied on was an operator's promise never to set one environment variable. That is a
compensating control, not an architecture.

Four consecutive attempts to make that promise real — an identity-aware admission layer in front of
Streamlit, reusing the existing `command_center.http_auth` boundary (ADR-0008) — were rejected on
adversarial review, each for a reason rooted in the same fact: Streamlit serves one long-lived
HTTP+WebSocket duplex session per browser tab, and that shape does not compose with the platform's
identity contract.

1. **PR #408.** Authentication was specified only at WebSocket admission. The platform's revocation
   contract (`command_center/http_auth/identity.py`) is immediate — no cache, checked on every
   request — but a design that checks once at connection open and never again lets a revoked
   principal keep a privileged session for the WebSocket's remaining lifetime, silently reintroducing
   the exact staleness window the platform boundary was built to close.
2. **PR #433.** The redo made Streamlit-specific authorization optional and left the revalidation
   interval unbounded — configurable to any value is not a bound. `command_center/http_auth/authz.py`
   is deny-by-default by design (an unconfigured deployment must refuse everyone); an ADR that treats
   its check as optional inverts that default for the one surface with the widest capability in the
   codebase.
3. **PR #480.** Fixing the interval problem needed a proxy that re-validates periodically, which
   needs the caller's credential kept somewhere to re-check it with — contradicting `identity.py`'s
   own stated property, "AICC stores no credential." The redo also missed that `authz.py.load_grants`
   memoizes by `(path, st_mtime)`; a grants-file edit that preserves or collides on mtime can leave a
   withdrawn grant cached past any claimed revalidation bound.
4. **PR #490.** The credential-retention problem was still open, and a browser's WebSocket API
   cannot set an `Authorization` header on the upgrade request at all — there is no request path left
   to authenticate through unless AICC invents a cookie, subprotocol, or query-token credential of its
   own, which is exactly the credential-issuing role `identity.py` was written to avoid taking on.
   That PR's diff also shipped only the ADR text, with no admission or proxy code — the condition
   this task exists to close was still open on disk.

Each fix addressed the previous review's specific objection and produced a new one in the same
family. That pattern — not any one bug — is the signal that gating *this* transport at the
connection-admission layer is not a safely specifiable design, independent of how many more attempts
follow.

## Decision

The Streamlit console — every deployment of `app.py`, including the AML container image — is never
published beyond loopback. This is now the architecture, not a default an operator can widen:

- `docker-compose.aml.yml` publishes the port on the literal `127.0.0.1`. `AML_BIND_HOST` is removed;
  there is no environment variable that can widen the bind, so the widening this ADR chain kept
  trying to gate cannot happen by omission or misconfiguration.
- The other three launch paths (`.streamlit/config.toml`, `scripts/start-ui.sh`,
  `scripts/aml-entrypoint.sh`) are unchanged: they already default to loopback and refuse to start
  silently exposed. They are not remote-access vectors — an operator invoking them with an explicit
  wider flag already holds a shell on the host, the same trust boundary as editing the compose file
  directly.
- Any operator who needs the console from off-host reaches it the way `docker-compose.server.yml`
  already requires for PostgreSQL: an SSH tunnel or a private network segment terminating on the
  loopback-published port, never a published port on a routable interface. That boundary is enforced
  by infrastructure AICC does not run and cannot silently misconfigure, which is precisely the
  property the four rejected designs could not obtain from an in-process proxy.
- `tests/test_deployment_exposure.py` gains a fitness test asserting the compose host bind is a fixed
  loopback literal with no `${...}` interpolation, so the removed variable cannot be quietly
  reintroduced.

This closes the reachability question without authenticating Streamlit's own transport. It does not
retire the console, and it does not claim the in-app MLRO/CO role checks
(`docs/aml/COMPLIANCE_CHECKLIST.md` F-01..F-04) are an identity boundary — they are authorization
over an already-trusted local session, unchanged by this ADR, and orthogonal to the pattern of
review findings above.

## Alternatives considered

- **Identity-aware reverse proxy in front of Streamlit** (option 1 in the task brief). Rejected: four
  attempts, four independent implementability failures rooted in the WebSocket transport itself
  (above), not in any one proxy's code. Revisiting this requires a different transport, not a fifth
  proxy design.
- **Route through the existing AIOS-identity boundary in-process** (option 2). Rejected for the same
  reason: `command_center.http_auth` already provides immediate revocation and deny-by-default
  authorization for AICC's JSON API routes (`command_center/api`, `command_center/webapi`,
  `command_center/dispatch/api.py`), and those routes work precisely because each is a short-lived
  request the boundary can check on every call. Streamlit's one persistent duplex session per tab is
  the part that does not fit that contract; wrapping it in the same boundary does not change its
  shape.
- **Give up remote Streamlit permanently, tie future remote access to a client built on the existing
  JSON API boundary** (option 3). **Accepted.** `command_center.http_auth` already authenticates and
  authorizes the API surface; a future remote-capable client (the native `command_center.desktop`
  shell already landing per ARCHITECTURE.md §1, or an equivalent web client) talks to that surface
  instead of to Streamlit's own server-rendered pages. Until such a client exists, remote operators
  use loopback plus a tunnel, exactly as the platform's own Postgres deployment already does.

## Consequences

- External deployments (including the AML bank image) lose the ability to publish the console on a
  routable interface. Multi-operator or off-host access requires network-layer tunneling the operator
  controls, documented in `docs/aml/ACCEPTANCE_PACKAGE.md`.
- No further remediation attempt against this task family should propose authenticating Streamlit's
  own connection admission; that path is closed by the review history above, not merely untried.
- Revisit only when AICC ships a client that speaks to `command_center.http_auth`-guarded JSON/API
  routes instead of to Streamlit directly; at that point this ADR's scope (Streamlit's own transport)
  remains closed and the new client is governed by ADR-0008 and this repository's existing HTTP
  authorization tests, not by this record.
