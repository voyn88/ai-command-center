# AICC Native Gateway v1

A separate, read-only HTTPS server contour for the native Mac/iPhone client.
AIOS remains the single owner of tasks, decisions, queues, access and
evidence; the gateway serves a redacted, allowlisted, versioned projection —
nothing else.

## Boundary invariants

| Invariant | Enforcement |
| --- | --- |
| HTTPS only | `native_gateway.serve` refuses to bind without TLS material; no plaintext flag exists. The client (`GatewayConfiguration`) independently rejects non-https URLs. |
| Read-only | Only GET routes are registered; the only issued scope is `read`; `test_openapi_exposes_only_read_routes` gates it. |
| No client infrastructure access | The client sees one origin. The gateway itself touches no PostgreSQL, SSH, systemd, GitHub or worker filesystem: its sole input is the projection artifact (below). |
| No second source of truth | The projection is produced by the AIOS pipeline; the gateway derives only presentation facts (freshness, rollups) that are pure functions of the artifact and the clock. |
| Redaction fail-closed | Allowlist DTO mapping + per-value prohibited-content scan + final serialized-body scan; residue aborts the response as an opaque 500 (`native_gateway/redaction.py`). |
| Safe errors | Uniform `{"error":{code,message,traceId}}`; fixed messages, no exception text, paths, hosts or stack traces. |
| Contract frozen | `openapi.json` + `schemas/snapshot-1.0.schema.json` are committed; `tests/native_gateway/test_contract_compat.py` fails on silent drift. |

## Data flow

```
AIOS (PostgreSQL, workers, control plane)          — owners, untouched
        │  existing pipeline writes
        ▼
projection artifact (JSON file, AIOS-owned)        — AICC_GATEWAY_PROJECTION
        │  read-only, sanitized, allowlisted
        ▼
native_gateway (FastAPI, TLS, bearer device auth)  — this contour
        │  DTO schema 1.0 over HTTPS
        ▼
AICC Native client (Mac / iPhone)
```

Freshness is derived from the artifact: producer-declared `degraded` wins;
otherwise age ≤ 120 s → `fresh`, ≤ 900 s → `stale`, else `offline`
(thresholds configurable via `AICC_GATEWAY_*` env). A missing or corrupt
artifact yields a calm, empty `offline` snapshot — never an error page.

## API surface (`/v1`, DTO `1.0`)

- `GET /v1/snapshot` — atomic dashboard snapshot; `ETag`/`If-None-Match`
  revision negotiation with `304 Not Modified` (304 only while `fresh`, so a
  freshness decay always reaches the client); requires
  `Accept: application/json` and `X-AICC-Client-Version: 1.x`.
- `GET /v1/tasks`, `GET /v1/tasks/{id}` — cursor-paginated task evidence.
- `GET /v1/dialogs` — summary-level dialog list (never raw messages).
- `GET /v1/decisions` — decision records (title/status/summary).
- `GET /v1/events?after_cursor=` — revision-bound cursor; a cursor minted
  against an older revision returns `409 resync_required`.

Errors: `401` (missing/invalid/disabled token, `WWW-Authenticate: Bearer`),
`403` (non-read scope), `404`, `409`, `422` (version/accept/cursor),
`429` (+`Retry-After`), opaque `5xx`.

## Authentication

Pre-provisioned per-device bearer tokens: `python -m native_gateway.provision
mint --registry <file> --device-id <id>` prints the token once and stores
only its SHA-256 hash (file mode 0600, outside the repository). Constant-time
comparison; v1 issues only scope `read`. The bare `--registry ... --device-id
...` form (no subcommand) still works as a deprecated alias for `mint`, for
existing deployment scripts.

Revocation is disable-without-delete: `python -m native_gateway.provision
revoke --registry <file> --device-id <id> --reason <text>` flips the device's
`disabled` flag (checked fresh on every request — no cache, so a revoked
device is refused on its very next call) and appends an `audit` entry to the
registry recording who was revoked, why, and when. Idempotent: revoking an
already-disabled device is not an error. Every mint/revoke write holds a file
lock across its full read-modify-write cycle and lands atomically (temp file
+ fsync + rename), so concurrent CLI invocations cannot corrupt the registry
or lose each other's updates.

## Operations

```
sh native_gateway/dev/gen_dev_cert.sh <dir>        # dev-only localhost cert
AICC_GATEWAY_PROJECTION=… AICC_GATEWAY_TOKEN_FILE=… \
AICC_GATEWAY_TLS_CERT=… AICC_GATEWAY_TLS_KEY=… \
uv run --with-requirements requirements-gateway.txt \
    python -m native_gateway.serve --host 0.0.0.0 --port 8443
```

Contract artifacts regenerate via `python -m native_gateway.contract_export`
(the compat tests force the diff to be reviewed).

## Known cross-lane contract deltas (reported, not edited here)

The client lane `codex/aicc-native-phase0` owns `clients/aicc-native/**` and
`docs/aicc_native/**`; this contour does not modify them. Two exact deltas:

1. **Client sends no credential.** `SnapshotRemoteStore` sets only `Accept`
   and `X-AICC-Client-Version`; the gateway requires
   `Authorization: Bearer <device token>` (a secure-by-default first release).
   The client lane needs a one-line header addition plus keychain storage of
   the provisioned token.
2. **Envelope divergence.** `docs/aicc_native/contracts/v1` sketches a
   snake_case envelope (`schema_version`, `staleness`, `data{}`), while the
   shipped Swift `SnapshotDecoder` decodes a flat camelCase DTO. The gateway
   serves the shape the shipped client actually decodes (flat camelCase,
   verified by `test_snapshot.py`), extended additively with `projects` and
   `connection`. The contract doc and the decoder should be reconciled in
   the client lane; whichever way that lands, the schema-compat tests here
   pin the served shape until a deliberate, reviewed change.

## AIOS boundary note (`native_gateway/auth.py`)

Device-token verification is deliberately a **placeholder**, mirroring the
already-frozen `command_center/companion/auth.py` precedent in
`AIOS_BOUNDARY_BASELINE.json`: no roles, no policy engine, no session store —
one hashed-token registry lookup gating a read-only projection. Per
ADR-0008/AC-01 the real identity capability belongs in AIOS; when AIOS
Identity (VOYN-W0-F4) exposes a device-credential contract, this module
converges onto it and its baseline entry shrinks away. Growing this file into
anything more than token verification is prohibited by the boundary gate.

## Command Gateway (future, out of v1)

No write route exists in v1. Requirements for the future command surface are
frozen in `COMMAND_GATEWAY_CONTRACT.md` — authorization, policy decision,
idempotency keys, explicit confirmation and durable audit are mandatory
before any command endpoint may be implemented.
