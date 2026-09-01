# ADR-0011: the Streamlit console is not made remotely reachable

Status: accepted for `VOYN-W0-AICC-CONSOLE-NO-AUTH` and its remediation chain
(`-REM` through `-REM-REM-REM-REM-REM`).

## Context

`app.py` performs privileged `git`, `gh` and subprocess operations and has no
authentication layer. `tests/test_deployment_exposure.py` already gates all
four ways it can be launched (`streamlit run app.py`, `scripts/start-ui.sh`,
`scripts/aml-entrypoint.sh`, `docker compose`) so that each one *defaults* to
loopback; an operator can still widen any of them explicitly (`AML_BIND_HOST`,
an explicit `--server.address`, an explicit `STREAMLIT_SERVER_ADDRESS`), which
is a documented, tested, individually-auditable operator act, not a default.

Four consecutive designs for closing the remaining gap — an authenticating
front for the case where an operator *does* widen it — were built and
rejected by adversarial review, each for a distinct, load-bearing reason:

1. Authenticate only at WebSocket connection admission: a revoked platform
   principal keeps a live session indefinitely, because Streamlit frames after
   the upgrade never pass back through HTTP.
2. Identity without authorization: any valid platform principal — not just an
   AICC-granted one — would satisfy the gate, because the design treated
   `command_center.http_auth.authz` as optional rather than mandatory.
3. Periodic revalidation over the same connection: it requires the proxy to
   hold the caller's platform bearer credential in memory for the connection's
   lifetime, recreating inside AICC the high-value credential store
   `command_center/http_auth/identity.py`'s own module docstring names as the
   reason AICC does not verify credentials in-process. The revalidation lookup
   also relies on `authz.load_grants()`, which memoises by `(path, st_mtime)`;
   an update that preserves or collides on that timestamp serves a withdrawn
   grant past whatever bound the design claims.
4. A cookie/token issued after an initial authenticated HTTP request, then
   presented on the WebSocket upgrade: undeliverable as stated, because a
   browser `WebSocket` cannot set an `Authorization` header on the upgrade
   request, and no alternative (cookie, subprotocol, query parameter) was
   specified precisely enough to review — and even a specified one still
   leaves "does the proxy strip the credential before forwarding to Streamlit"
   and "is it erased for the connection's full lifetime, not just synchronously
   after admission" as open, safety-critical questions each attempt left
   unanswered.

Each attempt treated the missing piece as an implementation detail to fill in
later. It is not: making Streamlit's own long-lived, stateful WebSocket
protocol carry per-request identity is a protocol-level mismatch that no
proxy placement fixes, and every attempt to patch around it produced a new
credential-handling hazard at least as serious as the one it closed.

## Decision

The Streamlit console is not made remotely reachable, and no reverse proxy,
bearer-token-over-WebSocket scheme, or session-credential store is built for
it. This is not "not yet" — it is the decision this ADR records, reversible
only under the condition below.

What this means concretely:

- The four launch paths keep defaulting to loopback exactly as
  `tests/test_deployment_exposure.py` already asserts. Nothing here changes
  that code.
- Documentation and comments that promised "widen this and put it behind a
  reviewed authenticating proxy" are corrected: no such proxy is planned, so
  the promise was a claim this project could not keep. `AML_BIND_HOST` remains
  available for an operator's own network topology (e.g. a private management
  segment the operator already trusts and controls), entirely at that
  operator's own risk and without any AICC-provided authentication in front of
  it.
- An operator who needs the Streamlit UI itself from off-host uses a host-level
  SSH port-forward terminating on the loopback address the console already
  binds — existing OS-level access control, not new application code. Nobody
  remote ever holds a live Streamlit session, so "a revoked principal keeps
  the session" (the defect that sank the first attempt) cannot occur: nobody
  remote can open one in the first place.
- Remote, authenticated, authorized administrative access is served by
  `command_center.webapi` — the accepted HTTP boundary from ADR-0010's sibling
  decision `VOYN-W0-AICC-AUTH-HTTP-01` (`command_center/http_auth/identity.py`
  + `authz.py`) — as already consumed by the React web client (`web/`) for the
  queue/task screens. Each Streamlit screen that needs remote reachability is a
  candidate to migrate there, on that boundary's existing identity and
  authorization guarantees, not a candidate for a Streamlit-specific proxy.
  This ADR does not change or re-certify that boundary's own semantics
  (grant-cache freshness under `authz.load_grants()`'s `(path, st_mtime)`
  memoisation remains that ADR's concern, not this one's).

## Rejected alternatives

- **Identity-aware reverse proxy in front of Streamlit.** Rejected by four
  independent review cycles (see Context) for four independent reasons: no
  attempt solved the "browser cannot authenticate a WebSocket upgrade"
  protocol mismatch without either leaving stale sessions live, skipping
  authorization, or building a new privileged credential store. A fifth
  attempt would face the same protocol mismatch; there is no proxy placement
  that changes what the browser `WebSocket` API can send.
- **Reuse the AIOS/webapi identity boundary directly inside Streamlit.** The
  blocking constraint is the WebSocket protocol itself, not which identity
  authority is asked to verify it — swapping the verifier does not create an
  `Authorization`-header-bearing upgrade request where the browser API offers
  none. This alternative fails for the same reason attempt 4 failed.
- **Do nothing and leave the ADR unwritten.** The compensating loopback
  defaults already exist and are tested; leaving the decision unrecorded is
  what let four different proxy designs each get partway through review
  before failing on the same underlying mismatch. Recording the decision is
  what stops a sixth attempt from repeating it.

## Revisit condition

Revisit only if one of the following becomes true:

- Streamlit is retired in favor of the web client for the screens that need
  remote reachability, at which point this ADR's scope (Streamlit
  specifically) no longer applies to that capability.
- An upstream, protocol-level mechanism exists for authenticating a Streamlit
  WebSocket upgrade with a platform-issued, revocable credential that AICC
  does not need to store — removing the credential-retention hazard this ADR
  treats as disqualifying, not merely inconvenient.

Widening a loopback default for a single deployment's own network topology
(`AML_BIND_HOST`, an explicit `--server.address`, or an explicit
`STREAMLIT_SERVER_ADDRESS`) remains an operator decision outside this ADR's
scope; it was never gated on the rejected proxy and is not gated on this
decision either.
