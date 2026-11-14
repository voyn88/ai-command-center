# Systemd unit authority (VOYN-W0-AICC-UNMANAGED-LIVE-UNITS)

Every live unit on control-01 / worker-01 is either installed by the
repo-owned installer (`deploy/install-agent-principal-isolation.sh` +
`ops/aicc_install_transaction.default_specs`) or explicitly retired with a
justification. «Works, but nobody installs it» is a defect: a rebuilt host
loses the unit the moment the hand-made `/etc/systemd/system` copy is gone.

## Acceptance

1. Live unit → installer-owned **or** justified removal.
2. Deletion of any unit file requires inactivity proof on **both** hosts —
   missing installer alone is not enough (2026-08-30 Codex false positive).
3. Queue/mirror modules are deleted only after a production-import proof in a
   dedicated reversible authority PR — not as part of unit cleanup.

## Live units brought under authority by this task

| Unit | Profile | Authority |
|---|---|---|
| `voyn-aicc-self-deploy.{service,timer}` | control install + enable | `CONTROL_ONLY_UNITS` / `CONTROL_ONLY_TIMERS` |
| `voyn-aicc-self-deploy.*` on worker | justified retirement | installer `disable --now`; replaced by `voyn-aicc-worker-release-deploy` |
| `voyn-aicc-pgtunnel.service` | worker install + enable before lane rollout | worker `FileSpec` + `WORKER_ONLY_TARGETS` + `WORKER_LIVE_UNITS` |

Host-local secrets (`/etc/aicc/pgtunnel.env`, control `.env`) stay off-repo;
the installer never writes them.

## 2026-08-30 recon: what looked dead and what that meant

Codex marked 17 `deploy/systemd/*` units «dead: no installer». Live check
confirmed the installer gap, but the conclusion was inverted for units that
were already carrying production:

- `voyn-aicc-self-deploy.timer` — active/enabled on control (and leftover on
  worker) → **keep / installer-own**, do not delete.
- `voyn-aicc-pgtunnel.service` — active/enabled on worker → **keep /
  installer-own**, do not delete.

True inactivity on both hosts (`not-found` / inactive) was observed for:

- `aicc-worker.service`
- `aicc-queue-reaper.*`
- `aicc-worktree-prune.*`
- `voyn-aicc-credential-rotation*`

Inactivity is **necessary** for deletion, not sufficient. These stay in the
tree until a dedicated authority PR proves no production path needs them:

| Candidate | Why not deleted here |
|---|---|
| `aicc-worker.service` | Retired-at-runtime legacy name (`RETIRED_LEGACY_UNITS`); drop-in + tests still name the shape. |
| `aicc-queue-reaper.*` | Production caller of `queue_reap()` — unfinished control-plane deploy, same seam class as an incomplete migration. |
| `aicc-worktree-prune.*` | Production caller of `command_center.worktree_sweep` — same unfinished-deploy shape. |
| `voyn-aicc-credential-rotation*` | Documented sole supported worker rotation path; installer already stop/starts the timer; sysusers creates `aicc-rotator`. Inactive because never installer-owned, not because retired. |

## Dedup lesson (queue / mirror)

A second audit almost deleted `command_center/queue_store.py`,
`command_center/db/queue_store.py`, and `command_center/record_mirror.py`
because «only tests import them». That metric is wrong for:

- **Unfinished forward migration** — `queue_store` / `db/queue_store` are the
  PostgreSQL half of SRV-01b; `execution_queue.save_queue` still writes
  `runtime_db` directly. Deleting them abandons the migration.
- **Structural Protocol** — `record_mirror.RecordMirror` is
  `@runtime_checkable` and the fitness target of
  `tests/db/test_mirror_contract.py`; 15 production `command_center/db/*_store.py`
  stores match it structurally. Nobody imports a Protocol that is the
  *contract*.
- **Live siblings** — `command_center/db/table_mirror.py` has 15 production
  importers; `execution_queue.py` / `task_pipeline.py` are the desktop /
  Streamlit line, intentionally separate from the server
  (`db/work_queue_store.py` + `orchestrator/review_merge.py`).

Rule: import-count and installer-count are inventory signals, not deletion
proofs.
