# `repo_lease_store.py` grep-cache false positive — reconciliation

Task: `VOYN-W0-AICC-GREP-CACHE-FALSE-POSITIVE`
(`stale-caches-invert-grep-results`), Wave 0, P3.
Snapshot: 2026-09-07. Prior triage note: 2026-08-20,
`backlog_triage`, see `VOYN-W0-BACKLOG-RECONCILE-ALL`.

## Reported symptom

Some agent/tool caches assert that a module named `repo_lease_store.py`
was removed from this repository, and a naive filesystem/working-tree
`grep` therefore returns a result that is the **opposite of the truth**
(it appears absent when a cache claims it should exist, or vice versa,
depending on which stale snapshot answered the query).

## Verification method

Per the task's own remediation guidance, placement was checked against
git's authoritative history, not the working tree or any external cache:

```
git grep -n "repo_lease_store" $(git rev-list --all)   # all commits, all branches
git log --all --name-only --pretty=format: | grep -i repo_lease_store
git log --all --diff-filter=D --summary -- "**repo_lease_store*"
```

## Finding

`repo_lease_store.py` does not exist, and has never existed, anywhere in
this repository's git history (no branch, no commit, no deleted/renamed
blob). It is not a predecessor name for the current lease-related
modules either — `command_center/worker/worktree_lease.py` and
`command_center/worker/lease_client.py` were both introduced directly
under their present names (confirmed with `git log --follow`), with no
rename ancestry pointing at `repo_lease_store.py`.

Conclusion: the cache claim is a pure false positive with no basis in
this repository's tracked history. No code change is required here; the
defect is external to this repo (in whatever cache/index produced the
claim).

## Guidance for future verification

To avoid propagating this class of false positive, verify file placement
or removal claims against git's source of truth, not a filesystem grep or
a cached index:

- `git grep <pattern> $(git rev-list --all)` — search file contents across
  every commit on every branch.
- `git show <ref>:<path>` — confirm a specific path's content at a
  specific commit.
- `git log --all --follow -- <path>` — trace a file's rename/removal
  history precisely.

A working-tree-only `grep`/`find` cannot distinguish "never existed" from
"removed on this branch but present on another" and must not be treated
as authoritative for repository-wide placement questions.
