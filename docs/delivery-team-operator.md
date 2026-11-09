# Synchronous delivery-team operator admission

Run `python -m ops.aicc_delivery_team_operator --help` from the installed,
reviewed release. This CLI does not deploy, run checks, assign corrections,
start a service or invoke a model. It is not an automatic release hook.
The existing release integrator owns invocation after successful deployment.

Supply `AICC_TEAM_OPERATOR_DSN` through the existing private operator environment;
never put its value in command arguments or logs. The connection must authenticate
as a non-superuser operator login with inherited `aicc_operator` authority,
without app/worker membership or SET ROLE. No privileges are granted by this CLI.
`AICC_TEAM_ARTIFACT_BEARER` supplies the private artifact reader; release also
requires `AICC_TEAM_OBSERVATION_BEARER`. Do not copy credentials into worker,
planner, owner-app or deploy services. No default PG connection is used.

Each command requires a canonical `--task-id`, positive `--revision`, explicit
`--expected-sha` and operator-selected HTTPS `--artifact-origin`:

- `release`: add `--repo`, `--version-url`, `--health-url`,
  `--observation-origin`, absolute `--artifact-directory`, `--artifact-base-url`.
  Expected SHA is the exact canonical release SHA, not a PR ancestor. The adapter
  observes version/health/version, retains and reads back the release artifact.
- `acceptance`: add `--repo`, `--criterion-id`, `--check-id`, `--artifact-url`,
  `--sha256`. These identify an independently produced production observation.
- `corrective`: task/revision identify the parent; add `--incident-key`,
  `--corrective-task-id`, `--corrective-revision`, `--criterion-id`, `--check-id`,
  `--artifact-url`, `--sha256`. Expected SHA identifies the completed correction.
  The existing SQL authority verifies its previously admitted assignment.

Choose producer, expected check ID and checksum from trusted independent run
provenance. Model text, URLs, checksums and green CI claims are not that authority.
The adapters only admit actual bounded HTTPS bytes; they never generate acceptance
or effectiveness results. SQL refusal or errors roll back the transaction; exit 0
means committed admission, not completed native team acceptance. Output excludes
connection diagnostics and credentials. Retained files after failure are not receipts.

Configure and verify store access before invoking. The authenticated owner artifact route serves strictly allowlisted release,
acceptance and corrective observations. Serving identifies bytes; it does not
authenticate their producer or admit evidence. Keep independent check production
and operator-selected provenance distinct from artifact storage. Collector files are private (0600, new directories 0700);
prove authorized serving/reviewer access without broadening permissions. The existing owner edge may retain its Basic authentication unchanged; use the
explicit Basic mode below. Do not bypass the edge.

After the existing release chain succeeds, verify the owner app actually restarted
from the immutable exact release. A moved symlink is insufficient. Commit the
release receipt before running independent production checks: SQL refuses checks
observed earlier than the receipt. Then admit their retained artifacts.

Repeated release admission preserves the first SQL receipt; a later observation
file does not replace its reference. Identical acceptance replays preserve the
original check; changed observations for the same criterion are refused. Preserve
original bytes, digest and timestamps. Current revision/SHA mismatches require
reconciliation through the canonical workflow, never forced receipt rewriting.

Keep the pilot to one real eligible canonical AICC task using the existing planner
and explicit delivery-team scope. No fake incidents, new producers, daemon, grants,
queue writer or expanded scope are supplied by this command.

## CTO enforcement switch (schema 37)

These are SQL functions, not commands of this CLI. Run them only through the trusted operator credential path described above: a non-superuser `aicc_operator` session, never app, worker, superuser or `SET ROLE`. Any other caller gets a denial or `false`, and the refusal is audited.

- `SELECT cto_activate('<fleet verification reference>')` turns enforcement on. It then applies only to delivery-team-enrolled tasks, meaning tasks with any `delivery_team_stage` row. Ordinary PR reviews, retries and never-enrolled legacy work are unaffected. Each enrolled task's work then needs its own `cto_assign` (exact task, revision, principal and payload digest).
- `SELECT cto_deactivate('<reason>')` turns enforcement off. A blank reason is refused. It deletes or revokes nothing: assignments, bindings, start decisions and audit rows are kept, and you can re-activate later.
- Each transition appends an immutable row to `cto_control_event`, which records the action, the reference or reason, the actor (`session_user`) and the time. Read the switch history there. `cto_control` holds only the current latch and the latest activation.

While enforcement is active, schema rollback (the 0037 or 0036 down migration) is refused. Deactivate with a recorded reason before any reviewed downgrade, and back up first: 0037.down drops `cto_control_event`.

## Existing Basic-auth edge

Bearer remains the default (`AICC_TEAM_AUTH_MODE=bearer`). To use an existing
Basic-auth HTTPS edge, set `AICC_TEAM_AUTH_MODE=basic` and
`AICC_TEAM_BASIC_CREDENTIALS_FILE` to its existing private JSON file containing
exactly `username` and `password` string keys. The file must be a regular file,
not a symlink, owned by the invoking identity, with no group/other permissions
(for example 0600), and at most 4096 bytes. No secret values belong on the CLI.
The reader rejects malformed/duplicate fields and invalid Basic credentials.
Private-file Basic mode requires POSIX `O_NOFOLLOW` and `geteuid`; unavailable
capabilities fail closed before opening the file. Bearer transport remains portable.

Unset both Bearer environment variables in Basic mode; even an empty conflicting
variable is refused. A Basic credential file in Bearer mode or an unknown mode
is refused. For `release`, observation and artifact origins must be the same
explicit HTTPS origin because they share this credential. Each reader still
refuses foreign origins, HTTP and redirects. Use existing edge authentication;
this option grants no new access and does not provision a producer or artifact
store. Acceptance/corrective origin selection must come from the operator's
trusted producer plan, never a model response.
