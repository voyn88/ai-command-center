# CTO migration lineage preflight — provisional, not deployed

`ops/aicc_cto_migration.py` composes the existing AICC adapter and AIOS migration
runner. It does not replace the runner, edit ledger history, grant permissions,
activate CTO enforcement, or add a second migration command to the public DB CLI.
The integrator owns final assembly and the authorized deployment chain.

## Reviewed inputs, never approval inferred from a live database

The wrapper requires a separately reviewed release manifest and its exact SHA256
supplied with `--manifest-sha256`. Its strict `aicc-cto-migration/v1` JSON contract is:

- `target`: integer 36, reserved for this reviewed foundation-plus-CTO lineage only.
- `identity`: `dbname` exactly `aicc_preprod`, `system_identifier` as a decimal
  string, integer `database_oid`, `server_major`, `server_port`, and exact
  `server_address` (null only for an explicitly pinned Unix socket connection).
- `execution_host`, `connection_host`, `connection_port`: exact deployment host
  hostname and libpq connection endpoint. A socket path must be absolute. These
  supplement cluster/database identity because physical clones can retain both
  system identifier and database OID. There is no hostname-only fallback.
- `identity_evidence_sha256`: hash of the separately supplied immutable approved
  SRE evidence file. The wrapper checks its cluster/database identity, deployment
  hostname and all 34 original ledger version/slug/checksum triples against the
  release manifest. A manifest generated from the connection being upgraded is
  not an approval source; the wrapper has no such generator.
- `connection_evidence_sha256`: hash of the separately approved connection-pin
  evidence, binding its socket selection and deployment hostname back to the
  approved identity evidence. `migration_session_user` pins the explicitly
  approved existing migration login; no hardcoded new login or admin fallback.
- `migrations`: exactly versions 1 through 36 in order, each with `version`, `slug`,
  `checksum` (SHA256 of UTF-8/LF up SQL), and `down_checksum`. Both directions of
  every local migration must match, including the approved foundation 0035 and
  new CTO 0036. No historical SQL or ledger checksum is rewritten.
- `catalog_sha256`: keys `34` and `35`, each the separately reviewed SHA256 of the
  structural public-schema inventory produced by this wrapper's `CATALOG_SQL`
  and `catalog_hash`. These hashes exclude owners, OIDs, timestamps and row data,
  but include object identities, column defaults, constraints, routines, indexes,
  triggers, policies, rules, types and sequence definitions. Unknown or modified
  objects therefore refuse the operation, including old policy 0036 artifacts
  on a misleading 1..34 ledger and unexpected foundation artifacts.

The approved identity evidence supplied to this work was
`CTO-AICC-IDENTITY-MANIFEST-01.json`, SHA256
`bc245ce12541367474aef48af39d553017965f71ad7aa0c8f33338d88f8fe56b`.
It records cluster `7674355837896772798`, database OID `66040`, database
`aicc_preprod`, PostgreSQL 16 on `voyn-control-01`, port 5433. Its observation
used a Unix socket (server address/port fields null). It does **not** supply this wrapper's full structural catalog fingerprints.
The additional approved `CTO-AICC-CONNECTION-PIN-01.json`, SHA256
`32aaaee579ab0660d58fc1f7bf6602a114ac2aa004226736d84f8401c867de2b`,
pins `/var/run/postgresql` (resolved `/run/postgresql`) on that host at port 5433. Its schema/function anchor hashes are a different representation and must
not be substituted for `catalog_sha256`. Approval of the composed release
manifest and missing structural catalog/login approval remains an integrator/CTO gate.
No runnable production manifest is shipped here.

Old policy candidate `277409f` remains separate historical evidence. Its hashes
must be retained by the integration handoff; its SQL is not included or relabeled
as the new migration. An existing live version 36 is rejected even if its name
looks plausible. Resumption supports only the exact approved foundation version
35, not arbitrary partially applied DDL or a different policy schema.

## Operation and exclusion

Invoke as a module from the reviewed release checkout:

```sh
python -m ops.aicc_cto_migration \
  --manifest /approved/release-manifest.json \
  --manifest-sha256 APPROVED_SHA256 \
  --identity-evidence /approved/CTO-AICC-IDENTITY-MANIFEST-01.json \
  --connection-evidence /approved/CTO-AICC-CONNECTION-PIN-01.json \
  --sql-dir /approved/assembled-sql
```

Use the existing protected credential mechanism to provide
`AICC_CTO_MIGRATION_DSN`; never include it in command arguments or reports. The
session must authenticate as the exact separately approved migration login in
the release manifest. Existing `db upgrade` consumes configured credentials and
does not enforce a fixed login; the restore runbook also permits the restored
table owner. This wrapper does not create an incompatible new LOGIN requirement
or change production roles. Failure to read cluster identity (such as missing
`pg_control_system` privileges) refuses; it never silently retries with a more
privileged credential or a weaker identity check. A privileged existing migration
path requires explicit approval in the manifest, not inference from its ability
to connect. Log messages from the dedicated
standalone CLI's pool/driver are suppressed while the pool is alive; errors are
reported using fixed codes without DSN-bearing exception text.

