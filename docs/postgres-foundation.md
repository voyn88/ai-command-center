# PostgreSQL foundation

The server deployment of AI Command Center stores its state in PostgreSQL.
This document is the operator's reference for standing one up, migrating it,
and proving the backups work.

Scope: this slice (`VOYN-W0-AICC-SRV-01a`) delivers the database substrate —
schema, migrations, roles, pooling, health probes, backup/restore. The runtime
store still writes to SQLite until `VOYN-W0-AICC-SRV-01b` moves it onto this
seam; both schemas exist in parallel until then.

## Where the parts live

The generic PostgreSQL machinery is **not** in this repository. Opening a pool
and proving the open, taking an advisory lock and guaranteeing its release,
executing migrations atomically and verifying their history — none of that is
specific to these tables, so it belongs to AIOS Core and ships as the
independently versioned `aios-db` library. It is consumed through exactly one
module, `command_center/db/adapter.py`; the architecture-fitness gate fails if
anything else imports `aios_db`.

What this repository owns is the part only it can: the 33-table schema and its
migrations, the `aicc_*` roles and their grants, the repositories, the
backup/restore policy, and the composition that decides what "ready" means for
this service. AIOS Core knows none of those.

The wheel is pinned in `aios-db.lock.json` by release tag and SHA-256 and
fetched by `scripts/fetch_aios_sdk_artifact.py --lock aios-db.lock.json`, the
same verified path the SDK uses. Upgrading it is a reviewed change to that lock,
never a floating version range.

## Configuration

Everything comes from the environment. There are no defaults for host,
database, user or password — a missing value is a startup error, not a guess.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `AICC_PG_HOST` | yes | — | Hostname, IP, or a Unix socket directory. |
| `AICC_PG_PORT` | no | `5432` | |
| `AICC_PG_DB` | yes | — | |
| `AICC_PG_USER` | yes | — | One of the three roles below. |
| `AICC_PG_PASSWORD` | yes | — | ≥16 chars, not a well-known default, not equal to the user. |
| `AICC_PG_SSLMODE` | no | `verify-full` | Non-verifying modes are rejected unless the host is loopback. |
| `AICC_PG_SSLROOTCERT` | with TLS | — | Required whenever the mode verifies. |
| `AICC_PG_POOL_MIN` / `AICC_PG_POOL_MAX` | no | `1` / `10` | |
| `AICC_PG_POOL_TIMEOUT` | no | `10` | Seconds to wait for a free connection. |
| `AICC_PG_CONNECT_TIMEOUT` | no | `10` | Seconds. |
| `AICC_PG_STATEMENT_TIMEOUT_MS` | no | `30000` | Applied to every session from the pool. |

`require` encrypts the transport but authenticates nothing, so it is treated as
unacceptable for any host reachable over a network. The loopback exemption
exists for the single-host compose deployment, where the database is not
reachable off the machine at all.

## Roles

Five roles, provisioned by `render_bootstrap()` / `render_table_grants()` and
enforced by the database:

| Role | May do | May not do |
| --- | --- | --- |
| `aicc_migrator` | Own the schema, run DDL | Nothing else runs as it |
| `aicc_app` | `SELECT`/`INSERT`/`UPDATE` on every domain table | DDL, `DELETE`, `TRUNCATE`, and any write to `schema_migration` |
| `aicc_worker` | Queue and execution tables only | Read or write proposals, council motions/votes, audit findings, provenance, marketplace, model registry; write `completion.review_*` |
| `aicc_operator` | Execute worker admission and revocation functions | Domain-table DML or deployment evidence |
| `aicc_deployer` | Execute exact deployment attestation only | Table DML, arbitrary merge evidence, or backlog transitions |

`aicc_worker` is deliberately the narrowest: execution hosts run agent
processes against untrusted repository content, so a compromised worker
credential must not be able to read or forge the governance record. Workers
claim queue entries (`UPDATE`) but cannot enqueue them (`INSERT`).

Two carve-outs are column- rather than table-level, because a table-level grant
would have been too wide for the claim above:

- `completion.review_verdict` / `review_run_id` / `review_summary` hold the
  independent review's outcome. A worker writes the rest of its own completion
  row, so without a column grant it could stamp `review_verdict = 'approved'`
  on any run — forging exactly the decision the review gate exists to make.
- `schema_migration` is read-only for both non-migrator roles. Write access
  would let an injection foothold in the web layer rewrite a checksum, which is
  the only guard against two environments reporting the same version for
  different schemas.

No role holds `DELETE` on any table. The schema is an append/update ledger;
removing rows is an owner-level migration operation.

`upgrade` re-strips `PUBLIC` of table, sequence and **function** privileges
every time it runs. The function case is the one that matters: `PUBLIC` gets
`EXECUTE` on every new function by default, so a migration adding a
`SECURITY DEFINER` helper would otherwise hand a worker a route into the tables
the matrix excludes. `ALTER DEFAULT PRIVILEGES` would be the tidier mechanism,
but it was measured against PostgreSQL 15 and 17 and does not persist a
revocation of that built-in default — so it would have been a control that
silently does nothing.

`tests/db/test_postgres_integration.py` connects **as each role** and asserts
both halves of the matrix — what the role can do and what it must be refused.
A grant that no test exercises was the root cause of `VOYN-W0-SEC-AUDIT-PG-CRED`,
so the matrix and its test are kept in one place on purpose.

