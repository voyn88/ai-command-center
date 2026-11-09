# Cheap API verdict bench: PR #745 remediation

Task: `VOYN-W0-AICC-CHEAP-API-VERDICT-BENCH-REM`  
Reconciled: 2026-11-09  
Supersedes: PR #745 at `80485d1fa7294aa0fce02fd617844403e49ed066`

## Reconciliation

PR #745 recorded a sandbox credential blocker in the production
`command_center.openai_exec` module docstring. That conclusion is no longer a
valid closure condition. The 2026-11-04 owner reconciliation explicitly
permits the existing Groq and OpenRouter credentials and classifies this as an
acceptance/measurement task, not evidence that the bridge is absent.

The bridge remains implemented and routing remains unchanged. Promotion still
requires a chunked-envelope holdout, using the same protocol as the completed
Groq verdict bench, with per-model evidence for all of the following:

- 100% recall of the known P0/P1 findings in PRs 578, 586, and 594;
- zero hallucinated findings;
- stable repeated output at `temperature=0`.

Until those measurements exist, this task supplies no basis for promoting a
Groq, OpenRouter, DeepSeek, GLM, or Kimi model into the verdict route.

## Rejected-check remediation

The two failed contexts on PR #745 do not establish a defect in the HTTP
bridge:

- `Secret scan (gitleaks, full history)` was contaminated by a synthetic
  fixture on another fetched task branch. Main later remediated that exact
  historical occurrence with the narrow fingerprint allowlist in
  `.gitleaksignore` (commit `a8acdbbad824276dfccaf8a90641e57bf8747b86`).
- `Impact fast pre-check (advisory)` expanded the docstring-only production
  change to the bridge's transitive test set. This follow-up keeps operational
  evidence out of production code; the required full quality gate remains the
  authority for merge acceptance.

No credential value, model verdict, holdout score, or routing change is
claimed by this remediation.

Registry evidence: owner reconciliation
`69409759e409f24de6c8f94c4893b707aa7ab254a91ae42badd4b05f50c0da88`;
reconciled main `1c5c83c42173d9a12a7cdb406d4d29f0978ca131`.