Default mode is a read-only **plan**: no ledger creation, DDL, grants, activation
or repair. It still takes the migration session advisory lock for a consistent
preflight and rejects missing/incompatible ledgers. `--apply` is a separate
explicit operation for the authorized release chain, not permission granted by
this document. The approved source bytes are staged in a private temporary
snapshot; checksums are checked again before invoking the existing runner.

A single dedicated autocommit pool connection holds the existing
`aicc:schema-migration` advisory lock across identity, exact ledger and structural
catalog validation, and the call to the existing runner. The runner acquires the
same session lock reentrantly; its release does not open a gap in the outer
lock. Accepted live histories are exactly 1..34 or exactly approved 1..35.
The runner preserves its existing per-migration transaction/ledger atomicity.
The outer lock covers the final exact 1..36 ledger read as well.

This does not claim protection against a privileged process ignoring the shared
migration lock or an attacker controlling the approved release host/account.
Runtime leases, external authority, backup/rollback, active-work drain, exact
candidate CI and independent review remain prerequisites. An apply failure can
leave an already committed version 35 and must be investigated before an approved
resume; a failure is not evidence of rollback of previous migrations. Re-running
against version 36 is deliberately refused and requires a fresh reviewed plan.

## Verification boundaries

Unit tests cover denial before runner invocation for wrong target, changed
checksum/history, prior version 36, unexpected schema artifacts, wrong connection
endpoint, changed staged source, and plan/apply ordering under the shared lock.
They also verify immutable source staging, explicit evidence binding and redacted
connection failure output. These are not PostgreSQL lock/DDL acceptance.
A bounded disposable PostgreSQL 16 proof verified the catalog query at baseline
34, changed and restored its fingerprint around an unexpected public table, and
committed foundation 35 through the nested existing runner. A second connection
was observed waiting in `pg_locks` after that inner runner returned, and acquired
the lock only after the outer context exited. The receipt records the exact
wrapper and foundation SQL hashes. This proves the lock interaction, not a
complete target-36 wrapper apply or production acceptance. Production execution
is not part of this candidate's preparation.

## Public upgrade and self-deploy wiring

`python -m command_center.db upgrade` now guards the first application of 0035
or 0036. `self_deploy._run_migrations` invokes that same command, so automatic
release cannot silently take the legacy route around the preflight. This changes
neither the low-level `migrations.upgrade` API used by disposable test/bootstrap
harnesses nor the generic AIOS runner.

Before observing applied/pending versions, the CLI acquires the same
`aicc:schema-migration` session lock. It checks for a nonempty contiguous known
ledger and verifies all applied checksums before choosing a route. An old or
modified version 36 is refused before any upgrade or grant restoration. Crossing
35/36 requires all four release-environment inputs:

- `AICC_CTO_MIGRATION_MANIFEST`: reviewed release manifest path.
- `AICC_CTO_MIGRATION_MANIFEST_SHA256`: exact approved manifest hash.
- `AICC_CTO_MIGRATION_IDENTITY_EVIDENCE`: approved identity evidence path.
- `AICC_CTO_MIGRATION_CONNECTION_EVIDENCE`: approved connection evidence path.

The CLI loads and validates those inputs, snapshots its current assembled
`migrations.SQL_DIR`, then invokes `execute(existing_connection, ..., apply=True)`.
It does **not** open the wrapper's separate DSN, call the wrapper CLI, switch
roles, or try another credential. Missing manifest, drift, missing packaged
`ops.aicc_cto_migration`, or any preflight failure returns nonzero without calling
legacy upgrade or restoring grants. The existing source-release package must
therefore include the reviewed `ops` module; a missing module is a hard failure.

After the guarded runner succeeds, the existing grant matrix is restored while
the outer lock remains held. A failed apply may already have committed 0035;
resume still requires its exact approved history and structural fingerprint.
Already-36 databases with verified unchanged history retain the ordinary
idempotent upgrade and future-migration path; no perpetual 34/35-only wrapper is
imposed on later deployments. A future bundle crossing from 34/35 is not silently
approved by this version's exact 1..36 manifest: assemble and review its lineage
explicitly. Empty/missing/unknown ledgers do not cause automatic ledger creation.

The public command suppresses pool/driver diagnostics while upgrading and emits
fixed refusal/error messages, so failures cannot print DSNs or arbitrary SQL
exception text. The focused tests invoke the actual `cli.main(["upgrade"])`
entrypoint and verify refusal paths, shared connection/lock ordering, success-only
grants, and the already-36/future paths. Final combined PostgreSQL acceptance and
authorized runtime rollout remain owned by the integrator.
