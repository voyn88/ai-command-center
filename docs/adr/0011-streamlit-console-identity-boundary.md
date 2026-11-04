# ADR-0011: identity boundary for the Streamlit console before external deployment

Status: accepted for `VOYN-W0-AICC-CONSOLE-NO-AUTH`.

## Context

The Streamlit console (`app.py`) performs privileged git/gh and subprocess
operations and has no authentication layer of its own.
`VOYN-W0-AICC-STREAMLIT-EXPOSED-NO-AUTH` closed the *accidental*-exposure gap —
`.streamlit/config.toml`, `scripts/start-ui.sh`, `scripts/aml-entrypoint.sh` and
`docker-compose.aml.yml` all now default to loopback and fail closed rather
than silently binding every interface — and `tests/test_deployment_exposure.py`
gates all four launch paths. That work was explicit that it "closes the
*exposure*, not the underlying absence of authentication."

Separately, `VOYN-W0-AICC-AUTH-HTTP-01` gave AICC's 29 mutating HTTP routes an
identity boundary (`command_center/http_auth/`, see
[`docs/AIOS_BOUNDARY.md`](../AIOS_BOUNDARY.md)): `identity.py` consumes the
platform's `GET /api/v1/whoami` rather than AICC verifying credentials itself,
and `authz.py` is a local deny-by-default ACL keyed by the returned principal
id. That boundary covers the FastAPI `webapi/` surface. It does not, and
structurally cannot as built, cover the Streamlit console: Streamlit is a
monolithic server-rendered app with no per-route middleware seam comparable to
a FastAPI dependency, so "add the same auth check" is not a drop-in extension
of `http_auth/` — it requires something in front of Streamlit, not inside it.

So today: the console is loopback-only by construction and by test, which is a
correct compensating control for a single-operator, single-host install, but
it is not an identity boundary. The localhost bind assumes the only principal
who can reach the interface is whoever has a shell on the host. The moment
that assumption is relaxed — `AML_BIND_HOST` widened, a tunnel or proxy placed
in front of the container, a multi-operator or hosted deployment — the console
is a fully privileged, fully unauthenticated surface reachable by anyone who
can reach the interface. `README.md` ("Current limitations and risks") and the
`docker-compose.aml.yml`/`scripts/aml-entrypoint.sh` comments already say this
in operator-facing terms; this ADR is the missing decision record for what
must be true before that relaxation is allowed to happen.

## Decision

External deployment of the Streamlit console — anything that widens
`AML_BIND_HOST` off `127.0.0.1`, publishes its port on a non-loopback host
interface, or otherwise makes it reachable by more than "whoever has a shell
on this host" — is **out of scope and not authorized** until one of the
following exists, and this ADR is updated to record which:

1. **Identity-aware reverse proxy consuming the accepted AIOS identity
   surface.** A proxy terminates every request in front of Streamlit,
   authenticates it the same way `command_center/http_auth/identity.py`
   already does (the platform's `whoami`, not a credential AICC verifies
   itself), and authorizes it against a deny-by-default ACL in the shape of
   `authz.py` before it ever reaches Streamlit. This is the designated path
   when remote or multi-operator access is actually needed: it reuses the one
   identity mechanism this repository has already accepted instead of
   inventing a second one.
2. **Retirement of remote Streamlit in favor of an already-authenticated
   client.** If the native desktop or the web dashboard reach parity for
   privileged operations (launch, cancel, complete, Portfolio — see the
   capability table in `README.md`) *and* adopt the same AIOS-identity
   boundary for their own write paths, remote/multi-operator access moves
   there and Streamlit is retired to a local-only operator tool. Not adopted
   now: the web dashboard is read-only and the native desktop is explicitly
   out of scope for privileged operations for Increment 1, so no replacement
   exists yet. This option stays open and should be revisited when that
   parity is reached.

A bespoke, Streamlit-native authentication layer (login form, session
cookies, a new credential store) is **rejected** as a third option: it would
make AICC a second identity authority and a second authz engine, which
ADR-0008's anti-engine-growth doctrine already prohibits for exactly this
class of capability (`docs/AIOS_BOUNDARY.md`, Gate 2, `authz` signature).
Reusing the platform identity the HTTP boundary already consumes is strictly
preferred over adding a new one.

