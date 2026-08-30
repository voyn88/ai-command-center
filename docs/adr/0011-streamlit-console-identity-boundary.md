# ADR-0011: identity-aware reverse proxy admits the Streamlit console

Status: proposed for `VOYN-W0-AICC-CONSOLE-NO-AUTH`.

## Context

`app.py` (the Streamlit operator console) performs privileged operations —
`git`, `gh`, and `subprocess.run` against the task-start script
(`app.py:343`) — and has no authentication layer of its own.
`.streamlit/config.toml:14-20` and `scripts/start-ui.sh:19-26` both pin
`--server.address localhost` and say so explicitly: localhost binding is
today's only control, and it is a network-position compensating control, not
an identity boundary. It stops working the moment anyone runs the console
with an explicit `--server.address` override or puts it behind a host-level
port-forward — nothing in the app itself objects.

AICC already has an identity boundary for this exact problem, built for the
FastAPI HTTP surfaces (`command_center/http_auth/`):

- `identity.py` — authentication. The caller presents a platform bearer
  credential; AICC forwards it to `GET /api/v1/whoami` and trusts the
  reflected `principal_id`. AICC "stores no credential, hashes nothing, and
  holds no signing key" (`identity.py:5-6`) and deliberately caches nothing,
  because the platform's own revocation has no cache either and any cache
  AICC added would be staleness AICC introduced (`identity.py:27-36`).
- `authz.py` — authorization. A closed, deny-by-default `principal_id ->
  operations` map loaded from a JSON grants file; an absent file is an empty
  map, and an unconfigured deployment therefore refuses everyone
  (`authz.py:159-165`).
- `routing.py` — enforcement. `authenticate()` (`routing.py:198-229`) is
  fail-closed: 401 with no/invalid credential, 503 if the platform can't be
  reached (never treated as "anonymous is fine"), 401 if the platform
  says no. `enforce()` (`routing.py:232-260`) then checks `authz.is_permitted`
  before any handler runs. `validate_routing()` refuses to boot a FastAPI app
  whose mutating routes aren't all wired to `enforce`.

Three prior decision records for this task (PR #408, PR #433, PR #480, all
superseded) proposed reusing this boundary in front of the console and were
each rejected for a distinct, concrete gap:

1. Authentication only at WebSocket connection admission has no revocation
   story for the remaining life of that connection — subsequent WebSocket
   frames carry no credential to re-check.
