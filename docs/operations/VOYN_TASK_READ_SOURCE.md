# voyn-task as a read-only source of `backlog_task` (stage 3)

Command: `python -m command_center.db backlog-import-voyn-task [--parse-only]`.

* **Flag** `AICC_VOYN_TASK_SOURCE` (`1|true|yes|on`), default **off**. Off: the command prints
  "nothing read" and exits 0; no DSN, directory or `psql` is touched. Markdown path
  (`ops/aicc_backlog_publish.py`, `backlog-import`) is unchanged and stays authoritative
  until the reconciliation week ends.
* **Definitions**: `tasks/*.yaml` (`VOYN_TASKS_DIR`, default `./tasks`). **State/revision**:
  `voyn_coordination.task_state` via `psql`, role `voyn_task_reader`; DSN only from
  `VOYN_TASK_READER_DSN` (else `VOYN_TASK_DSN`); a URL password is moved to `PGPASSWORD`.
* **Read only**: a single `SELECT`; no claim/transition/register. Writes go only to AICC's own
  `backlog_task` through the existing `BacklogStore` path.

| voyn-task | `backlog_task.status` |
|---|---|
| OPEN / IN_PROGRESS / READY_TO_REVIEW / DONE | same |
| BLOCKED | NEEDS_REFINEMENT (non-executable, no auto-resume; body keeps `[voyn-task status: BLOCKED]`) — proposal, owner decides |

UNTRIAGED, DEFER_TO_USER, SPLIT, DECIDED are AICC triage states and are never produced.
Fields: `project` aicc/crm/aios → repo ai-command-center/voyn-logistics-crm/aios (else none);
`gate: true` or id `-G<n>` → `kind=gate`; `why`+`acceptance` → body. DB status wins; a task
without a `task_state` row takes the YAML status and is listed as UNREGISTERED.
Known difference to reconcile: Markdown `title` is the slug, YAML `title` is the Russian heading.