## Standing up a database

```bash
cp .env.example .env   # then fill in real secrets; .env is never committed
docker compose -f docker-compose.server.yml up -d
```

The compose file publishes PostgreSQL on `127.0.0.1` only. Do not change that
to `0.0.0.0`: Docker's port forwarding is inserted ahead of `ufw`/`firewalld`
rules, so a database published on all interfaces is reachable from the internet
while the firewall still reports it closed. Remote access is an SSH tunnel or a
private network.

Then, once as a superuser, and once per deploy as the migrator:

```bash
AICC_PG_USER=postgres      AICC_PG_PASSWORD=... python -m command_center.db bootstrap
AICC_PG_USER=aicc_migrator AICC_PG_PASSWORD=... python -m command_center.db upgrade
```

`bootstrap` creates the roles, strips `PUBLIC` of schema privileges and grants
the migrator `CREATE`. `upgrade` applies pending migrations and then re-asserts
the table grants — unconditionally, so a table created by a migration can never
ship without an explicit access policy.

Give the roles credentials yourself; nothing in this repository generates or
stores them:

```sql
ALTER ROLE aicc_app LOGIN PASSWORD '...';
ALTER ROLE aicc_deployer LOGIN PASSWORD '...';
```

## Migrations

Plain SQL files in `command_center/db/sql/`, `NNNN_slug.up.sql` with a matching
`.down.sql`. Applied versions live in `schema_migration`.

- Each migration and its ledger row commit in one transaction, so an
  interrupted run leaves either the old schema or the new one.
- A session-level advisory lock serialises concurrent runners, which is the
  normal case during a rolling deploy. Its key is derived from the name
  `aicc:schema-migration` rather than being a hand-picked constant: advisory
  locks share one flat key space per database, so a constant copied into a
  second subsystem would serialise the two against each other silently.
- An applied migration's checksum is verified on every run: editing a migration
  after it has been applied is rejected rather than silently producing two
  environments that report the same version for different schemas.
- Every migration must have a downgrade, and the round trip is covered by
  `test_downgrade_removes_everything_and_upgrade_restores_it`.

```bash
python -m command_center.db status
python -m command_center.db downgrade --to 0 --yes-i-understand-this-drops-data
```

### Types

The PostgreSQL schema is not a transliteration of the SQLite one. SQLite has no
real date, boolean or JSON type, so the source stores ISO strings, `0`/`1`
integers and JSON text. Here those are `timestamptz`, `boolean` and `jsonb`,
identity columns are `bigint GENERATED ALWAYS AS IDENTITY`, and `REAL` is
`double precision`.

The cost lands on the data migration (`VOYN-W0-AICC-SRV-07`): its importer must
convert explicitly, and its reconciliation report must compare converted values
rather than raw strings. Two columns stay `text` on purpose — `owner_item.due`
and `digest_item.day` are free-form day keys in the source and are not
guaranteed to parse as dates.

## Health probes

| Endpoint | Meaning | Behaviour |
| --- | --- | --- |
| `GET /healthz` | Is the process alive? | Never touches the database; stays `200` during an outage so a database blip does not restart every replica at once. |
| `GET /readyz` | Should it get traffic? | `200` only when the database answers *and* `schema_migration` matches `EXPECTED_SCHEMA_VERSION`; otherwise `503`. |

Neither payload contains the host, user, database name or connection string —
probes are commonly unauthenticated.

A schema mismatch is reported as not-ready rather than as an error because a
process running against a schema it was not built for returns wrong answers,
which is worse than returning none.

## Backup and restore

```bash
# Nightly, with retention and an integrity check of the finished archive.
scripts/aicc_pg_backup.sh --out-dir /var/backups/aicc --verify --keep 14

# The drill. Restores side by side; it does not touch the live database.
scripts/aicc_pg_restore.sh \
  --archive /var/backups/aicc/aicc-aicc-20260813T020000Z.dump \
  --target-db aicc_restore_check
```

`pg_dump` custom format, written to a `.partial` name and renamed only on
success, with a SHA-256 sidecar that `aicc_pg_restore.sh` verifies **before**
it writes anything. Restoring over the live database requires
`--allow-overwrite`, because the default shape of this command should be the
drill — a restore procedure that requires an outage to rehearse is one nobody
rehearses.

`test_backup_restore_drill_round_trips_data` runs both scripts end to end
against a real server and reads the restored rows back, so what is verified is
the scripts an operator actually runs, not just that `pg_dump` exists.

The client must be at least as new as the server; `pg_dump` refuses to dump a
newer server outright. CI installs `postgresql-client-17` to match the pinned
server image.

## Running the tests

`tests/db` skips itself unless a server is provided:

```bash
docker run -d --name aicc-pg -e POSTGRES_PASSWORD=... -p 127.0.0.1:55432:5432 \
  postgres:17.6-alpine

AICC_TEST_PG_ADMIN_DSN="host=127.0.0.1 port=55432 dbname=postgres user=postgres password=..." \
  pytest tests/db -q
```

Each test creates and drops its own database, so the suite is safe under
`pytest -n auto`. CI supplies the DSN from a service container pinned to the
same image digest as `docker-compose.server.yml`.