2. AICC-side authorization (`authz.py`'s ACL) was written as something the
   console *might* add later, not as a mandatory gate — so any principal
   holding a live platform credential for any purpose would pass.
   Revalidation was "configurable" with no maximum, which bounds nothing.
3. The fix for (1) proposed retaining the caller's bearer credential in the
   proxy to re-poll `whoami` periodically — directly contradicting
   `identity.py`'s "stores no credential" property with no defined
   acquisition, lifetime, or erasure story. Separately, `authz.load_grants()`
   memoizes by `(path, st_mtime)` (`authz.py:159-189`); an edit that
   preserves or collides on that timestamp can serve a withdrawn grant
   indefinitely, which breaks any claimed revocation bound.

This ADR is the fourth pass and is written to close all three, by
specification precise enough to leave no such gap unaddressed, rather than by
asserting a stronger property than the design actually has.

## Decision

Front the Streamlit console with an **identity-aware reverse proxy** that
reuses AICC's existing authentication primitive (`identity.py`'s `whoami`
call) and mirrors its authorization idiom (`authz.py`'s deny-by-default ACL),
rather than inventing a new credential format or a new trust authority.
Localhost binding remains the deployed default; the proxy is what makes
*intentional* off-host reachability safe, not a replacement for it.

### 1. Two admission points, not one

The proxy performs a full authenticate-then-authorize check at:

- every initial HTTP request for the console's page/static assets, and
- every WebSocket upgrade handshake — the one point in Streamlit's protocol
  that still carries HTTP headers, hence a credential.

An already-open WebSocket is never treated as permanently admitted (this is
the gap PR #408 was rejected for): see §4 for how its exposure is bounded
instead of left open-ended.

### 2. Authorization is mandatory, not optional

The console gets its own closed, single-entry operation inventory —
`CONSOLE_OPERATIONS = frozenset({"console:access"})` — checked on every
admission in §1. This is **not** a call into `authz.OPERATIONS`/
`authz.is_permitted`: that inventory's own fitness tests assert every entry
maps to exactly one FastAPI route in `ROUTE_OPERATIONS`
(`authz.py:70-74`), and the console is not a FastAPI route, so adding
`console:access` there would violate an invariant those tests exist to
protect. Instead the console gets a small, parallel module
(`command_center/http_auth/console_admission.py`, not yet written) that
copies `authz.py`'s shape exactly: one JSON grants file named by a new env
var (`AICC_CONSOLE_GRANTS_FILE`), `principal_id -> ["console:access"]` or
absent, absent file or absent principal denies. There is no configuration
that makes this check optional — it is a required call in the admission path,
the same way `enforce` is a required dependency on every mutating FastAPI
route, and the same negative-control mutation suite that guards
`tests/http_auth` (`tests/http_auth/negative_control.py`) must gain a mutant
for this call before the proxy ships, so a future edit that skips it fails
CI rather than passing quietly. This closes PR #433's high finding: holding
*a* platform credential is authentication, not authorization, and the
console requires both, unconditionally.

### 3. Zero credential retention

The proxy reads the bearer token from the admitting request's `Authorization`
header, uses it for exactly one `whoami` round trip
(`identity._whoami`, imported the same way `routing.py:38` already does:
`from command_center.http_auth.identity import _whoami as whoami`), and
discards it when that request's handler returns. It is never written to
disk, logged, stored in an instance/session attribute, or reused after the
admitting request completes — including for the remaining lifetime of an
already-open WebSocket. Periodic in-connection re-authentication using a
retained token is explicitly rejected: it would create a proxy-memory
credential store this boundary does not otherwise have, with no acquisition,
lifetime, or erasure semantics achievable without changing what
`identity.py` guarantees everywhere else. This closes PR #480's high
finding.

### 4. Bounded exposure instead of mid-connection revalidation

Because §3 rules out holding the credential to re-check it, a connection's
remaining lifetime cannot be made instantly revocable the way an HTTP
request is. Instead it is bounded: the proxy forcibly closes every console
WebSocket connection **15 minutes** after it was admitted, independent of
activity, and the browser must re-establish it — re-entering §1's
WebSocket-upgrade admission with a freshly presented credential. This ceiling
is fixed in the proxy's code, not an operator setting; a configuration value
above 15 minutes is clamped, not honored, and there is no "off" state. A
revoked or de-authorized principal therefore loses console access within at
most 15 minutes of the platform/grants change — a real, stated bound, weaker
than the HTTP boundary's per-request immediacy and recorded here as exactly
that trade-off, not left implicit. This closes PR #408's finding (define
revalidation-and-disconnect or an explicit weaker bound — this is the
weaker bound) and PR #433's medium finding (a maximum, not an open
"configurable" knob).

### 5. Grant lookups are uncached by construction

`authz.load_grants()`'s `(path, st_mtime)` memoization exists because the 29
FastAPI mutating routes it guards see request volume where a same-second
edit-then-request race is an acceptable cost. Console admission events are
orders of magnitude rarer — one page load plus at most one forced reconnect
per 15 minutes per open session — so that trade-off does not transfer, and
`console_admission.py` must not import or extend `authz.py`'s cache to get
it. Its grants loader reads and parses the JSON file fresh on every single
admission call, with no memoization at all: the same "no cache on this path"
choice `identity.py` already makes for `whoami`, applied to the local half of
the same admission. This does not bypass a cache that could later be
re-added by a well-meaning refactor — there is no cache to bypass — which
closes PR #480's medium finding by construction rather than by convention.