Until option 1 or 2 exists, the loopback-only defaults enforced by
`.streamlit/config.toml`, `scripts/start-ui.sh`, `scripts/aml-entrypoint.sh`,
`docker-compose.aml.yml` and `tests/test_deployment_exposure.py` remain
mandatory, not optional hardening. Widening `AML_BIND_HOST` or passing an
explicit `--server.address` to reach a non-loopback interface without the
reverse proxy in place is a violation of this decision, not a configuration
choice an operator is free to make.

## Enforcement

Recorded as prose, the paragraph above protected nothing. Every launch path
*defaults* to loopback and every one of them also documented its override
(`--server.address 0.0.0.0`, `AML_BIND_HOST=0.0.0.0`), so the widening this
decision declines to authorize stayed one flag away and, at runtime, was
indistinguishable from a deliberate reviewed exposure. The decision is
therefore also a gate: `command_center/console_identity.py`.

- **What it checks is reach, not the listening socket.** The two differ in the
  container, where `0.0.0.0` inside a private namespace reaches nothing by
  itself and the exposure boundary is the *published* host interface.
  `docker-compose.aml.yml` passes that interface in as
  `AICC_CONSOLE_PUBLISH_ADDRESS`, from the same `AML_BIND_HOST` it publishes
  on; where no launch path declares one, the listening address is the answer.
- **Off-loopback reach fails closed.** `scripts/aml-entrypoint.sh` runs the
  gate before it seeds or starts anything and exits `EX_CONFIG`; `app.py` runs
  it before it loads a task or renders the shell, so a refusal is not a banner
  over a live console — no widget that could launch an agent is created at
  all.
- **There is no attestation bypass.** A value such as
  `AICC_CONSOLE_IDENTITY_PROXY=...` cannot prove that the named component is in
  the request path, covers Streamlit's HTTP and WebSocket traffic, or
  authenticated a principal. Accepting such a declaration would turn the P0
  control into a bypass flag. Off-loopback reach is therefore always refused
  in the current implementation. Option 1 must land as a separately reviewed
  deployment with end-to-end tests before this rule can change.
- **Stated residual.** A launch that states no address at all
  (`streamlit run /path/to/app.py` from a directory where neither this
  repository's `.streamlit/config.toml` nor an explicit flag applies) binds
  every interface while reporting nothing, and in-process that is
  indistinguishable from a test harness that binds no socket. The gate does not
  guess there; `scripts/start-ui.sh` cannot produce that state, and the
  entrypoint refuses to start without an address.

`tests/test_console_identity_boundary.py` owns this half — the rule, the
entrypoint, the compose declaration and `app.py`'s refusal — as
`tests/test_deployment_exposure.py` owns the static defaults.

## Consequences

- The supported deployment is unchanged: every launch path already defaulted
  to loopback, and loopback needs no attestation. What changes is that the
  defaults can no longer be quietly overridden — the gate above refuses every
  off-host reach.
- One new obligation on non-compose container launches: a `docker run` that
  publishes the port itself must pass `AICC_CONSOLE_PUBLISH_ADDRESS`, because
  the published interface is invisible from inside the container. Stating it is
  the point, and the refusal names the variable.
- Any future ticket that widens `AML_BIND_HOST`, adds a hosted/remote
  deployment target, or otherwise plans multi-operator access to the console
  must implement the reverse proxy of option 1 (or land the parity of option
  2) as a prerequisite, and must update this ADR's status rather than treating
  the widening as an isolated infrastructure change.
- `tests/test_deployment_exposure.py` continues to own the mechanical
  exposure gate; it does not and should not assert authentication, since none
  exists yet. Nor does the enforcement above: it asserts that remote Streamlit
  is disabled, which is a different claim. A future change implementing option
  1 owes tests that the proxy path is actually in front of every applicable
  launch path, covers WebSockets, and performs real authentication rather than
  trusting an attestation.

## Rejected alternatives

- **Streamlit-native auth (login/session/local credential store).** A second
  identity and authz engine in a repository that has already committed to
  consuming AIOS identity rather than growing its own (ADR-0008).
- **Network-layer-only controls (VPN, SSH tunnel) as a permanent
  substitute.** Acceptable as an interim *operational* control for a single
  trusted operator, but not a substitute for identity: it grants host-level
  reachability, not per-principal accountability, and does not extend to a
  multi-operator future the way option 1 does.
- **Treating the existing loopback default as sufficient.** It is a correct
  compensating control for the current single-host, single-operator
  deployment, but compensating controls are not architecture — the whole
  point of recording this decision is that "don't expose it" stops being true
  the moment someone needs to.
