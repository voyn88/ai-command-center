# ADR-0011: the Streamlit console stays local-only; remote reachability is gated on the AIOS-identity boundary plus bounded WebSocket revalidation, never a bare bind flip

Status: **Accepted** for `VOYN-W0-AICC-CONSOLE-NO-AUTH`.

## Context

`app.py` is a direct Streamlit script that performs privileged operations — it starts local Claude
CLI subprocesses, mutates Git worktrees, and drives `gh` — and it has **no authentication layer**
(`ARCHITECTURE.md` §1, `README.md` "Scope and limitations"). The only thing standing between that
surface and the network is a localhost bind, enforced across four launch paths
(`.streamlit/config.toml`, `scripts/start-ui.sh`, `scripts/aml-entrypoint.sh`,
`docker-compose.aml.yml`) and gated by `tests/test_deployment_exposure.py`. That control has already
failed once and been fixed reactively: the container entrypoint defaulted to `0.0.0.0` and the
compose file published the port unqualified until `VOYN-W0-AICC-STREAMLIT-EXPOSED-NO-AUTH` (#314)
closed it. Both the code and its own commit message are explicit that this closed the *exposure*,
not the underlying *absence of authentication* — a bind default is a compensating control, not a
security architecture, and every launch path retains a flag that can still widen it.

This ADR is the decision that finding asked for: what happens the day someone proposes flipping one
of those flags for a real reason, before that day arrives under time pressure.

Three options were on the table:

1. An identity-aware reverse proxy in front of Streamlit.
2. The same AIOS-identity boundary AICC has already accepted elsewhere.
3. Refuse remote Streamlit permanently, once a web client supersedes it for daily use.

They are not actually exclusive, and treating them as three competing whole-answers was the wrong
frame — see Decision.

## What already exists that this must not duplicate

AICC accepted an AIOS-identity boundary once already, for the 29 mutating routes of the webapi
surface: `command_center/http_auth/` (`VOYN-W0-AICC-AUTH-HTTP-01`, recorded in
`docs/AIOS_BOUNDARY.md` "why it was added to a frozen category"). Its shape:

- `identity.py` forwards the caller's own platform bearer credential to the platform's
  `GET /api/v1/whoami` and takes the reflected `principal_id` back. AICC stores no credential,
  hashes nothing, mints no token, and caches nothing — revocation must take effect on the very next
  request, which a local cache or a self-contained signed token would both break
  (`command_center/http_auth/identity.py` "Why no cache on this path").
- `authz.py` is a local, deny-by-default ACL keyed by that platform-issued identifier, because
  `whoami` answers "who", not "what may they do here."

`docs/AIOS_BOUNDARY.md`'s architecture-fitness gate (`tests/architecture/test_aios_boundary_fitness.py`)
mechanically forbids a second, competing identity/authz engine anywhere in this repository — no
credential store, no token format, no principal registry outside this one. Any answer to this ADR
that invents its own auth mechanism for Streamlit specifically is therefore not a live option: it
would be exactly the second engine ADR-0008 and the fitness gate exist to prevent.

## Decision

**Today: no change in default posture.** No external or multi-user deployment of Streamlit is
planned. The product's own committed direction — `docs/desktop/PRODUCT_VISION.md` §§1, 6, 9 — is
local-first, single-user, with "a server/SSO mode" explicitly out of scope, and `README.md` states
plainly this "is not a production, distributed, or remote-worker execution platform." Localhost
binding remains the correct default and stays enforced exactly as it is today.

**The gate for if that ever changes:** reachability beyond loopback for `app.py` — by any launch
path, present or future, including a manually passed `--server.address` /
`AML_BIND_HOST` / `STREAMLIT_SERVER_ADDRESS` — is not authorized by a flag alone. It requires an
identity-aware reverse proxy terminating the connection in front of Streamlit, and that proxy must
authenticate using the *same* AIOS-identity boundary already accepted for the mutating HTTP surface
— forwarding the caller's platform credential to `whoami` and gating on the resulting principal —
not a new mechanism built for Streamlit. This combines options 1 and 2: the reverse proxy is the
delivery point (Streamlit has no per-route middleware seam of its own to hang authentication off;
its WebSocket-backed, top-to-bottom-rerun session model needs the check at connection admission, not
mid-script), and the identity source is the one authority this repository is allowed to have.

**Connection admission alone does not satisfy the gate.** `http_auth`'s revocation semantics are
next-request-immediate because every mutating call re-forwards the caller's credential to `whoami`;
a Streamlit session is a single WebSocket held open for as long as the browser tab is, so
authenticating only once, at admission, would let a principal revoked five minutes into a session
keep issuing privileged git/gh/subprocess actions for the rest of that session — unboundedly, since
Streamlit does not otherwise expire a live socket. Reproducing an unbounded privileged-session window
on the one surface this ADR exists to close is not an acceptable trade for reusing the identity
source, so the proxy carries a second, equally mandatory obligation:

- **Bounded revalidation.** While a WebSocket session is open, the proxy re-runs the `whoami` check
  against the caller's held credential on a fixed cadence — **default 60 seconds, configurable per
  deployment, never unbounded and never cached beyond that interval** (the same "no cache" reasoning
  `identity.py` already gives applies here: any interval is a staleness window AICC is choosing to
  accept, so it must be small and explicit, not open-ended).
- **Forced disconnection.** The moment a revalidation call fails, errors, or returns a different
  principal than the one the session was admitted under, the proxy terminates the underlying
  connection outright — not merely stops forwarding frames while leaving the socket open — so the
  browser observes a hard disconnect, not a silently frozen session.
- **No trust on reconnect.** Streamlit's client auto-reconnects on socket drop. The proxy must re-run
  full connection-admission authentication (a fresh `whoami` call, not a cached verdict from the
  dropped connection) on every reconnect attempt, so a revoked principal cannot ride the client's own
  auto-retry back into a session.

Together these bound the worst-case revocation latency to one revalidation interval (≤ 60 s by
default) with no path back in on a stale credential — the closest a persistent-connection protocol
can get to the HTTP boundary's next-request-immediate guarantee, and an explicit, reviewable number
rather than an implicit "for as long as the tab stays open."

Option 3 as a *standalone* answer — do nothing to Streamlit and wait for a web client to replace it
— is rejected. `PRODUCT_VISION.md` §9 is explicit that retiring Streamlit is "not on any fixed
timeline"; making a privileged surface's authentication requirement depend on an uncommitted rewrite
reproduces the exact gap `AICC_STREAMLIT_EXPOSED_NO_AUTH` exploited once already, and would do so
indefinitely. The web dashboard (`web/`) remains the more likely long-term home for any genuinely
remote/multi-user need — its FastAPI surface already fits the `http_auth` boundary shape used
elsewhere in this repository far better than a Tornado-backed Streamlit script does, and a normal
HTTP request/response model does not carry the long-lived-session revocation problem a persistent
WebSocket does — but that is a product-roadmap fact, not a substitute for this gate applying to
Streamlit *while Streamlit exists and is reachable*.

## Consequences

- No code changes ship with this ADR. There is no reverse proxy to build today because there is no
  deployment to put one in front of.
- A future PR that widens Streamlit's bind beyond loopback for a real deployment need must land the
  identity-aware reverse proxy described above — connection-admission auth **and** the bounded
  revalidation/forced-disconnect/no-trust-on-reconnect behavior — in the same change, or it does not
  pass review. A proxy that authenticates once at admission and never again does not satisfy this
  gate regardless of which identity source it uses. This ADR is the standing justification a
  reviewer cites when rejecting a bind-only widening or an admission-only proxy.
- `tests/test_deployment_exposure.py` continues to gate the default; it is not itself sufficient
  once a deployment intentionally widens the bind, because it only asserts the *default*, not that
  an operator-chosen non-default carries authentication or bounded revalidation.
- If AICC ever needs Streamlit-specific authorization (not just identity), it extends
  `command_center/http_auth/authz.py`'s ACL rather than introducing a second one — same reasoning
  `docs/AIOS_BOUNDARY.md` already gives for why that module is a frozen, not-to-be-duplicated
  category.
- If the web client (`web/`) later reaches parity and Streamlit is retired for daily use per
  `PRODUCT_VISION.md` §8, this ADR's gate becomes moot for Streamlit by removal rather than by
  having been bypassed — the outcome option 3 wanted, reached without leaving a window open while
  waiting for it.