### 6. Deployment default and technology

Whichever reverse-proxy technology implements this (a small dedicated
ASGI app is the natural choice, since it can share `identity.py` directly and
be covered by the same mutation-tested style as `tests/http_auth`; a
general-purpose proxy's auth-request module is not ruled out but must
implement the identical fail-closed contract itself), external reachability
stays off by default: no proxy deployment means the console stays
localhost-only exactly as today, and standing the proxy up requires an
operator to explicitly configure `AICC_PLATFORM_URL` and
`AICC_CONSOLE_GRANTS_FILE` — the same "off unless explicitly configured"
posture ADR-0008 established for AIOS network transport. This is an
additive control on top of localhost binding, not a replacement for it: the
compensating control stays in place at every layer that doesn't run the
proxy.

### 7. This is a bridge, not a second permanent boundary

Once the web client (`command_center/webapi` + its SPA) fully replaces the
operator-facing Streamlit console, remote Streamlit access is retired
outright — the console reverts to localhost-only, permanently, rather than
being carried forward as a second authenticated surface alongside the web
client's own. Implementers must not build long-lived tooling around the
proxy that assumes it outlives that migration.

## Rejected alternatives

- **In-process check inside `app.py`** (call `whoami`/console-authz from
  Streamlit script-rerun code): rejected because Streamlit only re-runs the
  script on user interaction, not on connection admission or on a timer: it
  cannot see a WebSocket upgrade and cannot bound an idle-but-connected
  session, reintroducing exactly PR #408's gap.
- **Console-issued JWT/session cookie with a TTL**: rejected for the same
  reason `identity.py` rejects a locally verifiable signed token
  (`identity.py:8-17`) — it would be a second credential format the console
  invents, self-contained and valid until expiry regardless of what the
  platform's own revocation does in the meantime.
- **Retain the bearer credential to re-poll `whoami` periodically**: rejected
  per §3/PR #480.
- **Operator-configurable revalidation interval with no ceiling**: rejected
  per §4/PR #433 — "configurable" without a maximum is not a bound.
- **Do nothing remote until the web client ships**: a valid holding pattern,
  but it doesn't answer the task's actual trigger ("before external
  deployment"); recorded instead as this decision's eventual end state (§7).

## Operational cost and revisit condition

Running the proxy is one more small process to deploy and TLS-terminate, and
one more grants file (`AICC_CONSOLE_GRANTS_FILE`) an operator must populate;
no new identity infrastructure is introduced. The 15-minute forced-reconnect
ceiling is a visible interruption (a Streamlit reload) for any operator with
the console open across that boundary — accepted cost for a real, stated
revocation bound rather than an open-ended session. Revisit the ceiling
downward if operational experience shows meaningful risk inside 15 minutes;
revisit it upward only alongside a documented reason the immediacy trade-off
changed, never by exposing it as an unbounded operator knob. This decision
is superseded, not extended, once §7's retirement happens.

## Required before implementation ships

This ADR records the decision; `console_admission.py` and the proxy itself
do not exist yet. Before either is deployed reachable off-host, all of the
following must hold, each independently testable:

- `console_admission.py` denies when `AICC_CONSOLE_GRANTS_FILE` is unset,
  unreadable, or does not grant the calling `principal_id`.
- A grants-file edit takes effect on the very next admission call with no
  code change and no process restart (proves §5's no-cache claim).
- No open WebSocket outlives 15 minutes past its admission, verified by a
  test that opens a connection, advances a fake clock past the ceiling, and
  asserts the proxy closes it.
- Nothing the admission path handles — logs, exceptions, object attributes —
  contains the raw bearer token after the admitting call returns (proves
  §3's zero-retention claim).
- A `tests/http_auth/negative_control.py`-style mutation suite exists for the
  console admission path and every mutant it defines is killed.
