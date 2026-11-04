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

Configure and verify store access before invoking. The existing owner artifact route
serves release observations only; acceptance/corrective artifacts need an approved
independent HTTPS store. Collector files are private (0600, new directories 0700);
prove authorized serving/reviewer access without broadening permissions. The
standard owner edge uses Basic auth while these readers use Bearer: verify an
already approved compatible HTTPS machine-read route. Do not bypass the edge.

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
