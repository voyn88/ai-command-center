---
name: AICC bounded autonomous development
alwaysApply: true
---

You are working in an existing, concurrently modified AICC repository. Prioritize correctness, preserving in-flight agent work, and compact tool context.

## Operate independently within safe scope
- Complete scoped code edits and tests without asking for confirmation at every routine step. Continue automatically where tool permissions allow; never bypass Continue or OS confirmations.
- Shipping is pre-authorized: for your own task, push a feature branch, open a PR and enable auto-merge (squash) exactly as described in `ship-to-prod.md`. Merging happens only through GitHub after required checks pass.
- Never stop/restart production services, deploy manually, bypass required checks (`--admin`), push to `main`, reset, force-push, merge other people's PRs, or alter another agent's worktree.
- Avoid changing secrets, credentials, production databases, remote configuration, and live queues.
- Do not assume `main` is up to date; inspect current branch and git status before committing. Stage only the explicitly relevant files.
- Treat existing dirty files as another worker's possible work; don't overwrite, revert, or bulk-format them.

## Keep API requests bounded
- Start with the immediate task and 1–2 relevant files; don't read entire modules if a narrow function/line range is sufficient.
- Prefer read_file, grep_search, file_glob_search, ls, and multi_edit over terminal when possible.
- Never search the whole home directory; scope paths to the current repository.
- Exclude node_modules, .git, .venv, dist, build, coverage, source maps, minified bundles, huge logs, and binaries from broad searches.
- Cap each terminal command's output at approximately 10 KB; use bounded `rg -n` patterns, `sed -n` ranges, and timeouts.
- Run one concise diagnostic at a time; avoid parallel batches of heavy file reads.
- If context becomes large, write a concise progress summary to a repo-local task note only when the task calls for it, then start a fresh Continue chat. Do not repeat the complete investigation.
- Do not paste API secrets or large logs into chat outputs.

## Implementation and release practice
- Reuse existing orchestration, persistence, deploy, and rollback mechanisms; avoid shadow implementations.
- Make minimal, testable diffs. Prefer targeted tests before broad suites.
- Preserve run IDs, queue history, idempotency, auditability, and fail-closed behavior.
- Separate unrelated changes into distinct commits/PRs; run tests and report exactly what passed.
- A successful test does not mean a change is in production. Verify branch, merged SHA, active release, and running process version separately.
- Conclude with a short summary: changed files, test results, unresolved risks, and next safe step.
