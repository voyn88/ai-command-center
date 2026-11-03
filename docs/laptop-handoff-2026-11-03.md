# AICC laptop handoff — 2026-11-03

## Start here

This is a recovery checkpoint, not a release approval. The existing server deployment and PR gates remain authoritative. Do not start another writer or a second monitor.

Private recovery copies are stored on **control-01 and worker-01**, under the existing SSH account's home directory at `aicc-handoff-20261103/`. The directory is private to that account. It contains an encrypted archive, its key, checksums and a verification/restore program. Logs and working copies are intentionally not published in Git.

On either server, verify before restoring:

```sh
cd ~/aicc-handoff-20261103
sha256sum -c SHA256SUMS
python3 verify_restore.py .
python3 verify_restore.py . --extract ~/aicc-recovery-20261103
```

The restore program needs Python 3 and `cryptography`, confirmed present on both hosts. Keep the key and archive private. A new laptop still needs GitHub authentication and authorized SSH access; this checkpoint does not transfer login tokens or SSH private keys.

## What was preserved

- Twelve AICC working copies, Git objects, refs, indexes and uncommitted state.
- Local work outputs, coordinator/monitor journals, diagnostic history, SQL/diff intermediates and AICC helper scripts.
- Existing automation configuration as reference, plus Git inventory and per-file SHA-256 manifest inside the encrypted archive.
- `gb-992` contains an unfinished merge. Preserve its index and conflict state; do not treat it as reviewed or ready to push.

Reproducible virtual environments, node_modules, Python/linter/test caches, downloaded ZIP packages and the disposable PostgreSQL test installation/data/password were excluded. Other project directories were excluded. Restore dependencies normally. Do not copy credential stores into Git.

Linked Git worktrees contain old absolute Windows paths. After restoring, run `git worktree repair` from the restored `work/aicc-full` with the restored linked worktree paths. Inspect `recovery/git-inventory.json` before any reset, clean, checkout or pruning. Independent clones can be used directly after verifying their remotes. Never bulk-push archived refs.

## Continuation checkpoint

As verified on November 3 around 14:45 Moscow time:

| PR | Head | State |
|---|---|---|
| #982 | 487444843330a0bd459fa95660e881706a7ed22a | blocked; CI 41387683968 succeeded; Acceptance skipped; no exact-head review |
| #991 | 1fd1aad6b452f241283ab5dea1a29f1ae82114a0 | active; CI 41506851147 and Acceptance workflow 41506851079 succeeded; no exact-head review |

The active window held #914/#979/#990/#991/#992. #982 did not appear in sampled ticks; its exclusion cause is **unproven**. A workflow success is not an independent acceptance verdict. Re-query current SHAs and actual Linux/PostgreSQL, Windows, CodeQL/security, final gate and independent review before acting. Skipped checks are not successes.

Both servers still pointed at release `d0cbffc2a2d19e05ee9db8ab5dfb24b8be0ee66a` during handoff. No new deployment was performed by this migration. Worker service was observed `activating`; recheck health before any release.

Claude CLI is not logged in. The user clarified that login had not happened. Run `claude auth login` interactively on the intended machine, then give Claude a read-only #982/#991 diagnosis and verify its actual answer. Do not reuse expired OAuth or bypass denied service-account permissions.

## Coordination and scope

Use existing Grok Bot / AICC PR Coordinator and issue #1078. PING/ACK/PONG confirms Codex connectivity only; it does not establish a Grok reply or authorize changes. Last confirmed Grok reply was November 2 at 07:17 Moscow time. Do not send tasks through GUI, close sessions, launch old Cursor executors unchecked or create parallel writers.

Preserve migration order #1002 → #1032 → #914 → #982 → #979 (0033–0041), then #990 → #991 → #992 (0042–0050), insert_seq, queue semantics and rotation coverage. Scope also includes #693/#959/#966/#1025/#1033/#747/#969/#954. #954 has unique changes and is not a duplicate of #693.

Do not retry or route around previously denied #747/#969 operations. Do not change base or product A/B/C without the user's decision. stack.json specifies base, not push destination. Ordinary merge of main into a verified PR branch and ordinary head push are authorized. No direct push to main, force push, weakened checks/protections or label manipulation to bypass the queue.

Normal merge/release/deployment requires actual independent reviews and mandatory gates on the exact SHA, with a concrete target environment. Verify release SHA, applied migrations and control/worker health after deployment.

The existing `aicc` heartbeat is attached to the original Codex chat and depends on the desktop environment. Its saved configuration is a recovery reference, **not a server-resident replacement scheduler**. Confirm it resumes on the new laptop before retiring the old one; do not create a duplicate monitor. Continue updating `outputs/aicc-autonomous-monitor.md`, not the coordinator's status file. Notify only completion or a newly necessary user decision.
